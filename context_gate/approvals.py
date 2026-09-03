"""Explicit review receipts. Reviews never rewrite the original decision."""

from __future__ import annotations

from uuid import uuid4

from .models import (
    ActionRequest,
    DecisionRecord,
    ReviewAction,
    ReviewEvent,
    ReviewStatus,
)

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
    return ReviewEvent(
        review_id=f"rev-{uuid4().hex[:12]}",
        decision_id=decision.decision_id,
        request_id=request.request_id,
        action=action,
        resulting_status=RESULTING_STATUS[action],
        reviewer=reviewer,
        rationale=rationale,
        authoritative_value=decision.authoritative_value,
        requested_value=request.requested_value,
        # This demo records intent only. It never performs a calendar update.
        action_executed=False,
    )
