"""The collaboration websocket.

One socket per open editor. It carries three kinds of traffic:

- **document saves**, which are persisted and given an authoritative version
  before anyone is told about them;
- **ephemeral presence** (cursors, selections), which is relayed and never
  stored — it is worthless a second later;
- **housekeeping** (hello, ping/pong, join/leave).

Concurrency model is last-write-wins over the whole document, guarded by a
version number. That is the honest match for how the editor works: the client
holds one Zustand document and rewrites it wholesale, so there are no
character-level operations to transform. A save whose `base_version` is behind
is rejected with the current document attached, and the client reconciles.
"""

import asyncio
import contextlib
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ProjectRole, WsMessage
from app.core.database import AsyncSessionLocal
from app.core.exceptions import AppError
from app.core.time import now
from app.modules.auth import service as auth_service
from app.modules.auth.dependencies import authenticate_websocket
from app.modules.collab.hub import Connection, hub
from app.modules.projects import service as project_service
from app.modules.projects.access import resolve_access
from app.modules.projects.schemas import ProjectDocSave

logger = logging.getLogger("app.collab")

router = APIRouter(tags=["Collaboration"])

# Close codes. 4000-4999 is the range reserved for applications, so a browser
# can tell "you were signed out" from "the server restarted" (1012) without
# parsing a reason string.
WS_UNAUTHORISED = 4401
WS_FORBIDDEN = 4403
WS_NOT_FOUND = 4404
WS_BAD_MESSAGE = 4400

# A document snapshot is taken at most this often per socket. Saves themselves
# are not throttled — every one is persisted — but the undo history only needs
# to be coarse enough to be useful.
SNAPSHOT_INTERVAL_SECONDS = 90

# How often an idle socket re-checks that it is still allowed to be here.
#
# A websocket authenticates once, at the handshake, and then lives for as long
# as the tab is open. Every immediate eviction path — a membership change, an
# account deactivation — is a push, and a push only reaches the paths someone
# remembered to wire up. This is the pull: three indexed queries per socket
# every half minute, which bounds *every* revocation, including the ones nobody
# thought of, to thirty seconds instead of "until they close the tab".
REVALIDATE_SECONDS = 30


@router.websocket("/ws/projects/{project_id}")
async def collaborate(
    websocket: WebSocket,
    project_id: uuid.UUID,
    token: str = Query(description="An access token — the same one the REST API takes"),
) -> None:
    # Accepted before authentication so a rejection can carry a code the client
    # can act on. Closing during the handshake gives the browser a bare 1006
    # ("abnormal closure"), which is indistinguishable from a dropped network.
    await websocket.accept()

    authenticated = await authenticate_websocket(token)
    if authenticated is None:
        await _close(websocket, WS_UNAUTHORISED, "Your session has expired")
        return
    user, claims = authenticated
    access_jti = str(claims.get("jti", ""))

    async with AsyncSessionLocal() as db:
        try:
            access = await resolve_access(db, project_id, user, ProjectRole.VIEWER)
        except AppError as exc:
            code = WS_NOT_FOUND if exc.status_code == 404 else WS_FORBIDDEN
            await _close(websocket, code, exc.message)
            return
        role = access.role
        doc = access.project.doc
        doc_version = access.project.doc_version
        access.member.last_opened_at = now()
        await db.commit()

    connection = Connection(
        id=uuid.uuid4().hex,
        websocket=websocket,
        user_id=user.id,
        email=user.email,
        name=user.display_name,
        accent_color=user.accent_color,
        role=role,
        project_id=project_id,
    )

    last_snapshot = 0.0
    try:
        # Inside the try, deliberately. `join` writes to Redis, broadcasts twice
        # and publishes; a client that disappears during any of that used to
        # leave a dead Connection — and its WebSocket — pinned in the room dict
        # with nothing left to run `leave`.
        members = await hub.join(connection)
        await websocket.send_json(
            {
                "type": WsMessage.HELLO,
                "connection_id": connection.id,
                "you": {
                    "user_id": str(user.id),
                    "email": user.email,
                    "name": user.display_name,
                    "accent_color": user.accent_color,
                    "role": role.value,
                    "can_edit": role.at_least(ProjectRole.EDITOR),
                },
                # The document travels with the greeting so a joining editor is
                # up to date without a second HTTP round trip that could race an
                # edit arriving in between.
                "doc": doc,
                "doc_version": doc_version,
                "members": members,
            }
        )

        # A deadline, not a fixed timeout. With a timeout, a client that pings
        # every 25 seconds resets the wait each time and the next check lands at
        # 50 seconds rather than 30 — so the busier the tab, the longer a
        # revoked session survives, which is exactly backwards.
        deadline = now().timestamp() + REVALIDATE_SECONDS
        while True:
            remaining = max(0.0, deadline - now().timestamp())
            try:
                message = await asyncio.wait_for(websocket.receive_json(), timeout=remaining)
            except TimeoutError:
                message = None

            if now().timestamp() >= deadline:
                if not await _still_authorised(user.id, access_jti, project_id):
                    await _close(websocket, WS_UNAUTHORISED, "Your access changed")
                    return
                deadline = now().timestamp() + REVALIDATE_SECONDS

            if message is None:
                continue
            if not isinstance(message, dict):
                await _error(websocket, "Expected a JSON object")
                continue
            kind = message.get("type")

            if kind == WsMessage.PING:
                # The ping doubles as the presence heartbeat: the shared entry
                # has a TTL so a worker that dies cannot leave a ghost in the
                # avatar stack forever, and this is what keeps a live but idle
                # tab from expiring inside that window.
                await hub.touch_presence(connection)
                await websocket.send_json({"type": WsMessage.PONG, "at": now().isoformat()})

            elif kind in (WsMessage.CURSOR, WsMessage.SELECTION):
                # Relayed untouched and never stored. Attaching the identity
                # server-side rather than trusting the client's copy is what
                # stops one member drawing a cursor labelled as someone else.
                await hub.broadcast(
                    project_id,
                    {
                        "type": kind,
                        "user_id": str(user.id),
                        "connection_id": connection.id,
                        "accent_color": user.accent_color,
                        "name": user.display_name,
                        "payload": message.get("payload"),
                    },
                    exclude=connection.id,
                )

            elif kind == WsMessage.DOC_UPDATE:
                # No role pre-check here. `role` is whatever it was when the
                # socket opened, so someone promoted mid-session was told their
                # access was read-only while the identical REST call succeeded.
                # `_apply_update` re-resolves access against the database on
                # every save and answers a 403 with the same error frame, so it
                # is the single authority — and a demotion is still enforced.
                last_snapshot = await _apply_update(
                    websocket,
                    connection,
                    user.id,
                    access_jti,
                    project_id,
                    message,
                    last_snapshot,
                )

            else:
                await _error(websocket, f"Unknown message type: {kind!r}")

    except WebSocketDisconnect:
        pass
    except (ValueError, TypeError, KeyError):
        # `receive_json` on a frame that is not JSON text. KeyError is the
        # binary-frame case — Starlette looks for "text" in the message and a
        # binary frame does not have it — and without it the client got a bare
        # 1006 instead of a close code naming the problem.
        await _close(websocket, WS_BAD_MESSAGE, "Malformed message")
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - never leave the room holding a dead socket
        logger.exception("Collaboration socket failed for project %s", project_id)
    finally:
        await hub.leave(connection)


async def _apply_update(
    websocket: WebSocket,
    connection: Connection,
    user_id: uuid.UUID,
    access_jti: str,
    project_id: uuid.UUID,
    message: dict[str, Any],
    last_snapshot: float,
) -> float:
    """Persist one document save and tell the room. Returns the snapshot clock."""
    doc = message.get("doc")
    if not isinstance(doc, dict):
        await _error(websocket, "`doc` must be an object")
        return last_snapshot

    stamp = now().timestamp()
    take_snapshot = stamp - last_snapshot > SNAPSHOT_INTERVAL_SECONDS

    try:
        save = ProjectDocSave(
            doc=doc,
            base_version=message.get("base_version"),
            schema_version=message.get("schema_version"),
        )
    except PydanticValidationError:
        # Built before the session is opened, and answered with an error frame.
        # `ValidationError` is a `ValueError`, so left to propagate it was caught
        # by the loop's malformed-message handler and closed the connection —
        # dropping the person out of everyone's avatar stack over a bad field,
        # and straight into a reconnect loop that did it again.
        await _error(websocket, "That save was not in a shape the server accepts")
        return last_snapshot

    async with AsyncSessionLocal() as db:
        # The revalidation tick bounds a *read* to thirty seconds; a write has
        # to be refused the moment the session behind it is gone. This is one
        # indexed lookup on a path the client already debounces.
        if access_jti and await auth_service.is_token_revoked(db, access_jti):
            await _close(websocket, WS_UNAUTHORISED, "Your session has ended")
            return last_snapshot
        user = await _reload_user(db, user_id)
        if user is None:
            await _close(websocket, WS_UNAUTHORISED, "Your account is no longer active")
            return last_snapshot
        try:
            access = await resolve_access(db, project_id, user, ProjectRole.EDITOR)
            project = await project_service.save_doc(
                db,
                access,
                user,
                save,
                snapshot=take_snapshot,
                # The feed would otherwise gain a "saved changes" line every few
                # seconds and become unreadable. Autosaves are visible as the
                # version number moving; the feed records the things people did.
                activity=False,
            )
        except AppError as exc:
            # Release the FOR UPDATE before writing to the socket. The lock is
            # held from the version check, and `send_json` to a stalled peer is
            # unbounded — one client in a conflict loop would serialise every
            # save on the project and walk the connection pool.
            await db.rollback()
            if exc.status_code == 409:
                # Stale save. The current document goes back with the refusal so
                # the client can reconcile without a second request.
                fresh = await _current_doc(db, project_id)
                await websocket.send_json(
                    {
                        "type": WsMessage.DOC_CONFLICT,
                        "message": exc.message,
                        "doc": fresh[0],
                        "doc_version": fresh[1],
                        "sent_version": message.get("base_version"),
                    }
                )
            else:
                await _error(websocket, exc.message)
            return last_snapshot

        payload = {
            "type": WsMessage.DOC_UPDATED,
            "doc": project.doc,
            "doc_version": project.doc_version,
            "by": {
                "user_id": str(user.id),
                "name": user.display_name,
                "accent_color": user.accent_color,
            },
            "at": now().isoformat(),
        }

    # Everyone else gets the document; the sender gets a `doc.ack` carrying the
    # authoritative version so its next save has the right base — and NOT the
    # document it just sent, which would clobber whatever the person typed
    # while the round trip was in flight.
    await hub.broadcast(project_id, payload, exclude=connection.id)
    await websocket.send_json(
        {"type": WsMessage.DOC_ACK, "doc_version": payload["doc_version"], "ack": True}
    )
    return stamp if take_snapshot else last_snapshot


async def _still_authorised(user_id: uuid.UUID, access_jti: str, project_id: uuid.UUID) -> bool:
    """Is this socket still entitled to be in this room?

    Asks all three questions the handshake asked — is the token revoked, is the
    account live, is this person still a member — because any one of them can
    change while a socket sits open, and only some of them have a push path.
    """
    async with AsyncSessionLocal() as db:
        if access_jti and await auth_service.is_token_revoked(db, access_jti):
            return False
        user = await _reload_user(db, user_id)
        if user is None:
            return False
        try:
            await resolve_access(db, project_id, user, ProjectRole.VIEWER)
        except AppError:
            return False
    return True


async def _reload_user(db: AsyncSession, user_id: uuid.UUID) -> Any:
    from app.modules.users.models import User

    user = await db.get(User, user_id)
    if user is None or not user.is_active or user.is_deleted:
        return None
    return user


async def _current_doc(db: AsyncSession, project_id: uuid.UUID) -> tuple[dict[str, Any], int]:
    from app.modules.projects.models import Project

    project = await db.get(Project, project_id)
    if project is None:
        return {}, 0
    return project.doc, project.doc_version


async def _error(websocket: WebSocket, message: str) -> None:
    # A socket that has already gone is not an error worth raising: the caller
    # is usually in the middle of tidying up after that exact fact.
    with contextlib.suppress(Exception):
        await websocket.send_json({"type": WsMessage.ERROR, "message": message})


async def _close(websocket: WebSocket, code: int, reason: str) -> None:
    try:
        await websocket.send_json({"type": WsMessage.ERROR, "message": reason})
        await websocket.close(code=code, reason=reason)
    except Exception:  # noqa: BLE001 - closing a dead socket is not an error
        pass
