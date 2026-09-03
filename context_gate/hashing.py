"""Canonical hashing used for deduplication and audit integrity."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from .models import ContextEvent


def canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def sha256_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def event_content_hash(event: ContextEvent) -> str:
    """Hash semantic evidence content, excluding transport IDs and arrival time."""

    return sha256_digest(
        {
            "entity_id": event.entity_id.casefold(),
            "field_name": event.field_name.casefold(),
            "field_value": " ".join(event.field_value.split()).casefold(),
            "source_name": (event.source_name or "").casefold(),
            "source_type": (event.source_type or "").casefold(),
            "evidence_reference": event.evidence_reference or event.evidence_uri or "",
        }
    )
