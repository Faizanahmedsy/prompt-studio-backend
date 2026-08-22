"""Shared time helpers.

Everything stored is UTC and timezone-aware. `now()` exists so no module
reaches for a naive `datetime.utcnow()`, which compares wrongly against the
`timestamptz` columns the schema uses.
"""

from datetime import UTC, datetime


def now() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)
