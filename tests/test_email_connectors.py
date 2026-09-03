from __future__ import annotations

import base64
import hashlib
import json
import urllib.parse

import pytest

import context_gate.email_connectors as connectors
from context_gate.email_connectors import EmailConnectorError, EmailOAuthManager

GOOGLE_CLIENT_ID = "fictional-google-client.apps.exampleusercontent.com"
GOOGLE_CLIENT_SECRET = "fictional-google-client-secret"
MICROSOFT_CLIENT_ID = "12345678-1234-5678-9234-567812345678"


@pytest.fixture(autouse=True)
def no_real_connector_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "CONTEXTGATE_GOOGLE_CLIENT_ID",
        "CONTEXTGATE_GOOGLE_CLIENT_SECRET",
        "CONTEXTGATE_MICROSOFT_CLIENT_ID",
    ):
        monkeypatch.delenv(name, raising=False)


def _google_configuration() -> bytes:
    return json.dumps(
        {
            "installed": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": connectors.GOOGLE_AUTHORIZE,
                "token_uri": connectors.GOOGLE_TOKEN,
                "redirect_uris": ["http://localhost"],
            }
        }
    ).encode("utf-8")


def _parameters(url: str) -> dict[str, str]:
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query, strict_parsing=True)
    return {key: values[0] for key, values in query.items()}


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _configured_manager() -> EmailOAuthManager:
    manager = EmailOAuthManager()
    manager.configure_google_json(_google_configuration())
    manager.configure_microsoft(MICROSOFT_CLIENT_ID)
    return manager


def test_provider_urls_use_local_callbacks_read_only_scopes_state_and_pkce() -> None:
    manager = _configured_manager()

    google_url = manager.start_authorization("google", "http://127.0.0.1:8501")
    microsoft_url = manager.start_authorization("microsoft", "http://localhost:8501")
    google = _parameters(google_url)
    microsoft = _parameters(microsoft_url)

    assert google_url.startswith(f"{connectors.GOOGLE_AUTHORIZE}?")
    assert google["client_id"] == GOOGLE_CLIENT_ID
    assert google["redirect_uri"] == "http://localhost:8501"
    assert google["response_type"] == "code"
    assert set(google["scope"].split()) == set(connectors.GOOGLE_SCOPES)
    assert google["access_type"] == "offline"
    assert google["code_challenge_method"] == "S256"
    assert google["state"]
    assert google["code_challenge"]
    assert GOOGLE_CLIENT_SECRET not in google_url

    assert microsoft_url.startswith(f"{connectors.MICROSOFT_AUTHORIZE}?")
    assert microsoft["client_id"] == MICROSOFT_CLIENT_ID
    assert microsoft["redirect_uri"] == (
        "http://localhost:8501/oauth/microsoft/callback"
    )
    assert microsoft["response_type"] == "code"
    assert microsoft["response_mode"] == "query"
    assert set(microsoft["scope"].split()) == set(connectors.MICROSOFT_SCOPES)
    assert microsoft["code_challenge_method"] == "S256"
    assert microsoft["state"]
    assert microsoft["state"] != google["state"]


def test_google_callback_verifies_pkce_and_consumes_state_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = EmailOAuthManager()
    manager.configure_google_json(_google_configuration())
    authorization_url = manager.start_authorization("google", "http://127.0.0.1:8501")
    authorization = _parameters(authorization_url)
    calls: list[dict[str, object]] = []

    def fake_request(
        url: str,
        *,
        method: str = "GET",
        form: dict[str, str] | None = None,
        token: str | None = None,
    ) -> dict[str, object]:
        calls.append({"url": url, "method": method, "form": form, "token": token})
        if url == connectors.GOOGLE_TOKEN:
            return {
                "access_token": "fictional-access-token",
                "refresh_token": "fictional-refresh-token",
                "expires_in": 3600,
            }
        if url == f"{connectors.GOOGLE_GMAIL_API}/profile":
            return {"emailAddress": "person-one@example.test"}
        raise AssertionError(f"Unexpected mocked request: {url}")

    monkeypatch.setattr(connectors, "_request_json", fake_request)

    account = manager.finish_authorization(
        "google",
        state=authorization["state"],
        code="fictional-authorization-code",
    )

    assert account == "person-one@example.test"
    token_form = calls[0]["form"]
    assert isinstance(token_form, dict)
    assert calls[0]["url"] == connectors.GOOGLE_TOKEN
    assert calls[0]["method"] == "POST"
    assert token_form["redirect_uri"] == authorization["redirect_uri"]
    assert token_form["grant_type"] == "authorization_code"
    assert token_form["client_secret"] == GOOGLE_CLIENT_SECRET
    assert (
        _pkce_challenge(token_form["code_verifier"]) == authorization["code_challenge"]
    )
    assert calls[1]["token"] == "fictional-access-token"

    with pytest.raises(EmailConnectorError, match="missing or expired"):
        manager.finish_authorization(
            "google",
            state=authorization["state"],
            code="must-not-be-replayed",
        )
    assert len(calls) == 2


def test_multiple_accounts_disconnect_and_status_never_serialize_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = EmailOAuthManager()
    manager.configure_google_json(_google_configuration())
    revocations: list[str] = []

    def fake_request(
        url: str,
        *,
        method: str = "GET",
        form: dict[str, str] | None = None,
        token: str | None = None,
    ) -> dict[str, object]:
        if url == connectors.GOOGLE_TOKEN:
            assert form is not None
            code = form["code"]
            return {
                "access_token": f"access-{code}",
                "refresh_token": f"refresh-{code}",
                "expires_in": 3600,
            }
        if url == f"{connectors.GOOGLE_GMAIL_API}/profile":
            assert token is not None
            code = token.removeprefix("access-")
            return {"emailAddress": f"{code}@example.test"}
        if url == connectors.GOOGLE_REVOKE:
            assert method == "POST"
            assert form is not None
            revocations.append(form["token"])
            return {}
        raise AssertionError(f"Unexpected mocked request: {url}")

    monkeypatch.setattr(connectors, "_request_json", fake_request)

    for code in ("person-one", "person-two"):
        authorization = _parameters(
            manager.start_authorization("google", "http://127.0.0.1:8501")
        )
        connected = manager.finish_authorization(
            "google", state=authorization["state"], code=code
        )
        assert connected == f"{code}@example.test"

    status = manager.status()
    serialized = json.dumps(status, sort_keys=True)

    assert status["google"]["connected"] is True
    assert status["google"]["accounts"] == [
        "person-one@example.test",
        "person-two@example.test",
    ]
    for forbidden in (
        GOOGLE_CLIENT_SECRET,
        "access-person-one",
        "access-person-two",
        "refresh-person-one",
        "refresh-person-two",
    ):
        assert forbidden not in serialized

    assert manager.disconnect("google", account="PERSON-ONE@EXAMPLE.TEST") == 1
    assert manager.status()["google"]["accounts"] == ["person-two@example.test"]
    assert manager.disconnect("google") == 1
    assert manager.status()["google"]["connected"] is False
    assert manager.disconnect("google") == 0
    assert revocations == ["refresh-person-one", "refresh-person-two"]


def test_connector_configuration_and_callback_errors_are_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_marker = "fictional-secret-must-not-leak"
    manager = EmailOAuthManager()
    unexpected = json.dumps(
        {
            "installed": {
                "client_id": "client-id",
                "client_secret": secret_marker,
                "auth_uri": "https://attacker.invalid/authorize",
                "token_uri": connectors.GOOGLE_TOKEN,
            }
        }
    ).encode("utf-8")

    with pytest.raises(EmailConnectorError) as raised:
        manager.configure_google_json(unexpected)
    assert "unexpected authorization endpoints" in str(raised.value)
    assert secret_marker not in str(raised.value)

    manager.configure_google_json(_google_configuration())
    authorization = _parameters(
        manager.start_authorization("google", "http://127.0.0.1:8501")
    )

    def rejected_request(
        url: str,
        *,
        method: str = "GET",
        form: dict[str, str] | None = None,
        token: str | None = None,
    ) -> dict[str, object]:
        del url, method, form, token
        raise EmailConnectorError("The provider rejected the fictional request.")

    monkeypatch.setattr(connectors, "_request_json", rejected_request)
    with pytest.raises(EmailConnectorError) as callback_error:
        manager.finish_authorization(
            "google",
            state=authorization["state"],
            code=secret_marker,
        )
    assert secret_marker not in str(callback_error.value)


def test_local_callback_is_required_before_any_authorization() -> None:
    manager = EmailOAuthManager()
    manager.configure_microsoft(MICROSOFT_CLIENT_ID)

    with pytest.raises(EmailConnectorError, match="restricted to the local"):
        manager.start_authorization("microsoft", "https://example.test")


def test_google_desktop_configuration_requires_a_loopback_redirect() -> None:
    manager = EmailOAuthManager()
    payload = json.loads(_google_configuration())
    payload["installed"]["redirect_uris"] = ["https://example.test/callback"]

    with pytest.raises(EmailConnectorError, match="localhost loopback"):
        manager.configure_google_json(json.dumps(payload).encode("utf-8"))


def test_loopback_authorization_accepts_the_active_local_server_port() -> None:
    manager = _configured_manager()

    google_url = manager.start_authorization("google", "http://127.0.0.1:49152")
    microsoft_url = manager.start_authorization("microsoft", "http://localhost:49152")

    assert _parameters(google_url)["redirect_uri"] == "http://localhost:49152"
    assert _parameters(microsoft_url)["redirect_uri"] == (
        "http://localhost:49152/oauth/microsoft/callback"
    )
