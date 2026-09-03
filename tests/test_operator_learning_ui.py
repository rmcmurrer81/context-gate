from __future__ import annotations

from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
MEMORY_PATH_ENV = "CONTEXTGATE_MEMORY_PATH"
TENANT_ID_ENV = "CONTEXTGATE_TENANT_ID"
CHAT_INPUT_LABEL = (
    "Ask for details, patterns, comparisons, evidence, or a safe next step"
)


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


def _block_text(block: object) -> str:
    element_types = ("caption", "error", "info", "markdown", "success", "warning")
    return "\n".join(
        str(element.value)
        for element_type in element_types
        for element in block.get(element_type)  # type: ignore[attr-defined]
    )


def _latest_assistant_text(app: AppTest) -> str:
    message = next(
        item for item in reversed(app.chat_message) if item.name == "assistant"
    )
    return _block_text(message)


def _widget(items: object, label: str):
    return next(item for item in items if item.label == label)  # type: ignore[attr-defined]


def _submit_chat(app: AppTest, text: str, *, remember: bool = False) -> None:
    _widget(app.text_input, CHAT_INPUT_LABEL).set_value(text)
    _widget(app.checkbox, "Remember this as company guidance").set_value(remember)
    _widget(app.button, "Ask ContextGate").click().run()


def _learned_guidance_expander(app: AppTest):
    return next(
        item
        for item in app.expander
        if item.label.startswith("Learned operator guidance")
    )


def _configure_learning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    tenant_id: str,
) -> Path:
    db_path = tmp_path / "operator-learning.sqlite3"
    monkeypatch.setenv(MEMORY_PATH_ENV, str(db_path))
    monkeypatch.setenv(TENANT_ID_ENV, tenant_id)
    return db_path


@pytest.fixture(autouse=True)
def _clear_streamlit_resources() -> None:
    st.cache_resource.clear()
    yield
    st.cache_resource.clear()


def test_explicit_chat_guidance_persists_is_cited_and_can_be_retracted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_learning(monkeypatch, tmp_path, tenant_id="chat-guidance-test")
    guidance = (
        "For B1 marigold escalation, call the fictional supervisor before release."
    )

    first_session = AppTest.from_file(APP_PATH, default_timeout=20).run()
    _submit_chat(first_session, guidance, remember=True)

    assert not first_session.exception
    learned = _learned_guidance_expander(first_session)
    assert learned.label.endswith("1 active")
    assert guidance in _block_text(learned)

    second_session = AppTest.from_file(APP_PATH, default_timeout=20).run()
    _submit_chat(
        second_session,
        "What company guidance applies to the B1 marigold escalation?",
    )

    assert not second_session.exception
    cited_answer = _latest_assistant_text(second_session)
    assert guidance in cited_answer
    assert "Remembered operator guidance" in cited_answer
    assert "operator guidance guidance-" in cited_answer

    retract = next(
        button
        for button in second_session.button
        if button.label.startswith("Retract guidance ")
    )
    retract.click().run()
    assert not second_session.exception
    assert _learned_guidance_expander(second_session).label.endswith("0 active")
    assert "was retracted; its original record remains in history" in _rendered_text(
        second_session
    )

    after_retraction = AppTest.from_file(APP_PATH, default_timeout=20).run()
    _submit_chat(
        after_retraction,
        "What company guidance applies to the B1 marigold escalation?",
    )
    latest = _latest_assistant_text(after_retraction)
    assert guidance not in latest
    assert "operator guidance guidance-" not in latest


def test_unchecked_ordinary_chat_is_not_learned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_learning(monkeypatch, tmp_path, tenant_id="unchecked-chat-test")
    ordinary_chat = "For B1 zephyr chatter, use the fictional north desk."

    first_session = AppTest.from_file(APP_PATH, default_timeout=20).run()
    _submit_chat(first_session, ordinary_chat, remember=False)

    assert not first_session.exception
    assert _learned_guidance_expander(first_session).label.endswith("0 active")

    second_session = AppTest.from_file(APP_PATH, default_timeout=20).run()
    _submit_chat(
        second_session,
        "What company guidance applies to the B1 zephyr chatter?",
    )

    assert not second_session.exception
    latest = _latest_assistant_text(second_session)
    assert "fictional north desk" not in latest
    assert "operator guidance guidance-" not in latest
    assert _learned_guidance_expander(second_session).label.endswith("0 active")


def test_explicit_human_review_becomes_cited_operator_guidance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_learning(monkeypatch, tmp_path, tenant_id="review-guidance-test")
    rationale = "Cobalt attendance mismatch requires a fictional supervisor callback."

    app = AppTest.from_file(APP_PATH, default_timeout=20).run()
    _widget(app.button, "Open R2 details").click().run()
    _widget(app.text_area, "Rationale").set_value(rationale)
    _widget(app.button, "Hold").click().run()

    assert not app.exception
    assert "Review recorded: HELD" in _rendered_text(app)
    learned = _learned_guidance_expander(app)
    assert learned.label.endswith("1 active")
    assert rationale in _block_text(learned)

    persisted_session = AppTest.from_file(APP_PATH, default_timeout=20).run()
    _submit_chat(
        persisted_session,
        "What did the R2 reviewer say about the cobalt attendance mismatch?",
    )

    assert not persisted_session.exception
    latest = _latest_assistant_text(persisted_session)
    assert rationale in latest
    assert "operator guidance guidance-" in latest


def test_false_positive_correction_changes_only_resolved_view_then_retracts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_learning(monkeypatch, tmp_path, tenant_id="false-positive-test")
    rationale = (
        "Fictional signed organizer evidence proves B1 was a false-positive block."
    )

    app = AppTest.from_file(APP_PATH, default_timeout=20).run()
    _widget(app.selectbox, "Dashboard queue").set_value("BLOCK").run()
    _widget(app.selectbox, "Corrected outcome for B1").set_value("ALLOW")
    _widget(app.text_input, "Correction reviewer for B1").set_value(
        "fictional-authorized-reviewer"
    )
    _widget(app.text_area, "Correction rationale for B1").set_value(rationale)
    _widget(app.checkbox, "Remember correction for similar future cases").set_value(
        True
    )
    _widget(app.button, "Save correction for B1").click().run()

    assert not app.exception
    metrics = {metric.label: str(metric.value) for metric in app.metric}
    assert metrics["Passed gate"] == "4"
    assert metrics["Blocked"] == "2"
    assert metrics["Needs my attention"] == "3"
    assert metrics["Enforcement"] == "BLOCK"

    _widget(app.selectbox, "Dashboard queue").set_value("ALLOW").run()
    assert not app.exception
    rendered = _rendered_text(app)
    assert "Human-corrected: original BLOCK → effective ALLOW" in rendered
    assert "Original deterministic outcome: BLOCK" in rendered
    assert rationale in rendered
    assert any(
        "B1" in item.label and "HUMAN-CORRECTED" in item.label for item in app.expander
    )
    assert "execute an external action" in rendered

    _submit_chat(app, "What is the effective outcome for B1 now?")
    corrected_answer = _latest_assistant_text(app)
    assert "Human-corrected: original BLOCK → effective ALLOW" in corrected_answer
    assert rationale in corrected_answer
    assert "corrections correction-" in corrected_answer

    _widget(app.button, "Retract correction for B1").click().run()
    assert not app.exception
    metrics = {metric.label: str(metric.value) for metric in app.metric}
    assert metrics["Passed gate"] == "3"
    assert metrics["Blocked"] == "3"
    assert metrics["Needs my attention"] == "3"
    assert "original BLOCK outcome is effective again" in _rendered_text(app)

    _widget(app.selectbox, "Dashboard queue").set_value("BLOCK").run()
    assert "Corrected outcome for B1" in [item.label for item in app.selectbox]
    assert not any(
        "B1" in item.label and "HUMAN-CORRECTED" in item.label for item in app.expander
    )

    _submit_chat(app, "What is the effective outcome for B1 now?")
    restored_answer = _latest_assistant_text(app)
    assert "Human-corrected: original BLOCK → effective ALLOW" not in restored_answer
    assert "corrections correction-" not in restored_answer
    assert "B1" in restored_answer


def test_chat_correction_language_creates_only_a_pending_confirmable_proposal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_learning(monkeypatch, tmp_path, tenant_id="chat-correction-test")
    request = (
        "B1 was a mistake and should be allowed because fictional signed evidence "
        "resolved the venue conflict."
    )

    app = AppTest.from_file(APP_PATH, default_timeout=20).run()
    _submit_chat(app, request)

    assert not app.exception
    rendered = _rendered_text(app)
    assert "prepared a correction proposal for case B1" in rendered
    assert "Pending human correction · case B1 · original BLOCK" in rendered
    pending_outcome = _widget(app.selectbox, "Chat correction outcome for B1")
    assert pending_outcome.value == "ALLOW"
    assert _widget(app.text_area, "Chat correction rationale").value == request
    metrics = {metric.label: str(metric.value) for metric in app.metric}
    assert (metrics["Passed gate"], metrics["Blocked"]) == ("3", "3")
    assert metrics["Enforcement"] == "BLOCK"

    _widget(app.button, "Confirm chat correction").click().run()

    assert not app.exception
    metrics = {metric.label: str(metric.value) for metric in app.metric}
    assert (metrics["Passed gate"], metrics["Blocked"]) == ("4", "2")
    assert metrics["Enforcement"] == "BLOCK"
    assert "Correction correction-" in _rendered_text(app)

    _widget(app.selectbox, "Dashboard queue").set_value("ALLOW").run()
    rendered = _rendered_text(app)
    assert "Original deterministic outcome: BLOCK" in rendered
    assert "Human-corrected: original BLOCK → effective ALLOW" in rendered
    assert "execute an external action" in rendered
