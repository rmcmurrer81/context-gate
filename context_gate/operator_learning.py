"""Tenant-scoped, advisory operator guidance with reversible local persistence.

This module only stores and retrieves human-authored guidance. It is deliberately
disconnected from ContextGate enforcement, performs no network calls, and cannot
approve or execute an action. Retractions are append-only tombstones: they remove
guidance from active retrieval without deleting or rewriting the original record.
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
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from context_gate.models import EnforcementDecision

MAX_IDENTIFIER_LENGTH = 128
MAX_GUIDANCE_LENGTH = 4_000
MAX_REASON_LENGTH = 2_000
MAX_CASE_IDS = 32
MAX_LIST_LIMIT = 5_000
MAX_RETRIEVAL_LIMIT = 100
MAX_QUERY_LENGTH = 2_000
MAX_DB_PATH_LENGTH = 4_096
MAX_MATCHED_TOKENS = 128

Fingerprint = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]

Identifier = Annotated[str, Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)]
GuidanceText = Annotated[str, Field(min_length=3, max_length=MAX_GUIDANCE_LENGTH)]
ReasonText = Annotated[str, Field(min_length=3, max_length=MAX_REASON_LENGTH)]

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
_FINGERPRINT_PATTERN = re.compile(r"[a-f0-9]{64}")
_STOP_TOKENS = frozenset(
    {
        "and",
        "are",
        "but",
        "for",
        "from",
        "has",
        "have",
        "into",
        "not",
        "that",
        "the",
        "their",
        "then",
        "this",
        "was",
        "were",
        "when",
        "with",
    }
)


class _LearningModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        revalidate_instances="always",
        str_strip_whitespace=True,
        use_enum_values=False,
        validate_default=True,
    )


def _utc_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include an explicit timezone")
    return value.astimezone(UTC)


def _contains_control_characters(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)


def _normalized_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _validate_display_text(value: str) -> str:
    if _contains_control_characters(value):
        raise ValueError("text must not contain control characters")
    return value


def _validate_query_text(
    field_name: str,
    value: str,
    maximum_length: int,
) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum_length
        or _contains_control_characters(value)
    ):
        raise ValueError(f"{field_name} is invalid")
    return value.strip()


def _validate_limit(limit: int, maximum: int) -> None:
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= maximum
    ):
        raise ValueError(f"limit must be between 1 and {maximum}")


def _validate_fingerprint(field_name: str, value: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} is invalid")
    return value


def _tokens(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return {
        token
        for token in _TOKEN_PATTERN.findall(normalized)
        if len(token) >= 3 and token not in _STOP_TOKENS
    }


class GuidanceOrigin(StrEnum):
    """Supported provenance types for operator-authored guidance."""

    REVIEW = "review"
    CHAT = "chat"


class OperatorGuidance(_LearningModel):
    """An immutable guidance statement derived from a review or chat record."""

    tenant_id: Identifier
    guidance_id: Identifier
    origin: GuidanceOrigin
    source_record_id: Identifier
    created_at: datetime
    guidance: GuidanceText
    case_ids: list[Identifier] = Field(default_factory=list, max_length=MAX_CASE_IDS)

    _normalize_created_at = field_validator("created_at", mode="after")(_utc_timestamp)

    @field_validator(
        "tenant_id",
        "guidance_id",
        "source_record_id",
        "guidance",
    )
    @classmethod
    def text_is_display_safe(cls, value: str) -> str:
        return _validate_display_text(value)

    @field_validator("case_ids")
    @classmethod
    def case_ids_are_safe_and_unique(cls, values: list[str]) -> list[str]:
        normalized: set[str] = set()
        for value in values:
            _validate_display_text(value)
            key = _normalized_text(value)
            if key in normalized:
                raise ValueError("case IDs must be unique after normalization")
            normalized.add(key)
        return values


class GuidanceRetraction(_LearningModel):
    """An immutable tombstone that removes guidance from active retrieval."""

    tenant_id: Identifier
    retraction_id: Identifier
    guidance_id: Identifier
    retracted_at: datetime
    actor: Identifier
    reason: ReasonText

    _normalize_retracted_at = field_validator("retracted_at", mode="after")(
        _utc_timestamp
    )

    @field_validator(
        "tenant_id",
        "retraction_id",
        "guidance_id",
        "actor",
        "reason",
    )
    @classmethod
    def text_is_display_safe(cls, value: str) -> str:
        return _validate_display_text(value)


class GuidanceMatch(_LearningModel):
    """A deterministic explanation of why active guidance was retrieved."""

    guidance: OperatorGuidance
    score: int = Field(ge=1)
    matched_case_ids: list[Identifier] = Field(
        default_factory=list,
        max_length=MAX_CASE_IDS,
    )
    matched_tokens: list[str] = Field(
        default_factory=list,
        max_length=MAX_MATCHED_TOKENS,
    )


class DecisionCorrection(_LearningModel):
    """An immutable human resolution linked to an unchanged decision receipt."""

    tenant_id: Identifier
    correction_id: Identifier
    case_id: Identifier
    original_decision_id: Identifier
    request_fingerprint: Fingerprint
    evidence_fingerprint: Fingerprint
    policy_fingerprint: Fingerprint
    original_outcome: EnforcementDecision
    corrected_outcome: EnforcementDecision
    created_at: datetime
    reviewer: Identifier
    rationale: ReasonText
    resolution_status: Literal["RESOLVED"] = "RESOLVED"
    action_executed: Literal[False] = False

    _normalize_created_at = field_validator("created_at", mode="after")(_utc_timestamp)

    @field_validator(
        "tenant_id",
        "correction_id",
        "case_id",
        "original_decision_id",
        "reviewer",
        "rationale",
    )
    @classmethod
    def text_is_display_safe(cls, value: str) -> str:
        return _validate_display_text(value)

    @model_validator(mode="after")
    def outcome_is_a_real_human_correction(self) -> Self:
        if self.corrected_outcome == self.original_outcome:
            raise ValueError("corrected outcome must differ from original outcome")
        return self

    @property
    def effective_outcome(self) -> EnforcementDecision:
        """Return the human-corrected advisory outcome without executing it."""

        return self.corrected_outcome


class DecisionCorrectionRetraction(_LearningModel):
    """An immutable tombstone for one human decision correction."""

    tenant_id: Identifier
    retraction_id: Identifier
    correction_id: Identifier
    retracted_at: datetime
    actor: Identifier
    reason: ReasonText

    _normalize_retracted_at = field_validator("retracted_at", mode="after")(
        _utc_timestamp
    )

    @field_validator(
        "tenant_id",
        "retraction_id",
        "correction_id",
        "actor",
        "reason",
    )
    @classmethod
    def text_is_display_safe(cls, value: str) -> str:
        return _validate_display_text(value)


class OperatorLearningStoreError(RuntimeError):
    """Base class for sanitized operator-learning persistence errors."""


class OperatorLearningCollisionError(OperatorLearningStoreError):
    """Raised when an immutable record ID is reused with different content."""


class OperatorLearningStore:
    """Append-only SQLite persistence for tenant-scoped advisory guidance."""

    _EXPECTED_GUIDANCE_COLUMNS: ClassVar[set[str]] = {
        "tenant_id",
        "guidance_id",
        "origin",
        "created_at",
        "payload_digest",
        "payload_json",
    }
    _EXPECTED_RETRACTION_COLUMNS: ClassVar[set[str]] = {
        "tenant_id",
        "retraction_id",
        "guidance_id",
        "retracted_at",
        "payload_digest",
        "payload_json",
    }
    _EXPECTED_CORRECTION_COLUMNS: ClassVar[set[str]] = {
        "tenant_id",
        "correction_id",
        "case_key",
        "request_fingerprint",
        "evidence_fingerprint",
        "policy_fingerprint",
        "created_at",
        "payload_digest",
        "payload_json",
    }
    _EXPECTED_CORRECTION_RETRACTION_COLUMNS: ClassVar[set[str]] = {
        "tenant_id",
        "retraction_id",
        "correction_id",
        "retracted_at",
        "payload_digest",
        "payload_json",
    }

    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        raw_path = os.fspath(db_path)
        if raw_path == ":memory:":
            connection_target = raw_path
        else:
            if not raw_path or len(raw_path) > MAX_DB_PATH_LENGTH or "\x00" in raw_path:
                raise OperatorLearningStoreError(
                    "Operator learning database path is invalid."
                )
            path = Path(raw_path).expanduser()
            if not path.parent.exists() or not path.parent.is_dir():
                raise OperatorLearningStoreError(
                    "Operator learning database parent directory does not exist."
                )
            connection_target = os.fspath(path.absolute())

        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                connection_target,
                timeout=5.0,
                isolation_level=None,
                check_same_thread=False,
                uri=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA temp_store = MEMORY")
        except (OSError, sqlite3.Error):
            if connection is not None:
                connection.close()
            raise OperatorLearningStoreError(
                "Operator learning database could not be opened safely."
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
        try:
            with self._lock:
                self._connection.execute("BEGIN IMMEDIATE")
                self._connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS operator_guidance (
                        tenant_id TEXT NOT NULL,
                        guidance_id TEXT NOT NULL,
                        origin TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        payload_digest TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        PRIMARY KEY (tenant_id, guidance_id)
                    ) WITHOUT ROWID
                    """
                )
                guidance_columns = {
                    row["name"]
                    for row in self._connection.execute(
                        "PRAGMA table_info(operator_guidance)"
                    ).fetchall()
                }
                if guidance_columns != self._EXPECTED_GUIDANCE_COLUMNS:
                    raise OperatorLearningStoreError(
                        "Operator learning database schema is incompatible."
                    )
                self._connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS operator_guidance_retractions (
                        tenant_id TEXT NOT NULL,
                        retraction_id TEXT NOT NULL,
                        guidance_id TEXT NOT NULL,
                        retracted_at TEXT NOT NULL,
                        payload_digest TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        PRIMARY KEY (tenant_id, retraction_id),
                        FOREIGN KEY (tenant_id, guidance_id)
                            REFERENCES operator_guidance (tenant_id, guidance_id)
                            ON UPDATE RESTRICT ON DELETE RESTRICT
                    ) WITHOUT ROWID
                    """
                )
                retraction_columns = {
                    row["name"]
                    for row in self._connection.execute(
                        "PRAGMA table_info(operator_guidance_retractions)"
                    ).fetchall()
                }
                if retraction_columns != self._EXPECTED_RETRACTION_COLUMNS:
                    raise OperatorLearningStoreError(
                        "Operator learning retraction schema is incompatible."
                    )
                self._connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS operator_decision_corrections (
                        tenant_id TEXT NOT NULL,
                        correction_id TEXT NOT NULL,
                        case_key TEXT NOT NULL,
                        request_fingerprint TEXT NOT NULL,
                        evidence_fingerprint TEXT NOT NULL,
                        policy_fingerprint TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        payload_digest TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        PRIMARY KEY (tenant_id, correction_id)
                    ) WITHOUT ROWID
                    """
                )
                correction_columns = {
                    row["name"]
                    for row in self._connection.execute(
                        "PRAGMA table_info(operator_decision_corrections)"
                    ).fetchall()
                }
                if correction_columns != self._EXPECTED_CORRECTION_COLUMNS:
                    raise OperatorLearningStoreError(
                        "Operator decision correction schema is incompatible."
                    )
                self._connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS operator_decision_correction_retractions (
                        tenant_id TEXT NOT NULL,
                        retraction_id TEXT NOT NULL,
                        correction_id TEXT NOT NULL,
                        retracted_at TEXT NOT NULL,
                        payload_digest TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        PRIMARY KEY (tenant_id, retraction_id),
                        FOREIGN KEY (tenant_id, correction_id)
                            REFERENCES operator_decision_corrections
                                (tenant_id, correction_id)
                            ON UPDATE RESTRICT ON DELETE RESTRICT
                    ) WITHOUT ROWID
                    """
                )
                correction_retraction_columns = {
                    row["name"]
                    for row in self._connection.execute(
                        "PRAGMA table_info(operator_decision_correction_retractions)"
                    ).fetchall()
                }
                if (
                    correction_retraction_columns
                    != self._EXPECTED_CORRECTION_RETRACTION_COLUMNS
                ):
                    raise OperatorLearningStoreError(
                        "Operator decision correction retraction schema is incompatible."
                    )
                self._connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS operator_guidance_tenant_time
                    ON operator_guidance (tenant_id, created_at DESC, guidance_id)
                    """
                )
                self._connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS operator_retractions_tenant_guidance
                    ON operator_guidance_retractions
                    (tenant_id, guidance_id, retracted_at DESC, retraction_id)
                    """
                )
                self._connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS operator_corrections_context_time
                    ON operator_decision_corrections (
                        tenant_id, case_key, request_fingerprint,
                        evidence_fingerprint, policy_fingerprint,
                        created_at DESC, correction_id
                    )
                    """
                )
                self._connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS operator_correction_retractions_target
                    ON operator_decision_correction_retractions
                    (tenant_id, correction_id, retracted_at DESC, retraction_id)
                    """
                )
                self._connection.execute("COMMIT")
        except OperatorLearningStoreError:
            self._rollback_quietly()
            raise
        except sqlite3.Error:
            self._rollback_quietly()
            raise OperatorLearningStoreError(
                "Operator learning database could not be initialized safely."
            ) from None

    def _rollback_quietly(self) -> None:
        try:
            self._connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def _ensure_open(self) -> None:
        if self._closed:
            raise OperatorLearningStoreError("Operator learning database is closed.")

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
        record: (
            OperatorGuidance
            | GuidanceRetraction
            | DecisionCorrection
            | DecisionCorrectionRetraction
        ),
    ) -> tuple[str, str]:
        serialized = json.dumps(
            record.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        return serialized, digest

    def _verified_guidance_row(self, row: sqlite3.Row) -> OperatorGuidance:
        try:
            guidance = OperatorGuidance.model_validate(json.loads(row["payload_json"]))
            _serialized, computed_digest = self._canonical_payload(guidance)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            raise OperatorLearningStoreError(
                "Operator guidance integrity check failed."
            ) from None
        columns_match = (
            guidance.tenant_id == row["tenant_id"]
            and guidance.guidance_id == row["guidance_id"]
            and guidance.origin.value == row["origin"]
            and guidance.created_at.isoformat() == row["created_at"]
        )
        if not columns_match or not hmac.compare_digest(
            computed_digest,
            row["payload_digest"],
        ):
            raise OperatorLearningStoreError(
                "Operator guidance integrity check failed."
            )
        return guidance

    def _verified_retraction_row(self, row: sqlite3.Row) -> GuidanceRetraction:
        try:
            retraction = GuidanceRetraction.model_validate(
                json.loads(row["payload_json"])
            )
            _serialized, computed_digest = self._canonical_payload(retraction)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            raise OperatorLearningStoreError(
                "Operator guidance retraction integrity check failed."
            ) from None
        columns_match = (
            retraction.tenant_id == row["tenant_id"]
            and retraction.retraction_id == row["retraction_id"]
            and retraction.guidance_id == row["guidance_id"]
            and retraction.retracted_at.isoformat() == row["retracted_at"]
        )
        if not columns_match or not hmac.compare_digest(
            computed_digest,
            row["payload_digest"],
        ):
            raise OperatorLearningStoreError(
                "Operator guidance retraction integrity check failed."
            )
        return retraction

    def _verified_correction_row(self, row: sqlite3.Row) -> DecisionCorrection:
        try:
            correction = DecisionCorrection.model_validate(
                json.loads(row["payload_json"])
            )
            _serialized, computed_digest = self._canonical_payload(correction)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            raise OperatorLearningStoreError(
                "Operator decision correction integrity check failed."
            ) from None
        columns_match = (
            correction.tenant_id == row["tenant_id"]
            and correction.correction_id == row["correction_id"]
            and _normalized_text(correction.case_id) == row["case_key"]
            and correction.request_fingerprint == row["request_fingerprint"]
            and correction.evidence_fingerprint == row["evidence_fingerprint"]
            and correction.policy_fingerprint == row["policy_fingerprint"]
            and correction.created_at.isoformat() == row["created_at"]
        )
        if not columns_match or not hmac.compare_digest(
            computed_digest,
            row["payload_digest"],
        ):
            raise OperatorLearningStoreError(
                "Operator decision correction integrity check failed."
            )
        return correction

    def _verified_correction_retraction_row(
        self,
        row: sqlite3.Row,
    ) -> DecisionCorrectionRetraction:
        try:
            retraction = DecisionCorrectionRetraction.model_validate(
                json.loads(row["payload_json"])
            )
            _serialized, computed_digest = self._canonical_payload(retraction)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            raise OperatorLearningStoreError(
                "Operator decision correction retraction integrity check failed."
            ) from None
        columns_match = (
            retraction.tenant_id == row["tenant_id"]
            and retraction.retraction_id == row["retraction_id"]
            and retraction.correction_id == row["correction_id"]
            and retraction.retracted_at.isoformat() == row["retracted_at"]
        )
        if not columns_match or not hmac.compare_digest(
            computed_digest,
            row["payload_digest"],
        ):
            raise OperatorLearningStoreError(
                "Operator decision correction retraction integrity check failed."
            )
        return retraction

    def append_guidance(
        self,
        guidance: OperatorGuidance,
    ) -> Literal["inserted", "unchanged"]:
        """Append guidance once and reject any later mutation of its identity."""

        if not isinstance(guidance, OperatorGuidance):
            raise TypeError("guidance must be a validated OperatorGuidance")
        guidance = OperatorGuidance.model_validate(guidance)
        payload_json, payload_digest = self._canonical_payload(guidance)
        with self._lock:
            self._ensure_open()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                existing = self._connection.execute(
                    """
                    SELECT tenant_id, guidance_id, origin, created_at,
                           payload_digest, payload_json
                    FROM operator_guidance
                    WHERE tenant_id = ? AND guidance_id = ?
                    """,
                    (guidance.tenant_id, guidance.guidance_id),
                ).fetchone()
                if existing is not None:
                    stored = self._verified_guidance_row(existing)
                    _stored_json, stored_digest = self._canonical_payload(stored)
                    if not hmac.compare_digest(stored_digest, payload_digest):
                        raise OperatorLearningCollisionError(
                            "Guidance identity collision: stored content differs."
                        )
                    self._connection.execute("COMMIT")
                    return "unchanged"

                self._connection.execute(
                    """
                    INSERT INTO operator_guidance (
                        tenant_id, guidance_id, origin, created_at,
                        payload_digest, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        guidance.tenant_id,
                        guidance.guidance_id,
                        guidance.origin.value,
                        guidance.created_at.isoformat(),
                        payload_digest,
                        payload_json,
                    ),
                )
                self._connection.execute("COMMIT")
                return "inserted"
            except OperatorLearningStoreError:
                self._rollback_quietly()
                raise
            except sqlite3.Error:
                self._rollback_quietly()
                raise OperatorLearningStoreError(
                    "Operator guidance could not be stored."
                ) from None

    def append_retraction(
        self,
        retraction: GuidanceRetraction,
    ) -> Literal["inserted", "unchanged"]:
        """Append an immutable tombstone while preserving the original guidance."""

        if not isinstance(retraction, GuidanceRetraction):
            raise TypeError("retraction must be a validated GuidanceRetraction")
        retraction = GuidanceRetraction.model_validate(retraction)
        payload_json, payload_digest = self._canonical_payload(retraction)
        with self._lock:
            self._ensure_open()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                target = self._connection.execute(
                    """
                    SELECT tenant_id, guidance_id, origin, created_at,
                           payload_digest, payload_json
                    FROM operator_guidance
                    WHERE tenant_id = ? AND guidance_id = ?
                    """,
                    (retraction.tenant_id, retraction.guidance_id),
                ).fetchone()
                if target is None:
                    raise OperatorLearningStoreError(
                        "Retraction target is unavailable for this tenant."
                    )
                self._verified_guidance_row(target)

                existing = self._connection.execute(
                    """
                    SELECT tenant_id, retraction_id, guidance_id, retracted_at,
                           payload_digest, payload_json
                    FROM operator_guidance_retractions
                    WHERE tenant_id = ? AND retraction_id = ?
                    """,
                    (retraction.tenant_id, retraction.retraction_id),
                ).fetchone()
                if existing is not None:
                    stored = self._verified_retraction_row(existing)
                    _stored_json, stored_digest = self._canonical_payload(stored)
                    if not hmac.compare_digest(stored_digest, payload_digest):
                        raise OperatorLearningCollisionError(
                            "Retraction identity collision: stored content differs."
                        )
                    self._connection.execute("COMMIT")
                    return "unchanged"

                self._connection.execute(
                    """
                    INSERT INTO operator_guidance_retractions (
                        tenant_id, retraction_id, guidance_id, retracted_at,
                        payload_digest, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        retraction.tenant_id,
                        retraction.retraction_id,
                        retraction.guidance_id,
                        retraction.retracted_at.isoformat(),
                        payload_digest,
                        payload_json,
                    ),
                )
                self._connection.execute("COMMIT")
                return "inserted"
            except OperatorLearningStoreError:
                self._rollback_quietly()
                raise
            except sqlite3.Error:
                self._rollback_quietly()
                raise OperatorLearningStoreError(
                    "Operator guidance retraction could not be stored."
                ) from None

    def append_decision_correction(
        self,
        correction: DecisionCorrection,
    ) -> Literal["inserted", "unchanged"]:
        """Append a human resolution without mutating its original receipt."""

        if not isinstance(correction, DecisionCorrection):
            raise TypeError("correction must be a validated DecisionCorrection")
        correction = DecisionCorrection.model_validate(correction)
        payload_json, payload_digest = self._canonical_payload(correction)
        with self._lock:
            self._ensure_open()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                existing = self._connection.execute(
                    """
                    SELECT tenant_id, correction_id, case_key,
                           request_fingerprint, evidence_fingerprint,
                           policy_fingerprint, created_at,
                           payload_digest, payload_json
                    FROM operator_decision_corrections
                    WHERE tenant_id = ? AND correction_id = ?
                    """,
                    (correction.tenant_id, correction.correction_id),
                ).fetchone()
                if existing is not None:
                    stored = self._verified_correction_row(existing)
                    _stored_json, stored_digest = self._canonical_payload(stored)
                    if not hmac.compare_digest(stored_digest, payload_digest):
                        raise OperatorLearningCollisionError(
                            "Decision correction identity collision: stored content differs."
                        )
                    self._connection.execute("COMMIT")
                    return "unchanged"

                self._connection.execute(
                    """
                    INSERT INTO operator_decision_corrections (
                        tenant_id, correction_id, case_key,
                        request_fingerprint, evidence_fingerprint,
                        policy_fingerprint, created_at,
                        payload_digest, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        correction.tenant_id,
                        correction.correction_id,
                        _normalized_text(correction.case_id),
                        correction.request_fingerprint,
                        correction.evidence_fingerprint,
                        correction.policy_fingerprint,
                        correction.created_at.isoformat(),
                        payload_digest,
                        payload_json,
                    ),
                )
                self._connection.execute("COMMIT")
                return "inserted"
            except OperatorLearningStoreError:
                self._rollback_quietly()
                raise
            except sqlite3.Error:
                self._rollback_quietly()
                raise OperatorLearningStoreError(
                    "Operator decision correction could not be stored."
                ) from None

    def append_decision_correction_retraction(
        self,
        retraction: DecisionCorrectionRetraction,
    ) -> Literal["inserted", "unchanged"]:
        """Append a tombstone while preserving the decision correction."""

        if not isinstance(retraction, DecisionCorrectionRetraction):
            raise TypeError(
                "retraction must be a validated DecisionCorrectionRetraction"
            )
        retraction = DecisionCorrectionRetraction.model_validate(retraction)
        payload_json, payload_digest = self._canonical_payload(retraction)
        with self._lock:
            self._ensure_open()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                target = self._connection.execute(
                    """
                    SELECT tenant_id, correction_id, case_key,
                           request_fingerprint, evidence_fingerprint,
                           policy_fingerprint, created_at,
                           payload_digest, payload_json
                    FROM operator_decision_corrections
                    WHERE tenant_id = ? AND correction_id = ?
                    """,
                    (retraction.tenant_id, retraction.correction_id),
                ).fetchone()
                if target is None:
                    raise OperatorLearningStoreError(
                        "Decision correction target is unavailable for this tenant."
                    )
                self._verified_correction_row(target)

                existing = self._connection.execute(
                    """
                    SELECT tenant_id, retraction_id, correction_id, retracted_at,
                           payload_digest, payload_json
                    FROM operator_decision_correction_retractions
                    WHERE tenant_id = ? AND retraction_id = ?
                    """,
                    (retraction.tenant_id, retraction.retraction_id),
                ).fetchone()
                if existing is not None:
                    stored = self._verified_correction_retraction_row(existing)
                    _stored_json, stored_digest = self._canonical_payload(stored)
                    if not hmac.compare_digest(stored_digest, payload_digest):
                        raise OperatorLearningCollisionError(
                            "Decision correction retraction identity collision: "
                            "stored content differs."
                        )
                    self._connection.execute("COMMIT")
                    return "unchanged"

                self._connection.execute(
                    """
                    INSERT INTO operator_decision_correction_retractions (
                        tenant_id, retraction_id, correction_id, retracted_at,
                        payload_digest, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        retraction.tenant_id,
                        retraction.retraction_id,
                        retraction.correction_id,
                        retraction.retracted_at.isoformat(),
                        payload_digest,
                        payload_json,
                    ),
                )
                self._connection.execute("COMMIT")
                return "inserted"
            except OperatorLearningStoreError:
                self._rollback_quietly()
                raise
            except sqlite3.Error:
                self._rollback_quietly()
                raise OperatorLearningStoreError(
                    "Operator decision correction retraction could not be stored."
                ) from None

    def list_active_guidance(
        self,
        tenant_id: str,
        *,
        limit: int = 100,
    ) -> list[OperatorGuidance]:
        """List one tenant's non-retracted guidance in stable newest-first order."""

        tenant_id = _validate_query_text(
            "tenant_id",
            tenant_id,
            MAX_IDENTIFIER_LENGTH,
        )
        _validate_limit(limit, MAX_LIST_LIMIT)
        with self._lock:
            self._ensure_open()
            try:
                rows = self._connection.execute(
                    """
                    SELECT g.tenant_id, g.guidance_id, g.origin, g.created_at,
                           g.payload_digest, g.payload_json
                    FROM operator_guidance AS g
                    WHERE g.tenant_id = ?
                      AND NOT EXISTS (
                          SELECT 1
                          FROM operator_guidance_retractions AS r
                          WHERE r.tenant_id = g.tenant_id
                            AND r.guidance_id = g.guidance_id
                      )
                    ORDER BY g.created_at DESC, g.guidance_id ASC
                    LIMIT ?
                    """,
                    (tenant_id, limit),
                ).fetchall()
                guidance = [self._verified_guidance_row(row) for row in rows]
            except OperatorLearningStoreError:
                raise
            except (json.JSONDecodeError, sqlite3.Error, ValueError):
                raise OperatorLearningStoreError(
                    "Active operator guidance could not be read safely."
                ) from None
        if any(item.tenant_id != tenant_id for item in guidance):
            raise OperatorLearningStoreError(
                "Operator guidance tenant isolation check failed."
            )
        return guidance

    def list_retractions(
        self,
        tenant_id: str,
        *,
        guidance_id: str | None = None,
        limit: int = 100,
    ) -> list[GuidanceRetraction]:
        """List one tenant's immutable tombstones in stable newest-first order."""

        tenant_id = _validate_query_text(
            "tenant_id",
            tenant_id,
            MAX_IDENTIFIER_LENGTH,
        )
        if guidance_id is not None:
            guidance_id = _validate_query_text(
                "guidance_id",
                guidance_id,
                MAX_IDENTIFIER_LENGTH,
            )
        _validate_limit(limit, MAX_LIST_LIMIT)
        parameters: list[str | int] = [tenant_id]
        guidance_clause = ""
        if guidance_id is not None:
            guidance_clause = " AND guidance_id = ?"
            parameters.append(guidance_id)
        parameters.append(limit)
        query = f"""
            SELECT tenant_id, retraction_id, guidance_id, retracted_at,
                   payload_digest, payload_json
            FROM operator_guidance_retractions
            WHERE tenant_id = ?{guidance_clause}
            ORDER BY retracted_at DESC, retraction_id ASC
            LIMIT ?
        """
        with self._lock:
            self._ensure_open()
            try:
                rows = self._connection.execute(query, parameters).fetchall()
                retractions = [self._verified_retraction_row(row) for row in rows]
            except OperatorLearningStoreError:
                raise
            except (json.JSONDecodeError, sqlite3.Error, ValueError):
                raise OperatorLearningStoreError(
                    "Operator guidance retractions could not be read safely."
                ) from None
        if any(item.tenant_id != tenant_id for item in retractions):
            raise OperatorLearningStoreError(
                "Operator guidance tenant isolation check failed."
            )
        return retractions

    def list_active_decision_corrections(
        self,
        tenant_id: str,
        *,
        case_id: str | None = None,
        limit: int = 100,
    ) -> list[DecisionCorrection]:
        """List active human resolutions without changing decision receipts."""

        tenant_id = _validate_query_text(
            "tenant_id",
            tenant_id,
            MAX_IDENTIFIER_LENGTH,
        )
        normalized_case: str | None = None
        if case_id is not None:
            case_id = _validate_query_text(
                "case_id",
                case_id,
                MAX_IDENTIFIER_LENGTH,
            )
            normalized_case = _normalized_text(case_id)
        _validate_limit(limit, MAX_LIST_LIMIT)
        parameters: list[str | int] = [tenant_id]
        case_clause = ""
        if normalized_case is not None:
            case_clause = " AND c.case_key = ?"
            parameters.append(normalized_case)
        parameters.append(limit)
        query = f"""
            SELECT c.tenant_id, c.correction_id, c.case_key,
                   c.request_fingerprint, c.evidence_fingerprint,
                   c.policy_fingerprint, c.created_at,
                   c.payload_digest, c.payload_json
            FROM operator_decision_corrections AS c
            WHERE c.tenant_id = ?{case_clause}
              AND NOT EXISTS (
                  SELECT 1
                  FROM operator_decision_correction_retractions AS r
                  WHERE r.tenant_id = c.tenant_id
                    AND r.correction_id = c.correction_id
              )
            ORDER BY c.created_at DESC, c.correction_id ASC
            LIMIT ?
        """
        with self._lock:
            self._ensure_open()
            try:
                rows = self._connection.execute(query, parameters).fetchall()
                corrections = [self._verified_correction_row(row) for row in rows]
            except OperatorLearningStoreError:
                raise
            except (json.JSONDecodeError, sqlite3.Error, ValueError):
                raise OperatorLearningStoreError(
                    "Active operator decision corrections could not be read safely."
                ) from None
        if any(item.tenant_id != tenant_id for item in corrections):
            raise OperatorLearningStoreError(
                "Operator decision correction tenant isolation check failed."
            )
        return corrections

    def latest_active_decision_correction(
        self,
        tenant_id: str,
        *,
        case_id: str,
        request_fingerprint: str,
        evidence_fingerprint: str,
        policy_fingerprint: str,
    ) -> DecisionCorrection | None:
        """Return the newest active correction for one exact decision context."""

        tenant_id = _validate_query_text(
            "tenant_id",
            tenant_id,
            MAX_IDENTIFIER_LENGTH,
        )
        case_id = _validate_query_text("case_id", case_id, MAX_IDENTIFIER_LENGTH)
        request_fingerprint = _validate_fingerprint(
            "request_fingerprint",
            request_fingerprint,
        )
        evidence_fingerprint = _validate_fingerprint(
            "evidence_fingerprint",
            evidence_fingerprint,
        )
        policy_fingerprint = _validate_fingerprint(
            "policy_fingerprint",
            policy_fingerprint,
        )
        with self._lock:
            self._ensure_open()
            try:
                row = self._connection.execute(
                    """
                    SELECT c.tenant_id, c.correction_id, c.case_key,
                           c.request_fingerprint, c.evidence_fingerprint,
                           c.policy_fingerprint, c.created_at,
                           c.payload_digest, c.payload_json
                    FROM operator_decision_corrections AS c
                    WHERE c.tenant_id = ?
                      AND c.case_key = ?
                      AND c.request_fingerprint = ?
                      AND c.evidence_fingerprint = ?
                      AND c.policy_fingerprint = ?
                      AND NOT EXISTS (
                          SELECT 1
                          FROM operator_decision_correction_retractions AS r
                          WHERE r.tenant_id = c.tenant_id
                            AND r.correction_id = c.correction_id
                      )
                    ORDER BY c.created_at DESC, c.correction_id ASC
                    LIMIT 1
                    """,
                    (
                        tenant_id,
                        _normalized_text(case_id),
                        request_fingerprint,
                        evidence_fingerprint,
                        policy_fingerprint,
                    ),
                ).fetchone()
                correction = (
                    self._verified_correction_row(row) if row is not None else None
                )
            except OperatorLearningStoreError:
                raise
            except (json.JSONDecodeError, sqlite3.Error, ValueError):
                raise OperatorLearningStoreError(
                    "Latest operator decision correction could not be read safely."
                ) from None
        if correction is not None and correction.tenant_id != tenant_id:
            raise OperatorLearningStoreError(
                "Operator decision correction tenant isolation check failed."
            )
        return correction

    def list_decision_correction_retractions(
        self,
        tenant_id: str,
        *,
        correction_id: str | None = None,
        limit: int = 100,
    ) -> list[DecisionCorrectionRetraction]:
        """List immutable correction tombstones for one tenant."""

        tenant_id = _validate_query_text(
            "tenant_id",
            tenant_id,
            MAX_IDENTIFIER_LENGTH,
        )
        if correction_id is not None:
            correction_id = _validate_query_text(
                "correction_id",
                correction_id,
                MAX_IDENTIFIER_LENGTH,
            )
        _validate_limit(limit, MAX_LIST_LIMIT)
        parameters: list[str | int] = [tenant_id]
        correction_clause = ""
        if correction_id is not None:
            correction_clause = " AND correction_id = ?"
            parameters.append(correction_id)
        parameters.append(limit)
        query = f"""
            SELECT tenant_id, retraction_id, correction_id, retracted_at,
                   payload_digest, payload_json
            FROM operator_decision_correction_retractions
            WHERE tenant_id = ?{correction_clause}
            ORDER BY retracted_at DESC, retraction_id ASC
            LIMIT ?
        """
        with self._lock:
            self._ensure_open()
            try:
                rows = self._connection.execute(query, parameters).fetchall()
                retractions = [
                    self._verified_correction_retraction_row(row) for row in rows
                ]
            except OperatorLearningStoreError:
                raise
            except (json.JSONDecodeError, sqlite3.Error, ValueError):
                raise OperatorLearningStoreError(
                    "Operator decision correction retractions could not be read safely."
                ) from None
        if any(item.tenant_id != tenant_id for item in retractions):
            raise OperatorLearningStoreError(
                "Operator decision correction tenant isolation check failed."
            )
        return retractions

    def find_relevant_guidance(
        self,
        tenant_id: str,
        *,
        case_id: str | None = None,
        text: str | None = None,
        limit: int = 10,
    ) -> list[GuidanceMatch]:
        """Return active guidance ranked by exact case and token overlap.

        An exact normalized case match contributes 100 points. Each distinct
        non-trivial token shared with the guidance contributes one point. Ties are
        resolved by newest timestamp and then guidance ID. Retrieval is advisory;
        it does not feed or alter the enforcement engine.
        """

        tenant_id = _validate_query_text(
            "tenant_id",
            tenant_id,
            MAX_IDENTIFIER_LENGTH,
        )
        normalized_case: str | None = None
        if case_id is not None:
            case_id = _validate_query_text(
                "case_id",
                case_id,
                MAX_IDENTIFIER_LENGTH,
            )
            normalized_case = _normalized_text(case_id)
        query_tokens: set[str] = set()
        if text is not None:
            text = _validate_query_text("text", text, MAX_QUERY_LENGTH)
            query_tokens = _tokens(text)
        if normalized_case is None and not query_tokens:
            raise ValueError("case_id or searchable text is required")
        _validate_limit(limit, MAX_RETRIEVAL_LIMIT)

        active = self.list_active_guidance(tenant_id, limit=MAX_LIST_LIMIT)
        matches: list[GuidanceMatch] = []
        for guidance in active:
            matched_case_ids = [
                item
                for item in guidance.case_ids
                if normalized_case is not None
                and _normalized_text(item) == normalized_case
            ]
            matched_tokens = sorted(
                query_tokens.intersection(_tokens(guidance.guidance))
            )
            score = (100 * len(matched_case_ids)) + len(matched_tokens)
            if score:
                matches.append(
                    GuidanceMatch(
                        guidance=guidance,
                        score=score,
                        matched_case_ids=matched_case_ids,
                        matched_tokens=matched_tokens[:MAX_MATCHED_TOKENS],
                    )
                )
        return sorted(
            matches,
            key=lambda match: (
                -match.score,
                -match.guidance.created_at.timestamp(),
                match.guidance.guidance_id,
            ),
        )[:limit]
