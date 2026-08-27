import json
from functools import lru_cache
from typing import Annotated, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import computed_field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ── App ──────────────────────────────────────────────────────────────────
    PROJECT_NAME: str = "Prompt Studio API"
    ENVIRONMENT: str = "development"
    # Deliberately not `DEBUG`: that name is claimed by half the ecosystem (the
    # `debug` npm package, cargo profiles, VS Code's extension host all export
    # it), and pydantic reads the real environment BEFORE `.env` — so a stray
    # `DEBUG=release` in the shell kills the app at import with a bool_parsing
    # error no matter what `.env` says. `APP_DEBUG` is ours alone.
    APP_DEBUG: bool = False
    LOG_LEVEL: str = "INFO"
    API_V1_PREFIX: str = "/api/v1"
    PUBLIC_API_BASE_URL: str = "http://localhost:8010"
    FRONTEND_BASE_URL: str = "http://localhost:3000"

    # ── Database ─────────────────────────────────────────────────────────────
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5442
    POSTGRES_USER: str = "intelliwealth"
    POSTGRES_PASSWORD: str = "root"
    POSTGRES_DB: str = "prompt_studio"
    DB_ECHO: bool = False
    DB_HOST_PORT: int = 5442
    # Set by the test suite to point the whole app at an in-memory SQLite DB.
    DATABASE_URL_OVERRIDE: str | None = None

    # ── Redis ────────────────────────────────────────────────────────────────
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6382
    REDIS_PASSWORD: str = ""
    REDIS_DB: int = 0
    REDIS_HOST_PORT: int = 6382
    REDIS_URL: str | None = None

    # ── Auth ─────────────────────────────────────────────────────────────────
    SECRET_KEY: str = "dev-only-insecure-key-change-me"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    RESET_TOKEN_EXPIRE_MINUTES: int = 30
    INVITE_TOKEN_EXPIRE_DAYS: int = 14
    MAX_FAILED_LOGINS: int = 8
    LOCKOUT_MINUTES: int = 15
    # bcrypt work factor. 12 is the right number for a real deployment; the
    # test suite drops it to 4, because at 12 a hash costs ~250ms and a suite
    # that signs in a few hundred times spends minutes doing nothing else.
    BCRYPT_ROUNDS: int = 12

    # ── Rate limits ──────────────────────────────────────────────────────────
    # "<requests>/<seconds>", counted per caller IP. Sized to be invisible to a
    # person and painful to a script. Configurable because the right number
    # depends on whether the deployment sits behind a shared NAT — an office of
    # forty people is one IP, and a limit tuned for a home connection locks all
    # of them out at 09:00.
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_SIGN_IN: str = "20/300"
    RATE_LIMIT_REGISTER: str = "10/3600"
    RATE_LIMIT_RESET: str = "5/3600"
    # The one anonymous route that reads data. Generous — a public diagram is
    # meant to be opened by a lot of people — but not unbounded, so a token
    # cannot be used to hammer the database for free.
    RATE_LIMIT_PUBLIC_READ: str = "120/60"

    # ── Seeded accounts ──────────────────────────────────────────────────────
    # A real TLD, deliberately. `.local` is reserved for mDNS and the email
    # validator behind `EmailStr` rejects it — a seeded superadmin on a `.local`
    # address is created successfully and then cannot sign in, because the login
    # body fails validation before it ever reaches the password check.
    SUPERADMIN_EMAIL: str = "faizan@promptstudio.app"
    SUPERADMIN_PASSWORD: str = "superadmin@2026"
    SUPERADMIN_NAME: str = "Faizan Saiyed"

    # ── CORS ─────────────────────────────────────────────────────────────────
    # `NoDecode` is load-bearing. pydantic-settings JSON-decodes a complex field
    # inside the settings source, *before* any field validator runs — so the
    # comma-separated form the README documents, and an empty value, both died
    # with a pydantic-internals error at import and restart-looped the
    # container. With NoDecode the raw string reaches the validator below, which
    # does the parsing itself.
    BACKEND_CORS_ORIGINS: Annotated[list[str], NoDecode] = []

    # A regex matched against the Origin header, for origins whose exact value
    # is not known in advance. Vercel is the reason this exists: every preview
    # deployment gets its own hostname, so no fixed list can ever contain them.
    #
    # Anchor it to YOUR project, not to the platform. `https://.*\.vercel\.app`
    # trusts every site anybody has ever deployed to Vercel, which is not a
    # smaller set than "*" in any way that matters.
    #
    #   good: ^https://prompt-studio-v2(-[a-z0-9-]+)?\.vercel\.app$
    #   bad:  ^https://.*\.vercel\.app$
    BACKEND_CORS_ORIGIN_REGEX: str = ""

    # ── Mail ─────────────────────────────────────────────────────────────────
    # "console" logs the message instead of sending it, so invites and resets
    # are testable on a laptop with no SMTP account.
    EMAIL_TRANSPORT: str = "console"
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_STARTTLS: bool = True
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    MAIL_FROM: str = "Prompt Studio <no-reply@promptstudio.app>"
    SMTP_TIMEOUT_SECONDS: int = 10

    # Resend's HTTP API. Preferred over SMTP on Render, where outbound port 587
    # is not reliably open and a blocked connection looks exactly like a
    # silently undelivered password reset.
    RESEND_API_KEY: str = ""
    RESEND_BASE_URL: str = "https://api.resend.com"
    RESEND_TIMEOUT_SECONDS: int = 10

    # ── Collaboration ────────────────────────────────────────────────────────
    WS_HEARTBEAT_SECONDS: int = 25
    PROJECT_VERSION_LIMIT: int = 50

    @field_validator("BACKEND_CORS_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, value: Any) -> Any:
        """Accept a JSON array or a plain comma-separated list."""
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return []
            if raw.startswith("["):
                return json.loads(raw)
            return [item.strip() for item in raw.split(",") if item.strip()]
        return value

    @field_validator("RESEND_API_KEY", "SMTP_PASSWORD", "SMTP_USER", mode="before")
    @classmethod
    def _strip_secret(cls, value: Any) -> Any:
        """Trim whitespace off credentials pasted into a dashboard.

        A trailing newline on an API key is invisible in the UI and turns into
        an invalid HTTP header, which raises with the header *value* in the
        exception message — i.e. the key itself, into the log.
        """
        return value.strip() if isinstance(value, str) else value

    @field_validator("EMAIL_TRANSPORT")
    @classmethod
    def _known_transport(cls, value: str) -> str:
        allowed = {"console", "smtp", "resend"}
        if value not in allowed:
            raise ValueError(f"EMAIL_TRANSPORT must be one of {sorted(allowed)}, got {value!r}")
        return value

    @field_validator("ENVIRONMENT")
    @classmethod
    def _known_environment(cls, value: str) -> str:
        allowed = {"development", "staging", "production", "test"}
        if value not in allowed:
            raise ValueError(f"ENVIRONMENT must be one of {sorted(allowed)}, got {value!r}")
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def DATABASE_URL(self) -> str:
        if self.DATABASE_URL_OVERRIDE:
            return _normalise_db_url(self.DATABASE_URL_OVERRIDE)[0]
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def database_description(self) -> str:
        """`database@host:port`, for the line an operator reads at boot.

        Derived from the URL actually in use rather than from the component
        settings. Those are only the *fallback* — when `DATABASE_URL_OVERRIDE`
        is set, which is how every hosted deployment is configured, they hold
        their defaults and the startup line claimed `prompt_studio@localhost`
        on a service talking to a managed database three regions away. That is
        the one line somebody reads to answer "which database is this?", and it
        was wrong in exactly the case where the question gets asked.

        Never includes the password: this goes to a log aggregator.
        """
        parsed = urlsplit(self.DATABASE_URL)
        host = parsed.hostname or "?"
        port = f":{parsed.port}" if parsed.port else ""
        name = parsed.path.lstrip("/") or "?"
        return f"{name}@{host}{port}"

    @property
    def db_connect_args(self) -> dict[str, object]:
        """Driver arguments the URL asked for but asyncpg cannot read itself."""
        if not self.DATABASE_URL_OVERRIDE:
            return {}
        return _normalise_db_url(self.DATABASE_URL_OVERRIDE)[1]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def SYNC_DATABASE_URL(self) -> str:
        """psycopg-flavoured URL — Alembic's offline mode and psql-style tools."""
        return self.DATABASE_URL.replace("+asyncpg", "")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def redis_url(self) -> str:
        if self.REDIS_URL:
            return self.REDIS_URL
        auth = f":{self.REDIS_PASSWORD}@" if self.REDIS_PASSWORD else ""
        return f"redis://{auth}{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"


# ── managed Postgres URLs ────────────────────────────────────────────────────

# Query parameters that belong to libpq and mean nothing to asyncpg. Passed
# through untouched they do not get ignored — asyncpg raises
# `TypeError: connect() got an unexpected keyword argument 'sslmode'` and the
# app dies at the first query, which is a miserable way to discover it.
_LIBPQ_ONLY = {"sslmode", "channel_binding", "options", "target_session_attrs"}


def _normalise_db_url(raw: str) -> tuple[str, dict[str, object]]:
    """Turn a connection string from a managed provider into one asyncpg accepts.

    Neon, Supabase, Render and the rest all hand out a libpq-flavoured URL —
    `postgresql://…?sslmode=require&channel_binding=require`. Three things are
    wrong with it here: the scheme names no driver, and the two parameters are
    libpq's rather than asyncpg's.

    So the scheme is pinned to asyncpg, the libpq-only parameters are lifted
    out, and `sslmode` is returned as a driver argument instead — asyncpg takes
    the same words, just by another route. The point is that the string copied
    from a provider's dashboard can be pasted in as-is and work.

    SQLite is passed straight through: the test suite sets this variable too.
    """
    if raw.startswith("sqlite"):
        return raw, {}

    parsed = urlsplit(raw)
    scheme = parsed.scheme
    if scheme in {"postgres", "postgresql"}:
        scheme = "postgresql+asyncpg"

    kept: list[tuple[str, str]] = []
    connect_args: dict[str, object] = {}
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() == "sslmode":
            # `prefer` has no asyncpg equivalent and is the default anyway.
            if value and value != "prefer":
                connect_args["ssl"] = value
        elif key.lower() in _LIBPQ_ONLY:
            continue
        else:
            kept.append((key, value))

    url = urlunsplit((scheme, parsed.netloc, parsed.path, urlencode(kept), parsed.fragment))
    return url, connect_args


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
