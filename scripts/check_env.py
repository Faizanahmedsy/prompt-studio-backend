"""Fail fast, by name, when configuration is wrong.

Run before migrations in the container start-up chain. Without it a bad value
kills uvicorn at import with a bare pydantic traceback, which reaches the
browser three layers away as an unexplained CORS error.
"""

import sys
from pathlib import Path

# Running `python scripts/check_env.py` puts `scripts/` on the path, not the
# repo root, so `import app` fails — and this script is the FIRST thing the
# container runs, so that failure looked exactly like a bad configuration.
# Everything else (uvicorn, alembic, pytest) adds the root for its own reasons;
# this is the one entry point that has to do it itself.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    try:
        from app.core.config import Settings

        settings = Settings()
    except Exception as exc:  # noqa: BLE001 - the point is to report anything
        print(f"[check-env] configuration is invalid:\n{exc}", file=sys.stderr)
        return 1

    problems: list[str] = []
    # Not `is_production`. `staging` is an allowed environment and a real deploy
    # path, and it was getting no checks at all — so a staging box booted on the
    # committed development secret, and anyone with read access to the repo
    # could forge a superadmin token for it.
    if settings.ENVIRONMENT not in {"development", "test"}:
        if settings.SECRET_KEY.startswith("dev-only") or len(settings.SECRET_KEY) < 32:
            problems.append("SECRET_KEY must be a real 32+ character secret in production")
        if settings.APP_DEBUG:
            problems.append("APP_DEBUG must be false in production")
        if not settings.BACKEND_CORS_ORIGINS:
            problems.append("BACKEND_CORS_ORIGINS is empty — the frontend will be blocked")
        if settings.SUPERADMIN_PASSWORD in {"change-me", "superadmin@2026"}:
            problems.append("SUPERADMIN_PASSWORD is still the example value")
        if settings.EMAIL_TRANSPORT != "smtp" or not settings.SMTP_HOST:
            problems.append(
                "EMAIL_TRANSPORT must be smtp with a real SMTP_HOST outside development — "
                "otherwise invitations and password resets are delivered to nobody"
            )

    if problems:
        for problem in problems:
            print(f"[check-env] {problem}", file=sys.stderr)
        return 1

    print(
        f"[check-env] ok — {settings.ENVIRONMENT} / "
        f"db={settings.POSTGRES_DB}@{settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
