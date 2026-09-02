"""Sharing is by email address, so the address has to be proved.

Every rule here was a hole an audit found, and each one is written as the
attack rather than the mechanism: a test that asserts `email_verified_at is not
None` would pass against code that never reads it.
"""

import httpx

from app.core.security import create_invite_token, create_verify_token
from tests.conftest import API, register, sample_doc, unwrap


async def create(client: httpx.AsyncClient, actor) -> dict:  # type: ignore[no-untyped-def]
    response = await client.post(
        f"{API}/projects",
        headers=actor.headers,
        json={"name": "Shared Project", "doc": sample_doc(2)},
    )
    assert response.status_code == 201, response.text
    return unwrap(response)


async def test_squatting_an_address_before_it_is_invited_wins_nothing(
    client: httpx.AsyncClient,
) -> None:
    # The attacker registers an address they do not own and never confirms it.
    squatter = await register(client, "finance@theirclient.com", verify=False)

    owner = await register(client)
    project = await create(client, owner)
    member = unwrap(
        await client.post(
            f"{API}/projects/{project['id']}/members",
            headers=owner.headers,
            json={"email": "finance@theirclient.com", "role": "EDITOR"},
        )
    )
    # Not bound to the unconfirmed account: the invitation is still waiting.
    assert member["status"] == "INVITED"
    assert member["user"] is None

    listed = unwrap(await client.get(f"{API}/projects", headers=squatter.headers))
    assert listed["total"] == 0
    assert (
        await client.get(f"{API}/projects/{project['id']}", headers=squatter.headers)
    ).status_code == 404


async def test_confirming_the_address_claims_what_was_waiting(
    client: httpx.AsyncClient,
) -> None:
    invitee = await register(client, "real.person@example.com", verify=False)
    owner = await register(client)
    project = await create(client, owner)
    await client.post(
        f"{API}/projects/{project['id']}/members",
        headers=owner.headers,
        json={"email": "real.person@example.com", "role": "EDITOR"},
    )
    assert unwrap(await client.get(f"{API}/projects", headers=invitee.headers))["total"] == 0

    confirmed = await client.post(
        f"{API}/auth/verify-email",
        json={"token": create_verify_token(invitee.id, "real.person@example.com")},
    )
    assert confirmed.status_code == 200

    listed = unwrap(await client.get(f"{API}/projects", headers=invitee.headers))
    assert listed["total"] == 1
    assert listed["items"][0]["my_role"] == "EDITOR"


async def test_registering_with_the_invitation_gets_in_at_once(
    client: httpx.AsyncClient,
) -> None:
    # The intended path: the emailed link carries the token, the register form
    # forwards it, and the address is proved by holding it.
    owner = await register(client)
    project = await create(client, owner)
    await client.post(
        f"{API}/projects/{project['id']}/members",
        headers=owner.headers,
        json={"email": "invited@example.com", "role": "EDITOR"},
    )
    invitee = await register(
        client,
        "invited@example.com",
        verify=False,
        invite_token=create_invite_token(project["id"], "invited@example.com"),
    )
    listed = unwrap(await client.get(f"{API}/projects", headers=invitee.headers))
    assert listed["total"] == 1
    assert listed["items"][0]["my_role"] == "EDITOR"


async def test_an_invitation_for_someone_else_proves_nothing(
    client: httpx.AsyncClient,
) -> None:
    owner = await register(client)
    project = await create(client, owner)
    await client.post(
        f"{API}/projects/{project['id']}/members",
        headers=owner.headers,
        json={"email": "alice@example.com", "role": "EDITOR"},
    )
    # A token issued to Alice, presented while registering Bob's address.
    bob = await register(
        client,
        "bob@example.com",
        verify=False,
        invite_token=create_invite_token(project["id"], "alice@example.com"),
    )
    assert unwrap(await client.get(f"{API}/projects", headers=bob.headers))["total"] == 0
