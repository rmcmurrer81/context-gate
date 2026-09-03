"""Streamlit acceptance coverage for the important-details workbench."""

from __future__ import annotations

import json
from pathlib import Path

from streamlit.testing.v1 import AppTest

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


def _button(app: AppTest, label: str):
    return next(item for item in app.button if item.label == label)


def _text_area(app: AppTest, label: str):
    return next(item for item in app.text_area if item.label == label)


def _text_input(app: AppTest, label: str):
    return next(item for item in app.text_input if item.label == label)


def _all_rendered_text(app: AppTest) -> str:
    values: list[str] = []
    for element_type in (
        "caption",
        "code",
        "error",
        "info",
        "markdown",
        "success",
        "warning",
    ):
        values.extend(str(item.value) for item in app.get(element_type))
    return "\n".join(values)


def test_important_details_delta_total_identity_and_ambiguity_paths() -> None:
    app = AppTest.from_file(APP_PATH, default_timeout=20).run()
    assert not app.exception
    assert any(
        item.label == "Important Details · totals, additions, and corrections"
        for item in app.expander
    )

    _button(app, "Interpret important detail").click().run()
    assert not app.exception
    rendered = _all_rendered_text(app)
    assert "PROPOSE · DELTA · candidate total 113" in rendered
    assert "35 + 78 = 113" in rendered
    assert "Identity matched:** yes" in rendered
    assert "Automatic lookup performed: no" in rendered
    assert "state updated: no" in rendered
    assert "external action executed: no" in rendered
    assert "evidence_id=fictional-email-002" in rendered
    assert "config_fingerprint=" in rendered
    assert "input_digest=" in rendered

    _text_area(app, "Incoming important-detail statement").set_value(
        "78 people are going"
    )
    _button(app, "Interpret important detail").click().run()
    assert not app.exception
    rendered = _all_rendered_text(app)
    assert "PROPOSE · TOTAL · candidate total 78" in rendered
    assert "TOTAL 78 = 78" in rendered

    _text_input(app, "Incoming event name").set_value("Different Fictional Summit")
    _button(app, "Interpret important detail").click().run()
    assert not app.exception
    rendered = _all_rendered_text(app)
    assert "REVIEW · TOTAL · candidate total not calculated" in rendered
    assert "IDENTITY_MISMATCH" in rendered
    assert "Identity matched:** no" in rendered

    _text_input(app, "Incoming event name").set_value("Fictional Summit")
    _text_area(app, "Incoming important-detail statement").set_value(
        "Attendees will be discussed later"
    )
    _button(app, "Interpret important detail").click().run()
    assert not app.exception
    rendered = _all_rendered_text(app)
    assert "REVIEW · AMBIGUOUS · candidate total not calculated" in rendered
    assert "QUANTITY_MISSING" in rendered
    assert "AMBIGUOUS: no candidate total" in rendered


def test_important_details_local_answer_and_append_only_correction() -> None:
    app = AppTest.from_file(APP_PATH, default_timeout=20).run()
    _button(app, "Interpret important detail").click().run()

    answer = next(
        item for item in app.info if "Formula: 35 + 78 = 113" in str(item.value)
    )
    assert "fictional-email-002" in str(answer.value)
    assert "[evidence fictional-email-002" in str(answer.value)

    question = next(
        item
        for item in app.selectbox
        if item.label == "Ask about this important detail"
    )
    question.set_value("Is it the same event?").run()
    assert any(
        "every configured identity key matched" in str(item.value) for item in app.info
    )

    _button(app, "Append important-detail correction").click().run()
    assert not app.exception
    rendered = _all_rendered_text(app)
    assert "PROPOSE · TOTAL · candidate total 78" in rendered
    assert "TOTAL 78 = 78" in rendered
    assert "Correction history (1) · original preserved" in rendered
    assert "semantic-correction-001 · TOTAL 78" in rendered
    assert "Original: PROPOSE / DELTA / 35 + 78 = 113" in rendered
    assert "HUMAN_CORRECTION" in rendered
    assert "state updated: no" in rendered


def test_important_details_profile_is_customizable_and_errors_are_redacted() -> None:
    custom_profile = {
        "category": "event",
        "identity_keys": ["event_name", "event_date"],
        "important_fields": ["crowd_size"],
        "quantity_fields": [
            {
                "field_name": "crowd_size",
                "metric_nouns": ["participants"],
                "delta_markers": ["newly added"],
                "total_markers": ["complete total"],
                "status_markers": ["confirmed"],
                "negation_markers": ["withdrawn"],
                "maximum_plausible_value": 200,
                "maximum_absolute_change": 50,
            }
        ],
    }
    app = AppTest.from_file(APP_PATH, default_timeout=20).run()
    _text_area(app, "Important-details profile JSON").set_value(
        json.dumps(custom_profile)
    )
    _text_area(app, "Incoming important-detail statement").set_value(
        "10 newly added participants are confirmed"
    )
    _button(app, "Interpret important detail").click().run()

    assert not app.exception
    rendered = _all_rendered_text(app)
    assert "PROPOSE · DELTA · candidate total 45" in rendered
    assert "35 + 10 = 45" in rendered

    secret = "DO-NOT-ECHO-semantic-profile-secret-1942"
    _text_area(app, "Important-details profile JSON").set_value(
        json.dumps({"secret": secret})
    )
    _button(app, "Interpret important detail").click().run()

    assert not app.exception
    errors = "\n".join(str(item.value) for item in app.error)
    assert "Important-details profile failed schema validation" in errors
    assert secret not in errors
