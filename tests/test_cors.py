"""Which origins the browser is told it may call this API from.

Vercel gives every preview deployment its own hostname, so no fixed list can
contain them. The regex exists for that — and the thing worth testing is that
it stays anchored to *this* project rather than to the platform, because
`https://.*\\.vercel\\.app` trusts every site anyone has ever put on Vercel.
"""

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app

PROJECT_REGEX = r"^https://prompt-studio-v2(-[a-z0-9-]+)?\.vercel\.app$"


def client_for(monkeypatch: pytest.MonkeyPatch, **env: str) -> TestClient:
    import app.core.config as config
    import app.main as main

    settings = Settings(**env)  # type: ignore[arg-type]
    monkeypatch.setattr(config, "settings", settings)
    monkeypatch.setattr(main, "settings", settings)
    return TestClient(create_app())


def allowed_origin(client: TestClient, origin: str) -> str | None:
    response = client.options(
        "/api/v1/auth/login",
        headers={"Origin": origin, "Access-Control-Request-Method": "POST"},
    )
    return response.headers.get("access-control-allow-origin")


def test_an_exact_origin_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    client = client_for(monkeypatch, BACKEND_CORS_ORIGINS="https://app.example.com")
    assert allowed_origin(client, "https://app.example.com") == "https://app.example.com"
    assert allowed_origin(client, "https://other.example.com") is None


def test_the_regex_admits_preview_deployments(monkeypatch: pytest.MonkeyPatch) -> None:
    client = client_for(monkeypatch, BACKEND_CORS_ORIGIN_REGEX=PROJECT_REGEX)
    for origin in (
        "https://prompt-studio-v2.vercel.app",
        "https://prompt-studio-v2-git-add-search.vercel.app",
    ):
        assert allowed_origin(client, origin) == origin


def test_the_regex_does_not_admit_the_whole_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The failure this guards against: anchoring on `.vercel.app` instead of on
    # the project, which trusts anybody's deployment.
    client = client_for(monkeypatch, BACKEND_CORS_ORIGIN_REGEX=PROJECT_REGEX)
    for origin in (
        "https://evil.vercel.app",
        "https://prompt-studio-v2.evil.com",
        "https://evil.com",
    ):
        assert allowed_origin(client, origin) is None, origin


def test_the_list_and_the_regex_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    client = client_for(
        monkeypatch,
        BACKEND_CORS_ORIGINS="http://localhost:3000",
        BACKEND_CORS_ORIGIN_REGEX=PROJECT_REGEX,
    )
    assert allowed_origin(client, "http://localhost:3000") == "http://localhost:3000"
    assert allowed_origin(client, "https://prompt-studio-v2.vercel.app") is not None


def test_a_wildcard_still_works_but_echoes_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `allow_credentials=True` forbids a literal `*` in the response, so
    # Starlette echoes the request's origin instead. The practical effect is
    # "every site may call this API" — which is why check_env warns about it.
    client = client_for(monkeypatch, BACKEND_CORS_ORIGINS="*")
    assert allowed_origin(client, "https://anything.example") == "https://anything.example"


def test_no_cors_configuration_means_no_cors_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = client_for(monkeypatch)
    assert allowed_origin(client, "https://app.example.com") is None
