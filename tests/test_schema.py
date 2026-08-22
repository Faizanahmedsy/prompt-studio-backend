"""The migrations and the models must agree.

Autogenerate compares `Base.metadata` against a live database. If a model gains
a column and nobody writes the migration, this fails — which is the only cheap
way to catch it before a deploy runs `alembic upgrade head` and serves a schema
that does not match the code.
"""

import uuid

import asyncpg
import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.base_model import Base

ADMIN_URL = "postgresql://intelliwealth:root@127.0.0.1:5442/postgres"


@pytest.mark.asyncio(loop_scope="session")
async def test_migrations_produce_the_models_schema() -> None:
    scratch = f"prompt_studio_migrations_{uuid.uuid4().hex[:8]}"
    admin = await asyncpg.connect(ADMIN_URL)
    await admin.execute(f'CREATE DATABASE "{scratch}"')
    await admin.close()

    url = f"postgresql+asyncpg://intelliwealth:root@127.0.0.1:5442/{scratch}"
    engine = create_async_engine(url)
    try:
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", url)

        from alembic import command

        # Alembic's own runner is synchronous; run it against a sync connection
        # borrowed from the async engine rather than building a second engine
        # with a second driver.
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: _upgrade(config, sync_connection, command)
            )

        async with engine.connect() as connection:
            differences = await connection.run_sync(_diff)

        assert differences == [], (
            "the models and the migrations have drifted — run "
            "`uv run alembic revision --autogenerate -m '...'`:\n"
            + "\n".join(str(item) for item in differences)
        )
    finally:
        await engine.dispose()
        admin = await asyncpg.connect(ADMIN_URL)
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1", scratch
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{scratch}"')
        await admin.close()


def _upgrade(config: Config, connection: object, command: object) -> None:
    config.attributes["connection"] = connection
    ScriptDirectory.from_config(config)  # raises early if the tree is broken
    command.upgrade(config, "head")  # type: ignore[attr-defined]


def _diff(connection: object) -> list[object]:
    context = MigrationContext.configure(
        connection,  # type: ignore[arg-type]
        opts={"compare_type": True},
    )
    return list(compare_metadata(context, Base.metadata))


def test_every_model_is_registered() -> None:
    """A model nobody imported is a table Alembic will happily DROP."""
    from app import models

    registered = set(Base.metadata.tables)
    expected = {
        "users",
        "user_credentials",
        "sessions",
        "revoked_tokens",
        "projects",
        "project_members",
        "project_versions",
        "project_activity",
        "project_comments",
        "audit_logs",
    }
    assert expected <= registered, expected - registered
    assert models.Base is Base
