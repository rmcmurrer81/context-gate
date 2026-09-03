from __future__ import annotations

import hashlib
import sys
from types import SimpleNamespace

import pytest

from context_gate.intake import (
    MAX_EXTRACTED_TEXT_CHARS,
    ArtifactIntakeError,
    ArtifactStatus,
    create_context_event_candidate,
    ingest_artifact,
)
from context_gate.models import EvidenceStatus


def test_text_is_extracted_without_retaining_bytes() -> None:
    payload = b"Venue: 10 Innovation Street\nConfirmed"

    receipt = ingest_artifact("confirmation.txt", "text/plain", payload)

    assert receipt.status == ArtifactStatus.EXTRACTED
    assert receipt.extracted_text == payload.decode()
    assert receipt.extracted_chars == len(receipt.extracted_text)
    assert receipt.model_dump().keys().isdisjoint({"bytes", "content", "payload"})


def test_json_is_extracted_as_bounded_utf8_text() -> None:
    payload = b'{"venue":"10 Innovation Street"}'

    receipt = ingest_artifact("details.json", "application/json", payload)

    assert receipt.status == ArtifactStatus.EXTRACTED
    assert receipt.extracted_text == payload.decode()
    assert receipt.extractor == "utf-8"


def test_eml_extracts_subject_and_text_without_attachment_bytes() -> None:
    payload = (
        b"Subject: ContextGate Synthetic Test\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"Venue: North Lobby"
    )
    receipt = ingest_artifact("test.eml", "message/rfc822", payload)
    assert receipt.status == ArtifactStatus.EXTRACTED
    assert receipt.extractor == "stdlib-email"
    assert "ContextGate Synthetic Test" in (receipt.extracted_text or "")
    assert "Venue: North Lobby" in (receipt.extracted_text or "")


def test_extracted_text_is_truncated_to_a_fixed_bound() -> None:
    receipt = ingest_artifact(
        "long.md",
        "text/markdown",
        b"x" * (MAX_EXTRACTED_TEXT_CHARS + 100),
    )

    assert receipt.status == ArtifactStatus.EXTRACTED
    assert receipt.extracted_chars == MAX_EXTRACTED_TEXT_CHARS
    assert receipt.truncated is True


def test_image_requires_ocr_and_never_invents_text() -> None:
    receipt = ingest_artifact(
        "screenshot.png", "image/png", b"\x89PNG\r\n\x1a\nsynthetic"
    )

    assert receipt.status == ArtifactStatus.OCR_REQUIRED
    assert receipt.extracted_text is None
    assert receipt.extracted_chars == 0
    assert receipt.extractor == "ocr-not-run"


@pytest.mark.parametrize(
    ("module_name", "filename", "content_type", "payload"),
    [
        ("pypdf", "scan.pdf", "application/pdf", b"%PDF-synthetic"),
        (
            "docx",
            "notes.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            b"PK-synthetic",
        ),
    ],
)
def test_optional_parser_dependency_is_reported_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    filename: str,
    content_type: str,
    payload: bytes,
) -> None:
    monkeypatch.setitem(sys.modules, module_name, None)

    receipt = ingest_artifact(filename, content_type, payload)

    assert receipt.status == ArtifactStatus.DEPENDENCY_REQUIRED
    assert receipt.extracted_text is None
    assert "Install" in receipt.message


def test_pdf_without_a_text_layer_requires_ocr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeReader:
        def __init__(self, _stream: object) -> None:
            self.pages = [SimpleNamespace(extract_text=lambda: "")]

    monkeypatch.setitem(sys.modules, "pypdf", SimpleNamespace(PdfReader=FakeReader))

    receipt = ingest_artifact("scan.pdf", "application/pdf", b"%PDF-synthetic")

    assert receipt.status == ArtifactStatus.OCR_REQUIRED
    assert receipt.extracted_text is None


def test_size_limit_rejects_before_receipt_creation() -> None:
    with pytest.raises(ArtifactIntakeError, match="100-byte limit"):
        ingest_artifact("large.txt", "text/plain", b"x" * 101, max_bytes=100)


def test_filename_is_reduced_to_safe_bounded_basename() -> None:
    receipt = ingest_artifact(
        r"..\..\private folder\event <draft>.txt",
        "text/plain",
        b"synthetic",
    )

    assert receipt.safe_filename == "event_draft_.txt"
    assert "/" not in receipt.safe_filename
    assert "\\" not in receipt.safe_filename


def test_digest_and_artifact_id_are_deterministic() -> None:
    payload = b"same immutable evidence"

    first = ingest_artifact("one.txt", "text/plain", payload)
    second = ingest_artifact("two.md", "text/markdown", payload)

    assert first.sha256 == hashlib.sha256(payload).hexdigest()
    assert first.sha256 == second.sha256
    assert first.artifact_id == second.artifact_id


def test_unknown_binary_is_unsupported() -> None:
    receipt = ingest_artifact(
        "archive.bin", "application/octet-stream", b"\x00\x01\x02synthetic"
    )

    assert receipt.status == ArtifactStatus.UNSUPPORTED
    assert receipt.extracted_text is None


def test_candidate_uses_explicit_claim_and_remains_unverified() -> None:
    receipt = ingest_artifact(
        "claim.json",
        "application/json",
        b'{"venue":"value that must not self-authenticate"}',
    )

    candidate = create_context_event_candidate(
        receipt,
        event_id="evt-upload-1",
        entity_id="Synthetic Summit",
        field_name="Venue",
        claim_value="10 Innovation Street",
        source_name="User supplied upload",
        source_type="uploaded_document",
        trust_score=0.95,
    )

    assert candidate.field_value == "10 Innovation Street"
    assert candidate.status == EvidenceStatus.UNVERIFIED
    assert candidate.evidence_reference == f"upload-sha256://{receipt.sha256}"
    assert candidate.content_hash is not None
    assert "must not self-authenticate" not in candidate.model_dump_json()
