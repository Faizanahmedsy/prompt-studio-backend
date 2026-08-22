"""Lazy Redis accessor.

Redis is **optional**. It exists so collaboration messages reach browsers
connected to a *different* API worker; with one worker, in-process fan-out is
already correct. So every helper here degrades to `None` rather than raising,
and the collaboration hub falls back to local-only broadcast.
"""

import logging

from redis.asyncio import Redis

from app.core.config import settings

logger = logging.getLogger("app.redis")

_client: Redis | None = None
_unavailable = False


async def get_redis() -> Redis | None:
    """Connected client, or None if Redis is not reachable.

    The failure is latched: once a connection attempt fails, later calls return
    None immediately instead of paying the connect timeout on every websocket
    message. A restart of the API picks Redis back up.
    """
    global _client, _unavailable
    if _unavailable:
        return None
    if _client is not None:
        return _client
    try:
        client: Redis = Redis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=5,
            health_check_interval=30,
        )
        await client.ping()
    except Exception as exc:  # noqa: BLE001 - any connection failure means "no redis"
        logger.warning("Redis unavailable (%s) — collaboration stays single-worker", exc)
        _unavailable = True
        return None
    _client = client
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
