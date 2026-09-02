"""Test harness.

Everything runs against a **real Postgres database**, created and dropped per
session, rather than SQLite. The schema uses JSONB, partial-friendly indexes and
Postgres' own defaults; a SQLite suite would pass while the production database
did something else, which is the one thing a test must not do.
"""

import contextlib
import os
import socket
import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any

# Set BEFORE anything imports app.core.config, which builds its Settings
# singleton at import time. Environment beats the values in `.env`.
TEST_DB_NAME = f"prompt_studio_test_{uuid.uuid4().hex[:8]}"
_ADMIN_URL = "postgresql://intelliwealth:root@127.0.0.1:5442/postgres"
os.environ["DATABASE_URL_OVERRIDE"] = (
    f"postgresql+asyncpg://intelliwealth:root@127.0.0.1:5442/{TEST_DB_NAME}"
)
os.environ["ENVIRONMENT"] = "test"
os.environ["EMAIL_TRANSPORT"] = "console"
# Keep the lockout threshold low enough to exercise, high enough that ordinary
# wrong-password tests do not trip it by accident.
os.environ["MAX_FAILED_LOGINS"] = "5"
# The suite hashes a password on nearly every test; at the production cost of 12
# rounds that alone is minutes of wall clock.
os.environ["BCRYPT_ROUNDS"] = "4"
# Every test shares one caller identity (the ASGI transport has no peer
# address), so production quotas would throttle the suite itself rather than
# anything it is testing. `tests/test_rate_limit.py` sets its own tiny quotas
# and is the file that actually exercises the limiter.
os.environ["RATE_LIMIT_SIGN_IN"] = "100000/60"
os.environ["RATE_LIMIT_REGISTER"] = "100000/60"
os.environ["RATE_LIMIT_RESET"] = "100000/60"
os.environ["RATE_LIMIT_PUBLIC_READ"] = "100000/60"

import asyncpg  # noqa: E402
import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from app import models as _registry  # noqa: E402,F401  (registers every table)
from app.core.base_model import Base  # noqa: E402
from app.core.database import AsyncSessionLocal, engine  # noqa: E402
from app.core.security import create_verify_token  # noqa: E402
from app.main import app  # noqa: E402

API = "/api/v1"


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _database() -> AsyncGenerator[None]:
    """Create a throwaway database for the session and drop it afterwards."""
    connection = await asyncpg.connect(_ADMIN_URL)
    await connection.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
    await connection.close()

    async with engine.begin() as db_connection:
        await db_connection.run_sync(Base.metadata.create_all)

    yield

    await engine.dispose()
    connection = await asyncpg.connect(_ADMIN_URL)
    # Anything still holding a connection would block the DROP. Nothing should,
    # but a session leaked by a failing test would otherwise hang the whole run
    # instead of letting it report the failure.
    await connection.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
        TEST_DB_NAME,
    )
    await connection.execute(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}"')
    await connection.close()


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables() -> AsyncGenerator[None]:
    """Empty every table between tests.

    DELETE, not TRUNCATE. TRUNCATE takes an ACCESS EXCLUSIVE lock and forces
    the storage to be rewritten and fsynced, which measured at ~2.8s per test
    here — slower than every test in the file put together. These tables hold a
    handful of rows, and a plain DELETE in reverse dependency order is
    effectively instant.
    """
    yield
    async with engine.begin() as connection:
        from sqlalchemy import text

        # One statement per call: asyncpg prepares what it is given and
        # refuses a semicolon-joined batch.
        for table in reversed(Base.metadata.sorted_tables):
            await connection.execute(text(f'DELETE FROM "{table.name}"'))


@pytest_asyncio.fixture
async def client() -> AsyncGenerator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", timeout=30
    ) as http_client:
        yield http_client


# ── helpers ──────────────────────────────────────────────────────────────────


def unwrap(response: httpx.Response) -> Any:
    """The `data` half of the envelope, with a readable failure otherwise."""
    body = response.json()
    assert body["success"], f"{response.status_code}: {body.get('message')} {body.get('errors')}"
    return body["data"]


def message(response: httpx.Response) -> str:
    return str(response.json().get("message", ""))


class Actor:
    """A registered account plus the headers to act as it."""

    def __init__(self, email: str, password: str, payload: dict[str, Any]) -> None:
        self.email = email
        self.password = password
        self.access_token: str = payload["access_token"]
        self.refresh_token: str = payload["refresh_token"]
        self.user: dict[str, Any] = payload["user"]
        self.id: str = payload["user"]["id"]

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}


async def register(
    client: httpx.AsyncClient,
    email: str | None = None,
    password: str = "Password123",
    full_name: str = "Test Person",
    *,
    verify: bool = True,
    **extra: Any,
) -> Actor:
    """Register, and by default confirm the address.

    Confirmation is not decoration: sharing is by email, and an account that
    has not proved its address is no longer matched to an invitation — so a
    test actor that skips it is a test actor with no access to anything it was
    invited to. `verify=False` is for the tests that are about that rule.
    """
    address = email or f"user.{uuid.uuid4().hex[:10]}@example.com"
    response = await client.post(
        f"{API}/auth/register",
        json={"email": address, "password": password, "full_name": full_name, **extra},
    )
    assert response.status_code == 201, response.text
    actor = Actor(address, password, unwrap(response))
    if verify and actor.user.get("email_verified_at") is None:
        confirmed = await client.post(
            f"{API}/auth/verify-email",
            # The stored address, not the one typed: registration normalises it,
            # and a token carrying the raw spelling confirms nothing.
            json={"token": create_verify_token(actor.id, actor.user["email"])},
        )
        assert confirmed.status_code == 200, confirmed.text
    return actor


async def sign_in(client: httpx.AsyncClient, email: str, password: str) -> Actor:
    response = await client.post(f"{API}/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return Actor(email, password, unwrap(response))


async def make_superadmin(email: str) -> None:
    """Promote an existing account, the way the seed does."""
    from sqlalchemy import select

    from app.core.constants import GlobalRole
    from app.modules.users.models import User

    async with AsyncSessionLocal() as db:
        user = (await db.execute(select(User).where(User.email == email))).scalar_one()
        user.role = GlobalRole.SUPERADMIN
        user.is_superuser = True
        await db.commit()


def sample_doc(screens: int = 1, modules: int = 0) -> dict[str, Any]:
    """A document shaped like the editor's own ProjectDoc, at a useful size."""
    return {
        "name": "Sample",
        "target": "claude-code",
        "views": [{"id": "v1", "key": "admin", "name": "Admin", "note": ""}],
        "screens": [
            {
                "id": f"s{index}",
                "key": f"screen_{index}",
                "title": f"Screen {index}",
                "surface": "web",
                "template": "table",
                "layout": "table-advanced",
                "views": [],
                "x": index * 240,
                "y": 0,
            }
            for index in range(screens)
        ],
        "modules": [
            {
                "id": f"m{index}",
                "screenId": "s0",
                "key": f"module_{index}",
                "name": f"Module {index}",
                "kind": "panel",
                "order": index,
            }
            for index in range(modules)
        ],
        "edges": [],
        "moduleEdges": [],
        "sections": [],
    }


# ── live server, for the websocket tests ─────────────────────────────────────


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _start_server(workers: int = 1) -> Generator[str]:
    """A real uvicorn, in its own **process**.

    Two reasons it cannot run in this one. The collaboration socket only means
    anything with two independent clients seeing each other, which needs a
    server actually holding both connections. And an in-thread server would
    share this process's SQLAlchemy engine while running its own event loop —
    asyncpg connections are bound to the loop that opened them, so the pool
    would hand the server a connection from the test loop and fail. A subprocess
    builds its own engine against the same test database and sidesteps both.
    """
    import subprocess
    import time
    import urllib.error
    import urllib.request

    port = _free_port()
    environment = {**os.environ, "LOG_LEVEL": "WARNING"}
    command = [
        "uv",
        "run",
        "--no-sync",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--log-level",
        "warning",
    ]
    if workers > 1:
        command += ["--workers", str(workers)]
    process = subprocess.Popen(
        command,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    for _ in range(300):
        if process.poll() is not None:
            output = process.stdout.read().decode() if process.stdout else ""
            raise RuntimeError(f"the test server died on start-up:\n{output}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as reply:
                if reply.status == 200:
                    break
        except (urllib.error.URLError, OSError, TimeoutError):
            time.sleep(0.1)
    else:  # pragma: no cover - only on a broken environment
        process.kill()
        raise RuntimeError("the test server never came up")

    yield f"127.0.0.1:{port}"

    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:  # pragma: no cover
        process.kill()


@pytest.fixture(scope="session")
def live_server() -> Generator[str]:
    """One worker. Enough for everything except the cross-worker tests."""
    yield from _start_server(1)


@pytest.fixture(scope="session")
def live_cluster() -> Generator[str]:
    """Four workers behind one port.

    The only way to test what Redis is actually for: with a single process the
    in-memory hub is already correct, so a bug in the pub/sub fan-out or in the
    shared presence hash is completely invisible.
    """
    yield from _start_server(4)


@contextlib.asynccontextmanager
async def open_socket(live: str, project_id: str, token: str) -> AsyncGenerator[Any]:
    import websockets

    url = f"ws://{live}{API}/ws/projects/{project_id}?token={token}"
    async with websockets.connect(url) as socket_connection:
        yield socket_connection
