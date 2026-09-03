"""Run the vertical slice without a browser or model credentials."""

from __future__ import annotations

from .decision_engine import evaluate_request
from .scenario import load_demo_events, load_demo_request


def main() -> None:
    decision = evaluate_request(
        load_demo_events(), load_demo_request(), run_id="cli-demo"
    )
    print(decision.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
