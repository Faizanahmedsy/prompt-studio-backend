"""Sign up, sign in, tokens, passwords."""

import httpx
import pytest

from tests.conftest import API, message, register, sign_in, unwrap


async def test_register_returns_a_usable_session(client: httpx.AsyncClient) -> None:
    actor = await register(client, "new.person@example.com", full_name="New Person")
    assert actor.user["email"] == "new.person@example.com"

    response = await client.get(f"{API}/users/me", headers=actor.headers)
    assert unwrap(response)["email"] == "new.person@example.com"


async def test_register_commits_the_session_it_hands_back(client: httpx.AsyncClient) -> None:
    """The refresh token minted at registration must be usable.

    Regression: registration committed the user and *then* minted the tokens,
    so the session row was never written. The response looked perfectly fine
    and the refresh token failed the first time anyone used it.
    """
    actor = await register(client)
    response = await client.post(f"{API}/auth/refresh", json={"refresh_token": actor.refresh_token})
    assert response.status_code == 200, response.text


async def test_email_is_normalised(client: httpx.AsyncClient) -> None:
    await register(client, "Mixed.Case@Example.COM")

    duplicate = await client.post(
        f"{API}/auth/register", json={"email": "mixed.case@example.com", "password": "Password123"}
    )
    assert duplicate.status_code == 409

    signed_in = await client.post(
        f"{API}/auth/login", json={"email": "MIXED.CASE@example.com", "password": "Password123"}
    )
    assert signed_in.status_code == 200


@pytest.mark.parametrize(
    "password",
    ["short1", "alllettersnodigits", "12345678", "       1"],
)
async def test_weak_passwords_are_refused(client: httpx.AsyncClient, password: str) -> None:
    response = await client.post(
        f"{API}/auth/register", json={"email": "weak@example.com", "password": password}
    )
    assert response.status_code == 422


async def test_password_longer_than_bcrypt_allows(client: httpx.AsyncClient) -> None:
    """bcrypt truncates at 72 bytes; two long passwords sharing a prefix would
    otherwise verify against each other."""
    response = await client.post(
        f"{API}/auth/register",
        json={"email": "long@example.com", "password": "a1" + "x" * 200},
    )
    assert response.status_code == 422


async def test_wrong_password_is_rejected(client: httpx.AsyncClient) -> None:
    actor = await register(client)
    response = await client.post(
        f"{API}/auth/login", json={"email": actor.email, "password": "NotThePassword1"}
    )
    assert response.status_code == 401


async def test_unknown_and_wrong_password_read_identically(client: httpx.AsyncClient) -> None:
    actor = await register(client)
    wrong = await client.post(
        f"{API}/auth/login", json={"email": actor.email, "password": "NotThePassword1"}
    )
    missing = await client.post(
        f"{API}/auth/login", json={"email": "nobody@example.com", "password": "NotThePassword1"}
    )
    assert message(wrong) == message(missing)


async def test_repeated_failures_lock_the_account(client: httpx.AsyncClient) -> None:
    actor = await register(client)
    for _ in range(5):
        await client.post(
            f"{API}/auth/login", json={"email": actor.email, "password": "WrongOne111"}
        )
    # Locked — even the correct password is refused, with a different message.
    response = await client.post(
        f"{API}/auth/login", json={"email": actor.email, "password": actor.password}
    )
    assert response.status_code == 401
    assert "Try again" in message(response)


async def test_refresh_rotates_and_invalidates_the_old_token(client: httpx.AsyncClient) -> None:
    actor = await register(client)
    first = unwrap(
        await client.post(f"{API}/auth/refresh", json={"refresh_token": actor.refresh_token})
    )
    assert first["access_token"]

    replay = await client.post(f"{API}/auth/refresh", json={"refresh_token": actor.refresh_token})
    assert replay.status_code == 401, "a rotated refresh token must not work twice"

    again = await client.post(f"{API}/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert again.status_code == 200


async def test_an_access_token_cannot_be_used_as_a_refresh_token(
    client: httpx.AsyncClient,
) -> None:
    actor = await register(client)
    response = await client.post(f"{API}/auth/refresh", json={"refresh_token": actor.access_token})
    assert response.status_code == 401


async def test_a_refresh_token_cannot_authenticate_a_request(client: httpx.AsyncClient) -> None:
    actor = await register(client)
    response = await client.get(
        f"{API}/users/me", headers={"Authorization": f"Bearer {actor.refresh_token}"}
    )
    assert response.status_code == 401


async def test_logout_revokes_the_access_token_immediately(client: httpx.AsyncClient) -> None:
    actor = await register(client)
    logout = await client.post(
        f"{API}/auth/logout", headers=actor.headers, json={"refresh_token": actor.refresh_token}
    )
    assert logout.status_code == 200

    assert (await client.get(f"{API}/users/me", headers=actor.headers)).status_code == 401
    assert (
        await client.post(f"{API}/auth/refresh", json={"refresh_token": actor.refresh_token})
    ).status_code == 401


async def test_logout_without_a_refresh_token_still_works(client: httpx.AsyncClient) -> None:
    """A browser that has already dropped its refresh token must still be able
    to sign out, rather than being told its request body is invalid."""
    actor = await register(client)
    response = await client.post(f"{API}/auth/logout", headers=actor.headers, json={})
    assert response.status_code == 200
    assert (await client.get(f"{API}/users/me", headers=actor.headers)).status_code == 401


async def test_sessions_are_listed_and_revocable(client: httpx.AsyncClient) -> None:
    actor = await register(client)
    await sign_in(client, actor.email, actor.password)

    sessions = unwrap(await client.get(f"{API}/auth/sessions", headers=actor.headers))
    assert len(sessions) == 2
    assert [item["is_current"] for item in sessions].count(True) == 1, (
        "exactly one row is the device asking"
    )

    other = next(item for item in sessions if not item["is_current"])
    revoked = await client.delete(f"{API}/auth/sessions/{other['id']}", headers=actor.headers)
    assert revoked.status_code == 200

    remaining = unwrap(await client.get(f"{API}/auth/sessions", headers=actor.headers))
    assert len(remaining) == 1
    assert remaining[0]["is_current"] is True


async def test_revoking_a_session_kills_its_access_token_immediately(
    client: httpx.AsyncClient,
) -> None:
    """The point of revoking a device you do not recognise.

    A session row held only the refresh token's fingerprint, so revoking one
    stamped `revoked_at` and left the intruder reading and editing for the
    remaining life of their access token — up to an hour after you thought you
    had thrown them out.
    """
    actor = await register(client)
    intruder = await sign_in(client, actor.email, actor.password)
    assert (await client.get(f"{API}/users/me", headers=intruder.headers)).status_code == 200

    sessions = unwrap(await client.get(f"{API}/auth/sessions", headers=actor.headers))
    theirs = next(item for item in sessions if not item["is_current"])
    await client.delete(f"{API}/auth/sessions/{theirs['id']}", headers=actor.headers)

    assert (await client.get(f"{API}/users/me", headers=intruder.headers)).status_code == 401
    # ...and the person who did the revoking is still signed in.
    assert (await client.get(f"{API}/users/me", headers=actor.headers)).status_code == 200


async def test_logout_everywhere_kills_every_refresh_token(client: httpx.AsyncClient) -> None:
    actor = await register(client)
    other = await sign_in(client, actor.email, actor.password)

    result = unwrap(await client.post(f"{API}/auth/logout-everywhere", headers=actor.headers))
    assert result["sessions_revoked"] == 2
    assert (
        await client.post(f"{API}/auth/refresh", json={"refresh_token": other.refresh_token})
    ).status_code == 401


async def test_change_password(client: httpx.AsyncClient) -> None:
    actor = await register(client)
    wrong = await client.post(
        f"{API}/auth/change-password",
        headers=actor.headers,
        json={"current_password": "NotIt12345", "new_password": "Brandnew123"},
    )
    assert wrong.status_code == 401

    same = await client.post(
        f"{API}/auth/change-password",
        headers=actor.headers,
        json={"current_password": actor.password, "new_password": actor.password},
    )
    assert same.status_code == 422, "reusing the current password is not a change"

    changed = await client.post(
        f"{API}/auth/change-password",
        headers=actor.headers,
        json={"current_password": actor.password, "new_password": "Brandnew123"},
    )
    assert changed.status_code == 200
    assert (await sign_in(client, actor.email, "Brandnew123")).access_token


async def test_forgot_and_reset_password(client: httpx.AsyncClient) -> None:
    actor = await register(client)
    issued = unwrap(await client.post(f"{API}/auth/forgot-password", json={"email": actor.email}))
    token = issued["reset_token"]
    assert token, "outside production the token comes back so the flow is testable"

    reset = await client.post(
        f"{API}/auth/reset-password", json={"token": token, "new_password": "Recovered123"}
    )
    assert reset.status_code == 200
    assert (await sign_in(client, actor.email, "Recovered123")).access_token

    replay = await client.post(
        f"{API}/auth/reset-password", json={"token": token, "new_password": "Another12345"}
    )
    assert replay.status_code == 401, "a reset link is single-use"


async def test_reset_ends_existing_sessions(client: httpx.AsyncClient) -> None:
    """Whoever held a session got it with the old password. A reset is exactly
    when to assume that might not have been the owner."""
    actor = await register(client)
    token = unwrap(await client.post(f"{API}/auth/forgot-password", json={"email": actor.email}))[
        "reset_token"
    ]
    await client.post(
        f"{API}/auth/reset-password", json={"token": token, "new_password": "Recovered123"}
    )
    assert (
        await client.post(f"{API}/auth/refresh", json={"refresh_token": actor.refresh_token})
    ).status_code == 401


async def test_forgot_password_does_not_reveal_whether_an_account_exists(
    client: httpx.AsyncClient,
) -> None:
    known = await register(client)
    a = await client.post(f"{API}/auth/forgot-password", json={"email": known.email})
    b = await client.post(f"{API}/auth/forgot-password", json={"email": "ghost@example.com"})
    assert a.status_code == b.status_code == 200
    assert message(a) == message(b)


async def test_reset_clears_a_lockout(client: httpx.AsyncClient) -> None:
    """Otherwise the recovery path leaves you locked out of the account you
    just recovered."""
    actor = await register(client)
    for _ in range(5):
        await client.post(
            f"{API}/auth/login", json={"email": actor.email, "password": "WrongOne111"}
        )
    token = unwrap(await client.post(f"{API}/auth/forgot-password", json={"email": actor.email}))[
        "reset_token"
    ]
    await client.post(
        f"{API}/auth/reset-password", json={"token": token, "new_password": "Recovered123"}
    )
    response = await client.post(
        f"{API}/auth/login", json={"email": actor.email, "password": "Recovered123"}
    )
    assert response.status_code == 200


async def test_email_verification(client: httpx.AsyncClient) -> None:
    from app.core.security import create_verify_token

    actor = await register(client)
    assert (
        unwrap(await client.get(f"{API}/users/me", headers=actor.headers))["email_verified_at"]
        is None
    )

    token = create_verify_token(actor.id, actor.email)
    assert (await client.post(f"{API}/auth/verify-email", json={"token": token})).status_code == 200
    assert unwrap(await client.get(f"{API}/users/me", headers=actor.headers))["email_verified_at"]

    replay = await client.post(f"{API}/auth/verify-email", json={"token": token})
    assert replay.status_code == 401, "a confirmation link is single-use"


async def test_a_verification_token_for_another_address_does_nothing(
    client: httpx.AsyncClient,
) -> None:
    from app.core.security import create_verify_token

    actor = await register(client)
    token = create_verify_token(actor.id, "someone.else@example.com")
    response = await client.post(f"{API}/auth/verify-email", json={"token": token})
    assert response.status_code == 401


async def test_garbage_tokens_are_rejected_not_crashed(client: httpx.AsyncClient) -> None:
    for token in ["", "not-a-jwt", "a.b.c", "Bearer x"]:
        response = await client.get(f"{API}/users/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code in (401, 403), token


async def test_removing_your_avatar_is_expressible(client: httpx.AsyncClient) -> None:
    """`exclude_none` collapsed "not sent" with "sent as null", so the request
    answered `success: true` and changed nothing — and no other route can clear
    the column, which made "remove photo" unimplementable."""
    actor = await register(client)
    with_photo = unwrap(
        await client.patch(
            f"{API}/users/me",
            headers=actor.headers,
            json={"avatar_url": "https://example.com/me.png"},
        )
    )
    assert with_photo["avatar_url"] == "https://example.com/me.png"

    cleared = unwrap(
        await client.patch(f"{API}/users/me", headers=actor.headers, json={"avatar_url": None})
    )
    assert cleared["avatar_url"] is None

    # A null aimed at a NOT NULL column is ignored rather than crashing.
    kept = unwrap(
        await client.patch(f"{API}/users/me", headers=actor.headers, json={"full_name": None})
    )
    assert kept["full_name"] == actor.user["full_name"]
