"""Read-only self-check for a safe local ContextGate installation."""

from __future__ import annotations

import importlib.metadata
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if TYPE_CHECKING:
    from context_gate.models import DecisionRecord


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One diagnostic result with a human-readable detail."""

    name: str
    passed: bool
    detail: str


def check_python() -> str:
    """Confirm the interpreter meets the package's declared minimum."""
    version = sys.version_info
    if version < (3, 11):
        raise RuntimeError(
            f"Python {version.major}.{version.minor} is too old; install Python 3.11+."
        )
    return f"Python {version.major}.{version.minor}.{version.micro}"


def check_dependencies() -> str:
    """Confirm the application and local document-intake dependencies."""
    versions = {}
    missing = []
    for name in ("pydantic", "streamlit", "pypdf", "python-docx"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            missing.append(name)
    if missing:
        raise RuntimeError(
            f"missing {', '.join(missing)}; run the launcher or "
            "python -m pip install -r requirements.txt"
        )
    return ", ".join(f"{name} {version}" for name, version in versions.items())


def check_project_files() -> str:
    """Confirm files needed by the local application are present."""
    required = (
        "app.py",
        "context_gate/data/synthetic_context_events.json",
        "context_gate/data/synthetic_action_requests.json",
        "schemas/context_event.schema.json",
        "schemas/action_request.schema.json",
        "schemas/decision_record.schema.json",
        "schemas/review_event.schema.json",
        "config/source_policy.example.json",
        "config/semantic_profile.example.json",
        "examples/company/context-events.json",
        "examples/company/action-request.json",
        "examples/company/semantic-state.json",
        "examples/company/semantic-statements.json",
        "scripts/acceptance_matrix.py",
    )
    missing = [relative for relative in required if not (ROOT / relative).is_file()]
    if missing:
        raise RuntimeError(f"missing required files: {', '.join(missing)}")
    return f"{len(required)} required files found"


def check_schemas() -> str:
    """Ensure committed JSON Schemas match the authoritative models."""
    from context_gate.models import (
        ActionRequest,
        ContextEvent,
        DecisionRecord,
        ReviewEvent,
    )

    schemas = {
        "context_event.schema.json": ContextEvent,
        "action_request.schema.json": ActionRequest,
        "decision_record.schema.json": DecisionRecord,
        "review_event.schema.json": ReviewEvent,
    }
    stale = []
    for filename, model in schemas.items():
        stored = json.loads((ROOT / "schemas" / filename).read_text(encoding="utf-8"))
        if stored != model.model_json_schema():
            stale.append(filename)
    if stale:
        raise RuntimeError(
            "schema artifacts are stale: "
            f"{', '.join(stale)}; run python scripts/export_schemas.py"
        )
    return f"{len(schemas)} schema artifacts match the models"


def build_demo_decision() -> DecisionRecord:
    """Run the deterministic acceptance scenario without a browser or network."""
    from context_gate.decision_engine import evaluate_request
    from context_gate.scenario import load_demo_events, load_demo_request

    return evaluate_request(
        load_demo_events(), load_demo_request(), run_id="doctor-smoke"
    )


def check_policy() -> str:
    """Validate the active policy without exposing source labels or file paths."""
    from context_gate.policy_config import get_active_policy

    policy = get_active_policy()
    return (
        f"{policy.policy_version} · {len(policy.sources)} source types · "
        f"fingerprint {policy.policy_fingerprint[:12]}…"
    )


def check_decision() -> str:
    """Confirm the engine runs and the default policy fails closed as designed."""
    from context_gate.policy_config import DEFAULT_POLICY, get_active_policy

    decision = build_demo_decision()
    active_policy = get_active_policy()
    if (
        decision.policy_version != active_policy.policy_version
        or decision.policy_fingerprint != active_policy.policy_fingerprint
    ):
        raise RuntimeError("decision receipt does not identify the active policy")

    if active_policy.policy_fingerprint != DEFAULT_POLICY.policy_fingerprint:
        return (
            "company policy evaluated the acceptance scenario as "
            f"{decision.classification.value} / {decision.decision.value} / "
            f"{decision.risk.value}"
        )

    expected = ("CONFLICT", "BLOCK", "HIGH")
    actual = (
        decision.classification.value,
        decision.decision.value,
        decision.risk.value,
    )
    if actual != expected:
        raise RuntimeError(f"expected {expected}, received {actual}")
    if decision.model_explanation_used:
        raise RuntimeError(
            "the credential-free acceptance run unexpectedly used a model"
        )
    return "synthetic conflict produces CONFLICT / BLOCK / HIGH"


def check_local_fallback() -> str:
    """Prove publishing remains a no-network operation in local mode."""
    from context_gate.confluent_adapter import ConfluentAdapter

    previous_mode = os.environ.get("CONTEXTGATE_MODE")
    os.environ["CONTEXTGATE_MODE"] = "local"
    try:
        receipt = ConfluentAdapter().publish(
            "doctor-no-network", "doctor", build_demo_decision()
        )
    finally:
        if previous_mode is None:
            os.environ.pop("CONTEXTGATE_MODE", None)
        else:
            os.environ["CONTEXTGATE_MODE"] = previous_mode

    if receipt.mode != "local" or receipt.delivered:
        raise RuntimeError("local fallback unexpectedly reported an external delivery")
    return "local fallback made no network delivery"


def run_check(name: str, check: Callable[[], str]) -> CheckResult:
    """Run a check and turn expected diagnostic failures into output."""
    try:
        return CheckResult(name, True, check())
    # A diagnostic runner must keep going so it can report every failed subsystem.
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, False, f"{type(exc).__name__}: {exc}")


def main() -> int:
    """Run all checks and return a shell-friendly exit status."""
    print("ContextGate doctor (read-only; network disabled)")
    checks = (
        ("Python", check_python),
        ("Dependencies", check_dependencies),
        ("Project files", check_project_files),
        ("JSON Schemas", check_schemas),
        ("Active policy", check_policy),
        ("Decision engine", check_decision),
        ("Local fallback", check_local_fallback),
    )
    results = [run_check(name, check) for name, check in checks]
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"[{status}] {result.name}: {result.detail}")

    failures = sum(not result.passed for result in results)
    print(f"Result: {len(results) - failures} passed, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
