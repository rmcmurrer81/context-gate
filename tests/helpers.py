from __future__ import annotations

from copy import deepcopy

from context_gate.models import ActionRequest, ContextEvent

BASE_EVENT = {
    "event_id": "evt-official",
    "entity_id": "demo",
    "field_name": "venue",
    "field_value": "10 Innovation Street",
    "source_name": "Official Confirmation",
    "source_type": "registration_confirmation",
    "trust_score": 0.98,
    "observed_at": "2026-09-01T12:00:00Z",
    "effective_at": "2026-09-03T12:00:00Z",
    "sensitivity": "public",
    "evidence_reference": "synthetic://official",
    "content_hash": None,
    "status": "confirmed",
}

BASE_REQUEST = {
    "request_id": "req-1",
    "action_id": "act-1",
    "action_type": "preview_update",
    "entity_id": "demo",
    "field_name": "venue",
    "requested_value": "10 Innovation Street",
    "supporting_event_id": "evt-official",
    "consequential": False,
    "created_at": "2026-09-02T12:00:00Z",
}


def event(**updates: object) -> ContextEvent:
    payload = deepcopy(BASE_EVENT)
    payload.update(updates)
    return ContextEvent.model_validate(payload)


def request(**updates: object) -> ActionRequest:
    payload = deepcopy(BASE_REQUEST)
    payload.update(updates)
    return ActionRequest.model_validate(payload)
