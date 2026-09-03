from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from context_gate.authority import effective_trust, policy_for
from context_gate.decision_engine import POLICY_VERSION, evaluate_request
from context_gate.evidence import build_evidence_package
from context_gate.models import Classification, EnforcementDecision
from context_gate.policy_config import (
    DEFAULT_POLICY,
    DEFAULT_POLICY_PAYLOAD,
    MAX_POLICY_BYTES,
    POLICY_PATH_ENV,
    PolicyConfigError,
    clear_policy_cache,
    get_active_policy,
)

from .helpers import event, request


@pytest.fixture(autouse=True)
def isolated_policy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(POLICY_PATH_ENV, raising=False)
    clear_policy_cache()
    yield
    clear_policy_cache()


def _write_policy(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "source-policy.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _use_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: dict[str, object]
) -> Path:
    path = _write_policy(tmp_path, payload)
    monkeypatch.setenv(POLICY_PATH_ENV, str(path))
    return path


def test_zero_config_policy_exactly_matches_original_defaults() -> None:
    active = get_active_policy()

    assert active is DEFAULT_POLICY
    assert active.policy_version == POLICY_VERSION
    assert active.minimum_automatic_authority_rank == 70
    assert active.minimum_automatic_trust == 0.70
    assert active.near_peer_max_authority_rank_gap == 5
    assert active.near_peer_max_trust_gap == 0.10
    assert {
        source_type: (item.rank, item.trust_cap, item.label)
        for source_type, item in active.sources.items()
    } == {
        "registration_confirmation": (
            100,
            1.00,
            "Official registration confirmation",
        ),
        "organizer_api": (98, 1.00, "Organizer-controlled API"),
        "organizer_website": (95, 0.98, "Organizer website"),
        "official_email": (92, 0.98, "Verified organizer email"),
        "partner_website": (70, 0.85, "Named event partner"),
        "copied_webpage": (50, 0.70, "Copied or community webpage"),
        "user_report": (40, 0.65, "Unverified user report"),
        "unknown": (10, 0.40, "Unknown source"),
    }
    assert len(active.policy_fingerprint) == 64


def test_custom_source_rank_changes_deterministic_outcome(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    official = event()
    copied = event(
        event_id="evt-copy",
        field_value="2 Innovation Street",
        source_name="Company-approved mirror",
        source_type="copied_webpage",
        trust_score=0.99,
        evidence_reference="synthetic://copy",
        status="confirmed",
    )
    action = request(
        requested_value="2 Innovation Street", supporting_event_id="evt-copy"
    )

    default_decision = evaluate_request(
        [official, copied], action, run_id="custom-rank"
    )
    assert default_decision.decision == EnforcementDecision.BLOCK

    payload = deepcopy(DEFAULT_POLICY_PAYLOAD)
    payload["policy_version"] = "company-rank-policy-1"
    payload["sources"]["registration_confirmation"]["rank"] = 50
    payload["sources"]["copied_webpage"]["rank"] = 100
    payload["sources"]["copied_webpage"]["trust_cap"] = 1.0
    _use_policy(monkeypatch, tmp_path, payload)

    custom_decision = evaluate_request([official, copied], action, run_id="custom-rank")
    assert custom_decision.classification == Classification.SAFE
    assert custom_decision.decision == EnforcementDecision.ALLOW
    assert custom_decision.authoritative_value == "2 Innovation Street"
    assert custom_decision.policy_version == "company-rank-policy-1"


def test_custom_caps_thresholds_and_near_peer_window_are_active(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = deepcopy(DEFAULT_POLICY_PAYLOAD)
    payload["policy_version"] = "company-threshold-policy-1"
    payload["sources"]["registration_confirmation"]["trust_cap"] = 0.60
    payload["near_peer_max_authority_rank_gap"] = 60
    _use_policy(monkeypatch, tmp_path, payload)

    official = event(trust_score=1.0)
    assert effective_trust(official) == 0.60

    copied = event(
        event_id="evt-copy",
        field_value="2 Innovation Street",
        source_name="Copied page",
        source_type="copied_webpage",
        trust_score=0.65,
        evidence_reference="synthetic://copy",
        status="confirmed",
    )
    decision = evaluate_request(
        [official, copied],
        request(),
        run_id="custom-near-peer",
    )
    assert decision.classification == Classification.CONFLICT
    assert decision.decision == EnforcementDecision.REVIEW


def test_custom_automatic_threshold_prevents_automatic_action(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    default_decision = evaluate_request(
        [event(trust_score=0.98)], request(), run_id="automatic-threshold"
    )
    assert default_decision.decision == EnforcementDecision.ALLOW

    payload = deepcopy(DEFAULT_POLICY_PAYLOAD)
    payload["policy_version"] = "company-automatic-threshold-1"
    payload["minimum_automatic_trust"] = 0.99
    _use_policy(monkeypatch, tmp_path, payload)

    custom_decision = evaluate_request(
        [event(trust_score=0.98)], request(), run_id="automatic-threshold"
    )
    assert custom_decision.classification == Classification.UNTRUSTED
    assert custom_decision.decision == EnforcementDecision.REVIEW


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update({"unexpected": True}),
        lambda payload: payload["sources"].pop("unknown"),
        lambda payload: payload.pop("minimum_automatic_trust"),
        lambda payload: payload["sources"]["official_email"].update({"rank": 101}),
        lambda payload: payload["sources"].update(
            {"Bad Source Name": payload["sources"]["unknown"]}
        ),
    ],
)
def test_invalid_explicit_policy_fails_closed_without_default_fallback(
    mutation: object,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = deepcopy(DEFAULT_POLICY_PAYLOAD)
    mutation(payload)
    _use_policy(monkeypatch, tmp_path, payload)

    with pytest.raises(PolicyConfigError):
        get_active_policy()
    with pytest.raises(PolicyConfigError):
        evaluate_request([event()], request(), run_id="invalid-policy")


def test_validation_errors_do_not_echo_unknown_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = deepcopy(DEFAULT_POLICY_PAYLOAD)
    secret_marker = "DO-NOT-ECHO-THIS"
    payload["api_secret"] = secret_marker
    _use_policy(monkeypatch, tmp_path, payload)

    with pytest.raises(PolicyConfigError) as captured:
        get_active_policy()
    assert secret_marker not in str(captured.value)


def test_oversize_and_unreadable_explicit_policies_are_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    oversize = tmp_path / "oversize.json"
    oversize.write_bytes(b" " * (MAX_POLICY_BYTES + 1))
    monkeypatch.setenv(POLICY_PATH_ENV, str(oversize))
    with pytest.raises(PolicyConfigError, match="exceeds"):
        get_active_policy()

    monkeypatch.setenv(POLICY_PATH_ENV, str(tmp_path / "missing.json"))
    with pytest.raises(PolicyConfigError, match="cannot be opened"):
        get_active_policy()


def test_policy_version_and_fingerprint_are_bound_to_decision_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first = evaluate_request([event()], request(), run_id="policy-identity")

    payload = deepcopy(DEFAULT_POLICY_PAYLOAD)
    payload["policy_version"] = "company-policy-2"
    _use_policy(monkeypatch, tmp_path, payload)
    second = evaluate_request([event()], request(), run_id="policy-identity")

    assert first.policy_version != second.policy_version
    assert first.policy_fingerprint != second.policy_fingerprint
    assert first.decision_id != second.decision_id


def test_policy_file_changes_and_environment_cleanup_are_predictable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = deepcopy(DEFAULT_POLICY_PAYLOAD)
    payload["policy_version"] = "company-policy-a"
    path = _use_policy(monkeypatch, tmp_path, payload)

    first = get_active_policy()
    same_content = get_active_policy()
    assert same_content is first

    payload["policy_version"] = "company-policy-b"
    path.write_text(json.dumps(payload), encoding="utf-8")
    changed = get_active_policy()
    assert changed.policy_version == "company-policy-b"
    assert changed.policy_fingerprint != first.policy_fingerprint

    monkeypatch.delenv(POLICY_PATH_ENV)
    assert get_active_policy() is DEFAULT_POLICY


def test_unknown_event_types_use_configured_unknown_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = deepcopy(DEFAULT_POLICY_PAYLOAD)
    payload["policy_version"] = "unknown-fallback-1"
    payload["sources"]["unknown"] = {
        "rank": 7,
        "trust_cap": 0.25,
        "label": "Company unknown",
    }
    _use_policy(monkeypatch, tmp_path, payload)

    unknown = event(source_type="brand_new_connector", trust_score=0.99)
    assert policy_for(unknown).rank == 7
    assert policy_for(unknown).label == "Company unknown"
    assert effective_trust(unknown) == 0.25


def test_explicit_policy_snapshot_prevents_render_time_policy_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    official = event()
    copied = event(
        event_id="evt-copy",
        field_value="2 Innovation Street",
        source_name="Company-approved mirror",
        source_type="copied_webpage",
        trust_score=0.99,
        evidence_reference="synthetic://copy",
        status="confirmed",
    )
    action = request(
        requested_value="2 Innovation Street", supporting_event_id="evt-copy"
    )

    payload = deepcopy(DEFAULT_POLICY_PAYLOAD)
    payload["policy_version"] = "company-reversed-policy-1"
    payload["sources"]["registration_confirmation"]["rank"] = 50
    payload["sources"]["copied_webpage"]["rank"] = 100
    payload["sources"]["copied_webpage"]["trust_cap"] = 1.0
    _use_policy(monkeypatch, tmp_path, payload)

    current = evaluate_request([official, copied], action, run_id="current-policy")
    snapshot = evaluate_request(
        [official, copied],
        action,
        run_id="snapshot-policy",
        policy=DEFAULT_POLICY,
    )
    package = build_evidence_package([official, copied], action, policy=DEFAULT_POLICY)

    assert current.decision == EnforcementDecision.ALLOW
    assert snapshot.decision == EnforcementDecision.BLOCK
    assert snapshot.policy_fingerprint == DEFAULT_POLICY.policy_fingerprint
    assert package["authoritative_event_id"] == official.event_id
