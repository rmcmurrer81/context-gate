import json
from copy import deepcopy
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from context_gate.policy_config import (
    DEFAULT_POLICY,
    DEFAULT_POLICY_PAYLOAD,
    POLICY_PATH_ENV,
    clear_policy_cache,
)
from context_gate.scenario import load_scenario

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


def _metrics(app: AppTest) -> dict[str, str]:
    return {metric.label: str(metric.value) for metric in app.metric}


def _decision_metrics(app: AppTest) -> dict[str, str]:
    labels = {"Classification", "Enforcement", "Risk", "Review"}
    return {
        metric.label: str(metric.value)
        for metric in app.metric
        if metric.label in labels
    }


def _dashboard_buttons(app: AppTest) -> set[str]:
    return {
        button.label
        for button in app.button
        if button.label.startswith("Open ") and button.label.endswith(" details")
    }


def _dashboard_expander_case_ids(app: AppTest) -> set[str]:
    case_ids = {"A1", "A2", "A3", "B1", "B2", "B3", "R1", "R2", "R3"}
    return {
        case_id
        for item in app.expander
        for case_id in case_ids
        if f"{case_id} ·" in item.label
    }


def test_streamlit_exercises_block_allow_and_review_without_exceptions() -> None:
    app = AppTest.from_file(APP_PATH, default_timeout=20).run()
    assert not app.exception
    assert _metrics(app)["Enforcement"] == "BLOCK"

    scenario_picker = next(
        item for item in app.selectbox if item.label == "Choose a live decision path"
    )
    scenario_picker.set_value("safe").run()
    assert not app.exception
    assert _decision_metrics(app) == {
        "Classification": "SAFE",
        "Enforcement": "ALLOW",
        "Risk": "LOW",
        "Review": "NOT_REQUIRED",
    }

    scenario_picker = next(
        item for item in app.selectbox if item.label == "Choose a live decision path"
    )
    scenario_picker.set_value("missing-provenance").run()
    assert not app.exception
    assert _metrics(app)["Enforcement"] == "REVIEW"


def test_dashboard_totals_filters_expandable_entries_and_opens_r2() -> None:
    app = AppTest.from_file(APP_PATH, default_timeout=20).run()

    assert not app.exception
    metrics = _metrics(app)
    assert metrics["Total evaluated"] == "9"
    assert metrics["Passed gate"] == "3"
    assert metrics["Blocked"] == "3"
    assert metrics["Needs my attention"] == "3"

    queue = next(item for item in app.selectbox if item.label == "Dashboard queue")
    assert set(queue.options) == {"REVIEW", "ALLOW", "BLOCK"}

    queue.set_value("REVIEW").run()
    assert not app.exception
    assert _dashboard_buttons(app) == {
        "Open R1 details",
        "Open R2 details",
        "Open R3 details",
    }
    assert _dashboard_expander_case_ids(app) == {"R1", "R2", "R3"}

    queue = next(item for item in app.selectbox if item.label == "Dashboard queue")
    queue.set_value("ALLOW").run()
    assert not app.exception
    assert _dashboard_buttons(app) == {
        "Open A1 details",
        "Open A2 details",
        "Open A3 details",
    }

    queue = next(item for item in app.selectbox if item.label == "Dashboard queue")
    queue.set_value("BLOCK").run()
    assert not app.exception
    assert _dashboard_buttons(app) == {
        "Open B1 details",
        "Open B2 details",
        "Open B3 details",
    }

    queue = next(item for item in app.selectbox if item.label == "Dashboard queue")
    queue.set_value("REVIEW").run()
    open_r2 = next(item for item in app.button if item.label == "Open R2 details")
    open_r2.click().run()

    assert not app.exception
    scenario_picker = next(
        item for item in app.selectbox if item.label == "Choose a live decision path"
    )
    assert scenario_picker.value == "near-peer-time-conflict"
    assert _decision_metrics(app)["Enforcement"] == "REVIEW"


def test_dashboard_counts_are_recomputed_under_the_active_company_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = deepcopy(DEFAULT_POLICY_PAYLOAD)
    payload["policy_version"] = "dashboard-live-counts-policy-1"
    payload["minimum_automatic_trust"] = 0.99
    policy_path = tmp_path / "dashboard-policy.json"
    policy_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv(POLICY_PATH_ENV, str(policy_path))
    clear_policy_cache()

    try:
        app = AppTest.from_file(APP_PATH, default_timeout=20).run()
    finally:
        clear_policy_cache()

    assert not app.exception
    metrics = _metrics(app)
    assert metrics["Total evaluated"] == "9"
    assert metrics["Passed gate"] == "0"
    assert metrics["Blocked"] == "3"
    assert metrics["Needs my attention"] == "6"
    assert (
        metrics["Passed gate"],
        metrics["Blocked"],
        metrics["Needs my attention"],
    ) != ("3", "3", "3")


def test_chat_panel_is_rendered_immediately_after_the_dashboard() -> None:
    app = AppTest.from_file(APP_PATH, default_timeout=20).run()

    assert not app.exception
    top_level = list(app.main.children.values())
    dashboard_index = next(
        index
        for index, item in enumerate(top_level)
        if type(item).__name__ == "Subheader" and item.value == "Decision dashboard"
    )
    chat_index = next(
        index
        for index, item in enumerate(top_level)
        if getattr(item, "key", None) == "dashboard_chat_panel"
    )
    details_index = next(
        index
        for index, item in enumerate(top_level)
        if index > dashboard_index and type(item).__name__ == "Divider"
    )

    assert dashboard_index < chat_index < details_index
    assert any(
        type(item).__name__ == "Subheader" and item.value == "Ask ContextGate"
        for item in top_level[chat_index].children.values()
    )


def test_streamlit_grounded_chat_answers_from_case_receipt() -> None:
    app = AppTest.from_file(APP_PATH, default_timeout=20).run()
    question = next(
        item for item in app.text_input if item.label.startswith("Ask for details")
    )
    ask = next(item for item in app.button if item.label == "Ask ContextGate")
    question.set_value("Why was B1 blocked?")
    ask.click().run()

    assert not app.exception
    assert [message.name for message in app.chat_message][-2:] == [
        "user",
        "assistant",
    ]
    rendered = "\n".join(item.value for item in app.markdown)
    assert "[case B1]" in rendered
    assert "CG-002-LOWER-AUTHORITY-CONFLICT" in rendered


def test_company_workbench_evaluates_arbitrary_json_and_offers_receipt() -> None:
    events, request = load_scenario("safe")
    custom_event = events[0].model_copy(
        update={
            "event_id": "evt-company-workbench-1",
            "entity_id": "company:change-42",
            "field_value": "Approved maintenance window",
        }
    )
    custom_request = request.model_copy(
        update={
            "request_id": "req-company-workbench-1",
            "action_id": "act-company-workbench-1",
            "entity_id": "company:change-42",
            "requested_value": "Approved maintenance window",
            "supporting_event_id": custom_event.event_id,
        }
    )
    app = AppTest.from_file(APP_PATH, default_timeout=20).run()
    events_input = next(
        item for item in app.text_area if item.label == "ContextEvent[] JSON"
    )
    request_input = next(
        item for item in app.text_area if item.label == "ActionRequest JSON"
    )
    evaluate = next(item for item in app.button if item.label == "Evaluate safely")

    events_input.set_value(json.dumps([custom_event.model_dump(mode="json")]))
    request_input.set_value(json.dumps(custom_request.model_dump(mode="json")))
    evaluate.click().run()

    assert not app.exception
    assert any("SAFE / ALLOW" in item.value for item in app.success)
    assert DEFAULT_POLICY.policy_fingerprint in [item.value for item in app.code]
    assert "Download company workbench receipt" in [
        item.label for item in app.get("download_button")
    ]


def test_company_workbench_validation_does_not_echo_secret_values() -> None:
    secret = "DO-NOT-ECHO-company-secret-9384"
    app = AppTest.from_file(APP_PATH, default_timeout=20).run()
    request_input = next(
        item for item in app.text_area if item.label == "ActionRequest JSON"
    )
    evaluate = next(item for item in app.button if item.label == "Evaluate safely")
    request_input.set_value(json.dumps({"request_id": "req-1", "token": secret}))
    evaluate.click().run()

    assert not app.exception
    rendered_errors = "\n".join(item.value for item in app.error)
    assert "Request JSON failed schema validation" in rendered_errors
    assert secret not in rendered_errors
    assert "Download company workbench receipt" not in [
        item.label for item in app.get("download_button")
    ]


def test_company_workbench_records_review_for_its_own_decision() -> None:
    app = AppTest.from_file(APP_PATH, default_timeout=20).run()
    evaluate = next(item for item in app.button if item.label == "Evaluate safely")
    evaluate.click().run()

    disposition = next(
        item for item in app.selectbox if item.label == "Company review disposition"
    )
    disposition.set_value("HOLD")
    record = next(
        item for item in app.button if item.label == "Record company review receipt"
    )
    record.click().run()

    assert not app.exception
    assert any("Company review recorded: HELD" in item.value for item in app.success)
    assert "Download company review receipt" in [
        item.label for item in app.get("download_button")
    ]


def test_invalid_company_policy_stops_safely_without_echoing_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret_marker = "PRIVATE-POLICY-VALUE-DO-NOT-ECHO"
    policy_path = tmp_path / "invalid-policy.json"
    policy_path.write_text(json.dumps({"secret": secret_marker}), encoding="utf-8")
    monkeypatch.setenv(POLICY_PATH_ENV, str(policy_path))
    clear_policy_cache()

    app = AppTest.from_file(APP_PATH, default_timeout=20).run()

    assert not app.exception
    rendered_errors = "\n".join(item.value for item in app.error)
    assert "stopped safely" in rendered_errors
    assert secret_marker not in rendered_errors
