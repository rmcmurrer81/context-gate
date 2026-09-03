from context_gate.decision_engine import evaluate_request
from context_gate.scenario import load_demo_events, load_demo_request


def test_model_cannot_override_deterministic_block() -> None:
    malicious_or_wrong_output = {
        "decision": "ALLOW",
        "risk": "LOW",
        "classification": "SAFE",
        "authoritative_value": "2 Innovation Street",
        "explanation": "Go ahead.",
        "evidence_event_ids": ["evt-104"],
        "requires_human_approval": False,
    }
    decision = evaluate_request(
        load_demo_events(),
        load_demo_request(),
        model_output=malicious_or_wrong_output,
    )
    assert decision.decision.value == "BLOCK"
    assert decision.model_explanation_used is False
    assert decision.model_response_valid is False
    assert decision.explanation != "Go ahead."
