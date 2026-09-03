from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from context_gate.company_profile import (
    DEFAULT_COMPANY_PROFILE,
    MAX_IDENTITY_FIELDS,
    CompanyProfile,
    CompanyProfileError,
    load_company_profile,
    parse_identity_fields,
    save_company_profile,
)


def test_company_profile_round_trip_is_strict_and_normalized(tmp_path: Path) -> None:
    path = tmp_path / "company-profile.json"
    profile = CompanyProfile(
        company_name="Example Company",
        important_detail="Confirmed crowd size",
        identity_fields=[" Event name ", "Event   date"],
        risk_posture="custom_policy",
        source_mode="company_api",
        auto_monitor_enabled=True,
        auto_monitor_minutes=30,
    )

    save_company_profile(path, profile)
    loaded = load_company_profile(path)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert loaded == profile
    assert loaded.identity_fields == ["Event name", "Event date"]
    assert loaded.identity_summary == "Event name, Event date"
    assert loaded.auto_monitor_enabled is True
    assert loaded.auto_monitor_minutes == 30
    assert payload == profile.model_dump(mode="json")
    assert list(payload) == sorted(payload)


def test_missing_profile_returns_safe_first_run_default(tmp_path: Path) -> None:
    profile = load_company_profile(tmp_path / "missing.json")
    assert profile == DEFAULT_COMPANY_PROFILE
    assert profile.company_name == ""
    assert profile.operator_name == ""
    assert profile.auto_monitor_enabled is False
    assert profile.auto_monitor_minutes == 15


def test_existing_profile_without_monitor_fields_loads_with_monitoring_off(
    tmp_path: Path,
) -> None:
    path = tmp_path / "older-company-profile.json"
    path.write_text(
        json.dumps(
            {
                "company_name": "Example Company",
                "operator_name": "Example Operator",
            }
        ),
        encoding="utf-8",
    )

    profile = load_company_profile(path)

    assert profile.company_name == "Example Company"
    assert profile.auto_monitor_enabled is False
    assert profile.auto_monitor_minutes == 15


@pytest.mark.parametrize(
    "payload",
    [
        {
            "company_name": "Demo\nCompany",
            "important_detail": "Crowd size",
            "identity_fields": ["Event name"],
        },
        {
            "company_name": "Demo Company",
            "important_detail": "Crowd size",
            "identity_fields": ["Event name", " event NAME "],
        },
        {
            "company_name": "Demo Company",
            "important_detail": "Crowd size",
            "identity_fields": ["Event name"],
            "mailbox_token": "must-not-be-accepted",
        },
        {
            "company_name": "Demo Company",
            "important_detail": "Crowd size",
            "identity_fields": ["Event name"],
            "risk_posture": "disable_safety",
        },
    ],
)
def test_company_profile_rejects_unsafe_or_unknown_values(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        CompanyProfile.model_validate(payload)


def test_identity_field_parser_accepts_commas_and_semicolons_but_is_bounded() -> None:
    assert parse_identity_fields("Event name, Event date; Venue") == [
        "Event name",
        "Event date",
        "Venue",
    ]

    with pytest.raises(CompanyProfileError, match="at least one"):
        parse_identity_fields(" , ; ")
    with pytest.raises(CompanyProfileError, match=str(MAX_IDENTITY_FIELDS)):
        parse_identity_fields(",".join(f"field-{index}" for index in range(9)))


def test_invalid_profile_file_uses_a_sanitized_error(tmp_path: Path) -> None:
    secret_marker = "fictional-secret-that-must-not-leak"
    path = tmp_path / "invalid-profile.json"
    path.write_text(f'{{"company_name":"{secret_marker}"', encoding="utf-8")

    with pytest.raises(CompanyProfileError) as raised:
        load_company_profile(path)

    assert str(raised.value) == "Company profile could not be read safely."
    assert secret_marker not in str(raised.value)


@pytest.mark.parametrize("minutes", [0, 1441])
def test_auto_monitor_interval_is_bounded(minutes: int) -> None:
    with pytest.raises(ValidationError):
        CompanyProfile(auto_monitor_minutes=minutes)
