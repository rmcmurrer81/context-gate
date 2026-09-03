from context_gate.decision_engine import evaluate_request
from context_gate.models import Classification, EnforcementDecision, Risk
from context_gate.scenario import load_demo_events, load_demo_request


def test_lower_authority_conflict_blocks_requested_action() -> None:
    decision = evaluate_request(load_demo_events(), load_demo_request())
    assert decision.classification == Classification.CONFLICT
    assert decision.decision == EnforcementDecision.BLOCK
    assert decision.risk == Risk.HIGH
    assert decision.authoritative_value == "10 Innovation Street"
    assert decision.evidence_event_ids == ["evt-100", "evt-104"]
    assert decision.requires_human_approval is True
