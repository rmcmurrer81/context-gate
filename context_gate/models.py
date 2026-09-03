"""Strict public models shared by local and Confluent execution paths."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, use_enum_values=False
    )


class EvidenceStatus(StrEnum):
    CONFIRMED = "confirmed"
    UNVERIFIED = "unverified"
    INFERRED = "inferred"
    UNKNOWN = "unknown"


class Sensitivity(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    SENSITIVE = "sensitive"


class Classification(StrEnum):
    SAFE = "SAFE"
    DUPLICATE = "DUPLICATE"
    STALE = "STALE"
    CONFLICT = "CONFLICT"
    SENSITIVE = "SENSITIVE"
    UNTRUSTED = "UNTRUSTED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class EnforcementDecision(StrEnum):
    ALLOW = "ALLOW"
    BLOCK = "BLOCK"
    REVIEW = "REVIEW"


class Risk(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ReviewStatus(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    HELD = "HELD"
    REJECTED = "REJECTED"
    HUMAN_OVERRIDE = "HUMAN_OVERRIDE"


class ReviewAction(StrEnum):
    HOLD = "HOLD"
    REJECT = "REJECT"
    APPROVE_OVERRIDE = "APPROVE_OVERRIDE"


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class ContextEvent(StrictModel):
    event_id: str = Field(min_length=1, max_length=128)
    entity_id: str = Field(min_length=1, max_length=256)
    field_name: str = Field(min_length=1, max_length=128)
    field_value: str = Field(min_length=1, max_length=4096)
    source_name: str | None = Field(default=None, max_length=256)
    source_type: str | None = Field(default=None, max_length=128)
    trust_score: float = Field(ge=0.0, le=1.0)
    observed_at: datetime | None = None
    effective_at: datetime | None = None
    sensitivity: Sensitivity = Sensitivity.PUBLIC
    evidence_uri: str | None = Field(default=None, max_length=2048)
    evidence_reference: str | None = Field(default=None, max_length=2048)
    content_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    status: EvidenceStatus = EvidenceStatus.UNVERIFIED

    _normalize_observed = field_validator("observed_at", mode="after")(_aware_utc)
    _normalize_effective = field_validator("effective_at", mode="after")(_aware_utc)


class ActionRequest(StrictModel):
    request_id: str = Field(min_length=1, max_length=128)
    action_id: str = Field(min_length=1, max_length=128)
    action_type: str = Field(min_length=1, max_length=128)
    entity_id: str = Field(min_length=1, max_length=256)
    field_name: str = Field(min_length=1, max_length=128)
    requested_value: str = Field(min_length=1, max_length=4096)
    supporting_event_id: str | None = Field(default=None, max_length=128)
    requested_effective_at: datetime | None = None
    sensitivity: Sensitivity = Sensitivity.PUBLIC
    consequential: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    _normalize_requested_effective = field_validator(
        "requested_effective_at", mode="after"
    )(_aware_utc)
    _normalize_created = field_validator("created_at", mode="after")(_aware_utc)


class RuleResult(StrictModel):
    classification: Classification
    decision: EnforcementDecision
    risk: Risk
    authoritative_value: str | None = None
    competing_values: list[str] = Field(default_factory=list)
    evidence_event_ids: list[str] = Field(default_factory=list)
    explanation: str
    deterministic_rule_ids: list[str] = Field(min_length=1)
    requires_human_approval: bool


class AgentExplanation(StrictModel):
    decision: EnforcementDecision
    risk: Risk
    classification: Classification
    authoritative_value: str | None = None
    explanation: str = Field(min_length=1, max_length=4000)
    evidence_event_ids: list[str]
    requires_human_approval: bool


class DecisionRecord(StrictModel):
    decision_id: str
    run_id: str
    request_id: str
    classification: Classification
    decision: EnforcementDecision
    risk: Risk
    authoritative_value: str | None = None
    competing_values: list[str] = Field(default_factory=list)
    evidence_event_ids: list[str] = Field(default_factory=list)
    explanation: str
    deterministic_rule_ids: list[str] = Field(default_factory=list)
    model_explanation_used: bool = False
    model_response_valid: bool = False
    requires_human_approval: bool
    review_status: ReviewStatus
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    _normalize_reviewed = field_validator("reviewed_at", mode="after")(_aware_utc)
    _normalize_decision_created = field_validator("created_at", mode="after")(
        _aware_utc
    )

    @model_validator(mode="after")
    def review_state_is_consistent(self) -> DecisionRecord:
        if (
            self.requires_human_approval
            and self.review_status == ReviewStatus.NOT_REQUIRED
        ):
            raise ValueError(
                "approval-required decisions cannot have NOT_REQUIRED review status"
            )
        if (
            not self.requires_human_approval
            and self.review_status != ReviewStatus.NOT_REQUIRED
        ):
            raise ValueError("non-review decisions must use NOT_REQUIRED review status")
        return self


class ReviewEvent(StrictModel):
    review_id: str
    decision_id: str
    request_id: str
    action: ReviewAction
    resulting_status: ReviewStatus
    reviewer: str = Field(min_length=1, max_length=128)
    rationale: str = Field(min_length=3, max_length=2000)
    authoritative_value: str | None = None
    requested_value: str
    action_executed: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    _normalize_review_created = field_validator("created_at", mode="after")(_aware_utc)


class AuditEntry(StrictModel):
    audit_id: str
    sequence: int = Field(ge=1)
    record_type: str
    created_at: datetime
    payload: dict[str, Any]
    previous_hash: str
    entry_hash: str

    _normalize_audit_created = field_validator("created_at", mode="after")(_aware_utc)
