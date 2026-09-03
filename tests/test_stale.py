from context_gate.decision_engine import evaluate_request
from context_gate.models import Classification, EnforcementDecision

from .helpers import event, request


def test_older_effective_action_is_blocked_as_stale() -> None:
    decision = evaluate_request(
        [event()],
        request(requested_effective_at="2026-09-01T12:00:00Z"),
    )
    assert decision.classification == Classification.STALE
    assert decision.decision == EnforcementDecision.BLOCK
