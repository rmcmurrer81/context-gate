"""Grounded evidence packaging for deterministic rules and optional agents."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .authority import choose_authoritative, effective_trust, policy_for
from .models import ActionRequest, ContextEvent


def relevant_events(
    events: Iterable[ContextEvent], request: ActionRequest
) -> list[ContextEvent]:
    return [
        event
        for event in events
        if event.entity_id == request.entity_id
        and event.field_name == request.field_name
    ]


def unique_events(events: Iterable[ContextEvent]) -> list[ContextEvent]:
    seen: set[str] = set()
    result: list[ContextEvent] = []
    for event in events:
        digest = event.content_hash or event.event_id
        if digest not in seen:
            seen.add(digest)
            result.append(event)
    return result


def build_evidence_package(
    events: list[ContextEvent], request: ActionRequest
) -> dict[str, Any]:
    authoritative = choose_authoritative(events) if events else None
    return {
        "request": request.model_dump(mode="json"),
        "authoritative_event_id": authoritative.event_id if authoritative else None,
        "authoritative_value": authoritative.field_value if authoritative else None,
        "events": [
            {
                **event.model_dump(mode="json"),
                "policy_rank": policy_for(event).rank,
                "effective_trust": effective_trust(event),
            }
            for event in sorted(
                events, key=lambda item: item.observed_at or request.created_at
            )
        ],
    }
