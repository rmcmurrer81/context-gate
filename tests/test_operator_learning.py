from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from context_gate.audit_log import AppendOnlyAuditLog
from context_gate.company_memory import CompanyObservation, MemoryStore
from context_gate.decision_engine import evaluate_request
from context_gate.models import EnforcementDecision, EvidenceStatus, Sensitivity
from context_gate.operator_learning import (
    DecisionCorrection,
    DecisionCorrectionRetraction,
    GuidanceOrigin,
    GuidanceRetraction,
    OperatorGuidance,
    OperatorLearningCollisionError,
    OperatorLearningStore,
    OperatorLearningStoreError,
)
from context_gate.scenario import load_scenario

BASE_TIME = datetime(2026, 9, 2, 12, tzinfo=UTC)


def guidance(
    guidance_id: str,
    *,
    tenant_id: str = "tenant-alpha",
    origin: GuidanceOrigin = GuidanceOrigin.REVIEW,
    text: str = "Verify suite changes against an exact organizer source.",
    case_ids: list[str] | None = None,
    created_at: datetime = BASE_TIME,
) -> OperatorGuidance:
    return OperatorGuidance(
        tenant_id=tenant_id,
        guidance_id=guidance_id,
        origin=origin,
        source_record_id=f"source-{guidance_id}",
        created_at=created_at,
        guidance=text,
        case_ids=case_ids if case_ids is not None else ["B1"],
    )


def correction_from_decision(
    correction_id: str,
    *,
    decision,
    tenant_id: str = "tenant-alpha",
    corrected_outcome: EnforcementDecision = EnforcementDecision.REVIEW,
    created_at: datetime = BASE_TIME,
) -> DecisionCorrection:
    return DecisionCorrection(
        tenant_id=tenant_id,
        correction_id=correction_id,
        case_id="B1",
        original_decision_id=decision.decision_id,
        request_fingerprint=decision.request_digest,
        evidence_fingerprint=decision.evidence_digest,
        policy_fingerprint=decision.policy_fingerprint,
        original_outcome=decision.decision,
        corrected_outcome=corrected_outcome,
        created_at=created_at,
        reviewer="authorized-reviewer",
        rationale="The human verified stronger exact evidence after the original hold.",
    )


def test_guidance_is_tenant_scoped_retrievable_and_retractable(tmp_path: Path) -> None:
    path = tmp_path / "company-memory.sqlite3"
    observation = CompanyObservation(
        tenant_id="tenant-alpha",
        observation_id="observation-1",
        category="company_event",
        occurred_at=BASE_TIME,
        attributes={"address": "76 New Avenue", "suite": "232"},
        source_type="official_email",
        trust_score=0.95,
        status=EvidenceStatus.CONFIRMED,
        sensitivity=Sensitivity.INTERNAL,
        evidence_reference="synthetic://observation-1",
    )
    with MemoryStore(path) as memory:
        memory.upsert(observation)

    review_guidance = guidance("guide-review", case_ids=["B1", "R2"])
    chat_guidance = guidance(
        "guide-chat",
        origin=GuidanceOrigin.CHAT,
        text="Crowd totals must distinguish additional attendees from a new total.",
        case_ids=["R3"],
        created_at=BASE_TIME + timedelta(minutes=1),
    )
    other_tenant = guidance(
        "guide-review",
        tenant_id="tenant-beta",
        text="Private beta-only instruction.",
    )

    with OperatorLearningStore(path) as store:
        assert store.append_guidance(review_guidance) == "inserted"
        assert store.append_guidance(review_guidance) == "unchanged"
        assert store.append_guidance(chat_guidance) == "inserted"
        assert store.append_guidance(other_tenant) == "inserted"

        by_case = store.find_relevant_guidance(
            "tenant-alpha",
            case_id=" b1 ",
            text="Which organizer should verify the suite?",
        )
        by_tokens = store.find_relevant_guidance(
            "tenant-alpha",
            text="Are these additional crowd attendees or the total?",
        )

        assert [match.guidance.guidance_id for match in by_case] == ["guide-review"]
        assert by_case[0].matched_case_ids == ["B1"]
        assert set(by_case[0].matched_tokens) == {"organizer", "suite", "verify"}
        assert [match.guidance.guidance_id for match in by_tokens] == ["guide-chat"]
        assert set(by_tokens[0].matched_tokens) >= {"additional", "attendees", "crowd"}

        tombstone = GuidanceRetraction(
            tenant_id="tenant-alpha",
            retraction_id="retract-guide-review",
            guidance_id="guide-review",
            retracted_at=BASE_TIME + timedelta(minutes=2),
            actor="authorized-reviewer",
            reason="The guidance was based on a misunderstood source.",
        )
        assert store.append_retraction(tombstone) == "inserted"
        assert store.append_retraction(tombstone) == "unchanged"
        assert [
            item.guidance_id for item in store.list_active_guidance("tenant-alpha")
        ] == ["guide-chat"]
        assert store.find_relevant_guidance("tenant-alpha", case_id="B1") == []
        assert store.list_retractions("tenant-alpha") == [tombstone]
        assert [
            item.guidance_id for item in store.list_active_guidance("tenant-beta")
        ] == ["guide-review"]

    with sqlite3.connect(path) as connection:
        original_count = connection.execute(
            """
            SELECT count(*) FROM operator_guidance
            WHERE tenant_id = ? AND guidance_id = ?
            """,
            ("tenant-alpha", "guide-review"),
        ).fetchone()[0]
    assert original_count == 1

    with OperatorLearningStore(path) as reopened:
        assert [
            item.guidance_id for item in reopened.list_active_guidance("tenant-alpha")
        ] == ["guide-chat"]
        assert reopened.list_retractions("tenant-alpha") == [tombstone]

    with MemoryStore(path) as memory:
        assert memory.list_observations("tenant-alpha") == [observation]


def test_guidance_records_are_bounded_immutable_and_tamper_evident(
    tmp_path: Path,
) -> None:
    secret = "private-guidance-value-not-for-errors"
    with pytest.raises(ValidationError) as too_long:
        guidance("too-long", text=secret + ("x" * 4_000))
    assert secret not in str(too_long.value)

    with pytest.raises(ValidationError, match="unique after normalization"):
        guidance("duplicate-cases", case_ids=["B1", " b1 "])
    with pytest.raises(ValidationError, match="explicit timezone"):
        guidance("naive-time", created_at=datetime(2026, 9, 2, 12))  # noqa: DTZ001
    with pytest.raises(ValidationError, match="control characters"):
        guidance("bad-control", text="Unsafe\noperator guidance")

    path = tmp_path / "tamper.sqlite3"
    original = guidance("immutable")
    with OperatorLearningStore(path) as store:
        store.append_guidance(original)
        changed = original.model_copy(update={"guidance": secret})
        with pytest.raises(OperatorLearningCollisionError) as collision:
            store.append_guidance(changed)
        assert secret not in str(collision.value)

        with sqlite3.connect(path) as attacker:
            attacker.execute(
                """
                UPDATE operator_guidance
                SET payload_json = replace(payload_json, 'Verify suite', 'Trust suite')
                WHERE tenant_id = ? AND guidance_id = ?
                """,
                ("tenant-alpha", "immutable"),
            )
        with pytest.raises(OperatorLearningStoreError, match="integrity check failed"):
            store.list_active_guidance("tenant-alpha")


def test_concurrent_idempotent_appends_are_serialized(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.sqlite3"
    repeated = guidance("shared-guidance")
    with OperatorLearningStore(path) as store:

        def append_repeated(_: int) -> str:
            return store.append_guidance(repeated)

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(append_repeated, range(24)))

        assert results.count("inserted") == 1
        assert results.count("unchanged") == 23

        records = [
            guidance(
                f"concurrent-{number:02d}",
                created_at=BASE_TIME + timedelta(seconds=number),
            )
            for number in range(20)
        ]
        with ThreadPoolExecutor(max_workers=8) as executor:
            unique_results = list(executor.map(store.append_guidance, records))

        assert unique_results == ["inserted"] * 20
        assert len(store.list_active_guidance("tenant-alpha")) == 21


def test_latest_decision_correction_preserves_receipt_and_retracts(
    tmp_path: Path,
) -> None:
    events, request = load_scenario("conflict")
    original_decision = evaluate_request(
        events,
        request,
        run_id="operator-learning-original",
    )
    assert original_decision.decision == EnforcementDecision.BLOCK
    audit = AppendOnlyAuditLog(tmp_path / "audit.jsonl")
    audit.append_decision(original_decision)
    receipt_before = audit.read_entries()[0].model_dump(mode="json")

    older = correction_from_decision(
        "decision-correction-1", decision=original_decision
    )
    newer = correction_from_decision(
        "decision-correction-2",
        decision=original_decision,
        corrected_outcome=EnforcementDecision.ALLOW,
        created_at=BASE_TIME + timedelta(minutes=5),
    )
    path = tmp_path / "decision-corrections.sqlite3"
    with OperatorLearningStore(path) as store:
        assert store.append_decision_correction(older) == "inserted"
        assert store.append_decision_correction(newer) == "inserted"
        assert store.append_decision_correction(newer) == "unchanged"

        lookup_arguments = {
            "case_id": "b1",
            "request_fingerprint": original_decision.request_digest,
            "evidence_fingerprint": original_decision.evidence_digest,
            "policy_fingerprint": original_decision.policy_fingerprint,
        }
        latest = store.latest_active_decision_correction(
            "tenant-alpha",
            **lookup_arguments,
        )
        assert latest == newer
        assert latest.resolution_status == "RESOLVED"
        assert latest.effective_outcome == EnforcementDecision.ALLOW
        assert latest.action_executed is False
        assert (
            store.latest_active_decision_correction(
                "tenant-beta",
                **lookup_arguments,
            )
            is None
        )

        with pytest.raises(
            OperatorLearningStoreError, match="unavailable for this tenant"
        ):
            store.append_decision_correction_retraction(
                DecisionCorrectionRetraction(
                    tenant_id="tenant-beta",
                    retraction_id="cross-tenant-retraction",
                    correction_id=newer.correction_id,
                    retracted_at=BASE_TIME + timedelta(minutes=6),
                    actor="beta-reviewer",
                    reason="A cross-tenant request must not find the correction.",
                )
            )

        retract_newer = DecisionCorrectionRetraction(
            tenant_id="tenant-alpha",
            retraction_id="retract-correction-2",
            correction_id=newer.correction_id,
            retracted_at=BASE_TIME + timedelta(minutes=7),
            actor="authorized-reviewer",
            reason="The newer human resolution was itself mistaken.",
        )
        assert store.append_decision_correction_retraction(retract_newer) == "inserted"
        assert store.append_decision_correction_retraction(retract_newer) == "unchanged"
        assert (
            store.latest_active_decision_correction(
                "tenant-alpha",
                **lookup_arguments,
            )
            == older
        )

        retract_older = DecisionCorrectionRetraction(
            tenant_id="tenant-alpha",
            retraction_id="retract-correction-1",
            correction_id=older.correction_id,
            retracted_at=BASE_TIME + timedelta(minutes=8),
            actor="authorized-reviewer",
            reason="Withdraw the remaining superseded human resolution.",
        )
        store.append_decision_correction_retraction(retract_older)
        assert (
            store.latest_active_decision_correction(
                "tenant-alpha",
                **lookup_arguments,
            )
            is None
        )
        assert store.list_active_decision_corrections("tenant-alpha") == []
        assert store.list_decision_correction_retractions("tenant-alpha") == [
            retract_older,
            retract_newer,
        ]

    receipt_after = audit.read_entries()[0].model_dump(mode="json")
    assert receipt_after == receipt_before
    repeated_decision = evaluate_request(
        events,
        request,
        run_id="operator-learning-original",
    )
    assert repeated_decision == original_decision

    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM operator_decision_corrections"
            ).fetchone()[0]
            == 2
        )

    with OperatorLearningStore(path) as reopened:
        assert (
            reopened.latest_active_decision_correction(
                "tenant-alpha",
                **lookup_arguments,
            )
            is None
        )
        assert len(reopened.list_decision_correction_retractions("tenant-alpha")) == 2


def test_decision_correction_validates_outcomes_and_exact_context(
    tmp_path: Path,
) -> None:
    events, request = load_scenario("conflict")
    decision = evaluate_request(events, request, run_id="correction-validation")

    safe_events, safe_request = load_scenario("safe")
    allow_decision = evaluate_request(
        safe_events,
        safe_request,
        run_id="allow-correction-validation",
    )
    allow_to_block = correction_from_decision(
        "allow-to-block",
        decision=allow_decision,
        corrected_outcome=EnforcementDecision.BLOCK,
    )
    assert allow_to_block.original_outcome == EnforcementDecision.ALLOW
    assert allow_to_block.effective_outcome == EnforcementDecision.BLOCK
    assert allow_to_block.action_executed is False

    with pytest.raises(ValidationError, match="must differ"):
        correction_from_decision(
            "same-outcome",
            decision=decision,
            corrected_outcome=EnforcementDecision.BLOCK,
        )

    path = tmp_path / "exact-context.sqlite3"
    stored = correction_from_decision("stored-correction", decision=decision)
    with OperatorLearningStore(path) as store:
        store.append_decision_correction(stored)
        assert (
            store.latest_active_decision_correction(
                "tenant-alpha",
                case_id="B1",
                request_fingerprint="0" * 64,
                evidence_fingerprint=decision.evidence_digest,
                policy_fingerprint=decision.policy_fingerprint,
            )
            is None
        )
        with pytest.raises(ValueError, match="request_fingerprint"):
            store.latest_active_decision_correction(
                "tenant-alpha",
                case_id="B1",
                request_fingerprint="not-a-fingerprint",
                evidence_fingerprint=decision.evidence_digest,
                policy_fingerprint=decision.policy_fingerprint,
            )
