"""A connection string copied from a provider's dashboard must just work.

Every managed Postgres hands out a libpq-flavoured URL. Pasted in untouched it
does not fail loudly at startup — it fails at the first query, with
`TypeError: connect() got an unexpected keyword argument 'sslmode'`, which is a
miserable thing to debug at deploy time.
"""

import pytest

from app.core.config import _normalise_db_url

NEON = (
    "postgresql://alice:pw@ep-cool-sun-123.eu-central-1.aws.neon.tech"
    "/neondb?sslmode=require&channel_binding=require"
)


def test_neon_url_becomes_an_asyncpg_url() -> None:
    url, connect_args = _normalise_db_url(NEON)
    assert url.startswith("postgresql+asyncpg://")
    assert "sslmode" not in url
    assert "channel_binding" not in url
    assert connect_args == {"ssl": "require"}


def test_the_host_and_credentials_survive() -> None:
    url, _ = _normalise_db_url(NEON)
    assert "alice:pw@ep-cool-sun-123.eu-central-1.aws.neon.tech" in url
    assert url.endswith("/neondb")


@pytest.mark.parametrize("scheme", ["postgres", "postgresql"])
def test_both_spellings_of_the_scheme_are_accepted(scheme: str) -> None:
    url, _ = _normalise_db_url(f"{scheme}://u:p@host/db")
    assert url.startswith("postgresql+asyncpg://")


def test_a_url_that_already_names_the_driver_is_left_alone() -> None:
    original = "postgresql+asyncpg://u:p@localhost:5442/prompt_studio"
    url, connect_args = _normalise_db_url(original)
    assert url == original
    assert connect_args == {}


def test_sqlite_passes_straight_through() -> None:
    # The test suite sets DATABASE_URL_OVERRIDE too.
    original = "sqlite+aiosqlite:///:memory:"
    assert _normalise_db_url(original) == (original, {})


def test_prefer_is_dropped_rather_than_passed_on() -> None:
    # asyncpg has no equivalent, and it is the default behaviour anyway.
    _, connect_args = _normalise_db_url("postgresql://u:p@host/db?sslmode=prefer")
    assert connect_args == {}


def test_parameters_asyncpg_does_understand_are_kept() -> None:
    url, _ = _normalise_db_url("postgresql://u:p@host/db?application_name=promptstudio")
    assert "application_name=promptstudio" in url
