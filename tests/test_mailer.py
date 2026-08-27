"""The Resend transport, and the guards around choosing a transport.

No database and no network: the point of these is that a mail failure is
contained and loudly logged, which is only observable by driving the transport
directly.
"""

import logging

import httpx
import pytest

from app.core import mailer
from app.core.config import Settings, settings


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or str(self._payload)

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Stands in for httpx.AsyncClient, recording the one request it is given."""

    calls: list[dict] = []
    response: _FakeResponse = _FakeResponse(200, {"id": "eml_123"})
    raises: Exception | None = None

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *_exc) -> None:
        return None

    async def post(self, url: str, **kwargs) -> _FakeResponse:
        type(self).calls.append({"url": url, **kwargs})
        if type(self).raises is not None:
            raise type(self).raises
        return type(self).response


@pytest.fixture
def resend(monkeypatch: pytest.MonkeyPatch):
    _FakeClient.calls = []
    _FakeClient.response = _FakeResponse(200, {"id": "eml_123"})
    _FakeClient.raises = None
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(settings, "EMAIL_TRANSPORT", "resend")
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test_key")
    monkeypatch.setattr(settings, "MAIL_FROM", "Prompt Studio <no-reply@example.test>")
    return _FakeClient


class TestResendTransport:
    async def test_posts_the_message_to_resend(self, resend) -> None:
        assert await mailer.send_mail("dev@example.test", "Subject", "Body") is True
        assert len(resend.calls) == 1
        call = resend.calls[0]
        assert call["url"] == "https://api.resend.com/emails"
        assert call["json"] == {
            "from": "Prompt Studio <no-reply@example.test>",
            "to": ["dev@example.test"],
            "subject": "Subject",
            "text": "Body",
        }

    async def test_sends_the_api_key_as_a_bearer_token(self, resend) -> None:
        await mailer.send_mail("dev@example.test", "s", "b")
        assert resend.calls[0]["headers"]["Authorization"] == "Bearer re_test_key"

    async def test_does_not_double_the_slash_on_a_trailing_base_url(
        self, resend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "RESEND_BASE_URL", "https://api.resend.com/")
        await mailer.send_mail("dev@example.test", "s", "b")
        assert resend.calls[0]["url"] == "https://api.resend.com/emails"

    async def test_a_rejection_is_reported_not_raised(
        self, resend, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The unverified-domain case, which is what a fresh Resend account does.
        resend.response = _FakeResponse(403, text="The example.test domain is not verified")
        with caplog.at_level(logging.WARNING, logger="app.mail"):
            assert await mailer.send_mail("dev@example.test", "s", "b") is False
        # The reason has to reach the log, or a one-DNS-record problem reads as "403".
        assert "not verified" in caplog.text

    async def test_a_network_failure_does_not_break_the_request(self, resend) -> None:
        resend.raises = httpx.ConnectError("no route to host")
        assert await mailer.send_mail("dev@example.test", "s", "b") is False

    async def test_a_timeout_does_not_break_the_request(self, resend) -> None:
        resend.raises = httpx.ReadTimeout("timed out")
        assert await mailer.send_mail("dev@example.test", "s", "b") is False


class TestTransportGuards:
    async def test_resend_without_a_key_refuses_rather_than_logging_the_token(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(settings, "EMAIL_TRANSPORT", "resend")
        monkeypatch.setattr(settings, "RESEND_API_KEY", "")
        with caplog.at_level(logging.ERROR, logger="app.mail"):
            assert await mailer.send_mail("dev@example.test", "s", "secret-token") is False
        # Falling back to console here would put live reset tokens in the log.
        assert "secret-token" not in caplog.text
        assert "NOT sent" in caplog.text

    async def test_smtp_without_a_host_still_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "EMAIL_TRANSPORT", "smtp")
        monkeypatch.setattr(settings, "SMTP_HOST", "")
        assert await mailer.send_mail("dev@example.test", "s", "b") is False

    async def test_console_still_logs_instead_of_sending(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(settings, "EMAIL_TRANSPORT", "console")
        with caplog.at_level(logging.INFO, logger="app.mail"):
            assert await mailer.send_mail("dev@example.test", "Subject", "Body") is True
        assert "[mail:console]" in caplog.text

    def test_an_unknown_transport_is_refused_at_startup(self) -> None:
        # Better a boot failure than a running service that silently sends nothing.
        with pytest.raises(ValueError, match="EMAIL_TRANSPORT"):
            Settings(EMAIL_TRANSPORT="mailgun")

    @pytest.mark.parametrize("transport", ["console", "smtp", "resend"])
    def test_the_documented_transports_are_accepted(self, transport: str) -> None:
        assert transport == Settings(EMAIL_TRANSPORT=transport).EMAIL_TRANSPORT


class TestTemplatesReachTheTransport:
    """Every template goes through send_mail, so Resend covers all of them."""

    async def test_password_reset_carries_a_working_link(self, resend, monkeypatch) -> None:
        monkeypatch.setattr(settings, "FRONTEND_BASE_URL", "https://studio.example.test")
        await mailer.send_password_reset("dev@example.test", "tok_abc")
        body = resend.calls[0]["json"]["text"]
        assert "https://studio.example.test/reset-password?token=tok_abc" in body

    async def test_email_verification_carries_a_working_link(self, resend, monkeypatch) -> None:
        monkeypatch.setattr(settings, "FRONTEND_BASE_URL", "https://studio.example.test")
        await mailer.send_email_verification("dev@example.test", "tok_xyz")
        body = resend.calls[0]["json"]["text"]
        assert "https://studio.example.test/verify-email?token=tok_xyz" in body

    async def test_an_invite_points_a_new_user_at_register(self, resend) -> None:
        await mailer.send_project_invite("dev@example.test", "Acme", "Sam", "tok", True)
        assert "/register?invite=tok" in resend.calls[0]["json"]["text"]

    async def test_an_invite_points_an_existing_user_at_login(self, resend) -> None:
        await mailer.send_project_invite("dev@example.test", "Acme", "Sam", "tok", False)
        assert "/login?invite=tok" in resend.calls[0]["json"]["text"]

    async def test_welcome_includes_a_temporary_password_when_one_was_issued(self, resend) -> None:
        await mailer.send_welcome("dev@example.test", "Sam", "temp-pw-1")
        assert "temp-pw-1" in resend.calls[0]["json"]["text"]

    async def test_welcome_omits_the_password_line_when_none_was_issued(self, resend) -> None:
        await mailer.send_welcome("dev@example.test", "Sam")
        assert "temporary password" not in resend.calls[0]["json"]["text"]
