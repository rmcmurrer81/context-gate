"""Small, local company-profile store for the one-screen operator console.

The profile is display and interpretation configuration.  It is not an
identity provider, does not prove company access, and never contains mailbox
passwords or OAuth tokens.  Enforcement remains controlled by the validated
ContextGate policy object.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_PROFILE_BYTES = 16 * 1024
MAX_IDENTITY_FIELDS = 8
MAX_SOURCE_FILTERS = 32


class CompanyProfileError(ValueError):
    """Raised with a safe message when a local profile cannot be used."""


class CompanyProfile(BaseModel):
    """Validated operator-selected context for one local workspace."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
    )

    company_name: str = Field(default="", max_length=80)
    operator_name: str = Field(default="", max_length=80)
    important_detail: str = Field(default="Crowd size", min_length=2, max_length=120)
    identity_fields: list[str] = Field(
        default_factory=lambda: ["Event name", "Event date"],
        min_length=1,
        max_length=MAX_IDENTITY_FIELDS,
    )
    risk_posture: Literal["safety_first", "custom_policy"] = "safety_first"
    source_mode: Literal["fictional_demo", "file_upload", "company_api"] = (
        "fictional_demo"
    )
    voice_enabled: bool = True
    mail_scan_limit: int = Field(default=25, ge=1, le=25)
    auto_monitor_enabled: bool = False
    auto_monitor_minutes: int = Field(default=15, ge=1, le=1440)
    document_company_header: bool = True
    document_footer: str = Field(default="", max_length=240)
    company_website: str = Field(default="", max_length=240)
    hidden_sources: list[str] = Field(
        default_factory=list, max_length=MAX_SOURCE_FILTERS
    )
    deleted_sources: list[str] = Field(
        default_factory=list, max_length=MAX_SOURCE_FILTERS
    )

    @field_validator("company_name", "operator_name", "important_detail")
    @classmethod
    def display_text_is_safe(cls, value: str) -> str:
        if any(unicodedata.category(character).startswith("C") for character in value):
            raise ValueError("must not contain control characters")
        return value

    @field_validator("document_footer")
    @classmethod
    def optional_footer_is_safe(cls, value: str) -> str:
        if any(unicodedata.category(character).startswith("C") for character in value):
            raise ValueError("document footer must not contain control characters")
        return " ".join(value.split())

    @field_validator("company_website")
    @classmethod
    def optional_website_is_safe(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            return ""
        parsed = urlparse(cleaned)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("company website must be an http or https URL")
        return cleaned

    @field_validator("identity_fields")
    @classmethod
    def identity_fields_are_safe_and_unique(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for value in values:
            item = " ".join(value.split())
            if not 1 <= len(item) <= 80:
                raise ValueError("identity fields must contain 1 to 80 characters")
            if any(
                unicodedata.category(character).startswith("C") for character in item
            ):
                raise ValueError("identity fields must not contain control characters")
            key = item.casefold()
            if key in seen:
                raise ValueError("identity fields must be unique")
            seen.add(key)
            cleaned.append(item)
        return cleaned

    @field_validator("hidden_sources", "deleted_sources")
    @classmethod
    def source_filters_are_safe_and_unique(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for value in values:
            item = " ".join(value.split())
            if not 1 <= len(item) <= 120:
                raise ValueError("source filters must contain 1 to 120 characters")
            if any(
                unicodedata.category(character).startswith("C") for character in item
            ):
                raise ValueError("source filters must not contain control characters")
            key = item.casefold()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(item)
        return cleaned

    @property
    def identity_summary(self) -> str:
        return ", ".join(self.identity_fields)


DEFAULT_COMPANY_PROFILE = CompanyProfile()


def parse_identity_fields(value: str) -> list[str]:
    """Parse a short comma-delimited UI value into validated identity fields."""

    if not isinstance(value, str) or len(value) > 800:
        raise CompanyProfileError("Identity fields are invalid.")
    fields = [item.strip() for item in re.split(r"[,;]", value) if item.strip()]
    if not fields:
        raise CompanyProfileError("Enter at least one identity field.")
    if len(fields) > MAX_IDENTITY_FIELDS:
        raise CompanyProfileError(
            f"Use no more than {MAX_IDENTITY_FIELDS} identity fields."
        )
    return fields


def load_company_profile(path: str | os.PathLike[str]) -> CompanyProfile:
    """Load a bounded UTF-8 JSON profile, or return the safe first-run default."""

    profile_path = Path(path)
    if not profile_path.exists():
        return DEFAULT_COMPANY_PROFILE
    try:
        if (
            not profile_path.is_file()
            or profile_path.stat().st_size > MAX_PROFILE_BYTES
        ):
            raise CompanyProfileError("Company profile is not a valid bounded file.")
        raw = profile_path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
        return CompanyProfile.model_validate(payload)
    except CompanyProfileError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise CompanyProfileError("Company profile could not be read safely.") from None


def save_company_profile(path: str | os.PathLike[str], profile: CompanyProfile) -> None:
    """Atomically persist a validated profile without following a target symlink."""

    if not isinstance(profile, CompanyProfile):
        profile = CompanyProfile.model_validate(profile)
    profile_path = Path(path)
    try:
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        if profile_path.exists() and profile_path.is_symlink():
            raise CompanyProfileError(
                "Company profile path must not be a symbolic link."
            )
        payload = json.dumps(
            profile.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ).encode("utf-8")
        if len(payload) > MAX_PROFILE_BYTES:
            raise CompanyProfileError("Company profile exceeds the storage limit.")
        descriptor, temp_name = tempfile.mkstemp(
            prefix=".company-profile-",
            suffix=".tmp",
            dir=profile_path.parent,
        )
        try:
            with os.fdopen(descriptor, "wb") as temporary:
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temp_name, profile_path)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
    except CompanyProfileError:
        raise
    except OSError:
        raise CompanyProfileError(
            "Company profile could not be saved safely."
        ) from None
