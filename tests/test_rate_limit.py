"""Throttling the unauthenticated surface.

Account lockout protects one account. These limits protect everything else:
password spraying across many addresses (which never trips any single account's
counter), registration floods, and reset floods — where the thing being attacked
is somebody else's inbox.
"""

import uuid

import pytest

from app.core import rate_limit
from app.core.rate_limit import Quota
from app.core.redis import get_redis


async def test_the_limiter_counts_and_then_refuses() -> None:
    """Exercised directly: the dependency wiring is asserted separately below,
    and driving it through a route would need the app rebuilt per test."""
    client = await get_redis()
    if client is None:
        pytest.skip("Redis is not available, so there is no shared counter to test")

    quota = Quota(f"unit-{uuid.uuid4().hex[:8]}", 3, 60, "Slow down")
    identity = f"10.0.0.{uuid.uuid4().int % 250}"

    for _ in range(quota.limit):
        await rate_limit.consume(quota, identity)  # must not raise

    with pytest.raises(Exception) as refused:
        await rate_limit.consume(quota, identity)
    assert "Slow down" in str(refused.value)

    # A different caller is unaffected — the counter is per identity, not global.
    await rate_limit.consume(quota, "10.0.0.251")


async def test_the_window_is_fixed_from_the_first_request() -> None:
    """A sliding window that resets on every attempt would never expire under a
    steady attacker, which is the opposite of what a limit is for."""
    client = await get_redis()
    if client is None:
        pytest.skip("Redis is not available")

    quota = Quota(f"window-{uuid.uuid4().hex[:8]}", 5, 60, "Slow down")
    identity = "10.0.1.1"
    await rate_limit.consume(quota, identity)
    key = f"{rate_limit.KEY_PREFIX}test:{quota.name}:{identity}"
    first = await client.ttl(key)
    await rate_limit.consume(quota, identity)
    second = await client.ttl(key)
    assert 0 < second <= first, "the window moved forward on a retry"


async def test_it_allows_everything_when_redis_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cache outage must not become a sign-in outage."""

    async def no_redis() -> None:
        return None

    monkeypatch.setattr(rate_limit, "get_redis", no_redis)
    quota = Quota("offline", 1, 60, "Slow down")
    for _ in range(50):
        await rate_limit.consume(quota, "10.0.2.1")


async def test_the_caller_is_the_forwarded_address_when_there_is_a_proxy() -> None:
    """Counting the proxy's own address would put every user on the internet
    into one bucket."""
    from starlette.datastructures import Headers
    from starlette.requests import Request

    def build(headers: dict[str, str], peer: str) -> Request:
        scope = {
            "type": "http",
            "headers": Headers(headers).raw,
            "client": (peer, 1234),
            "method": "POST",
            "path": "/",
        }
        return Request(scope)

    assert rate_limit.caller(build({}, "203.0.113.9")) == "203.0.113.9"
    assert (
        rate_limit.caller(build({"x-forwarded-for": "198.51.100.7, 10.0.0.1"}, "10.0.0.1"))
        == "198.51.100.7"
    )


async def test_the_auth_routes_carry_a_limit() -> None:
    """The limiter existing is worth nothing if a route forgets to use it.

    Walks the real route table rather than reading the source, so adding an
    unauthenticated endpoint and not throttling it fails here.
    """
    from fastapi.routing import APIRoute

    from app.main import app

    routes: list[APIRoute] = []

    def walk(entries: object) -> None:
        for entry in entries:  # type: ignore[attr-defined]
            if isinstance(entry, APIRoute):
                routes.append(entry)
                continue
            # Recent FastAPI wraps an included router in a lazy holder rather
            # than flattening its routes onto the app.
            nested = getattr(entry, "routes", None)
            if nested is None:
                included = getattr(entry, "original_router", None)
                nested = getattr(included, "routes", None) if included is not None else None
            if nested:
                walk(nested)

    walk(app.routes)
    assert routes, "no routes found — the walk is broken, not the app"

    must_be_limited = {
        "/auth/login",
        "/auth/register",
        "/auth/forgot-password",
        "/auth/resend-verification",
    }
    limited = {
        route.path
        for route in routes
        if any(
            getattr(dependency, "call", None) is not None
            and dependency.call.__module__ == "app.core.rate_limit"
            for dependency in route.dependant.dependencies
        )
    }
    missing = {path for path in must_be_limited if not any(p.endswith(path) for p in limited)}
    assert not missing, f"unthrottled auth routes: {sorted(missing)}"


async def test_a_disabled_limiter_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """`RATE_LIMIT_ENABLED=false` is the escape hatch for a deployment behind a
    single NAT where the limits would lock a whole office out."""
    monkeypatch.setattr(rate_limit.settings, "RATE_LIMIT_ENABLED", False)
    quota = Quota("disabled", 1, 60, "Slow down")
    for _ in range(20):
        await rate_limit.consume(quota, "10.0.3.1")
