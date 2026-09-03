"""Run ContextGate's deterministic, fictional real-world acceptance matrix.

This script performs no network calls and no durable writes. Company memory uses
an in-memory SQLite database that is closed before the script exits.
"""

from __future__ import annotations

import sys
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))

from context_gate.company_memory import (
    CompanyObservation,
    MemoryStore,
    PatternAnalyzer,
    PatternOutcome,
)
from context_gate.decision_engine import evaluate_request
from context_gate.models import (
    EnforcementDecision,
    EvidenceStatus,
    Sensitivity,
)
from context_gate.operator_learning import (
    DecisionCorrection,
    DecisionCorrectionRetraction,
    GuidanceOrigin,
    GuidanceRetraction,
    OperatorGuidance,
    OperatorLearningStore,
)
from context_gate.policy_config import DEFAULT_POLICY
from context_gate.scenario import Scenario, get_scenario, iter_scenarios
from context_gate.semantic_updates import (
    CategorySemanticConfig,
    ContributionKind,
    EntityQuantityState,
    HumanQuantityCorrection,
    IncomingQuantityStatement,
    ProposalOutcome,
    QuantityFieldConfig,
    QuantityMode,
    apply_human_correction,
    interpret_quantity_update,
)

NOW = datetime(2026, 9, 2, 14, 0, tzinfo=UTC)
TENANT_ID = "fictional-company"
CATEGORY = "company_event"


class AcceptanceFailure(AssertionError):
    """A concise assertion failure suitable for the command-line matrix."""


def _require(condition: bool, detail: str) -> None:
    if not condition:
        raise AcceptanceFailure(detail)


def _check_scenario(scenario: Scenario) -> str:
    events, request = scenario.load()
    result = evaluate_request(
        events,
        request,
        run_id=f"acceptance-{scenario.case_id.casefold()}",
        policy=DEFAULT_POLICY,
    )
    _require(
        result.classification == scenario.expected_classification,
        f"{scenario.case_id} classification differed from its catalog contract",
    )
    _require(
        result.decision == scenario.expected_decision,
        f"{scenario.case_id} decision differed from its catalog contract",
    )
    _require(not result.model_explanation_used, "a model was unexpectedly used")
    return (
        f"{scenario.case_id} {scenario.name}: "
        f"{result.classification.value} / {result.decision.value}"
    )


def _observation(
    observation_id: str,
    *,
    day: int,
    address: str,
    suite: str,
) -> CompanyObservation:
    return CompanyObservation(
        tenant_id=TENANT_ID,
        observation_id=observation_id,
        category=CATEGORY,
        occurred_at=datetime(2026, 8, day, 12, 0, tzinfo=UTC),
        attributes={"address": address, "suite": suite},
        source_type="official_email",
        trust_score=0.95,
        status=EvidenceStatus.CONFIRMED,
        sensitivity=Sensitivity.INTERNAL,
        evidence_reference=f"synthetic-email://{observation_id}",
    )


def _check_company_memory() -> str:
    with MemoryStore(":memory:") as store:
        for number in range(1, 4):
            store.upsert(
                _observation(
                    f"main-{number}",
                    day=number,
                    address="35 Main St",
                    suite="110",
                )
            )
        for number in range(1, 9):
            store.upsert(
                _observation(
                    f"avenue-{number}",
                    day=number + 3,
                    address="76 New Avenue",
                    suite="232",
                )
            )

        analyzer = PatternAnalyzer(store, policy=DEFAULT_POLICY)
        summary = analyzer.summarize_patterns(TENANT_ID, category=CATEGORY)
        address_counts = {
            pattern.normalized_value: pattern.count
            for pattern in summary.attribute_patterns
            if pattern.attribute_key.casefold() == "address"
        }
        _require(address_counts.get("35 main st") == 3, "35 Main St count was not 3")
        _require(
            address_counts.get("76 new avenue") == 8,
            "76 New Avenue count was not 8",
        )
        suite_pattern = next(
            (
                pattern
                for pattern in summary.conditional_patterns
                if pattern.parent_display_value == "76 New Avenue"
                and pattern.child_display_value == "232"
            ),
            None,
        )
        _require(suite_pattern is not None, "Suite 232 pattern was missing")
        assert suite_pattern is not None
        _require(suite_pattern.count == 8, "Suite 232 count was not 8")
        _require(
            len(suite_pattern.contributors) == 8,
            "Suite 232 contributor trace was incomplete",
        )
        _require(
            all(item.evidence_reference for item in suite_pattern.contributors),
            "a contributor lacked an evidence reference",
        )

        before_ids = {
            item.observation_id for item in store.list_observations(TENANT_ID)
        }
        candidate = _observation(
            "candidate-suite-354",
            day=20,
            address="76 New Avenue",
            suite="354",
        )
        assessment = analyzer.assess_candidate(candidate)
        after_ids = {item.observation_id for item in store.list_observations(TENANT_ID)}

    reason_codes = {reason.code for reason in assessment.reasons}
    _require(assessment.outcome == PatternOutcome.REVIEW, "Suite 354 was not REVIEW")
    _require(
        "NEW_SUITE_AT_KNOWN_ADDRESS" in reason_codes,
        "Suite 354 anomaly reason was missing",
    )
    _require(
        any("232" in item and "354" in item for item in assessment.human_questions),
        "Suite 354 confirmation question did not show both suites",
    )
    comparison = next(
        (
            pattern
            for pattern in assessment.comparison_patterns
            if pattern.attribute_key == "suite at address"
        ),
        None,
    )
    _require(comparison is not None, "Suite 354 comparison trace was missing")
    assert comparison is not None
    _require(comparison.trusted_count == 8, "comparison trace did not show 8 sources")
    _require(
        len(comparison.contributors) == 8,
        "comparison contributor trace was incomplete",
    )
    _require(
        before_ids == after_ids, "candidate assessment unexpectedly changed memory"
    )
    _require(not assessment.candidate_stored, "candidate was unexpectedly stored")
    _require(not assessment.action_executed, "candidate assessment executed an action")
    return "memory: 35 Main St=3; 76 New Avenue Suite 232=8; Suite 354=REVIEW with 8-source trace"


def _semantic_config() -> CategorySemanticConfig:
    return CategorySemanticConfig(
        category="event",
        identity_keys=["event_name", "event_date"],
        important_fields=["crowd_size"],
        quantity_fields=[
            QuantityFieldConfig(
                field_name="crowd_size",
                metric_nouns=["people", "attendee", "attendees", "crowd size"],
                delta_markers=["more", "additional", "add"],
                total_markers=["total", "in all", "overall"],
                status_markers=["confirmed", "going", "attending", "registered"],
                maximum_plausible_value=500,
                maximum_absolute_change=200,
            )
        ],
    )


def _semantic_state() -> EntityQuantityState:
    return EntityQuantityState(
        category="event",
        entity_id="fictional-summit-2026-09-03",
        identity={"event_name": "Fictional Summit", "event_date": "2026-09-03"},
        quantity_values={"crowd_size": 35},
        as_of=NOW,
    )


def _statement(
    evidence_id: str,
    text: str,
    *,
    event_name: str = "Fictional Summit",
    source_type: str = "official_email",
) -> IncomingQuantityStatement:
    return IncomingQuantityStatement(
        evidence_id=evidence_id,
        category="event",
        identity={"event_name": event_name, "event_date": "2026-09-03"},
        source_type=source_type,
        evidence_reference=f"synthetic-{source_type}://{evidence_id}",
        text=text,
        observed_at=NOW + timedelta(minutes=5),
    )


def _check_delta_semantics() -> str:
    proposal = interpret_quantity_update(
        _semantic_config(),
        _semantic_state(),
        _statement("attendance-delta", "Update: 78 more people are confirmed."),
    )
    _require(proposal.outcome == ProposalOutcome.PROPOSE, "delta did not propose")
    _require(proposal.mode == QuantityMode.DELTA, "'78 more' was not a delta")
    _require(proposal.proposed_total == 113, "35 + 78 did not equal 113")
    _require(
        proposal.calculation_trace.formula == "35 + 78 = 113",
        "delta calculation trace was incorrect",
    )
    return "semantic delta: 35 + '78 more' = 113 with source trace"


def _check_total_semantics() -> str:
    proposal = interpret_quantity_update(
        _semantic_config(),
        _semantic_state(),
        _statement("attendance-total", "The latest update says 78 people are going."),
    )
    _require(proposal.outcome == ProposalOutcome.PROPOSE, "total did not propose")
    _require(proposal.mode == QuantityMode.TOTAL, "'78 people are going' was not total")
    _require(proposal.proposed_total == 78, "replacement total was not 78")
    _require(
        proposal.calculation_trace.formula == "TOTAL 78 = 78",
        "total calculation trace was incorrect",
    )
    return "semantic total: '78 people are going' replaces 35 with 78"


def _check_entity_mismatch() -> str:
    proposal = interpret_quantity_update(
        _semantic_config(),
        _semantic_state(),
        _statement(
            "different-event",
            "78 attendees are going.",
            event_name="Different Fictional Summit",
        ),
    )
    codes = {reason.code for reason in proposal.reasons}
    _require(
        proposal.outcome == ProposalOutcome.REVIEW, "different event was not REVIEW"
    )
    _require("IDENTITY_MISMATCH" in codes, "identity mismatch reason was missing")
    _require(
        proposal.proposed_total is None, "different event produced a candidate total"
    )
    return "entity verification: different event=REVIEW with no candidate update"


def _check_ambiguous_receipt() -> str:
    proposal = interpret_quantity_update(
        _semantic_config(),
        _semantic_state(),
        _statement(
            "receipt-no-count",
            "Fictional receipt lists attendees but no count.",
            source_type="receipt",
        ),
    )
    codes = {reason.code for reason in proposal.reasons}
    _require(
        proposal.outcome == ProposalOutcome.REVIEW, "no-number receipt was not REVIEW"
    )
    _require(
        "QUANTITY_MISSING" in codes,
        "no-number receipt did not explain the missing quantity",
    )
    _require(proposal.proposed_total is None, "no-number receipt fabricated a total")
    return "ambiguous receipt: no number=REVIEW; no total fabricated"


def _check_human_correction() -> str:
    config = _semantic_config()
    state = _semantic_state()
    original = interpret_quantity_update(
        config,
        state,
        _statement("ambiguous-attendance", "The crowd size may become 78."),
    )
    _require(
        original.outcome == ProposalOutcome.REVIEW, "ambiguous original was not REVIEW"
    )
    original_digest = original.input_digest
    original_reference = original.evidence_reference
    original_contribution = original.calculation_trace.contributions[0]
    correction = HumanQuantityCorrection(
        correction_id="human-correction-001",
        field_name="crowd_size",
        mode=QuantityMode.DELTA,
        quantity=78,
        reviewer="fictional-authorized-reviewer",
        rationale="The fictional sender confirmed that 78 means additional attendees.",
        created_at=NOW + timedelta(minutes=10),
        evidence_reference="synthetic-review://human-correction-001",
    )
    corrected = apply_human_correction(config, state, original, correction)

    _require(
        corrected.outcome == ProposalOutcome.PROPOSE,
        "correction did not produce a proposal",
    )
    _require(corrected.proposed_total == 113, "correction did not recalculate 35 + 78")
    _require(
        corrected.original_proposal == original, "original proposal was not preserved"
    )
    _require(
        corrected.original_proposal.input_digest == original_digest
        and corrected.original_proposal.evidence_reference == original_reference,
        "original evidence identity was not preserved",
    )
    _require(
        corrected.calculation_trace.contributions[0] == original_contribution,
        "original calculation contribution was not preserved",
    )
    _require(
        corrected.calculation_trace.contributions[-1].kind
        == ContributionKind.HUMAN_CORRECTION,
        "human correction contribution was not appended",
    )
    _require(
        corrected.calculation_trace.formula == "35 + 78 = 113",
        "corrected calculation trace was incorrect",
    )
    return "human correction: original evidence preserved; corrected delta recalculates to 113"


def _check_operator_guidance_retraction() -> str:
    guidance = OperatorGuidance(
        tenant_id=TENANT_ID,
        guidance_id="acceptance-guidance-b1",
        origin=GuidanceOrigin.REVIEW,
        source_record_id="acceptance-review-b1",
        created_at=NOW + timedelta(minutes=20),
        guidance="For B1, verify a changed suite against an exact organizer source.",
        case_ids=["B1"],
    )
    original_payload = guidance.model_dump(mode="json")
    retraction = GuidanceRetraction(
        tenant_id=TENANT_ID,
        retraction_id="acceptance-guidance-retraction-b1",
        guidance_id=guidance.guidance_id,
        retracted_at=NOW + timedelta(minutes=21),
        actor="fictional-authorized-reviewer",
        reason="The operator withdrew this fictional guidance after rechecking it.",
    )

    with OperatorLearningStore(":memory:") as store:
        _require(
            store.append_guidance(guidance) == "inserted",
            "explicit operator guidance was not inserted",
        )
        matches = store.find_relevant_guidance(
            TENANT_ID,
            case_id="B1",
            text="Which organizer should verify the suite?",
        )
        _require(len(matches) == 1, "explicit operator guidance was not retrieved")
        _require(
            matches[0].guidance == guidance,
            "retrieved operator guidance differed from the immutable original",
        )
        _require(
            matches[0].matched_case_ids == ["B1"],
            "operator guidance did not explain its case match",
        )
        _require(
            store.append_retraction(retraction) == "inserted",
            "operator guidance retraction was not appended",
        )
        _require(
            store.append_retraction(retraction) == "unchanged",
            "identical operator guidance retraction was not idempotent",
        )
        _require(
            store.list_active_guidance(TENANT_ID) == [],
            "retracted operator guidance remained active",
        )
        _require(
            store.find_relevant_guidance(TENANT_ID, case_id="B1") == [],
            "retracted operator guidance remained retrievable",
        )
        _require(
            store.list_retractions(TENANT_ID) == [retraction],
            "append-only operator guidance tombstone was not retained",
        )
    _require(
        guidance.model_dump(mode="json") == original_payload,
        "operator guidance was mutated by its retraction",
    )
    return (
        "operator guidance: explicit B1 guidance retrieved, then append-only retracted"
    )


def _check_decision_correction_retraction() -> str:
    events, request = get_scenario("conflict").load()
    original = evaluate_request(
        events,
        request,
        run_id="acceptance-operator-correction-b1",
        policy=DEFAULT_POLICY,
    )
    _require(
        original.decision == EnforcementDecision.BLOCK,
        "decision correction fixture did not begin at BLOCK",
    )
    original_payload = original.model_dump(mode="json")
    correction = DecisionCorrection(
        tenant_id=TENANT_ID,
        correction_id="acceptance-decision-correction-b1",
        case_id="B1",
        original_decision_id=original.decision_id,
        request_fingerprint=original.request_digest,
        evidence_fingerprint=original.evidence_digest,
        policy_fingerprint=original.policy_fingerprint,
        original_outcome=original.decision,
        corrected_outcome=EnforcementDecision.ALLOW,
        created_at=NOW + timedelta(minutes=30),
        reviewer="fictional-authorized-reviewer",
        rationale="The fictional reviewer confirmed the BLOCK was a false positive.",
    )
    retraction = DecisionCorrectionRetraction(
        tenant_id=TENANT_ID,
        retraction_id="acceptance-decision-correction-retraction-b1",
        correction_id=correction.correction_id,
        retracted_at=NOW + timedelta(minutes=31),
        actor="fictional-authorized-reviewer",
        reason="Withdraw the fictional corrected outcome after a second review.",
    )
    lookup = {
        "case_id": correction.case_id,
        "request_fingerprint": correction.request_fingerprint,
        "evidence_fingerprint": correction.evidence_fingerprint,
        "policy_fingerprint": correction.policy_fingerprint,
    }

    with OperatorLearningStore(":memory:") as store:
        _require(
            store.append_decision_correction(correction) == "inserted",
            "decision correction was not inserted",
        )
        effective = store.latest_active_decision_correction(TENANT_ID, **lookup)
        _require(effective is not None, "active decision correction was not found")
        assert effective is not None
        _require(
            effective.original_outcome == EnforcementDecision.BLOCK
            and effective.effective_outcome == EnforcementDecision.ALLOW,
            "decision correction did not expose advisory BLOCK to ALLOW",
        )
        _require(
            effective.resolution_status == "RESOLVED",
            "decision correction did not expose resolved status",
        )
        _require(
            not effective.action_executed,
            "advisory decision correction unexpectedly executed an action",
        )
        _require(
            original.model_dump(mode="json") == original_payload,
            "decision correction mutated the original receipt",
        )
        _require(
            store.append_decision_correction_retraction(retraction) == "inserted",
            "decision correction retraction was not appended",
        )
        _require(
            store.append_decision_correction_retraction(retraction) == "unchanged",
            "identical decision correction retraction was not idempotent",
        )
        _require(
            store.latest_active_decision_correction(TENANT_ID, **lookup) is None,
            "retraction did not restore no active decision correction",
        )
        _require(
            store.list_decision_correction_retractions(TENANT_ID) == [retraction],
            "append-only decision correction tombstone was not retained",
        )
    _require(
        original.model_dump(mode="json") == original_payload,
        "decision receipt changed after correction retraction",
    )
    return (
        "decision correction: advisory BLOCK->ALLOW resolved with original receipt "
        "preserved and no action; retraction restored no active correction"
    )


def _run_check(label: str, check: Callable[[], str]) -> bool:
    try:
        detail = check()
    except Exception as exc:  # noqa: BLE001 - matrix must report all failed checks
        print(f"FAIL {label}: {type(exc).__name__}: {exc}")
        return False
    print(f"PASS {detail}")
    return True


def main() -> int:
    """Run every acceptance check and return zero only if all checks pass."""

    results: list[bool] = []
    scenarios = tuple(iter_scenarios())
    for scenario in scenarios:
        results.append(
            _run_check(
                f"Pattern Lab {scenario.case_id}",
                lambda scenario=scenario: _check_scenario(scenario),
            )
        )

    if all(results):
        decisions = Counter(scenario.expected_decision for scenario in scenarios)
        expected = {
            EnforcementDecision.ALLOW: 3,
            EnforcementDecision.REVIEW: 3,
            EnforcementDecision.BLOCK: 3,
        }
        if decisions == expected:
            print("PASS Pattern Lab summary: 3 ALLOW / 3 REVIEW / 3 BLOCK")
        else:
            print("FAIL Pattern Lab summary: catalog distribution changed")
            results.append(False)

    results.extend(
        [
            _run_check("company memory", _check_company_memory),
            _run_check("semantic delta", _check_delta_semantics),
            _run_check("semantic total", _check_total_semantics),
            _run_check("entity mismatch", _check_entity_mismatch),
            _run_check("ambiguous receipt", _check_ambiguous_receipt),
            _run_check("human correction", _check_human_correction),
            _run_check(
                "operator guidance retraction", _check_operator_guidance_retraction
            ),
            _run_check(
                "decision correction retraction",
                _check_decision_correction_retraction,
            ),
        ]
    )
    passed = sum(results)
    total = len(results)
    if passed == total:
        print(f"PASS acceptance matrix complete: {passed}/{total} checks")
        return 0
    print(f"FAIL acceptance matrix: {passed}/{total} checks passed")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
