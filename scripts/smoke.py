"""End-to-end smoke test against a RUNNING server.

Not a replacement for `pytest` — that suite is the real one, and it runs against
its own throwaway database. This answers the question pytest cannot: *is the
thing I just deployed actually working*, over a real socket, with a real
browser-shaped client? It registers accounts, shares a project by email, edits it
from two websockets at once, and exercises the admin surface.

    uv run python scripts/smoke.py
    API=https://api.example.com uv run python scripts/smoke.py

It writes real rows and cannot clean up after itself. Point it at a throwaway
environment, never at production.
"""

import asyncio
import json
import os
import sys
import uuid
from typing import Any

import httpx
import websockets

ROOT = os.environ.get("API", "http://127.0.0.1:8010").rstrip("/")
BASE = f"{ROOT}/api/v1"
WS = BASE.replace("https://", "wss://").replace("http://", "ws://")
SUPERADMIN = os.environ.get("SUPERADMIN_EMAIL", "faizan@promptstudio.app")
SUPERADMIN_PASSWORD = os.environ.get("SUPERADMIN_PASSWORD", "superadmin@2026")
PASSWORD = "Password123"

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + ("" if condition else f"  <- {detail}"))
    if not condition:
        failures.append(label)


def data(response: httpx.Response) -> Any:
    """The `data` half of the envelope, or None if the call failed."""
    try:
        return response.json().get("data")
    except ValueError:
        return None


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def wait_for(socket: Any, kind: str, timeout: float = 6.0) -> dict[str, Any]:
    """The next frame of one type, skipping whatever arrives before it.

    Presence chatter is interleaved with document traffic by design, so reading
    "the next frame" and asserting on it would be flaky for a reason that is not
    a bug.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError(f"never received {kind!r}")
        frame: dict[str, Any] = json.loads(await asyncio.wait_for(socket.recv(), remaining))
        if frame.get("type") == kind:
            return frame


def sample_doc(screens: int) -> dict[str, Any]:
    return {
        "name": "Smoke",
        "screens": [
            {"id": f"s{i}", "key": f"screen_{i}", "title": f"Screen {i}"} for i in range(screens)
        ],
        "modules": [],
        "edges": [],
    }


async def check_auth(client: httpx.AsyncClient, alice: str) -> dict[str, Any]:
    print("\n== auth ==")
    created = await client.post(
        f"{BASE}/auth/register",
        json={"email": alice, "password": PASSWORD, "full_name": "Alice A"},
    )
    check("register", created.status_code == 201, created.text[:300])
    if created.status_code != 201:
        # Nothing after this can mean anything without a session, and a 429 here
        # is the common case: this script registers several accounts per run and
        # the registration quota is per IP. Say so instead of dying on a KeyError
        # twenty lines later.
        raise SystemExit(
            f"cannot continue without an account ({created.status_code}): "
            f"{created.json().get('message')}"
        )
    session: dict[str, Any] = data(created) or {}

    duplicate = await client.post(
        f"{BASE}/auth/register", json={"email": alice, "password": PASSWORD}
    )
    check("duplicate email is refused", duplicate.status_code == 409, duplicate.text[:200])

    weak = await client.post(
        f"{BASE}/auth/register",
        json={"email": f"weak.{uuid.uuid4().hex[:6]}@x.com", "password": "abcdefgh"},
    )
    check("weak password is refused", weak.status_code == 422, weak.text[:200])

    wrong = await client.post(f"{BASE}/auth/login", json={"email": alice, "password": "Nope12345"})
    check("wrong password is refused", wrong.status_code == 401, wrong.text[:200])

    mixed = await client.post(
        f"{BASE}/auth/login", json={"email": alice.upper(), "password": PASSWORD}
    )
    check("sign-in ignores address case", mixed.status_code == 200, mixed.text[:200])

    me = await client.get(f"{BASE}/users/me", headers=headers(session["access_token"]))
    check("the session works", data(me) is not None and data(me)["email"] == alice, me.text[:200])
    check("a member holds no admin permissions", data(me)["permissions"] == [], me.text[:200])

    anonymous = await client.get(f"{BASE}/users/me")
    check("no token is refused", anonymous.status_code in (401, 403), str(anonymous.status_code))

    refreshed = await client.post(
        f"{BASE}/auth/refresh", json={"refresh_token": session["refresh_token"]}
    )
    check("refresh works", refreshed.status_code == 200, refreshed.text[:200])
    replay = await client.post(
        f"{BASE}/auth/refresh", json={"refresh_token": session["refresh_token"]}
    )
    check("a rotated refresh token is dead", replay.status_code == 401, replay.text[:200])
    return dict(data(refreshed) or {}, email=alice)


async def check_projects(client: httpx.AsyncClient, token: str) -> str:
    print("\n== projects ==")
    created = await client.post(
        f"{BASE}/projects",
        headers=headers(token),
        json={
            "name": "Ops Console",
            "description": "internal",
            "doc": sample_doc(1),
            "schema_version": 4,
        },
    )
    check("create", created.status_code == 201, created.text[:300])
    project = data(created)
    check("screen count comes from the document", project["screen_count"] == 1, str(project))
    check("the author is the owner", project["my_role"] == "OWNER", str(project["my_role"]))

    listed = await client.get(f"{BASE}/projects", headers=headers(token))
    check("it appears in the list", data(listed)["total"] == 1, listed.text[:200])
    project_id: str = project["id"]
    return project_id


async def check_sharing(
    client: httpx.AsyncClient, owner_token: str, project_id: str, bob: str, carol: str
) -> dict[str, str]:
    print("\n== sharing by email ==")
    invited = await client.post(
        f"{BASE}/projects/{project_id}/members",
        headers=headers(owner_token),
        json={"email": bob, "role": "EDITOR"},
    )
    check("invite an address with no account", invited.status_code == 201, invited.text[:300])
    check(
        "it waits as a pending invitation",
        data(invited)["status"] == "INVITED" and data(invited)["user"] is None,
        str(data(invited)),
    )

    joined = await client.post(
        f"{BASE}/auth/register", json={"email": bob, "password": PASSWORD, "full_name": "Bob B"}
    )
    check("the invitee registers", joined.status_code == 201, joined.text[:200])
    bob_token: str = data(joined)["access_token"]

    theirs = await client.get(f"{BASE}/projects", headers=headers(bob_token))
    check("registering claims the invitation", data(theirs)["total"] == 1, theirs.text[:300])
    check(
        "with the role they were given",
        data(theirs)["items"][0]["my_role"] == "EDITOR",
        str(data(theirs)["items"][0]["my_role"]),
    )

    stranger = await client.post(
        f"{BASE}/auth/register", json={"email": carol, "password": PASSWORD}
    )
    carol_token: str = data(stranger)["access_token"]
    nothing = await client.get(f"{BASE}/projects", headers=headers(carol_token))
    check("an outsider sees nothing", data(nothing)["total"] == 0, nothing.text[:200])
    hidden = await client.get(f"{BASE}/projects/{project_id}", headers=headers(carol_token))
    check("and cannot open it", hidden.status_code == 404, hidden.text[:200])

    return {"bob": bob_token, "carol": carol_token}


async def check_concurrency(
    client: httpx.AsyncClient, owner_token: str, editor_token: str, project_id: str
) -> None:
    print("\n== document concurrency ==")
    current = await client.get(f"{BASE}/projects/{project_id}", headers=headers(editor_token))
    version = data(current)["doc_version"]

    saved = await client.put(
        f"{BASE}/projects/{project_id}/document",
        headers=headers(editor_token),
        json={"doc": sample_doc(2), "base_version": version},
    )
    check("an editor can save", saved.status_code == 200, saved.text[:300])
    check("the version moves", data(saved)["doc_version"] == version + 1, str(data(saved)))
    check("the counts follow", data(saved)["screen_count"] == 2, str(data(saved)))

    stale = await client.put(
        f"{BASE}/projects/{project_id}/document",
        headers=headers(owner_token),
        json={"doc": sample_doc(1), "base_version": version},
    )
    check("a stale save is refused", stale.status_code == 409, stale.text[:200])
    check(
        "the refusal says what the current version is",
        stale.json()["errors"][0]["current_version"] == version + 1,
        stale.text[:300],
    )


async def check_roles(
    client: httpx.AsyncClient, owner_token: str, editor_token: str, project_id: str
) -> None:
    print("\n== role limits ==")
    members = await client.get(
        f"{BASE}/projects/{project_id}/members", headers=headers(owner_token)
    )
    editor = next(m for m in data(members) if m["role"] == "EDITOR")

    promoted = await client.patch(
        f"{BASE}/projects/{project_id}/members/{editor['id']}",
        headers=headers(editor_token),
        json={"role": "OWNER"},
    )
    check("an editor cannot change roles", promoted.status_code == 403, promoted.text[:200])
    removed = await client.delete(f"{BASE}/projects/{project_id}", headers=headers(editor_token))
    check("an editor cannot delete the project", removed.status_code == 403, removed.text[:200])

    demoted = await client.patch(
        f"{BASE}/projects/{project_id}/members/{editor['id']}",
        headers=headers(owner_token),
        json={"role": "VIEWER"},
    )
    check("the owner can demote", demoted.status_code == 200, demoted.text[:200])
    blocked = await client.put(
        f"{BASE}/projects/{project_id}/document",
        headers=headers(editor_token),
        json={"doc": sample_doc(1)},
    )
    check("a viewer cannot save", blocked.status_code == 403, blocked.text[:200])
    readable = await client.get(f"{BASE}/projects/{project_id}", headers=headers(editor_token))
    check("but can still read", readable.status_code == 200, readable.text[:200])

    # Put them back for the collaboration checks.
    await client.patch(
        f"{BASE}/projects/{project_id}/members/{editor['id']}",
        headers=headers(owner_token),
        json={"role": "EDITOR"},
    )


async def check_history(client: httpx.AsyncClient, token: str, project_id: str) -> None:
    print("\n== versions, activity, comments ==")
    snapshot = await client.post(
        f"{BASE}/projects/{project_id}/versions", headers=headers(token), json={"label": "before"}
    )
    check("named snapshot", snapshot.status_code == 201, snapshot.text[:200])
    restored = await client.post(
        f"{BASE}/projects/{project_id}/versions/{data(snapshot)['id']}/restore",
        headers=headers(token),
    )
    check("restore", restored.status_code == 200, restored.text[:200])

    feed = await client.get(f"{BASE}/projects/{project_id}/activity", headers=headers(token))
    check("the activity feed is populated", data(feed)["total"] >= 3, feed.text[:200])

    comment = await client.post(
        f"{BASE}/projects/{project_id}/comments",
        headers=headers(token),
        json={"body": "looks good", "target_kind": "screen", "target_key": "screen_0"},
    )
    check("comment", comment.status_code == 201, comment.text[:200])


async def check_admin(client: httpx.AsyncClient, member_token: str, project_id: str) -> None:
    print("\n== admin ==")
    signed_in = await client.post(
        f"{BASE}/auth/login", json={"email": SUPERADMIN, "password": SUPERADMIN_PASSWORD}
    )
    check("superadmin can sign in", signed_in.status_code == 200, signed_in.text[:200])
    if signed_in.status_code != 200:
        return
    boss = headers(data(signed_in)["access_token"])

    walled = await client.get(f"{BASE}/admin/stats", headers=headers(member_token))
    check("a member cannot reach the admin surface", walled.status_code == 403, walled.text[:200])

    stats = await client.get(f"{BASE}/admin/stats", headers=boss)
    check("stats", stats.status_code == 200 and data(stats)["users_total"] >= 4, stats.text[:300])

    projects = await client.get(f"{BASE}/admin/projects", headers=boss)
    check(
        "the admin project list carries no document",
        "doc" not in data(projects)["items"][0],
        str(sorted(data(projects)["items"][0])),
    )
    unreachable = await client.get(f"{BASE}/projects/{project_id}", headers=boss)
    check(
        "a superadmin cannot open a project they are not on",
        unreachable.status_code == 404,
        unreachable.text[:200],
    )

    address = f"dave.{uuid.uuid4().hex[:8]}@example.com"
    made = await client.post(
        f"{BASE}/admin/users", headers=boss, json={"email": address, "full_name": "Dave"}
    )
    check("create a user", made.status_code == 201, made.text[:300])
    temporary = data(made)["temporary_password"]
    check("a one-time password comes back", bool(temporary), str(data(made)))

    issued = await client.post(f"{BASE}/auth/login", json={"email": address, "password": temporary})
    check("the issued password signs in", issued.status_code == 200, issued.text[:200])
    check(
        "and is flagged for replacement",
        data(issued)["must_change_password"] is True,
        issued.text[:200],
    )
    dave = headers(data(issued)["access_token"])
    check(
        "the flagged account is walled off",
        (await client.get(f"{BASE}/projects", headers=dave)).status_code == 403,
        "",
    )
    check(
        "but can still read itself",
        (await client.get(f"{BASE}/users/me", headers=dave)).status_code == 200,
        "",
    )
    chosen = await client.post(
        f"{BASE}/auth/set-initial-password", headers=dave, json={"new_password": "ChosenPass1"}
    )
    check("it can choose its own password", chosen.status_code == 200, chosen.text[:200])
    check(
        "and the wall lifts",
        (await client.get(f"{BASE}/projects", headers=dave)).status_code == 200,
        "",
    )

    audit = await client.get(f"{BASE}/admin/audit?action=auth", headers=boss)
    check("the audit trail filters by namespace", data(audit)["total"] > 0, audit.text[:200])


async def check_collaboration(
    client: httpx.AsyncClient, owner: dict[str, Any], bob_email: str, project_id: str
) -> None:
    print("\n== live collaboration ==")
    first = await client.post(
        f"{BASE}/auth/login", json={"email": owner["email"], "password": PASSWORD}
    )
    second = await client.post(
        f"{BASE}/auth/login", json={"email": bob_email, "password": PASSWORD}
    )
    a_token = data(first)["access_token"]
    b_token = data(second)["access_token"]

    async with websockets.connect(f"{WS}/ws/projects/{project_id}?token={a_token}") as a:
        hello = json.loads(await a.recv())
        check(
            "hello carries the document",
            hello["type"] == "hello" and "doc" in hello,
            str(hello)[:200],
        )
        check("and says what you may do", hello["you"]["can_edit"] is True, str(hello["you"]))

        async with websockets.connect(f"{WS}/ws/projects/{project_id}?token={b_token}") as b:
            greeting = json.loads(await b.recv())
            check("the second person joins", greeting["type"] == "hello", str(greeting)[:120])
            check(
                "the first is told",
                (await wait_for(a, "member.joined"))["member"]["email"] == bob_email,
                "",
            )
            roster = await wait_for(a, "presence")
            check(
                "and gets the authoritative roster", len(roster["members"]) == 2, str(roster)[:200]
            )

            await b.send(
                json.dumps(
                    {
                        "type": "doc.update",
                        "doc": sample_doc(3),
                        "base_version": greeting["doc_version"],
                    }
                )
            )
            relayed = await wait_for(a, "doc.updated")
            check("the edit arrives live", len(relayed["doc"]["screens"]) == 3, str(relayed)[:200])
            acknowledged = await wait_for(b, "doc.ack")
            check(
                "the sender is acknowledged",
                acknowledged.get("ack") is True,
                str(acknowledged)[:200],
            )
            check("the ack carries no document", "doc" not in acknowledged, str(acknowledged)[:200])

            await b.send(
                json.dumps({"type": "doc.update", "doc": sample_doc(1), "base_version": 1})
            )
            conflict = await wait_for(b, "doc.conflict")
            check(
                "a stale socket save is told to reconcile", "doc" in conflict, str(conflict)[:200]
            )

            await a.send(json.dumps({"type": "cursor", "payload": {"x": 10, "y": 20}}))
            cursor = await wait_for(b, "cursor")
            check(
                "cursors are relayed with a server-set identity",
                cursor["name"] == "Alice A",
                str(cursor)[:200],
            )

            await a.send(json.dumps({"type": "ping"}))
            check("ping/pong", (await wait_for(a, "pong"))["at"] != "", "")

            presence = await client.get(
                f"{BASE}/projects/{project_id}/presence", headers=headers(a_token)
            )
            check("presence lists both people", len(data(presence)) == 2, presence.text[:300])

        check(
            "departure is announced",
            (await wait_for(a, "member.left"))["member"]["email"] == bob_email,
            "",
        )


async def main() -> int:
    tag = uuid.uuid4().hex[:8]
    alice, bob, carol = (f"{name}.{tag}@example.com" for name in ("alice", "bob", "carol"))
    print(f"smoke test against {ROOT}")

    async with httpx.AsyncClient(timeout=30) as client:
        owner = await check_auth(client, alice)
        project_id = await check_projects(client, owner["access_token"])
        tokens = await check_sharing(client, owner["access_token"], project_id, bob, carol)
        await check_concurrency(client, owner["access_token"], tokens["bob"], project_id)
        await check_roles(client, owner["access_token"], tokens["bob"], project_id)
        await check_history(client, owner["access_token"], project_id)
        await check_admin(client, owner["access_token"], project_id)
        await check_collaboration(client, owner, bob, project_id)

    print("\n" + ("ALL PASSED" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
