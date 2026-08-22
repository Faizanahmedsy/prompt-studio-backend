"""Sharing by email — the rule the whole product rests on.

A project is visible to the addresses added to it, and to nobody else. These
tests are the definition of that sentence.
"""

import httpx

from tests.conftest import API, make_superadmin, register, sample_doc, sign_in, unwrap


async def create(client: httpx.AsyncClient, actor, **overrides) -> dict:  # type: ignore[no-untyped-def]
    body = {"name": "Shared Project", "doc": sample_doc(2)}
    body.update(overrides)
    response = await client.post(f"{API}/projects", headers=actor.headers, json=body)
    assert response.status_code == 201, response.text
    return unwrap(response)


async def test_an_outsider_sees_nothing(client: httpx.AsyncClient) -> None:
    owner = await register(client)
    outsider = await register(client)
    project = await create(client, owner)

    assert unwrap(await client.get(f"{API}/projects", headers=outsider.headers))["total"] == 0
    # 404 rather than 403: telling someone "forbidden" confirms the project
    # exists, which is a fact only its members are entitled to.
    assert (
        await client.get(f"{API}/projects/{project['id']}", headers=outsider.headers)
    ).status_code == 404


async def test_every_project_route_is_closed_to_an_outsider(client: httpx.AsyncClient) -> None:
    owner = await register(client)
    outsider = await register(client)
    project = await create(client, owner)
    pid = project["id"]

    attempts = [
        client.get(f"{API}/projects/{pid}", headers=outsider.headers),
        client.patch(f"{API}/projects/{pid}", headers=outsider.headers, json={"name": "x"}),
        client.put(f"{API}/projects/{pid}/document", headers=outsider.headers, json={"doc": {}}),
        client.delete(f"{API}/projects/{pid}", headers=outsider.headers),
        client.get(f"{API}/projects/{pid}/members", headers=outsider.headers),
        client.post(
            f"{API}/projects/{pid}/members",
            headers=outsider.headers,
            json={"email": "x@example.com"},
        ),
        client.get(f"{API}/projects/{pid}/versions", headers=outsider.headers),
        client.get(f"{API}/projects/{pid}/activity", headers=outsider.headers),
        client.get(f"{API}/projects/{pid}/comments", headers=outsider.headers),
        client.get(f"{API}/projects/{pid}/presence", headers=outsider.headers),
    ]
    for attempt in attempts:
        response = await attempt
        assert response.status_code == 404, response.request.url


async def test_inviting_an_address_that_has_no_account_yet(client: httpx.AsyncClient) -> None:
    owner = await register(client)
    project = await create(client, owner)

    member = unwrap(
        await client.post(
            f"{API}/projects/{project['id']}/members",
            headers=owner.headers,
            json={"email": "not.registered.yet@example.com", "role": "EDITOR"},
        )
    )
    assert member["status"] == "INVITED"
    assert member["user"] is None

    # Registering with that address claims the invitation.
    invitee = await register(client, "not.registered.yet@example.com")
    listed = unwrap(await client.get(f"{API}/projects", headers=invitee.headers))
    assert listed["total"] == 1
    assert listed["items"][0]["my_role"] == "EDITOR"


async def test_an_invitation_is_matched_case_insensitively(client: httpx.AsyncClient) -> None:
    owner = await register(client)
    project = await create(client, owner)
    await client.post(
        f"{API}/projects/{project['id']}/members",
        headers=owner.headers,
        json={"email": "Mixed.Case@Example.com", "role": "VIEWER"},
    )
    invitee = await register(client, "mixed.case@example.com")
    assert unwrap(await client.get(f"{API}/projects", headers=invitee.headers))["total"] == 1


async def test_inviting_an_existing_account_activates_it_immediately(
    client: httpx.AsyncClient,
) -> None:
    owner = await register(client)
    guest = await register(client)
    project = await create(client, owner)

    member = unwrap(
        await client.post(
            f"{API}/projects/{project['id']}/members",
            headers=owner.headers,
            json={"email": guest.email, "role": "EDITOR"},
        )
    )
    assert member["status"] == "ACTIVE"
    assert member["user"]["email"] == guest.email
    assert unwrap(await client.get(f"{API}/projects", headers=guest.headers))["total"] == 1


async def test_inviting_the_same_address_twice_is_a_conflict(client: httpx.AsyncClient) -> None:
    owner = await register(client)
    project = await create(client, owner)
    body = {"email": "twice@example.com", "role": "EDITOR"}
    assert (
        await client.post(
            f"{API}/projects/{project['id']}/members", headers=owner.headers, json=body
        )
    ).status_code == 201
    repeat = await client.post(
        f"{API}/projects/{project['id']}/members", headers=owner.headers, json=body
    )
    assert repeat.status_code == 409


async def test_bulk_invite_skips_duplicates_instead_of_failing(client: httpx.AsyncClient) -> None:
    """Pasting a team list twice is a normal thing to do; it must not be an
    error that loses the addresses that were new."""
    owner = await register(client)
    project = await create(client, owner)
    await client.post(
        f"{API}/projects/{project['id']}/members",
        headers=owner.headers,
        json={"email": "one@example.com"},
    )
    added = unwrap(
        await client.post(
            f"{API}/projects/{project['id']}/members/bulk",
            headers=owner.headers,
            json={
                "emails": ["one@example.com", "two@example.com", "three@example.com"],
                "role": "VIEWER",
            },
        )
    )
    assert {member["email"] for member in added} == {"two@example.com", "three@example.com"}


async def test_member_emails_at_creation_time(client: httpx.AsyncClient) -> None:
    owner = await register(client)
    project = await create(
        client, owner, member_emails=["colleague@example.com", "another@example.com"]
    )
    members = unwrap(
        await client.get(f"{API}/projects/{project['id']}/members", headers=owner.headers)
    )
    assert {member["email"] for member in members} == {
        owner.email,
        "colleague@example.com",
        "another@example.com",
    }


async def test_role_ladder(client: httpx.AsyncClient) -> None:
    owner = await register(client)
    project = await create(client, owner)
    pid = project["id"]

    people = {}
    for role in ("VIEWER", "COMMENTER", "EDITOR"):
        person = await register(client)
        await client.post(
            f"{API}/projects/{pid}/members",
            headers=owner.headers,
            json={"email": person.email, "role": role},
        )
        people[role] = person

    # Everyone can read.
    for person in people.values():
        assert (
            await client.get(f"{API}/projects/{pid}", headers=person.headers)
        ).status_code == 200

    # Commenting starts at COMMENTER.
    assert (
        await client.post(
            f"{API}/projects/{pid}/comments", headers=people["VIEWER"].headers, json={"body": "hi"}
        )
    ).status_code == 403
    assert (
        await client.post(
            f"{API}/projects/{pid}/comments",
            headers=people["COMMENTER"].headers,
            json={"body": "hi"},
        )
    ).status_code == 201

    # Saving starts at EDITOR.
    for role in ("VIEWER", "COMMENTER"):
        assert (
            await client.put(
                f"{API}/projects/{pid}/document",
                headers=people[role].headers,
                json={"doc": sample_doc(1)},
            )
        ).status_code == 403
    assert (
        await client.put(
            f"{API}/projects/{pid}/document",
            headers=people["EDITOR"].headers,
            json={"doc": sample_doc(1)},
        )
    ).status_code == 200

    # Membership and deletion are the owner's alone.
    assert (
        await client.post(
            f"{API}/projects/{pid}/members",
            headers=people["EDITOR"].headers,
            json={"email": "x@example.com"},
        )
    ).status_code == 403
    assert (
        await client.delete(f"{API}/projects/{pid}", headers=people["EDITOR"].headers)
    ).status_code == 403


async def test_a_project_always_keeps_an_owner(client: httpx.AsyncClient) -> None:
    owner = await register(client)
    guest = await register(client)
    project = await create(client, owner)
    pid = project["id"]
    await client.post(
        f"{API}/projects/{pid}/members", headers=owner.headers, json={"email": guest.email}
    )
    members = unwrap(await client.get(f"{API}/projects/{pid}/members", headers=owner.headers))
    owner_row = next(m for m in members if m["email"] == owner.email)
    guest_row = next(m for m in members if m["email"] == guest.email)

    # The sole owner cannot leave...
    assert (
        await client.post(f"{API}/projects/{pid}/leave", headers=owner.headers)
    ).status_code == 409
    # ...nor be removed.
    assert (
        await client.delete(
            f"{API}/projects/{pid}/members/{owner_row['id']}", headers=owner.headers
        )
    ).status_code == 409

    # With a second owner in place, both become possible.
    await client.patch(
        f"{API}/projects/{pid}/members/{guest_row['id']}",
        headers=owner.headers,
        json={"role": "OWNER"},
    )
    assert (
        await client.post(f"{API}/projects/{pid}/leave", headers=owner.headers)
    ).status_code == 200
    assert unwrap(await client.get(f"{API}/projects", headers=owner.headers))["total"] == 0
    assert unwrap(await client.get(f"{API}/projects", headers=guest.headers))["total"] == 1


async def test_an_owner_cannot_change_their_own_role(client: httpx.AsyncClient) -> None:
    owner = await register(client)
    project = await create(client, owner)
    members = unwrap(
        await client.get(f"{API}/projects/{project['id']}/members", headers=owner.headers)
    )
    response = await client.patch(
        f"{API}/projects/{project['id']}/members/{members[0]['id']}",
        headers=owner.headers,
        json={"role": "VIEWER"},
    )
    assert response.status_code == 409


async def test_removing_a_member_takes_the_project_away(client: httpx.AsyncClient) -> None:
    owner = await register(client)
    guest = await register(client)
    project = await create(client, owner)
    pid = project["id"]
    member = unwrap(
        await client.post(
            f"{API}/projects/{pid}/members", headers=owner.headers, json={"email": guest.email}
        )
    )
    assert unwrap(await client.get(f"{API}/projects", headers=guest.headers))["total"] == 1

    await client.delete(f"{API}/projects/{pid}/members/{member['id']}", headers=owner.headers)
    assert unwrap(await client.get(f"{API}/projects", headers=guest.headers))["total"] == 0
    assert (await client.get(f"{API}/projects/{pid}", headers=guest.headers)).status_code == 404


async def test_a_member_of_one_project_cannot_reach_another(client: httpx.AsyncClient) -> None:
    owner = await register(client)
    guest = await register(client)
    shared = await create(client, owner, name="Shared")
    private = await create(client, owner, name="Private")
    await client.post(
        f"{API}/projects/{shared['id']}/members", headers=owner.headers, json={"email": guest.email}
    )
    assert (
        await client.get(f"{API}/projects/{shared['id']}", headers=guest.headers)
    ).status_code == 200
    assert (
        await client.get(f"{API}/projects/{private['id']}", headers=guest.headers)
    ).status_code == 404


async def test_a_platform_superadmin_still_cannot_open_a_project(
    client: httpx.AsyncClient,
) -> None:
    """The sharing promise has no admin exception. Administering accounts is
    not the same as being able to read what people wrote."""
    owner = await register(client)
    boss = await register(client)
    await make_superadmin(boss.email)
    boss = await sign_in(client, boss.email, boss.password)
    project = await create(client, owner)

    assert (
        await client.get(f"{API}/projects/{project['id']}", headers=boss.headers)
    ).status_code == 404
    assert unwrap(await client.get(f"{API}/projects", headers=boss.headers))["total"] == 0

    # But the metadata view an operator needs does work.
    listed = unwrap(await client.get(f"{API}/admin/projects", headers=boss.headers))
    assert listed["total"] == 1
    assert "doc" not in listed["items"][0]
