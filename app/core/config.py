import json
from functools import lru_cache
from typing import Annotated, Any

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
            return self.DATABASE_URL_OVERRIDE
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

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


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
