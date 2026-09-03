"""Dark, one-screen local ContextGate command center.

This is the default desktop experience.  The original Streamlit workbench is
kept as an optional advanced lab, while this server exposes a compact local API
and a static, fixed-layout browser interface.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import html
import io
import json
import mimetypes
import os
import re
import tempfile
import threading
import webbrowser
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from PIL import Image, UnidentifiedImageError
from pydantic import ValidationError

from .chat import GroundedChatEngine
from .company_profile import (
    CompanyProfile,
    CompanyProfileError,
    load_company_profile,
    parse_identity_fields,
    save_company_profile,
)
from .decision_engine import evaluate_request
from .email_connectors import EmailConnectorError, EmailOAuthManager
from .grouped_metrics import (
    MAX_GROUPED_METRIC_DATASETS,
    GroupedMetricDataset,
    GroupedMetricError,
    TrackingTopic,
    TrackingTopicStore,
    answer_grouped_metric_question,
    parse_grouped_metric_artifact,
    proposed_tracking_topic,
)
from .intake import MAX_ARTIFACT_BYTES, ArtifactIntakeError, ingest_artifact
from .models import EnforcementDecision
from .operator_learning import (
    DecisionCorrection,
    DecisionCorrectionRetraction,
    GuidanceOrigin,
    OperatorGuidance,
    OperatorLearningStore,
    OperatorLearningStoreError,
)
from .policy_config import PolicyConfigError, get_active_policy
from .report_exports import create_exports, is_export_instruction
from .scenario import iter_scenarios, resolve_scenario
from .source_catalog import SourceCatalog, SourceRecord
from .website_sources import WebsiteSourceError, WebsiteSourceRegistry

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent
STATIC_ROOT = PACKAGE_ROOT / "web" / "static"
RUNTIME_ROOT = PROJECT_ROOT / "runtime"
PROFILE_PATH = RUNTIME_ROOT / "company_profile.json"
MEMORY_PATH = RUNTIME_ROOT / "company_memory.sqlite3"
LOGO_PATH = RUNTIME_ROOT / "company_logo.png"
WEBSITE_SOURCES_PATH = RUNTIME_ROOT / "website_sources.json"
TRACKING_TOPICS_PATH = RUNTIME_ROOT / "tracking_topics.json"
TENANT_ID = os.environ.get("CONTEXTGATE_TENANT_ID", "local-company")

MAX_JSON_BODY = ((MAX_ARTIFACT_BYTES + 2) // 3) * 4 + 128 * 1024
MAX_CHAT_CHARS = 2_000
MAX_HISTORY = 16
MAX_LOGO_BYTES = 1 * 1024 * 1024
ALLOWED_HOSTS: Final = {"127.0.0.1:8501", "localhost:8501"}
WELCOME_CHAT_TEXT: Final = (
    "I'm ready. Ask me what needs attention, why something was blocked, "
    "how a total was calculated, or what patterns I found."
)


class ConsoleError(ValueError):
    """Safe error suitable for a local JSON response."""


def _safe_text(value: object, *, label: str, minimum: int, maximum: int) -> str:
    if not isinstance(value, str):
        raise ConsoleError(f"{label} must be text.")
    cleaned = " ".join(value.split())
    if not minimum <= len(cleaned) <= maximum:
        raise ConsoleError(f"{label} must contain {minimum} to {maximum} characters.")
    if any(ord(character) < 32 for character in cleaned):
        raise ConsoleError(f"{label} contains unsupported characters.")
    return cleaned


def _clean_source_target(value: str) -> str:
    cleaned = re.sub(
        r"\s+(?:again|please|from now on|for now)$", "", value, flags=re.IGNORECASE
    )
    return _safe_text(
        cleaned.strip(" \t\r\n.,!?;:'\""),
        label="Source name",
        minimum=1,
        maximum=120,
    )


def _source_instruction_target(question: str, instruction: str) -> str | None:
    patterns = {
        "hide": (
            r"\b(?:do\s+not|don't|never)\s+show\s+(?:me\s+)?(?:any\s+)?"
            r"(?:data|events?|information|records?)\s+(?:from|by|for)\s+([^?.!]+)"
        ),
        "show": (
            r"\b(?:show|include)\s+(?:me\s+)?(?:the\s+)?(?:data|events?|"
            r"information|records?)\s+(?:from|by|for)\s+([^?.!]+?)"
            r"(?:\s+again)?\s*[?.!]*$"
        ),
        "delete": (
            r"\b(?:delete|erase|remove)\s+(?:all\s+)?(?:the\s+)?(?:data|events?|"
            r"information|records?)\s+(?:from|by|for)\s+([^?.!]+)"
        ),
    }
    match = re.search(patterns[instruction], question.strip(), flags=re.IGNORECASE)
    return _clean_source_target(match.group(1)) if match else None


def _is_affirmative_delete_request(question: str) -> bool:
    """Require an explicit imperative before deleting local source records."""

    return bool(
        re.match(
            r"^\s*(?:please\s+)?(?:delete|erase|remove)\b",
            question,
            flags=re.IGNORECASE,
        )
    )


def _asks_upload_help(normalized: str) -> bool:
    words = set(normalized.split())
    return bool(
        (
            words.intersection({"upload", "accept", "support"})
            and words.intersection(
                {
                    "format",
                    "formats",
                    "type",
                    "types",
                    "file",
                    "files",
                    "screenshot",
                    "screenshots",
                    "picture",
                    "pictures",
                    "photo",
                    "photos",
                    "document",
                    "documents",
                    "pdf",
                    "word",
                    "email",
                    "emails",
                    "eml",
                    "export",
                }
            )
        )
        or re.search(r"\bwhat file types? (?:are supported|can i upload)\b", normalized)
    )


def _json_ready(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


class ConsoleApplication:
    """Thread-safe application state and request-independent operations."""

    def __init__(self) -> None:
        RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.policy = get_active_policy()
        try:
            self.profile = load_company_profile(PROFILE_PATH)
            self.profile_error: str | None = None
        except CompanyProfileError as exc:
            self.profile = CompanyProfile()
            self.profile_error = str(exc)
        self.learning = OperatorLearningStore(MEMORY_PATH)
        self.oauth = EmailOAuthManager()
        try:
            self.websites: WebsiteSourceRegistry | None = WebsiteSourceRegistry(
                WEBSITE_SOURCES_PATH
            )
            self.website_sources_error: str | None = None
        except WebsiteSourceError as exc:
            self.websites = None
            self.website_sources_error = str(exc)
        self.website_scan_status: dict[str, dict[str, object]] = {}
        self.catalog = SourceCatalog()
        self.catalog.configure_visibility(
            hidden_sources=self.profile.hidden_sources,
            deleted_sources=self.profile.deleted_sources,
        )
        try:
            self.tracking_topics: TrackingTopicStore | None = TrackingTopicStore(
                TRACKING_TOPICS_PATH
            )
            self.tracking_topics_error: str | None = None
        except GroupedMetricError as exc:
            self.tracking_topics = None
            self.tracking_topics_error = str(exc)
        self.grouped_metric_datasets: dict[str, GroupedMetricDataset] = {}
        self.pending_tracking_topic: dict[str, object] | None = None
        self.last_added_tracking_topic_id: str | None = None
        self.selected_case_id = "R1"
        self.last_upload: dict[str, Any] | None = None
        self.last_exports: list[dict[str, object]] = []
        self.chat_history: list[dict[str, Any]] = [
            {
                "role": "assistant",
                "text": WELCOME_CHAT_TEXT,
                "citations": [],
                "saved": False,
            }
        ]

    def close(self) -> None:
        self.learning.close()

    def _evaluated_cases(self) -> list[dict[str, Any]]:
        cases: list[dict[str, Any]] = []
        for scenario in iter_scenarios():
            events, request = scenario.load()
            decision = evaluate_request(
                events,
                request,
                run_id=(
                    f"console-{scenario.case_id.casefold()}-"
                    f"{self.policy.policy_fingerprint[:12]}"
                ),
                policy=self.policy,
            )
            try:
                correction = self.learning.latest_active_decision_correction(
                    TENANT_ID,
                    case_id=scenario.case_id,
                    request_fingerprint=decision.request_digest,
                    evidence_fingerprint=decision.evidence_digest,
                    policy_fingerprint=decision.policy_fingerprint,
                )
            except OperatorLearningStoreError:
                correction = None
            effective = (
                correction.corrected_outcome if correction else decision.decision
            )
            cases.append(
                {
                    "case_id": scenario.case_id,
                    "data_origin": "fictional_demo",
                    "fictional": True,
                    "name": scenario.name,
                    "title": scenario.title,
                    "description": scenario.description,
                    "outcome": effective.value,
                    "original_outcome": decision.decision.value,
                    "classification": decision.classification.value,
                    "risk": decision.risk.value,
                    "summary": decision.explanation,
                    "source": ", ".join(
                        dict.fromkeys(
                            event.source_name or "Unknown source" for event in events
                        )
                    ),
                    "evidence_count": len(events),
                    "policy_fingerprint": decision.policy_fingerprint,
                    "request_fingerprint": decision.request_digest,
                    "rule_ids": decision.deterministic_rule_ids,
                    "requires_approval": decision.requires_human_approval,
                    "corrected": correction is not None,
                    "correction_id": correction.correction_id if correction else None,
                    "correction_rationale": correction.rationale
                    if correction
                    else None,
                    "active_correction": (
                        {
                            "correction_id": correction.correction_id,
                            "original_outcome": correction.original_outcome.value,
                            "corrected_outcome": correction.corrected_outcome.value,
                            "reviewer": correction.reviewer,
                            "rationale": correction.rationale,
                            "created_at": correction.created_at.isoformat(),
                            "active": True,
                        }
                        if correction
                        else None
                    ),
                    "request": {
                        "action_type": request.action_type,
                        "requested_value": request.requested_value,
                        "field_name": request.field_name,
                    },
                    "decision": {
                        "decision_id": decision.decision_id,
                        "explanation": decision.explanation,
                        "authoritative_value": decision.authoritative_value,
                        "competing_values": decision.competing_values,
                        "evidence_event_ids": decision.evidence_event_ids,
                        "rule_ids": decision.deterministic_rule_ids,
                        "policy_version": decision.policy_version,
                        "policy_fingerprint": decision.policy_fingerprint,
                        "request_fingerprint": decision.request_digest,
                        "evidence_fingerprint": decision.evidence_digest,
                        "action_executed": False,
                    },
                    "evidence": [
                        {
                            "event_id": event.event_id,
                            "source_name": event.source_name or "Unknown source",
                            "source_type": event.source_type or "Unknown",
                            "value": event.field_value,
                            "trust_score": event.trust_score,
                            "status": event.status.value,
                            "observed_at": (
                                event.observed_at.isoformat()
                                if event.observed_at is not None
                                else None
                            ),
                            "reference": event.evidence_reference or event.evidence_uri,
                        }
                        for event in events
                    ],
                }
            )
        return cases

    def _logo_data_url(self) -> str | None:
        try:
            if (
                not LOGO_PATH.is_file()
                or LOGO_PATH.is_symlink()
                or LOGO_PATH.stat().st_size > MAX_LOGO_BYTES
            ):
                return None
            encoded = base64.b64encode(LOGO_PATH.read_bytes()).decode("ascii")
            return f"data:image/png;base64,{encoded}"
        except OSError:
            return None

    def state(self) -> dict[str, Any]:
        with self._lock:
            cases = self._evaluated_cases()
            counts = CounterLike(case["outcome"] for case in cases)
            selected = next(
                (case for case in cases if case["case_id"] == self.selected_case_id),
                cases[0],
            )
            try:
                guidance_count = len(
                    self.learning.list_active_guidance(TENANT_ID, limit=100)
                )
            except OperatorLearningStoreError:
                guidance_count = 0
            connectors = self.oauth.status()
            website_sources: list[dict[str, object]] = []
            if self.websites is not None:
                for source in self.websites.list_sources():
                    scan_status = self.website_scan_status.get(source.source_id, {})
                    website_sources.append(
                        {
                            **source.model_dump(mode="json"),
                            "status": scan_status.get("status", "Ready to scan"),
                            "last_scan_at": scan_status.get("last_scan_at"),
                            "records_count": scan_status.get("records_count", 0),
                            "events_found": scan_status.get("events_found", 0),
                            "last_error": scan_status.get("last_error"),
                        }
                    )
            account_count = sum(
                len(provider["accounts"]) for provider in connectors.values()
            )
            catalog_summary = self.catalog.summary()
            calendar_events = self.catalog.calendar_events()
            scheduled_calendar_events = sum(
                bool(item.get("date_iso")) for item in calendar_events
            )
            if not calendar_events:
                calendar_mode = "empty"
            elif all(bool(item.get("fictional")) for item in calendar_events):
                calendar_mode = "fictional_demo"
            elif any(bool(item.get("fictional")) for item in calendar_events):
                calendar_mode = "mixed_sources"
            else:
                calendar_mode = "scanned_or_uploaded"
            source_counts = catalog_summary.get("source_counts", {})
            location_counts = catalog_summary.get("location_counts", {})
            logo_data_url = self._logo_data_url()
            demo_label = (
                "Fictional demo inbox"
                if catalog_summary.get("fictional")
                else "Currently scanned sources"
            )
            patterns = [
                {
                    "label": "Distinct events",
                    "count": catalog_summary.get("distinct_events", 0),
                    "description": (
                        f"{catalog_summary.get('messages_scanned', 0)} messages "
                        f"scanned · {catalog_summary.get('duplicate_updates', 0)} "
                        f"duplicate/update excluded · {demo_label}"
                    ),
                },
                {
                    "label": "Eventbrite events",
                    "count": (
                        source_counts.get("Eventbrite", 0)
                        if isinstance(source_counts, dict)
                        else 0
                    ),
                    "description": "Distinct events after update-message deduplication.",
                },
                {
                    "label": "New York City events",
                    "count": (
                        location_counts.get("New York City", 0)
                        if isinstance(location_counts, dict)
                        else 0
                    ),
                    "description": (
                        "Includes Manhattan, Brooklyn, Queens, the Bronx, and "
                        "Staten Island aliases."
                    ),
                },
            ]
            if catalog_summary.get("fictional"):
                patterns.extend(
                    [
                        {
                            "label": "Address recurrence",
                            "count": "8 vs 3",
                            "description": (
                                "Fictional memory: 8 events at 76 New Avenue versus "
                                "3 at 35 Main Street; a suite change is held for review."
                            ),
                        },
                        {
                            "label": "Crowd-size update",
                            "count": 113,
                            "description": (
                                "Fictional trace: 35 confirmed + ‘78 more’ = 113; "
                                "total wording would replace instead of add."
                            ),
                        },
                    ]
                )
            patterns.extend(
                [
                    {
                        "label": "Hidden source rules",
                        "count": len(self.profile.hidden_sources),
                        "description": (
                            ", ".join(self.profile.hidden_sources)
                            if self.profile.hidden_sources
                            else "No sources are hidden."
                        ),
                    },
                    {
                        "label": "Deleted source exclusions",
                        "count": len(self.profile.deleted_sources),
                        "description": (
                            ", ".join(self.profile.deleted_sources)
                            if self.profile.deleted_sources
                            else "No source deletion exclusions are active."
                        ),
                    },
                ]
            )
            tracking = (
                self.tracking_topics.snapshot()
                if self.tracking_topics is not None
                else {
                    "active_topic_id": None,
                    "active_topic": None,
                    "topics": [],
                    "switching_note": (
                        "Tracking topics are unavailable until the local store is repaired."
                    ),
                }
            )
            return {
                "service": "ContextGate",
                "mode": "LOCAL-ONLY",
                "company": {
                    **self.profile.model_dump(mode="json"),
                    "identity_summary": self.profile.identity_summary,
                    "profile_error": self.profile_error,
                    "has_company_logo": logo_data_url is not None,
                    "company_logo_data_url": logo_data_url,
                },
                "policy": {
                    "version": self.policy.policy_version,
                    "fingerprint": self.policy.policy_fingerprint,
                },
                "totals": {
                    "total": len(cases),
                    "allow": counts.get("ALLOW"),
                    "review": counts.get("REVIEW"),
                    "block": counts.get("BLOCK"),
                    "corrections": sum(bool(case["corrected"]) for case in cases),
                },
                "cases": cases,
                "selected": selected,
                "selected_case_id": selected["case_id"],
                "source_status": {
                    "mailbox": (
                        f"{account_count} connected"
                        if account_count
                        else "Not connected"
                    ),
                    "accounts": account_count,
                    "upload": "Ready",
                    "website": f"{len(website_sources)} configured",
                    "demo": (
                        "Fictional data active"
                        if catalog_summary.get("fictional")
                        else "Scanned source data active"
                    ),
                    "last_upload": self.last_upload,
                },
                "connectors": connectors,
                "website_sources": website_sources,
                "website_sources_error": self.website_sources_error,
                "chat_history": self.chat_history[-MAX_HISTORY:],
                "patterns": patterns,
                "source_summary": catalog_summary,
                "calendar": {
                    "events": calendar_events,
                    "scheduled_count": scheduled_calendar_events,
                    "unscheduled_count": len(calendar_events)
                    - scheduled_calendar_events,
                    "data_mode": calendar_mode,
                },
                "tracking": {
                    **tracking,
                    "error": self.tracking_topics_error,
                    "pending_confirmation": self.pending_tracking_topic is not None,
                },
                "grouped_metrics": [
                    item.model_dump(mode="json")
                    for item in self.grouped_metric_datasets.values()
                ],
                "recent_exports": self.last_exports[-12:],
                "source_preferences": {
                    "hidden_sources": self.profile.hidden_sources,
                    "deleted_sources": self.profile.deleted_sources,
                },
                "guidance_count": guidance_count,
                "no_external_action": True,
            }

    def select_case(self, payload: dict[str, Any]) -> dict[str, Any]:
        case_id = _safe_text(
            payload.get("case_id"), label="Case ID", minimum=2, maximum=8
        ).upper()
        valid_ids = {scenario.case_id for scenario in iter_scenarios()}
        if case_id not in valid_ids:
            raise ConsoleError("That case is not available.")
        with self._lock:
            self.selected_case_id = case_id
        return self.state()

    def update_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        identity_value = payload.get("identity_fields", "")
        if isinstance(identity_value, list):
            identity_fields = identity_value
        else:
            identity_fields = parse_identity_fields(str(identity_value))
        source_mode = payload.get("source_mode", self.profile.source_mode)
        risk_posture = payload.get("risk_posture", self.profile.risk_posture)
        if risk_posture == "validated_custom_policy":
            risk_posture = "custom_policy"
        profile = CompanyProfile(
            company_name=payload.get("company_name", self.profile.company_name),
            operator_name=payload.get("operator_name", self.profile.operator_name),
            important_detail=payload.get(
                "important_detail", self.profile.important_detail
            ),
            identity_fields=identity_fields,
            risk_posture=risk_posture,
            source_mode=source_mode,
            voice_enabled=payload.get("voice_enabled", self.profile.voice_enabled),
            mail_scan_limit=payload.get(
                "mail_scan_limit", self.profile.mail_scan_limit
            ),
            auto_monitor_enabled=payload.get(
                "auto_monitor_enabled", self.profile.auto_monitor_enabled
            ),
            auto_monitor_minutes=payload.get(
                "auto_monitor_minutes", self.profile.auto_monitor_minutes
            ),
            document_company_header=payload.get(
                "document_company_header", self.profile.document_company_header
            ),
            document_footer=payload.get(
                "document_footer", self.profile.document_footer
            ),
            company_website=payload.get(
                "company_website", self.profile.company_website
            ),
            hidden_sources=payload.get("hidden_sources", self.profile.hidden_sources),
            deleted_sources=payload.get(
                "deleted_sources", self.profile.deleted_sources
            ),
        )
        save_company_profile(PROFILE_PATH, profile)
        with self._lock:
            self.profile = profile
            self.profile_error = None
            self.catalog.configure_visibility(
                hidden_sources=profile.hidden_sources,
                deleted_sources=profile.deleted_sources,
            )
        return self.state()

    def _save_source_preferences(
        self, *, hidden_sources: list[str], deleted_sources: list[str]
    ) -> None:
        payload = self.profile.model_dump(mode="json")
        payload["hidden_sources"] = hidden_sources
        payload["deleted_sources"] = deleted_sources
        profile = CompanyProfile.model_validate(payload)
        save_company_profile(PROFILE_PATH, profile)
        with self._lock:
            self.profile = profile
            self.profile_error = None
            self.catalog.configure_visibility(
                hidden_sources=profile.hidden_sources,
                deleted_sources=profile.deleted_sources,
            )

    def save_company_logo(self, payload: dict[str, Any]) -> dict[str, Any]:
        encoded = payload.get("data_base64")
        content_type = payload.get("content_type")
        if content_type not in {"image/png", "image/jpeg"}:
            raise ConsoleError("Choose a PNG or JPEG company logo.")
        if not isinstance(encoded, str) or len(encoded) > MAX_LOGO_BYTES * 2:
            raise ConsoleError("The company logo is too large.")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise ConsoleError("The company logo could not be decoded.") from None
        if not raw or len(raw) > MAX_LOGO_BYTES:
            raise ConsoleError("The company logo must be no larger than 1 MB.")
        try:
            with Image.open(io.BytesIO(raw)) as source:
                if source.width * source.height > 16_000_000:
                    raise ConsoleError("The company logo dimensions are too large.")
                source.load()
                normalized = source.convert("RGBA")
                normalized.thumbnail((512, 512), Image.Resampling.LANCZOS)
                buffer = io.BytesIO()
                normalized.save(buffer, format="PNG", optimize=True)
                content = buffer.getvalue()
        except ConsoleError:
            raise
        except (OSError, ValueError, UnidentifiedImageError):
            raise ConsoleError(
                "The selected file is not a valid company logo."
            ) from None
        if len(content) > MAX_LOGO_BYTES:
            raise ConsoleError("The normalized company logo is too large.")
        if LOGO_PATH.exists() and LOGO_PATH.is_symlink():
            raise ConsoleError("The company logo path is not safe to replace.")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".company-logo-", suffix=".tmp", dir=RUNTIME_ROOT
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, LOGO_PATH)
        except OSError:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
            raise ConsoleError("The company logo could not be saved safely.") from None
        return self.state()

    def remove_company_logo(self) -> dict[str, Any]:
        try:
            if LOGO_PATH.exists():
                LOGO_PATH.unlink()
        except OSError:
            raise ConsoleError(
                "The company logo could not be removed safely."
            ) from None
        return self.state()

    def _website_registry(self) -> WebsiteSourceRegistry:
        if self.websites is None:
            raise ConsoleError(
                self.website_sources_error
                or "Website sources are unavailable in this workspace."
            )
        return self.websites

    def add_website_source(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = payload.get("url")
        extraction_goal = payload.get("extraction_goal")
        label = payload.get("label", "")
        if not isinstance(url, str) or not isinstance(extraction_goal, str):
            raise ConsoleError("Enter a public website URL and extraction goal.")
        if not isinstance(label, str):
            raise ConsoleError("Website label must be text.")
        source = self._website_registry().add_source(
            url,
            extraction_goal,
            label=label,
        )
        with self._lock:
            self.website_scan_status.setdefault(
                source.source_id,
                {
                    "status": "Ready to scan",
                    "records_count": 0,
                    "events_found": 0,
                    "last_error": None,
                },
            )
        return self.state()

    def remove_website_source(self, payload: dict[str, Any]) -> dict[str, Any]:
        source_id = _safe_text(
            payload.get("source_id"),
            label="Website source ID",
            minimum=32,
            maximum=64,
        )
        if not self._website_registry().remove_source(source_id):
            raise ConsoleError("Website source was not found.")
        with self._lock:
            self.website_scan_status.pop(source_id, None)
        return self.state()

    @staticmethod
    def _website_location(value: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
        if any(
            term in normalized
            for term in (
                "new york city",
                "new york ny",
                "nyc",
                "manhattan",
                "brooklyn",
                "queens",
                "bronx",
                "staten island",
            )
        ):
            return "New York City"
        return value[:200] or "Unknown"

    def scan_website_source(self, payload: dict[str, Any]) -> dict[str, Any]:
        source_id = _safe_text(
            payload.get("source_id"),
            label="Website source ID",
            minimum=32,
            maximum=64,
        )
        try:
            result = self._website_registry().scan_source(source_id)
        except WebsiteSourceError as exc:
            with self._lock:
                self.website_scan_status[source_id] = {
                    "status": "Scan failed",
                    "records_count": 0,
                    "events_found": 0,
                    "last_error": str(exc),
                }
            raise
        catalog_records: list[SourceRecord] = []
        for item in result.records:
            if item.kind != "event":
                continue
            fields = item.fields
            start_date = fields.get("startDate")
            event_date = start_date
            event_time = None
            if start_date and "T" in start_date:
                event_date, event_time = start_date.split("T", 1)
            location_text = " ".join(
                part for part in (fields.get("location"), fields.get("address")) if part
            )
            source_name = result.source.label
            if "eventbrite" in f"{source_name} {result.source.url}".casefold():
                source_name = "Eventbrite"
            event_key = re.sub(
                r"[^a-z0-9]+",
                " ",
                f"{item.title} {event_date or ''}".casefold(),
            ).strip()[:300]
            catalog_records.append(
                SourceRecord(
                    record_id=f"website-{item.record_id}",
                    event_key=event_key or item.record_id,
                    title=item.title,
                    source_name=source_name,
                    location=self._website_location(location_text),
                    organization=fields.get("organizer") or result.source.label,
                    event_date=event_date,
                    event_time=event_time,
                    address=fields.get("address") or fields.get("location"),
                    evidence_reference=item.evidence_reference,
                    fictional=False,
                )
            )
        imported = self.catalog.add_external_records(catalog_records)
        with self._lock:
            self.website_scan_status[source_id] = {
                "status": "Scan complete",
                "last_scan_at": result.scanned_at,
                "records_count": len(result.records),
                "events_found": len(result.events),
                "last_error": None,
            }
        return {
            "imported": imported,
            "records_found": len(result.records),
            "events_found": len(result.events),
            "state": self.state(),
        }

    def reset_demo(self) -> dict[str, Any]:
        with self._lock:
            self.catalog.reset_fictional_demo()
            self.selected_case_id = "R1"
            self.chat_history = [
                {
                    "role": "assistant",
                    "text": WELCOME_CHAT_TEXT,
                    "citations": [],
                    "saved": False,
                }
            ]
            self.last_upload = None
        return self.state()

    def _tracking_store(self) -> TrackingTopicStore:
        if self.tracking_topics is None:
            raise ConsoleError(
                self.tracking_topics_error
                or "The local tracking-topic store is unavailable."
            )
        return self.tracking_topics

    def _apply_grouped_topic_profile(self, topic: TrackingTopic) -> None:
        if (
            topic.kind != "grouped_metric"
            or not topic.metric_field
            or not topic.group_fields
        ):
            return
        profile_payload = self.profile.model_dump(mode="json")
        profile_payload["important_detail"] = topic.metric_field
        profile_payload["identity_fields"] = topic.group_fields
        profile = CompanyProfile.model_validate(profile_payload)
        save_company_profile(PROFILE_PATH, profile)
        self.profile = profile
        self.profile_error = None

    def _commit_pending_tracking_topic(self) -> TrackingTopic:
        proposal = self.pending_tracking_topic
        if proposal is None:
            raise ConsoleError("There is no pending tracking configuration to confirm.")
        kind = proposal.get("kind")
        if kind not in {"grouped_metric", "named_filter"}:
            raise ConsoleError("The pending tracking configuration is invalid.")
        groups = proposal.get("group_fields")
        if not isinstance(groups, list) or not all(
            isinstance(item, str) for item in groups
        ):
            raise ConsoleError("The pending tracking grouping is invalid.")
        metric = proposal.get("metric_field")
        if metric is not None and not isinstance(metric, str):
            raise ConsoleError("The pending tracking metric is invalid.")
        with self._lock:
            topic = self._tracking_store().add_topic(
                name=str(proposal.get("name", "")),
                kind=kind,
                metric_field=metric,
                group_fields=groups,
                query_scope=str(proposal.get("query_scope", "")),
            )
            self._apply_grouped_topic_profile(topic)
            self.pending_tracking_topic = None
            self.last_added_tracking_topic_id = topic.topic_id
        return topic

    def _tracking_summary(self) -> str:
        store = self._tracking_store()
        topics = store.topics()
        if not topics:
            return (
                "No named tracking topics are configured. Say, for example, "
                "‘also track office sales’ or ‘track revenue by region’; I will "
                "show a proposal and wait for confirmation."
            )
        active = store.active_topic()
        descriptions = []
        for topic in topics:
            if topic.kind == "grouped_metric":
                scope = f"{topic.metric_field} by {', '.join(topic.group_fields)}"
            else:
                scope = f"named filter: {topic.query_scope}"
            marker = " — active" if active and active.topic_id == topic.topic_id else ""
            descriptions.append(f"{topic.name} ({scope}{marker})")
        return (
            f"Tracking {len(topics)} independent topic"
            f"{'s' if len(topics) != 1 else ''}: "
            + "; ".join(descriptions)
            + ". Switching the active chat/report context does not remove other "
            "topics or stop source collection."
        )

    def _metric_datasets_for_answer(self) -> list[GroupedMetricDataset]:
        datasets = list(self.grouped_metric_datasets.values())
        active = self.tracking_topics.active_topic() if self.tracking_topics else None
        if active is None:
            return datasets
        return sorted(datasets, key=lambda item: item.topic_id == active.topic_id)

    def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        question = _safe_text(
            payload.get("message"),
            label="Message",
            minimum=1,
            maximum=MAX_CHAT_CHARS,
        )
        save_guidance = payload.get("save_guidance", False)
        if not isinstance(save_guidance, bool):
            raise ConsoleError("Save-guidance choice is invalid.")

        normalized = re.sub(r"[^a-z0-9]+", " ", question.casefold()).strip()
        calendar_answer = self.catalog.answer_calendar_question(question)
        delete_mentioned = bool(
            re.search(
                r"\b(?:delete|erase)\b|"
                r"\bremove\s+(?:(?:all|the)\s+)*(?:data|events?|information|"
                r"records?|it)\b",
                question,
                flags=re.IGNORECASE,
            )
        )
        affirmative_delete = _is_affirmative_delete_request(question)
        delete_target = (
            _source_instruction_target(question, "delete")
            if affirmative_delete
            else None
        )
        if (
            delete_target is None
            and affirmative_delete
            and re.search(
                r"\b(?:delete|erase|remove)\s+it\b", question, flags=re.IGNORECASE
            )
        ):
            delete_target = (
                self.profile.hidden_sources[-1] if self.profile.hidden_sources else None
            )
        hide_target = _source_instruction_target(question, "hide")
        show_candidate = (
            None
            if hide_target is not None
            else _source_instruction_target(question, "show")
        )
        show_target = (
            show_candidate
            if show_candidate is not None
            and (
                re.search(r"\bagain\b", question, flags=re.IGNORECASE)
                or any(
                    item.casefold() == show_candidate.casefold()
                    for item in self.profile.hidden_sources
                )
            )
            else None
        )
        metric_answer = answer_grouped_metric_question(
            question, self._metric_datasets_for_answer()
        )
        count_words = set(normalized.split())
        source_count_targeted = bool(
            {"eventbrite", "eventbright", "nyc", "manhattan", "brooklyn", "queens"}
            & count_words
            or "new york city" in normalized
            or "new york ny" in normalized
        )
        mixes_decision_and_source_counts = bool(
            source_count_targeted
            and {
                "red",
                "amber",
                "yellow",
                "green",
                "allow",
                "allowed",
                "review",
                "blocked",
            }
            & count_words
            and any(
                phrase in normalized
                for phrase in ("how many", "count", "number of", "total")
            )
        )
        count_answer = (
            {
                "text": (
                    "That question combines decision-queue outcomes with source-catalog "
                    "events, but those records are not joined in this local workspace. "
                    "Ask for the red, amber, or green decision cases and the Eventbrite "
                    "or NYC event count separately; I will not invent an intersection."
                ),
                "evidence": [],
            }
            if mixes_decision_and_source_counts
            else self.catalog.answer_count_question(question)
        )
        tracking_answer = self.catalog.answer_tracking_question(question)
        tracking_proposal = proposed_tracking_topic(question)
        correction_case_match = re.search(r"\b([abr]\d+)\b", normalized)
        correction_case_id = (
            correction_case_match.group(1).upper() if correction_case_match else None
        )
        retract_correction = bool(
            correction_case_id
            and re.search(
                r"\b(?:undo|retract|remove|cancel)\s+(?:the\s+)?correction\b",
                normalized,
            )
        )
        asks_about_correction = bool(
            "correction" in normalized
            or re.search(r"\bcorrect(?:ed|ing)?\b", normalized)
        )
        confirm_tracking = normalized in {
            "confirm tracking configuration",
            "confirm tracking topic",
            "confirm tracker",
            "yes confirm tracking",
        } or bool(
            self.pending_tracking_topic is not None
            and re.fullmatch(
                r"(?:yes )?(?:please )?confirm(?: (?:it|that|this|tracker|tracking))?",
                normalized,
            )
        )
        cancel_tracking = normalized in {
            "cancel tracking configuration",
            "cancel tracking topic",
            "cancel tracker",
        } or bool(
            self.pending_tracking_topic is not None
            and (
                re.fullmatch(
                    r"(?:no )?(?:please )?(?:cancel|discard)"
                    r"(?: (?:it|that|this|tracker|tracking))?",
                    normalized,
                )
                or normalized in {"never mind", "nevermind", "dont save it"}
            )
        )
        undo_tracking = normalized in {
            "undo last tracking change",
            "undo last tracking configuration",
            "undo tracking configuration",
        }
        list_tracking = bool(
            re.fullmatch(
                r"(?:what|which) (?:topics )?(?:are|am i) (?:you |i )?tracking",
                normalized,
            )
            or normalized
            in {
                "list tracking topics",
                "show tracking topics",
                "what are you monitoring",
                "what am i monitoring",
                "list monitoring topics",
            }
        )
        previous_tracking = normalized in {
            "go back",
            "go back to previous topic",
            "previous tracking topic",
        }
        switch_candidate = (
            None
            if any(
                item is not None
                for item in (
                    show_candidate,
                    calendar_answer,
                    metric_answer,
                    count_answer,
                    tracking_answer,
                )
            )
            else re.fullmatch(
                r"(?:show(?: me)?|switch to|go back to) "
                r"(?:the )?(.+?)(?: topic| tracker)?",
                normalized,
            )
        )
        switch_match = switch_candidate
        if switch_candidate is not None:
            switch_target = switch_candidate.group(1)
            normalized_target = re.sub(
                r"[^a-z0-9]+", " ", switch_target.casefold()
            ).strip()
            topic_match = any(
                normalized_target
                and normalized_target
                in re.sub(
                    r"[^a-z0-9]+",
                    " ",
                    f"{topic.name} {topic.query_scope}".casefold(),
                ).strip()
                for topic in self._tracking_store().topics()
            )
            explicit_switch = normalized.startswith(("switch to ", "go back to "))
            names_topic = normalized.endswith((" topic", " tracker"))
            if not (topic_match or explicit_switch or names_topic):
                switch_match = None
        export_instruction = is_export_instruction(question)
        upload_help = _asks_upload_help(normalized)
        citations: list[str] = []
        saved = False
        if delete_mentioned and not affirmative_delete:
            answer_text = (
                "No data was deleted. For safety, local source deletion runs only "
                "from an affirmative command that starts with ‘delete’, ‘erase’, or "
                "‘remove’, such as ‘delete data from Posh.’ Questions and negated "
                "phrases never delete records."
            )
        elif delete_target is not None:
            removed = self.catalog.matching_count(delete_target)
            deleted = [
                *(
                    item
                    for item in self.profile.deleted_sources
                    if item.casefold() != delete_target.casefold()
                ),
                delete_target,
            ]
            hidden = [
                item
                for item in self.profile.hidden_sources
                if item.casefold() != delete_target.casefold()
            ]
            self._save_source_preferences(
                hidden_sources=hidden, deleted_sources=deleted
            )
            answer_text = (
                f"Deleted {removed} stored record{'s' if removed != 1 else ''} "
                f"matching {delete_target}. I retained only a content-free deletion "
                "exclusion so later scans do not silently import that source again. "
                "The deleted record content is no longer available in this workspace."
            )
            citations = [f"profile:deleted-source:{delete_target.casefold()}"]
            saved = True
        elif show_target is not None:
            if any(
                item.casefold() == show_target.casefold()
                for item in self.profile.deleted_sources
            ):
                answer_text = (
                    f"I cannot show data from {show_target}: it was deleted and is "
                    "excluded from later scans. Remove the deletion exclusion in an "
                    "authorized data-retention workflow before importing it again."
                )
                citations = [f"profile:deleted-source:{show_target.casefold()}"]
            else:
                hidden = [
                    item
                    for item in self.profile.hidden_sources
                    if item.casefold() != show_target.casefold()
                ]
                self._save_source_preferences(
                    hidden_sources=hidden,
                    deleted_sources=self.profile.deleted_sources,
                )
                visible = self.catalog.matching_count(show_target)
                answer_text = (
                    f"The hide rule for {show_target} is removed. "
                    f"{visible} matching stored record"
                    f"{'s are' if visible != 1 else ' is'} visible again."
                )
                citations = [f"profile:visible-source:{show_target.casefold()}"]
                saved = True
        elif hide_target is not None:
            matching = self.catalog.matching_count(hide_target)
            hidden = [
                *(
                    item
                    for item in self.profile.hidden_sources
                    if item.casefold() != hide_target.casefold()
                ),
                hide_target,
            ]
            self._save_source_preferences(
                hidden_sources=hidden,
                deleted_sources=self.profile.deleted_sources,
            )
            answer_text = (
                f"Saved a hide rule for {hide_target}. {matching} matching stored "
                f"record{'s are' if matching != 1 else ' is'} now excluded from "
                "counts, patterns, and answers, but the underlying data was not "
                "deleted. Say ‘show me data from "
                f"{hide_target} again’ to reverse the hide rule, or ‘delete data "
                f"from {hide_target}’ to remove it."
            )
            citations = [f"profile:hidden-source:{hide_target.casefold()}"]
            saved = True
        elif retract_correction and correction_case_id is not None:
            case = next(
                (
                    item
                    for item in self._evaluated_cases()
                    if item["case_id"] == correction_case_id
                ),
                None,
            )
            active = case.get("active_correction") if case else None
            if not isinstance(active, dict):
                answer_text = (
                    f"{correction_case_id} has no active human correction to retract. "
                    "Nothing was changed."
                )
            else:
                self.retract(
                    {
                        "case_id": correction_case_id,
                        "reason": "Operator explicitly requested retraction in chat.",
                    }
                )
                answer_text = (
                    f"Retracted the active human correction for {correction_case_id}. "
                    f"Its effective outcome returned from {active['corrected_outcome']} "
                    f"to the original {active['original_outcome']}. The original decision, "
                    "correction, and retraction remain in the audit history; no external "
                    "action was executed."
                )
                citations = [
                    f"case:{correction_case_id}",
                    f"correction:{active['correction_id']}",
                ]
                saved = True
        elif asks_about_correction:
            case = next(
                (
                    item
                    for item in self._evaluated_cases()
                    if item["case_id"] == correction_case_id
                ),
                None,
            )
            if case is None and correction_case_id is not None:
                answer_text = (
                    f"{correction_case_id} is not an available case. No decision or "
                    "correction was changed."
                )
            elif case is not None and isinstance(case.get("active_correction"), dict):
                active = case["active_correction"]
                answer_text = (
                    f"{correction_case_id} keeps its original deterministic "
                    f"{active['original_outcome']} receipt, and an active human correction "
                    f"sets the effective outcome to {active['corrected_outcome']}. "
                    f"Recorded rationale: {active['rationale']} To reverse it, say "
                    f"‘undo the correction for {correction_case_id}.’ Retraction preserves "
                    "the history and executes no external action."
                )
                citations = [
                    f"case:{correction_case_id}",
                    f"correction:{active['correction_id']}",
                ]
            elif case is not None:
                answer_text = (
                    f"{correction_case_id} currently has no active human correction; its "
                    f"effective and original outcomes are both {case['original_outcome']}. "
                    "Open Case details, choose a different corrected outcome, enter an "
                    "attributable rationale, and confirm it. Chat did not change the "
                    "decision."
                )
                citations = [f"case:{correction_case_id}"]
            else:
                answer_text = (
                    "Open a case in Case details, choose a different corrected outcome, "
                    "enter an attributable rationale, and confirm it. The correction is "
                    "stored separately from the original deterministic receipt and can be "
                    "retracted later. Chat did not change any decision."
                )
        elif confirm_tracking:
            if self.pending_tracking_topic is None:
                answer_text = (
                    "There is no pending tracking configuration to confirm. "
                    "No stored topic was changed."
                )
            else:
                topic = self._commit_pending_tracking_topic()
                if topic.kind == "grouped_metric":
                    answer_text = (
                        f"Confirmed and saved {topic.name}: {topic.metric_field} grouped "
                        f"by {', '.join(topic.group_fields)}. It is now the active "
                        "chat/report context, and matching CSV or JSON uploads can be "
                        "summed with row-level evidence. Say ‘undo last tracking change’ "
                        "to remove this confirmed definition."
                    )
                else:
                    answer_text = (
                        f"Confirmed and saved the named tracking topic {topic.name}. "
                        "It is now the active chat/report context. Switching topics does "
                        "not remove other definitions or stop source collection. Say "
                        "‘undo last tracking change’ to remove this confirmed definition."
                    )
                citations = [f"tracking-topic:{topic.topic_id}"]
                saved = True
        elif cancel_tracking:
            if self.pending_tracking_topic is None:
                answer_text = "There is no pending tracking configuration to cancel."
            else:
                self.pending_tracking_topic = None
                answer_text = (
                    "Canceled the pending tracking configuration. Nothing was saved."
                )
        elif undo_tracking:
            if self.last_added_tracking_topic_id is None:
                answer_text = (
                    "There is no confirmed tracking addition from this session to undo. "
                    "No stored topic was changed."
                )
            else:
                with self._lock:
                    removed = self._tracking_store().remove(
                        self.last_added_tracking_topic_id
                    )
                    self.last_added_tracking_topic_id = None
                    active = self._tracking_store().active_topic()
                    if active is not None:
                        self._apply_grouped_topic_profile(active)
                answer_text = (
                    f"Removed the last confirmed tracking definition, {removed.name}. "
                    "Other topics and collected source data were not deleted."
                )
                citations = [f"tracking-topic-removed:{removed.topic_id}"]
                saved = True
        elif list_tracking:
            answer_text = self._tracking_summary()
            citations = [
                f"tracking-topic:{item.topic_id}"
                for item in self._tracking_store().topics()
            ]
        elif previous_tracking or switch_match is not None:
            try:
                with self._lock:
                    topic = self._tracking_store().activate(
                        switch_match.group(1) if switch_match is not None else None,
                        previous=previous_tracking,
                    )
                    self._apply_grouped_topic_profile(topic)
            except GroupedMetricError:
                answer_text = (
                    "That tracking topic is not available. Say ‘what are you "
                    "tracking?’ to list the saved topics; no topic was changed."
                )
            else:
                answer_text = (
                    f"Switched the active chat/report context to {topic.name}. All "
                    "tracking definitions remain stored, and switching does not stop "
                    "source collection. Ask a topic-specific question to query its evidence."
                )
                citations = [f"tracking-topic:{topic.topic_id}"]
                saved = True
        elif tracking_proposal is not None:
            self.pending_tracking_topic = tracking_proposal
            if tracking_proposal["kind"] == "grouped_metric":
                answer_text = (
                    f"Proposed tracking topic: {tracking_proposal['name']}; metric "
                    f"{tracking_proposal['metric_field']}; group by "
                    f"{', '.join(tracking_proposal['group_fields'])}. This would "
                    "update the displayed important detail and identity fields, and "
                    "add—not replace—an independent report context. Reply ‘confirm "
                    "tracking configuration’ to save it, or ‘cancel tracking "
                    "configuration’."
                )
            else:
                answer_text = (
                    f"Proposed named tracking topic: {tracking_proposal['name']}. "
                    "This stores a report/chat focus definition; it does not claim "
                    "continuous monitoring. Reply ‘confirm tracking configuration’ "
                    "to save it, or ‘cancel tracking configuration’."
                )
        elif export_instruction:
            artifacts = create_exports(self.state(), question)
            exported = [artifact.model_dump() for artifact in artifacts]
            with self._lock:
                self.last_exports.extend(exported)
                self.last_exports = self.last_exports[-12:]
            folder = str(Path(artifacts[0].path).parent)
            file_lines = "\n".join(
                f"- {artifact.kind.upper()}: {artifact.filename}"
                for artifact in artifacts
            )
            answer_text = (
                f"Saved {len(artifacts)} export file"
                f"{'s' if len(artifacts) != 1 else ''} in {folder}:\n"
                f"{file_lines}\n\nThe files use the currently visible state and "
                "label its data origin. Nothing was emailed automatically."
            )
            citations = [f"local-export:{artifact.path}" for artifact in artifacts]
        elif affirmative_delete and re.search(
            r"\b(?:delete|erase|remove)\s+it\b", question, flags=re.IGNORECASE
        ):
            answer_text = (
                "Tell me which source to delete, for example ‘delete data from "
                "Posh.’ I will not guess the target of an irreversible request."
            )
        elif metric_answer is not None:
            answer_text = str(metric_answer["text"])
            citations = [
                str(item["reference"])
                for item in metric_answer["evidence"]
                if isinstance(item, dict) and item.get("reference")
            ][:12]
        elif calendar_answer is not None:
            answer_text = str(calendar_answer["text"])
            citations = [
                str(item["reference"])
                for item in calendar_answer["evidence"]
                if isinstance(item, dict) and item.get("reference")
            ][:12]
        elif count_answer is not None:
            answer_text = str(count_answer["text"])
            citations = [
                str(item["reference"])
                for item in count_answer["evidence"]
                if isinstance(item, dict) and item.get("reference")
            ][:8]
        elif tracking_answer is not None:
            answer_text = str(tracking_answer["text"])
            citations = [
                str(item["reference"])
                for item in tracking_answer["evidence"]
                if isinstance(item, dict) and item.get("reference")
            ][:8]
            # “Keep track” is itself an explicit operator instruction. Preserve
            # it as retractable company guidance even when the UI checkbox was
            # not selected; ordinary “show/list” questions remain transient.
            save_guidance = save_guidance or bool(tracking_answer["remember"])
        elif upload_help:
            answer_text = (
                "Local intake accepts UTF-8 text, Markdown, CSV, JSON, HTML, XML, "
                "exported EML email, text-layer PDF, DOCX, PNG, JPEG, GIF, and WebP "
                "up to 10 MiB. CSV/JSON grouped metrics have a stricter 1 MiB limit. "
                "Images and scanned PDFs return OCR_REQUIRED because this build does "
                "not infer text without an explicit OCR step. Upload bytes are not saved."
            )
        elif any(
            term in normalized
            for term in (
                "connect email",
                "log into email",
                "gmail",
                "hotmail",
                "outlook",
            )
        ):
            answer_text = (
                "Open Settings → Email accounts, configure your installation's "
                "Google or Microsoft client ID, then choose Connect. The provider "
                "opens its own sign-in and read-only consent screen. Typing an email "
                "address or password into ContextGate never connects a mailbox."
            )
        elif normalized in {
            "what can you do",
            "what can contextgate do",
            "show me what you can do",
        }:
            answer_text = (
                "I can explain the visible ALLOW, REVIEW, and BLOCK receipts; count "
                "or list visible events with source citations; configure confirmed "
                "tracking topics; total bounded CSV or JSON metrics; hide, restore, "
                "or explicitly delete local source records; explain connector setup; "
                "and create local evidence-labeled reports and charts. I do not send "
                "mail, publish files, approve actions, or invent missing evidence."
            )
        elif bool(
            re.search(
                r"\b(?:change|configure|edit|update) (?:what|the topics?) "
                r"(?:you |i )?(?:track|tracking)\b",
                normalized,
            )
            or "change what you track" in normalized
        ):
            answer_text = (
                "Describe one topic in chat, such as ‘track sales by office.’ I will "
                "show the proposed metric and identity/grouping fields; reply ‘confirm "
                "tracking configuration’ to save it or ‘cancel tracking configuration’ "
                "to discard it. Company setup can edit the displayed important detail "
                "and identity fields directly. Topics remain independent: use ‘what are "
                "you tracking?’, ‘show sales’, or ‘undo last tracking change’."
            )
        elif "website" in normalized and any(
            phrase in normalized
            for phrase in (
                "set up",
                "setup",
                "connect",
                "add a website",
                "website source",
                "scan a website",
            )
        ):
            answer_text = (
                "Open Connect sources, add a public HTTP or HTTPS URL and a short "
                "extraction goal, then choose Scan. ContextGate reads bounded public "
                "page evidence and structured event data without signing in, running "
                "page JavaScript, bypassing access controls, or reaching private/local "
                "addresses. Manual scans work at any time; the optional automatic check "
                "runs only while the browser app remains open."
            )
        elif (
            "continuously monitoring" in normalized
            or "continuous monitoring" in normalized
            or "monitoring my sources" in normalized
            or "monitoring our sources" in normalized
        ):
            if self.profile.auto_monitor_enabled:
                interval = self.profile.auto_monitor_minutes
                answer_text = (
                    f"Automatic source checks are configured every {interval} minute"
                    f"{'s' if interval != 1 else ''}, but only while the browser app "
                    "remains open. The browser calls the same bounded scan endpoints for "
                    "configured public websites and already-connected read-only mailboxes. "
                    "This chat cannot see the browser timer’s last or next run; Settings "
                    "shows that live status. There is no always-on server watcher, and "
                    "closing the page stops scheduled checks."
                )
            else:
                answer_text = (
                    "Automatic source checks are off, so ContextGate is not continuously "
                    "monitoring your sources. Manual Scan remains available for configured "
                    "public websites and connected read-only mailboxes. If enabled in "
                    "Settings, periodic checks run only while the browser app remains open; "
                    "there is no always-on server watcher."
                )
        elif (
            "company setup" in normalized
            or "set up my company" in normalized
            or bool(
                re.search(
                    r"\bset (?:this|it|contextgate) up for (?:my|our|the) company\b",
                    normalized,
                )
            )
        ):
            company_label = self.profile.company_name or "not named yet"
            answer_text = (
                f"Open Company setup in the left rail. This workspace is currently "
                f"{company_label}; its most important detail is "
                f"{self.profile.important_detail}, matched by "
                f"{self.profile.identity_summary}."
            )
        elif (
            "crowd" in normalized
            or (
                "113" in normalized.split()
                and any(
                    term in normalized.split()
                    for term in ("people", "person", "attendees", "attendance")
                )
            )
        ) and any(term in normalized for term in ("calculate", "total", "how", "get")):
            answer_text = (
                "The fictional event started at 35 confirmed attendees. A later "
                "message said ‘78 more,’ so I treated it as a delta: 35 + 78 = 113. "
                "If it had said ‘78 people are going,’ I would treat 78 as the new "
                "total after confirming the event identity."
            )
            citations = [
                "fictional-email://crowd-baseline-35",
                "fictional-email://crowd-delta-78",
            ]
        elif (
            "35 main" in normalized
            or "76 new" in normalized
            or "address" in normalized
            and "pattern" in normalized
        ):
            answer_text = (
                "The fictional pattern set contains 3 events at 35 Main Street and "
                "8 at 76 New Avenue, mostly suite 232. A new suite 354 record is held "
                "for confirmation instead of being silently merged."
            )
            citations = [
                "fictional-memory://35-main",
                "fictional-memory://76-new-avenue",
            ]
        else:
            engine = GroundedChatEngine(policy=self.policy)
            result = engine.answer(question, self.chat_history)
            answer_text = result.text
            citations = [
                *(f"case:{item}" for item in result.case_ids),
                *(f"event:{item}" for item in result.evidence_event_ids),
                *(f"rule:{item}" for item in result.rule_ids),
            ]

        try:
            relevant = self.learning.find_relevant_guidance(
                TENANT_ID, text=question, limit=8
            )
        except (OperatorLearningStoreError, ValueError):
            relevant = []
        unique_relevant = []
        seen_guidance: set[str] = set()
        for match in relevant:
            guidance_key = " ".join(match.guidance.guidance.split()).casefold()
            if match.score < 2 or guidance_key in seen_guidance:
                continue
            seen_guidance.add(guidance_key)
            unique_relevant.append(match)
            if len(unique_relevant) == 2:
                break
        relevant = unique_relevant
        if relevant:
            remembered = " ".join(
                f"Remembered company guidance: {match.guidance.guidance}"
                for match in relevant
            )
            answer_text = f"{answer_text}\n\n{remembered}"
            citations.extend(
                f"guidance:{match.guidance.guidance_id}" for match in relevant
            )

        if save_guidance:
            guidance_id = f"guidance-{uuid4()}"
            guidance = OperatorGuidance(
                tenant_id=TENANT_ID,
                guidance_id=guidance_id,
                origin=GuidanceOrigin.CHAT,
                source_record_id=f"chat-{uuid4()}",
                created_at=datetime.now(UTC),
                guidance=question,
                case_ids=[],
            )
            self.learning.append_guidance(guidance)
            citations.append(f"guidance:{guidance_id}")
            saved = True

        user_row = {"role": "user", "text": question, "citations": [], "saved": saved}
        assistant_row = {
            "role": "assistant",
            "text": answer_text,
            "citations": list(dict.fromkeys(citations))[:12],
            "saved": False,
        }
        with self._lock:
            self.chat_history.extend((user_row, assistant_row))
            self.chat_history = self.chat_history[-MAX_HISTORY:]
        return {"answer": assistant_row, "state": self.state()}

    def correct(self, payload: dict[str, Any]) -> dict[str, Any]:
        case_id = _safe_text(
            payload.get("case_id"), label="Case ID", minimum=2, maximum=8
        ).upper()
        scenario = resolve_scenario(case_id)
        events, request = scenario.load()
        decision = evaluate_request(
            events,
            request,
            run_id=f"console-correction-{case_id.casefold()}",
            policy=self.policy,
        )
        try:
            corrected = EnforcementDecision(str(payload.get("corrected_outcome", "")))
        except ValueError:
            raise ConsoleError("Choose ALLOW, REVIEW, or BLOCK.") from None
        reviewer = _safe_text(
            payload.get("reviewer"), label="Reviewer", minimum=2, maximum=128
        )
        rationale = _safe_text(
            payload.get("rationale"), label="Rationale", minimum=3, maximum=2_000
        )
        correction = DecisionCorrection(
            tenant_id=TENANT_ID,
            correction_id=f"correction-{uuid4()}",
            case_id=case_id,
            original_decision_id=decision.decision_id,
            request_fingerprint=decision.request_digest,
            evidence_fingerprint=decision.evidence_digest,
            policy_fingerprint=decision.policy_fingerprint,
            original_outcome=decision.decision,
            corrected_outcome=corrected,
            created_at=datetime.now(UTC),
            reviewer=reviewer,
            rationale=rationale,
        )
        try:
            self.learning.append_decision_correction(correction)
        except ValidationError as exc:
            if corrected == decision.decision:
                raise ConsoleError(
                    "The corrected outcome must differ from the original outcome."
                ) from None
            raise ConsoleError("The correction could not be validated.") from exc
        with self._lock:
            self.selected_case_id = case_id
        return self.state()

    def retract(self, payload: dict[str, Any]) -> dict[str, Any]:
        case_id = _safe_text(
            payload.get("case_id"), label="Case ID", minimum=2, maximum=8
        ).upper()
        reason = _safe_text(
            payload.get("reason", "Operator retracted the correction."),
            label="Reason",
            minimum=3,
            maximum=2_000,
        )
        scenario = resolve_scenario(case_id)
        events, request = scenario.load()
        decision = evaluate_request(
            events,
            request,
            run_id=f"console-retraction-{case_id.casefold()}",
            policy=self.policy,
        )
        correction = self.learning.latest_active_decision_correction(
            TENANT_ID,
            case_id=case_id,
            request_fingerprint=decision.request_digest,
            evidence_fingerprint=decision.evidence_digest,
            policy_fingerprint=decision.policy_fingerprint,
        )
        if correction is None:
            raise ConsoleError("This case has no active correction to retract.")
        retraction = DecisionCorrectionRetraction(
            tenant_id=TENANT_ID,
            retraction_id=f"correction-retraction-{uuid4()}",
            correction_id=correction.correction_id,
            retracted_at=datetime.now(UTC),
            actor="local-operator",
            reason=reason,
        )
        self.learning.append_decision_correction_retraction(retraction)
        return self.state()

    def upload(self, payload: dict[str, Any]) -> dict[str, Any]:
        filename = _safe_text(
            payload.get("filename"), label="Filename", minimum=1, maximum=512
        )
        content_type = _safe_text(
            payload.get("content_type", "application/octet-stream"),
            label="Content type",
            minimum=3,
            maximum=255,
        )
        encoded = payload.get("data_base64")
        if not isinstance(encoded, str) or len(encoded) > (MAX_ARTIFACT_BYTES * 2):
            raise ConsoleError("The uploaded file is empty or too large.")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise ConsoleError("The uploaded file encoding is invalid.") from None
        try:
            receipt = ingest_artifact(filename, content_type, content)
        except ArtifactIntakeError as exc:
            raise ConsoleError(str(exc)) from None
        metric_datasets: list[GroupedMetricDataset] = []
        metric_errors: list[str] = []
        reference = f"upload://{receipt.artifact_id}"
        grouped_topics = [
            item
            for item in (
                self.tracking_topics.topics()
                if self.tracking_topics is not None
                else []
            )
            if item.kind == "grouped_metric" and item.metric_field and item.group_fields
        ]
        for topic in grouped_topics:
            try:
                dataset = parse_grouped_metric_artifact(
                    receipt.safe_filename,
                    content_type,
                    content,
                    preferred_group_fields=topic.group_fields,
                    preferred_metric_fields=[topic.metric_field or ""],
                    source_reference=reference,
                    topic_id=topic.topic_id,
                    topic_name=topic.name,
                    require_preferred_fields=True,
                )
            except GroupedMetricError as exc:
                metric_errors.append(str(exc))
                continue
            if dataset is not None:
                metric_datasets.append(dataset)
        if not metric_datasets:
            try:
                dataset = parse_grouped_metric_artifact(
                    receipt.safe_filename,
                    content_type,
                    content,
                    preferred_group_fields=self.profile.identity_fields,
                    preferred_metric_fields=[self.profile.important_detail],
                    source_reference=reference,
                )
            except GroupedMetricError as exc:
                metric_errors.append(str(exc))
            else:
                if dataset is not None:
                    metric_datasets.append(dataset)
        with self._lock:
            for dataset in metric_datasets:
                self.grouped_metric_datasets[dataset.dataset_id] = dataset
            while len(self.grouped_metric_datasets) > MAX_GROUPED_METRIC_DATASETS:
                oldest_id = next(iter(self.grouped_metric_datasets))
                del self.grouped_metric_datasets[oldest_id]
        self.last_upload = {
            "filename": receipt.safe_filename,
            "status": receipt.status.value,
            "size_bytes": receipt.size_bytes,
            "message": receipt.message,
            "extracted_chars": receipt.extracted_chars,
            "sha256": receipt.sha256,
            "grouped_metric_datasets": [item.dataset_id for item in metric_datasets],
            "grouped_metric_rows": sum(item.row_count for item in metric_datasets),
            "grouped_metric_error": metric_errors[0] if metric_errors else None,
        }
        if receipt.extracted_text and not metric_datasets:
            text_preview = receipt.extracted_text[:1_000]
            self.catalog.add_record(
                SourceRecord(
                    record_id=receipt.artifact_id,
                    event_key=re.sub(
                        r"[^a-z0-9]+", "-", receipt.safe_filename.casefold()
                    ).strip("-")
                    or receipt.artifact_id,
                    title=receipt.safe_filename,
                    source_name="Uploaded file",
                    location=(
                        "New York City"
                        if re.search(
                            r"\b(new york city|nyc|manhattan|brooklyn|queens)\b",
                            text_preview,
                            flags=re.IGNORECASE,
                        )
                        else "Unknown"
                    ),
                    evidence_reference=f"upload://{receipt.artifact_id}",
                    fictional=False,
                )
            )
        return self.state()

    def configure_google(self, payload: dict[str, Any]) -> dict[str, Any]:
        encoded = payload.get("client_json_base64")
        if not isinstance(encoded, str) or len(encoded) > 64 * 1024:
            raise ConsoleError("Choose a Google Desktop OAuth JSON file.")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise ConsoleError("Google client JSON encoding is invalid.") from None
        self.oauth.configure_google_json(raw)
        return self.state()

    def configure_microsoft(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.oauth.configure_microsoft(str(payload.get("client_id", "")))
        return self.state()

    def start_oauth(self, provider: str, base_url: str) -> dict[str, str]:
        if provider not in {"google", "microsoft"}:
            raise ConsoleError("Unknown email provider.")
        return {
            "authorization_url": self.oauth.start_authorization(provider, base_url)  # type: ignore[arg-type]
        }

    def scan_mail(self, payload: dict[str, Any]) -> dict[str, Any]:
        provider = str(payload.get("provider", ""))
        if provider not in {"google", "microsoft"}:
            raise ConsoleError("Unknown email provider.")
        account = payload.get("account")
        if account is not None and not isinstance(account, str):
            raise ConsoleError("Mailbox account is invalid.")
        limit = payload.get("limit", 25)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ConsoleError("Mailbox scan limit is invalid.")
        messages = self.oauth.list_messages(  # type: ignore[arg-type]
            provider, account=account, limit=limit
        )
        imported = self.catalog.add_mail(messages)
        return {"imported": imported, "state": self.state()}

    def disconnect_mail(self, payload: dict[str, Any]) -> dict[str, Any]:
        provider = str(payload.get("provider", ""))
        if provider not in {"google", "microsoft"}:
            raise ConsoleError("Unknown email provider.")
        account = payload.get("account")
        if account is not None and not isinstance(account, str):
            raise ConsoleError("Mailbox account is invalid.")
        removed = self.oauth.disconnect(provider, account=account)  # type: ignore[arg-type]
        return {"removed": removed, "state": self.state()}


class CounterLike:
    """Tiny explicit counter that always returns integer zero for missing keys."""

    def __init__(self, values: Any) -> None:
        self._counts: dict[str, int] = {}
        for value in values:
            self._counts[value] = self._counts.get(value, 0) + 1

    def get(self, key: str) -> int:
        return self._counts.get(key, 0)


class ContextGateHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self, server_address: tuple[str, int], application: ConsoleApplication
    ):
        super().__init__(server_address, ContextGateRequestHandler)
        self.application = application

    def server_close(self) -> None:
        try:
            self.application.close()
        finally:
            super().server_close()


class ContextGateRequestHandler(BaseHTTPRequestHandler):
    server_version = "ContextGateLocal/0.1"

    @property
    def application(self) -> ConsoleApplication:
        server = self.server
        assert isinstance(server, ContextGateHTTPServer)
        return server.application

    def log_message(self, format: str, *args: object) -> None:
        # Keep local logs useful without including query strings or request bodies.
        if self.path.startswith("/api/health"):
            return
        safe_path = urlparse(self.path).path
        print(f"{self.address_string()} {self.command} {safe_path}")

    def _headers(self, status: HTTPStatus, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'",
        )
        self.end_headers()

    def _json(
        self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK
    ) -> None:
        body = json.dumps(payload, default=_json_ready, ensure_ascii=False).encode(
            "utf-8"
        )
        self._headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _html(self, markup: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = markup.encode("utf-8")
        self._headers(status, "text/html; charset=utf-8", len(body))
        self.wfile.write(body)

    def _validated_host(self) -> bool:
        host = self.headers.get("Host", "")
        expected = f"127.0.0.1:{self.server.server_port}"
        localhost = f"localhost:{self.server.server_port}"
        return host in {expected, localhost}

    def _check_post_origin(self) -> None:
        if not self._validated_host():
            raise ConsoleError("The local request host is invalid.")
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {
            f"http://127.0.0.1:{self.server.server_port}",
            f"http://localhost:{self.server.server_port}",
        }:
            raise ConsoleError("Cross-origin requests are not allowed.")
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
        if content_type != "application/json":
            raise ConsoleError("Requests must use application/json.")

    def _body(self) -> dict[str, Any]:
        self._check_post_origin()
        raw_length = self.headers.get("Content-Length", "")
        try:
            length = int(raw_length)
        except ValueError:
            raise ConsoleError("Request length is invalid.") from None
        if not 0 <= length <= MAX_JSON_BODY:
            raise ConsoleError("Request body exceeds the safe limit.")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ConsoleError("Request body must be valid UTF-8 JSON.") from None
        if not isinstance(payload, dict):
            raise ConsoleError("Request body must be a JSON object.")
        return payload

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/health":
                self._json(
                    {
                        "service": "ContextGate",
                        "status": "ok",
                        "ui": "one-screen-command-center",
                    }
                )
                return
            if parsed.path == "/api/state":
                self._json(self.application.state())
                return
            if parsed.path.startswith("/oauth/") and parsed.path.endswith("/callback"):
                self._oauth_callback(parsed.path, parse_qs(parsed.query))
                return
            oauth_query = parse_qs(parsed.query)
            if (
                parsed.path == "/"
                and oauth_query.get("state")
                and (oauth_query.get("code") or oauth_query.get("error"))
            ):
                self._oauth_callback("/oauth/google/callback", oauth_query)
                return
            if parsed.path == "/":
                self._serve_file(STATIC_ROOT / "index.html")
                return
            if parsed.path in {
                "/styles.css",
                "/app.js",
                "/favicon.svg",
                "/static/styles.css",
                "/static/app.js",
                "/static/favicon.svg",
            }:
                self._serve_file(STATIC_ROOT / Path(parsed.path).name)
                return
            self._json({"error": "Not found."}, HTTPStatus.NOT_FOUND)
        except (ConsoleError, EmailConnectorError, OperatorLearningStoreError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception:  # noqa: BLE001 - HTTP boundary must return a safe error
            self._json(
                {"error": "ContextGate could not complete that request safely."},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def _serve_file(self, path: Path) -> None:
        if not path.is_file() or path.parent != STATIC_ROOT:
            self._json({"error": "Interface asset is missing."}, HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type == "application/javascript":
            content_type += "; charset=utf-8"
        self._headers(HTTPStatus.OK, content_type, len(body))
        self.wfile.write(body)

    def _oauth_callback(self, path: str, query: dict[str, list[str]]) -> None:
        provider = "google" if "/google/" in path else "microsoft"
        if query.get("error"):
            reason = html.escape(query["error"][0][:100])
            self._html(
                _oauth_result_page(False, f"Sign-in was not completed ({reason}).")
            )
            return
        state = query.get("state", [""])[0]
        code = query.get("code", [""])[0]
        try:
            account = self.application.oauth.finish_authorization(
                provider,
                state=state,
                code=code,  # type: ignore[arg-type]
            )
        except EmailConnectorError as exc:
            self._html(_oauth_result_page(False, str(exc)), HTTPStatus.BAD_REQUEST)
            return
        self._html(
            _oauth_result_page(
                True,
                f"{provider.title()} connected with read-only mailbox access for {account}.",
            )
        )

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = self._body()
            routes: dict[str, Any] = {
                "/api/select": self.application.select_case,
                "/api/chat": self.application.chat,
                "/api/profile": self.application.update_profile,
                "/api/profile/logo": self.application.save_company_logo,
                "/api/profile/logo/remove": lambda _payload: (
                    self.application.remove_company_logo()
                ),
                "/api/websites/add": self.application.add_website_source,
                "/api/websites/scan": self.application.scan_website_source,
                "/api/websites/remove": self.application.remove_website_source,
                "/api/correct": self.application.correct,
                "/api/retract": self.application.retract,
                "/api/upload": self.application.upload,
                "/api/demo/reset": lambda _payload: self.application.reset_demo(),
                "/api/connectors/google/configure": self.application.configure_google,
                "/api/connectors/microsoft/configure": self.application.configure_microsoft,
                "/api/connectors/scan": self.application.scan_mail,
                "/api/connectors/disconnect": self.application.disconnect_mail,
            }
            if parsed.path in routes:
                self._json(routes[parsed.path](payload))
                return
            if parsed.path in {
                "/api/connectors/google/start",
                "/api/connectors/microsoft/start",
            }:
                provider = "google" if "/google/" in parsed.path else "microsoft"
                base_url = f"http://127.0.0.1:{self.server.server_port}"
                self._json(self.application.start_oauth(provider, base_url))
                return
            self._json({"error": "Not found."}, HTTPStatus.NOT_FOUND)
        except (
            ConsoleError,
            CompanyProfileError,
            EmailConnectorError,
            OperatorLearningStoreError,
            PolicyConfigError,
            WebsiteSourceError,
            ValidationError,
            ValueError,
        ) as exc:
            message = str(exc)
            if isinstance(exc, ValidationError):
                message = "One or more settings are invalid."
            self._json({"error": message}, HTTPStatus.BAD_REQUEST)
        except Exception:  # noqa: BLE001 - HTTP boundary must return a safe error
            self._json(
                {"error": "ContextGate could not complete that request safely."},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )


def _oauth_result_page(success: bool, message: str) -> str:
    title = "Mailbox connected" if success else "Connection not completed"
    color = "#38d9ff" if success else "#fb7185"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>body{{margin:0;background:#040711;color:#eaf6ff;font:16px system-ui;display:grid;place-items:center;min-height:100vh}}main{{max-width:620px;padding:42px;border:1px solid #173658;background:#07111f;border-radius:20px;box-shadow:0 0 50px #0ea5e933}}h1{{color:{color}}}a{{color:#38d9ff}}</style></head>
<body><main><h1>{html.escape(title)}</h1><p>{html.escape(message)}</p><p>Tokens are held in memory only and disappear when ContextGate closes.</p><p><a href="/">Return to ContextGate</a></p></main></body></html>"""


def create_server(
    host: str = "127.0.0.1",
    port: int = 8501,
    application: ConsoleApplication | None = None,
) -> ContextGateHTTPServer:
    """Create a local-only server for tests or the desktop launcher."""

    if host != "127.0.0.1":
        raise ConsoleError("ContextGate's desktop server must bind to 127.0.0.1.")
    return ContextGateHTTPServer((host, port), application or ConsoleApplication())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the local ContextGate command center"
    )
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1"])
    parser.add_argument("--port", default=8501, type=int)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    try:
        server = create_server(args.host, args.port)
    except OSError:
        print(
            f"ContextGate could not use http://{args.host}:{args.port}. "
            "Close the older ContextGate window/process and try again."
        )
        return 1
    url = f"http://{args.host}:{args.port}"
    print(f"ContextGate command center: {url}")
    print("Local-only · read-only connectors · no external action")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
