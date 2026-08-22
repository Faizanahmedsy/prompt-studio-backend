"""The admin surface: user management, guards, audit trail."""

import httpx

from tests.conftest import API, make_superadmin, register, sign_in, unwrap


async def superadmin(client: httpx.AsyncClient):  # type: ignore[no-untyped-def]
    actor = await register(client, full_name="The Boss")
    await make_superadmin(actor.email)
    return await sign_in(client, actor.email, actor.password)


async def admin(client: httpx.AsyncClient, boss):  # type: ignore[no-untyped-def]
    """An ADMIN, created by a superadmin — only they may grant the role."""
    created = unwrap(
        await client.post(
            f"{API}/admin/users",
            headers=boss.headers,
            json={"email": "admin.person@example.com", "full_name": "Admin", "role": "ADMIN"},
        )
    )
    signed_in = await sign_in(client, created["email"], created["temporary_password"])
    await client.post(
        f"{API}/auth/set-initial-password",
        headers=signed_in.headers,
        json={"new_password": "AdminChosen1"},
    )
    return await sign_in(client, created["email"], "AdminChosen1")


async def test_a_member_cannot_reach_the_admin_surface(client: httpx.AsyncClient) -> None:
    actor = await register(client)
    for path in ("/admin/stats", "/admin/users", "/admin/projects", "/admin/audit", "/admin/roles"):
        response = await client.get(f"{API}{path}", headers=actor.headers)
        assert response.status_code == 403, path


async def test_stats(client: httpx.AsyncClient) -> None:
    boss = await superadmin(client)
    await register(client)
    stats = unwrap(await client.get(f"{API}/admin/stats", headers=boss.headers))
    assert stats["users_total"] >= 2
    assert stats["admins"] == 1
    assert stats["signups_last_7_days"] >= 2


async def test_creating_a_user_issues_a_one_time_password(client: httpx.AsyncClient) -> None:
    boss = await superadmin(client)
    created = unwrap(
        await client.post(
            f"{API}/admin/users",
            headers=boss.headers,
            json={"email": "new.hire@example.com", "full_name": "New Hire", "role": "MEMBER"},
        )
    )
    temporary = created["temporary_password"]
    assert temporary and len(temporary) >= 12

    signed_in = await sign_in(client, "new.hire@example.com", temporary)
    assert signed_in.user["must_change_password"] is True

    # The issued credential opens the door and nothing else.
    assert (await client.get(f"{API}/projects", headers=signed_in.headers)).status_code == 403
    assert (await client.get(f"{API}/users/me", headers=signed_in.headers)).status_code == 200
    assert (
        await client.post(f"{API}/auth/logout", headers=signed_in.headers, json={})
    ).status_code == 200

    signed_in = await sign_in(client, "new.hire@example.com", temporary)
    assert (
        await client.post(
            f"{API}/auth/set-initial-password",
            headers=signed_in.headers,
            json={"new_password": "TheirOwn123"},
        )
    ).status_code == 200
    assert (await client.get(f"{API}/projects", headers=signed_in.headers)).status_code == 200


async def test_a_supplied_password_is_not_echoed_back(client: httpx.AsyncClient) -> None:
    """Only a password the platform generated is returned. One the operator
    typed is already theirs, and repeating it is pure exposure."""
    boss = await superadmin(client)
    created = unwrap(
        await client.post(
            f"{API}/admin/users",
            headers=boss.headers,
            json={"email": "chosen@example.com", "password": "OperatorSet1", "role": "MEMBER"},
        )
    )
    assert created["temporary_password"] is None
    assert (await sign_in(client, "chosen@example.com", "OperatorSet1")).access_token


async def test_set_initial_password_is_refused_on_a_normal_account(
    client: httpx.AsyncClient,
) -> None:
    """It skips the current-password check, so it must only ever apply to an
    account that really is holding an issued credential."""
    actor = await register(client)
    response = await client.post(
        f"{API}/auth/set-initial-password",
        headers=actor.headers,
        json={"new_password": "Sneaky12345"},
    )
    assert response.status_code == 409


async def test_only_a_superadmin_may_grant_admin_roles(client: httpx.AsyncClient) -> None:
    boss = await superadmin(client)
    operator = await admin(client, boss)

    blocked = await client.post(
        f"{API}/admin/users",
        headers=operator.headers,
        json={"email": "promoted@example.com", "role": "SUPERADMIN"},
    )
    assert blocked.status_code == 422

    ordinary = await client.post(
        f"{API}/admin/users",
        headers=operator.headers,
        json={"email": "ordinary@example.com", "role": "MEMBER"},
    )
    assert ordinary.status_code == 201


async def test_an_admin_cannot_promote_themselves(client: httpx.AsyncClient) -> None:
    boss = await superadmin(client)
    operator = await admin(client, boss)
    listing = unwrap(
        await client.get(f"{API}/admin/users?search=admin.person", headers=operator.headers)
    )
    own_id = listing["items"][0]["id"]
    response = await client.patch(
        f"{API}/admin/users/{own_id}", headers=operator.headers, json={"role": "SUPERADMIN"}
    )
    assert response.status_code == 422


async def test_deleting_a_user_needs_a_superadmin(client: httpx.AsyncClient) -> None:
    boss = await superadmin(client)
    operator = await admin(client, boss)
    victim = await register(client)
    listing = unwrap(
        await client.get(
            f"{API}/admin/users?search={victim.email.split('@')[0]}", headers=boss.headers
        )
    )
    victim_id = listing["items"][0]["id"]

    assert (
        await client.delete(f"{API}/admin/users/{victim_id}", headers=operator.headers)
    ).status_code == 403
    assert (
        await client.delete(f"{API}/admin/users/{victim_id}", headers=boss.headers)
    ).status_code == 200
    assert (await client.get(f"{API}/users/me", headers=victim.headers)).status_code == 401


async def test_deactivating_ends_the_sessions_that_are_already_open(
    client: httpx.AsyncClient,
) -> None:
    """Otherwise "deactivated" means nothing until the token happens to expire."""
    boss = await superadmin(client)
    victim = await register(client)
    listing = unwrap(
        await client.get(
            f"{API}/admin/users?search={victim.email.split('@')[0]}", headers=boss.headers
        )
    )
    victim_id = listing["items"][0]["id"]

    assert (await client.get(f"{API}/users/me", headers=victim.headers)).status_code == 200
    await client.patch(
        f"{API}/admin/users/{victim_id}", headers=boss.headers, json={"is_active": False}
    )
    assert (await client.get(f"{API}/users/me", headers=victim.headers)).status_code == 401
    assert (
        await client.post(f"{API}/auth/refresh", json={"refresh_token": victim.refresh_token})
    ).status_code == 401
    assert (
        await client.post(
            f"{API}/auth/login", json={"email": victim.email, "password": victim.password}
        )
    ).status_code == 401


async def test_reissuing_a_password_invalidates_the_old_one(client: httpx.AsyncClient) -> None:
    boss = await superadmin(client)
    victim = await register(client)
    listing = unwrap(
        await client.get(
            f"{API}/admin/users?search={victim.email.split('@')[0]}", headers=boss.headers
        )
    )
    victim_id = listing["items"][0]["id"]

    issued = unwrap(
        await client.patch(
            f"{API}/admin/users/{victim_id}", headers=boss.headers, json={"reset_password": True}
        )
    )
    assert issued["temporary_password"]
    assert (
        await client.post(
            f"{API}/auth/login", json={"email": victim.email, "password": victim.password}
        )
    ).status_code == 401
    assert (await sign_in(client, victim.email, issued["temporary_password"])).access_token


async def test_the_last_superadmin_cannot_be_removed(client: httpx.AsyncClient) -> None:
    boss = await superadmin(client)
    listing = unwrap(await client.get(f"{API}/admin/users?role=SUPERADMIN", headers=boss.headers))
    own_id = listing["items"][0]["id"]

    # Not by demotion, not by deactivation, not by deletion.
    assert (
        await client.patch(
            f"{API}/admin/users/{own_id}", headers=boss.headers, json={"role": "MEMBER"}
        )
    ).status_code == 409
    assert (
        await client.patch(
            f"{API}/admin/users/{own_id}", headers=boss.headers, json={"is_active": False}
        )
    ).status_code == 409
    assert (
        await client.delete(f"{API}/admin/users/{own_id}", headers=boss.headers)
    ).status_code == 409


async def test_user_list_filters_and_counts(client: httpx.AsyncClient) -> None:
    boss = await superadmin(client)
    person = await register(client, "findable@example.com", full_name="Findable Person")
    await client.post(f"{API}/projects", headers=person.headers, json={"name": "Theirs"})

    found = unwrap(await client.get(f"{API}/admin/users?search=findable", headers=boss.headers))
    assert found["total"] == 1
    row = found["items"][0]
    assert row["project_count"] == 1
    assert row["owned_project_count"] == 1

    by_role = unwrap(await client.get(f"{API}/admin/users?role=MEMBER", headers=boss.headers))
    assert all(item["role"] == "MEMBER" for item in by_role["items"])


async def test_audit_trail_records_the_events_that_matter(client: httpx.AsyncClient) -> None:
    boss = await superadmin(client)
    unwrap(
        await client.post(
            f"{API}/admin/users",
            headers=boss.headers,
            json={"email": "audited@example.com", "role": "MEMBER"},
        )
    )
    logs = unwrap(await client.get(f"{API}/admin/audit", headers=boss.headers))
    actions = {entry["action"] for entry in logs["items"]}
    assert "admin.user_created" in actions
    assert "auth.login" in actions
    assert "auth.register" in actions

    filtered = unwrap(
        await client.get(f"{API}/admin/audit?action=admin.user_created", headers=boss.headers)
    )
    assert filtered["total"] == 1
    assert filtered["items"][0]["actor_email"] == boss.email

    # A namespace prefix is the filter an operator actually reaches for.
    namespace = unwrap(await client.get(f"{API}/admin/audit?action=auth", headers=boss.headers))
    assert namespace["total"] >= 2
    assert all(entry["action"].startswith("auth") for entry in namespace["items"])


async def test_the_role_matrix_is_served_rather_than_duplicated(
    client: httpx.AsyncClient,
) -> None:
    boss = await superadmin(client)
    roles = unwrap(await client.get(f"{API}/admin/roles", headers=boss.headers))
    matrix = {entry["role"]: set(entry["permissions"]) for entry in roles}
    assert matrix["MEMBER"] == set()
    assert "users.read" in matrix["ADMIN"]
    assert "users.delete" not in matrix["ADMIN"]
    assert matrix["ADMIN"] < matrix["SUPERADMIN"]


async def test_admin_deletes_a_project_without_ever_reading_it(
    client: httpx.AsyncClient,
) -> None:
    boss = await superadmin(client)
    owner = await register(client)
    project = unwrap(
        await client.post(
            f"{API}/projects",
            headers=owner.headers,
            json={"name": "Objectionable", "doc": {"screens": []}},
        )
    )
    assert (
        await client.delete(f"{API}/admin/projects/{project['id']}", headers=boss.headers)
    ).status_code == 200
    assert unwrap(await client.get(f"{API}/projects", headers=owner.headers))["total"] == 0
    still_visible = unwrap(
        await client.get(f"{API}/admin/projects?include_deleted=true", headers=boss.headers)
    )
    assert still_visible["items"][0]["is_deleted"] is True


async def test_deactivating_twice_does_not_crash(client: httpx.AsyncClient) -> None:
    """Revoking a session's access token has to be idempotent.

    Deactivating an account and re-issuing its password both end every open
    session, and an operator dealing with a compromise does both — a second
    denylist INSERT of the same token id is a primary-key violation, surfacing
    as a 500 from an action that had already half-succeeded.
    """
    boss = await superadmin(client)
    victim = await register(client)
    await sign_in(client, victim.email, victim.password)  # a second device
    listing = unwrap(
        await client.get(
            f"{API}/admin/users?search={victim.email.split('@')[0]}", headers=boss.headers
        )
    )
    victim_id = listing["items"][0]["id"]

    for body in ({"reset_password": True}, {"is_active": False}, {"is_active": True}):
        response = await client.patch(
            f"{API}/admin/users/{victim_id}", headers=boss.headers, json=body
        )
        assert response.status_code == 200, f"{body} -> {response.text[:300]}"
