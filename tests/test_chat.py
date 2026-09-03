from __future__ import annotations

from collections import Counter

import pytest
from pydantic import ValidationError

from context_gate.chat import (
    MAX_CITED_CASES,
    MAX_HISTORY_MESSAGES,
    GroundedChatAnswer,
    GroundedChatEngine,
    answer_question,
)
from context_gate.models import EnforcementDecision
from context_gate.scenario import get_scenario, scenario_names


def case_id(name: str) -> str:
    return get_scenario(name).case_id


def test_explanation_is_grounded_in_selected_case() -> None:
    answer = answer_question("Why was case conflict blocked?")

    assert answer.abstained is False
    assert answer.case_ids == [case_id("conflict")]
    assert "BLOCK" in answer.text
    assert "CG-002-LOWER-AUTHORITY-CONFLICT" in answer.rule_ids
    assert set(answer.evidence_event_ids) == {"evt-100", "evt-104"}


def test_case_can_be_selected_by_title() -> None:
    title = get_scenario("missing-provenance").title

    answer = answer_question(f"Explain {title}")

    assert answer.abstained is False
    assert answer.case_ids == [case_id("missing-provenance")]
    assert "REVIEW" in answer.text


def test_short_followup_uses_recent_bounded_context() -> None:
    history = [
        {"role": "user", "content": "Explain case conflict."},
        {
            "role": "assistant",
            "content": "That action was blocked.",
            "case_ids": ["conflict"],
        },
    ]

    answer = answer_question("What should happen next?", history)

    assert answer.abstained is False
    assert answer.case_ids == [case_id("conflict")]
    assert "Do not execute" in answer.text


def test_history_older_than_twelve_messages_is_not_used() -> None:
    history = ["Explain case conflict."] + [
        f"unrelated turn {index}" for index in range(MAX_HISTORY_MESSAGES)
    ]

    answer = answer_question("Why?", history)

    assert answer.abstained is True
    assert answer.case_ids == []


def test_comparison_cites_both_cases_and_their_rules() -> None:
    answer = answer_question("Compare case conflict with case missing-provenance.")

    assert answer.abstained is False
    assert set(answer.case_ids) == {
        case_id("conflict"),
        case_id("missing-provenance"),
    }
    assert "CG-002-LOWER-AUTHORITY-CONFLICT" in answer.rule_ids
    assert "CG-005-REQUIRED-PROVENANCE" in answer.rule_ids
    assert "human approval=yes" in answer.text


def test_evidence_answer_cites_only_real_case_event_ids() -> None:
    engine = GroundedChatEngine()
    answer = engine.answer("Show the sources for case conflict.")
    case = next(item for item in engine.cases if item.name == "conflict")
    known_ids = {event.event_id for event in case.events}

    assert answer.abstained is False
    assert set(answer.evidence_event_ids) <= known_ids
    assert "[event evt-100]" in answer.text
    assert "effective trust" in answer.text


def test_catalog_pattern_counts_are_computed_from_live_index() -> None:
    engine = GroundedChatEngine()
    expected = Counter(case.decision.decision.value for case in engine.cases)

    answer = engine.answer("Show the overall outcome breakdown and rule patterns.")

    assert answer.abstained is False
    assert f"{len(engine.cases)} cases" in answer.text
    for outcome in EnforcementDecision:
        assert f"{outcome.value}={expected[outcome.value]}" in answer.text
    assert set(answer.rule_ids) == {
        rule for case in engine.cases for rule in case.decision.deterministic_rule_ids
    }
    assert len(answer.case_ids) <= MAX_CITED_CASES


def test_named_rule_query_counts_matching_cases() -> None:
    engine = GroundedChatEngine()
    rule = "CG-002-LOWER-AUTHORITY-CONFLICT"
    expected = sum(
        rule in case.decision.deterministic_rule_ids for case in engine.cases
    )

    answer = engine.answer(f"How many cases use {rule}?")

    assert answer.abstained is False
    assert f"{expected} case(s) use {rule}" in answer.text
    assert answer.rule_ids == [rule]
    assert len(answer.case_ids) <= MAX_CITED_CASES


def test_outcome_query_lists_at_most_three_matching_cases() -> None:
    engine = GroundedChatEngine()

    answer = engine.answer("Which cases are blocked?")

    assert answer.abstained is False
    assert 1 <= len(answer.case_ids) <= MAX_CITED_CASES
    indexed = {case.case_id: case for case in engine.cases}
    assert all(
        indexed[case_id].decision.decision == EnforcementDecision.BLOCK
        for case_id in answer.case_ids
    )


@pytest.mark.parametrize(
    ("question", "outcome"),
    [
        ("Why are there so many red items?", EnforcementDecision.BLOCK),
        ("Go over what was blocked and why.", EnforcementDecision.BLOCK),
        ("Show the stopped items.", EnforcementDecision.BLOCK),
        ("What failed?", EnforcementDecision.BLOCK),
        ("Which amber cases need attention?", EnforcementDecision.REVIEW),
        ("Why are the yellow items needing attention?", EnforcementDecision.REVIEW),
        ("Go over what passed and why.", EnforcementDecision.ALLOW),
        ("Show every green case.", EnforcementDecision.ALLOW),
    ],
)
def test_color_and_operator_language_selects_complete_outcome_queue(
    question: str,
    outcome: EnforcementDecision,
) -> None:
    engine = GroundedChatEngine()
    expected = [case for case in engine.cases if case.decision.decision == outcome]

    answer = engine.answer(question)

    assert answer.abstained is False
    assert answer.case_ids == [case.case_id for case in expected]
    assert len(answer.case_ids) <= MAX_CITED_CASES
    assert set(answer.evidence_event_ids) == {
        event_id for case in expected for event_id in case.decision.evidence_event_ids
    }
    assert set(answer.rule_ids) == {
        rule for case in expected for rule in case.decision.deterministic_rule_ids
    }
    assert "Evidence:" in answer.text
    assert "Rules:" in answer.text
    for case in expected:
        assert f"[case {case.case_id}]" in answer.text


def test_color_word_on_explicit_case_does_not_broaden_to_whole_queue() -> None:
    answer = answer_question("Why was case B1 red?")

    assert answer.abstained is False
    assert answer.case_ids == ["B1"]
    assert "BLOCK" in answer.text


def test_related_cases_preserve_three_case_citation_cap() -> None:
    answer = answer_question("Which cases are related to case conflict?")

    assert answer.abstained is False
    assert answer.case_ids[0] == case_id("conflict")
    assert len(answer.case_ids) <= MAX_CITED_CASES


def test_unsupported_question_abstains_without_fake_citations() -> None:
    answer = answer_question("What will the weather be next Thursday?")

    assert answer.abstained is True
    assert answer.case_ids == []
    assert answer.evidence_event_ids == []
    assert answer.rule_ids == []
    assert "cannot answer" in answer.text
    assert "will not invent" in answer.text


def test_answer_schema_forbids_extra_fields_and_duplicate_citations() -> None:
    valid = answer_question("Explain case safe.").model_dump()

    with pytest.raises(ValidationError):
        GroundedChatAnswer.model_validate({**valid, "unexpected": True})
    with pytest.raises(ValidationError):
        GroundedChatAnswer.model_validate({**valid, "case_ids": ["safe", "safe"]})


def test_engine_indexes_every_scenario_dynamically() -> None:
    engine = GroundedChatEngine()

    assert {case.name for case in engine.cases} == set(scenario_names())


def test_every_engine_citation_exists_in_the_indexed_cases() -> None:
    engine = GroundedChatEngine()
    known_cases = {case.case_id: case for case in engine.cases}

    answer = engine.answer("Which cases are blocked?")

    assert set(answer.case_ids) <= set(known_cases)
    cited_cases = [known_cases[case_id] for case_id in answer.case_ids]
    assert set(answer.evidence_event_ids) <= {
        event.event_id for case in cited_cases for event in case.events
    }
    assert set(answer.rule_ids) <= {
        rule for case in cited_cases for rule in case.decision.deterministic_rule_ids
    }


def test_history_mapping_can_carry_structured_case_citations() -> None:
    history = [
        {
            "role": "assistant",
            "content": "Prior structured answer",
            "case_ids": ["safe"],
        }
    ]

    answer = answer_question("Tell me more about it.", history)

    assert answer.abstained is False
    assert answer.case_ids == [case_id("safe")]
