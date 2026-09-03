"""Tenant-scoped local memory and deterministic company pattern analysis.

The memory in this module is deliberately modest: it persists bounded, structured
observations in a caller-selected SQLite database and computes explainable counts.
It does not call a model, browse the internet, send messages, or execute actions.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator

from context_gate.models import EvidenceStatus, Sensitivity
from context_gate.policy_config import DEFAULT_POLICY, ActivePolicy

MAX_ATTRIBUTES = 32
MAX_ATTRIBUTE_KEY_LENGTH = 64
MAX_ATTRIBUTE_VALUE_LENGTH = 512
MAX_NORMALIZED_VALUE_LENGTH = MAX_ATTRIBUTE_VALUE_LENGTH * 4
MAX_PATTERN_DISPLAY_LENGTH = (MAX_ATTRIBUTE_VALUE_LENGTH * 2) + 2
MAX_PATTERN_CONTRIBUTORS = 25
MAX_LIST_LIMIT = 5_000
MAX_HISTORY_LIMIT = MAX_LIST_LIMIT - 1
MAX_DB_PATH_LENGTH = 4_096
DEFAULT_MINIMUM_SUPPORT = 3
DEFAULT_MINIMUM_TRUST = 0.70

AttributeKey = Annotated[
    str,
    Field(min_length=1, max_length=MAX_ATTRIBUTE_KEY_LENGTH),
]
AttributeValue = Annotated[
    str,
    Field(min_length=1, max_length=MAX_ATTRIBUTE_VALUE_LENGTH),
]


class _MemoryModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        use_enum_values=False,
        hide_input_in_errors=True,
        frozen=True,
        revalidate_instances="always",
        validate_default=True,
    )


def _utc_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include an explicit timezone")
    return value.astimezone(UTC)


def _contains_control_characters(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)


def _display_text(value: str) -> str:
    """Collapse whitespace while retaining the value's human-readable casing."""

    return " ".join(unicodedata.normalize("NFKC", value).split())


def normalize_pattern_text(value: str) -> str:
    """Return the deterministic comparison form used by the pattern engine."""

    return _display_text(value).casefold()


def normalize_attribute_key(value: str) -> str:
    """Normalize common key spelling variants without changing stored display text."""

    normalized = normalize_pattern_text(value)
    return re.sub(r"[\s_-]+", " ", normalized).strip()


def _validate_attributes(value: dict[str, str]) -> dict[str, str]:
    normalized_keys: set[str] = set()
    for key, item in value.items():
        if _contains_control_characters(key) or _contains_control_characters(item):
            raise ValueError("attribute text must not contain control characters")
        normalized_key = normalize_attribute_key(key)
        if not normalized_key:
            raise ValueError("attribute keys must contain visible text")
        if normalized_key in normalized_keys:
            raise ValueError("attribute keys must be unique after normalization")
        normalized_keys.add(normalized_key)
    return value


class CompanyObservation(_MemoryModel):
    """A bounded fact supplied to one company's private local memory."""

    tenant_id: str = Field(min_length=1, max_length=128)
    observation_id: str = Field(min_length=1, max_length=128)
    category: str = Field(min_length=1, max_length=128)
    occurred_at: datetime
    attributes: dict[AttributeKey, AttributeValue] = Field(
        min_length=1,
        max_length=MAX_ATTRIBUTES,
    )
    source_type: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$",
    )
    trust_score: float = Field(ge=0.0, le=1.0, strict=True)
    status: EvidenceStatus = EvidenceStatus.UNVERIFIED
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    evidence_reference: str | None = Field(default=None, max_length=2_048)

    _normalize_occurred_at = field_validator("occurred_at", mode="after")(
        _utc_timestamp
    )

    @field_validator(
        "tenant_id",
        "observation_id",
        "category",
        "source_type",
        "evidence_reference",
    )
    @classmethod
    def text_fields_are_display_safe(cls, value: str | None) -> str | None:
        if value is not None and _contains_control_characters(value):
            raise ValueError("must not contain control characters")
        return value

    @field_validator("attributes")
    @classmethod
    def attributes_are_safe_and_unambiguous(
        cls, value: dict[str, str]
    ) -> dict[str, str]:
        return _validate_attributes(value)


class HumanCorrection(_MemoryModel):
    """An immutable human correction linked to, but not replacing, an observation."""

    tenant_id: str = Field(min_length=1, max_length=128)
    correction_id: str = Field(min_length=1, max_length=128)
    target_observation_id: str = Field(min_length=1, max_length=128)
    submitted_at: datetime
    reviewer: str = Field(min_length=1, max_length=128)
    rationale: str = Field(min_length=3, max_length=2_000)
    corrected_attributes: dict[AttributeKey, AttributeValue] = Field(
        min_length=1,
        max_length=MAX_ATTRIBUTES,
    )
    evidence_reference: str = Field(min_length=1, max_length=2_048)

    _normalize_submitted_at = field_validator("submitted_at", mode="after")(
        _utc_timestamp
    )

    @field_validator(
        "tenant_id",
        "correction_id",
        "target_observation_id",
        "reviewer",
        "rationale",
        "evidence_reference",
    )
    @classmethod
    def text_fields_are_display_safe(cls, value: str) -> str:
        if _contains_control_characters(value):
            raise ValueError("must not contain control characters")
        return value

    @field_validator("corrected_attributes")
    @classmethod
    def attributes_are_safe_and_unambiguous(
        cls, value: dict[str, str]
    ) -> dict[str, str]:
        return _validate_attributes(value)


class MemoryStoreError(RuntimeError):
    """Base class for sanitized company-memory persistence errors."""


class MemoryCollisionError(MemoryStoreError):
    """Raised when an existing tenant/observation identity changes content."""


class MemoryStore:
    """Bounded, tenant-isolated SQLite storage for ``CompanyObservation`` values."""

    _EXPECTED_COLUMNS: ClassVar[set[str]] = {
        "tenant_id",
        "observation_id",
        "category_key",
        "occurred_at",
        "payload_digest",
        "payload_json",
    }
    _EXPECTED_CORRECTION_COLUMNS: ClassVar[set[str]] = {
        "tenant_id",
        "correction_id",
        "target_observation_id",
        "submitted_at",
        "payload_digest",
        "payload_json",
    }

    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        raw_path = os.fspath(db_path)
        if raw_path == ":memory:":
            connection_target = raw_path
        else:
            if not raw_path or len(raw_path) > MAX_DB_PATH_LENGTH or "\x00" in raw_path:
                raise MemoryStoreError("Company memory database path is invalid.")
            path = Path(raw_path).expanduser()
            if not path.parent.exists() or not path.parent.is_dir():
                raise MemoryStoreError(
                    "Company memory database parent directory does not exist."
                )
            connection_target = os.fspath(path.absolute())

        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                connection_target,
                timeout=5.0,
                isolation_level=None,
                uri=False,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA temp_store = MEMORY")
        except (OSError, sqlite3.Error):
            if connection is not None:
                connection.close()
            raise MemoryStoreError(
                "Company memory database could not be opened safely."
            ) from None

        self._connection = connection
        self._lock = threading.RLock()
        self._closed = False
        try:
            self._initialize_schema()
        except Exception:
            self.close()
            raise

    def _initialize_schema(self) -> None:
        statement = """
            CREATE TABLE IF NOT EXISTS company_observations (
                tenant_id TEXT NOT NULL,
                observation_id TEXT NOT NULL,
                category_key TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                payload_digest TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (tenant_id, observation_id)
            ) WITHOUT ROWID
        """
        try:
            with self._lock:
                self._connection.execute("BEGIN IMMEDIATE")
                self._connection.execute(statement)
                columns = {
                    row["name"]
                    for row in self._connection.execute(
                        "PRAGMA table_info(company_observations)"
                    ).fetchall()
                }
                if columns != self._EXPECTED_COLUMNS:
                    raise MemoryStoreError(
                        "Company memory database schema is incompatible."
                    )
                self._connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS human_corrections (
                        tenant_id TEXT NOT NULL,
                        correction_id TEXT NOT NULL,
                        target_observation_id TEXT NOT NULL,
                        submitted_at TEXT NOT NULL,
                        payload_digest TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        PRIMARY KEY (tenant_id, correction_id),
                        FOREIGN KEY (tenant_id, target_observation_id)
                            REFERENCES company_observations (tenant_id, observation_id)
                            ON UPDATE RESTRICT ON DELETE RESTRICT
                    ) WITHOUT ROWID
                    """
                )
                correction_columns = {
                    row["name"]
                    for row in self._connection.execute(
                        "PRAGMA table_info(human_corrections)"
                    ).fetchall()
                }
                if correction_columns != self._EXPECTED_CORRECTION_COLUMNS:
                    raise MemoryStoreError(
                        "Company memory correction schema is incompatible."
                    )
                self._connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS company_observations_tenant_time
                    ON company_observations (tenant_id, occurred_at DESC, observation_id)
                    """
                )
                self._connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS human_corrections_tenant_target
                    ON human_corrections
                    (tenant_id, target_observation_id, submitted_at DESC, correction_id)
                    """
                )
                self._connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS company_observations_tenant_category
                    ON company_observations
                    (tenant_id, category_key, occurred_at DESC, observation_id)
                    """
                )
                self._connection.execute("COMMIT")
        except MemoryStoreError:
            self._rollback_quietly()
            raise
        except sqlite3.Error:
            self._rollback_quietly()
            raise MemoryStoreError(
                "Company memory database could not be initialized safely."
            ) from None

    def _rollback_quietly(self) -> None:
        try:
            self._connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def _ensure_open(self) -> None:
        if self._closed:
            raise MemoryStoreError("Company memory database is closed.")

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def __enter__(self) -> Self:
        self._ensure_open()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @staticmethod
    def _canonical_payload(
        record: CompanyObservation | HumanCorrection,
        *,
        attributes_field: str,
    ) -> tuple[str, str]:
        payload = record.model_dump(mode="json")
        attributes = payload[attributes_field]
        payload[attributes_field] = {
            key: attributes[key]
            for key in sorted(
                attributes,
                key=lambda item: (normalize_attribute_key(item), item),
            )
        }
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return serialized, hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _verified_observation_row(self, row: sqlite3.Row) -> CompanyObservation:
        try:
            observation = CompanyObservation.model_validate(
                json.loads(row["payload_json"])
            )
            _serialized, computed_digest = self._canonical_payload(
                observation,
                attributes_field="attributes",
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            raise MemoryStoreError(
                "Company memory observation integrity check failed."
            ) from None
        columns_match = (
            observation.tenant_id == row["tenant_id"]
            and observation.observation_id == row["observation_id"]
            and normalize_pattern_text(observation.category) == row["category_key"]
            and observation.occurred_at.isoformat() == row["occurred_at"]
        )
        if not columns_match or not hmac.compare_digest(
            computed_digest,
            row["payload_digest"],
        ):
            raise MemoryStoreError("Company memory observation integrity check failed.")
        return observation

    def _verified_correction_row(self, row: sqlite3.Row) -> HumanCorrection:
        try:
            correction = HumanCorrection.model_validate(json.loads(row["payload_json"]))
            _serialized, computed_digest = self._canonical_payload(
                correction,
                attributes_field="corrected_attributes",
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            raise MemoryStoreError(
                "Company memory correction integrity check failed."
            ) from None
        columns_match = (
            correction.tenant_id == row["tenant_id"]
            and correction.correction_id == row["correction_id"]
            and correction.target_observation_id == row["target_observation_id"]
            and correction.submitted_at.isoformat() == row["submitted_at"]
        )
        if not columns_match or not hmac.compare_digest(
            computed_digest,
            row["payload_digest"],
        ):
            raise MemoryStoreError("Company memory correction integrity check failed.")
        return correction

    def upsert(
        self, observation: CompanyObservation
    ) -> Literal["inserted", "unchanged"]:
        """Insert once, accept an identical retry, and reject identity mutation."""

        if not isinstance(observation, CompanyObservation):
            raise TypeError("observation must be a validated CompanyObservation")
        observation = CompanyObservation.model_validate(observation)
        payload_json, payload_digest = self._canonical_payload(
            observation,
            attributes_field="attributes",
        )
        with self._lock:
            self._ensure_open()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                existing = self._connection.execute(
                    """
                    SELECT tenant_id, observation_id, category_key, occurred_at,
                           payload_digest, payload_json
                    FROM company_observations
                    WHERE tenant_id = ? AND observation_id = ?
                    """,
                    (observation.tenant_id, observation.observation_id),
                ).fetchone()
                if existing is not None:
                    stored_observation = self._verified_observation_row(existing)
                    _stored_json, stored_digest = self._canonical_payload(
                        stored_observation,
                        attributes_field="attributes",
                    )
                    if not hmac.compare_digest(stored_digest, payload_digest):
                        raise MemoryCollisionError(
                            "Observation identity collision: stored content differs."
                        )
                    self._connection.execute("COMMIT")
                    return "unchanged"

                self._connection.execute(
                    """
                    INSERT INTO company_observations (
                        tenant_id,
                        observation_id,
                        category_key,
                        occurred_at,
                        payload_digest,
                        payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        observation.tenant_id,
                        observation.observation_id,
                        normalize_pattern_text(observation.category),
                        observation.occurred_at.isoformat(),
                        payload_digest,
                        payload_json,
                    ),
                )
                self._connection.execute("COMMIT")
                return "inserted"
            except MemoryStoreError:
                self._rollback_quietly()
                raise
            except sqlite3.Error:
                self._rollback_quietly()
                raise MemoryStoreError(
                    "Company memory observation could not be stored."
                ) from None

    def append_correction(
        self, correction: HumanCorrection
    ) -> Literal["inserted", "unchanged"]:
        """Append an immutable correction while preserving its source observation."""

        if not isinstance(correction, HumanCorrection):
            raise TypeError("correction must be a validated HumanCorrection")
        correction = HumanCorrection.model_validate(correction)
        payload_json, payload_digest = self._canonical_payload(
            correction,
            attributes_field="corrected_attributes",
        )
        with self._lock:
            self._ensure_open()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                target = self._connection.execute(
                    """
                    SELECT tenant_id, observation_id, category_key, occurred_at,
                           payload_digest, payload_json
                    FROM company_observations
                    WHERE tenant_id = ? AND observation_id = ?
                    """,
                    (correction.tenant_id, correction.target_observation_id),
                ).fetchone()
                if target is None:
                    raise MemoryStoreError(
                        "Correction target is unavailable for this tenant."
                    )
                self._verified_observation_row(target)
                existing = self._connection.execute(
                    """
                    SELECT tenant_id, correction_id, target_observation_id,
                           submitted_at, payload_digest, payload_json
                    FROM human_corrections
                    WHERE tenant_id = ? AND correction_id = ?
                    """,
                    (correction.tenant_id, correction.correction_id),
                ).fetchone()
                if existing is not None:
                    stored_correction = self._verified_correction_row(existing)
                    _stored_json, stored_digest = self._canonical_payload(
                        stored_correction,
                        attributes_field="corrected_attributes",
                    )
                    if not hmac.compare_digest(stored_digest, payload_digest):
                        raise MemoryCollisionError(
                            "Correction identity collision: stored content differs."
                        )
                    self._connection.execute("COMMIT")
                    return "unchanged"

                self._connection.execute(
                    """
                    INSERT INTO human_corrections (
                        tenant_id,
                        correction_id,
                        target_observation_id,
                        submitted_at,
                        payload_digest,
                        payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        correction.tenant_id,
                        correction.correction_id,
                        correction.target_observation_id,
                        correction.submitted_at.isoformat(),
                        payload_digest,
                        payload_json,
                    ),
                )
                self._connection.execute("COMMIT")
                return "inserted"
            except (MemoryCollisionError, MemoryStoreError):
                self._rollback_quietly()
                raise
            except sqlite3.Error:
                self._rollback_quietly()
                raise MemoryStoreError(
                    "Company memory correction could not be stored."
                ) from None

    def list_observations(
        self,
        tenant_id: str,
        *,
        category: str | None = None,
        as_of: datetime | None = None,
        limit: int = 100,
    ) -> list[CompanyObservation]:
        """Return only one tenant's records in stable newest-first order."""

        _validate_query_text("tenant_id", tenant_id, 128)
        if category is not None:
            _validate_query_text("category", category, 128)
        if as_of is not None:
            as_of = _utc_timestamp(as_of)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_LIST_LIMIT
        ):
            raise ValueError(f"limit must be between 1 and {MAX_LIST_LIMIT}")

        parameters: list[str | int] = [tenant_id]
        category_clause = ""
        if category is not None:
            category_clause = " AND category_key = ?"
            parameters.append(normalize_pattern_text(category))
        as_of_clause = ""
        if as_of is not None:
            as_of_clause = " AND occurred_at <= ?"
            parameters.append(as_of.isoformat())
        parameters.append(limit)
        query = f"""
            SELECT tenant_id, observation_id, category_key, occurred_at,
                   payload_digest, payload_json
            FROM company_observations
            WHERE tenant_id = ?{category_clause}{as_of_clause}
            ORDER BY occurred_at DESC, observation_id ASC
            LIMIT ?
        """
        with self._lock:
            self._ensure_open()
            try:
                rows = self._connection.execute(query, parameters).fetchall()
                observations = [self._verified_observation_row(row) for row in rows]
            except MemoryStoreError:
                raise
            except (json.JSONDecodeError, sqlite3.Error, ValueError):
                raise MemoryStoreError(
                    "Company memory observations could not be read safely."
                ) from None
        if any(item.tenant_id != tenant_id for item in observations):
            raise MemoryStoreError("Company memory tenant isolation check failed.")
        return observations

    def list_corrections(
        self,
        tenant_id: str,
        *,
        target_observation_id: str | None = None,
        limit: int = 100,
    ) -> list[HumanCorrection]:
        """Return one tenant's append-only corrections in stable newest-first order."""

        _validate_query_text("tenant_id", tenant_id, 128)
        if target_observation_id is not None:
            _validate_query_text("target_observation_id", target_observation_id, 128)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_LIST_LIMIT
        ):
            raise ValueError(f"limit must be between 1 and {MAX_LIST_LIMIT}")

        parameters: list[str | int] = [tenant_id]
        target_clause = ""
        if target_observation_id is not None:
            target_clause = " AND target_observation_id = ?"
            parameters.append(target_observation_id)
        parameters.append(limit)
        query = f"""
            SELECT tenant_id, correction_id, target_observation_id, submitted_at,
                   payload_digest, payload_json
            FROM human_corrections
            WHERE tenant_id = ?{target_clause}
            ORDER BY submitted_at DESC, correction_id ASC
            LIMIT ?
        """
        with self._lock:
            self._ensure_open()
            try:
                rows = self._connection.execute(query, parameters).fetchall()
                corrections = [self._verified_correction_row(row) for row in rows]
            except MemoryStoreError:
                raise
            except (json.JSONDecodeError, sqlite3.Error, ValueError):
                raise MemoryStoreError(
                    "Company memory corrections could not be read safely."
                ) from None
        if any(item.tenant_id != tenant_id for item in corrections):
            raise MemoryStoreError("Company memory tenant isolation check failed.")
        return corrections


def _validate_query_text(field_name: str, value: str, maximum_length: int) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum_length
        or _contains_control_characters(value)
    ):
        raise ValueError(f"{field_name} is invalid")


class PatternOutcome(StrEnum):
    ALLOW_LIKE = "ALLOW_LIKE"
    REVIEW = "REVIEW"


class PatternContributor(_MemoryModel):
    observation_id: str = Field(min_length=1, max_length=128)
    occurred_at: datetime
    source_type: str = Field(min_length=1, max_length=128)
    status: EvidenceStatus
    sensitivity: Sensitivity
    trust_score: float = Field(ge=0.0, le=1.0)
    evidence_reference: str | None = Field(default=None, max_length=2_048)
    counted_as_trusted_support: bool

    _normalize_occurred_at = field_validator("occurred_at", mode="after")(
        _utc_timestamp
    )


class AttributePattern(_MemoryModel):
    attribute_key: str = Field(min_length=1, max_length=MAX_ATTRIBUTE_KEY_LENGTH)
    display_value: str = Field(min_length=1, max_length=MAX_PATTERN_DISPLAY_LENGTH)
    normalized_value: str = Field(min_length=1, max_length=MAX_NORMALIZED_VALUE_LENGTH)
    count: int = Field(ge=1)
    trusted_count: int = Field(ge=0)
    contributors: list[PatternContributor] = Field(max_length=MAX_PATTERN_CONTRIBUTORS)
    contributors_truncated: bool


class ConditionalPattern(_MemoryModel):
    parent_attribute: str = Field(min_length=1, max_length=MAX_ATTRIBUTE_KEY_LENGTH)
    parent_display_value: str = Field(
        min_length=1, max_length=MAX_ATTRIBUTE_VALUE_LENGTH
    )
    child_attribute: str = Field(min_length=1, max_length=MAX_ATTRIBUTE_KEY_LENGTH)
    child_display_value: str = Field(
        min_length=1, max_length=MAX_ATTRIBUTE_VALUE_LENGTH
    )
    count: int = Field(ge=1)
    trusted_count: int = Field(ge=0)
    contributors: list[PatternContributor] = Field(max_length=MAX_PATTERN_CONTRIBUTORS)
    contributors_truncated: bool


class PatternSummary(_MemoryModel):
    """Bounded traces for an authorized local company user.

    Display values and evidence references are intentionally preserved for audit.
    Callers must not expose this tenant-scoped report to unauthorized users.
    """

    tenant_id: str = Field(min_length=1, max_length=128)
    category: str | None = Field(default=None, max_length=128)
    observation_count: int = Field(ge=0)
    history_truncated: bool
    patterns_truncated: bool
    policy_version: str = Field(min_length=1, max_length=64)
    policy_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    analyzer_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    minimum_support: int = Field(ge=1, le=1_000)
    minimum_trust_score: float = Field(ge=0.0, le=1.0)
    history_limit: int = Field(ge=1, le=MAX_HISTORY_LIMIT)
    attribute_patterns: list[AttributePattern] = Field(max_length=MAX_LIST_LIMIT)
    conditional_patterns: list[ConditionalPattern] = Field(max_length=MAX_LIST_LIMIT)


class PatternReason(_MemoryModel):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z0-9_]+$")
    detail: str = Field(min_length=1, max_length=1_000)


class CandidateAssessment(_MemoryModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    candidate_observation_id: str = Field(min_length=1, max_length=128)
    outcome: PatternOutcome
    summary: str = Field(min_length=1, max_length=1_000)
    history_observation_count: int = Field(ge=0)
    history_truncated: bool
    policy_version: str = Field(min_length=1, max_length=64)
    policy_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    analyzer_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    minimum_support: int = Field(ge=1, le=1_000)
    minimum_trust_score: float = Field(ge=0.0, le=1.0)
    history_limit: int = Field(ge=1, le=MAX_HISTORY_LIMIT)
    matching_patterns: list[AttributePattern] = Field(max_length=MAX_ATTRIBUTES)
    comparison_patterns: list[AttributePattern] = Field(max_length=MAX_ATTRIBUTES)
    reasons: list[PatternReason] = Field(max_length=MAX_ATTRIBUTES + 8)
    human_questions: list[str] = Field(max_length=MAX_ATTRIBUTES + 4)
    recommended_confirmation: list[str] = Field(min_length=1, max_length=8)
    candidate_stored: Literal[False] = False
    automatic_lookup_performed: Literal[False] = False
    action_executed: Literal[False] = False


@dataclass(slots=True)
class _CountBucket:
    count: int = 0
    trusted_count: int = 0
    displays: Counter[str] = field(default_factory=Counter)
    contributors: list[PatternContributor] = field(default_factory=list)
    trusted_evidence_keys: set[str] = field(default_factory=set)

    def add(
        self,
        display: str,
        trusted: bool,
        observation: CompanyObservation,
    ) -> None:
        self.count += 1
        self.displays[display] += 1
        reference = observation.evidence_reference or ""
        evidence_key = hashlib.sha256(
            f"{normalize_pattern_text(observation.source_type)}\0{reference}".encode()
        ).hexdigest()
        counted_as_trusted = trusted and evidence_key not in self.trusted_evidence_keys
        if counted_as_trusted:
            self.trusted_count += 1
            self.trusted_evidence_keys.add(evidence_key)
        if len(self.contributors) < MAX_PATTERN_CONTRIBUTORS:
            self.contributors.append(
                PatternContributor(
                    observation_id=observation.observation_id,
                    occurred_at=observation.occurred_at,
                    source_type=observation.source_type,
                    status=observation.status,
                    sensitivity=observation.sensitivity,
                    trust_score=observation.trust_score,
                    evidence_reference=observation.evidence_reference,
                    counted_as_trusted_support=counted_as_trusted,
                )
            )

    def preferred_display(self) -> str:
        return min(
            self.displays,
            key=lambda item: (-self.displays[item], item.casefold(), item),
        )


class PatternAnalyzer:
    """Compute local counts and advisory candidate assessments without side effects."""

    def __init__(
        self,
        store: MemoryStore,
        *,
        minimum_support: int = DEFAULT_MINIMUM_SUPPORT,
        minimum_trust_score: float = DEFAULT_MINIMUM_TRUST,
        history_limit: int = 1_000,
        policy: ActivePolicy = DEFAULT_POLICY,
    ) -> None:
        if isinstance(minimum_support, bool) or not 1 <= minimum_support <= 1_000:
            raise ValueError("minimum_support must be between 1 and 1000")
        if not 0.0 <= minimum_trust_score <= 1.0:
            raise ValueError("minimum_trust_score must be between 0 and 1")
        if (
            isinstance(history_limit, bool)
            or not 1 <= history_limit <= MAX_HISTORY_LIMIT
        ):
            raise ValueError(f"history_limit must be between 1 and {MAX_HISTORY_LIMIT}")
        self._store = store
        self.minimum_support = minimum_support
        self.minimum_trust_score = minimum_trust_score
        self.history_limit = history_limit
        self._policy = policy
        analyzer_payload = json.dumps(
            {
                "history_limit": history_limit,
                "minimum_support": minimum_support,
                "minimum_trust_score": minimum_trust_score,
                "policy_fingerprint": policy.policy_fingerprint,
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        self.analyzer_fingerprint = hashlib.sha256(
            analyzer_payload.encode("utf-8")
        ).hexdigest()

    def summarize_patterns(
        self,
        tenant_id: str,
        *,
        category: str | None = None,
    ) -> PatternSummary:
        observations, truncated = self._bounded_history(tenant_id, category)
        attribute_patterns = self._attribute_patterns(observations)
        conditional_patterns = self._conditional_patterns(observations)
        patterns_truncated = len(attribute_patterns) > MAX_LIST_LIMIT
        return PatternSummary(
            tenant_id=tenant_id,
            category=category,
            observation_count=len(observations),
            history_truncated=truncated,
            patterns_truncated=patterns_truncated,
            policy_version=self._policy.policy_version,
            policy_fingerprint=self._policy.policy_fingerprint,
            analyzer_fingerprint=self.analyzer_fingerprint,
            minimum_support=self.minimum_support,
            minimum_trust_score=self.minimum_trust_score,
            history_limit=self.history_limit,
            attribute_patterns=attribute_patterns[:MAX_LIST_LIMIT],
            conditional_patterns=conditional_patterns,
        )

    def assess_candidate(self, candidate: CompanyObservation) -> CandidateAssessment:
        """Assess a candidate against history; never persist or act on the candidate."""

        if not isinstance(candidate, CompanyObservation):
            raise TypeError("candidate must be a validated CompanyObservation")
        candidate = CompanyObservation.model_validate(candidate)
        observations, truncated = self._bounded_history(
            candidate.tenant_id,
            candidate.category,
            as_of=candidate.occurred_at,
        )
        attribute_patterns = self._attribute_patterns(observations)
        conditional_patterns = self._conditional_patterns(observations)
        patterns_by_attribute: dict[str, list[AttributePattern]] = {}
        for pattern in attribute_patterns:
            patterns_by_attribute.setdefault(
                normalize_attribute_key(pattern.attribute_key), []
            ).append(pattern)

        reasons: list[PatternReason] = []
        questions: list[str] = []
        matches: list[AttributePattern] = []
        comparisons: list[AttributePattern] = []

        def add_reason(code: str, detail: str) -> None:
            if code not in {item.code for item in reasons}:
                reasons.append(PatternReason(code=code, detail=detail))

        if truncated:
            add_reason(
                "HISTORY_TRUNCATED",
                "The bounded history window was full, so a reviewer should confirm the broader record.",
            )
        if not observations:
            add_reason(
                "NO_HISTORY",
                "No same-category history exists for this tenant.",
            )
        if candidate.status != EvidenceStatus.CONFIRMED:
            add_reason(
                "UNVERIFIED_CANDIDATE",
                "The candidate is not marked confirmed by its source workflow.",
            )
        source_policy = self._policy.sources.get(
            normalize_pattern_text(candidate.source_type)
        )
        if (
            source_policy is None
            or normalize_pattern_text(candidate.source_type) == "unknown"
        ):
            add_reason(
                "UNKNOWN_SOURCE",
                "The candidate source type is not configured as a trusted company source.",
            )
        elif source_policy.rank < self._policy.minimum_automatic_authority_rank:
            add_reason(
                "LOW_AUTHORITY_SOURCE",
                "The candidate source is below the configured automatic authority threshold.",
            )
        effective_candidate_trust = min(
            candidate.trust_score,
            source_policy.trust_cap if source_policy is not None else 0.0,
        )
        if effective_candidate_trust < max(
            self.minimum_trust_score,
            self._policy.minimum_automatic_trust,
        ):
            add_reason(
                "LOW_TRUST",
                "The candidate trust score is below the configured automatic threshold.",
            )
        if not candidate.evidence_reference:
            add_reason(
                "MISSING_EVIDENCE_REFERENCE",
                "The candidate has no exact evidence reference.",
            )
        if candidate.sensitivity == Sensitivity.SENSITIVE:
            add_reason(
                "SENSITIVE_CANDIDATE",
                "Sensitive candidates always require an authorized human review.",
            )

        candidate_attributes = _normalized_attributes(candidate)
        address_key = "address"
        suite_key = "suite"
        has_address_suite_pair = (
            address_key in candidate_attributes and suite_key in candidate_attributes
        )

        for key in sorted(candidate_attributes):
            if key == suite_key and has_address_suite_pair:
                continue
            _display_key, display_value, normalized_value = candidate_attributes[key]
            patterns = patterns_by_attribute.get(key, [])
            exact = next(
                (
                    pattern
                    for pattern in patterns
                    if pattern.normalized_value == normalized_value
                ),
                None,
            )
            if exact is not None and exact.trusted_count >= self.minimum_support:
                matches.append(exact)
                continue

            dominant = _dominant_attribute_pattern(patterns)
            if dominant is not None and dominant.trusted_count >= self.minimum_support:
                comparisons.append(dominant)
                add_reason(
                    f"{_reason_key(key)}_PATTERN_CONFLICT",
                    f"The candidate {key} differs from a strongly supported historical value.",
                )
                questions.append(
                    f"History most strongly supports {key} '{dominant.display_value}' "
                    f"({dominant.trusted_count} trusted observations), while the candidate "
                    f"says '{display_value}'. Which exact evidence confirms the change?"
                )
            else:
                if exact is not None:
                    comparisons.append(exact)
                add_reason(
                    f"{_reason_key(key)}_LOW_SUPPORT",
                    f"The candidate {key} has fewer than {self.minimum_support} trusted matching observations.",
                )

        if has_address_suite_pair:
            _address_label, address_display, address_value = candidate_attributes[
                address_key
            ]
            _suite_label, suite_display, suite_value = candidate_attributes[suite_key]
            relevant = [
                pattern
                for pattern in conditional_patterns
                if normalize_pattern_text(pattern.parent_display_value) == address_value
            ]
            exact_suite = next(
                (
                    pattern
                    for pattern in relevant
                    if normalize_pattern_text(pattern.child_display_value)
                    == suite_value
                ),
                None,
            )
            if (
                exact_suite is not None
                and exact_suite.trusted_count >= self.minimum_support
            ):
                matches.append(
                    _conditional_as_attribute_pattern(
                        exact_suite,
                        address_display=address_display,
                    )
                )
            else:
                dominant_suite = _dominant_conditional_pattern(relevant)
                if (
                    dominant_suite is not None
                    and dominant_suite.trusted_count >= self.minimum_support
                ):
                    comparisons.append(
                        _conditional_as_attribute_pattern(
                            dominant_suite,
                            address_display=address_display,
                        )
                    )
                    add_reason(
                        "NEW_SUITE_AT_KNOWN_ADDRESS",
                        "The suite differs from the strongly supported suite at the same address.",
                    )
                    questions.append(
                        f"At '{address_display}', history supports suite "
                        f"'{dominant_suite.child_display_value}' "
                        f"({dominant_suite.trusted_count} trusted observations), but this "
                        f"candidate says '{suite_display}'. Has the suite changed, and which "
                        "exact source confirms it?"
                    )
                else:
                    if exact_suite is not None:
                        comparisons.append(
                            _conditional_as_attribute_pattern(
                                exact_suite,
                                address_display=address_display,
                            )
                        )
                    add_reason(
                        "SUITE_AT_ADDRESS_LOW_SUPPORT",
                        f"The address-and-suite pair has fewer than {self.minimum_support} trusted matching observations.",
                    )

        questions = _stable_unique(questions, MAX_ATTRIBUTES + 4)
        outcome = PatternOutcome.REVIEW if reasons else PatternOutcome.ALLOW_LIKE
        if outcome == PatternOutcome.ALLOW_LIKE:
            summary = (
                "The candidate is consistent with sufficiently supported tenant history. "
                "This advisory result is not an execution approval."
            )
            recommendation = [
                "Retain the exact evidence reference and apply the company's configured decision policy before any action."
            ]
        else:
            summary = (
                "The candidate needs human review before it can be treated as confirmed; "
                "no lookup or action was performed."
            )
            recommendation = [
                "Check a stronger source permitted by the company's source policy.",
                "Record the exact evidence reference and ask a human to confirm the differing or weakly supported fields.",
            ]
            if not questions:
                questions = [
                    "Which exact, configured source confirms the candidate details?"
                ]

        return CandidateAssessment(
            tenant_id=candidate.tenant_id,
            candidate_observation_id=candidate.observation_id,
            outcome=outcome,
            summary=summary,
            history_observation_count=len(observations),
            history_truncated=truncated,
            policy_version=self._policy.policy_version,
            policy_fingerprint=self._policy.policy_fingerprint,
            analyzer_fingerprint=self.analyzer_fingerprint,
            minimum_support=self.minimum_support,
            minimum_trust_score=self.minimum_trust_score,
            history_limit=self.history_limit,
            matching_patterns=sorted(
                matches,
                key=lambda item: (item.attribute_key, item.normalized_value),
            ),
            comparison_patterns=sorted(
                comparisons,
                key=lambda item: (item.attribute_key, item.normalized_value),
            )[:MAX_ATTRIBUTES],
            reasons=reasons,
            human_questions=questions,
            recommended_confirmation=recommendation,
        )

    def _bounded_history(
        self,
        tenant_id: str,
        category: str | None,
        *,
        as_of: datetime | None = None,
    ) -> tuple[list[CompanyObservation], bool]:
        observations = self._store.list_observations(
            tenant_id,
            category=category,
            as_of=as_of,
            limit=self.history_limit + 1,
        )
        return observations[: self.history_limit], len(
            observations
        ) > self.history_limit

    def _is_trusted(self, observation: CompanyObservation) -> bool:
        source_policy = self._policy.sources.get(
            normalize_pattern_text(observation.source_type)
        )
        return (
            observation.status == EvidenceStatus.CONFIRMED
            and source_policy is not None
            and normalize_pattern_text(observation.source_type) != "unknown"
            and source_policy.rank >= self._policy.minimum_automatic_authority_rank
            and min(observation.trust_score, source_policy.trust_cap)
            >= max(
                self.minimum_trust_score,
                self._policy.minimum_automatic_trust,
            )
            and bool(observation.evidence_reference)
        )

    def _attribute_patterns(
        self, observations: list[CompanyObservation]
    ) -> list[AttributePattern]:
        buckets: dict[tuple[str, str], _CountBucket] = {}
        key_displays: dict[str, Counter[str]] = {}
        for observation in observations:
            trusted = self._is_trusted(observation)
            for key, (
                display_key,
                display_value,
                normalized_value,
            ) in _normalized_attributes(observation).items():
                key_displays.setdefault(key, Counter())[display_key] += 1
                buckets.setdefault((key, normalized_value), _CountBucket()).add(
                    display_value,
                    trusted,
                    observation,
                )

        patterns: list[AttributePattern] = []
        for (key, normalized_value), bucket in buckets.items():
            display_key = min(
                key_displays[key],
                key=lambda item: (
                    -key_displays[key][item],
                    item.casefold(),
                    item,
                ),
            )
            patterns.append(
                AttributePattern(
                    attribute_key=display_key,
                    display_value=bucket.preferred_display(),
                    normalized_value=normalized_value,
                    count=bucket.count,
                    trusted_count=bucket.trusted_count,
                    contributors=bucket.contributors,
                    contributors_truncated=bucket.count > len(bucket.contributors),
                )
            )
        return sorted(
            patterns,
            key=lambda item: (
                normalize_attribute_key(item.attribute_key),
                -item.count,
                item.normalized_value,
            ),
        )

    def _conditional_patterns(
        self, observations: list[CompanyObservation]
    ) -> list[ConditionalPattern]:
        buckets: dict[tuple[str, str], _CountBucket] = {}
        parent_displays: dict[str, Counter[str]] = {}
        for observation in observations:
            attributes = _normalized_attributes(observation)
            if "address" not in attributes or "suite" not in attributes:
                continue
            _address_key, address_display, address_value = attributes["address"]
            _suite_key, suite_display, suite_value = attributes["suite"]
            parent_displays.setdefault(address_value, Counter())[address_display] += 1
            buckets.setdefault((address_value, suite_value), _CountBucket()).add(
                suite_display,
                self._is_trusted(observation),
                observation,
            )

        patterns: list[ConditionalPattern] = []
        for (address_value, suite_value), bucket in buckets.items():
            address_display = min(
                parent_displays[address_value],
                key=lambda item: (
                    -parent_displays[address_value][item],
                    item.casefold(),
                    item,
                ),
            )
            patterns.append(
                ConditionalPattern(
                    parent_attribute="address",
                    parent_display_value=address_display,
                    child_attribute="suite",
                    child_display_value=bucket.preferred_display(),
                    count=bucket.count,
                    trusted_count=bucket.trusted_count,
                    contributors=bucket.contributors,
                    contributors_truncated=bucket.count > len(bucket.contributors),
                )
            )
        return sorted(
            patterns,
            key=lambda item: (
                normalize_pattern_text(item.parent_display_value),
                -item.count,
                normalize_pattern_text(item.child_display_value),
            ),
        )


def _normalized_attributes(
    observation: CompanyObservation,
) -> dict[str, tuple[str, str, str]]:
    attributes: dict[str, tuple[str, str, str]] = {}
    for key, value in observation.attributes.items():
        display_key = _display_text(key)
        display_value = _display_text(value)
        attributes[normalize_attribute_key(key)] = (
            display_key,
            display_value,
            normalize_pattern_text(value),
        )
    return attributes


def _dominant_attribute_pattern(
    patterns: list[AttributePattern],
) -> AttributePattern | None:
    if not patterns:
        return None
    return min(
        patterns,
        key=lambda item: (-item.trusted_count, -item.count, item.normalized_value),
    )


def _dominant_conditional_pattern(
    patterns: list[ConditionalPattern],
) -> ConditionalPattern | None:
    if not patterns:
        return None
    return min(
        patterns,
        key=lambda item: (
            -item.trusted_count,
            -item.count,
            normalize_pattern_text(item.child_display_value),
        ),
    )


def _conditional_as_attribute_pattern(
    pattern: ConditionalPattern,
    *,
    address_display: str,
) -> AttributePattern:
    return AttributePattern(
        attribute_key="suite at address",
        display_value=f"{address_display}, {pattern.child_display_value}",
        normalized_value=(
            f"{normalize_pattern_text(address_display)}|"
            f"{normalize_pattern_text(pattern.child_display_value)}"
        ),
        count=pattern.count,
        trusted_count=pattern.trusted_count,
        contributors=pattern.contributors,
        contributors_truncated=pattern.contributors_truncated,
    )


def _reason_key(attribute_key: str) -> str:
    result = re.sub(r"[^A-Z0-9]+", "_", attribute_key.upper()).strip("_")
    return (result or "ATTRIBUTE")[:40]


def _stable_unique(values: list[str], limit: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
        if len(result) == limit:
            break
    return result
