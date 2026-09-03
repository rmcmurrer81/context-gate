"""Bundled, fully synthetic scenarios for trying ContextGate locally."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from .models import (
    ActionRequest,
    Classification,
    ContextEvent,
    EnforcementDecision,
)

DATA_ROOT = Path(__file__).resolve().parent / "data"


def load_demo_events(path: str | Path | None = None) -> list[ContextEvent]:
    source = Path(path) if path else DATA_ROOT / "synthetic_context_events.json"
    return [
        ContextEvent.model_validate(item)
        for item in json.loads(source.read_text(encoding="utf-8"))
    ]


def load_demo_request(path: str | Path | None = None) -> ActionRequest:
    source = Path(path) if path else DATA_ROOT / "synthetic_action_requests.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    item = payload[0] if isinstance(payload, list) else payload
    return ActionRequest.model_validate(item)


ScenarioLoader = Callable[[], tuple[list[ContextEvent], ActionRequest]]


@dataclass(frozen=True, slots=True)
class Scenario:
    """Metadata and a fresh-input loader for one runnable example."""

    case_id: str
    name: str
    title: str
    description: str
    expected_classification: Classification
    expected_decision: EnforcementDecision
    _loader: ScenarioLoader = field(repr=False)

    def load(self) -> tuple[list[ContextEvent], ActionRequest]:
        """Return new model instances so callers can safely modify their inputs."""

        events, request = self._loader()
        return (
            [event.model_copy(deep=True) for event in events],
            request.model_copy(deep=True),
        )

    def summary(self) -> dict[str, str]:
        """Return JSON-friendly catalog metadata."""

        return {
            "case_id": self.case_id,
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "expected_classification": self.expected_classification.value,
            "expected_decision": self.expected_decision.value,
        }


def _load_conflict() -> tuple[list[ContextEvent], ActionRequest]:
    return load_demo_events(), load_demo_request()


def _official_event() -> ContextEvent:
    try:
        return next(
            event for event in load_demo_events() if event.event_id == "evt-100"
        )
    except StopIteration as exc:  # pragma: no cover - protects edited demo fixtures
        raise ValueError("the bundled demo is missing official event evt-100") from exc


def _derived_event(**updates: object) -> ContextEvent:
    payload = _official_event().model_dump(mode="json")
    payload.update(updates)
    return ContextEvent.model_validate(payload)


def _derived_request(**updates: object) -> ActionRequest:
    payload = load_demo_request().model_dump(mode="json")
    payload.update(updates)
    return ActionRequest.model_validate(payload)


def _load_safe() -> tuple[list[ContextEvent], ActionRequest]:
    official = _official_event()
    request = load_demo_request().model_copy(
        update={
            "request_id": "req-202",
            "action_id": "act-202",
            "action_type": "preview_calendar_update",
            "requested_value": official.field_value,
            "supporting_event_id": official.event_id,
            "consequential": False,
        }
    )
    return [official], request


def _load_missing_provenance() -> tuple[list[ContextEvent], ActionRequest]:
    incomplete = _official_event().model_copy(
        update={
            "event_id": "evt-105",
            "source_name": None,
            "evidence_reference": None,
        }
    )
    request = load_demo_request().model_copy(
        update={
            "request_id": "req-203",
            "action_id": "act-203",
            "action_type": "preview_calendar_update",
            "requested_value": incomplete.field_value,
            "supporting_event_id": incomplete.event_id,
            "consequential": False,
        }
    )
    return [incomplete], request


def _load_stale_agenda() -> tuple[list[ContextEvent], ActionRequest]:
    current = _derived_event(
        event_id="evt-210",
        field_name="agenda_start",
        field_value="2:00 PM",
        source_name="Organizer Schedule API",
        source_type="organizer_api",
        observed_at="2026-09-02T16:00:00Z",
        effective_at="2026-09-03T14:00:00Z",
        evidence_reference="synthetic-api://nova-summit/evt-210",
    )
    action = _derived_request(
        request_id="req-210",
        action_id="act-210",
        action_type="publish_agenda_start",
        field_name="agenda_start",
        requested_value=current.field_value,
        supporting_event_id=current.event_id,
        requested_effective_at="2026-09-03T13:00:00Z",
    )
    return [current], action


def _load_awards_time_conflict() -> tuple[list[ContextEvent], ActionRequest]:
    official = _derived_event(
        event_id="evt-310",
        field_name="awards_time",
        field_value="4:30 PM",
        source_name="Organizer Event Page",
        source_type="organizer_website",
        observed_at="2026-09-01T15:00:00Z",
        effective_at="2026-09-03T20:30:00Z",
        evidence_reference="synthetic-site://nova-summit/evt-310",
    )
    copied = _derived_event(
        event_id="evt-311",
        field_name="awards_time",
        field_value="5:00 PM",
        source_name="Community Agenda Copy",
        source_type="copied_webpage",
        trust_score=0.61,
        observed_at="2026-09-02T17:00:00Z",
        effective_at="2026-09-03T20:30:00Z",
        evidence_reference="synthetic-copy://nova-summit/evt-311",
        status="unverified",
    )
    action = _derived_request(
        request_id="req-310",
        action_id="act-310",
        action_type="send_awards_reminder",
        field_name="awards_time",
        requested_value=copied.field_value,
        supporting_event_id=copied.event_id,
        requested_effective_at="2026-09-03T20:30:00Z",
    )
    return [official, copied], action


def _load_near_peer_time_conflict() -> tuple[list[ContextEvent], ActionRequest]:
    confirmation = _derived_event(
        event_id="evt-410",
        field_name="check_in_time",
        field_value="9:00 AM",
        source_name="Registration Confirmation",
        source_type="registration_confirmation",
        observed_at="2026-09-01T14:00:00Z",
        effective_at="2026-09-03T13:00:00Z",
        evidence_reference="synthetic-confirmation://nova-summit/evt-410",
    )
    organizer_api = _derived_event(
        event_id="evt-411",
        field_name="check_in_time",
        field_value="9:15 AM",
        source_name="Organizer Check-in API",
        source_type="organizer_api",
        trust_score=0.97,
        observed_at="2026-09-02T17:15:00Z",
        effective_at="2026-09-03T13:00:00Z",
        evidence_reference="synthetic-api://nova-summit/evt-411",
    )
    action = _derived_request(
        request_id="req-410",
        action_id="act-410",
        action_type="preview_check_in_notice",
        field_name="check_in_time",
        requested_value=confirmation.field_value,
        supporting_event_id=confirmation.event_id,
        requested_effective_at="2026-09-03T13:00:00Z",
        consequential=False,
    )
    return [confirmation, organizer_api], action


def _load_consequential_calendar() -> tuple[list[ContextEvent], ActionRequest]:
    official = _official_event()
    action = _derived_request(
        request_id="req-510",
        action_id="act-510",
        action_type="update_attendee_calendar",
        requested_value=official.field_value,
        supporting_event_id=official.event_id,
        consequential=True,
    )
    return [official], action


def _load_accessibility_preview() -> tuple[list[ContextEvent], ActionRequest]:
    entrance = _derived_event(
        event_id="evt-610",
        field_name="accessible_entrance",
        field_value="West Lobby",
        source_name="Organizer Accessibility Page",
        source_type="organizer_website",
        trust_score=0.96,
        observed_at="2026-09-01T16:00:00Z",
        effective_at="2026-09-03T12:30:00Z",
        evidence_reference="synthetic-site://nova-summit/evt-610",
    )
    action = _derived_request(
        request_id="req-610",
        action_id="act-610",
        action_type="preview_accessibility_card",
        field_name="accessible_entrance",
        requested_value=entrance.field_value,
        supporting_event_id=entrance.event_id,
        consequential=False,
    )
    return [entrance], action


def _load_webcast_preview() -> tuple[list[ContextEvent], ActionRequest]:
    webcast = _derived_event(
        event_id="evt-710",
        field_name="keynote_webcast",
        field_value="https://stream.example.invalid/nova-keynote",
        source_name="Verified Organizer Email",
        source_type="official_email",
        trust_score=0.96,
        observed_at="2026-09-02T16:30:00Z",
        effective_at="2026-09-03T13:30:00Z",
        evidence_reference="synthetic-email://nova-summit/evt-710",
    )
    action = _derived_request(
        request_id="req-710",
        action_id="act-710",
        action_type="preview_webcast_button",
        field_name="keynote_webcast",
        requested_value=webcast.field_value,
        supporting_event_id=webcast.event_id,
        requested_effective_at="2026-09-03T13:30:00Z",
        consequential=False,
    )
    return [webcast], action


_SCENARIOS = (
    Scenario(
        case_id="B1",
        name="conflict",
        title="Lower-authority conflict",
        description=(
            "A community listing conflicts with the official venue confirmation."
        ),
        expected_classification=Classification.CONFLICT,
        expected_decision=EnforcementDecision.BLOCK,
        _loader=_load_conflict,
    ),
    Scenario(
        case_id="A1",
        name="safe",
        title="Safe preview",
        description=(
            "A non-consequential preview matches complete, authoritative evidence."
        ),
        expected_classification=Classification.SAFE,
        expected_decision=EnforcementDecision.ALLOW,
        _loader=_load_safe,
    ),
    Scenario(
        case_id="R1",
        name="missing-provenance",
        title="Missing provenance",
        description=(
            "A matching value lacks source identity and an evidence reference."
        ),
        expected_classification=Classification.INSUFFICIENT_EVIDENCE,
        expected_decision=EnforcementDecision.REVIEW,
        _loader=_load_missing_provenance,
    ),
    Scenario(
        case_id="B2",
        name="stale-agenda",
        title="Stale agenda request",
        description="An action asks to publish a time older than the current schedule.",
        expected_classification=Classification.STALE,
        expected_decision=EnforcementDecision.BLOCK,
        _loader=_load_stale_agenda,
    ),
    Scenario(
        case_id="B3",
        name="awards-time-conflict",
        title="Copied awards-time conflict",
        description="A copied agenda conflicts with the organizer's awards time.",
        expected_classification=Classification.CONFLICT,
        expected_decision=EnforcementDecision.BLOCK,
        _loader=_load_awards_time_conflict,
    ),
    Scenario(
        case_id="R2",
        name="near-peer-time-conflict",
        title="Near-peer check-in conflict",
        description="Two high-authority sources disagree about check-in time.",
        expected_classification=Classification.CONFLICT,
        expected_decision=EnforcementDecision.REVIEW,
        _loader=_load_near_peer_time_conflict,
    ),
    Scenario(
        case_id="R3",
        name="consequential-calendar",
        title="Consequential calendar write",
        description="The value is verified, but the requested external write needs approval.",
        expected_classification=Classification.SAFE,
        expected_decision=EnforcementDecision.REVIEW,
        _loader=_load_consequential_calendar,
    ),
    Scenario(
        case_id="A2",
        name="accessibility-preview",
        title="Accessibility preview",
        description="A reversible preview matches the organizer's accessibility page.",
        expected_classification=Classification.SAFE,
        expected_decision=EnforcementDecision.ALLOW,
        _loader=_load_accessibility_preview,
    ),
    Scenario(
        case_id="A3",
        name="webcast-preview",
        title="Webcast-link preview",
        description="A preview uses the verified webcast link without publishing it.",
        expected_classification=Classification.SAFE,
        expected_decision=EnforcementDecision.ALLOW,
        _loader=_load_webcast_preview,
    ),
)

SCENARIO_CATALOG: Mapping[str, Scenario] = MappingProxyType(
    {scenario.name: scenario for scenario in _SCENARIOS}
)


def iter_scenarios() -> Iterator[Scenario]:
    """Iterate through scenarios in their user-facing display order."""

    return iter(_SCENARIOS)


def scenario_names() -> tuple[str, ...]:
    """Return valid scenario names in display order."""

    return tuple(SCENARIO_CATALOG)


def scenario_identifiers() -> tuple[str, ...]:
    """Return every CLI-friendly scenario name and short case ID."""

    return (*scenario_names(), *(scenario.case_id for scenario in _SCENARIOS))


def get_scenario(name: str) -> Scenario:
    """Look up a scenario and provide a useful error for programmatic callers."""

    try:
        return SCENARIO_CATALOG[name]
    except KeyError as exc:
        available = ", ".join(scenario_names())
        raise ValueError(
            f"unknown scenario {name!r}; choose from: {available}"
        ) from exc


def resolve_scenario(identifier: str) -> Scenario:
    """Resolve either a descriptive scenario name or a short case ID."""

    normalized = identifier.casefold()
    match = next(
        (
            scenario
            for scenario in _SCENARIOS
            if scenario.name.casefold() == normalized
            or scenario.case_id.casefold() == normalized
        ),
        None,
    )
    if match is None:
        available = ", ".join(scenario_names())
        case_ids = ", ".join(scenario.case_id for scenario in _SCENARIOS)
        raise ValueError(
            f"unknown scenario {identifier!r}; choose from: {available}; "
            f"case IDs: {case_ids}"
        )
    return match


def load_scenario(name: str) -> tuple[list[ContextEvent], ActionRequest]:
    """Load fresh events and an action request for a named scenario."""

    return resolve_scenario(name).load()
