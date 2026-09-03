from context_gate.deterministic_rules import classify_incoming_event
from context_gate.models import Classification
from context_gate.normalization import normalize_event

from .helpers import event


def test_identical_semantic_evidence_is_duplicate() -> None:
    original = normalize_event(event())
    repeated = normalize_event(
        event(event_id="evt-repeat", observed_at="2026-09-02T12:00:00Z")
    )
    assert classify_incoming_event(repeated, [original]) == Classification.DUPLICATE
