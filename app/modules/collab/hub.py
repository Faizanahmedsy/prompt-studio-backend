"""Live collaboration fan-out.

Two layers, and the split is the whole design:

1. **In-process rooms.** Every open websocket is held in a dict keyed by
   project. Delivering to the people editing the same diagram is then a loop
   over a list — no broker, no serialisation round-trip, no failure mode.
2. **Redis pub/sub, optional.** With more than one API worker (uvicorn
   `--workers 4`, or two containers behind a load balancer) two people editing
   the same project can land on different processes, and layer 1 alone would
   leave them invisible to each other. Each process publishes what it
   broadcasts and replays what it receives.

Redis is optional **only for a single-worker deployment**, which is already
correct without it — requiring it there would make the common case depend on a
service it does not need. Past one worker it is a requirement, not an
accelerator: without it two people on different workers cannot see each other,
and `presence_snapshot` quietly falls back to this process's own rooms, which
undercounts. `app.main` logs which mode it came up in at start-up so a deploy
can tell the difference rather than discovering it from a user report.
"""

import asyncio
import contextlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket

from app.core.constants import ProjectRole, WsMessage
from app.core.redis import get_redis
from app.core.time import now

logger = logging.getLogger("app.collab")

CHANNEL_PREFIX = "prompt-studio:project:"
PRESENCE_PREFIX = "prompt-studio:presence:"
# A presence entry outlives its socket by at most this long. The key is
# refreshed whenever anything happens on the room, and the client pings well
# inside the window, so the only rows that ever reach the timeout are the
# ones belonging to a worker that died without cleaning up.
PRESENCE_TTL_SECONDS = 120

# Application close code for "you no longer have access here". Distinct from
# 4403 at the handshake so a client can tell "you were never allowed in" from
# "your access was taken away while you were working".
WS_ACCESS_REVOKED = 4403

# Identifies this process on the bus. A message tagged with our own origin has
# already been delivered locally; replaying it would double every edit.
ORIGIN = uuid.uuid4().hex


@dataclass
class Connection:
    """One open websocket: a person, on a project, in a tab."""

    id: str
    websocket: WebSocket
    user_id: uuid.UUID
    email: str
    name: str
    accent_color: str
    role: ProjectRole
    project_id: uuid.UUID
    joined_at: float = field(default_factory=lambda: now().timestamp())

    async def send(self, message: dict[str, Any]) -> bool:
        """Deliver one message. Returns False if the socket is gone.

        Never raises: a broadcast walks every member, and one dead socket must
        not abort delivery to the rest of the room.
        """
        try:
            await self.websocket.send_json(message)
        except Exception:  # noqa: BLE001 - a closed socket is not an error here
            return False
        return True


class CollabHub:
    def __init__(self) -> None:
        self._rooms: dict[uuid.UUID, dict[str, Connection]] = {}
        self._lock = asyncio.Lock()
        self._pubsub_task: asyncio.Task[None] | None = None
        self._pubsub: Any | None = None

    # ── membership ───────────────────────────────────────────────────────────

    async def join(self, connection: Connection) -> list[dict[str, Any]]:
        """Register a socket and return who else is already here."""
        async with self._lock:
            room = self._rooms.setdefault(connection.project_id, {})
            first_in_room = not room
            room[connection.id] = connection
        if first_in_room:
            await self._subscribe(connection.project_id)
        await self._record_presence(connection)
        await self.broadcast(
            connection.project_id,
            {
                "type": WsMessage.MEMBER_JOINED,
                "member": _describe(connection),
                "at": now().isoformat(),
            },
            exclude=connection.id,
        )
        members = await self.presence_snapshot(connection.project_id)
        await self.broadcast(
            connection.project_id,
            {"type": WsMessage.PRESENCE, "members": members, "at": now().isoformat()},
            exclude=connection.id,
        )
        return members

    async def leave(self, connection: Connection) -> None:
        await self._forget_presence(connection)
        async with self._lock:
            room = self._rooms.get(connection.project_id)
            if room is not None:
                room.pop(connection.id, None)
                if not room:
                    self._rooms.pop(connection.project_id, None)
        await self.broadcast(
            connection.project_id,
            {
                "type": WsMessage.MEMBER_LEFT,
                "member": _describe(connection),
                "at": now().isoformat(),
            },
            exclude=connection.id,
        )
        await self.broadcast(
            connection.project_id,
            {
                "type": WsMessage.PRESENCE,
                "members": await self.presence_snapshot(connection.project_id),
                "at": now().isoformat(),
            },
            exclude=connection.id,
        )

    async def disconnect_user(
        self, project_id: uuid.UUID, user_id: uuid.UUID, *, local_only: bool = False
    ) -> int:
        """Close one person's sockets on one project.

        Membership is resolved once, at the handshake, and the role is then held
        on the `Connection`. Without this, removing someone from a project — or
        demoting them — does not reach a socket they already have open, and the
        fan-out keeps sending them the entire document on every save while the
        REST API correctly answers 404. That is the product's central promise
        being broken by a connection nobody thought to close.

        Re-checking membership inside the fan-out instead would mean a database
        round trip per recipient per keystroke, which is why the check lives
        here, on the rare event, rather than there, on the hot path.
        """
        closed = 0
        for connection in list(self._rooms.get(project_id, {}).values()):
            if connection.user_id != user_id:
                continue
            await self.leave(connection)
            with contextlib.suppress(Exception):
                await connection.websocket.close(
                    code=WS_ACCESS_REVOKED, reason="Your access to this project changed"
                )
            closed += 1
        if not local_only:
            # The socket may be held by a different worker entirely, which is
            # the case this whole mechanism exists for.
            await self._publish_control(project_id, {"user_id": str(user_id)})
        return closed

    def presence(self, project_id: uuid.UUID) -> list[dict[str, Any]]:
        """Who is in this room **on this worker**.

        Collapsed by user id, with a `connections` count: someone with the
        project open in three tabs is one person in the avatar stack, not three.
        """
        return _collapse(
            _describe(connection) for connection in self._rooms.get(project_id, {}).values()
        )

    async def presence_snapshot(self, project_id: uuid.UUID) -> list[dict[str, Any]]:
        """Who is in this room, across every worker.

        Presence is kept in a Redis hash keyed by project because it is the one
        piece of collaboration state a *request* has to read — `GET /presence`
        is answered by whichever worker the load balancer picked, which is very
        often not the worker holding the sockets. Broadcasting join/leave over
        pub/sub keeps open sockets in step, but it cannot answer a question
        asked from outside the room.

        Falls back to this worker's own view when Redis is absent, which is
        exactly right for a single-worker deployment.
        """
        client = await get_redis()
        if client is None:
            return self.presence(project_id)
        try:
            stored = await client.hgetall(f"{PRESENCE_PREFIX}{project_id}")
        except Exception as exc:  # noqa: BLE001 - fall back rather than 500
            logger.warning("Could not read presence: %s", exc)
            return self.presence(project_id)
        entries: list[dict[str, Any]] = []
        for raw in stored.values():
            try:
                entries.append(json.loads(raw))
            except (TypeError, ValueError):
                continue
        return _collapse(entries)

    async def _record_presence(self, connection: Connection) -> None:
        client = await get_redis()
        if client is None:
            return
        key = f"{PRESENCE_PREFIX}{connection.project_id}"
        try:
            await client.hset(key, connection.id, json.dumps(_describe(connection)))
            await client.expire(key, PRESENCE_TTL_SECONDS)
        except Exception as exc:  # noqa: BLE001 - presence is not worth a 500
            logger.warning("Could not record presence: %s", exc)

    async def touch_presence(self, connection: Connection) -> None:
        """Push the expiry out. Called on traffic, so a busy room never lapses."""
        await self._record_presence(connection)

    async def _forget_presence(self, connection: Connection) -> None:
        client = await get_redis()
        if client is None:
            return
        try:
            await client.hdel(f"{PRESENCE_PREFIX}{connection.project_id}", connection.id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not clear presence: %s", exc)

    def room_size(self, project_id: uuid.UUID) -> int:
        return len(self._rooms.get(project_id, {}))

    # ── delivery ─────────────────────────────────────────────────────────────

    async def broadcast(
        self,
        project_id: uuid.UUID,
        message: dict[str, Any],
        *,
        exclude: str | None = None,
        local_only: bool = False,
    ) -> None:
        await self._deliver_local(project_id, message, exclude=exclude)
        if not local_only:
            await self._publish(project_id, message, exclude)

    async def _deliver_local(
        self, project_id: uuid.UUID, message: dict[str, Any], *, exclude: str | None
    ) -> None:
        room = list(self._rooms.get(project_id, {}).values())
        dead: list[Connection] = []
        for connection in room:
            if connection.id == exclude:
                continue
            if not await connection.send(message):
                dead.append(connection)
        for connection in dead:
            # Reaped WITHOUT going through `leave`. `leave` broadcasts, and a
            # broadcast re-enters this function — so a room where n sockets died
            # together nested n deep and sent n(n+1)/2 frames; at ~330 it hit
            # RecursionError, which the listener's catch-all swallowed, silently
            # ending cross-worker collaboration until some unrelated project
            # happened to restart it.
            #
            # Reaping at all is still necessary: a browser that vanished (laptop
            # lid, dropped tunnel) may never run the disconnect path, and a
            # ghost in the presence list is worse than a late departure notice.
            await self._forget_presence(connection)
            async with self._lock:
                room_map = self._rooms.get(connection.project_id)
                if room_map is not None:
                    room_map.pop(connection.id, None)
                    if not room_map:
                        self._rooms.pop(connection.project_id, None)
        if dead:
            # One roster afterwards, rather than one departure notice each.
            await self.broadcast(
                project_id,
                {
                    "type": WsMessage.PRESENCE,
                    "members": await self.presence_snapshot(project_id),
                    "at": now().isoformat(),
                },
            )

    # ── cross-worker bus ─────────────────────────────────────────────────────

    async def _publish(
        self, project_id: uuid.UUID, message: dict[str, Any], exclude: str | None
    ) -> None:
        client = await get_redis()
        if client is None:
            return
        try:
            await client.publish(
                f"{CHANNEL_PREFIX}{project_id}",
                json.dumps({"origin": ORIGIN, "exclude": exclude, "message": message}, default=str),
            )
        except Exception as exc:  # noqa: BLE001 - the local room already has it
            logger.warning("Could not publish collaboration message: %s", exc)

    async def _publish_control(self, project_id: uuid.UUID, payload: dict[str, Any]) -> None:
        """Send an instruction, rather than a message to relay, to the other
        workers holding sockets on this project."""
        client = await get_redis()
        if client is None:
            return
        try:
            await client.publish(
                f"{CHANNEL_PREFIX}{project_id}",
                json.dumps({"origin": ORIGIN, "control": "access.revoked", **payload}),
            )
        except Exception as exc:  # noqa: BLE001 - the local room is already handled
            logger.warning("Could not publish an eviction: %s", exc)

    async def _subscribe(self, project_id: uuid.UUID) -> None:
        client = await get_redis()
        if client is None:
            return
        if self._pubsub is None:
            self._pubsub = client.pubsub(ignore_subscribe_messages=True)
        await self._pubsub.subscribe(f"{CHANNEL_PREFIX}{project_id}")
        if self._pubsub_task is None or self._pubsub_task.done():
            self._pubsub_task = asyncio.create_task(self._listen())

    async def _listen(self) -> None:
        """Replay messages published by the other workers into local rooms."""
        assert self._pubsub is not None
        try:
            async for raw in self._pubsub.listen():
                if raw.get("type") != "message":
                    continue
                try:
                    payload = json.loads(raw["data"])
                except (TypeError, ValueError):
                    continue
                if payload.get("origin") == ORIGIN:
                    continue  # already delivered locally by whoever published it
                channel = str(raw.get("channel", ""))
                try:
                    project_id = uuid.UUID(channel.rsplit(":", 1)[-1])
                except ValueError:
                    continue
                if payload.get("control") == "access.revoked":
                    try:
                        target = uuid.UUID(str(payload.get("user_id")))
                    except ValueError:
                        continue
                    # `local_only`, or the two workers publish evictions at each
                    # other forever.
                    await self.disconnect_user(project_id, target, local_only=True)
                    continue
                await self._deliver_local(
                    project_id, payload.get("message", {}), exclude=payload.get("exclude")
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the local room keeps working
            logger.warning("Collaboration bus stopped: %s", exc)

    async def shutdown(self) -> None:
        if self._pubsub_task is not None:
            self._pubsub_task.cancel()
            with contextlib.suppress(Exception):
                await self._pubsub_task
            self._pubsub_task = None
        if self._pubsub is not None:
            await self._pubsub.aclose()
            self._pubsub = None


def _collapse(entries: Any) -> list[dict[str, Any]]:
    """One row per person, with a count of how many tabs they have open."""
    people: dict[str, dict[str, Any]] = {}
    for entry in entries:
        user_id = str(entry.get("user_id"))
        existing = people.get(user_id)
        if existing is None:
            people[user_id] = {**entry, "connections": 1}
        else:
            existing["connections"] += 1
    return list(people.values())


def _describe(connection: Connection) -> dict[str, Any]:
    return {
        "user_id": str(connection.user_id),
        "email": connection.email,
        "name": connection.name,
        "accent_color": connection.accent_color,
        "role": connection.role.value,
        "connection_id": connection.id,
    }


hub = CollabHub()
