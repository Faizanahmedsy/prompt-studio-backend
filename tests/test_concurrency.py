"""Races that silently destroyed data.

Each of these passed as a single-threaded test and failed the moment two
requests overlapped, which is every real deployment.
"""

import asyncio

import httpx

from tests.conftest import API, register, sample_doc, unwrap


async def test_two_simultaneous_saves_cannot_both_win(live_server: str) -> None:
    """The optimistic-concurrency check was a read, a compare and a write.

    Unlocked, two saves that both read version N both wrote N+1: both returned
    200, both reported the same version, one document vanished with no 409 —
    and neither snapshot held the lost content, because both captured the same
    pre-race document.

    Run against a live server so the two requests are genuinely concurrent;
    through the ASGI transport they would serialise on one event loop task.
    """
    base = f"http://{live_server}"
    async with httpx.AsyncClient(base_url=base, timeout=30) as remote:
        actor = await register(remote)
        project = unwrap(
            await remote.post(
                f"{API}/projects",
                headers=actor.headers,
                json={"name": "Race", "doc": sample_doc(1)},
            )
        )
        version = project["doc_version"]

        first, second = await asyncio.gather(
            remote.put(
                f"{API}/projects/{project['id']}/document",
                headers=actor.headers,
                json={"doc": sample_doc(5), "base_version": version},
            ),
            remote.put(
                f"{API}/projects/{project['id']}/document",
                headers=actor.headers,
                json={"doc": sample_doc(9), "base_version": version},
            ),
        )
        statuses = sorted([first.status_code, second.status_code])
        assert statuses == [200, 409], f"both saves were accepted: {statuses}"

        # The winner's document is what is stored, and the counts describe it.
        stored = unwrap(await remote.get(f"{API}/projects/{project['id']}", headers=actor.headers))
        assert len(stored["doc"]["screens"]) == stored["screen_count"]
        assert stored["doc_version"] == version + 1


async def test_parallel_refresh_does_not_500(live_server: str) -> None:
    """A SPA's interceptor refreshes twice when two requests 401 together.

    Both used to pass the revocation check and collide on the primary key of
    the denylist; the loser got an unhandled IntegrityError and a 500, which a
    client reads as a hard failure rather than "retry with the new token".
    """
    base = f"http://{live_server}"
    async with httpx.AsyncClient(base_url=base, timeout=30) as remote:
        actor = await register(remote)
        replies = await asyncio.gather(
            *[
                remote.post(f"{API}/auth/refresh", json={"refresh_token": actor.refresh_token})
                for _ in range(4)
            ]
        )
        statuses = [reply.status_code for reply in replies]
        assert 500 not in statuses, f"a concurrent refresh crashed: {statuses}"
        assert statuses.count(200) == 1, f"a rotated token was accepted twice: {statuses}"
        assert set(statuses) <= {200, 401}


async def test_concurrent_guesses_still_lock_the_account(live_server: str) -> None:
    """`count += 1` on an ORM object is a lost update across sessions.

    Eight sequential wrong passwords locked the account and a hundred and fifty
    concurrent ones did not — which is exactly the shape of a real attack.
    """
    base = f"http://{live_server}"
    async with httpx.AsyncClient(base_url=base, timeout=60) as remote:
        actor = await register(remote)
        await asyncio.gather(
            *[
                remote.post(
                    f"{API}/auth/login", json={"email": actor.email, "password": "WrongOne111"}
                )
                for _ in range(40)
            ]
        )
        blocked = await remote.post(
            f"{API}/auth/login", json={"email": actor.email, "password": actor.password}
        )
        assert blocked.status_code == 401
        assert "Try again" in blocked.json()["message"], (
            "the correct password still worked — the counter lost its increments"
        )


async def test_a_duplicate_address_does_not_fail_project_creation(
    client: httpx.AsyncClient,
) -> None:
    """The project, its owner row and its activity are committed before the
    invitations run. A 409 after that tells the client nothing happened, and
    the retry makes a second project."""
    actor = await register(client)
    response = await client.post(
        f"{API}/projects",
        headers=actor.headers,
        json={
            "name": "Duplicated",
            "doc": sample_doc(1),
            "member_emails": ["dup@example.com", "DUP@example.com", "dup@example.com"],
        },
    )
    assert response.status_code == 201
    members = unwrap(
        await client.get(f"{API}/projects/{unwrap(response)['id']}/members", headers=actor.headers)
    )
    assert {m["email"] for m in members} == {actor.email, "dup@example.com"}
    assert unwrap(await client.get(f"{API}/projects", headers=actor.headers))["total"] == 1
