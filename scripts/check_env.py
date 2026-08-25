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

    # Two lists, because they deserve different answers.
    #
    # `problems` are the ones where booting anyway is worse than not booting:
    # every one of them means the deployment is insecure, and it looks perfectly
    # healthy from outside while being trivially exploitable. Refusing to start
    # is the only thing that gets them fixed.
    #
    # `warnings` are the ones where something is degraded but nothing is unsafe.
    # A free-tier deployment with no SMTP is a real and reasonable thing to run;
    # failing the boot for it would mean the security checks below never get to
    # run at all, because nobody can deploy. Loud, and every restart.
    problems: list[str] = []
    warnings: list[str] = []

    # Not `is_production`. `staging` is an allowed environment and a real deploy
    # path, and it was getting no checks at all — so a staging box booted on the
    # committed development secret, and anyone with read access to the repo
    # could forge a superadmin token for it.
    if settings.ENVIRONMENT not in {"development", "test"}:
        if settings.SECRET_KEY.startswith("dev-only") or len(settings.SECRET_KEY) < 32:
            problems.append(
                "SECRET_KEY must be a real 32+ character secret in production. "
                "The default is committed to this repository, so anyone who can "
                "read it can forge a superadmin token. Generate one with "
                "`openssl rand -hex 32`."
            )
        if settings.APP_DEBUG:
            problems.append("APP_DEBUG must be false in production")
        if settings.SUPERADMIN_PASSWORD in {"change-me", "superadmin@2026"}:
            problems.append("SUPERADMIN_PASSWORD is still the example value")

        if not settings.BACKEND_CORS_ORIGINS and not settings.BACKEND_CORS_ORIGIN_REGEX:
            warnings.append(
                "BACKEND_CORS_ORIGINS is empty — every browser request from the "
                "frontend will be blocked, and it will look like a network error"
            )
        if "*" in settings.BACKEND_CORS_ORIGINS:
            warnings.append(
                'BACKEND_CORS_ORIGINS contains "*" — every website on the internet '
                "may call this API from a browser. Auth is a bearer token rather "
                "than a cookie, so this is not the session-riding hole it would "
                "otherwise be, but it still hands the unauthenticated endpoints "
                "(register, login, forgot-password) to anyone's page. Prefer "
                "listing the origins, or BACKEND_CORS_ORIGIN_REGEX for previews."
            )
        if settings.EMAIL_TRANSPORT != "smtp" or not settings.SMTP_HOST:
            warnings.append(
                "EMAIL_TRANSPORT is not smtp with a real SMTP_HOST — invitations "
                "and password resets are written to this log instead of being "
                "delivered. Read them here, or configure SMTP."
            )

    for warning in warnings:
        print(f"[check-env] WARNING: {warning}", file=sys.stderr)

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
