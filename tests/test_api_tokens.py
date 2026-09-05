"""Personal API tokens — the credential for callers that are not a browser.

The token is a full-rights credential handed to CI and to `weaver push`, so the
tests are about the three things that make that safe: the plaintext exists once,
the database never holds it, and revoking stops it immediately.
"""

import httpx
from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.core.security import fingerprint
from app.modules.auth.models import ApiToken
from tests.conftest import API, register, sample_doc, unwrap


async def mint(client: httpx.AsyncClient, actor, name: str = "weaver push") -> str:  # type: ignore[no-untyped-def]
    response = await client.post(
        f"{API}/users/me/tokens", headers=actor.headers, json={"name": name}
    )
    assert response.status_code == 201, response.text
    return str(unwrap(response)["token"])


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_the_plaintext_is_shown_once_and_never_stored(client: httpx.AsyncClient) -> None:
    actor = await register(client)
    token = await mint(client, actor)
    assert token.startswith("pst_")

    listed = unwrap(await client.get(f"{API}/users/me/tokens", headers=actor.headers))
    assert len(listed) == 1
    assert listed[0]["name"] == "weaver push"
    assert listed[0]["revoked_at"] is None
    assert "token" not in listed[0], "the secret must not be readable after it is created"

    async with AsyncSessionLocal() as db:
        stored = (await db.execute(select(ApiToken))).scalars().all()
    assert len(stored) == 1
    # Only the SHA-256 fingerprint: a dump of this table hands an attacker
    # nothing they can present.
    assert stored[0].token_hash == fingerprint(token)
    assert token not in stored[0].token_hash


async def test_a_token_authenticates_the_whole_api(client: httpx.AsyncClient) -> None:
    """No scopes. It is the account's credential, which is the documented
    trade — so it has to work on an ordinary route and on a project write."""
    actor = await register(client)
    token = await mint(client, actor)

    me = unwrap(await client.get(f"{API}/users/me", headers=bearer(token)))
    assert me["email"] == actor.email

    project = unwrap(
        await client.post(
            f"{API}/projects", headers=bearer(token), json={"name": "CRM", "doc": sample_doc(1)}
        )
    )
    created = await client.post(
        f"{API}/projects/{project['id']}/discovery/runs",
        headers=bearer(token),
        json={
            "label": "pushed by CI",
            "items": [{"key": "R1", "family": "RULE", "title": "Deletes are soft"}],
        },
    )
    assert created.status_code == 201, created.text
    assert unwrap(created)["progress"]["total"] == 1


async def test_revoking_stops_the_token_at_once(client: httpx.AsyncClient) -> None:
    actor = await register(client)
    token = await mint(client, actor)
    listed = unwrap(await client.get(f"{API}/users/me/tokens", headers=actor.headers))

    revoked = await client.delete(f"{API}/users/me/tokens/{listed[0]['id']}", headers=actor.headers)
    assert revoked.status_code == 200, revoked.text

    assert (await client.get(f"{API}/users/me", headers=bearer(token))).status_code == 401
    after = unwrap(await client.get(f"{API}/users/me/tokens", headers=actor.headers))
    assert after[0]["revoked_at"] is not None


async def test_a_token_belongs_to_one_account(client: httpx.AsyncClient) -> None:
    actor = await register(client)
    stranger = await register(client)
    token = await mint(client, actor)
    listed = unwrap(await client.get(f"{API}/users/me/tokens", headers=actor.headers))

    assert (
        await client.delete(f"{API}/users/me/tokens/{listed[0]['id']}", headers=stranger.headers)
    ).status_code == 404
    assert unwrap(await client.get(f"{API}/users/me/tokens", headers=stranger.headers)) == []
    # Still live: the stranger's attempt changed nothing.
    assert (await client.get(f"{API}/users/me", headers=bearer(token))).status_code == 200


async def test_a_token_that_was_never_issued_is_refused(client: httpx.AsyncClient) -> None:
    for credential in ("pst_not-a-real-token", "not-a-jwt-either"):
        response = await client.get(f"{API}/users/me", headers=bearer(credential))
        assert response.status_code == 401, credential

    # No credential at all never reaches either path — `HTTPBearer` refuses the
    # request before the dependency runs, and it answers the same 401.
    assert (await client.get(f"{API}/users/me")).status_code == 401


async def test_logout_everywhere_leaves_api_tokens_alive(client: httpx.AsyncClient) -> None:
    """GitHub-PAT semantics, and the reason it is written down in three places.

    "Sign out everywhere" ends sessions. A token a script holds is not a
    session, so CI does not break because somebody clicked the button on their
    laptop — and the flip side, that a leaked token survives the panic button,
    is why the dialog says to revoke it there.
    """
    actor = await register(client)
    token = await mint(client, actor)

    revoked = unwrap(await client.post(f"{API}/auth/logout-everywhere", headers=actor.headers))
    assert revoked["sessions_revoked"] >= 1

    assert (await client.get(f"{API}/users/me", headers=bearer(token))).status_code == 200
