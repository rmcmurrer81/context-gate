from __future__ import annotations

from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from context_gate.company_memory import MemoryStore, PatternAnalyzer

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
MEMORY_PATH_ENV = "CONTEXTGATE_MEMORY_PATH"


def _rendered_text(app: AppTest) -> str:
    element_types = (
        "caption",
        "code",
        "error",
        "info",
        "markdown",
        "success",
        "text",
        "warning",
    )
    return "\n".join(
        str(element.value)
        for element_type in element_types
        for element in app.get(element_type)
    )


def _button(app: AppTest, label: str):
    return next(button for button in app.button if button.label == label)


def _text_input(app: AppTest, label: str):
    return next(item for item in app.text_input if item.label == label)


@pytest.fixture(autouse=True)
def _clear_streamlit_resources() -> None:
    st.cache_resource.clear()
    yield
    st.cache_resource.clear()


def test_memory_ui_seed_review_correction_and_tenant_isolation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "company-memory.sqlite3"
    monkeypatch.setenv(MEMORY_PATH_ENV, str(db_path))

    # This legacy Streamlit lab performs its full module render before the first
    # widget delta. Cold Windows runners can legitimately take just over 20
    # seconds under concurrent browser/PowerPoint load, so give startup enough
    # headroom while preserving every behavioral assertion below.
    app = AppTest.from_file(APP_PATH, default_timeout=40).run()
    assert not app.exception
    assert "persistent local SQLite stored as plaintext" in _rendered_text(app)
    assert "authentication must bind the tenant ID" in _rendered_text(app)

    _button(app, "Load fictional 3 + 8 event dataset").click().run()
    assert not app.exception
    rendered = _rendered_text(app)
    assert "address · 35 Main St · 3 observation(s)" in rendered
    assert "address · 76 New Avenue · 8 observation(s)" in rendered

    _button(app, "Load fictional 3 + 8 event dataset").click().run()
    assert not app.exception
    assert "0 inserted, 11 already present" in _rendered_text(app)

    with MemoryStore(db_path) as store:
        summary = PatternAnalyzer(store).summarize_patterns(
            "example-company",
            category="company_event",
        )
    address_counts = {
        pattern.display_value: pattern.count
        for pattern in summary.attribute_patterns
        if pattern.attribute_key.casefold() == "address"
    }
    assert address_counts == {"35 Main St": 3, "76 New Avenue": 8}

    _button(app, "Assess candidate against company memory").click().run()
    assert not app.exception
    rendered = _rendered_text(app)
    assert "REVIEW ·" in rendered
    assert "same address historically used Suite 232 8 times" in rendered
    assert "candidate says Suite 354" in rendered
    assert "Has the suite changed" in rendered
    assert "which exact source confirms it?" in rendered
    assert "automatic lookup performed: no" in rendered

    _button(app, "Append human correction").click().run()
    assert not app.exception
    rendered = _rendered_text(app)
    assert "original observation retained unchanged" in rendered
    assert "Corrected attributes" in rendered
    assert "Original retained" in rendered
    assert "does not recalculate counts from correction records" in rendered

    with MemoryStore(db_path) as store:
        originals = {
            observation.observation_id: observation
            for observation in store.list_observations("example-company", limit=100)
        }
        corrections = store.list_corrections("example-company", limit=100)
        summary_after_correction = PatternAnalyzer(store).summarize_patterns(
            "example-company",
            category="company_event",
        )
    assert originals["fictional-76-new-8"].attributes["suite"] == "232"
    assert corrections[0].target_observation_id == "fictional-76-new-8"
    assert corrections[0].corrected_attributes["suite"] == "354"
    assert any(
        pattern.child_display_value == "232" and pattern.count == 8
        for pattern in summary_after_correction.conditional_patterns
    )
    assert all(
        pattern.child_display_value != "354"
        for pattern in summary_after_correction.conditional_patterns
    )

    _text_input(app, "Company memory tenant ID").set_value("other-company").run()
    assert not app.exception
    rendered = _rendered_text(app)
    assert "Remembered event observations for this tenant:** 0" in rendered
    assert "address · 35 Main St · 3 observation(s)" not in rendered
    assert "address · 76 New Avenue · 8 observation(s)" not in rendered
    assert "candidate says Suite 354" not in rendered
    assert "fictional-76-new-8" not in rendered

    with MemoryStore(db_path) as store:
        assert store.list_observations("other-company") == []
        assert store.list_corrections("other-company") == []


def test_memory_database_failure_disables_only_memory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(MEMORY_PATH_ENV, str(tmp_path))

    app = AppTest.from_file(APP_PATH, default_timeout=20).run()

    assert not app.exception
    rendered = _rendered_text(app)
    assert "Company Memory is unavailable" in rendered
    assert "rest of ContextGate remains usable" in rendered
    assert any(metric.label == "Enforcement" for metric in app.metric)
