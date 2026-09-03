import json

from context_gate.audit_log import AppendOnlyAuditLog
from context_gate.decision_engine import evaluate_request

from .helpers import event, request


def test_audit_chain_detects_tampering(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AppendOnlyAuditLog(path)
    log.append("one", {"value": 1})
    log.append("two", {"value": 2})
    assert log.verify_chain() is True

    lines = path.read_text(encoding="utf-8").splitlines()
    payload = json.loads(lines[0])
    payload["payload"]["value"] = 999
    lines[0] = json.dumps(payload)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert log.verify_chain() is False


def test_same_decision_is_idempotent(tmp_path) -> None:
    log = AppendOnlyAuditLog(tmp_path / "audit.jsonl")
    decision = evaluate_request([event()], request(), run_id="stable-run")
    log.append_decision(decision)
    log.append_decision(decision)
    assert len(log.read_entries()) == 1
