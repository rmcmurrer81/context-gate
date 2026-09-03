"""Grounded evidence packaging for deterministic rules and optional agents."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .authority import authority_key, choose_authoritative, effective_trust, policy_for
from .models import ActionRequest, ContextEvent
from .normalization import normalize_event, normalize_request
from .policy_config import ActivePolicy, get_active_policy


def missing_evidence_fields(event: ContextEvent) -> list[str]:
    """Return provenance fields the gate refuses to infer."""

    missing: list[str] = []
    for field in (
        "source_name",
        "source_type",
        "observed_at",
        "effective_at",
        "content_hash",
    ):
        if getattr(event, field) in (None, ""):
            missing.append(field)
    if not event.evidence_uri and not event.evidence_reference:
        missing.append("evidence_reference")
    return missing


def relevant_events(
    events: Iterable[ContextEvent], request: ActionRequest
) -> list[ContextEvent]:
    return [
        event
        for event in events
        if event.entity_id == request.entity_id
        and event.field_name == request.field_name
    ]


def events_available_as_of(
    events: Iterable[ContextEvent], request: ActionRequest
) -> list[ContextEvent]:
    """Keep only matching evidence that existed when the action was requested.

    An event with a missing observation time stays in scope so deterministic rules can
    route it to missing-provenance review instead of silently dropping it.
    """

    return [
        event
        for event in relevant_events(events, request)
        if event.observed_at is None or event.observed_at <= request.created_at
    ]


def unique_events(
    events: Iterable[ContextEvent], policy: ActivePolicy | None = None
) -> list[ContextEvent]:
    active_policy = policy or get_active_policy()
    selected: dict[str, ContextEvent] = {}
    for event in events:
        digest = event.content_hash or event.event_id
        current = selected.get(digest)
        if current is None or authority_key(event, active_policy) > authority_key(
            current, active_policy
        ):
            selected[digest] = event
    return sorted(selected.values(), key=lambda item: item.event_id)


def build_evidence_package(
    events: list[ContextEvent],
    request: ActionRequest,
    *,
    policy: ActivePolicy | None = None,
) -> dict[str, Any]:
    active_policy = policy or get_active_policy()
    normalized_request = normalize_request(request)
    normalized_events = [normalize_event(event) for event in events]
    related = relevant_events(normalized_events, normalized_request)
    scoped = unique_events(
        events_available_as_of(related, normalized_request), active_policy
    )
    excluded_future_event_ids = sorted(
        event.event_id
        for event in related
        if event.observed_at is not None
        and event.observed_at > normalized_request.created_at
    )
    supporting_is_bound = bool(normalized_request.supporting_event_id) and any(
        event.event_id == normalized_request.supporting_event_id
        and event.field_value.casefold()
        == normalized_request.requested_value.casefold()
        for event in scoped
    )
    complete = bool(scoped) and all(
        not missing_evidence_fields(event) for event in scoped
    )
    authoritative = (
        choose_authoritative(scoped, active_policy)
        if complete and supporting_is_bound
        else None
    )
    return {
        "request": normalized_request.model_dump(mode="json"),
        "authoritative_event_id": authoritative.event_id if authoritative else None,
        "authoritative_value": authoritative.field_value if authoritative else None,
        "excluded_future_event_ids": excluded_future_event_ids,
        "events": [
            {
                **event.model_dump(mode="json"),
                "policy_rank": policy_for(event, active_policy).rank,
                "effective_trust": effective_trust(event, active_policy),
            }
            for event in sorted(
                scoped,
                key=lambda item: (
                    item.observed_at or normalized_request.created_at,
                    item.event_id,
                ),
            )
        ],
    }
