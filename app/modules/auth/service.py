import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_context import get_audit_context
from app.core.config import settings
from app.core.constants import API_TOKEN_PREFIX, TokenType
from app.core.exceptions import AuthenticationError, ConflictError, NotFoundError, ValidationError
from app.core.mailer import send_email_verification, send_password_reset
from app.core.messages import ErrorMessage
from app.core.security import (
    assert_password_strong,
    create_access_token,
    create_refresh_token,
    create_reset_token,
    create_verify_token,
    decode_token,
    fingerprint,
    hash_password,
    new_opaque_token,
    verify_password,
)
from app.core.time import now
from app.modules.audit import service as audit_service
from app.modules.auth.models import ApiToken, RevokedToken, Session
from app.modules.auth.schemas import LoginResponse, RegisterRequest, Token
from app.modules.users import service as user_service
from app.modules.users.models import User, UserCredentials

logger = logging.getLogger("app.auth")


# ── token minting ────────────────────────────────────────────────────────────


def _claims(user: User) -> dict[str, Any]:
    """What the access token carries.

    Identity only — never permissions. A token lives up to an hour; baking the
    role into it would mean a demotion or a deactivation takes an hour to bite.
    The role is read from the row on each request instead, which is one indexed
    primary-key lookup the request was making anyway.
    """
    return {
        "user_id": str(user.id),
        "email": user.email,
        "name": user.display_name,
    }


async def _issue(
    db: AsyncSession, user: User, *, user_agent: str | None = None, ip: str | None = None
) -> Token:
    """Mint an access + refresh pair and record the refresh as a live session."""
    refresh_token = create_refresh_token(str(user.id))
    access_token = create_access_token(_claims(user))
    # The pair is minted together, so recording which access token belongs to
    # which session costs nothing — and it is the only thing that lets a later
    # revocation reach the access token rather than only the refresh token.
    access_payload = decode_token(access_token)
    db.add(
        Session(
            user_id=user.id,
            refresh_token_hash=fingerprint(refresh_token),
            access_jti=str(access_payload.get("jti")),
            access_expires_at=datetime.fromtimestamp(int(access_payload["exp"]), tz=UTC),
            user_agent=(user_agent or "")[:400] or None,
            ip_address=(ip or "")[:64] or None,
            expires_at=now() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
            last_used_at=now(),
        )
    )
    return Token(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


async def _login_response(
    db: AsyncSession, user: User, credentials: UserCredentials
) -> LoginResponse:
    context = get_audit_context()
    token = await _issue(db, user, user_agent=context.user_agent, ip=context.ip)
    count = await user_service.project_count(db, user.id)
    return LoginResponse(
        **token.model_dump(),
        user=user_service.to_me(
            user, must_change_password=credentials.must_change_password, project_count=count
        ),
        must_change_password=credentials.must_change_password,
    )


# ── registration ─────────────────────────────────────────────────────────────


async def register(db: AsyncSession, data: RegisterRequest) -> LoginResponse:
    """Create an account and sign it in.

    Signing in immediately rather than bouncing to a login form: the very next
    thing a new account does is open the project it was invited to, and a
    second password entry buys nothing — the password was just chosen, in this
    tab, seconds ago.
    """
    invited_email = _invited_email(data.invite_token) if data.invite_token else None
    address = user_service.normalise_email(data.email)
    # An invitation is proof the address exists and belongs to whoever read the
    # mail, so it substitutes for the confirmation email — but only for the
    # address it was issued to. A token for someone else's address confirms
    # nothing about this one.
    verified = invited_email == address

    user = await user_service.create_user(
        db, email=address, password=data.password, full_name=data.full_name, email_verified=verified
    )
    # Imported here rather than at module scope: `projects.service` imports the
    # collaboration hub, which imports back into this package.
    from app.modules.projects import service as project_service

    # Only for an address this registration actually proved — the invite token
    # names the address it was issued to, and matching it is the proof.
    #
    # Claiming unconditionally meant anyone who knew an invited address could
    # register it with their own password and be an EDITOR on that project
    # seconds later, reading and writing the whole document, while the real
    # invitee could never register because the address was taken. An address in
    # a registration form is a claim, not evidence. An unproved registration
    # leaves the invitations pending; confirming the address claims them.
    claimed = await project_service.claim_invites(db, user) if verified else 0
    await audit_service.record(
        db,
        "auth.register",
        entity_type="user",
        entity_id=user.id,
        summary=f"{user.email} created an account",
        meta={"claimed_invites": claimed},
    )
    await db.commit()
    await db.refresh(user)

    if not verified:
        await send_email_verification(user.email, create_verify_token(str(user.id), user.email))

    credentials = await db.get(UserCredentials, user.id)
    assert credentials is not None  # just written by create_user
    response = await _login_response(db, user, credentials)
    # `_login_response` records the refresh token as a live session, and that
    # row has to be committed too — without this the token comes back looking
    # valid and is rejected the first time it is used.
    await db.commit()
    return response


def _invited_email(token: str) -> str | None:
    """The address an invite token was issued to, or None if it is not valid."""
    try:
        payload = decode_token(token)
    except jwt.PyJWTError:
        return None
    if payload.get("type") != TokenType.INVITE:
        return None
    email = payload.get("email")
    return user_service.normalise_email(email) if isinstance(email, str) else None


# ── sign in ──────────────────────────────────────────────────────────────────


async def authenticate(db: AsyncSession, email: str, password: str) -> LoginResponse:
    user = await user_service.get_by_email(db, email)
    if user is None or user.is_deleted:
        # Hash-shaped work is skipped here, so a missing account answers faster
        # than a wrong password. That timing difference reveals only whether an
        # address is registered — which `/auth/forgot-password` is required to
        # be honest about anyway — and paying a dummy hash on every probe is a
        # denial-of-service lever, not a defence.
        raise AuthenticationError(ErrorMessage.INVALID_CREDENTIALS)

    if user.locked_until and user.locked_until > now():
        raise AuthenticationError(ErrorMessage.ACCOUNT_LOCKED)

    credentials = await db.get(UserCredentials, user.id)
    if credentials is None or not verify_password(password, credentials.hashed_password):
        await _register_failure(db, user)
        raise AuthenticationError(ErrorMessage.INVALID_CREDENTIALS)

    if not user.is_active:
        raise AuthenticationError(ErrorMessage.INACTIVE_USER)

    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now()
    user.last_seen_at = now()
    await audit_service.record(
        db, "auth.login", entity_type="user", entity_id=user.id, summary=f"{user.email} signed in"
    )
    response = await _login_response(db, user, credentials)
    await db.commit()
    return response


async def _register_failure(db: AsyncSession, user: User) -> None:
    """Count a bad password and lock the account once it is clearly guessing.

    Locking the *account* rather than the IP: the credential is what is under
    attack, and an attacker with a botnet has as many IPs as they like. The
    lock is short and self-clearing so a person who mistyped is not stranded.
    """
    # An atomic UPDATE, not a read-modify-write. `count += 1` on an ORM object
    # loaded by a plain SELECT is a lost update across per-request sessions:
    # eight sequential guesses locked the account and a hundred and fifty
    # concurrent ones did not, which is precisely the attack the lock exists
    # for. The per-IP limiter is no substitute — an attacker with a botnet has
    # as many addresses as they like.
    result = await db.execute(
        update(User)
        .where(User.id == user.id)
        .values(failed_login_count=User.failed_login_count + 1)
        .returning(User.failed_login_count)
        .execution_options(synchronize_session=False)
    )
    count = int(result.scalar_one())
    if count >= settings.MAX_FAILED_LOGINS:
        await db.execute(
            update(User)
            .where(User.id == user.id)
            .values(
                locked_until=now() + timedelta(minutes=settings.LOCKOUT_MINUTES),
                failed_login_count=0,
            )
            .execution_options(synchronize_session=False)
        )
        await audit_service.record(
            db,
            "auth.locked",
            entity_type="user",
            entity_id=user.id,
            summary=f"{user.email} locked after repeated failures",
        )
    await db.commit()


# ── refresh & sign out ───────────────────────────────────────────────────────


async def refresh_tokens(db: AsyncSession, refresh_token: str) -> Token:
    """Exchange a refresh token for a new pair, rotating the old one out.

    Rotation matters: a stolen refresh token works exactly once, and the moment
    the real client refreshes, the thief's copy is a revoked fingerprint.
    """
    try:
        payload = decode_token(refresh_token)
    except jwt.PyJWTError as exc:
        raise AuthenticationError(ErrorMessage.INVALID_REFRESH_TOKEN) from exc
    if payload.get("type") != TokenType.REFRESH:
        raise AuthenticationError(ErrorMessage.INVALID_TOKEN_TYPE)
    if await is_token_revoked(db, payload.get("jti", "")):
        raise AuthenticationError(ErrorMessage.TOKEN_REVOKED)

    user = await _load_active_user(db, _subject(payload))

    session = await _find_session(db, refresh_token)
    if session is None or session.revoked_at is not None or session.expires_at <= now():
        raise AuthenticationError(ErrorMessage.INVALID_REFRESH_TOKEN)
    session.revoked_at = now()
    # The rotated session's ACCESS token too. Without this the sessions screen
    # physically cannot end a device that refreshed recently: the row is marked
    # revoked, so it is hidden, and its access token stays valid for its full
    # hour with nothing left pointing at it.
    #
    # An in-flight request still carrying the pre-refresh access token now 401s
    # and has to retry with the new one. That is what rotation means, and the
    # client already handles 401 -> refresh -> replay.
    await _revoke_session_access(db, session)
    await _revoke_jti(db, payload)

    context = get_audit_context()
    token = await _issue(db, user, user_agent=context.user_agent, ip=context.ip)
    user.last_seen_at = now()
    await db.commit()
    return token


async def logout(
    db: AsyncSession, access_payload: dict[str, Any], refresh_token: str | None
) -> None:
    """Sign out of one device."""
    await _revoke_jti(db, access_payload)

    session: Session | None = None
    if refresh_token:
        session = await _find_session(db, refresh_token)
        try:
            payload = decode_token(refresh_token)
            if payload.get("type") == TokenType.REFRESH:
                await _revoke_jti(db, payload)
        except jwt.PyJWTError:
            pass  # an already-invalid refresh token needs no revoking
    else:
        # No body — a browser that has already dropped its refresh token. The
        # access token alone identifies the session now that its `jti` is
        # recorded on the row. Without this the person is told "Signed out", the
        # access token stops working, and the refresh token quietly keeps
        # minting new ones for the next thirty days.
        jti = access_payload.get("jti")
        if jti:
            found = await db.execute(select(Session).where(Session.access_jti == str(jti)))
            session = found.scalars().first()

    if session is not None:
        if session.revoked_at is None:
            # `refresh_tokens` rejects a session with `revoked_at` set, so the
            # refresh token is dead without needing a denylist row of its own.
            session.revoked_at = now()
        # ...but the ACCESS token it minted is only stopped by the denylist.
        # This was the one site of four that set `revoked_at` and stopped there,
        # which is the exact mistake `access_jti` was added to prevent.
        await _revoke_session_access(db, session)

    await purge_expired_revocations(db)
    await db.commit()


async def logout_everywhere(db: AsyncSession, user: User) -> int:
    """Revoke every live session, access tokens included.

    The filter is on `expires_at`, not `revoked_at`: a session that was already
    rotated away by a refresh still has a live access token out there, and
    "sign out everywhere" has to mean everywhere.

    Personal API tokens are deliberately left alone — GitHub-PAT semantics: a
    token a script holds is not a session, and is revoked only by
    `DELETE /users/me/tokens/{id}`.
    """
    revoked = await revoke_all_sessions(db, user)
    await db.commit()
    return revoked


async def list_sessions(db: AsyncSession, user: User) -> list[Session]:
    """Every device currently signed in as this person.

    Which one is *this* one is answered by the caller matching its own access
    `jti` against `Session.access_jti` — the column added so revocation could
    reach an access token now also tells the sessions screen which row is the
    browser looking at it, so nobody signs themselves out by accident.
    """
    result = await db.execute(
        select(Session)
        .where(Session.user_id == user.id, Session.revoked_at.is_(None), Session.expires_at > now())
        .order_by(Session.last_used_at.desc().nullslast(), Session.created_at.desc())
    )
    return list(result.scalars())


async def revoke_session(db: AsyncSession, user: User, session_id: uuid.UUID) -> None:
    session = await db.get(Session, session_id)
    if session is None or session.user_id != user.id:
        raise AuthenticationError(ErrorMessage.RESOURCE_NOT_FOUND)
    session.revoked_at = now()
    await _revoke_session_access(db, session)
    await db.commit()


# ── passwords ────────────────────────────────────────────────────────────────


async def change_password(
    db: AsyncSession, user: User, current_password: str, new_password: str
) -> None:
    credentials = await db.get(UserCredentials, user.id)
    if credentials is None or not verify_password(current_password, credentials.hashed_password):
        raise AuthenticationError(ErrorMessage.CURRENT_PASSWORD_INCORRECT)
    if verify_password(new_password, credentials.hashed_password):
        raise ValidationError(ErrorMessage.SAME_PASSWORD)
    _apply_chosen_password(credentials, new_password)
    await audit_service.record(
        db,
        "auth.password_changed",
        entity_type="user",
        entity_id=user.id,
        summary=f"{user.email} changed their password",
    )
    await db.commit()


async def set_initial_password(
    db: AsyncSession, user: User, new_password: str, *, keep_jti: str | None = None
) -> None:
    """Replace an issued password. The only route a flagged account may reach.

    Rejects accounts that were never flagged, so this cannot be used as an
    authentication-free password change by anyone holding a stolen access token.
    """
    credentials = await db.get(UserCredentials, user.id)
    if credentials is None:
        raise AuthenticationError(ErrorMessage.USER_NOT_FOUND_OR_INACTIVE)
    if not credentials.must_change_password:
        raise ConflictError("This account already has a chosen password")
    _apply_chosen_password(credentials, new_password)
    # The wall is evaluated per request, so the instant it comes down every
    # token minted from the emailed password becomes fully powerful — including
    # one held by whoever else read that email. Clearing the flag has to retire
    # them, keeping only the session doing the clearing.
    await revoke_all_sessions(db, user, keep_jti=keep_jti)
    await audit_service.record(
        db,
        "auth.initial_password_set",
        entity_type="user",
        entity_id=user.id,
        summary=f"{user.email} set their own password",
    )
    await db.commit()


def _apply_chosen_password(credentials: UserCredentials, new_password: str) -> None:
    """Store a password the person picked themselves.

    Always clears the issued-password flag. Every route reaching here means the
    secret was demonstrably chosen by its owner, and leaving the flag set would
    trap them behind the guard with no way out.
    """
    assert_password_strong(new_password)
    credentials.hashed_password = hash_password(new_password)
    credentials.must_change_password = False
    credentials.password_changed_at = now()


async def forgot_password(db: AsyncSession, email: str) -> str | None:
    """Issue a reset token and email it. Returns the token for non-production.

    Answers identically whether or not the address exists — the *response* is
    the same either way, which is what stops this being an account-enumeration
    oracle.
    """
    user = await user_service.get_by_email(db, email)
    if user is None or not user.is_active or user.is_deleted:
        return None
    token = create_reset_token(str(user.id))
    await send_password_reset(user.email, token)
    await audit_service.record(
        db,
        "auth.password_reset_requested",
        entity_type="user",
        entity_id=user.id,
        summary=f"reset link sent to {user.email}",
    )
    await db.commit()
    return token


async def reset_password(db: AsyncSession, token: str, new_password: str) -> None:
    payload = _decode_typed(token, TokenType.RESET, ErrorMessage.INVALID_RESET_TOKEN)
    if await is_token_revoked(db, payload.get("jti", "")):
        raise AuthenticationError(ErrorMessage.RESET_TOKEN_USED)
    user = await _load_active_user(db, _subject(payload))
    credentials = await db.get(UserCredentials, user.id)
    if credentials is None:
        raise AuthenticationError(ErrorMessage.USER_NOT_FOUND_OR_INACTIVE)
    # Denylisting the consumed `jti` only retires the link that was used. Every
    # other link issued in the last half hour stayed live — so an attacker who
    # triggered a reset first could wait for the owner to complete theirs and
    # then set a password of their own, locking the owner out of the account
    # they had just recovered. Completing one reset closes the whole window.
    issued = payload.get("iat")
    if (
        credentials.password_changed_at is not None
        and issued is not None
        # Both truncated to whole seconds. A JWT's `iat` has one-second
        # resolution while the column has microseconds, so comparing them
        # directly rejects a token minted in the same second as the change that
        # produced the account — which is every reset on a fresh signup.
        and int(issued) < int(credentials.password_changed_at.timestamp())
    ):
        raise AuthenticationError(ErrorMessage.RESET_TOKEN_USED)
    _apply_chosen_password(credentials, new_password)
    await _revoke_jti(db, payload)  # one-time use
    # Anyone holding a session on this account got it with the old password. A
    # reset is exactly the moment to assume that includes someone unwelcome —
    # so this ends the ACCESS tokens too, not just the refresh tokens. Marking
    # the rows revoked and stopping there left the intruder reading and writing
    # for the remaining hour, right after the victim thought they had recovered.
    await revoke_all_sessions(db, user)
    user.failed_login_count = 0
    user.locked_until = None
    await audit_service.record(
        db,
        "auth.password_reset",
        entity_type="user",
        entity_id=user.id,
        summary=f"{user.email} reset their password",
    )
    await db.commit()


# ── email confirmation ───────────────────────────────────────────────────────


async def verify_email(db: AsyncSession, token: str) -> User:
    payload = _decode_typed(token, TokenType.VERIFY, ErrorMessage.INVALID_VERIFY_TOKEN)
    if await is_token_revoked(db, payload.get("jti", "")):
        raise AuthenticationError(ErrorMessage.INVALID_VERIFY_TOKEN)
    user = await _load_active_user(db, _subject(payload))
    # The address is carried in the token, so a link issued before an address
    # change cannot confirm the new one.
    if payload.get("email") != user.email:
        raise AuthenticationError(ErrorMessage.INVALID_VERIFY_TOKEN)
    if user.email_verified_at is None:
        user.email_verified_at = now()
    # The other half of the rule above: an address confirmed here is proved, so
    # anything waiting for it is now theirs. Without this, someone who
    # registered before opening the invitation would confirm their address and
    # still see none of the projects they were told about.
    from app.modules.projects import service as project_service

    claimed = await project_service.claim_invites(db, user)
    if claimed:
        await audit_service.record(
            db,
            "auth.verify_email",
            entity_type="user",
            entity_id=user.id,
            summary=f"{user.email} confirmed their address and claimed {claimed} invitation(s)",
            meta={"claimed_invites": claimed},
        )
    await _revoke_jti(db, payload)
    await db.commit()
    return user


async def resend_verification(db: AsyncSession, user: User) -> None:
    if user.email_verified_at is not None:
        raise ConflictError("This address is already confirmed")
    await send_email_verification(user.email, create_verify_token(str(user.id), user.email))


# ── shared helpers ───────────────────────────────────────────────────────────


def _decode_typed(token: str, expected: TokenType, message: str) -> dict[str, Any]:
    try:
        payload = decode_token(token)
    except jwt.PyJWTError as exc:
        raise AuthenticationError(message) from exc
    if payload.get("type") != expected:
        raise AuthenticationError(ErrorMessage.INVALID_TOKEN_TYPE)
    return payload


def _subject(payload: dict[str, Any]) -> uuid.UUID:
    subject = payload.get("user_id")
    if not isinstance(subject, str):
        raise AuthenticationError()
    try:
        return uuid.UUID(subject)
    except ValueError as exc:
        raise AuthenticationError() from exc


async def _load_active_user(db: AsyncSession, user_id: uuid.UUID) -> User:
    user = await db.get(User, user_id)
    if user is None or not user.is_active or user.is_deleted:
        raise AuthenticationError(ErrorMessage.USER_NOT_FOUND_OR_INACTIVE)
    return user


async def _find_session(db: AsyncSession, refresh_token: str) -> Session | None:
    """Locked, because refresh is the one endpoint a SPA calls twice at once.

    Two parallel requests hitting a 401 both refresh. Unlocked, both pass the
    `revoked_at` check and then collide inserting the same `jti` — the loser
    got an unhandled IntegrityError and a 500, which a client reads as a hard
    failure rather than "retry with the new token". Locked, the loser waits,
    re-reads the row it now sees as revoked, and gets the intended 401.
    """
    result = await db.execute(
        select(Session)
        .where(Session.refresh_token_hash == fingerprint(refresh_token))
        .with_for_update()
    )
    return result.scalar_one_or_none()


async def is_token_revoked(db: AsyncSession, jti: str) -> bool:
    if not jti:
        return False
    return await db.get(RevokedToken, jti) is not None


async def _revoke_jti(db: AsyncSession, payload: dict[str, Any]) -> None:
    """Stage a token id for revocation. The caller commits."""
    jti = payload.get("jti")
    exp = payload.get("exp")
    if not jti or exp is None:
        return
    if not await _already_revoked(db, jti):
        db.add(RevokedToken(jti=jti, expires_at=datetime.fromtimestamp(exp, tz=UTC)))


async def revoke_all_sessions(db: AsyncSession, user: User, *, keep_jti: str | None = None) -> int:
    """End every live sign-in for this account, access tokens included.

    Filtered on `expires_at`, not `revoked_at`: a session already rotated away
    by a refresh still has a live access token out there, and every caller here
    means "nobody else is holding this account".

    `keep_jti` spares the session making the request — the person choosing their
    own password should not be signed out by doing so.
    """
    result = await db.execute(
        select(Session).where(Session.user_id == user.id, Session.expires_at > now())
    )
    ended = 0
    for session in result.scalars():
        if keep_jti and session.access_jti == keep_jti:
            continue
        if session.revoked_at is None:
            session.revoked_at = now()
            ended += 1
        await _revoke_session_access(db, session)
    return ended


async def _revoke_session_access(db: AsyncSession, session: Session) -> None:
    """Deny-list the access token this session issued. The caller commits.

    The existence check is not belt-and-braces. Two revocations of the same
    session are ordinary — deactivating an account and re-issuing its password
    both call `logout_everywhere`, and an operator does both — and a second
    INSERT of the same `jti` is a primary-key violation that surfaces as a 500
    from an admin action that had already half-succeeded.

    Rows written before `access_jti` existed have nothing to revoke; their
    access tokens expired long ago.
    """
    if not session.access_jti:
        return
    expires_at = session.access_expires_at or session.expires_at
    if expires_at <= now():
        return  # already worthless; a denylist row would only be litter
    if await _already_revoked(db, session.access_jti):
        return
    db.add(RevokedToken(jti=session.access_jti, expires_at=expires_at))


async def _already_revoked(db: AsyncSession, jti: str) -> bool:
    """Is this token id on the denylist, or about to be?

    The pending check is the load-bearing half. The session is built with
    `autoflush=False`, so a row added earlier in the same request is invisible
    to `db.get` — and `logout` revokes the caller's access token by `jti` and
    then revokes the session that minted it, which is usually the same token.
    Without this the second insert is a primary-key violation and signing out
    answers 500.
    """
    if any(isinstance(pending, RevokedToken) and pending.jti == jti for pending in db.new):
        return True
    return await db.get(RevokedToken, jti) is not None


async def purge_expired_revocations(db: AsyncSession) -> None:
    """Drop denylist rows whose tokens have expired anyway. Caller commits."""
    await db.execute(delete(RevokedToken).where(RevokedToken.expires_at < now()))


# ── personal API tokens ──────────────────────────────────────────────────────


async def create_api_token(db: AsyncSession, user: User, name: str) -> tuple[ApiToken, str]:
    """Mint a token and return it with its plaintext, which is never stored."""
    plaintext = API_TOKEN_PREFIX + new_opaque_token()
    token = ApiToken(user_id=user.id, name=name, token_hash=fingerprint(plaintext))
    db.add(token)
    await db.commit()
    await db.refresh(token)
    return token, plaintext


async def list_api_tokens(db: AsyncSession, user: User) -> list[ApiToken]:
    result = await db.execute(
        select(ApiToken).where(ApiToken.user_id == user.id).order_by(ApiToken.created_at.desc())
    )
    return list(result.scalars())


async def revoke_api_token(db: AsyncSession, user: User, token_id: uuid.UUID) -> None:
    token = await db.get(ApiToken, token_id)
    if token is None or token.user_id != user.id:
        raise NotFoundError(ErrorMessage.TOKEN_NOT_FOUND)
    token.revoked_at = token.revoked_at or now()
    await db.commit()


async def api_token_payload(db: AsyncSession, plaintext: str) -> dict[str, Any]:
    """Resolve a `pst_…` bearer to the same claims a JWT would have carried.

    A synthetic payload rather than a second identity path: everything
    downstream — the user lookup, the issued-password wall, project access —
    already reads `user_id` off this dict and keeps working untouched. The
    `jti` is namespaced so it can never collide with a real token id on the
    revocation denylist; an API token is revoked by its own `revoked_at`.

    One consequence, deliberate: `POST /auth/logout` carrying a `pst_` token is
    a no-op. There is no session behind it to end, and the denylist would be
    denying a `jti` nothing ever checks. An API token is revoked by
    `DELETE /users/me/tokens/{id}`.
    """
    result = await db.execute(
        select(ApiToken).where(
            ApiToken.token_hash == fingerprint(plaintext), ApiToken.revoked_at.is_(None)
        )
    )
    token = result.scalar_one_or_none()
    if token is None:
        raise AuthenticationError()
    return {"user_id": str(token.user_id), "type": TokenType.ACCESS, "jti": f"pat:{token.id}"}
