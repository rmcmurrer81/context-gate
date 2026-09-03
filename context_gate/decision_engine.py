"""Orchestrate normalization, deterministic enforcement, and safe explanation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from .deterministic_rules import evaluate_rules
from .explanation import validated_explanation
from .models import ActionRequest, ContextEvent, DecisionRecord, ReviewStatus
from .normalization import normalize_event, normalize_request


def evaluate_request(
    events: list[ContextEvent],
    request: ActionRequest,
    *,
    model_output: Mapping[str, Any] | str | None = None,
    run_id: str | None = None,
) -> DecisionRecord:
    normalized_events = [normalize_event(event) for event in events]
    normalized_request = normalize_request(request)
    result = evaluate_rules(normalized_events, normalized_request)
    explanation, model_used, model_valid = validated_explanation(result, model_output)
    actual_run_id = run_id or f"run-{uuid4().hex[:10]}"
    decision_id = f"dec-{uuid5(NAMESPACE_URL, f'contextgate:{actual_run_id}:{normalized_request.request_id}').hex[:12]}"
    return DecisionRecord(
        decision_id=decision_id,
        run_id=actual_run_id,
        request_id=normalized_request.request_id,
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
    )
