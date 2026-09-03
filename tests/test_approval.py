from context_gate.approvals import record_review
from context_gate.decision_engine import evaluate_request
from context_gate.models import ReviewAction, ReviewStatus
from context_gate.scenario import load_demo_events, load_demo_request


def test_explicit_override_is_a_new_receipt_not_an_executed_action() -> None:
    request = load_demo_request()
    decision = evaluate_request(load_demo_events(), request)
    review = record_review(
        decision,
        request,
        action=ReviewAction.APPROVE_OVERRIDE,
        reviewer="judge",
        rationale="Organizer verbally confirmed the change.",
    )
    assert review.resulting_status == ReviewStatus.HUMAN_OVERRIDE
    assert review.decision_id == decision.decision_id
    assert review.action_executed is False
    assert decision.review_status == ReviewStatus.PENDING
