from __future__ import annotations

from itertools import permutations

import pytest
from pydantic import ValidationError

from context_gate.approvals import record_review
from context_gate.audit_log import AppendOnlyAuditLog
from context_gate.confluent_adapter import ConfluentAdapter
from context_gate.decision_engine import evaluate_request
from context_gate.deterministic_rules import RULE_PEER_CONFLICT, RULE_UNTRUSTED
from context_gate.evidence import build_evidence_package
from context_gate.models import (
    ActionRequest,
    Classification,
    EnforcementDecision,
    ReviewAction,
)

from .helpers import event, request


def test_near_peer_disagreement_is_reviewed_for_every_input_order() -> None:
    official = event(event_id="evt-a", field_value="Room A")
    organizer_api = event(
        event_id="evt-b",
        field_value="Room B",
        source_name="Organizer API",
        source_type="organizer_api",
        evidence_reference="synthetic://organizer-api",
    )
    action = request(requested_value="Room A", supporting_event_id="evt-a")

    results = [
        evaluate_request(list(order), action, run_id="peer-order")
        for order in permutations([official, organizer_api])
    ]

    assert all(item.classification == Classification.CONFLICT for item in results)
    assert all(item.decision == EnforcementDecision.REVIEW for item in results)
    assert all(RULE_PEER_CONFLICT in item.deterministic_rule_ids for item in results)
    assert results[0].model_dump(mode="json") == results[1].model_dump(mode="json")


def test_deduplication_never_discards_sensitive_upgrade() -> None:
    public = event(event_id="evt-public", sensitivity="public")
    sensitive = event(event_id="evt-sensitive", sensitivity="sensitive")

    results = [
        evaluate_request(
            list(order),
            request(supporting_event_id="evt-public"),
            run_id="sensitivity-order",
        )
        for order in permutations([public, sensitive])
    ]

    assert all(item.classification == Classification.SENSITIVE for item in results)
    assert all(item.decision == EnforcementDecision.REVIEW for item in results)
    assert results[0].model_dump(mode="json") == results[1].model_dump(mode="json")


@pytest.mark.parametrize(
    ("support_id", "support_value"),
    [
        (None, "10 Innovation Street"),
        ("evt-missing", "10 Innovation Street"),
        ("evt-official", "2 Innovation Street"),
    ],
)
def test_unbound_supporting_evidence_cannot_allow(
    support_id: str | None, support_value: str
) -> None:
    decision = evaluate_request(
        [event()],
        request(supporting_event_id=support_id, requested_value=support_value),
    )
    assert decision.classification == Classification.INSUFFICIENT_EVIDENCE
    assert decision.decision == EnforcementDecision.REVIEW


def test_future_evidence_is_unavailable_as_of_request_time() -> None:
    future = event(observed_at="2026-09-03T12:00:00Z")
    decision = evaluate_request([future], request())
    assert decision.classification == Classification.INSUFFICIENT_EVIDENCE
    assert decision.decision == EnforcementDecision.REVIEW
    assert "after" in decision.explanation or "later" in decision.explanation


def test_matching_low_authority_evidence_requires_review() -> None:
    report = event(
        source_name="Anonymous report",
        source_type="user_report",
        trust_score=0.99,
        evidence_reference="synthetic://user-report",
        status="unverified",
    )
    decision = evaluate_request([report], request())
    assert decision.classification == Classification.UNTRUSTED
    assert decision.decision == EnforcementDecision.REVIEW
    assert decision.deterministic_rule_ids == [RULE_UNTRUSTED]


def test_decision_identity_binds_request_evidence_and_payload(tmp_path) -> None:
    first = evaluate_request([event(trust_score=0.98)], request(), run_id="same-run")
    second = evaluate_request([event(trust_score=0.97)], request(), run_id="same-run")
    assert first.decision_id != second.decision_id

    log = AppendOnlyAuditLog(tmp_path / "audit.jsonl")
    log.append_decision(first)
    collision = first.model_copy(update={"explanation": "different payload"})
    with pytest.raises(ValueError, match="identity collision"):
        log.append_decision(collision)


def test_review_binds_exact_request_and_is_one_shot(tmp_path) -> None:
    action = request(consequential=True)
    decision = evaluate_request([event()], action)
    changed = action.model_copy(update={"requested_value": "Different"})
    with pytest.raises(ValueError, match="content are not bound"):
        record_review(
            decision,
            changed,
            action=ReviewAction.REJECT,
            reviewer="reviewer",
            rationale="The request changed after the decision.",
        )

    first = record_review(
        decision,
        action,
        action=ReviewAction.HOLD,
        reviewer="reviewer",
        rationale="Hold for verification.",
    )
    second = record_review(
        decision,
        action,
        action=ReviewAction.REJECT,
        reviewer="reviewer",
        rationale="Reject after verification.",
    )
    assert first.review_id == second.review_id
    log = AppendOnlyAuditLog(tmp_path / "reviews.jsonl")
    log.append_decision(decision)
    log.append_review(first)
    with pytest.raises(ValueError, match="identity collision"):
        log.append_review(second)


def test_identical_review_retry_returns_original_audit_entry(tmp_path) -> None:
    action = request(consequential=True)
    decision = evaluate_request([event()], action)
    first = record_review(
        decision,
        action,
        action=ReviewAction.HOLD,
        reviewer="reviewer",
        rationale="Hold for verification.",
    )
    retry = record_review(
        decision,
        action,
        action=ReviewAction.HOLD,
        reviewer="reviewer",
        rationale="Hold for verification.",
    )
    log = AppendOnlyAuditLog(tmp_path / "reviews.jsonl")

    log.append_decision(decision)
    original_entry = log.append_review(first)
    retry_entry = log.append_review(retry)

    assert retry_entry.audit_id == original_entry.audit_id
    assert len(log.read_entries()) == 2


def test_orphan_or_mismatched_review_cannot_enter_audit_log(tmp_path) -> None:
    action = request(consequential=True)
    decision = evaluate_request([event()], action)
    review = record_review(
        decision,
        action,
        action=ReviewAction.HOLD,
        reviewer="reviewer",
        rationale="Hold for verification.",
    )
    log = AppendOnlyAuditLog(tmp_path / "reviews.jsonl")

    with pytest.raises(ValueError, match="durable decision"):
        log.append_review(review)

    log.append_decision(decision)
    mismatched = review.model_copy(update={"decision_digest": "0" * 64})
    with pytest.raises(ValueError, match="does not match"):
        log.append_review(mismatched)


def test_naive_timestamps_are_rejected() -> None:
    with pytest.raises(ValidationError, match="explicit timezone"):
        ActionRequest.model_validate(
            {
                **request().model_dump(mode="json"),
                "created_at": "2026-09-02T12:00:00",
            }
        )


def test_evidence_package_uses_only_matching_as_of_evidence() -> None:
    relevant = event()
    unrelated = event(
        event_id="evt-unrelated",
        entity_id="other-event",
        field_value="Wrong place",
        observed_at="2026-09-01T11:00:00Z",
    )
    future = event(
        event_id="evt-future",
        field_value="Future place",
        observed_at="2026-09-03T12:00:00Z",
    )
    package = build_evidence_package([unrelated, future, relevant], request())
    assert package["authoritative_event_id"] == "evt-official"
    assert [item["event_id"] for item in package["events"]] == ["evt-official"]
    assert package["excluded_future_event_ids"] == ["evt-future"]


class _PendingProducer:
    def produce(self, *_args: object, **_kwargs: object) -> None:
        return None

    def flush(self, _timeout: int) -> int:
        return 2


def test_kafka_publish_does_not_claim_success_while_messages_are_pending() -> None:
    adapter = ConfluentAdapter()
    adapter.mode = "confluent"
    adapter._producer = _PendingProducer()
    receipt = adapter.publish("decisions", "key", {"value": 1})
    assert receipt.delivered is False
    assert "2 message(s) pending" in receipt.detail


class _DeliveryError:
    def code(self) -> int:
        return 29

    def name(self) -> str:
        return "AUTHENTICATION"


class _CallbackProducer:
    def __init__(self, error: object | None, *, invoke_callback: bool = True) -> None:
        self.error = error
        self.invoke_callback = invoke_callback

    def produce(self, *_args: object, **kwargs: object) -> None:
        if self.invoke_callback:
            kwargs["on_delivery"](self.error, object())

    def flush(self, _timeout: int) -> int:
        return 0


def test_kafka_publish_requires_positive_broker_acknowledgement() -> None:
    adapter = ConfluentAdapter()
    adapter.mode = "confluent"
    adapter._producer = _CallbackProducer(None)
    receipt = adapter.publish("decisions", "key", {"value": 1})
    assert receipt.delivered is True
    assert "acknowledged" in receipt.detail


def test_kafka_publish_reports_callback_error_even_when_queue_drains() -> None:
    adapter = ConfluentAdapter()
    adapter.mode = "confluent"
    adapter._producer = _CallbackProducer(_DeliveryError())
    receipt = adapter.publish("decisions", "key", {"value": 1})
    assert receipt.delivered is False
    assert "AUTHENTICATION (29)" in receipt.detail


def test_kafka_publish_does_not_infer_acknowledgement_from_zero_flush() -> None:
    adapter = ConfluentAdapter()
    adapter.mode = "confluent"
    adapter._producer = _CallbackProducer(None, invoke_callback=False)
    receipt = adapter.publish("decisions", "key", {"value": 1})
    assert receipt.delivered is False
    assert "without an observed broker acknowledgement" in receipt.detail
