"""Streamlit checks for bounded, fail-soft artifact uploads."""

from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

from context_gate.intake import MAX_ARTIFACT_BYTES

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


def _rendered(app: AppTest, element_type: str) -> str:
    return "\n".join(str(item.value) for item in app.get(element_type))


def test_oversized_upload_is_rejected_before_intake() -> None:
    app = AppTest.from_file(APP_PATH, default_timeout=30).run()

    app.file_uploader[0].set_value(
        ("oversized.txt", b"x" * (MAX_ARTIFACT_BYTES + 1), "text/plain")
    ).run()

    assert not app.exception
    assert "Artifact exceeds the 10 MiB limit" in _rendered(app, "error")
    assert not any(metric.label == "Intake status" for metric in app.metric)


def test_malformed_image_preview_does_not_crash_the_app() -> None:
    app = AppTest.from_file(APP_PATH, default_timeout=30).run()

    app.file_uploader[0].set_value(
        ("broken.png", b"this-is-not-a-valid-png", "image/png")
    ).run()

    assert not app.exception
    assert any(
        metric.label == "Intake status" and metric.value == "OCR_REQUIRED"
        for metric in app.metric
    )
    assert "No text was inferred" in _rendered(app, "warning")
