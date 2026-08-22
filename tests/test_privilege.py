"""The boundary between an ADMIN and a SUPERADMIN.

The RBAC matrix withholds the irreversible half — deleting accounts, granting
roles — "so an ADMIN can run day-to-day user management without being able to
promote themselves". Every test here is a way that sentence was false.
"""

import httpx

from tests.conftest import API, make_superadmin, register, sign_in, unwrap


async def superadmin(client: httpx.AsyncClient):  # type: ignore[no-untyped-def]
    actor = await register(client, full_name="The Boss")
    await make_superadmin(actor.email)
    return await sign_in(client, actor.email, actor.password)


async def admin(client: httpx.AsyncClient, boss, email: str):  # type: ignore[no-untyped-def]
    created = unwrap(
        await client.post(
            f"{API}/admin/users",
            headers=boss.headers,
            json={"email": email, "full_name": "Operator", "role": "ADMIN"},
        )
    )
    signed_in = await sign_in(client, email, created["temporary_password"])
    await client.post(
        f"{API}/auth/set-initial-password",
        headers=signed_in.headers,
        json={"new_password": "AdminChosen1"},
    )
    return await sign_in(client, email, "AdminChosen1")


async def _find(client: httpx.AsyncClient, boss, email: str) -> str:  # type: ignore[no-untyped-def]
    listing = unwrap(
        await client.get(f"{API}/admin/users?search={email.split('@')[0]}", headers=boss.headers)
    )
    return str(listing["items"][0]["id"])


async def test_an_admin_cannot_reset_a_superadmins_password(
    client: httpx.AsyncClient,
) -> None:
    """The full takeover this closes.

    An ADMIN reset a SUPERADMIN's password, read the plaintext out of the
    response body, signed in as them, cleared the must-change-password flag
    through the route that exists for exactly that, and held the whole
    platform — with the audit log recording it as routine user management.
    """
    boss = await superadmin(client)
    operator = await admin(client, boss, "operator@example.com")
    boss_id = await _find(client, boss, boss.email)

    refused = await client.patch(
        f"{API}/admin/users/{boss_id}", headers=operator.headers, json={"reset_password": True}
    )
    assert refused.status_code == 403
    assert "superadmin" in refused.json()["message"].lower()

    # The victim's own password still works, so nothing was changed on the way out.
    assert (await sign_in(client, boss.email, boss.password)).access_token


async def test_an_admin_cannot_reset_another_admins_password(
    client: httpx.AsyncClient,
) -> None:
    """Peers too — otherwise two ADMINs are one compromise away from each other."""
    boss = await superadmin(client)
    one = await admin(client, boss, "one@example.com")
    await admin(client, boss, "two@example.com")
    two_id = await _find(client, boss, "two@example.com")

    refused = await client.patch(
        f"{API}/admin/users/{two_id}", headers=one.headers, json={"reset_password": True}
    )
    assert refused.status_code == 403


async def test_an_admin_cannot_deactivate_or_demote_a_peer(
    client: httpx.AsyncClient,
) -> None:
    boss = await superadmin(client)
    one = await admin(client, boss, "one@example.com")
    await admin(client, boss, "two@example.com")
    two_id = await _find(client, boss, "two@example.com")

    for body in ({"is_active": False}, {"role": "MEMBER"}, {"full_name": "Renamed"}):
        refused = await client.patch(f"{API}/admin/users/{two_id}", headers=one.headers, json=body)
        assert refused.status_code == 403, body


async def test_an_admin_still_manages_ordinary_accounts(client: httpx.AsyncClient) -> None:
    """The fix must not take away the job the role exists for."""
    boss = await superadmin(client)
    operator = await admin(client, boss, "operator@example.com")
    member = await register(client)
    member_id = await _find(client, boss, member.email)

    renamed = await client.patch(
        f"{API}/admin/users/{member_id}", headers=operator.headers, json={"full_name": "Renamed"}
    )
    assert renamed.status_code == 200

    issued = await client.patch(
        f"{API}/admin/users/{member_id}", headers=operator.headers, json={"reset_password": True}
    )
    assert issued.status_code == 200
    assert unwrap(issued)["temporary_password"]

    deactivated = await client.patch(
        f"{API}/admin/users/{member_id}", headers=operator.headers, json={"is_active": False}
    )
    assert deactivated.status_code == 200


async def test_a_superadmin_may_still_manage_an_admin(client: httpx.AsyncClient) -> None:
    boss = await superadmin(client)
    await admin(client, boss, "operator@example.com")
    operator_id = await _find(client, boss, "operator@example.com")

    demoted = await client.patch(
        f"{API}/admin/users/{operator_id}", headers=boss.headers, json={"role": "MEMBER"}
    )
    assert demoted.status_code == 200


async def test_an_admin_can_still_edit_their_own_account(client: httpx.AsyncClient) -> None:
    """Self-management is not the thing being prevented."""
    boss = await superadmin(client)
    operator = await admin(client, boss, "operator@example.com")
    operator_id = await _find(client, boss, "operator@example.com")

    renamed = await client.patch(
        f"{API}/admin/users/{operator_id}",
        headers=operator.headers,
        json={"full_name": "Still Me"},
    )
    assert renamed.status_code == 200


async def test_an_admin_created_account_claims_its_invitations(
    client: httpx.AsyncClient,
) -> None:
    """`user_id` on the membership row is the key every eviction path uses.

    Self-registration claims pending invitations; account creation from the
    admin surface did not. The address still had access — it is matched on the
    email — but the row kept `user_id = NULL` forever, so removing that person
    from a project silently did nothing to the editor they had open.
    """
    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.modules.projects.models import ProjectMember

    boss = await superadmin(client)
    project = unwrap(
        await client.post(f"{API}/projects", headers=boss.headers, json={"name": "Shared"})
    )
    await client.post(
        f"{API}/projects/{project['id']}/members",
        headers=boss.headers,
        json={"email": "provisioned@example.com", "role": "EDITOR"},
    )

    created = unwrap(
        await client.post(
            f"{API}/admin/users", headers=boss.headers, json={"email": "provisioned@example.com"}
        )
    )

    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(ProjectMember).where(ProjectMember.email == "provisioned@example.com")
            )
        ).scalar_one()
        assert row.user_id is not None, "the eviction key was never filled in"
        assert str(row.user_id) == created["user_id"]
        assert row.status == "ACTIVE"


async def test_deactivating_an_account_is_recorded_against_its_sockets(
    client: httpx.AsyncClient,
) -> None:
    """`logout_everywhere` ends the HTTP session. A websocket authenticated at
    its handshake knows nothing about that, so the admin path has to close it
    explicitly — this asserts the call is wired, not the socket behaviour, which
    `tests/test_collab.py` covers with a real connection."""
    boss = await superadmin(client)
    victim = await register(client)
    victim_id = await _find(client, boss, victim.email)

    response = await client.patch(
        f"{API}/admin/users/{victim_id}", headers=boss.headers, json={"is_active": False}
    )
    assert response.status_code == 200
    assert (await client.get(f"{API}/users/me", headers=victim.headers)).status_code == 401
