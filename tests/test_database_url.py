"""A connection string copied from a provider's dashboard must just work.

Every managed Postgres hands out a libpq-flavoured URL. Pasted in untouched it
does not fail loudly at startup — it fails at the first query, with
`TypeError: connect() got an unexpected keyword argument 'sslmode'`, which is a
miserable thing to debug at deploy time.
"""

import pytest

from app.core.config import Settings, _normalise_db_url

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


def test_database_description_reports_the_url_actually_in_use() -> None:
    """The startup line has to name the database the app connected to.

    It was built from the POSTGRES_* settings, which are only the fallback —
    with an override set, which is how every hosted deployment is configured,
    they hold their defaults. A production service on a managed database logged
    `prompt_studio@localhost:5442`, sending anyone reading it to look for a
    local Postgres that does not exist.
    """
    settings = Settings(
        DATABASE_URL_OVERRIDE=(
            "postgresql://someone:hunter2@ep-cool-name.us-east-2.aws.neon.tech/neondb"
            "?sslmode=require"
        )
    )
    assert settings.database_description == "neondb@ep-cool-name.us-east-2.aws.neon.tech"


def test_database_description_never_prints_the_password() -> None:
    # It goes to a log aggregator.
    settings = Settings(
        DATABASE_URL_OVERRIDE="postgresql://someone:hunter2@db.example.com:5432/app"
    )
    assert "hunter2" not in settings.database_description
    assert "someone" not in settings.database_description
    assert settings.database_description == "app@db.example.com:5432"


def test_database_description_falls_back_to_the_component_settings() -> None:
    # The override has to be cleared explicitly: the test environment sets one,
    # and it is meant to win over the component settings.
    settings = Settings(
        DATABASE_URL_OVERRIDE=None,
        POSTGRES_DB="prompt_studio",
        POSTGRES_HOST="localhost",
        POSTGRES_PORT=5442,
    )
    assert settings.database_description == "prompt_studio@localhost:5442"
