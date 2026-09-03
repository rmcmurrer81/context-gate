"""Load the fully synthetic Nova AI Summit demo scenario."""

from __future__ import annotations

import json
from pathlib import Path

from .models import ActionRequest, ContextEvent

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_demo_events(path: str | Path | None = None) -> list[ContextEvent]:
    source = (
        Path(path) if path else PROJECT_ROOT / "data" / "synthetic_context_events.json"
    )
    return [
        ContextEvent.model_validate(item)
        for item in json.loads(source.read_text(encoding="utf-8"))
    ]


def load_demo_request(path: str | Path | None = None) -> ActionRequest:
    source = (
        Path(path) if path else PROJECT_ROOT / "data" / "synthetic_action_requests.json"
    )
    payload = json.loads(source.read_text(encoding="utf-8"))
    item = payload[0] if isinstance(payload, list) else payload
    return ActionRequest.model_validate(item)
