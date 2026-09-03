"""Generate JSON Schema artifacts from the authoritative Pydantic models."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "schemas"
sys.path.insert(0, str(ROOT))

from context_gate.models import (
    ActionRequest,
    ContextEvent,
    DecisionRecord,
)


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    models = {
        "context_event.schema.json": ContextEvent,
        "action_request.schema.json": ActionRequest,
        "decision_record.schema.json": DecisionRecord,
    }
    for filename, model in models.items():
        path = OUTPUT / filename
        path.write_text(
            json.dumps(model.model_json_schema(), indent=2) + "\n", encoding="utf-8"
        )
        print(path.relative_to(ROOT))


if __name__ == "__main__":
    main()
