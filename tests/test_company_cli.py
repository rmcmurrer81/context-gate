from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

import context_gate.__main__ as company_cli
from context_gate.policy_config import (
    DEFAULT_POLICY_PAYLOAD,
    POLICY_PATH_ENV,
    clear_policy_cache,
)

from .helpers import event, request


@pytest.fixture(autouse=True)
def isolated_policy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(POLICY_PATH_ENV, raising=False)
    clear_policy_cache()
    yield
    clear_policy_cache()


def _write_inputs(tmp_path: Path) -> tuple[Path, Path]:
    events_path = tmp_path / "events.json"
    request_path = tmp_path / "request.json"
    events_path.write_text(
        json.dumps([event().model_dump(mode="json")]),
        encoding="utf-8",
    )
    request_path.write_text(
        request().model_dump_json(),
        encoding="utf-8",
    )
    return events_path, request_path


@pytest.mark.parametrize("command", ["policy", "verify-policy"])
def test_policy_command_emits_only_safe_metadata(
    command: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert company_cli.main([command]) == 0

    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert captured.err == ""
    assert output["valid"] is True
    assert output["source_count"] >= 1
    assert len(output["policy_fingerprint"]) == 64
    assert set(output) == {
        "valid",
        "policy_version",
        "policy_fingerprint",
        "source_count",
        "minimum_automatic_authority_rank",
        "minimum_automatic_trust",
        "near_peer_max_authority_rank_gap",
        "near_peer_max_trust_gap",
    }


def test_policy_metadata_does_not_echo_source_labels_or_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret_marker = "PRIVATE-COMPANY-LABEL-DO-NOT-PRINT"
    payload = deepcopy(DEFAULT_POLICY_PAYLOAD)
    payload["policy_version"] = "private-company-policy-1"
    payload["sources"]["unknown"]["label"] = secret_marker
    policy_path = tmp_path / "private-policy.json"
    policy_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv(POLICY_PATH_ENV, str(policy_path))

    assert company_cli.main(["policy"]) == 0

    captured = capsys.readouterr()
    assert secret_marker not in captured.out
    assert str(policy_path) not in captured.out
    assert json.loads(captured.out)["policy_version"] == "private-company-policy-1"


def test_evaluate_reads_real_local_models_and_emits_decision(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events_path, request_path = _write_inputs(tmp_path)

    assert (
        company_cli.main(
            [
                "evaluate",
                "--events",
                str(events_path),
                "--request",
                str(request_path),
                "--run-id",
                "company-cli-test",
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert captured.err == ""
    assert output["run_id"] == "company-cli-test"
    assert output["request_id"] == "req-1"
    assert output["classification"] == "SAFE"
    assert output["decision"] == "ALLOW"
    assert len(output["request_digest"]) == 64
    assert len(output["evidence_digest"]) == 64
    assert len(output["policy_fingerprint"]) == 64


@pytest.mark.parametrize(
    ("events_content", "expected_message"),
    [
        ("{}", "must be an array"),
        ("[]", "at least one"),
        ('[{"event_id":"one","event_id":"two"}]', "duplicate JSON key"),
        ("[NaN]", "non-finite number"),
        ("not-json", "not valid JSON"),
    ],
)
def test_evaluate_rejects_malformed_event_documents(
    events_content: str,
    expected_message: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, request_path = _write_inputs(tmp_path)
    events_path = tmp_path / "bad-events.json"
    events_path.write_text(events_content, encoding="utf-8")

    result = company_cli.main(
        [
            "evaluate",
            "--events",
            str(events_path),
            "--request",
            str(request_path),
        ]
    )

    captured = capsys.readouterr()
    assert result != 0
    assert captured.out == ""
    assert expected_message in captured.err


def test_validation_failure_does_not_echo_input_values(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret_marker = "DO-NOT-ECHO-COMPANY-DATA"
    events_path, request_path = _write_inputs(tmp_path)
    payload = request().model_dump(mode="json")
    payload["consequential"] = secret_marker
    request_path.write_text(json.dumps(payload), encoding="utf-8")

    assert (
        company_cli.main(
            [
                "evaluate",
                "--events",
                str(events_path),
                "--request",
                str(request_path),
            ]
        )
        != 0
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Request at consequential" in captured.err
    assert secret_marker not in captured.err


def test_evaluate_uses_strict_json_types(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events_path, request_path = _write_inputs(tmp_path)
    payload = event().model_dump(mode="json")
    payload["trust_score"] = "0.98"
    events_path.write_text(json.dumps([payload]), encoding="utf-8")

    assert (
        company_cli.main(
            [
                "evaluate",
                "--events",
                str(events_path),
                "--request",
                str(request_path),
            ]
        )
        != 0
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Events item 0 at trust_score" in captured.err


def test_evaluate_rejects_directory_and_oversize_input(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, request_path = _write_inputs(tmp_path)

    assert (
        company_cli.main(
            [
                "evaluate",
                "--events",
                str(tmp_path),
                "--request",
                str(request_path),
            ]
        )
        != 0
    )
    first = capsys.readouterr()
    assert first.out == ""
    assert "cannot be opened" in first.err or "regular file" in first.err

    oversize = tmp_path / "oversize.json"
    oversize.write_bytes(b" " * (company_cli.MAX_INPUT_BYTES + 1))
    assert (
        company_cli.main(
            [
                "evaluate",
                "--events",
                str(oversize),
                "--request",
                str(request_path),
            ]
        )
        != 0
    )
    second = capsys.readouterr()
    assert second.out == ""
    assert "exceeds" in second.err


def test_evaluate_enforces_event_count_bound(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events_path, request_path = _write_inputs(tmp_path)
    two_events = [
        event(event_id="evt-one").model_dump(mode="json"),
        event(event_id="evt-two").model_dump(mode="json"),
    ]
    events_path.write_text(json.dumps(two_events), encoding="utf-8")
    monkeypatch.setattr(company_cli, "MAX_EVENTS", 1)

    assert (
        company_cli.main(
            [
                "evaluate",
                "--events",
                str(events_path),
                "--request",
                str(request_path),
            ]
        )
        != 0
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "event limit" in captured.err


def test_evaluate_rejects_symbolic_link_input_when_supported(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events_path, request_path = _write_inputs(tmp_path)
    link = tmp_path / "events-link.json"
    try:
        link.symlink_to(events_path)
    except (NotImplementedError, OSError):
        pytest.skip("symbolic links are unavailable in this test environment")

    assert (
        company_cli.main(
            [
                "evaluate",
                "--events",
                str(link),
                "--request",
                str(request_path),
            ]
        )
        != 0
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "symbolic links" in captured.err


def test_bundled_company_starter_files_produce_safe_allow(
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = Path(__file__).resolve().parents[1]

    assert (
        company_cli.main(
            [
                "evaluate",
                "--events",
                str(root / "examples" / "company" / "context-events.json"),
                "--request",
                str(root / "examples" / "company" / "action-request.json"),
                "--run-id",
                "company-starter-smoke",
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    decision = json.loads(captured.out)
    assert captured.err == ""
    assert decision["classification"] == "SAFE"
    assert decision["decision"] == "ALLOW"
    assert decision["run_id"] == "company-starter-smoke"
