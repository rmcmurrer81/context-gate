"""Read-only Gmail and Microsoft mailbox OAuth connectors.

The connectors deliberately use authorization-code + PKCE in the user's system
browser and request read-only scopes. Access and refresh tokens are held in
memory only and disappear when the local server stops. Client registration is
installation-specific and can be provided through environment variables or a
Git-ignored local administrator setup; no shared credential is embedded here.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Provider = Literal["google", "microsoft"]

GOOGLE_AUTHORIZE: Final = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN: Final = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE: Final = "https://oauth2.googleapis.com/revoke"
GOOGLE_GMAIL_API: Final = "https://gmail.googleapis.com/gmail/v1/users/me"
GOOGLE_SCOPES: Final = (
    "openid",
    "email",
    "https://www.googleapis.com/auth/gmail.readonly",
)

MICROSOFT_AUTHORIZE: Final = (
    "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
)
MICROSOFT_TOKEN: Final = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
MICROSOFT_GRAPH: Final = "https://graph.microsoft.com/v1.0"
MICROSOFT_SCOPES: Final = (
    "openid",
    "profile",
    "email",
    "offline_access",
    "User.Read",
    "Mail.Read",
)

MAX_HTTP_BYTES = 2 * 1024 * 1024
MAX_CLIENT_CONFIG_BYTES = 32 * 1024
MAX_MESSAGES = 25
PENDING_SECONDS = 10 * 60


class EmailConnectorError(RuntimeError):
    """Safe, user-facing connector failure without tokens or response bodies."""


class MailSummary(BaseModel):
    """A bounded, non-HTML mailbox preview row."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    provider: Provider
    message_id: str = Field(min_length=1, max_length=512)
    subject: str = Field(default="(no subject)", max_length=500)
    sender: str = Field(default="Unknown sender", max_length=500)
    received_at: str = Field(default="Unknown time", max_length=128)
    preview: str = Field(default="", max_length=1000)

    @field_validator("message_id", "subject", "sender", "received_at", "preview")
    @classmethod
    def text_has_no_controls(cls, value: str) -> str:
        without_controls = "".join(
            character if ord(character) >= 32 else " " for character in value
        )
        return " ".join(without_controls.split())


@dataclass(slots=True)
class _PendingAuthorization:
    provider: Provider
    state: str
    verifier: str
    redirect_uri: str
    created_at: float


@dataclass(slots=True)
class _MailboxSession:
    provider: Provider
    account_email: str
    access_token: str
    refresh_token: str | None
    expires_at: float


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(72)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _clean_identifier(value: object, *, label: str, limit: int = 512) -> str:
    if not isinstance(value, str):
        raise EmailConnectorError(f"{label} is missing or invalid.")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > limit or any(ord(item) < 32 for item in cleaned):
        raise EmailConnectorError(f"{label} is missing or invalid.")
    return cleaned


def _read_json_response(response: Any) -> dict[str, Any]:
    length_header = response.headers.get("Content-Length")
    if length_header:
        try:
            if int(length_header) > MAX_HTTP_BYTES:
                raise EmailConnectorError(
                    "The provider response exceeded the safe limit."
                )
        except ValueError:
            pass
    raw = response.read(MAX_HTTP_BYTES + 1)
    if len(raw) > MAX_HTTP_BYTES:
        raise EmailConnectorError("The provider response exceeded the safe limit.")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise EmailConnectorError(
            "The provider returned an invalid response."
        ) from None
    if not isinstance(payload, dict):
        raise EmailConnectorError("The provider returned an invalid response.")
    return payload


def _request_json(
    url: str,
    *,
    method: str = "GET",
    form: dict[str, str] | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json", "User-Agent": "ContextGate/0.1"}
    if form is not None:
        data = urllib.parse.urlencode(form).encode("ascii")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return _read_json_response(response)
    except EmailConnectorError:
        raise
    except urllib.error.HTTPError as exc:
        # Do not surface provider bodies because they can contain account details.
        if exc.code in {400, 401, 403}:
            raise EmailConnectorError(
                "The provider rejected the request. Check the client registration, "
                "redirect URI, requested scopes, and consent."
            ) from None
        raise EmailConnectorError(
            f"The provider request failed with HTTP {exc.code}."
        ) from None
    except (OSError, TimeoutError, urllib.error.URLError):
        raise EmailConnectorError(
            "The provider could not be reached. Check the internet connection."
        ) from None


class EmailOAuthManager:
    """Coordinate two read-only OAuth providers for one local process."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._google_client_id = os.environ.get("CONTEXTGATE_GOOGLE_CLIENT_ID", "")
        self._google_client_secret = os.environ.get(
            "CONTEXTGATE_GOOGLE_CLIENT_SECRET", ""
        )
        self._google_redirect_host = "127.0.0.1"
        self._microsoft_client_id = os.environ.get(
            "CONTEXTGATE_MICROSOFT_CLIENT_ID", ""
        )
        self._pending: dict[str, _PendingAuthorization] = {}
        self._sessions: dict[Provider, dict[str, _MailboxSession]] = {
            "google": {},
            "microsoft": {},
        }

    def configure_google_json(self, raw: bytes) -> None:
        """Accept a Google Desktop OAuth client JSON for this process only."""

        if not raw or len(raw) > MAX_CLIENT_CONFIG_BYTES:
            raise EmailConnectorError("Google client JSON is empty or too large.")
        try:
            payload = json.loads(raw.decode("utf-8"))
            installed = payload["installed"]
            client_id = _clean_identifier(
                installed["client_id"], label="Google client ID", limit=1024
            )
            client_secret = _clean_identifier(
                installed.get("client_secret", ""),
                label="Google client secret",
                limit=1024,
            )
            auth_uri = installed.get("auth_uri", GOOGLE_AUTHORIZE)
            token_uri = installed.get("token_uri", GOOGLE_TOKEN)
            redirect_uris = installed.get("redirect_uris", [])
        except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
            raise EmailConnectorError(
                "Use the downloaded JSON for a Google OAuth Desktop app."
            ) from None
        if auth_uri != GOOGLE_AUTHORIZE or token_uri != GOOGLE_TOKEN:
            raise EmailConnectorError(
                "Google client JSON contains unexpected authorization endpoints."
            )
        if not isinstance(redirect_uris, list):
            raise EmailConnectorError(
                "Google client JSON contains invalid loopback redirect settings."
            )
        redirect_host = "127.0.0.1"
        if redirect_uris:
            redirect_host = ""
            for candidate in redirect_uris:
                if not isinstance(candidate, str):
                    continue
                parsed = urllib.parse.urlsplit(candidate)
                if (
                    parsed.scheme == "http"
                    and parsed.hostname in {"127.0.0.1", "localhost"}
                    and not parsed.username
                    and not parsed.password
                    and parsed.path in {"", "/"}
                    and not parsed.query
                    and not parsed.fragment
                ):
                    redirect_host = parsed.hostname
                    break
            if not redirect_host:
                raise EmailConnectorError(
                    "Google Desktop OAuth JSON must allow a localhost loopback redirect."
                )
        self.configure_google_credentials(
            client_id,
            client_secret=client_secret,
            redirect_host=redirect_host,
        )

    def configure_google_credentials(
        self,
        client_id: str,
        *,
        client_secret: str = "",
        redirect_host: str = "127.0.0.1",
    ) -> None:
        """Set a Google Desktop-app registration without requiring a JSON upload."""

        client_id = _clean_identifier(
            client_id,
            label="Google client ID",
            limit=1024,
        )
        if any(character.isspace() for character in client_id):
            raise EmailConnectorError("Google client ID is invalid.")
        cleaned_secret = ""
        if client_secret:
            cleaned_secret = _clean_identifier(
                client_secret,
                label="Google client secret",
                limit=1024,
            )
        if redirect_host not in {"127.0.0.1", "localhost"}:
            raise EmailConnectorError("Google redirect host must be local.")
        with self._lock:
            self._google_client_id = client_id
            self._google_client_secret = cleaned_secret
            self._google_redirect_host = redirect_host

    def configure_microsoft(self, client_id: str) -> None:
        """Set a public-client application ID for this process only."""

        client_id = _clean_identifier(
            client_id, label="Microsoft application client ID", limit=128
        )
        # Entra application IDs are UUIDs. Rejecting other shapes catches paste errors.
        import uuid

        try:
            uuid.UUID(client_id)
        except ValueError:
            raise EmailConnectorError(
                "Microsoft application client ID must be a valid UUID."
            ) from None
        with self._lock:
            self._microsoft_client_id = client_id

    def status(self) -> dict[str, dict[str, Any]]:
        """Return non-secret setup and connection status."""

        with self._lock:
            return {
                "google": {
                    "configured": bool(self._google_client_id),
                    "connected": bool(self._sessions["google"]),
                    "accounts": sorted(self._sessions["google"]),
                    "access": "Read-only Gmail metadata and message previews",
                    "token_storage": "Memory only — cleared when ContextGate closes",
                },
                "microsoft": {
                    "configured": bool(self._microsoft_client_id),
                    "connected": bool(self._sessions["microsoft"]),
                    "accounts": sorted(self._sessions["microsoft"]),
                    "access": "Read-only Outlook/Hotmail metadata and message previews",
                    "token_storage": "Memory only — cleared when ContextGate closes",
                },
            }

    def start_authorization(self, provider: Provider, base_url: str) -> str:
        """Create a short-lived PKCE transaction and return the provider URL."""

        if provider not in {"google", "microsoft"}:
            raise EmailConnectorError("Unknown email provider.")
        base = base_url.rstrip("/")
        parsed_base = urllib.parse.urlsplit(base)
        try:
            base_port = parsed_base.port
        except ValueError:
            base_port = None
        if (
            parsed_base.scheme != "http"
            or parsed_base.hostname not in {"127.0.0.1", "localhost"}
            or base_port is None
            or not 1 <= base_port <= 65535
            or parsed_base.path not in {"", "/"}
            or parsed_base.username
            or parsed_base.password
            or parsed_base.query
            or parsed_base.fragment
        ):
            raise EmailConnectorError(
                "OAuth is restricted to the local ContextGate URL."
            )
        with self._lock:
            client_id = (
                self._google_client_id
                if provider == "google"
                else self._microsoft_client_id
            )
            google_redirect_host = self._google_redirect_host
        if not client_id:
            provider_name = (
                "Google Desktop OAuth JSON"
                if provider == "google"
                else "Microsoft application client ID"
            )
            raise EmailConnectorError(f"Configure the {provider_name} first.")

        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(32)
        redirect_uri = (
            f"http://{google_redirect_host}:{base_port}"
            if provider == "google"
            else f"{base}/oauth/{provider}/callback"
        )
        pending = _PendingAuthorization(
            provider=provider,
            state=state,
            verifier=verifier,
            redirect_uri=redirect_uri,
            created_at=time.time(),
        )
        with self._lock:
            self._purge_pending_locked()
            self._pending[state] = pending

        if provider == "google":
            parameters = {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(GOOGLE_SCOPES),
                "access_type": "offline",
                "include_granted_scopes": "true",
                "prompt": "consent select_account",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
            return f"{GOOGLE_AUTHORIZE}?{urllib.parse.urlencode(parameters)}"

        parameters = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "response_mode": "query",
            "scope": " ".join(MICROSOFT_SCOPES),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }
        return f"{MICROSOFT_AUTHORIZE}?{urllib.parse.urlencode(parameters)}"

    def finish_authorization(
        self,
        provider: Provider,
        *,
        state: str,
        code: str,
    ) -> str:
        """Verify state, exchange the code, and return the connected address."""

        state = _clean_identifier(state, label="OAuth state", limit=256)
        code = _clean_identifier(code, label="OAuth authorization code", limit=4096)
        with self._lock:
            self._purge_pending_locked()
            pending = self._pending.pop(state, None)
            if pending is None or pending.provider != provider:
                raise EmailConnectorError(
                    "The sign-in session is missing or expired. Start again from ContextGate."
                )
            client_id = (
                self._google_client_id
                if provider == "google"
                else self._microsoft_client_id
            )
            client_secret = self._google_client_secret

        form = {
            "client_id": client_id,
            "code": code,
            "code_verifier": pending.verifier,
            "grant_type": "authorization_code",
            "redirect_uri": pending.redirect_uri,
        }
        token_url = GOOGLE_TOKEN if provider == "google" else MICROSOFT_TOKEN
        if provider == "google" and client_secret:
            form["client_secret"] = client_secret
        token_payload = _request_json(token_url, method="POST", form=form)
        access_token = _clean_identifier(
            token_payload.get("access_token"), label="Provider access token", limit=8192
        )
        refresh_token_value = token_payload.get("refresh_token")
        refresh_token = (
            _clean_identifier(
                refresh_token_value, label="Provider refresh token", limit=8192
            )
            if refresh_token_value
            else None
        )
        expires_in = token_payload.get("expires_in", 3600)
        if not isinstance(expires_in, (int, float)) or not 60 <= expires_in <= 86400:
            expires_in = 3600

        if provider == "google":
            profile = _request_json(f"{GOOGLE_GMAIL_API}/profile", token=access_token)
            account = _clean_identifier(
                profile.get("emailAddress"), label="Connected Gmail address", limit=320
            )
        else:
            profile = _request_json(
                f"{MICROSOFT_GRAPH}/me?$select=mail,userPrincipalName",
                token=access_token,
            )
            account = _clean_identifier(
                profile.get("mail") or profile.get("userPrincipalName"),
                label="Connected Microsoft address",
                limit=320,
            )
        session = _MailboxSession(
            provider=provider,
            account_email=account,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=time.time() + float(expires_in),
        )
        with self._lock:
            self._sessions[provider][account.casefold()] = session
        return account

    def list_messages(
        self,
        provider: Provider,
        *,
        account: str | None = None,
        limit: int = 10,
    ) -> list[MailSummary]:
        """Read recent messages from one account, or from all provider accounts."""

        if not 1 <= limit <= MAX_MESSAGES:
            raise EmailConnectorError(f"Message limit must be 1 to {MAX_MESSAGES}.")
        with self._lock:
            account_keys = sorted(self._sessions.get(provider, {}))
        if account is not None:
            requested = account.strip().casefold()
            account_keys = [key for key in account_keys if key == requested]
        if not account_keys:
            raise EmailConnectorError("That mailbox is not connected.")
        per_account_limit = max(1, min(limit, MAX_MESSAGES // len(account_keys)))
        combined: list[MailSummary] = []
        for account_key in account_keys:
            combined.extend(
                self._list_account_messages(
                    provider,
                    account_key=account_key,
                    limit=per_account_limit,
                )
            )
        return combined[:limit]

    def _list_account_messages(
        self,
        provider: Provider,
        *,
        account_key: str,
        limit: int,
    ) -> list[MailSummary]:
        access_token = self._usable_access_token(provider, account_key=account_key)
        if provider == "google":
            listing = _request_json(
                f"{GOOGLE_GMAIL_API}/messages?maxResults={limit}&q={urllib.parse.quote('newer_than:90d')}",
                token=access_token,
            )
            references = listing.get("messages", [])
            if not isinstance(references, list):
                raise EmailConnectorError("Gmail returned an invalid message list.")
            rows: list[MailSummary] = []
            for reference in references[:limit]:
                if not isinstance(reference, dict) or not reference.get("id"):
                    continue
                message_id = urllib.parse.quote(str(reference["id"]), safe="")
                detail = _request_json(
                    f"{GOOGLE_GMAIL_API}/messages/{message_id}?format=metadata&metadataHeaders=Subject&metadataHeaders=From&metadataHeaders=Date",
                    token=access_token,
                )
                headers = {
                    str(item.get("name", "")).casefold(): str(item.get("value", ""))
                    for item in detail.get("payload", {}).get("headers", [])
                    if isinstance(item, dict)
                }
                rows.append(
                    MailSummary(
                        provider="google",
                        message_id=str(reference["id"]),
                        subject=headers.get("subject") or "(no subject)",
                        sender=headers.get("from") or "Unknown sender",
                        received_at=headers.get("date") or "Unknown time",
                        preview=str(detail.get("snippet") or ""),
                    )
                )
            return rows

        query = urllib.parse.urlencode(
            {
                "$top": str(limit),
                "$select": "id,subject,from,receivedDateTime,bodyPreview",
                "$orderby": "receivedDateTime desc",
            }
        )
        listing = _request_json(
            f"{MICROSOFT_GRAPH}/me/messages?{query}", token=access_token
        )
        values = listing.get("value", [])
        if not isinstance(values, list):
            raise EmailConnectorError("Microsoft returned an invalid message list.")
        rows = []
        for item in values[:limit]:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            sender = item.get("from", {}).get("emailAddress", {})
            sender_text = (
                sender.get("name") or sender.get("address") or "Unknown sender"
            )
            rows.append(
                MailSummary(
                    provider="microsoft",
                    message_id=str(item["id"]),
                    subject=str(item.get("subject") or "(no subject)"),
                    sender=str(sender_text),
                    received_at=str(item.get("receivedDateTime") or "Unknown time"),
                    preview=str(item.get("bodyPreview") or ""),
                )
            )
        return rows

    def disconnect(self, provider: Provider, *, account: str | None = None) -> int:
        """Remove one or all local sessions and best-effort revoke Google access."""

        with self._lock:
            sessions = self._sessions.get(provider)
            if sessions is None:
                raise EmailConnectorError("Unknown email provider.")
            if account is None:
                removed = list(sessions.values())
                sessions.clear()
            else:
                session = sessions.pop(account.strip().casefold(), None)
                removed = [session] if session is not None else []
        if provider == "google":
            for session in removed:
                token = session.refresh_token or session.access_token
                try:
                    _request_json(
                        GOOGLE_REVOKE,
                        method="POST",
                        form={"token": token},
                    )
                except EmailConnectorError:
                    # Local disconnect remains effective; remote revocation can be
                    # done from Google's account-security page if needed.
                    pass
        return len(removed)

    def _usable_access_token(self, provider: Provider, *, account_key: str) -> str:
        with self._lock:
            session = self._sessions.get(provider, {}).get(account_key)
            if session is None:
                raise EmailConnectorError("That mailbox is not connected.")
            if session.expires_at > time.time() + 60:
                return session.access_token
            refresh_token = session.refresh_token
            account = session.account_email
            client_id = (
                self._google_client_id
                if provider == "google"
                else self._microsoft_client_id
            )
            client_secret = self._google_client_secret
        if not refresh_token:
            raise EmailConnectorError("The mailbox session expired. Sign in again.")
        form = {
            "client_id": client_id,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        if provider == "google" and client_secret:
            form["client_secret"] = client_secret
        if provider == "microsoft":
            form["scope"] = " ".join(MICROSOFT_SCOPES)
        payload = _request_json(
            GOOGLE_TOKEN if provider == "google" else MICROSOFT_TOKEN,
            method="POST",
            form=form,
        )
        access_token = _clean_identifier(
            payload.get("access_token"), label="Provider access token", limit=8192
        )
        expires_in = payload.get("expires_in", 3600)
        if not isinstance(expires_in, (int, float)):
            expires_in = 3600
        updated = _MailboxSession(
            provider=provider,
            account_email=account,
            access_token=access_token,
            refresh_token=str(payload.get("refresh_token") or refresh_token),
            expires_at=time.time() + float(expires_in),
        )
        with self._lock:
            self._sessions[provider][account_key] = updated
        return access_token

    def _purge_pending_locked(self) -> None:
        cutoff = time.time() - PENDING_SECONDS
        self._pending = {
            state: pending
            for state, pending in self._pending.items()
            if pending.created_at >= cutoff
        }
