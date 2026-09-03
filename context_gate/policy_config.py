"""Strict, bounded source-policy configuration for self-hosted deployments."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

POLICY_PATH_ENV = "CONTEXTGATE_POLICY_PATH"
MAX_POLICY_BYTES = 64 * 1024
MAX_POLICY_PATH_LENGTH = 4096
MAX_SOURCES = 128

SourceType = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")]


class PolicyConfigError(ValueError):
    """Raised when an explicitly configured policy cannot be loaded safely."""


class _StrictConfigModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
    )


class SourcePolicyConfig(_StrictConfigModel):
    rank: int = Field(ge=0, le=100)
    trust_cap: float = Field(ge=0.0, le=1.0)
    label: str = Field(min_length=1, max_length=128)

    @field_validator("label")
    @classmethod
    def label_must_be_display_safe(cls, value: str) -> str:
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("must not contain control characters")
        return value


class CompanyPolicyConfig(_StrictConfigModel):
    policy_version: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    sources: dict[SourceType, SourcePolicyConfig] = Field(
        min_length=1,
        max_length=MAX_SOURCES,
    )
    minimum_automatic_authority_rank: int = Field(ge=0, le=100)
    minimum_automatic_trust: float = Field(ge=0.0, le=1.0)
    near_peer_max_authority_rank_gap: int = Field(default=5, ge=0, le=100)
    near_peer_max_trust_gap: float = Field(default=0.10, ge=0.0, le=1.0)

    @field_validator("sources")
    @classmethod
    def unknown_source_is_required(
        cls, value: dict[str, SourcePolicyConfig]
    ) -> dict[str, SourcePolicyConfig]:
        if "unknown" not in value:
            raise ValueError("must include an 'unknown' source policy")
        return value


@dataclass(frozen=True, slots=True)
class AuthorityPolicy:
    rank: int
    trust_cap: float
    label: str


@dataclass(frozen=True, slots=True)
class ActivePolicy:
    policy_version: str
    policy_fingerprint: str
    sources: Mapping[str, AuthorityPolicy]
    minimum_automatic_authority_rank: int
    minimum_automatic_trust: float
    near_peer_max_authority_rank_gap: int
    near_peer_max_trust_gap: float


DEFAULT_POLICY_PAYLOAD: dict[str, Any] = {
    "policy_version": "contextgate-default-policy-1.0.0",
    "sources": {
        "registration_confirmation": {
            "rank": 100,
            "trust_cap": 1.00,
            "label": "Official registration confirmation",
        },
        "organizer_api": {
            "rank": 98,
            "trust_cap": 1.00,
            "label": "Organizer-controlled API",
        },
        "organizer_website": {
            "rank": 95,
            "trust_cap": 0.98,
            "label": "Organizer website",
        },
        "official_email": {
            "rank": 92,
            "trust_cap": 0.98,
            "label": "Verified organizer email",
        },
        "partner_website": {
            "rank": 70,
            "trust_cap": 0.85,
            "label": "Named event partner",
        },
        "copied_webpage": {
            "rank": 50,
            "trust_cap": 0.70,
            "label": "Copied or community webpage",
        },
        "user_report": {
            "rank": 40,
            "trust_cap": 0.65,
            "label": "Unverified user report",
        },
        "unknown": {
            "rank": 10,
            "trust_cap": 0.40,
            "label": "Unknown source",
        },
    },
    "minimum_automatic_authority_rank": 70,
    "minimum_automatic_trust": 0.70,
    "near_peer_max_authority_rank_gap": 5,
    "near_peer_max_trust_gap": 0.10,
}


def _canonical_payload(config: CompanyPolicyConfig) -> str:
    return json.dumps(
        config.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _to_active_policy(config: CompanyPolicyConfig) -> ActivePolicy:
    canonical = _canonical_payload(config)
    sources = MappingProxyType(
        {
            source_type: AuthorityPolicy(
                rank=source.rank,
                trust_cap=source.trust_cap,
                label=source.label,
            )
            for source_type, source in config.sources.items()
        }
    )
    return ActivePolicy(
        policy_version=config.policy_version,
        policy_fingerprint=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        sources=sources,
        minimum_automatic_authority_rank=config.minimum_automatic_authority_rank,
        minimum_automatic_trust=config.minimum_automatic_trust,
        near_peer_max_authority_rank_gap=config.near_peer_max_authority_rank_gap,
        near_peer_max_trust_gap=config.near_peer_max_trust_gap,
    )


DEFAULT_POLICY = _to_active_policy(
    CompanyPolicyConfig.model_validate(DEFAULT_POLICY_PAYLOAD)
)


def _without_input_details(exc: ValidationError) -> str:
    details: list[str] = []
    for error in exc.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    ):
        location = ".".join(str(item) for item in error["loc"]) or "policy"
        details.append(f"{location}: {error['msg']}")
    return "; ".join(details)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PolicyConfigError(
                "Policy configuration contains a duplicate JSON key."
            )
        result[key] = value
    return result


_PARSED_CACHE: OrderedDict[str, ActivePolicy] = OrderedDict()
_CACHE_LIMIT = 8


def _parse_policy(raw: bytes) -> ActivePolicy:
    content_fingerprint = hashlib.sha256(raw).hexdigest()
    cached = _PARSED_CACHE.get(content_fingerprint)
    if cached is not None:
        _PARSED_CACHE.move_to_end(content_fingerprint)
        return cached
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PolicyConfigError("Policy configuration must be UTF-8 JSON.") from exc
    try:
        payload = json.loads(text, object_pairs_hook=_unique_object)
    except PolicyConfigError:
        raise
    except json.JSONDecodeError as exc:
        raise PolicyConfigError(
            f"Policy configuration is not valid JSON (line {exc.lineno}, column {exc.colno})."
        ) from exc
    try:
        config = CompanyPolicyConfig.model_validate(payload)
    except ValidationError as exc:
        raise PolicyConfigError(
            f"Policy configuration is invalid: {_without_input_details(exc)}"
        ) from exc
    active = _to_active_policy(config)
    _PARSED_CACHE[content_fingerprint] = active
    _PARSED_CACHE.move_to_end(content_fingerprint)
    while len(_PARSED_CACHE) > _CACHE_LIMIT:
        _PARSED_CACHE.popitem(last=False)
    return active


def _absolute_without_resolving_links(raw_path: str) -> Path:
    if not raw_path or len(raw_path) > MAX_POLICY_PATH_LENGTH:
        raise PolicyConfigError("Configured policy path is empty or too long.")
    expanded = Path(raw_path).expanduser()
    return Path(os.path.abspath(os.fspath(expanded)))


def _reject_symbolic_links(path: Path) -> None:
    current = path
    while True:
        try:
            if current.is_symlink():
                raise PolicyConfigError(
                    "Configured policy path must not contain symbolic links."
                )
        except OSError as exc:
            raise PolicyConfigError(
                "Configured policy path cannot be inspected."
            ) from exc
        parent = current.parent
        if parent == current:
            return
        current = parent


def _read_bounded_policy(path: Path) -> bytes:
    _reject_symbolic_links(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except (OSError, ValueError) as exc:
        raise PolicyConfigError("Configured policy file cannot be opened.") from exc
    try:
        file_status = os.fstat(descriptor)
        if not stat.S_ISREG(file_status.st_mode):
            raise PolicyConfigError(
                "Configured policy path must identify a regular file."
            )
        if file_status.st_size > MAX_POLICY_BYTES:
            raise PolicyConfigError(
                f"Configured policy file exceeds the {MAX_POLICY_BYTES}-byte limit."
            )
        chunks: list[bytes] = []
        bytes_remaining = MAX_POLICY_BYTES + 1
        while bytes_remaining:
            chunk = os.read(descriptor, min(bytes_remaining, 16 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            bytes_remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_POLICY_BYTES:
            raise PolicyConfigError(
                f"Configured policy file exceeds the {MAX_POLICY_BYTES}-byte limit."
            )
        return raw
    except OSError as exc:
        raise PolicyConfigError("Configured policy file cannot be read.") from exc
    finally:
        os.close(descriptor)


def get_active_policy() -> ActivePolicy:
    """Return the default policy or load the explicitly configured strict policy.

    Explicit configuration is fail-closed: an unreadable or invalid file raises
    ``PolicyConfigError`` instead of silently switching back to defaults. The file
    is content-hashed on each load, so an atomic policy replacement is visible to
    the next decision while parsed immutable policy objects remain cheaply cached.
    """

    raw_path = os.environ.get(POLICY_PATH_ENV)
    if raw_path is None:
        return DEFAULT_POLICY
    path = _absolute_without_resolving_links(raw_path)
    return _parse_policy(_read_bounded_policy(path))


def clear_policy_cache() -> None:
    """Clear parsed policy objects, primarily for isolated test processes."""

    _PARSED_CACHE.clear()
