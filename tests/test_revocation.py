"""Ending a session has to end the *access* token, not just the refresh token.

A `Session` row records only a fingerprint of the refresh token, so for a while
every "sign out" here marked the row revoked and stopped — leaving whoever held
the access token reading and writing for its full hour. The original test suite
missed it because it only ever replayed the refresh token.
"""

import httpx

from tests.conftest import API, make_superadmin, register, sign_in, unwrap


async def test_reset_password_ends_the_access_token_too(client: httpx.AsyncClient) -> None:
    """The one recovery action an unauthenticated victim has.

    Someone resets their password precisely because they believe another person
    is in the account. Leaving that person's access token alive for the next
    hour is the opposite of what the button says it does.
    """
    victim = await register(client)
    intruder = await sign_in(client, victim.email, victim.password)
    assert (await client.get(f"{API}/users/me", headers=intruder.headers)).status_code == 200

    token = unwrap(await client.post(f"{API}/auth/forgot-password", json={"email": victim.email}))[
        "reset_token"
    ]
    await client.post(
        f"{API}/auth/reset-password", json={"token": token, "new_password": "Recovered123"}
    )

    assert (await client.get(f"{API}/users/me", headers=intruder.headers)).status_code == 401
    assert (await client.get(f"{API}/users/me", headers=victim.headers)).status_code == 401
    assert (
        await client.post(f"{API}/auth/refresh", json={"refresh_token": intruder.refresh_token})
    ).status_code == 401


async def test_refreshing_retires_the_access_token_it_replaces(
    client: httpx.AsyncClient,
) -> None:
    """Otherwise the sessions screen cannot end a device that refreshed recently:
    the row is marked revoked, so it is hidden, and its access token lives on
    with nothing left pointing at it."""
    actor = await register(client)
    old_access = actor.access_token

    refreshed = unwrap(
        await client.post(f"{API}/auth/refresh", json={"refresh_token": actor.refresh_token})
    )
    assert (
        await client.get(f"{API}/users/me", headers={"Authorization": f"Bearer {old_access}"})
    ).status_code == 401
    assert (
        await client.get(
            f"{API}/users/me", headers={"Authorization": f"Bearer {refreshed['access_token']}"}
        )
    ).status_code == 200


async def test_choosing_your_own_password_retires_the_issued_one(
    client: httpx.AsyncClient,
) -> None:
    """The wall is evaluated per request, so the instant it comes down every
    token minted from the emailed password becomes fully powerful — including
    one held by whoever else read that email."""
    boss = await register(client)
    await make_superadmin(boss.email)
    boss = await sign_in(client, boss.email, boss.password)

    created = unwrap(
        await client.post(
            f"{API}/admin/users", headers=boss.headers, json={"email": "issued@example.com"}
        )
    )
    temporary = created["temporary_password"]

    # Two sessions from the same emailed credential: the real owner, and someone
    # else who read the mail.
    owner = await sign_in(client, "issued@example.com", temporary)
    eavesdropper = await sign_in(client, "issued@example.com", temporary)

    chosen = await client.post(
        f"{API}/auth/set-initial-password",
        headers=owner.headers,
        json={"new_password": "MineNow1234"},
    )
    assert chosen.status_code == 200

    # The owner keeps working; the other session is gone.
    assert (await client.get(f"{API}/projects", headers=owner.headers)).status_code == 200
    assert (await client.get(f"{API}/users/me", headers=eavesdropper.headers)).status_code == 401


async def test_logout_without_a_body_still_ends_the_refresh_token(
    client: httpx.AsyncClient,
) -> None:
    """The person is told "Signed out". Before this the access token stopped
    working and the refresh token quietly kept minting new ones for a month."""
    actor = await register(client)
    assert (
        await client.post(f"{API}/auth/logout", headers=actor.headers, json={})
    ).status_code == 200
    assert (await client.get(f"{API}/users/me", headers=actor.headers)).status_code == 401
    assert (
        await client.post(f"{API}/auth/refresh", json={"refresh_token": actor.refresh_token})
    ).status_code == 401


async def test_a_long_forwarded_header_does_not_break_sign_in(
    client: httpx.AsyncClient,
) -> None:
    """`X-Forwarded-For` is attacker-controlled and lands in a 64-character
    column. Oversized, it failed the audit INSERT — which shares the request's
    transaction, so the sign-in rolled back with a 500 *and* left no trace of
    the attempt."""
    actor = await register(client)
    response = await client.post(
        f"{API}/auth/login",
        json={"email": actor.email, "password": actor.password},
        headers={"x-forwarded-for": "1.2.3.4" + ("0" * 5000)},
    )
    assert response.status_code == 200, response.text[:300]
