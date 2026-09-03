from __future__ import annotations

import json
from collections import Counter

import pytest

from context_gate.__main__ import main
from context_gate.decision_engine import evaluate_request
from context_gate.models import Classification, EnforcementDecision
from context_gate.scenario import (
    SCENARIO_CATALOG,
    get_scenario,
    iter_scenarios,
    load_demo_events,
    load_demo_request,
    load_scenario,
    scenario_names,
)


@pytest.mark.parametrize(
    ("name", "classification", "decision"),
    [
        ("conflict", Classification.CONFLICT, EnforcementDecision.BLOCK),
        ("safe", Classification.SAFE, EnforcementDecision.ALLOW),
        (
            "missing-provenance",
            Classification.INSUFFICIENT_EVIDENCE,
            EnforcementDecision.REVIEW,
        ),
    ],
)
def test_scenario_reaches_documented_outcome(
    name: str,
    classification: Classification,
    decision: EnforcementDecision,
) -> None:
    events, request = load_scenario(name)

    result = evaluate_request(events, request, run_id=f"test-{name}")

    assert result.classification == classification
    assert result.decision == decision
    assert get_scenario(name).expected_classification == classification
    assert get_scenario(name).expected_decision == decision


def test_catalog_loads_fresh_inputs() -> None:
    first_events, first_request = load_scenario("safe")
    second_events, second_request = load_scenario("safe")

    assert first_events == second_events
    assert first_events[0] is not second_events[0]
    assert first_request == second_request
    assert first_request is not second_request


def test_original_demo_loaders_still_back_conflict_scenario() -> None:
    events, request = load_scenario("conflict")

    assert events == load_demo_events()
    assert request == load_demo_request()


def test_short_case_id_loads_the_same_scenario() -> None:
    assert load_scenario("B1") == load_scenario("conflict")
    assert load_scenario("a1") == load_scenario("safe")


def test_unknown_scenario_lists_valid_names() -> None:
    with pytest.raises(ValueError, match="conflict, safe, missing-provenance"):
        load_scenario("does-not-exist")


def test_cli_lists_scenarios_as_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["list", "--json"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert [item["name"] for item in output] == list(scenario_names())
    assert {item["expected_decision"] for item in output} == {
        "ALLOW",
        "BLOCK",
        "REVIEW",
    }


def test_cli_runs_selected_scenario(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "safe", "--run-id", "cli-test"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["run_id"] == "cli-test"
    assert output["classification"] == "SAFE"
    assert output["decision"] == "ALLOW"
    assert output["requires_human_approval"] is False


def test_catalog_is_read_only() -> None:
    with pytest.raises(TypeError):
        SCENARIO_CATALOG["replacement"] = get_scenario("safe")  # type: ignore[index]


def test_every_catalog_case_reaches_its_documented_outcome() -> None:
    case_ids: set[str] = set()
    outcomes: Counter[EnforcementDecision] = Counter()
    for scenario in iter_scenarios():
        events, request = scenario.load()
        result = evaluate_request(events, request, run_id=f"catalog-{scenario.name}")
        assert result.classification == scenario.expected_classification
        assert result.decision == scenario.expected_decision
        assert scenario.case_id not in case_ids
        case_ids.add(scenario.case_id)
        outcomes[result.decision] += 1

    assert outcomes == {
        EnforcementDecision.ALLOW: 3,
        EnforcementDecision.REVIEW: 3,
        EnforcementDecision.BLOCK: 3,
    }
