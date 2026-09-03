"""Append-only JSONL audit log with a tamper-evident hash chain."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .hashing import sha256_digest
from .models import AuditEntry, DecisionRecord, ReviewEvent

GENESIS_HASH = "0" * 64


class AppendOnlyAuditLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def read_entries(self) -> list[AuditEntry]:
        if not self.path.exists():
            return []
        entries: list[AuditEntry] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entries.append(AuditEntry.model_validate_json(line))
        return entries

    def append(
        self,
        record_type: str,
        payload: Mapping[str, Any],
        *,
        unique_field: str | None = None,
    ) -> AuditEntry:
        with self._lock:
            entries = self.read_entries()
            if unique_field is not None:
                unique_value = payload.get(unique_field)
                for existing in entries:
                    if (
                        existing.record_type == record_type
                        and existing.payload.get(unique_field) == unique_value
                    ):
                        return existing
            previous_hash = entries[-1].entry_hash if entries else GENESIS_HASH
            body = {
                "audit_id": f"audit-{uuid4().hex[:12]}",
                "sequence": len(entries) + 1,
                "record_type": record_type,
                "created_at": datetime.now(UTC),
                "payload": dict(payload),
                "previous_hash": previous_hash,
            }
            entry = AuditEntry(**body, entry_hash=sha256_digest(body))
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(entry.model_dump_json() + "\n")
                handle.flush()
            return entry

    def append_decision(self, decision: DecisionRecord) -> AuditEntry:
        return self.append(
            "decision",
            decision.model_dump(mode="json"),
            unique_field="decision_id",
        )

    def append_review(self, review: ReviewEvent) -> AuditEntry:
        return self.append(
            "review",
            review.model_dump(mode="json"),
            unique_field="review_id",
        )

    def verify_chain(self) -> bool:
        previous_hash = GENESIS_HASH
        for entry in self.read_entries():
            body = entry.model_dump(mode="python", exclude={"entry_hash"})
            if (
                entry.previous_hash != previous_hash
                or sha256_digest(body) != entry.entry_hash
            ):
                return False
            previous_hash = entry.entry_hash
        return True
