"""Orchestrate normalization, deterministic enforcement, and safe explanation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from .deterministic_rules import evaluate_rules
from .explanation import validated_explanation
from .hashing import canonical_json, sha256_digest
from .models import ActionRequest, ContextEvent, DecisionRecord, ReviewStatus
from .normalization import normalize_event, normalize_request
from .policy_config import DEFAULT_POLICY, ActivePolicy, get_active_policy

POLICY_VERSION = DEFAULT_POLICY.policy_version


def evaluate_request(
    events: list[ContextEvent],
    request: ActionRequest,
    *,
    model_output: Mapping[str, Any] | str | None = None,
    run_id: str | None = None,
    policy: ActivePolicy | None = None,
) -> DecisionRecord:
    active_policy = policy or get_active_policy()
    normalized_events = [normalize_event(event) for event in events]
    normalized_request = normalize_request(request)
    result = evaluate_rules(normalized_events, normalized_request, policy=active_policy)
    explanation, model_used, model_valid = validated_explanation(result, model_output)
    actual_run_id = run_id or f"run-{uuid4().hex[:10]}"
    request_payload = normalized_request.model_dump(mode="json")
    evidence_payload = {
        "events": [
            event.model_dump(mode="json")
            for event in sorted(
                normalized_events,
                key=lambda item: (item.event_id, item.content_hash or ""),
            )
        ]
    }
    request_digest = sha256_digest(request_payload)
    evidence_digest = sha256_digest(evidence_payload)
    decision_identity = {
        "run_id": actual_run_id,
        "request_digest": request_digest,
        "evidence_digest": evidence_digest,
        "policy_version": active_policy.policy_version,
        "policy_fingerprint": active_policy.policy_fingerprint,
        "rule_result": result.model_dump(mode="json"),
        "accepted_explanation": explanation,
        "model_explanation_used": model_used,
        "model_response_valid": model_valid,
    }
    decision_id = (
        f"dec-{uuid5(NAMESPACE_URL, canonical_json(decision_identity)).hex[:24]}"
    )
    return DecisionRecord(
        decision_id=decision_id,
        run_id=actual_run_id,
        request_id=normalized_request.request_id,
        request_digest=request_digest,
        evidence_digest=evidence_digest,
        policy_version=active_policy.policy_version,
        policy_fingerprint=active_policy.policy_fingerprint,
        classification=result.classification,
        decision=result.decision,
        risk=result.risk,
        authoritative_value=result.authoritative_value,
        competing_values=result.competing_values,
        evidence_event_ids=result.evidence_event_ids,
        explanation=explanation,
        deterministic_rule_ids=result.deterministic_rule_ids,
        model_explanation_used=model_used,
        model_response_valid=model_valid,
        requires_human_approval=result.requires_human_approval,
        review_status=(
            ReviewStatus.PENDING
            if result.requires_human_approval
            else ReviewStatus.NOT_REQUIRED
        ),
        created_at=normalized_request.created_at,
    )
