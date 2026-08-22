import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app import models as _model_registry  # noqa: F401  (registers every table)
from app.core.base_model import Base
from app.core.config import settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The URL comes from Settings, never from alembic.ini — one source of truth, so
# a migration cannot be run against a different database than the app uses.
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

target_metadata = Base.metadata


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Without this, a column changing from String(80) to String(160) is not
        # detected and autogenerate produces an empty migration.
        compare_type=True,
        compare_server_default=True,
        render_as_batch=connection.dialect.name == "sqlite",
    )


def run_migrations_offline() -> None:
    context.configure(
        url=settings.SYNC_DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_run)
    await connectable.dispose()


# A caller may hand us an open connection through `config.attributes` — the
# standard Alembic pattern for driving migrations programmatically. The test
# suite uses it to upgrade a scratch database and then diff the result against
# the models; without it, `command.upgrade()` would reach `asyncio.run()` from
# inside an already-running loop and die.
_supplied_connection = config.attributes.get("connection")

if _supplied_connection is not None:
    _run(_supplied_connection)
elif context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
