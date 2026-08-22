"""The collaboration websocket, against a real server with two real clients."""

import asyncio
import json
from typing import Any

import httpx
import pytest
import websockets

from tests.conftest import API, open_socket, register, sample_doc, unwrap

pytestmark = pytest.mark.usefixtures("live_server")


async def receive(socket: Any, timeout: float = 5.0) -> dict[str, Any]:
    raw = await asyncio.wait_for(socket.recv(), timeout)
    parsed: dict[str, Any] = json.loads(raw)
    return parsed


async def receive_of_type(socket: Any, kind: str, timeout: float = 5.0) -> dict[str, Any]:
    """Wait for one specific message, skipping whatever else arrives first.

    Presence chatter (`member.joined`, cursors) is interleaved with document
    traffic by design, so a test that assumed the very next frame was the one
    it wanted would be flaky for a reason that is not a bug.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise TimeoutError(f"never received {kind!r}")
        message = await receive(socket, remaining)
        if message.get("type") == kind:
            return message


async def make_project(client: httpx.AsyncClient, owner, guest_email: str | None = None) -> dict:  # type: ignore[no-untyped-def]
    project = unwrap(
        await client.post(
            f"{API}/projects", headers=owner.headers, json={"name": "Live", "doc": sample_doc(2)}
        )
    )
    if guest_email:
        await client.post(
            f"{API}/projects/{project['id']}/members",
            headers=owner.headers,
            json={"email": guest_email, "role": "EDITOR"},
        )
    return project


async def test_hello_carries_the_document_and_the_room(
    client: httpx.AsyncClient, live_server: str
) -> None:
    owner = await register(client)
    project = await make_project(client, owner)

    async with open_socket(live_server, project["id"], owner.access_token) as socket:
        hello = await receive(socket)
        assert hello["type"] == "hello"
        assert hello["doc_version"] == project["doc_version"]
        assert len(hello["doc"]["screens"]) == 2
        assert hello["you"]["can_edit"] is True
        assert hello["members"] == [] or hello["members"][0]["email"] == owner.email


async def test_an_edit_reaches_the_other_person(
    client: httpx.AsyncClient, live_server: str
) -> None:
    owner = await register(client)
    guest = await register(client)
    project = await make_project(client, owner, guest.email)

    async with open_socket(live_server, project["id"], owner.access_token) as a:
        await receive(a)  # hello
        async with open_socket(live_server, project["id"], guest.access_token) as b:
            hello = await receive(b)
            joined = await receive_of_type(a, "member.joined")
            assert joined["member"]["email"] == guest.email

            await b.send(
                json.dumps(
                    {
                        "type": "doc.update",
                        "doc": sample_doc(5),
                        "base_version": hello["doc_version"],
                    }
                )
            )
            relayed = await receive_of_type(a, "doc.updated")
            assert len(relayed["doc"]["screens"]) == 5
            assert relayed["doc_version"] == hello["doc_version"] + 1
            assert relayed["by"]["name"] == guest.user["full_name"]

            ack = await receive_of_type(b, "doc.ack")
            assert ack["ack"] is True
            assert ack["doc_version"] == hello["doc_version"] + 1
            # The sender is told the version, never handed its own document back
            # — that would clobber whatever they typed in the meantime.
            assert "doc" not in ack

        left = await receive_of_type(a, "member.left")
        assert left["member"]["email"] == guest.email

    # ...and it was actually persisted, not just relayed.
    saved = unwrap(await client.get(f"{API}/projects/{project['id']}", headers=owner.headers))
    assert len(saved["doc"]["screens"]) == 5


async def test_a_stale_socket_save_is_told_to_reconcile(
    client: httpx.AsyncClient, live_server: str
) -> None:
    owner = await register(client)
    project = await make_project(client, owner)

    async with open_socket(live_server, project["id"], owner.access_token) as socket:
        hello = await receive(socket)
        await socket.send(
            json.dumps(
                {"type": "doc.update", "doc": sample_doc(3), "base_version": hello["doc_version"]}
            )
        )
        await receive_of_type(socket, "doc.ack")

        await socket.send(
            json.dumps(
                {"type": "doc.update", "doc": sample_doc(1), "base_version": hello["doc_version"]}
            )
        )
        conflict = await receive_of_type(socket, "doc.conflict")
        assert conflict["sent_version"] == hello["doc_version"]
        assert conflict["doc_version"] == hello["doc_version"] + 1
        # The current document comes back with the refusal, so the client can
        # reconcile without a second request.
        assert len(conflict["doc"]["screens"]) == 3


async def test_a_viewer_may_watch_but_not_write(
    client: httpx.AsyncClient, live_server: str
) -> None:
    owner = await register(client)
    guest = await register(client)
    project = await make_project(client, owner)
    await client.post(
        f"{API}/projects/{project['id']}/members",
        headers=owner.headers,
        json={"email": guest.email, "role": "VIEWER"},
    )

    async with open_socket(live_server, project["id"], guest.access_token) as socket:
        hello = await receive(socket)
        assert hello["you"]["can_edit"] is False
        await socket.send(json.dumps({"type": "doc.update", "doc": sample_doc(9)}))
        error = await receive_of_type(socket, "error")
        # The message comes from the real authorization check, re-run against
        # the database on every save — not from a role frozen at handshake time.
        assert "does not allow" in error["message"]

    unchanged = unwrap(await client.get(f"{API}/projects/{project['id']}", headers=owner.headers))
    assert len(unchanged["doc"]["screens"]) == 2


async def test_cursors_are_relayed_with_a_server_set_identity(
    client: httpx.AsyncClient, live_server: str
) -> None:
    """The name on a cursor comes from the token, never from the message —
    otherwise one member could draw a cursor labelled as someone else."""
    owner = await register(client, full_name="Owner Person")
    guest = await register(client)
    project = await make_project(client, owner, guest.email)

    async with open_socket(live_server, project["id"], owner.access_token) as a:
        await receive(a)
        async with open_socket(live_server, project["id"], guest.access_token) as b:
            await receive(b)
            await receive_of_type(a, "member.joined")
            await a.send(
                json.dumps(
                    {"type": "cursor", "payload": {"x": 12, "y": 34}, "name": "Someone Else"}
                )
            )
            cursor = await receive_of_type(b, "cursor")
            assert cursor["name"] == "Owner Person"
            assert cursor["user_id"] == owner.id
            assert cursor["payload"] == {"x": 12, "y": 34}


async def test_presence_collapses_a_persons_tabs(
    client: httpx.AsyncClient, live_server: str
) -> None:
    owner = await register(client)
    project = await make_project(client, owner)

    async with open_socket(live_server, project["id"], owner.access_token) as a:
        await receive(a)
        async with open_socket(live_server, project["id"], owner.access_token) as b:
            await receive(b)
            await asyncio.sleep(0.3)
            # Asked of the LIVE server, not the in-process app: presence is
            # shared through Redis, and this asserts the sharing works — the
            # sockets are held by a different process entirely.
            async with httpx.AsyncClient(base_url=f"http://{live_server}", timeout=10) as remote:
                presence = unwrap(
                    await remote.get(
                        f"{API}/projects/{project['id']}/presence", headers=owner.headers
                    )
                )
            # Two sockets, one person — an avatar stack should not show them twice.
            assert len(presence) == 1
            assert presence[0]["connections"] == 2

            # And now the part that only works because presence is shared: ask
            # THIS process, which holds no sockets at all. Before presence moved
            # into Redis this returned an empty list, which is what a second
            # uvicorn worker would have served in production.
            cross_worker = unwrap(
                await client.get(f"{API}/projects/{project['id']}/presence", headers=owner.headers)
            )
            assert len(cross_worker) == 1, "presence must survive crossing a worker"
            assert cross_worker[0]["email"] == owner.email


async def test_an_outsider_is_refused(client: httpx.AsyncClient, live_server: str) -> None:
    owner = await register(client)
    outsider = await register(client)
    project = await make_project(client, owner)

    async with open_socket(live_server, project["id"], outsider.access_token) as socket:
        first = await receive(socket)
        assert first["type"] == "error"


async def test_a_bad_token_is_refused(client: httpx.AsyncClient, live_server: str) -> None:
    owner = await register(client)
    project = await make_project(client, owner)

    async with open_socket(live_server, project["id"], "not-a-token") as socket:
        first = await receive(socket)
        assert first["type"] == "error"
        assert "expired" in first["message"] or "session" in first["message"].lower()


async def test_ping_pong_and_unknown_messages(client: httpx.AsyncClient, live_server: str) -> None:
    owner = await register(client)
    project = await make_project(client, owner)

    async with open_socket(live_server, project["id"], owner.access_token) as socket:
        await receive(socket)
        await socket.send(json.dumps({"type": "ping"}))
        assert (await receive_of_type(socket, "pong"))["at"]

        await socket.send(json.dumps({"type": "nonsense"}))
        error = await receive_of_type(socket, "error")
        assert "Unknown message type" in error["message"]

        # A malformed doc.update is answered, not fatal — the socket survives.
        await socket.send(json.dumps({"type": "doc.update", "doc": "not an object"}))
        assert "must be an object" in (await receive_of_type(socket, "error"))["message"]
        await socket.send(json.dumps({"type": "ping"}))
        assert (await receive_of_type(socket, "pong"))["at"]


async def test_a_roster_frame_follows_every_join_and_leave(
    client: httpx.AsyncClient, live_server: str
) -> None:
    """`member.joined` says what changed; `presence` says what the room now is.

    A client that only accumulated the deltas would drift the first time it
    missed one — a reconnect, a dropped frame, a member on another worker.
    """
    owner = await register(client)
    guest = await register(client)
    project = await make_project(client, owner, guest.email)

    async with open_socket(live_server, project["id"], owner.access_token) as a:
        await receive(a)
        async with open_socket(live_server, project["id"], guest.access_token) as b:
            await receive(b)
            roster = await receive_of_type(a, "presence")
            assert {member["email"] for member in roster["members"]} == {owner.email, guest.email}
        after = await receive_of_type(a, "presence")
        assert [member["email"] for member in after["members"]] == [owner.email]


async def test_a_removed_member_is_disconnected(
    client: httpx.AsyncClient, live_server: str
) -> None:
    """The product's central promise, on the one route that streams documents.

    Membership is resolved once, when the socket opens. Nothing used to close a
    connection when the membership behind it was deleted, so a removed member
    kept receiving the entire document on every save while the REST API
    correctly answered 404.
    """
    owner = await register(client)
    guest = await register(client)
    project = await make_project(client, owner, guest.email)
    members = unwrap(
        await client.get(f"{API}/projects/{project['id']}/members", headers=owner.headers)
    )
    guest_row = next(m for m in members if m["email"] == guest.email)

    async with open_socket(live_server, project["id"], guest.access_token) as socket:
        await receive(socket)

        removed = await client.delete(
            f"{API}/projects/{project['id']}/members/{guest_row['id']}", headers=owner.headers
        )
        assert removed.status_code == 200

        # The socket must close rather than keep streaming.
        with pytest.raises((websockets.exceptions.ConnectionClosed, TimeoutError)):
            for _ in range(20):
                await receive(socket, timeout=1.0)

    # And it cannot simply be reopened.
    async with open_socket(live_server, project["id"], guest.access_token) as denied:
        assert (await receive(denied))["type"] == "error"


async def test_a_demoted_editor_is_disconnected(
    client: httpx.AsyncClient, live_server: str
) -> None:
    """Same reasoning, one step milder: the role travels with the connection,
    so taking it away has to reach the connection."""
    owner = await register(client)
    guest = await register(client)
    project = await make_project(client, owner, guest.email)
    members = unwrap(
        await client.get(f"{API}/projects/{project['id']}/members", headers=owner.headers)
    )
    guest_row = next(m for m in members if m["email"] == guest.email)

    async with open_socket(live_server, project["id"], guest.access_token) as socket:
        hello = await receive(socket)
        assert hello["you"]["can_edit"] is True

        await client.patch(
            f"{API}/projects/{project['id']}/members/{guest_row['id']}",
            headers=owner.headers,
            json={"role": "VIEWER"},
        )
        with pytest.raises((websockets.exceptions.ConnectionClosed, TimeoutError)):
            for _ in range(20):
                await receive(socket, timeout=1.0)


async def test_a_promoted_viewer_can_save_without_reconnecting(
    client: httpx.AsyncClient, live_server: str
) -> None:
    """The other direction must NOT disconnect — and must start working.

    The role was frozen at handshake, so a promoted member was told their
    access was read-only while the identical REST call succeeded: a silently
    broken editor with no explanation.
    """
    owner = await register(client)
    guest = await register(client)
    project = await make_project(client, owner)
    await client.post(
        f"{API}/projects/{project['id']}/members",
        headers=owner.headers,
        json={"email": guest.email, "role": "VIEWER"},
    )
    members = unwrap(
        await client.get(f"{API}/projects/{project['id']}/members", headers=owner.headers)
    )
    guest_row = next(m for m in members if m["email"] == guest.email)

    async with open_socket(live_server, project["id"], guest.access_token) as socket:
        hello = await receive(socket)
        assert hello["you"]["can_edit"] is False

        await client.patch(
            f"{API}/projects/{project['id']}/members/{guest_row['id']}",
            headers=owner.headers,
            json={"role": "EDITOR"},
        )
        await socket.send(json.dumps({"type": "doc.update", "doc": sample_doc(4)}))
        acknowledged = await receive_of_type(socket, "doc.ack")
        assert acknowledged["ack"] is True

    saved = unwrap(await client.get(f"{API}/projects/{project['id']}", headers=owner.headers))
    assert len(saved["doc"]["screens"]) == 4


async def test_an_issued_password_cannot_open_a_socket(
    client: httpx.AsyncClient, live_server: str
) -> None:
    """The issued-password wall lives in the HTTP dependency, which a socket
    never passes through — so the one route that reads and writes the whole
    document was the one route it did not cover."""
    from tests.conftest import make_superadmin, sign_in

    boss = await register(client)
    await make_superadmin(boss.email)
    boss = await sign_in(client, boss.email, boss.password)

    created = unwrap(
        await client.post(
            f"{API}/admin/users", headers=boss.headers, json={"email": "issued@example.com"}
        )
    )
    flagged = await sign_in(client, created["email"], created["temporary_password"])

    project = unwrap(
        await client.post(
            f"{API}/projects", headers=boss.headers, json={"name": "Walled", "doc": sample_doc(1)}
        )
    )
    await client.post(
        f"{API}/projects/{project['id']}/members",
        headers=boss.headers,
        json={"email": created["email"], "role": "EDITOR"},
    )

    # 403 over HTTP...
    assert (
        await client.get(f"{API}/projects/{project['id']}", headers=flagged.headers)
    ).status_code == 403
    # ...and refused on the socket too, rather than handed the whole document.
    async with open_socket(live_server, project["id"], flagged.access_token) as socket:
        first = await receive(socket)
        assert first["type"] == "error"


async def test_a_socket_is_re_checked_while_it_is_open(
    client: httpx.AsyncClient, live_server: str
) -> None:
    """A websocket authenticates once and then lives for hours.

    Every immediate eviction is a *push*, and a push only reaches the paths
    somebody remembered to wire up. This is the pull: signing out everywhere
    does not call into the hub at all, and the socket must still close.
    """
    owner = await register(client)
    project = await make_project(client, owner)

    async with open_socket(live_server, project["id"], owner.access_token) as socket:
        await receive(socket)
        await client.post(f"{API}/auth/logout-everywhere", headers=owner.headers)

        # Well inside REVALIDATE_SECONDS + the client's own silence.
        with pytest.raises((websockets.exceptions.ConnectionClosed, TimeoutError)):
            for _ in range(45):
                await receive(socket, timeout=1.0)


async def test_a_revoked_session_cannot_write_through_its_socket(
    client: httpx.AsyncClient, live_server: str
) -> None:
    """Throwing a device out has to reach the editor it has open.

    The revalidation tick bounds a *read* to thirty seconds; a write has to be
    refused at once, or the person you just signed out gets one more save in.
    """
    from tests.conftest import sign_in

    owner = await register(client)
    intruder = await sign_in(client, owner.email, owner.password)
    project = await make_project(client, owner)

    async with open_socket(live_server, project["id"], intruder.access_token) as socket:
        await receive(socket)

        listed = unwrap(await client.get(f"{API}/auth/sessions", headers=owner.headers))
        theirs = next(row for row in listed if not row["is_current"])
        await client.delete(f"{API}/auth/sessions/{theirs['id']}", headers=owner.headers)

        await socket.send(json.dumps({"type": "doc.update", "doc": sample_doc(9)}))
        with pytest.raises((websockets.exceptions.ConnectionClosed, TimeoutError)):
            for _ in range(45):
                frame = await receive(socket, timeout=1.0)
                assert frame.get("type") != "doc.ack", "a revoked session saved the document"

    # The owner is still signed in, and the document is untouched.
    unchanged = unwrap(await client.get(f"{API}/projects/{project['id']}", headers=owner.headers))
    assert len(unchanged["doc"]["screens"]) == 2


async def test_a_bad_save_payload_does_not_close_the_socket(
    client: httpx.AsyncClient, live_server: str
) -> None:
    """A pydantic `ValidationError` is a `ValueError`, so it used to be caught
    by the malformed-message handler and tear the connection down — dropping
    the person out of everyone's avatar stack over one bad field, and into a
    reconnect loop that did it again."""
    owner = await register(client)
    project = await make_project(client, owner)

    async with open_socket(live_server, project["id"], owner.access_token) as socket:
        await receive(socket)
        await socket.send(json.dumps({"type": "doc.update", "doc": {"screens": "not a list"}}))
        error = await receive_of_type(socket, "error")
        assert "shape" in error["message"] or "accept" in error["message"]

        # Still alive, and still able to save something valid.
        await socket.send(json.dumps({"type": "doc.update", "doc": sample_doc(3)}))
        assert (await receive_of_type(socket, "doc.ack"))["ack"] is True
