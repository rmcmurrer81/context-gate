"""Input normalization that never fabricates missing provenance or timestamps."""

from __future__ import annotations

from .hashing import event_content_hash
from .models import ActionRequest, ContextEvent


def _clean(value: str) -> str:
    return " ".join(value.split())


def normalize_event(event: ContextEvent) -> ContextEvent:
    normalized = event.model_copy(
        update={
            "entity_id": _clean(event.entity_id).casefold(),
            "field_name": _clean(event.field_name).casefold(),
            "field_value": _clean(event.field_value),
            "source_name": _clean(event.source_name) if event.source_name else None,
            "source_type": _clean(event.source_type).casefold()
            if event.source_type
            else None,
            "evidence_reference": _clean(event.evidence_reference)
            if event.evidence_reference
            else None,
            "evidence_uri": _clean(event.evidence_uri) if event.evidence_uri else None,
        }
    )
    # Producer-supplied hashes are not trusted; the gate derives its own canonical hash.
    return normalized.model_copy(
        update={"content_hash": event_content_hash(normalized)}
    )


def normalize_request(request: ActionRequest) -> ActionRequest:
    return request.model_copy(
        update={
            "entity_id": _clean(request.entity_id).casefold(),
            "field_name": _clean(request.field_name).casefold(),
            "requested_value": _clean(request.requested_value),
            "action_type": _clean(request.action_type).casefold(),
        }
    )
