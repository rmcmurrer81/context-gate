"""Run ContextGate scenarios or evaluate company-owned local evidence."""

from __future__ import annotations

import argparse
import json
import math
import os
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .decision_engine import evaluate_request
from .models import ActionRequest, ContextEvent
from .policy_config import PolicyConfigError, get_active_policy
from .scenario import iter_scenarios, load_scenario, scenario_identifiers

MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_INPUT_PATH_LENGTH = 4096
MAX_EVENTS = 10_000


class CliInputError(ValueError):
    """Raised when a local CLI input cannot be accepted safely."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m context_gate",
        description=(
            "Evaluate company evidence locally with deterministic ContextGate rules, "
            "or explore the bundled synthetic scenarios."
        ),
        epilog=(
            "Examples: python -m context_gate policy | "
            "python -m context_gate evaluate --events events.json "
            "--request request.json | python -m context_gate run safe"
        ),
    )
    subparsers = parser.add_subparsers(dest="command")

    list_parser = subparsers.add_parser("list", help="list bundled scenarios")
    list_parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable catalog metadata",
    )

    run_parser = subparsers.add_parser("run", help="run one bundled scenario")
    run_parser.add_argument("scenario", choices=scenario_identifiers())
    run_parser.add_argument(
        "--run-id",
        help="set the audit run ID (default: cli-<scenario>)",
    )

    subparsers.add_parser(
        "policy",
        aliases=["verify-policy"],
        help="validate the active company policy and print safe metadata",
    )

    evaluate_parser = subparsers.add_parser(
        "evaluate",
        help="evaluate company-owned local JSON without making external changes",
    )
    evaluate_parser.add_argument(
        "--events",
        required=True,
        metavar="PATH",
        help="JSON array of ContextEvent objects",
    )
    evaluate_parser.add_argument(
        "--request",
        required=True,
        metavar="PATH",
        help="JSON ActionRequest object",
    )
    evaluate_parser.add_argument(
        "--run-id",
        help="optional audit run ID (otherwise ContextGate creates one)",
    )
    return parser


def _list_scenarios(*, as_json: bool) -> None:
    scenarios = list(iter_scenarios())
    if as_json:
        print(json.dumps([scenario.summary() for scenario in scenarios], indent=2))
        return

    print("Bundled scenarios:")
    width = max(len(scenario.name) for scenario in scenarios)
    for scenario in scenarios:
        print(
            f"  {scenario.case_id:<2}  {scenario.name:<{width}}  "
            f"{scenario.expected_decision.value:<6}  {scenario.description}"
        )
    print("\nTry one: python -m context_gate run <name-or-case-id>")


def _run_scenario(name: str, run_id: str | None = None) -> None:
    events, request = load_scenario(name)
    decision = evaluate_request(
        events,
        request,
        run_id=run_id or f"cli-{name}",
    )
    print(decision.model_dump_json(indent=2))


def _reject_symbolic_links(path: Path, *, label: str) -> None:
    current = path
    while True:
        try:
            if current.is_symlink():
                raise CliInputError(f"{label} path must not contain symbolic links.")
        except OSError as exc:
            raise CliInputError(f"{label} path cannot be inspected.") from exc
        parent = current.parent
        if parent == current:
            return
        current = parent


def _bounded_json_file(raw_path: str, *, label: str) -> Any:
    if not raw_path or len(raw_path) > MAX_INPUT_PATH_LENGTH:
        raise CliInputError(f"{label} path is empty or too long.")
    path = Path(os.path.abspath(os.fspath(Path(raw_path).expanduser())))
    _reject_symbolic_links(path, label=label)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except (OSError, ValueError) as exc:
        raise CliInputError(f"{label} file cannot be opened.") from exc

    try:
        file_status = os.fstat(descriptor)
        if not stat.S_ISREG(file_status.st_mode):
            raise CliInputError(f"{label} path must identify a regular file.")
        if file_status.st_size > MAX_INPUT_BYTES:
            raise CliInputError(
                f"{label} file exceeds the {MAX_INPUT_BYTES}-byte limit."
            )
        chunks: list[bytes] = []
        remaining = MAX_INPUT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_INPUT_BYTES:
            raise CliInputError(
                f"{label} file exceeds the {MAX_INPUT_BYTES}-byte limit."
            )
    except OSError as exc:
        raise CliInputError(f"{label} file cannot be read.") from exc
    finally:
        os.close(descriptor)

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CliInputError(f"{label} file must be UTF-8 JSON.") from exc

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise CliInputError(f"{label} file contains a duplicate JSON key.")
            result[key] = value
        return result

    def reject_non_finite(_: str) -> None:
        raise CliInputError(f"{label} file contains a non-finite number.")

    def finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            reject_non_finite(value)
        return parsed

    try:
        return json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_constant=reject_non_finite,
            parse_float=finite_float,
        )
    except CliInputError:
        raise
    except json.JSONDecodeError as exc:
        raise CliInputError(
            f"{label} file is not valid JSON (line {exc.lineno}, column {exc.colno})."
        ) from exc
    except (RecursionError, ValueError) as exc:
        raise CliInputError(f"{label} file contains an invalid JSON value.") from exc


def _validation_summary(exc: ValidationError, *, label: str) -> str:
    details: list[str] = []
    for error in exc.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    )[:5]:
        location = ".".join(str(item) for item in error["loc"])
        location = f" at {location}" if location else ""
        details.append(f"{label}{location}: {error['msg']}")
    suffix = "; additional validation errors omitted" if exc.error_count() > 5 else ""
    return "; ".join(details) + suffix


def _load_company_inputs(
    events_path: str, request_path: str
) -> tuple[list[ContextEvent], ActionRequest]:
    events_payload = _bounded_json_file(events_path, label="Events")
    if not isinstance(events_payload, list):
        raise CliInputError("Events JSON must be an array of ContextEvent objects.")
    if not events_payload:
        raise CliInputError("Events JSON must contain at least one ContextEvent.")
    if len(events_payload) > MAX_EVENTS:
        raise CliInputError(f"Events JSON exceeds the {MAX_EVENTS}-event limit.")

    events: list[ContextEvent] = []
    for index, payload in enumerate(events_payload):
        if not isinstance(payload, dict):
            raise CliInputError(f"Events item {index} must be a JSON object.")
        try:
            events.append(
                ContextEvent.model_validate_json(
                    json.dumps(payload, ensure_ascii=False, allow_nan=False),
                    strict=True,
                )
            )
        except ValidationError as exc:
            raise CliInputError(
                _validation_summary(exc, label=f"Events item {index}")
            ) from exc

    request_payload = _bounded_json_file(request_path, label="Request")
    if not isinstance(request_payload, dict):
        raise CliInputError("Request JSON must be one ActionRequest object.")
    try:
        request = ActionRequest.model_validate_json(
            json.dumps(request_payload, ensure_ascii=False, allow_nan=False),
            strict=True,
        )
    except ValidationError as exc:
        raise CliInputError(_validation_summary(exc, label="Request")) from exc
    return events, request


def _print_policy_metadata() -> None:
    policy = get_active_policy()
    metadata = {
        "valid": True,
        "policy_version": policy.policy_version,
        "policy_fingerprint": policy.policy_fingerprint,
        "source_count": len(policy.sources),
        "minimum_automatic_authority_rank": (policy.minimum_automatic_authority_rank),
        "minimum_automatic_trust": policy.minimum_automatic_trust,
        "near_peer_max_authority_rank_gap": (policy.near_peer_max_authority_rank_gap),
        "near_peer_max_trust_gap": policy.near_peer_max_trust_gap,
    }
    print(json.dumps(metadata, indent=2, sort_keys=True))


def _evaluate_company_inputs(
    events_path: str, request_path: str, run_id: str | None
) -> None:
    events, request = _load_company_inputs(events_path, request_path)
    decision = evaluate_request(events, request, run_id=run_id)
    print(decision.model_dump_json(indent=2))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "list":
            _list_scenarios(as_json=args.json)
            return 0

        if args.command == "run":
            _run_scenario(args.scenario, args.run_id)
            return 0

        if args.command in {"policy", "verify-policy"}:
            _print_policy_metadata()
            return 0

        if args.command == "evaluate":
            _evaluate_company_inputs(args.events, args.request, args.run_id)
            return 0

        # Keep the original no-argument acceptance behavior for existing users.
        _run_scenario("conflict", "cli-acceptance")
        return 0
    except (CliInputError, PolicyConfigError) as exc:
        print(f"ContextGate error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
