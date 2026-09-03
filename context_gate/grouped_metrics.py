"""Bounded, deterministic grouped-metric intake for local CSV and JSON files.

This module deliberately knows nothing about sales, offices, or any other
business-specific vocabulary.  A caller supplies preferred identity and metric
field names; explicit JSON metadata or conservative tabular inference is used
as a fallback.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_GROUPED_METRIC_BYTES = 1 * 1024 * 1024
MAX_GROUPED_METRIC_ROWS = 5_000
MAX_GROUPS = 250
MAX_GROUPED_METRIC_DATASETS = 24
MAX_TRACKING_TOPICS = 24
MAX_TRACKING_STORE_BYTES = 64 * 1024


class GroupedMetricError(ValueError):
    """Raised when an explicitly structured metric file is invalid."""


class MetricEvidence(BaseModel):
    """One source row contributing to a deterministic grouped total."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    row_number: int = Field(ge=1, le=MAX_GROUPED_METRIC_ROWS + 1)
    group: str = Field(min_length=1, max_length=200)
    value: int | float
    reference: str = Field(min_length=1, max_length=1_000)


class GroupedMetricDataset(BaseModel):
    """Normalized grouped totals plus row-level evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: str = Field(min_length=1, max_length=100)
    dataset_name: str = Field(min_length=1, max_length=160)
    topic_id: str | None = Field(default=None, max_length=100)
    topic_name: str | None = Field(default=None, max_length=120)
    source_filename: str = Field(min_length=1, max_length=512)
    source_reference: str = Field(min_length=1, max_length=1_000)
    group_field: str = Field(min_length=1, max_length=100)
    metric_field: str = Field(min_length=1, max_length=100)
    unit: str | None = Field(default=None, max_length=40)
    row_count: int = Field(ge=1, le=MAX_GROUPED_METRIC_ROWS)
    group_totals: dict[str, int | float]
    evidence: list[MetricEvidence]
    fictional: bool = False


class TrackingTopic(BaseModel):
    """One independently retained chat-configured tracking definition."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    topic_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["grouped_metric", "named_filter"]
    metric_field: str | None = Field(default=None, max_length=100)
    group_fields: list[str] = Field(default_factory=list, max_length=8)
    query_scope: str = Field(min_length=1, max_length=160)
    created_at: datetime


class TrackingTopicStore:
    """Small atomic JSON store for multiple named, independently active topics."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._topics: list[TrackingTopic] = []
        self._active_topic_id: str | None = None
        self._previous_topic_id: str | None = None
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            if (
                not self.path.is_file()
                or self.path.stat().st_size > MAX_TRACKING_STORE_BYTES
            ):
                raise GroupedMetricError(
                    "Tracking-topic storage is not a valid bounded file."
                )
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != 1:
                raise ValueError
            topics = [
                TrackingTopic.model_validate(item) for item in payload.get("topics", [])
            ]
            if len(topics) > MAX_TRACKING_TOPICS or len(
                {item.topic_id for item in topics}
            ) != len(topics):
                raise ValueError
            valid_ids = {item.topic_id for item in topics}
            active = payload.get("active_topic_id")
            previous = payload.get("previous_topic_id")
            self._topics = topics
            self._active_topic_id = active if active in valid_ids else None
            self._previous_topic_id = previous if previous in valid_ids else None
        except GroupedMetricError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise GroupedMetricError(
                "Tracking-topic storage could not be read safely."
            ) from None

    def _save(self) -> None:
        payload = json.dumps(
            {
                "version": 1,
                "active_topic_id": self._active_topic_id,
                "previous_topic_id": self._previous_topic_id,
                "topics": [item.model_dump(mode="json") for item in self._topics],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        if len(payload) > MAX_TRACKING_STORE_BYTES:
            raise GroupedMetricError("Tracking-topic storage exceeds its safe limit.")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.is_symlink():
                raise GroupedMetricError(
                    "Tracking-topic storage must not be a symbolic link."
                )
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".tracking-topics-", suffix=".tmp", dir=self.path.parent
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_name, self.path)
            except Exception:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass
                raise
        except GroupedMetricError:
            raise
        except OSError:
            raise GroupedMetricError(
                "Tracking-topic storage could not be saved safely."
            ) from None

    def topics(self) -> list[TrackingTopic]:
        return list(self._topics)

    def active_topic(self) -> TrackingTopic | None:
        return next(
            (item for item in self._topics if item.topic_id == self._active_topic_id),
            None,
        )

    def snapshot(self) -> dict[str, object]:
        active = self.active_topic()
        return {
            "active_topic_id": active.topic_id if active else None,
            "active_topic": active.model_dump(mode="json") if active else None,
            "topics": [item.model_dump(mode="json") for item in self._topics],
            "switching_note": (
                "Switching the active chat/report context does not remove other topics "
                "or stop source collection."
            ),
        }

    def add_topic(
        self,
        *,
        name: str,
        kind: Literal["grouped_metric", "named_filter"],
        metric_field: str | None = None,
        group_fields: Sequence[str] = (),
        query_scope: str | None = None,
    ) -> TrackingTopic:
        safe_name = _safe_label(name, fallback="", maximum=120)
        safe_metric = (
            _safe_label(metric_field, fallback="", maximum=100)
            if metric_field
            else None
        )
        safe_groups = [
            _safe_label(item, fallback="", maximum=80) for item in group_fields
        ]
        if (
            not safe_name
            or not query_scope
            or (kind == "grouped_metric" and (not safe_metric or not safe_groups))
        ):
            raise GroupedMetricError("The proposed tracking topic is incomplete.")
        if len(safe_groups) > 8:
            raise GroupedMetricError("Use no more than eight grouping fields.")
        signature = _normal(
            f"{kind} {safe_name} {safe_metric or ''} {' '.join(safe_groups)}"
        )
        topic_id = f"topic-{sha256(signature.encode('utf-8')).hexdigest()[:16]}"
        existing = next(
            (item for item in self._topics if item.topic_id == topic_id), None
        )
        if existing is None:
            if len(self._topics) >= MAX_TRACKING_TOPICS:
                raise GroupedMetricError(
                    f"At most {MAX_TRACKING_TOPICS} tracking topics can be stored."
                )
            existing = TrackingTopic(
                topic_id=topic_id,
                name=safe_name,
                kind=kind,
                metric_field=safe_metric,
                group_fields=safe_groups,
                query_scope=_safe_label(query_scope, fallback=safe_name, maximum=160),
                created_at=datetime.now(UTC),
            )
            self._topics.append(existing)
        self._activate_id(existing.topic_id)
        self._save()
        return existing

    def _activate_id(self, topic_id: str) -> None:
        if topic_id != self._active_topic_id:
            self._previous_topic_id = self._active_topic_id
            self._active_topic_id = topic_id

    def activate(
        self, target: str | None = None, *, previous: bool = False
    ) -> TrackingTopic:
        if previous:
            match = next(
                (
                    item
                    for item in self._topics
                    if item.topic_id == self._previous_topic_id
                ),
                None,
            )
        else:
            normalized = _normal(target or "")
            exact = [item for item in self._topics if _normal(item.name) == normalized]
            partial = [
                item
                for item in self._topics
                if normalized
                and normalized in _normal(f"{item.name} {item.query_scope}")
            ]
            matches = exact or partial
            match = matches[0] if len(matches) == 1 else None
            if len(matches) > 1:
                raise GroupedMetricError(
                    "More than one tracking topic matches that name."
                )
        if match is None:
            raise GroupedMetricError("That tracking topic is not available.")
        self._activate_id(match.topic_id)
        self._save()
        return match

    def remove(self, topic_id: str) -> TrackingTopic:
        match = next((item for item in self._topics if item.topic_id == topic_id), None)
        if match is None:
            raise GroupedMetricError("That tracking topic is not available.")
        self._topics = [item for item in self._topics if item.topic_id != topic_id]
        if self._active_topic_id == topic_id:
            self._active_topic_id = (
                self._previous_topic_id
                if any(
                    item.topic_id == self._previous_topic_id for item in self._topics
                )
                else (self._topics[-1].topic_id if self._topics else None)
            )
        if self._previous_topic_id == topic_id:
            self._previous_topic_id = None
        self._save()
        return match


def _normal(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()


def _safe_label(value: object, *, fallback: str, maximum: int) -> str:
    label = " ".join(str(value or "").split())
    if not label:
        label = fallback
    if any(ord(character) < 32 for character in label):
        raise GroupedMetricError(
            "Structured metric labels contain unsupported characters."
        )
    return label[:maximum]


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        candidate = str(value)
    elif isinstance(value, str):
        candidate = value.strip().replace(",", "")
    else:
        return None
    if not re.fullmatch(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", candidate):
        return None
    try:
        parsed = Decimal(candidate)
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def _json_number(value: Decimal) -> int | float:
    integral = value.to_integral_value()
    if value == integral:
        return int(integral)
    return float(value)


def _candidate_names(values: Iterable[str]) -> list[str]:
    candidates: list[str] = []
    for value in values:
        normalized = _normal(value)
        if normalized:
            candidates.append(normalized)
            reduced = " ".join(
                token
                for token in normalized.split()
                if token
                not in {"total", "totals", "amount", "metric", "value", "values"}
            )
            if reduced:
                candidates.append(reduced)
    return list(dict.fromkeys(candidates))


def _select_preferred_field(
    headers: Sequence[str], preferred: Iterable[str]
) -> str | None:
    candidates = _candidate_names(preferred)
    normalized_headers = {header: _normal(header) for header in headers}
    for candidate in candidates:
        for header, normalized in normalized_headers.items():
            if normalized == candidate:
                return header
    for candidate in candidates:
        if len(candidate) < 4:
            continue
        for header, normalized in normalized_headers.items():
            if candidate in normalized or normalized in candidate:
                return header
    return None


def _records_from_csv(
    content: bytes,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise GroupedMetricError("Structured CSV must be UTF-8 text.") from None
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if not reader.fieldnames:
        return [], {}
    headers = [
        _safe_label(item, fallback="", maximum=100) for item in reader.fieldnames
    ]
    if any(not item for item in headers) or len(
        {_normal(item) for item in headers}
    ) != len(headers):
        raise GroupedMetricError("Structured CSV headers must be non-empty and unique.")
    records: list[dict[str, object]] = []
    for index, row in enumerate(reader, start=1):
        if index > MAX_GROUPED_METRIC_ROWS:
            raise GroupedMetricError(
                f"Structured metric files may contain at most {MAX_GROUPED_METRIC_ROWS} rows."
            )
        if None in row:
            raise GroupedMetricError(
                "A structured CSV row has more values than headers."
            )
        records.append(
            {
                header: row.get(original)
                for header, original in zip(headers, reader.fieldnames, strict=True)
            }
        )
    return records, {}


def _records_from_json(
    content: bytes,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    try:
        payload = json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GroupedMetricError("Structured JSON must be valid UTF-8 JSON.") from None
    metadata: dict[str, object] = {}
    rows: object = payload
    if isinstance(payload, Mapping):
        for key in (
            "dataset",
            "dataset_name",
            "group_by",
            "group_field",
            "metric",
            "metric_field",
            "unit",
            "fictional",
        ):
            if key in payload:
                metadata[key] = payload[key]
        rows = payload.get("rows", payload.get("data"))
        if rows is None:
            return [], metadata
    if not isinstance(rows, list):
        return [], metadata
    if len(rows) > MAX_GROUPED_METRIC_ROWS:
        raise GroupedMetricError(
            f"Structured metric files may contain at most {MAX_GROUPED_METRIC_ROWS} rows."
        )
    records: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, Mapping) or not all(isinstance(key, str) for key in row):
            raise GroupedMetricError(
                "Every structured metric row must be a JSON object."
            )
        records.append(dict(row))
    return records, metadata


def _infer_fields(
    records: Sequence[Mapping[str, object]],
    *,
    preferred_group_fields: Iterable[str],
    preferred_metric_fields: Iterable[str],
    metadata: Mapping[str, object],
    require_preferred_fields: bool,
) -> tuple[str, str] | None:
    if not records:
        return None
    headers = [str(key) for key in records[0]]
    if len(headers) < 2 or any(set(row) != set(headers) for row in records):
        return None
    explicit_group = metadata.get("group_by", metadata.get("group_field"))
    explicit_metric = metadata.get("metric", metadata.get("metric_field"))
    preferred_groups = list(preferred_group_fields)
    preferred_metrics = list(preferred_metric_fields)
    group_field = _select_preferred_field(
        headers,
        preferred_groups
        if require_preferred_fields and preferred_groups
        else ([str(explicit_group)] if explicit_group else preferred_groups),
    )
    metric_field = _select_preferred_field(
        headers,
        preferred_metrics
        if require_preferred_fields and preferred_metrics
        else ([str(explicit_metric)] if explicit_metric else preferred_metrics),
    )
    if require_preferred_fields and (group_field is None or metric_field is None):
        return None
    numeric_headers = [
        header
        for header in headers
        if all(_decimal(row.get(header)) is not None for row in records)
    ]
    if metric_field is None and len(numeric_headers) == 1:
        metric_field = numeric_headers[0]
    if group_field is None:
        text_headers = [
            header
            for header in headers
            if header != metric_field
            and all(str(row.get(header, "")).strip() for row in records)
            and any(_decimal(row.get(header)) is None for row in records)
        ]
        if len(text_headers) == 1:
            group_field = text_headers[0]
    if not group_field or not metric_field or group_field == metric_field:
        return None
    return group_field, metric_field


def parse_grouped_metric_artifact(
    filename: str,
    content_type: str,
    content: bytes,
    *,
    preferred_group_fields: Iterable[str] = (),
    preferred_metric_fields: Iterable[str] = (),
    source_reference: str | None = None,
    topic_id: str | None = None,
    topic_name: str | None = None,
    require_preferred_fields: bool = False,
) -> GroupedMetricDataset | None:
    """Parse one bounded CSV/JSON table, or return ``None`` when it is not metric data."""

    suffix = Path(filename).suffix.casefold()
    media_type = content_type.split(";", 1)[0].strip().casefold()
    if suffix not in {".csv", ".json"} and media_type not in {
        "text/csv",
        "application/csv",
        "application/json",
    }:
        return None
    if not content or len(content) > MAX_GROUPED_METRIC_BYTES:
        return None

    if suffix == ".csv" or media_type in {"text/csv", "application/csv"}:
        records, metadata = _records_from_csv(content)
    else:
        records, metadata = _records_from_json(content)
    fields = _infer_fields(
        records,
        preferred_group_fields=preferred_group_fields,
        preferred_metric_fields=preferred_metric_fields,
        metadata=metadata,
        require_preferred_fields=require_preferred_fields,
    )
    if fields is None:
        return None
    group_field, metric_field = fields
    digest = sha256(content).hexdigest()
    topic_signature = (
        f"-{sha256(topic_id.encode('utf-8')).hexdigest()[:8]}" if topic_id else ""
    )
    dataset_id = f"metric-{digest[:20]}{topic_signature}"
    reference = source_reference or f"upload://{dataset_id}"
    totals: dict[str, Decimal] = {}
    evidence: list[MetricEvidence] = []
    for index, row in enumerate(records, start=2 if suffix == ".csv" else 1):
        group = _safe_label(row.get(group_field), fallback="", maximum=200)
        numeric = _decimal(row.get(metric_field))
        if not group or numeric is None:
            raise GroupedMetricError(
                f"Row {index} must contain a group and a finite numeric metric value."
            )
        if group not in totals and len(totals) >= MAX_GROUPS:
            raise GroupedMetricError(
                f"Structured metric files may contain at most {MAX_GROUPS} groups."
            )
        totals[group] = totals.get(group, Decimal(0)) + numeric
        evidence.append(
            MetricEvidence(
                row_number=index,
                group=group,
                value=_json_number(numeric),
                reference=f"{reference}#row={index}",
            )
        )
    if not evidence:
        return None
    dataset_name = _safe_label(
        metadata.get("dataset_name", metadata.get("dataset")),
        fallback=Path(filename).stem.replace("_", " ").replace("-", " "),
        maximum=160,
    )
    unit_value = metadata.get("unit")
    unit = _safe_label(unit_value, fallback="", maximum=40) if unit_value else None
    fictional = bool(metadata.get("fictional")) or bool(
        re.search(
            r"\b(fictional|synthetic|demo)\b", _normal(f"{dataset_name} {filename}")
        )
    )
    return GroupedMetricDataset(
        dataset_id=dataset_id,
        dataset_name=dataset_name,
        topic_id=topic_id,
        topic_name=topic_name,
        source_filename=Path(filename).name,
        source_reference=reference,
        group_field=group_field,
        metric_field=metric_field,
        unit=unit,
        row_count=len(evidence),
        group_totals={group: _json_number(value) for group, value in totals.items()},
        evidence=evidence,
        fictional=fictional,
    )


def proposed_tracking_topic(question: str) -> dict[str, object] | None:
    """Extract a conservative named/grouped tracking proposal from chat text."""

    cleaned = " ".join(question.strip().split()).strip(" .!?")
    if re.search(r"\bevents?\s+(?:from|by|for)\b", cleaned, flags=re.IGNORECASE):
        return None
    explicit = re.search(
        r"\b(?:also\s+)?(?:track|configure|monitor)\s+(.{1,80}?)\s+by\s+(.{1,80})$",
        cleaned,
        flags=re.IGNORECASE,
    )
    if explicit:
        metric = _safe_label(explicit.group(1), fallback="", maximum=100)
        group = _safe_label(explicit.group(2), fallback="", maximum=80)
        metric = re.sub(r"\s+totals?$", "", metric, flags=re.IGNORECASE).strip()
        if metric and group:
            return {
                "name": f"{group} {metric}",
                "kind": "grouped_metric",
                "metric_field": metric,
                "group_fields": [group],
                "query_scope": f"{metric} by {group}",
            }
    setting = re.search(
        r"\bset\s+(?:(?:the|our|my)\s+)?important\s+detail\s+to\s+"
        r"(.{1,80}?)\s+and\s+(?:(?:group|identify)(?:\s+records?)?\s+by|"
        r"(?:the\s+)?(?:identity|grouping)\s+fields?\s+to)\s+(.{1,80})$",
        cleaned,
        flags=re.IGNORECASE,
    )
    if setting:
        metric = _safe_label(setting.group(1), fallback="", maximum=100)
        group = _safe_label(setting.group(2), fallback="", maximum=80)
        if metric and group:
            return {
                "name": f"{group} {metric}",
                "kind": "grouped_metric",
                "metric_field": metric,
                "group_fields": [group],
                "query_scope": f"{metric} by {group}",
            }
    shorthand = re.search(
        r"\b(?:also\s+)?track\s+(?:the\s+)?([A-Za-z][A-Za-z0-9_-]{1,39})\s+"
        r"([A-Za-z][A-Za-z0-9 _-]{1,79})$",
        cleaned,
        flags=re.IGNORECASE,
    )
    if shorthand:
        group = shorthand.group(1)
        metric = re.sub(r"\s+totals?$", "", shorthand.group(2), flags=re.IGNORECASE)
        return {
            "name": f"{group} {metric}",
            "kind": "grouped_metric",
            "metric_field": metric,
            "group_fields": [group],
            "query_scope": f"{metric} by {group}",
        }
    named = re.search(
        r"\b(?:also\s+)?(?:track|monitor)\s+(?:the\s+)?(.{2,100})$",
        cleaned,
        flags=re.IGNORECASE,
    )
    if named:
        name = _safe_label(named.group(1), fallback="", maximum=120)
        if name:
            return {
                "name": name,
                "kind": "named_filter",
                "metric_field": None,
                "group_fields": [],
                "query_scope": name,
            }
    return None


def answer_grouped_metric_question(
    question: str, datasets: Sequence[GroupedMetricDataset]
) -> dict[str, object] | None:
    """Answer a total/by-group question using exact stored rows and citations."""

    normalized = _normal(question)
    if not normalized:
        return None
    words = set(normalized.split())
    explicit_total_request = bool(
        words.intersection({"total", "totals", "sum", "amount"})
        or "how much" in normalized
        or "how many" in normalized
    )
    lookup_request = bool(re.search(r"\b(?:what|show|give|list|tell)\b", normalized))
    for dataset in reversed(datasets):
        metric_name = _normal(dataset.metric_field)
        group_field = _normal(dataset.group_field)
        matching_groups = [
            group
            for group in dataset.group_totals
            if _normal(group) and _normal(group) in normalized
        ]
        metric_aliases = {metric_name}
        if metric_name.endswith("s") and len(metric_name) > 3:
            metric_aliases.add(metric_name[:-1])
        metric_mentioned = any(
            alias and alias in normalized for alias in metric_aliases
        )
        grouped_request = bool(
            group_field
            and (
                f"by {group_field}" in normalized
                or group_field in normalized
                or f"each {group_field}" in normalized
            )
        )
        scope_mentioned = metric_mentioned or bool(
            group_field and group_field in normalized
        )
        if matching_groups:
            if not scope_mentioned or not (explicit_total_request or lookup_request):
                continue
        elif not (
            metric_mentioned
            and grouped_request
            and (explicit_total_request or lookup_request)
        ):
            continue
        groups = matching_groups or list(dataset.group_totals)
        statements: list[str] = []
        evidence: list[dict[str, object]] = []
        for group in groups:
            total = dataset.group_totals[group]
            row_evidence = [item for item in dataset.evidence if item.group == group]
            unit = f" {dataset.unit}" if dataset.unit else ""
            statements.append(
                f"{group}: {total}{unit} {dataset.metric_field} "
                f"from {len(row_evidence)} contributing row"
                f"{'s' if len(row_evidence) != 1 else ''}"
            )
            evidence.extend(item.model_dump(mode="json") for item in row_evidence)
        origin = "fictional sample" if dataset.fictional else "uploaded local data"
        return {
            "text": (
                f"Deterministic totals from {dataset.dataset_name} ({origin}): "
                + "; ".join(statements)
                + "."
            ),
            "dataset_id": dataset.dataset_id,
            "evidence": evidence,
            "fictional": dataset.fictional,
        }
    return None
