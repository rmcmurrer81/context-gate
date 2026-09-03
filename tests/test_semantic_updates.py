"""Tests for the constrained deterministic quantitative-update interpreter."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from context_gate.semantic_updates import (
    CategorySemanticConfig,
    ContributionKind,
    CorrectedSemanticUpdateProposal,
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


def _config(*, maximum: int = 500, maximum_change: int = 200) -> CategorySemanticConfig:
    return CategorySemanticConfig(
        category="event",
        identity_keys=["event_name", "event_date"],
        important_fields=["crowd_size", "room_count"],
        quantity_fields=[
            QuantityFieldConfig(
                field_name="crowd_size",
                metric_nouns=[
                    "people",
                    "attendee",
                    "attendees",
                    "guests",
                    "crowd size",
                ],
                delta_markers=["more", "additional", "add"],
                total_markers=["total", "in all", "overall"],
                status_markers=["confirmed", "going", "attending", "registered"],
                maximum_plausible_value=maximum,
                maximum_absolute_change=maximum_change,
            )
        ],
    )


def _state(*, total: int | None = 35) -> EntityQuantityState:
    values = {} if total is None else {"crowd_size": total}
    return EntityQuantityState(
        category="event",
        entity_id="fictional-summit-2026-09-03",
        identity={"event_name": "Fictional Summit", "event_date": "2026-09-03"},
        quantity_values=values,
        as_of=NOW,
    )


def _statement(
    text: str,
    *,
    identity: dict[str, str | list[str]] | None = None,
    observed_at: datetime | None = None,
) -> IncomingQuantityStatement:
    return IncomingQuantityStatement(
        evidence_id="fictional-email-002",
        category="event",
        identity=identity
        or {"event_name": "Fictional Summit", "event_date": "2026-09-03"},
        text=text,
        observed_at=observed_at or NOW + timedelta(minutes=5),
    )


def _reason_codes(proposal: object) -> set[str]:
    return {reason.code for reason in proposal.reasons}  # type: ignore[attr-defined]


def test_initial_confirmed_quantity_establishes_total() -> None:
    statement = _statement(
        "Subject: attendance\nConfirmed attendees: 35\nSynthetic test receipt: R-100."
    )

    result = interpret_quantity_update(_config(), _state(total=None), statement)

    assert result.outcome == ProposalOutcome.PROPOSE
    assert result.mode == QuantityMode.TOTAL
    assert result.prior_total is None
    assert result.stated_quantity == 35
    assert result.proposed_total == 35
    assert result.identity_matched is True
    assert result.matched_identity == {
        "event_name": "Fictional Summit",
        "event_date": "2026-09-03",
    }
    assert result.field_is_important is True
    assert result.automatic_lookup_performed is False
    assert result.state_updated is False
    assert result.external_action_executed is False


def test_more_is_delta_and_adds_to_prior_total() -> None:
    statement = _statement("Update: 78 more people are confirmed.").model_copy(
        update={"evidence_reference": "mailbox://fictional/email-002"}
    )
    result = interpret_quantity_update(
        _config(),
        _state(total=35),
        statement,
    )

    assert result.outcome == ProposalOutcome.PROPOSE
    assert result.mode == QuantityMode.DELTA
    assert result.stated_quantity == 78
    assert result.prior_total == 35
    assert result.proposed_total == 113
    assert "INTERPRETED_AS_DELTA" in _reason_codes(result)
    assert result.calculation_trace is not None
    assert result.calculation_trace.formula == "35 + 78 = 113"
    contribution = result.calculation_trace.contributions[0]
    assert contribution.kind == ContributionKind.DETERMINISTIC_INTERPRETATION
    assert contribution.evidence_id == "fictional-email-002"
    assert contribution.evidence_reference == "mailbox://fictional/email-002"
    assert contribution.mode == QuantityMode.DELTA
    assert contribution.interpreted_excerpt == "Update: 78 more people are confirmed"
    assert len(contribution.content_digest) == 64


def test_going_is_replacement_total_not_delta() -> None:
    result = interpret_quantity_update(
        _config(),
        _state(total=35),
        _statement("The latest email says 78 people are going."),
    )

    assert result.outcome == ProposalOutcome.PROPOSE
    assert result.mode == QuantityMode.TOTAL
    assert result.stated_quantity == 78
    assert result.prior_total == 35
    assert result.proposed_total == 78
    assert "INTERPRETED_AS_TOTAL" in _reason_codes(result)


@pytest.mark.parametrize(
    ("identity", "expected_code"),
    [
        ({"event_name": "Fictional Summit"}, "IDENTITY_MISSING"),
        (
            {"event_name": "Different Summit", "event_date": "2026-09-03"},
            "IDENTITY_MISMATCH",
        ),
        (
            {
                "event_name": ["Fictional Summit", "Different Summit"],
                "event_date": "2026-09-03",
            },
            "IDENTITY_AMBIGUOUS",
        ),
    ],
)
def test_identity_uncertainty_prevents_a_proposal(
    identity: dict[str, str | list[str]],
    expected_code: str,
) -> None:
    result = interpret_quantity_update(
        _config(),
        _state(),
        _statement("78 attendees are going.", identity=identity),
    )

    assert result.outcome == ProposalOutcome.REVIEW
    assert result.identity_matched is False
    assert result.proposed_total is None
    assert expected_code in _reason_codes(result)
    assert result.human_questions


@pytest.mark.parametrize(
    ("text", "expected_code"),
    [
        (
            "The email says 35 people confirmed, but a receipt says 78 attendees confirmed.",
            "MULTIPLE_CONFLICTING_QUANTITIES",
        ),
        ("This 78-attendee notice was cancelled.", "NEGATED_OR_CANCELLED"),
        ("The crowd size may become 78.", "UNSUPPORTED_QUANTITY_WORDING"),
        ("The event is canceled; 78 people are confirmed.", "NEGATED_OR_CANCELLED"),
        ("The update lists attendees but no count.", "QUANTITY_MISSING"),
    ],
)
def test_ambiguous_or_negated_wording_requires_review(
    text: str,
    expected_code: str,
) -> None:
    result = interpret_quantity_update(_config(), _state(), _statement(text))

    assert result.outcome == ProposalOutcome.REVIEW
    assert result.mode == QuantityMode.AMBIGUOUS
    assert result.proposed_total is None
    assert expected_code in _reason_codes(result)
    assert result.human_questions


def test_delta_without_prior_total_requires_human_confirmation() -> None:
    result = interpret_quantity_update(
        _config(),
        _state(total=None),
        _statement("An additional 12 guests are registered."),
    )

    assert result.outcome == ProposalOutcome.REVIEW
    assert result.mode == QuantityMode.DELTA
    assert result.stated_quantity == 12
    assert result.prior_total is None
    assert result.proposed_total is None
    assert "PRIOR_TOTAL_MISSING" in _reason_codes(result)


@pytest.mark.parametrize(
    ("config", "text", "expected_code", "candidate"),
    [
        (
            _config(maximum=100),
            "150 people are going.",
            "PROPOSED_TOTAL_OUT_OF_BOUNDS",
            150,
        ),
        (
            _config(maximum_change=20),
            "78 people are going.",
            "CHANGE_EXCEEDS_LIMIT",
            78,
        ),
    ],
)
def test_configured_safety_bounds_route_to_review(
    config: CategorySemanticConfig,
    text: str,
    expected_code: str,
    candidate: int,
) -> None:
    result = interpret_quantity_update(config, _state(), _statement(text))

    assert result.outcome == ProposalOutcome.REVIEW
    assert result.proposed_total == candidate
    assert expected_code in _reason_codes(result)
    assert "approved, newer source" in result.human_questions[0]


def test_company_vocabulary_is_configurable() -> None:
    config = CategorySemanticConfig(
        category="shipment",
        identity_keys=["purchase_order"],
        important_fields=["carton_count"],
        quantity_fields=[
            QuantityFieldConfig(
                field_name="carton_count",
                metric_nouns=["cartons", "boxes"],
                delta_markers=["extra"],
                total_markers=["final total"],
                status_markers=["received"],
                negation_markers=["rejected"],
                maximum_plausible_value=1_000,
                maximum_absolute_change=100,
            )
        ],
    )
    state = EntityQuantityState(
        category="shipment",
        entity_id="po-fictional-7",
        identity={"purchase_order": "PO-FICTIONAL-7"},
        quantity_values={"carton_count": 20},
        as_of=NOW,
    )
    incoming = IncomingQuantityStatement(
        evidence_id="fictional-receipt-7",
        category="shipment",
        identity={"purchase_order": "po-fictional-7"},
        text="Warehouse receipt\n5 extra boxes received.",
        observed_at=NOW + timedelta(minutes=1),
    )

    result = interpret_quantity_update(config, state, incoming)

    assert result.outcome == ProposalOutcome.PROPOSE
    assert result.field_name == "carton_count"
    assert result.mode == QuantityMode.DELTA
    assert result.proposed_total == 25


def test_stale_statement_is_not_proposed() -> None:
    result = interpret_quantity_update(
        _config(),
        _state(),
        _statement("78 people are going.", observed_at=NOW - timedelta(seconds=1)),
    )

    assert result.outcome == ProposalOutcome.REVIEW
    assert result.proposed_total is None
    assert "STALE_STATEMENT" in _reason_codes(result)


@pytest.mark.parametrize(
    ("text", "expected_code"),
    [
        ("7.5 attendees are confirmed.", "NON_INTEGER_QUANTITY"),
        ("-5 more people are confirmed.", "UNSUPPORTED_DECREASE_WORDING"),
        ("−5 more people are confirmed.", "UNSUPPORTED_DECREASE_WORDING"),
        ("5 fewer people are confirmed.", "UNSUPPORTED_DECREASE_WORDING"),
        ("78 more people total.", "CONFLICTING_MODE_MARKERS"),
        ("1,000,000,000 people are confirmed.", "QUANTITY_EXCEEDS_PARSER_LIMIT"),
    ],
)
def test_unsafe_numeric_syntax_fails_closed(text: str, expected_code: str) -> None:
    result = interpret_quantity_update(_config(), _state(), _statement(text))

    assert result.outcome == ProposalOutcome.REVIEW
    assert result.mode == QuantityMode.AMBIGUOUS
    assert result.stated_quantity is None
    assert result.proposed_total is None
    assert expected_code in _reason_codes(result)


def test_human_correction_preserves_original_and_appends_exact_derivation() -> None:
    original = interpret_quantity_update(
        _config(),
        _state(),
        _statement("The crowd size may become 78."),
    )
    correction = HumanQuantityCorrection(
        correction_id="correction-001",
        field_name="crowd_size",
        mode=QuantityMode.DELTA,
        quantity=78,
        reviewer="fictional-reviewer",
        rationale="The sender explicitly confirmed this means 78 additional people.",
        created_at=NOW + timedelta(minutes=10),
        evidence_reference="review://correction-001",
    )

    corrected = apply_human_correction(_config(), _state(), original, correction)

    assert isinstance(corrected, CorrectedSemanticUpdateProposal)
    assert corrected.original_proposal == original
    assert corrected.corrections == [correction]
    assert corrected.outcome == ProposalOutcome.PROPOSE
    assert corrected.mode == QuantityMode.DELTA
    assert corrected.prior_total == 35
    assert corrected.proposed_total == 113
    assert corrected.calculation_trace is not None
    assert corrected.calculation_trace.formula == "35 + 78 = 113"
    assert (
        corrected.calculation_trace.contributions[-1].kind
        == ContributionKind.HUMAN_CORRECTION
    )
    assert (
        corrected.calculation_trace.contributions[-1].evidence_reference
        == "review://correction-001"
    )
    assert corrected.automatic_lookup_performed is False
    assert corrected.state_updated is False
    assert corrected.external_action_executed is False


def test_second_human_correction_appends_without_overwriting_first() -> None:
    state = _state()
    original = interpret_quantity_update(
        _config(),
        state,
        _statement("78 people are going."),
    )
    first = HumanQuantityCorrection(
        correction_id="correction-001",
        field_name="crowd_size",
        mode=QuantityMode.DELTA,
        quantity=78,
        reviewer="fictional-reviewer",
        rationale="First correction for a synthetic test.",
        created_at=NOW + timedelta(minutes=10),
    )
    second = HumanQuantityCorrection(
        correction_id="correction-002",
        field_name="crowd_size",
        mode=QuantityMode.TOTAL,
        quantity=78,
        reviewer="fictional-supervisor",
        rationale="The source clarified that 78 is the complete total.",
        created_at=NOW + timedelta(minutes=20),
    )

    once = apply_human_correction(_config(), state, original, first)
    twice = apply_human_correction(_config(), state, once, second)

    assert twice.original_proposal == original
    assert twice.corrections == [first, second]
    assert twice.proposed_total == 78
    assert twice.calculation_trace is not None
    assert twice.calculation_trace.formula == "TOTAL 78 = 78"
    assert len(twice.calculation_trace.contributions) == 3


def test_correction_cannot_bypass_unverified_identity() -> None:
    state = _state()
    original = interpret_quantity_update(
        _config(),
        state,
        _statement(
            "78 people are going.",
            identity={"event_name": "Different Summit", "event_date": "2026-09-03"},
        ),
    )
    correction = HumanQuantityCorrection(
        correction_id="correction-identity-001",
        field_name="crowd_size",
        mode=QuantityMode.TOTAL,
        quantity=78,
        reviewer="fictional-reviewer",
        rationale="Corrected only the numeric interpretation.",
        created_at=NOW + timedelta(minutes=10),
    )

    corrected = apply_human_correction(_config(), state, original, correction)

    assert corrected.outcome == ProposalOutcome.REVIEW
    assert corrected.proposed_total is None
    assert "IDENTITY_NOT_VERIFIED_AFTER_CORRECTION" in _reason_codes(corrected)


def test_correction_cannot_rebind_original_to_different_state_snapshot() -> None:
    original_state = _state(total=35)
    original = interpret_quantity_update(
        _config(),
        original_state,
        _statement("78 people are going."),
    )
    changed_state = EntityQuantityState(
        category="event",
        entity_id=original_state.entity_id,
        identity=original_state.identity,
        quantity_values={"crowd_size": 40},
        as_of=NOW,
    )
    correction = HumanQuantityCorrection(
        correction_id="correction-state-001",
        field_name="crowd_size",
        mode=QuantityMode.DELTA,
        quantity=78,
        reviewer="fictional-reviewer",
        rationale="Synthetic correction against the wrong snapshot.",
        created_at=NOW + timedelta(minutes=10),
    )

    corrected = apply_human_correction(
        _config(),
        changed_state,
        original,
        correction,
    )

    assert corrected.outcome == ProposalOutcome.REVIEW
    assert corrected.identity_matched is False
    assert corrected.proposed_total is None
    assert "CURRENT_STATE_MISMATCH_AFTER_CORRECTION" in _reason_codes(corrected)


def test_strict_models_reject_naive_time_extra_fields_and_bad_configuration() -> None:
    with pytest.raises(ValidationError, match="explicit timezone"):
        _statement(
            "35 people are confirmed.",
            observed_at=NOW.replace(tzinfo=None),
        )

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        IncomingQuantityStatement.model_validate(
            {
                "evidence_id": "fictional-email",
                "category": "event",
                "identity": {},
                "text": "35 people are confirmed.",
                "observed_at": NOW,
                "secret": "must not be accepted",
            }
        )

    with pytest.raises(ValidationError, match="delta and total markers"):
        QuantityFieldConfig(
            field_name="crowd_size",
            metric_nouns=["people"],
            delta_markers=["now"],
            total_markers=["NOW"],
            status_markers=["confirmed"],
            maximum_plausible_value=100,
            maximum_absolute_change=50,
        )
