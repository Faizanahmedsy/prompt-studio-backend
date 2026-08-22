"""Per-caller throttling for the endpoints an attacker hammers.

Account lockout (`auth.service._register_failure`) protects **one** account from
being guessed. It does nothing about the other three shapes of abuse the
unauthenticated surface has:

- spraying one common password across thousands of addresses, which never trips
  any single account's counter;
- registration floods, which cost a bcrypt hash each;
- password-reset floods, which send mail to a third party — someone else's
  inbox is the thing being attacked.

So this limits by **caller** rather than by account. Redis-backed, because the
counter has to be shared across workers or four processes each allow the full
quota. With Redis absent it degrades to allowing everything: the alternative is
refusing traffic because an optional dependency is missing, which turns a cache
outage into an outage.
"""

import logging
from dataclasses import dataclass
from typing import cast

from fastapi import Depends, Request, params

from app.core.config import settings
from app.core.exceptions import RateLimitError
from app.core.redis import get_redis

logger = logging.getLogger("app.ratelimit")

KEY_PREFIX = "prompt-studio:rate:"


@dataclass(frozen=True)
class Quota:
    """`limit` requests per `window` seconds, counted per caller."""

    name: str
    limit: int
    window: int
    message: str


def _quota(name: str, spec: str, message: str) -> Quota:
    """Parse a `"<requests>/<seconds>"` setting into a quota."""
    requests, _, window = spec.partition("/")
    return Quota(name, int(requests), int(window or 60), message)


# Sign-in is the loosest: a real person with a password manager retries a few
# times. Reset and registration are tight because each one has a side effect an
# attacker wants to amplify — an email to somebody else's inbox, a bcrypt hash.
SIGN_IN = _quota(
    "login",
    settings.RATE_LIMIT_SIGN_IN,
    "Too many sign-in attempts. Wait a few minutes and try again",
)
REGISTER = _quota(
    "register",
    settings.RATE_LIMIT_REGISTER,
    "Too many accounts created from here. Try again later",
)
RESET = _quota(
    "reset",
    settings.RATE_LIMIT_RESET,
    "Too many reset requests. Check your inbox, or try again later",
)


def caller(request: Request) -> str:
    """Who is being counted.

    The first hop in `X-Forwarded-For` when a proxy set one, otherwise the peer
    address. Behind a proxy the peer is the proxy, so counting it would put
    every user on the internet into one bucket — and with no proxy the header is
    attacker-controlled, which is why the deployment guide says to strip it at
    the edge.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def consume(quota: Quota, identity: str) -> None:
    """Count one request against a quota, raising 429 once it is spent."""
    if not settings.RATE_LIMIT_ENABLED:
        return
    client = await get_redis()
    if client is None:
        return  # no shared counter available — see the module docstring
    # Namespaced by environment so a test run against a shared Redis cannot
    # spend the quota of the dev server sitting next to it.
    key = f"{KEY_PREFIX}{settings.ENVIRONMENT}:{quota.name}:{identity}"
    try:
        used = int(await client.incr(key))
        if used == 1:
            # Only the first request in a window sets the expiry, so the window
            # is fixed from the first attempt rather than sliding forward with
            # every retry — otherwise a steady attacker resets it forever.
            await client.expire(key, quota.window)
    except Exception as exc:  # noqa: BLE001 - a broken limiter must not break sign-in
        logger.warning("Rate limiter unavailable (%s) — allowing the request", exc)
        return
    if used > quota.limit:
        raise RateLimitError(quota.message)


def limit(quota: Quota) -> params.Depends:
    """Dependency factory. Attach to a route with `dependencies=[limit(SIGN_IN)]`."""

    async def dependency(request: Request) -> None:
        await consume(quota, caller(request))

    # `Depends` is untyped in FastAPI's stubs, so the cast is what carries
    # the real return type to callers rather than leaking `Any` into every
    # router that uses this.
    return cast(params.Depends, Depends(dependency))
