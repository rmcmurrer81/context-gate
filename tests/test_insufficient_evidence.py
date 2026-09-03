from context_gate.decision_engine import evaluate_request
from context_gate.models import Classification, EnforcementDecision

from .helpers import event, request


def test_missing_timestamp_is_reviewed_not_invented() -> None:
    decision = evaluate_request([event(observed_at=None)], request())
    assert decision.classification == Classification.INSUFFICIENT_EVIDENCE
    assert decision.decision == EnforcementDecision.REVIEW
    assert "observed_at" in decision.explanation
