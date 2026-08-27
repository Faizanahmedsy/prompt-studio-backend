"""The public read-only link.

A capability handed out deliberately by an owner: whoever holds the token may
READ one document with no account. These tests are the definition of "read
only" and of "only what the owner shared" — the two things that make this
feature safe to have in a product where every other project is private.
"""

import httpx

from tests.conftest import API, register, sample_doc, unwrap


async def create(client: httpx.AsyncClient, actor, **overrides) -> dict:  # type: ignore[no-untyped-def]
    body = {"name": "Shared Project", "doc": sample_doc(2)}
    body.update(overrides)
    response = await client.post(f"{API}/projects", headers=actor.headers, json=body)
    assert response.status_code == 201, response.text
    return unwrap(response)


async def enable(client: httpx.AsyncClient, actor, pid: str) -> str:  # type: ignore[no-untyped-def]
    response = await client.post(f"{API}/projects/{pid}/public-link", headers=actor.headers)
    assert response.status_code == 200, response.text
    body = unwrap(response)
    assert body["enabled"] is True
    return body["token"]


# ── off by default ───────────────────────────────────────────────────────────


async def test_a_new_project_is_not_public(client: httpx.AsyncClient) -> None:
    owner = await register(client)
    project = await create(client, owner)
    state = unwrap(
        await client.get(f"{API}/projects/{project['id']}/public-link", headers=owner.headers)
    )
    assert state["enabled"] is False
    assert state["token"] is None


async def test_the_anonymous_route_refuses_an_unknown_token(client: httpx.AsyncClient) -> None:
    assert (await client.get(f"{API}/public/projects/not-a-real-token")).status_code == 404


async def test_an_empty_token_does_not_match_every_private_project(
    client: httpx.AsyncClient,
) -> None:
    # The bug this guards: `public_token == ""` matching NULL columns, or a
    # blank path segment resolving to the first row with no link.
    owner = await register(client)
    await create(client, owner)
    for token in ["", " ", "%20", "null", "None"]:
        response = await client.get(f"{API}/public/projects/{token}")
        assert response.status_code in (404, 405), f"{token!r} -> {response.status_code}"


# ── reading with the link ────────────────────────────────────────────────────


async def test_anyone_with_the_link_can_read_it_without_an_account(
    client: httpx.AsyncClient,
) -> None:
    owner = await register(client)
    project = await create(client, owner)
    token = await enable(client, owner, project["id"])

    # No Authorization header at all — this is the whole point of the feature.
    response = await client.get(f"{API}/public/projects/{token}")
    assert response.status_code == 200, response.text
    body = unwrap(response)
    assert body["name"] == "Shared Project"
    assert body["doc"]["screens"]


async def test_the_public_view_carries_no_information_about_the_team(
    client: httpx.AsyncClient,
) -> None:
    owner = await register(client)
    project = await create(client, owner)
    token = await enable(client, owner, project["id"])
    body = unwrap(await client.get(f"{API}/public/projects/{token}"))

    # A shared diagram must not disclose who works on it.
    for leaked in ("members", "owner", "my_role", "member_count", "public_token"):
        assert leaked not in body, f"{leaked} reached an anonymous reader"
    assert owner.email not in str(body)


async def test_the_link_survives_the_document_changing(client: httpx.AsyncClient) -> None:
    owner = await register(client)
    project = await create(client, owner)
    token = await enable(client, owner, project["id"])

    saved = await client.put(
        f"{API}/projects/{project['id']}/document",
        headers=owner.headers,
        json={"doc": sample_doc(5), "base_version": project["doc_version"]},
    )
    assert saved.status_code == 200, saved.text

    # Live, not a snapshot: the reader sees the project as it is now.
    body = unwrap(await client.get(f"{API}/public/projects/{token}"))
    assert len(body["doc"]["screens"]) == 5


# ── read only, and only read ─────────────────────────────────────────────────


async def test_the_token_cannot_be_used_to_write(client: httpx.AsyncClient) -> None:
    owner = await register(client)
    project = await create(client, owner)
    token = await enable(client, owner, project["id"])
    pid = project["id"]

    # Presented every way an attacker would try it.
    headers = {"Authorization": f"Bearer {token}"}
    attempts = [
        client.put(f"{API}/projects/{pid}/document", headers=headers, json={"doc": {}}),
        client.patch(f"{API}/projects/{pid}", headers=headers, json={"name": "Taken"}),
        client.delete(f"{API}/projects/{pid}", headers=headers),
        client.post(f"{API}/projects/{pid}/members", headers=headers, json={"email": "x@y.z"}),
        client.put(f"{API}/public/projects/{token}", json={"doc": {}}),
        client.post(f"{API}/public/projects/{token}", json={"doc": {}}),
        client.delete(f"{API}/public/projects/{token}"),
    ]
    for attempt in attempts:
        response = await attempt
        assert response.status_code in (401, 403, 404, 405), response.text

    # And the document is untouched.
    after = unwrap(await client.get(f"{API}/projects/{pid}", headers=owner.headers))
    assert after["name"] == "Shared Project"
    assert len(after["doc"]["screens"]) == 2


async def test_the_token_does_not_grant_access_to_other_projects(
    client: httpx.AsyncClient,
) -> None:
    owner = await register(client)
    shared = await create(client, owner, name="Shared")
    private = await create(client, owner, name="Private")
    token = await enable(client, owner, shared["id"])

    body = unwrap(await client.get(f"{API}/public/projects/{token}"))
    assert body["name"] == "Shared"
    assert body["id"] == shared["id"]
    assert body["id"] != private["id"]


async def test_holding_a_link_does_not_make_you_a_member(client: httpx.AsyncClient) -> None:
    owner = await register(client)
    reader = await register(client)
    project = await create(client, owner)
    await enable(client, owner, project["id"])

    # A signed-in stranger who has seen the link still has no project access.
    assert unwrap(await client.get(f"{API}/projects", headers=reader.headers))["total"] == 0
    assert (
        await client.get(f"{API}/projects/{project['id']}", headers=reader.headers)
    ).status_code == 404
    members = unwrap(
        await client.get(f"{API}/projects/{project['id']}/members", headers=owner.headers)
    )
    assert [m["email"] for m in members] == [owner.email]


# ── who may manage it ────────────────────────────────────────────────────────


async def test_only_an_owner_can_turn_the_link_on(client: httpx.AsyncClient) -> None:
    owner = await register(client)
    editor = await register(client)
    project = await create(client, owner)
    pid = project["id"]
    await client.post(
        f"{API}/projects/{pid}/members",
        headers=owner.headers,
        json={"email": editor.email, "role": "EDITOR"},
    )

    assert (
        await client.post(f"{API}/projects/{pid}/public-link", headers=editor.headers)
    ).status_code == 403
    # And it really is still off.
    state = unwrap(await client.get(f"{API}/projects/{pid}/public-link", headers=owner.headers))
    assert state["enabled"] is False


async def test_an_outsider_cannot_discover_whether_a_link_exists(
    client: httpx.AsyncClient,
) -> None:
    owner = await register(client)
    outsider = await register(client)
    project = await create(client, owner)
    pid = project["id"]
    await enable(client, owner, pid)

    for response in (
        await client.get(f"{API}/projects/{pid}/public-link", headers=outsider.headers),
        await client.post(f"{API}/projects/{pid}/public-link", headers=outsider.headers),
        await client.delete(f"{API}/projects/{pid}/public-link", headers=outsider.headers),
    ):
        # 404, not 403 — same rule as every other project route.
        assert response.status_code == 404


async def test_managing_the_link_requires_a_signed_in_caller(
    client: httpx.AsyncClient,
) -> None:
    owner = await register(client)
    project = await create(client, owner)
    pid = project["id"]
    assert (await client.post(f"{API}/projects/{pid}/public-link")).status_code == 401
    assert (await client.get(f"{API}/projects/{pid}/public-link")).status_code == 401


# ── turning it off, and rotating it ──────────────────────────────────────────


async def test_disabling_the_link_breaks_it_immediately(client: httpx.AsyncClient) -> None:
    owner = await register(client)
    project = await create(client, owner)
    token = await enable(client, owner, project["id"])
    assert (await client.get(f"{API}/public/projects/{token}")).status_code == 200

    disabled = unwrap(
        await client.delete(f"{API}/projects/{project['id']}/public-link", headers=owner.headers)
    )
    assert disabled["enabled"] is False
    assert (await client.get(f"{API}/public/projects/{token}")).status_code == 404


async def test_rotating_invalidates_the_link_already_handed_out(
    client: httpx.AsyncClient,
) -> None:
    owner = await register(client)
    project = await create(client, owner)
    first = await enable(client, owner, project["id"])

    rotated = unwrap(
        await client.post(
            f"{API}/projects/{project['id']}/public-link/rotate", headers=owner.headers
        )
    )
    second = rotated["token"]
    assert second != first
    assert (await client.get(f"{API}/public/projects/{first}")).status_code == 404
    assert (await client.get(f"{API}/public/projects/{second}")).status_code == 200


async def test_enabling_twice_keeps_the_link_that_was_already_shared(
    client: httpx.AsyncClient,
) -> None:
    # Re-minting here would silently break a URL the owner had already pasted
    # into a message. Rotation is the explicit action for that.
    owner = await register(client)
    project = await create(client, owner)
    first = await enable(client, owner, project["id"])
    second = await enable(client, owner, project["id"])
    assert first == second


async def test_disabling_twice_is_not_an_error(client: httpx.AsyncClient) -> None:
    owner = await register(client)
    project = await create(client, owner)
    await enable(client, owner, project["id"])
    for _ in range(2):
        response = await client.delete(
            f"{API}/projects/{project['id']}/public-link", headers=owner.headers
        )
        assert response.status_code == 200
        assert unwrap(response)["enabled"] is False


async def test_a_rotated_token_is_not_predictable_from_the_last(
    client: httpx.AsyncClient,
) -> None:
    owner = await register(client)
    project = await create(client, owner)
    seen = {await enable(client, owner, project["id"])}
    for _ in range(4):
        body = unwrap(
            await client.post(
                f"{API}/projects/{project['id']}/public-link/rotate", headers=owner.headers
            )
        )
        assert body["token"] not in seen
        assert len(body["token"]) >= 32
        seen.add(body["token"])


# ── the link does not outlive the project ────────────────────────────────────


async def test_deleting_the_project_takes_the_link_with_it(client: httpx.AsyncClient) -> None:
    owner = await register(client)
    project = await create(client, owner)
    token = await enable(client, owner, project["id"])

    await client.delete(f"{API}/projects/{project['id']}", headers=owner.headers)
    # The owner's last act was to take it away; a link handed out before that
    # must not keep serving it.
    assert (await client.get(f"{API}/public/projects/{token}")).status_code == 404


async def test_archiving_the_project_takes_the_link_down(client: httpx.AsyncClient) -> None:
    owner = await register(client)
    project = await create(client, owner)
    token = await enable(client, owner, project["id"])

    archived = await client.patch(
        f"{API}/projects/{project['id']}", headers=owner.headers, json={"is_archived": True}
    )
    assert archived.status_code == 200, archived.text
    assert (await client.get(f"{API}/public/projects/{token}")).status_code == 404


async def test_restoring_the_project_brings_the_link_back(client: httpx.AsyncClient) -> None:
    owner = await register(client)
    project = await create(client, owner)
    token = await enable(client, owner, project["id"])
    await client.delete(f"{API}/projects/{project['id']}", headers=owner.headers)
    await client.post(f"{API}/projects/{project['id']}/restore", headers=owner.headers)
    assert (await client.get(f"{API}/public/projects/{token}")).status_code == 200


# ── the owner's own view ─────────────────────────────────────────────────────


async def test_the_owner_sees_the_token_on_the_project_detail(
    client: httpx.AsyncClient,
) -> None:
    owner = await register(client)
    project = await create(client, owner)
    token = await enable(client, owner, project["id"])
    detail = unwrap(await client.get(f"{API}/projects/{project['id']}", headers=owner.headers))
    assert detail["public_token"] == token


async def test_the_project_list_never_carries_the_token(client: httpx.AsyncClient) -> None:
    # The list is reused by the admin surface; a token in a summary row is a
    # token in places nobody audited.
    owner = await register(client)
    project = await create(client, owner)
    await enable(client, owner, project["id"])
    page = unwrap(await client.get(f"{API}/projects", headers=owner.headers))
    assert "public_token" not in page["items"][0]


async def test_the_link_is_recorded_in_the_activity_feed(client: httpx.AsyncClient) -> None:
    owner = await register(client)
    project = await create(client, owner)
    pid = project["id"]
    await enable(client, owner, pid)
    await client.post(f"{API}/projects/{pid}/public-link/rotate", headers=owner.headers)
    await client.delete(f"{API}/projects/{pid}/public-link", headers=owner.headers)

    feed = unwrap(await client.get(f"{API}/projects/{pid}/activity", headers=owner.headers))
    kinds = {entry["type"] for entry in feed["items"]}
    assert {"PUBLIC_LINK_ENABLED", "PUBLIC_LINK_ROTATED", "PUBLIC_LINK_DISABLED"} <= kinds
