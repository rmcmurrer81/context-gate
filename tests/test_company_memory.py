from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Thread

import pytest
from pydantic import ValidationError

from context_gate.company_memory import (
    CompanyObservation,
    HumanCorrection,
    MemoryCollisionError,
    MemoryStore,
    MemoryStoreError,
    PatternAnalyzer,
    PatternOutcome,
)
from context_gate.models import EvidenceStatus, Sensitivity


def observation(
    observation_id: str,
    *,
    tenant_id: str = "tenant-alpha",
    occurred_at: datetime | None = None,
    attributes: dict[str, str] | None = None,
    source_type: str = "official_email",
    trust_score: float = 0.95,
    status: EvidenceStatus = EvidenceStatus.CONFIRMED,
    evidence_reference: str | None = None,
) -> CompanyObservation:
    suffix = observation_id.rsplit("-", 1)[-1]
    sequence = int(suffix) if suffix.isdigit() else 0
    return CompanyObservation(
        tenant_id=tenant_id,
        observation_id=observation_id,
        category="company_event",
        occurred_at=occurred_at
        or datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=sequence),
        attributes=attributes or {"address": "76 New Avenue", "suite": "232"},
        source_type=source_type,
        trust_score=trust_score,
        status=status,
        sensitivity=Sensitivity.INTERNAL,
        evidence_reference=evidence_reference or f"synthetic://{observation_id}",
    )


def seeded_store(tmp_path: Path) -> MemoryStore:
    store = MemoryStore(tmp_path / "memory.sqlite")
    for number in range(1, 4):
        store.upsert(
            observation(
                f"main-{number}",
                attributes={"address": "35 Main St", "suite": "110"},
            )
        )
    for number in range(1, 9):
        address = "  76   NEW avenue  " if number == 8 else "76 New Avenue"
        store.upsert(
            observation(
                f"new-{number}",
                occurred_at=datetime(2026, 2, number, tzinfo=UTC),
                attributes={"Address": address, "Suite": "232"},
            )
        )
    return store


def test_generic_and_conditional_counts_are_tenant_scoped(tmp_path) -> None:
    with seeded_store(tmp_path) as store:
        other = observation(
            "other-1",
            tenant_id="tenant-beta",
            attributes={"address": "Private Other Place", "suite": "999"},
        )
        store.upsert(other)

        summary = PatternAnalyzer(store).summarize_patterns(
            "tenant-alpha", category="COMPANY_EVENT"
        )

    addresses = {
        item.normalized_value: (item.display_value, item.count, item.trusted_count)
        for item in summary.attribute_patterns
        if item.attribute_key.casefold() == "address"
    }
    assert addresses["35 main st"][1:] == (3, 3)
    assert addresses["76 new avenue"][1:] == (8, 8)
    assert addresses["76 new avenue"][0] == "76 New Avenue"
    assert all(
        "private other" not in item.normalized_value
        for item in summary.attribute_patterns
    )

    suite_232 = next(
        item
        for item in summary.conditional_patterns
        if item.parent_display_value == "76 New Avenue"
        and item.child_display_value == "232"
    )
    assert suite_232.count == 8
    assert suite_232.trusted_count == 8
    assert len(suite_232.contributors) == 8
    assert all(item.evidence_reference for item in suite_232.contributors)
    assert {item.observation_id for item in suite_232.contributors} == {
        f"new-{number}" for number in range(1, 9)
    }
    assert suite_232.contributors_truncated is False
    assert len(summary.policy_fingerprint) == 64
    assert len(summary.analyzer_fingerprint) == 64
    assert summary.minimum_support == 3
    assert summary.minimum_trust_score == 0.70
    assert summary.history_limit == 1_000


def test_new_suite_at_dominant_address_requires_review_without_storing(
    tmp_path,
) -> None:
    with seeded_store(tmp_path) as store:
        analyzer = PatternAnalyzer(store)
        before = store.list_observations("tenant-alpha", limit=100)
        candidate = observation(
            "candidate-99",
            occurred_at=datetime(2026, 3, 1, tzinfo=UTC),
            attributes={"address": "76 new avenue", "suite": "354"},
        )

        assessment = analyzer.assess_candidate(candidate)
        after = store.list_observations("tenant-alpha", limit=100)

    assert assessment.outcome == PatternOutcome.REVIEW
    assert "NEW_SUITE_AT_KNOWN_ADDRESS" in {
        reason.code for reason in assessment.reasons
    }
    assert any(
        "232" in question and "354" in question
        for question in assessment.human_questions
    )
    suite_comparison = next(
        item
        for item in assessment.comparison_patterns
        if item.attribute_key == "suite at address"
    )
    assert suite_comparison.trusted_count == 8
    assert len(suite_comparison.contributors) == 8
    assert all(item.evidence_reference for item in suite_comparison.contributors)
    assert any("exact" in step.lower() for step in assessment.recommended_confirmation)
    assert assessment.candidate_stored is False
    assert assessment.automatic_lookup_performed is False
    assert assessment.action_executed is False
    assert [item.observation_id for item in before] == [
        item.observation_id for item in after
    ]
    assert "candidate-99" not in {item.observation_id for item in after}


def test_well_supported_exact_address_and_suite_is_allow_like(tmp_path) -> None:
    with seeded_store(tmp_path) as store:
        assessment = PatternAnalyzer(store).assess_candidate(
            observation(
                "candidate-20",
                occurred_at=datetime(2026, 3, 1, tzinfo=UTC),
                attributes={"address": "76 NEW AVENUE", "suite": " 232 "},
            )
        )

    assert assessment.outcome == PatternOutcome.ALLOW_LIKE
    assert assessment.reasons == []
    assert "not an execution approval" in assessment.summary
    assert assessment.action_executed is False


@pytest.mark.parametrize(
    ("source_type", "trust_score", "status", "evidence_reference", "reason_code"),
    [
        (
            "unknown",
            0.99,
            EvidenceStatus.CONFIRMED,
            "synthetic://candidate",
            "UNKNOWN_SOURCE",
        ),
        (
            "official_email",
            0.30,
            EvidenceStatus.CONFIRMED,
            "synthetic://candidate",
            "LOW_TRUST",
        ),
        (
            "official_email",
            0.99,
            EvidenceStatus.UNVERIFIED,
            "synthetic://candidate",
            "UNVERIFIED_CANDIDATE",
        ),
        (
            "official_email",
            0.99,
            EvidenceStatus.CONFIRMED,
            None,
            "MISSING_EVIDENCE_REFERENCE",
        ),
    ],
)
def test_weak_candidate_provenance_never_auto_confirms(
    tmp_path,
    source_type: str,
    trust_score: float,
    status: EvidenceStatus,
    evidence_reference: str | None,
    reason_code: str,
) -> None:
    with seeded_store(tmp_path) as store:
        candidate = observation(
            "candidate-22",
            attributes={"address": "76 New Avenue", "suite": "232"},
            source_type=source_type,
            trust_score=trust_score,
            status=status,
            evidence_reference=evidence_reference,
        )
        if evidence_reference is None:
            candidate = candidate.model_copy(update={"evidence_reference": None})
        assessment = PatternAnalyzer(store).assess_candidate(candidate)

    assert assessment.outcome == PatternOutcome.REVIEW
    assert reason_code in {item.code for item in assessment.reasons}


def test_low_historical_support_never_auto_confirms(tmp_path) -> None:
    with MemoryStore(tmp_path / "low-support.sqlite") as store:
        store.upsert(observation("known-1"))
        assessment = PatternAnalyzer(store, minimum_support=3).assess_candidate(
            observation("candidate-2")
        )

    assert assessment.outcome == PatternOutcome.REVIEW
    assert any(reason.code.endswith("LOW_SUPPORT") for reason in assessment.reasons)


def test_sensitive_candidate_always_requires_authorized_review(tmp_path) -> None:
    with seeded_store(tmp_path) as store:
        candidate = observation("candidate-33").model_copy(
            update={"sensitivity": Sensitivity.SENSITIVE}
        )
        assessment = PatternAnalyzer(store).assess_candidate(candidate)

    assert assessment.outcome == PatternOutcome.REVIEW
    assert "SENSITIVE_CANDIDATE" in {item.code for item in assessment.reasons}


def test_upsert_is_idempotent_and_changed_payload_is_collision(tmp_path) -> None:
    secret = "not-for-error-output"
    with MemoryStore(tmp_path / "idempotency.sqlite") as store:
        original = observation(
            "same-1", attributes={"address": "76 New Avenue", "note": secret}
        )
        assert store.upsert(original) == "inserted"
        assert store.upsert(original) == "unchanged"
        changed = original.model_copy(
            update={"attributes": {"address": "Changed Place", "note": secret}}
        )
        with pytest.raises(MemoryCollisionError) as caught:
            store.upsert(changed)

    assert secret not in str(caught.value)
    assert "Changed Place" not in str(caught.value)


def test_same_observation_id_in_different_tenants_never_mixes(tmp_path) -> None:
    with MemoryStore(tmp_path / "tenants.sqlite") as store:
        store.upsert(
            observation(
                "shared-1",
                tenant_id="tenant-alpha",
                attributes={"address": "Alpha Place"},
            )
        )
        store.upsert(
            observation(
                "shared-1",
                tenant_id="tenant-beta",
                attributes={"address": "Beta Place"},
            )
        )

        alpha = store.list_observations("tenant-alpha")
        beta = store.list_observations("tenant-beta")

    assert [item.attributes["address"] for item in alpha] == ["Alpha Place"]
    assert [item.attributes["address"] for item in beta] == ["Beta Place"]


def test_list_order_is_stable_and_bounded(tmp_path) -> None:
    same_time = datetime(2026, 4, 1, tzinfo=UTC)
    with MemoryStore(tmp_path / "order.sqlite") as store:
        for identifier in ("record-c", "record-a", "record-b"):
            store.upsert(observation(identifier, occurred_at=same_time))

        first = store.list_observations("tenant-alpha", limit=2)
        second = store.list_observations("tenant-alpha", limit=2)

        with pytest.raises(ValueError, match="limit"):
            store.list_observations("tenant-alpha", limit=0)

    assert [item.observation_id for item in first] == ["record-a", "record-b"]
    assert [item.observation_id for item in second] == ["record-a", "record-b"]


def test_malformed_observations_are_rejected_without_echoing_input() -> None:
    secret = "SECRET-VALUE-THAT-MUST-NOT-APPEAR"
    with pytest.raises(ValidationError) as timestamp_error:
        observation("bad-1").model_copy(
            update={"occurred_at": datetime(2026, 1, 1)}  # noqa: DTZ001
        ).model_validate(
            {
                **observation("bad-1").model_dump(),
                "occurred_at": datetime(2026, 1, 1),  # noqa: DTZ001
                "attributes": {"note": secret},
            }
        )
    assert secret not in str(timestamp_error.value)

    with pytest.raises(ValidationError, match="unique after normalization"):
        CompanyObservation(
            **{
                **observation("bad-2").model_dump(),
                "attributes": {"Suite": "232", " suite ": "354"},
            }
        )

    with pytest.raises(ValidationError, match="control characters") as control_error:
        CompanyObservation(
            **{
                **observation("bad-3").model_dump(),
                "attributes": {"note": f"{secret}\x00"},
            }
        )
    assert secret not in str(control_error.value)


def test_memory_survives_reopen(tmp_path) -> None:
    path = tmp_path / "persistent.sqlite"
    with MemoryStore(path) as store:
        store.upsert(observation("persist-1"))

    with MemoryStore(path) as reopened:
        result = reopened.list_observations("tenant-alpha")

    assert [item.observation_id for item in result] == ["persist-1"]


def test_store_serializes_access_across_streamlit_rerun_threads(tmp_path) -> None:
    """A cached store remains usable when Streamlit creates a new script thread."""

    store = MemoryStore(tmp_path / "threaded.sqlite")
    failures: list[Exception] = []

    def write_and_read() -> None:
        try:
            item = observation("threaded-1")
            store.upsert(item)
            assert store.list_observations("tenant-alpha") == [item]
        except Exception as exc:  # noqa: BLE001  # pragma: no cover - asserted below
            failures.append(exc)

    worker = Thread(target=write_and_read)
    worker.start()
    worker.join(timeout=5)
    store.close()

    assert not worker.is_alive()
    assert failures == []


def test_duplicate_evidence_reference_cannot_manufacture_trusted_support(
    tmp_path,
) -> None:
    with MemoryStore(tmp_path / "replay.sqlite") as store:
        for number in range(1, 5):
            store.upsert(
                observation(
                    f"replay-{number}",
                    evidence_reference="synthetic://same-provider-record",
                )
            )
        summary = PatternAnalyzer(store).summarize_patterns("tenant-alpha")
        assessment = PatternAnalyzer(store).assess_candidate(
            observation("candidate-10")
        )

    address = next(
        item
        for item in summary.attribute_patterns
        if item.attribute_key == "address" and item.display_value == "76 New Avenue"
    )
    assert address.count == 4
    assert address.trusted_count == 1
    assert sum(item.counted_as_trusted_support for item in address.contributors) == 1
    assert assessment.outcome == PatternOutcome.REVIEW


def test_unconfigured_source_name_cannot_manufacture_trust(tmp_path) -> None:
    with MemoryStore(tmp_path / "unconfigured.sqlite") as store:
        for number in range(1, 4):
            store.upsert(
                observation(
                    f"invented-{number}",
                    source_type="invented_authority",
                    trust_score=1.0,
                )
            )
        candidate = observation(
            "candidate-4",
            source_type="invented_authority",
            trust_score=1.0,
        )
        summary = PatternAnalyzer(store).summarize_patterns("tenant-alpha")
        assessment = PatternAnalyzer(store).assess_candidate(candidate)

    address = next(
        item for item in summary.attribute_patterns if item.attribute_key == "address"
    )
    assert address.trusted_count == 0
    assert assessment.outcome == PatternOutcome.REVIEW
    assert "UNKNOWN_SOURCE" in {item.code for item in assessment.reasons}


def test_sqlite_payload_tampering_fails_closed(tmp_path) -> None:
    path = tmp_path / "tamper.sqlite"
    original = observation("tamper-1")
    with MemoryStore(path) as store:
        store.upsert(original)
        with sqlite3.connect(path) as attacker:
            attacker.execute(
                """
                UPDATE company_observations
                SET payload_json = replace(payload_json, '76 New Avenue', 'Altered')
                WHERE tenant_id = ? AND observation_id = ?
                """,
                (original.tenant_id, original.observation_id),
            )

        with pytest.raises(MemoryStoreError, match="integrity check failed"):
            store.list_observations("tenant-alpha")
        with pytest.raises(MemoryStoreError, match="integrity check failed"):
            store.upsert(original)


def test_human_correction_is_append_only_and_preserves_original(tmp_path) -> None:
    secret = "correction-secret-not-for-errors"
    with MemoryStore(tmp_path / "corrections.sqlite") as store:
        original = observation("target-1")
        store.upsert(original)
        correction = HumanCorrection(
            tenant_id="tenant-alpha",
            correction_id="correction-1",
            target_observation_id="target-1",
            submitted_at=datetime(2026, 5, 1, tzinfo=UTC),
            reviewer="human-reviewer",
            rationale="The newer confirmation names a different suite.",
            corrected_attributes={"suite": "354", "note": secret},
            evidence_reference="synthetic://human-confirmation-1",
        )

        assert store.append_correction(correction) == "inserted"
        assert store.append_correction(correction) == "unchanged"
        stored_original = store.list_observations("tenant-alpha")[0]
        stored_correction = store.list_corrections(
            "tenant-alpha", target_observation_id="target-1"
        )[0]
        summary_after_correction = PatternAnalyzer(store).summarize_patterns(
            "tenant-alpha"
        )

        changed = correction.model_copy(update={"rationale": "Changed rationale"})
        with pytest.raises(MemoryCollisionError) as caught:
            store.append_correction(changed)

    assert stored_original.attributes["suite"] == "232"
    assert stored_correction.corrected_attributes["suite"] == "354"
    assert any(
        item.display_value == "232"
        for item in summary_after_correction.attribute_patterns
    )
    assert all(
        item.display_value != "354"
        for item in summary_after_correction.attribute_patterns
    )
    assert secret not in str(caught.value)


def test_candidate_assessment_excludes_later_observations(tmp_path) -> None:
    with MemoryStore(tmp_path / "as-of.sqlite") as store:
        for number in range(1, 4):
            store.upsert(
                observation(
                    f"past-{number}",
                    occurred_at=datetime(2026, 1, number, tzinfo=UTC),
                    attributes={"address": "Past Place", "suite": "100"},
                )
            )
        for number in range(1, 5):
            store.upsert(
                observation(
                    f"future-{number}",
                    occurred_at=datetime(2026, 3, number, tzinfo=UTC),
                    attributes={"address": "Future Place", "suite": "900"},
                )
            )

        assessment = PatternAnalyzer(store).assess_candidate(
            observation(
                "candidate-as-of",
                occurred_at=datetime(2026, 2, 1, tzinfo=UTC),
                attributes={"address": "Past Place", "suite": "100"},
            )
        )

    assert assessment.outcome == PatternOutcome.ALLOW_LIKE
    assert assessment.history_observation_count == 3
    assert all(
        contributor.observation_id.startswith("past-")
        for pattern in assessment.matching_patterns
        for contributor in pattern.contributors
    )
    assert len(assessment.analyzer_fingerprint) == 64


def test_correction_cannot_cross_tenant_boundary(tmp_path) -> None:
    with MemoryStore(tmp_path / "correction-tenants.sqlite") as store:
        store.upsert(observation("alpha-only", tenant_id="tenant-alpha"))
        correction = HumanCorrection(
            tenant_id="tenant-beta",
            correction_id="correction-1",
            target_observation_id="alpha-only",
            submitted_at=datetime(2026, 5, 1, tzinfo=UTC),
            reviewer="human-reviewer",
            rationale="Synthetic cross-tenant correction attempt.",
            corrected_attributes={"suite": "354"},
            evidence_reference="synthetic://correction",
        )

        with pytest.raises(MemoryStoreError, match="unavailable for this tenant"):
            store.append_correction(correction)
        assert store.list_corrections("tenant-alpha") == []
        assert store.list_corrections("tenant-beta") == []
