"""Explicit review receipts. Reviews never rewrite the original decision."""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from .hashing import sha256_digest
from .models import (
    ActionRequest,
    DecisionRecord,
    ReviewAction,
    ReviewEvent,
    ReviewStatus,
)
from .normalization import normalize_request

RESULTING_STATUS = {
    ReviewAction.HOLD: ReviewStatus.HELD,
    ReviewAction.REJECT: ReviewStatus.REJECTED,
    ReviewAction.APPROVE_OVERRIDE: ReviewStatus.HUMAN_OVERRIDE,
}


def record_review(
    decision: DecisionRecord,
    request: ActionRequest,
    *,
    action: ReviewAction,
    reviewer: str,
    rationale: str,
) -> ReviewEvent:
    if not decision.requires_human_approval:
        raise ValueError("this decision does not require review")
    if decision.request_id != request.request_id:
        raise ValueError("decision and request are not bound")
    normalized_request = normalize_request(request)
    request_digest = sha256_digest(normalized_request.model_dump(mode="json"))
    if decision.request_digest != request_digest:
        raise ValueError("decision and request content are not bound")
    decision_digest = sha256_digest(decision.model_dump(mode="json"))
    return ReviewEvent(
        review_id=f"rev-{uuid5(NAMESPACE_URL, f'contextgate-review:{decision.decision_id}').hex[:24]}",
        decision_id=decision.decision_id,
        request_id=request.request_id,
        request_digest=request_digest,
        decision_digest=decision_digest,
        action=action,
        resulting_status=RESULTING_STATUS[action],
        reviewer=reviewer,
        rationale=rationale,
        authoritative_value=decision.authoritative_value,
        requested_value=request.requested_value,
        # ContextGate records review intent only. It never performs the action.
        action_executed=False,
    )
