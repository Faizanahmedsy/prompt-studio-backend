"""Outbound mail.

Three transports:

- ``console`` (the default) logs a fully rendered message. Invites and password
  resets are then testable on a laptop with no mail account, and the link is
  right there in the server log.
- ``resend`` posts to Resend's HTTP API. This is the one to use on Render:
  outbound SMTP is not dependably open there, and a blocked port 587 looks
  exactly like a password reset that was never requested.
- ``smtp`` sends through a mail server directly.

Sending **never raises into the request**: a mail outage must not fail the
invite that was already written to the database. Failures are logged and the
caller carries on. SMTP additionally runs on a thread, because ``smtplib``
blocks.
"""

import asyncio
import logging
import smtplib
from email.message import EmailMessage

import httpx

from app.core.config import settings

logger = logging.getLogger("app.mail")


def _scrub(text: str) -> str:
    """Never let a credential reach the log, whatever raised it.

    Transport errors quote the offending HTTP header, so an unusable API key
    can arrive here inside an exception message. Belt and braces with the
    stripping validator in `config`.
    """
    for secret in (settings.RESEND_API_KEY, settings.SMTP_PASSWORD):
        if secret and len(secret) >= 8:
            text = text.replace(secret, "***")
    return text


def _build(to: str, subject: str, body: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = settings.MAIL_FROM
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    return message


def _send_sync(message: EmailMessage) -> None:
    with smtplib.SMTP(
        settings.SMTP_HOST, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT_SECONDS
    ) as smtp:
        if settings.SMTP_STARTTLS:
            smtp.starttls()
        if settings.SMTP_USER:
            smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        smtp.send_message(message)


async def _send_resend(to: str, subject: str, body: str) -> bool:
    """POST one message to Resend. Returns whether it was accepted."""
    async with httpx.AsyncClient(timeout=settings.RESEND_TIMEOUT_SECONDS) as client:
        response = await client.post(
            f"{settings.RESEND_BASE_URL.rstrip('/')}/emails",
            headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
            json={
                "from": settings.MAIL_FROM,
                "to": [to],
                "subject": subject,
                "text": body,
            },
        )
    if response.status_code >= 400:
        # The body carries the actual reason — an unverified sending domain, a
        # recipient the sandbox will not deliver to — and without it the log
        # says only "422" for a problem that takes one DNS record to fix.
        logger.warning(
            "Resend rejected mail to %s (%s): %s",
            to,
            response.status_code,
            _scrub(response.text[:500]),
        )
        return False
    logger.info("Sent mail to %s via Resend (id=%s)", to, response.json().get("id"))
    return True


async def send_mail(to: str, subject: str, body: str) -> bool:
    """Deliver one message. Returns whether it went out; never raises."""
    if settings.EMAIL_TRANSPORT == "smtp" and not settings.SMTP_HOST:
        # Asked to send, and cannot. Falling back to the console here would put
        # reset links, invite tokens and admin-issued passwords into the
        # container log — where a half-configured production deployment would
        # quietly leave every credential it ever issued. Failing loudly is the
        # safer half of that choice.
        logger.error("EMAIL_TRANSPORT=smtp but SMTP_HOST is empty; mail to %s was NOT sent", to)
        return False
    if settings.EMAIL_TRANSPORT == "resend" and not settings.RESEND_API_KEY:
        # Same reasoning as the SMTP guard above: falling back to the console
        # would write live reset tokens into the container log.
        logger.error(
            "EMAIL_TRANSPORT=resend but RESEND_API_KEY is empty; mail to %s was NOT sent", to
        )
        return False
    if settings.EMAIL_TRANSPORT == "console":
        logger.info("[mail:console] to=%s subject=%s\n%s", to, subject, body)
        return True
    try:
        if settings.EMAIL_TRANSPORT == "resend":
            return await _send_resend(to, subject, body)
        await asyncio.to_thread(_send_sync, _build(to, subject, body))
    except Exception as exc:  # noqa: BLE001 - mail must never break the request
        logger.warning("Could not send mail to %s (%s)", to, _scrub(str(exc)))
        return False
    return True


# ── Templates ────────────────────────────────────────────────────────────────
# Plain text on purpose. These are transactional one-liners with a link, and a
# text body renders in every client without a layout to maintain.


async def send_password_reset(to: str, token: str) -> None:
    link = f"{settings.FRONTEND_BASE_URL}/reset-password?token={token}"
    await send_mail(
        to,
        "Reset your Prompt Studio password",
        f"Someone asked to reset the password for this address.\n\n"
        f"{link}\n\n"
        f"The link expires in {settings.RESET_TOKEN_EXPIRE_MINUTES} minutes. "
        f"If it wasn't you, ignore this email — nothing has changed.",
    )


async def send_email_verification(to: str, token: str) -> None:
    link = f"{settings.FRONTEND_BASE_URL}/verify-email?token={token}"
    await send_mail(
        to,
        "Confirm your email address",
        f"Confirm this address to finish setting up your Prompt Studio account.\n\n{link}\n",
    )


async def send_project_invite(
    to: str, project_name: str, inviter: str, token: str, is_new_user: bool
) -> None:
    path = "register" if is_new_user else "login"
    link = f"{settings.FRONTEND_BASE_URL}/{path}?invite={token}"
    joining = (
        "Create an account with this email address to open it:"
        if is_new_user
        else "Sign in to open it:"
    )
    await send_mail(
        to,
        f"{inviter} shared “{project_name}” with you",
        f"{inviter} added you to the Prompt Studio project “{project_name}”.\n\n"
        f"{joining}\n{link}\n\n"
        f"The invitation expires in {settings.INVITE_TOKEN_EXPIRE_DAYS} days.",
    )


async def send_welcome(to: str, name: str, temporary_password: str | None = None) -> None:
    credentials = (
        f"\n\nYour temporary password is: {temporary_password}\n"
        "You will be asked to change it the first time you sign in."
        if temporary_password
        else ""
    )
    await send_mail(
        to,
        "Welcome to Prompt Studio",
        f"Hi {name},\n\nYour Prompt Studio account is ready."
        f"{credentials}\n\n{settings.FRONTEND_BASE_URL}/login\n",
    )
