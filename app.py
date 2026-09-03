"""Self-hosted ContextGate operator app and built-in acceptance lab."""

from __future__ import annotations

import html
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import streamlit as st
from pydantic import TypeAdapter, ValidationError

from context_gate.approvals import record_review
from context_gate.audit_log import AppendOnlyAuditLog
from context_gate.authority import effective_trust, policy_for
from context_gate.chat import GroundedChatEngine
from context_gate.company_memory import (
    CompanyObservation,
    HumanCorrection,
    MemoryStore,
    MemoryStoreError,
    PatternAnalyzer,
    PatternOutcome,
)
from context_gate.decision_engine import evaluate_request
from context_gate.evidence import build_evidence_package
from context_gate.intake import (
    MAX_ARTIFACT_BYTES,
    ArtifactIntakeError,
    ArtifactStatus,
    create_context_event_candidate,
    ingest_artifact,
)
from context_gate.models import (
    ActionRequest,
    ContextEvent,
    DecisionRecord,
    EnforcementDecision,
    EvidenceStatus,
    ReviewAction,
    ReviewEvent,
    Sensitivity,
)
from context_gate.normalization import normalize_event, normalize_request
from context_gate.operator_learning import (
    DecisionCorrection,
    DecisionCorrectionRetraction,
    GuidanceMatch,
    GuidanceOrigin,
    GuidanceRetraction,
    OperatorGuidance,
    OperatorLearningStore,
    OperatorLearningStoreError,
)
from context_gate.policy_config import (
    DEFAULT_POLICY,
    ActivePolicy,
    PolicyConfigError,
    get_active_policy,
)
from context_gate.scenario import Scenario, get_scenario, iter_scenarios, load_scenario
from context_gate.semantic_updates import (
    CategorySemanticConfig,
    CorrectedSemanticUpdateProposal,
    EntityQuantityState,
    HumanQuantityCorrection,
    IncomingQuantityStatement,
    ProposalOutcome,
    QuantityMode,
    SemanticUpdateProposal,
    apply_human_correction,
    interpret_quantity_update,
)
from context_gate.voice import browser_speaker_html

ROOT = Path(__file__).resolve().parent
AUDIT_PATH = ROOT / "runtime" / "audit.jsonl"
COMPANY_MEMORY_PATH_ENV = "CONTEXTGATE_MEMORY_PATH"
DEFAULT_COMPANY_MEMORY_PATH = ROOT / "runtime" / "company_memory.sqlite3"
OPERATOR_TENANT_ID_ENV = "CONTEXTGATE_TENANT_ID"
DEFAULT_OPERATOR_TENANT_ID = "local-company"
MAX_MEMORY_PATH_LENGTH = 4_096
MAX_MEMORY_UI_CORRECTIONS = 25
MAX_MEMORY_UI_TRACE_ROWS = 25
WORKBENCH_MAX_JSON_BYTES = 64 * 1024
WORKBENCH_MAX_EVENTS = 128
EVENT_LIST_ADAPTER = TypeAdapter(list[ContextEvent])
SEMANTIC_PROFILE_MAX_BYTES = 16 * 1024
SEMANTIC_STATE_AS_OF = datetime(2026, 9, 2, 12, tzinfo=UTC)
OPERATOR_TENANT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
CHAT_CORRECTION_PATTERN = re.compile(
    r"\b(mistake|mistaken|wrong|incorrect|false positive|undo|correction|corrected)\b"
    r"|\bcorrect (?:this|that|it|the decision)\b",
    re.IGNORECASE,
)
DEFAULT_SEMANTIC_PROFILE = {
    "category": "event",
    "identity_keys": ["event_name", "event_date"],
    "important_fields": ["crowd_size"],
    "quantity_fields": [
        {
            "field_name": "crowd_size",
            "metric_nouns": [
                "people",
                "attendee",
                "attendees",
                "guests",
                "crowd size",
            ],
            "delta_markers": ["more", "additional", "add"],
            "total_markers": ["total", "in all", "overall"],
            "status_markers": ["confirmed", "going", "attending", "registered"],
            "negation_markers": [
                "not",
                "no longer",
                "cancelled",
                "canceled",
                "void",
                "ignore",
            ],
            "maximum_plausible_value": 500,
            "maximum_absolute_change": 200,
        }
    ],
}

BANNER_CONTENT = {
    EnforcementDecision.BLOCK: (
        "block",
        "Unsafe action blocked",
        "The requested change conflicts with stronger evidence.",
    ),
    EnforcementDecision.REVIEW: (
        "review",
        "Action held for human review",
        "ContextGate cannot safely decide without a person.",
    ),
    EnforcementDecision.ALLOW: (
        "allow",
        "Safe preview allowed",
        "The non-consequential request matches authoritative evidence.",
    ),
}

SCENARIO_LESSONS = {
    "conflict": (
        "Freshness is not authority. The later community listing does not replace "
        "the official confirmation, so CG-002 blocks before any model runs."
    ),
    "safe": (
        "ContextGate is a gate, not a blanket blocker. Complete authoritative "
        "evidence can allow a non-consequential preview through CG-008."
    ),
    "missing-provenance": (
        "Missing provenance is a decision, not an invitation to guess. CG-005 "
        "holds the action until the evidence can be verified."
    ),
    "stale-agenda": (
        "A valid source cannot make an outdated request current. CG-004 blocks "
        "the older effective time and preserves the live schedule."
    ),
    "awards-time-conflict": (
        "A copied agenda cannot outrank the organizer page. CG-002 keeps the "
        "official awards time and blocks the conflicting reminder."
    ),
    "near-peer-time-conflict": (
        "Automation should not break a close authority tie. CG-003 routes two "
        "credible but conflicting check-in times to a person."
    ),
    "consequential-calendar": (
        "Correct evidence is necessary but not always sufficient. CG-007 requires "
        "approval before a verified value becomes an external calendar write."
    ),
    "accessibility-preview": (
        "The organizer-backed value is complete and the preview is reversible, "
        "so CG-008 allows it without pretending an external action occurred."
    ),
    "webcast-preview": (
        "A verified link can safely populate a private preview. Any later publish "
        "or evidence change must pass through the gate again."
    ),
}

REVIEW_RATIONALES = {
    "conflict": "Conflicting lower-authority evidence must be verified with the organizer.",
    "missing-provenance": "The source and evidence reference must be supplied before action.",
}

st.set_page_config(page_title="ContextGate", page_icon="🛡️", layout="wide")
st.markdown(
    """
    <style>
    .block-container {max-width: 1220px; padding-top: 1.7rem; padding-bottom: 3rem;}
    .cg-kicker {color:#7dd3fc; font-size:.78rem; font-weight:700; letter-spacing:.14em; text-transform:uppercase;}
    .cg-subtitle {color:#94a3b8; font-size:1.02rem; margin-top:-.4rem;}
    .cg-pipeline {display:flex; gap:.45rem; align-items:center; flex-wrap:wrap; margin:.9rem 0 1.15rem 0;}
    .cg-stage {border:1px solid #334155; background:#0f172a; color:#e2e8f0; border-radius:999px;
      padding:.32rem .7rem; font-size:.78rem; font-weight:600;}
    .cg-arrow {color:#64748b; font-weight:700;}
    .cg-banner {border-radius:14px; padding:1.2rem 1.4rem; margin:.6rem 0 1.2rem 0;}
    .cg-banner h2 {margin:0 0 .25rem 0;}
    .cg-banner p {color:#e2e8f0; margin:.18rem 0;}
    .cg-banner.block {border:1px solid #ef4444; background:linear-gradient(135deg,#2a1115,#170d11);}
    .cg-banner.block h2 {color:#fca5a5;}
    .cg-banner.review {border:1px solid #f59e0b; background:linear-gradient(135deg,#2a1d0a,#181107);}
    .cg-banner.review h2 {color:#fcd34d;}
    .cg-banner.allow {border:1px solid #22c55e; background:linear-gradient(135deg,#0b281b,#07170f);}
    .cg-banner.allow h2 {color:#86efac;}
    .cg-source {border:1px solid #334155; background:#111827; color:#e2e8f0; border-radius:12px;
      padding:1rem; min-height:230px;}
    .cg-source.authoritative {border-color:#22c55e; box-shadow:0 0 0 1px #22c55e22;}
    .cg-source.incomplete {border-color:#f59e0b; box-shadow:0 0 0 1px #f59e0b22;}
    .cg-label {color:#94a3b8; font-size:.75rem; text-transform:uppercase; letter-spacing:.08em;}
    .cg-source strong, .cg-value {color:#f8fafc;}
    .cg-value {font-size:1.3rem; font-weight:700; margin:.2rem 0 .65rem 0;}
    .cg-pill {display:inline-block; border-radius:999px; padding:.18rem .55rem; font-size:.73rem;
      margin:.12rem .2rem .12rem 0; background:#1e293b; color:#cbd5e1;}
    .st-key-dashboard_total_card, .st-key-dashboard_allow_card,
    .st-key-dashboard_block_card, .st-key-dashboard_review_card {
      border-radius:14px; border:1px solid; padding:.2rem .45rem; min-height:112px;
    }
    .st-key-dashboard_total_card {background:#0c1d33; border-color:#38bdf8;}
    .st-key-dashboard_allow_card {background:#052e16; border-color:#4ade80;}
    .st-key-dashboard_block_card {background:#450a0a; border-color:#fb7185;}
    .st-key-dashboard_review_card {background:#422006; border-color:#fbbf24;}
    .st-key-dashboard_total_card [data-testid="stMetricValue"] {color:#bae6fd;}
    .st-key-dashboard_allow_card [data-testid="stMetricValue"] {color:#dcfce7;}
    .st-key-dashboard_block_card [data-testid="stMetricValue"] {color:#ffe4e6;}
    .st-key-dashboard_review_card [data-testid="stMetricValue"] {color:#fef3c7;}
    .st-key-dashboard_chat_panel {border-color:#38bdf8 !important;
      background:linear-gradient(135deg,#071827,#0b1220); margin-top:.8rem;}
    .cg-status-strip {border-left:4px solid; border-radius:8px; padding:.55rem .75rem;
      margin:.1rem 0 .65rem 0; font-weight:700;}
    .cg-status-strip.allow {background:#052e16; border-color:#4ade80; color:#dcfce7;}
    .cg-status-strip.review {background:#422006; border-color:#fbbf24; color:#fef3c7;}
    .cg-status-strip.block {background:#450a0a; border-color:#fb7185; color:#ffe4e6;}
    button:focus-visible, [role="button"]:focus-visible {outline:3px solid #7dd3fc !important;
      outline-offset:2px;}
    @media (prefers-color-scheme: light) {
      .st-key-dashboard_allow_card, .cg-status-strip.allow {background:#f0fdf4; color:#14532d;}
      .st-key-dashboard_review_card, .cg-status-strip.review {background:#fffbeb; color:#78350f;}
      .st-key-dashboard_block_card, .cg-status-strip.block {background:#fef2f2; color:#7f1d1d;}
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def escaped(value: object | None, *, missing: str = "Missing") -> str:
    """Escape a value before placing it into custom HTML."""

    return html.escape(missing if value in (None, "") else str(value))


@st.cache_resource
def audit_log() -> AppendOnlyAuditLog:
    return AppendOnlyAuditLog(AUDIT_PATH)


def _company_memory_path() -> Path:
    """Resolve only a local filesystem path for the SQLite memory database."""

    configured = os.environ.get(COMPANY_MEMORY_PATH_ENV)
    raw_path = (
        configured if configured is not None else str(DEFAULT_COMPANY_MEMORY_PATH)
    )
    if (
        not raw_path
        or len(raw_path) > MAX_MEMORY_PATH_LENGTH
        or "\x00" in raw_path
        or raw_path == ":memory:"
        or "://" in raw_path
        or raw_path.startswith(("\\\\", "//"))
    ):
        raise MemoryStoreError("Company memory database path must be a local file.")

    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    resolved = candidate.resolve(strict=False)
    if resolved.exists() and not resolved.is_file():
        raise MemoryStoreError("Company memory database path must name a file.")
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise MemoryStoreError(
            "Company memory database directory could not be prepared safely."
        ) from None
    return resolved


@st.cache_resource(max_entries=32)
def company_memory_store(db_path: str) -> MemoryStore:
    """Keep one process-local memory resource per configured database path."""

    return MemoryStore(db_path)


@st.cache_resource(max_entries=32)
def operator_learning_store(db_path: str) -> OperatorLearningStore:
    """Keep one process-local operator-learning resource per local database."""

    return OperatorLearningStore(db_path)


def _operator_tenant_id() -> str:
    """Return a bounded local tenant label; production must bind this to auth."""

    tenant_id = os.environ.get(
        OPERATOR_TENANT_ID_ENV,
        DEFAULT_OPERATOR_TENANT_ID,
    ).strip()
    if not OPERATOR_TENANT_PATTERN.fullmatch(tenant_id):
        raise OperatorLearningStoreError("The operator-learning tenant ID is invalid.")
    return tenant_id


def _fictional_company_observations(tenant_id: str) -> list[CompanyObservation]:
    """Return the fixed, retry-safe 3 + 8 fictional company dataset."""

    source_sequence = (
        ("registration_confirmation", 0.99),
        ("organizer_api", 0.98),
        ("organizer_website", 0.95),
        ("official_email", 0.92),
        ("partner_website", 0.80),
    )
    definitions: list[tuple[str, datetime, str, str, str, float, str]] = []
    for number in range(1, 4):
        source_type, trust_score = source_sequence[(number - 1) % len(source_sequence)]
        definitions.append(
            (
                f"fictional-35-main-{number}",
                datetime(2026, 1, number, 15, tzinfo=UTC),
                "35 Main St",
                "110",
                source_type,
                trust_score,
                f"fictional://{source_type}/35-main/{number}",
            )
        )
    for number in range(1, 9):
        source_type, trust_score = source_sequence[(number + 1) % len(source_sequence)]
        definitions.append(
            (
                f"fictional-76-new-{number}",
                datetime(2026, 2, number, 15, tzinfo=UTC),
                "76 New Avenue",
                "232",
                source_type,
                trust_score,
                f"fictional://{source_type}/76-new/{number}",
            )
        )

    return [
        CompanyObservation(
            tenant_id=tenant_id,
            observation_id=observation_id,
            category="company_event",
            occurred_at=occurred_at,
            attributes={"address": address, "suite": suite},
            source_type=source_type,
            trust_score=trust_score,
            status=EvidenceStatus.CONFIRMED,
            sensitivity=Sensitivity.INTERNAL,
            evidence_reference=evidence_reference,
        )
        for (
            observation_id,
            occurred_at,
            address,
            suite,
            source_type,
            trust_score,
            evidence_reference,
        ) in definitions
    ]


def _seed_fictional_company_memory(
    store: MemoryStore,
    tenant_id: str,
) -> tuple[int, int]:
    inserted = 0
    unchanged = 0
    for observation in _fictional_company_observations(tenant_id):
        result = store.upsert(observation)
        if result == "inserted":
            inserted += 1
        else:
            unchanged += 1
    return inserted, unchanged


def start_run(
    scenario_name: str,
    run_id: str | None = None,
    *,
    policy: ActivePolicy | None = None,
) -> None:
    """Evaluate a fresh scenario and append its immutable decision receipt."""

    events, request = load_scenario(scenario_name)
    run_id = run_id or f"run-{scenario_name}-{uuid4().hex[:10]}"
    decision = evaluate_request(events, request, run_id=run_id, policy=policy)
    try:
        audit_log().append_decision(decision)
        st.session_state.cg_audit_error = None
    except (OSError, ValueError) as exc:
        st.session_state.cg_audit_error = f"{type(exc).__name__}: {exc}"
    st.session_state.cg_scenario = scenario_name
    st.session_state.cg_run_id = run_id
    st.session_state.cg_decision = decision
    st.session_state.cg_review = None


def read_audit_state() -> tuple[list[object], bool, str | None]:
    """Keep a malformed local log visible as a failure instead of crashing the UI."""

    try:
        entries = audit_log().read_entries()
        return entries, audit_log().verify_chain(), None
    except (OSError, ValueError) as exc:
        return [], False, f"{type(exc).__name__}: {exc}"


def _scenario_from_case_id(case_id: str) -> Scenario | None:
    return next(
        (
            scenario
            for scenario in iter_scenarios()
            if scenario.case_id.casefold() == case_id.casefold()
        ),
        None,
    )


def _chat_correction_intent(
    question: str,
    cited_case_ids: list[str],
) -> dict[str, str] | None:
    """Recognize a correction request but leave it pending for human confirmation."""

    if not CHAT_CORRECTION_PATTERN.search(question):
        return None
    explicit_case = re.search(r"\b([ABR][1-3])\b", question, re.IGNORECASE)
    if explicit_case:
        case_id = explicit_case.group(1).upper()
    elif len(cited_case_ids) == 1:
        case_id = cited_case_ids[0]
    else:
        case_id = get_scenario(st.session_state.cg_scenario).case_id
    if _scenario_from_case_id(case_id) is None:
        return None

    normalized = question.casefold()
    directional_outcome = re.search(
        r"(?:should|must|needs? to|change(?:d)?(?: it)? to|correct(?:ed)?(?: it)? to)\s+"
        r"(?:be\s+)?(allow(?:ed)?|pass(?:ed)?|approve(?:d)?|review(?:ed)?|hold|"
        r"block(?:ed)?|reject(?:ed)?|stop(?:ped)?)\b",
        normalized,
    )
    proposed = ""
    if directional_outcome:
        outcome_word = directional_outcome.group(1)
        if outcome_word.startswith(("allow", "pass", "approve")):
            proposed = EnforcementDecision.ALLOW.value
        elif outcome_word.startswith(("review", "hold")):
            proposed = EnforcementDecision.REVIEW.value
        else:
            proposed = EnforcementDecision.BLOCK.value
    elif re.search(r"\b(undo|remove|false positive)\b", normalized):
        proposed = EnforcementDecision.ALLOW.value

    return {
        "case_id": case_id,
        "proposed_outcome": proposed,
        "rationale": question.strip()[:2_000],
    }


def _find_relevant_guidance(
    store: OperatorLearningStore,
    tenant_id: str,
    *,
    case_ids: list[str],
    question: str,
) -> list[GuidanceMatch]:
    matches_by_id: dict[str, GuidanceMatch] = {}
    searches: list[tuple[str | None, str | None]] = [(None, question)]
    searches.extend((case_id, question) for case_id in case_ids)
    for case_id, text in searches:
        try:
            matches = store.find_relevant_guidance(
                tenant_id,
                case_id=case_id,
                text=text,
                limit=3,
            )
        except ValueError:
            continue
        for match in matches:
            matches_by_id.setdefault(match.guidance.guidance_id, match)
    return list(matches_by_id.values())[:3]


def _append_operator_guidance(
    store: OperatorLearningStore,
    tenant_id: str,
    *,
    guidance: str,
    case_ids: list[str],
    origin: GuidanceOrigin,
    source_record_id: str,
    guidance_id: str | None = None,
    created_at: datetime | None = None,
) -> OperatorGuidance:
    record = OperatorGuidance(
        tenant_id=tenant_id,
        guidance_id=guidance_id or f"guidance-{uuid4().hex[:20]}",
        origin=origin,
        source_record_id=source_record_id,
        created_at=created_at or datetime.now(UTC),
        guidance=guidance,
        case_ids=case_ids,
    )
    store.append_guidance(record)
    return record


def _save_decision_correction(
    store: OperatorLearningStore,
    tenant_id: str,
    *,
    scenario: Scenario,
    decision: DecisionRecord,
    corrected_outcome: EnforcementDecision,
    reviewer: str,
    rationale: str,
    remember_similar: bool,
    correction_id: str | None = None,
    created_at: datetime | None = None,
) -> tuple[DecisionCorrection, bool]:
    correction = DecisionCorrection(
        tenant_id=tenant_id,
        correction_id=correction_id or f"correction-{uuid4().hex[:20]}",
        case_id=scenario.case_id,
        original_decision_id=decision.decision_id,
        request_fingerprint=decision.request_digest,
        evidence_fingerprint=decision.evidence_digest,
        policy_fingerprint=decision.policy_fingerprint,
        original_outcome=decision.decision,
        corrected_outcome=corrected_outcome,
        created_at=created_at or datetime.now(UTC),
        reviewer=reviewer,
        rationale=rationale,
    )
    store.append_decision_correction(correction)
    guidance_saved = not remember_similar
    if remember_similar:
        try:
            _append_operator_guidance(
                store,
                tenant_id,
                guidance=(
                    f"For cases like {scenario.case_id} ({scenario.title}), the "
                    f"operator corrected {decision.decision.value} to "
                    f"{corrected_outcome.value}: {rationale}"
                ),
                case_ids=[scenario.case_id],
                origin=GuidanceOrigin.REVIEW,
                source_record_id=correction.correction_id,
                guidance_id=f"guidance-{correction.correction_id}",
                created_at=correction.created_at,
            )
            guidance_saved = True
        except (OperatorLearningStoreError, ValidationError, TypeError, ValueError):
            guidance_saved = False
    return correction, guidance_saved


def _retract_decision_correction(
    store: OperatorLearningStore,
    tenant_id: str,
    correction: DecisionCorrection,
) -> bool:
    """Retract the resolution and best-effort retract its advisory guidance."""

    guidance_retracted = True
    try:
        related_guidance = [
            record
            for record in store.list_active_guidance(tenant_id, limit=5_000)
            if record.source_record_id == correction.correction_id
        ]
        for record in related_guidance:
            store.append_retraction(
                GuidanceRetraction(
                    tenant_id=tenant_id,
                    retraction_id=f"retract-guidance-{uuid4().hex[:18]}",
                    guidance_id=record.guidance_id,
                    retracted_at=datetime.now(UTC),
                    actor=correction.reviewer,
                    reason="The linked decision correction was retracted.",
                )
            )
    except (OperatorLearningStoreError, ValidationError, TypeError, ValueError):
        guidance_retracted = False

    store.append_decision_correction_retraction(
        DecisionCorrectionRetraction(
            tenant_id=tenant_id,
            retraction_id=f"retract-correction-{uuid4().hex[:18]}",
            correction_id=correction.correction_id,
            retracted_at=datetime.now(UTC),
            actor=correction.reviewer,
            reason="The operator restored the original deterministic outcome.",
        )
    )
    return guidance_retracted


def ask_contextgate(
    question: str,
    *,
    learning_store: OperatorLearningStore | None = None,
    tenant_id: str | None = None,
    remember_guidance: bool = False,
) -> None:
    """Append a grounded answer plus traceable, advisory operator learning."""

    history = st.session_state.cg_chat_history
    answer = GroundedChatEngine(policy=page_policy).answer(question, history)
    response_text = answer.text
    guidance_ids: list[str] = []
    correction_ids: list[str] = []
    learned_support = False

    if learning_store is not None and tenant_id is not None:
        try:
            matches = _find_relevant_guidance(
                learning_store,
                tenant_id,
                case_ids=answer.case_ids,
                question=question,
            )
            if matches:
                learned_support = True
                guidance_ids = [match.guidance.guidance_id for match in matches]
                remembered = " ".join(
                    f"[{match.guidance.guidance_id}] {match.guidance.guidance}"
                    for match in matches
                )
                response_text += (
                    "\n\nRemembered operator guidance (advisory only; it cannot "
                    f"change the gate): {remembered}"
                )

            active_corrections: dict[str, DecisionCorrection] = {}
            for case_id in answer.case_ids:
                matching_scenario = _scenario_from_case_id(case_id)
                if matching_scenario is None:
                    continue
                matching_events, matching_request = matching_scenario.load()
                matching_decision = evaluate_request(
                    matching_events,
                    matching_request,
                    run_id=f"chat-correction-{matching_scenario.name}",
                    policy=page_policy,
                )
                correction = learning_store.latest_active_decision_correction(
                    tenant_id,
                    case_id=case_id,
                    request_fingerprint=matching_decision.request_digest,
                    evidence_fingerprint=matching_decision.evidence_digest,
                    policy_fingerprint=matching_decision.policy_fingerprint,
                )
                if correction is not None:
                    active_corrections[case_id] = correction
            if active_corrections:
                learned_support = True
                correction_ids = [
                    correction.correction_id
                    for correction in active_corrections.values()
                ]
                correction_text = " ".join(
                    f"[case {case_id}] Human-corrected: original "
                    f"{correction.original_outcome.value} → effective "
                    f"{correction.corrected_outcome.value}. Why: "
                    f"{correction.rationale} [correction "
                    f"{correction.correction_id}]"
                    for case_id, correction in active_corrections.items()
                )
                response_text += (
                    "\n\nThe original receipts remain immutable. The resolved "
                    f"dashboard also applies these active corrections: {correction_text}"
                )
        except (OperatorLearningStoreError, ValidationError, TypeError, ValueError):
            st.session_state.cg_operator_error = (
                "Stored operator learning could not be read safely; the original "
                "deterministic answer is still available."
            )

    correction_intent = _chat_correction_intent(question, answer.case_ids)
    if correction_intent is not None:
        st.session_state.cg_pending_chat_correction = correction_intent
        response_text += (
            f"\n\nI prepared a correction proposal for case "
            f"{correction_intent['case_id']} below. Confirm the outcome and rationale "
            "to append a correction receipt. I will not erase the original or execute "
            "an external action."
        )

    if remember_guidance:
        if learning_store is None or tenant_id is None:
            st.session_state.cg_operator_error = "Guidance was not stored because local operator learning is unavailable."
        else:
            source_record_id = f"chat-{uuid4().hex[:20]}"
            try:
                record = _append_operator_guidance(
                    learning_store,
                    tenant_id,
                    guidance=question.strip()[:4_000],
                    case_ids=answer.case_ids,
                    origin=GuidanceOrigin.CHAT,
                    source_record_id=source_record_id,
                )
                st.session_state.cg_operator_notice = (
                    f"Remembered explicit company guidance {record.guidance_id}."
                )
            except (OperatorLearningStoreError, ValidationError, TypeError, ValueError):
                st.session_state.cg_operator_error = (
                    "The guidance was not stored. Check its length and local database."
                )

    history.extend(
        [
            {"role": "user", "content": question.strip()},
            {
                "role": "assistant",
                "content": response_text[:6_000],
                "case_ids": answer.case_ids,
                "evidence_event_ids": answer.evidence_event_ids,
                "rule_ids": answer.rule_ids,
                "guidance_ids": guidance_ids,
                "correction_ids": correction_ids,
                "suggested_followups": answer.suggested_followups,
                "abstained": answer.abstained and not learned_support,
            },
        ]
    )
    st.session_state.cg_chat_history = history[-12:]


def _render_chat_correction_proposal(
    learning_store: OperatorLearningStore | None,
    tenant_id: str | None,
    dashboard_entries: list[
        tuple[Scenario, ActionRequest, DecisionRecord, DecisionCorrection | None]
    ],
) -> None:
    pending = st.session_state.get("cg_pending_chat_correction")
    if not pending:
        return
    entry = next(
        (row for row in dashboard_entries if row[0].case_id == pending["case_id"]),
        None,
    )
    if entry is None:
        st.error("The proposed correction case is no longer available.")
        return
    scenario, _request, decision, active_correction = entry
    st.warning(
        f"Pending human correction · case {scenario.case_id} · original "
        f"{decision.decision.value}. Confirmation is required."
    )
    if active_correction is not None:
        st.info(
            f"Case {scenario.case_id} already has active correction "
            f"{active_correction.correction_id} → "
            f"{active_correction.corrected_outcome.value}. Retract that correction "
            "from its resolved queue entry before replacing it."
        )
        if st.button("Dismiss chat correction proposal"):
            st.session_state.cg_pending_chat_correction = None
            st.rerun()
        return
    correction_options = [
        outcome.value for outcome in EnforcementDecision if outcome != decision.decision
    ]
    proposed_outcome = pending.get("proposed_outcome")
    proposed_index = (
        correction_options.index(proposed_outcome)
        if proposed_outcome in correction_options
        else 0
    )
    with st.form("chat_decision_correction_form"):
        chat_corrected_outcome = st.selectbox(
            f"Chat correction outcome for {scenario.case_id}",
            options=correction_options,
            index=proposed_index,
        )
        chat_correction_reviewer = st.text_input(
            "Chat correction reviewer",
            value="local-reviewer",
        )
        chat_correction_rationale = st.text_area(
            "Chat correction rationale",
            value=pending["rationale"],
        )
        chat_remember_similar = st.checkbox(
            "Remember chat correction for similar future cases",
            value=True,
        )
        chat_correction_submitted = st.form_submit_button(
            "Confirm chat correction",
            width="stretch",
        )
    st.caption(
        "Confirmation appends a local correction receipt only. No external action "
        "is executed, and the original deterministic receipt remains available."
    )
    if chat_correction_submitted:
        if learning_store is None or tenant_id is None:
            st.error("Operator learning is unavailable; no correction was stored.")
            return
        try:
            correction, guidance_saved = _save_decision_correction(
                learning_store,
                tenant_id,
                scenario=scenario,
                decision=decision,
                corrected_outcome=EnforcementDecision(chat_corrected_outcome),
                reviewer=chat_correction_reviewer,
                rationale=chat_correction_rationale,
                remember_similar=chat_remember_similar,
            )
        except (OperatorLearningStoreError, ValidationError, TypeError, ValueError):
            st.error(
                "The correction was not stored. Check the reviewer, rationale, and "
                "local learning database."
            )
        else:
            st.session_state.cg_pending_chat_correction = None
            st.session_state.cg_operator_notice = (
                f"Correction {correction.correction_id} saved: original "
                f"{decision.decision.value} → effective "
                f"{correction.corrected_outcome.value}."
            )
            if not guidance_saved:
                st.session_state.cg_operator_error = (
                    "The exact correction was saved, but similar-case guidance could "
                    "not be stored."
                )
            st.rerun()


def render_contextgate_chat(
    learning_store: OperatorLearningStore | None,
    tenant_id: str | None,
    dashboard_entries: list[
        tuple[Scenario, ActionRequest, DecisionRecord, DecisionCorrection | None]
    ],
) -> None:
    """Render the always-visible, evidence-grounded and teachable chat."""

    with st.container(key="dashboard_chat_panel", border=True):
        st.subheader("Ask ContextGate")
        st.caption(
            "Ask why items are red, inspect evidence, or propose a correction. "
            "Operator learning is explicit, tenant-scoped, traceable, and reversible."
        )
        st.caption(
            "Learned guidance shapes chat only. Exact human correction receipts can "
            "change the resolved dashboard view, but never rewrite evidence, weaken "
            "the deterministic gate, or execute an action."
        )
        st.caption(
            "Local learning is stored in plaintext SQLite. Do not paste passwords or "
            "tokens; a company deployment must bind the workspace to authenticated "
            "tenant access, encryption, and retention controls."
        )

        operator_notice = st.session_state.pop("cg_operator_notice", None)
        operator_error = st.session_state.pop("cg_operator_error", None)
        if operator_notice:
            st.success(operator_notice)
        if operator_error:
            st.warning(operator_error)

        latest_assistant = next(
            (
                message
                for message in reversed(st.session_state.cg_chat_history)
                if message["role"] == "assistant"
            ),
            None,
        )
        suggestions = (
            latest_assistant.get("suggested_followups", [])
            if latest_assistant is not None
            else []
        )
        if suggestions:
            suggestion_columns = st.columns(len(suggestions))
            chosen_suggestion = None
            for index, (column, suggestion) in enumerate(
                zip(suggestion_columns, suggestions, strict=True)
            ):
                if column.button(
                    suggestion,
                    key=f"chat_suggestion_{index}",
                    width="stretch",
                ):
                    chosen_suggestion = suggestion
            if chosen_suggestion:
                ask_contextgate(
                    chosen_suggestion,
                    learning_store=learning_store,
                    tenant_id=tenant_id,
                )
                st.rerun()

        for message in st.session_state.cg_chat_history:
            avatar = "🛡️" if message["role"] == "assistant" else "👤"
            with st.chat_message(message["role"], avatar=avatar):
                if message.get("abstained"):
                    st.warning(message["content"])
                else:
                    st.write(message["content"])
                citations = []
                if message.get("case_ids"):
                    citations.append(f"cases {', '.join(message['case_ids'])}")
                if message.get("evidence_event_ids"):
                    citations.append(
                        f"events {', '.join(message['evidence_event_ids'])}"
                    )
                if message.get("rule_ids"):
                    citations.append(f"rules {', '.join(message['rule_ids'])}")
                if message.get("guidance_ids"):
                    citations.append(
                        f"operator guidance {', '.join(message['guidance_ids'])}"
                    )
                if message.get("correction_ids"):
                    citations.append(
                        f"corrections {', '.join(message['correction_ids'])}"
                    )
                if citations:
                    st.caption("Grounded in · " + " · ".join(citations))

        with st.form("contextgate_chat", clear_on_submit=True):
            chat_question = st.text_input(
                "Ask for details, patterns, comparisons, evidence, or a safe next step",
                placeholder="Why are there so many red items?",
            )
            remember_chat_guidance = st.checkbox(
                "Remember this as company guidance",
                value=False,
                help=(
                    "Unchecked questions remain bounded chat history only. Check this "
                    "only when your message is a deliberate company instruction."
                ),
            )
            chat_submitted = st.form_submit_button(
                "Ask ContextGate", type="primary", width="stretch"
            )
        if chat_submitted:
            if chat_question.strip():
                ask_contextgate(
                    chat_question,
                    learning_store=learning_store,
                    tenant_id=tenant_id,
                    remember_guidance=remember_chat_guidance,
                )
                st.rerun()
            else:
                st.warning("Enter a question first.")

        _render_chat_correction_proposal(
            learning_store,
            tenant_id,
            dashboard_entries,
        )

        if learning_store is not None and tenant_id is not None:
            try:
                active_guidance = learning_store.list_active_guidance(
                    tenant_id,
                    limit=25,
                )
            except OperatorLearningStoreError:
                st.warning("Learned guidance could not be listed safely.")
            else:
                with st.expander(
                    f"Learned operator guidance · {len(active_guidance)} active"
                ):
                    st.caption(
                        "Guidance is advisory and never changes an enforcement result. "
                        f"Local workspace: {tenant_id}."
                    )
                    if not active_guidance:
                        st.info("No operator guidance has been saved yet.")
                    for guidance in active_guidance:
                        st.write(guidance.guidance)
                        st.caption(
                            f"{guidance.origin.value} · {guidance.guidance_id} · "
                            f"cases {', '.join(guidance.case_ids) or 'general'} · "
                            f"{guidance.created_at.isoformat()}"
                        )
                        if st.button(
                            f"Retract guidance {guidance.guidance_id[-8:]}",
                            key=f"retract-guidance-{guidance.guidance_id}",
                        ):
                            try:
                                learning_store.append_retraction(
                                    GuidanceRetraction(
                                        tenant_id=tenant_id,
                                        retraction_id=(
                                            f"retraction-{uuid4().hex[:20]}"
                                        ),
                                        guidance_id=guidance.guidance_id,
                                        retracted_at=datetime.now(UTC),
                                        actor="local-reviewer",
                                        reason=(
                                            "Operator retracted guidance from the "
                                            "dashboard learning panel."
                                        ),
                                    )
                                )
                            except (
                                OperatorLearningStoreError,
                                ValidationError,
                                TypeError,
                                ValueError,
                            ):
                                st.error("The guidance retraction was not stored.")
                            else:
                                st.session_state.cg_operator_notice = (
                                    f"Guidance {guidance.guidance_id} was retracted; "
                                    "its original record remains in history."
                                )
                                st.rerun()

        if latest_assistant is not None:
            st.iframe(
                browser_speaker_html(latest_assistant["content"]),
                height=62,
            )


def _render_decision_correction_controls(
    learning_store: OperatorLearningStore | None,
    tenant_id: str | None,
    scenario: Scenario,
    decision: DecisionRecord,
    active_correction: DecisionCorrection | None,
) -> None:
    st.divider()
    st.markdown("**Fix a mistaken decision**")
    st.caption(f"Original deterministic outcome: {decision.decision.value}")
    if learning_store is None or tenant_id is None:
        st.warning("Operator learning is unavailable; correction is disabled.")
        return
    if active_correction is not None:
        st.success(
            f"Human-corrected: original {decision.decision.value} → effective "
            f"{active_correction.corrected_outcome.value}"
        )
        st.write(active_correction.rationale)
        st.caption(
            f"Correction {active_correction.correction_id} · reviewer "
            f"{active_correction.reviewer} · {active_correction.created_at.isoformat()}"
        )
        if st.button(
            f"Retract correction for {scenario.case_id}",
            key=f"retract-correction-{scenario.name}",
            width="stretch",
        ):
            try:
                guidance_retracted = _retract_decision_correction(
                    learning_store,
                    tenant_id,
                    active_correction,
                )
            except (
                OperatorLearningStoreError,
                ValidationError,
                TypeError,
                ValueError,
            ):
                st.error("The correction retraction was not stored.")
            else:
                st.session_state.cg_operator_notice = (
                    f"Correction for {scenario.case_id} was retracted. The original "
                    f"{decision.decision.value} outcome is effective again."
                )
                if not guidance_retracted:
                    st.session_state.cg_operator_error = (
                        "The correction was retracted, but its similar-case guidance "
                        "needs administrator review."
                    )
                st.rerun()
    else:
        correction_options = [
            outcome.value
            for outcome in EnforcementDecision
            if outcome != decision.decision
        ]
        with st.form(f"decision_correction_{scenario.name}"):
            corrected_outcome = st.selectbox(
                f"Corrected outcome for {scenario.case_id}",
                options=correction_options,
            )
            correction_reviewer = st.text_input(
                f"Correction reviewer for {scenario.case_id}",
                value="local-reviewer",
            )
            correction_rationale = st.text_area(
                f"Correction rationale for {scenario.case_id}",
                placeholder=(
                    "Explain what the original decision misunderstood and cite the "
                    "stronger confirmation."
                ),
            )
            remember_similar = st.checkbox(
                "Remember correction for similar future cases",
                value=True,
                key=f"remember-correction-{scenario.name}",
            )
            correction_submitted = st.form_submit_button(
                f"Save correction for {scenario.case_id}",
                width="stretch",
            )
        st.caption(
            "This adds a resolved-view correction and optional advisory lesson. It "
            "does not rewrite the original receipt or execute an external action."
        )
        if correction_submitted:
            try:
                correction, guidance_saved = _save_decision_correction(
                    learning_store,
                    tenant_id,
                    scenario=scenario,
                    decision=decision,
                    corrected_outcome=EnforcementDecision(corrected_outcome),
                    reviewer=correction_reviewer,
                    rationale=correction_rationale,
                    remember_similar=remember_similar,
                )
            except (
                OperatorLearningStoreError,
                ValidationError,
                TypeError,
                ValueError,
            ):
                st.error(
                    "The correction was not stored. Add a reviewer and a specific "
                    "rationale, then try again."
                )
            else:
                st.session_state.cg_operator_notice = (
                    f"Correction {correction.correction_id} saved: original "
                    f"{decision.decision.value} → effective "
                    f"{correction.corrected_outcome.value}."
                )
                if not guidance_saved:
                    st.session_state.cg_operator_error = (
                        "The exact correction was saved, but similar-case guidance "
                        "could not be stored."
                    )
                st.rerun()


def _learn_from_review(
    store: OperatorLearningStore,
    tenant_id: str,
    *,
    scenario: Scenario,
    decision: DecisionRecord,
    review: ReviewEvent,
) -> tuple[DecisionCorrection | None, bool]:
    """Persist an explicit review response as guidance and, if changed, resolution."""

    corrected_outcome = {
        ReviewAction.HOLD: EnforcementDecision.REVIEW,
        ReviewAction.REJECT: EnforcementDecision.BLOCK,
        ReviewAction.APPROVE_OVERRIDE: EnforcementDecision.ALLOW,
    }[review.action]
    correction = None
    if corrected_outcome != decision.decision:
        correction, _ = _save_decision_correction(
            store,
            tenant_id,
            scenario=scenario,
            decision=decision,
            corrected_outcome=corrected_outcome,
            reviewer=review.reviewer,
            rationale=review.rationale,
            remember_similar=False,
            correction_id=f"correction-review-{review.review_id}",
            created_at=review.created_at,
        )

    guidance_saved = True
    try:
        _append_operator_guidance(
            store,
            tenant_id,
            guidance=(
                f"For case {scenario.case_id} ({scenario.title}), reviewer "
                f"{review.reviewer} chose {review.action.value}: {review.rationale}"
            ),
            case_ids=[scenario.case_id],
            origin=GuidanceOrigin.REVIEW,
            source_record_id=review.review_id,
            guidance_id=f"guidance-review-{review.review_id}",
            created_at=review.created_at,
        )
    except (OperatorLearningStoreError, ValidationError, TypeError, ValueError):
        guidance_saved = False
    return correction, guidance_saved


class WorkbenchInputError(ValueError):
    """A safe, non-secret-bearing workbench validation error."""


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise WorkbenchInputError("Duplicate JSON object keys are not allowed.")
        result[key] = value
    return result


def _reject_non_finite_json(_: str) -> None:
    raise WorkbenchInputError("Non-finite JSON numbers are not allowed.")


def _parse_workbench_json(raw: str, *, label: str) -> object:
    if len(raw.encode("utf-8")) > WORKBENCH_MAX_JSON_BYTES:
        raise WorkbenchInputError(
            f"{label} exceeds the {WORKBENCH_MAX_JSON_BYTES:,}-byte limit."
        )
    try:
        return json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_non_finite_json,
        )
    except WorkbenchInputError:
        raise
    except json.JSONDecodeError as exc:
        raise WorkbenchInputError(
            f"{label} is not valid JSON (line {exc.lineno}, column {exc.colno})."
        ) from exc


def _safe_pydantic_errors(label: str, exc: ValidationError) -> str:
    details: list[str] = []
    for error in exc.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    )[:8]:
        location = ".".join(str(item) for item in error["loc"]) or label
        details.append(f"{location}: {error['msg']}")
    suffix = "; additional errors omitted" if exc.error_count() > 8 else ""
    return f"{label} failed schema validation: {'; '.join(details)}{suffix}"


def _validate_workbench_inputs(
    events_raw: str, request_raw: str
) -> tuple[list[ContextEvent], ActionRequest]:
    events_payload = _parse_workbench_json(events_raw, label="Events JSON")
    request_payload = _parse_workbench_json(request_raw, label="Request JSON")
    if not isinstance(events_payload, list):
        raise WorkbenchInputError("Events JSON must be a JSON array.")
    if len(events_payload) > WORKBENCH_MAX_EVENTS:
        raise WorkbenchInputError(
            f"Events JSON exceeds the {WORKBENCH_MAX_EVENTS}-event limit."
        )
    if not isinstance(request_payload, dict):
        raise WorkbenchInputError("Request JSON must be a JSON object.")
    try:
        parsed_events = EVENT_LIST_ADAPTER.validate_json(
            json.dumps(events_payload, ensure_ascii=False, allow_nan=False),
            strict=True,
        )
    except ValidationError as exc:
        raise WorkbenchInputError(_safe_pydantic_errors("Events JSON", exc)) from exc
    try:
        parsed_request = ActionRequest.model_validate_json(
            json.dumps(request_payload, ensure_ascii=False, allow_nan=False),
            strict=True,
        )
    except ValidationError as exc:
        raise WorkbenchInputError(_safe_pydantic_errors("Request JSON", exc)) from exc
    return (
        [normalize_event(event) for event in parsed_events],
        normalize_request(parsed_request),
    )


def _validate_semantic_profile(raw: str) -> CategorySemanticConfig:
    if len(raw.encode("utf-8")) > SEMANTIC_PROFILE_MAX_BYTES:
        raise WorkbenchInputError(
            "Important-details profile exceeds the 16,384-byte limit."
        )
    payload = _parse_workbench_json(raw, label="Important-details profile")
    if not isinstance(payload, dict):
        raise WorkbenchInputError("Important-details profile must be a JSON object.")
    try:
        return CategorySemanticConfig.model_validate(payload)
    except ValidationError as exc:
        raise WorkbenchInputError(
            _safe_pydantic_errors("Important-details profile", exc)
        ) from exc


def _semantic_original(
    result: SemanticUpdateProposal | CorrectedSemanticUpdateProposal,
) -> SemanticUpdateProposal:
    return (
        result.original_proposal
        if isinstance(result, CorrectedSemanticUpdateProposal)
        else result
    )


def _semantic_local_answer(
    question: str,
    result: SemanticUpdateProposal | CorrectedSemanticUpdateProposal,
) -> str:
    original = _semantic_original(result)
    trace = result.calculation_trace
    if question == "How did you get that total?":
        contribution_labels = []
        for contribution in trace.contributions:
            reference = contribution.evidence_reference or "reference not supplied"
            contribution_labels.append(
                f"{contribution.mode.value} {contribution.stated_quantity} from "
                f"{contribution.evidence_id} ({reference})"
            )
        return (
            f"Formula: {trace.formula}. "
            f"Derivation: {'; '.join(contribution_labels)}. "
            f"[evidence {original.evidence_id}; input {original.input_digest[:12]}]"
        )
    if question == "Is it the same event?":
        if result.identity_matched:
            identity = ", ".join(
                f"{key}={value}" for key, value in result.matched_identity.items()
            )
            return (
                f"Yes for this constrained check: every configured identity key "
                f"matched exactly after normalization ({identity}). "
                f"[state {original.state_digest[:12]}; config "
                f"{original.config_fingerprint[:12]}]"
            )
        return (
            "No verified identity match was established. Keep the candidate in "
            "REVIEW and confirm one exact entity before recalculating. "
            f"[evidence {original.evidence_id}; state {original.state_digest[:12]}]"
        )

    if result.outcome == ProposalOutcome.REVIEW:
        next_step = (
            result.human_questions[0]
            if result.human_questions
            else "Ask an authorized human to confirm a stronger exact source."
        )
        return (
            f"Safe next step: keep this in REVIEW. {next_step} No state or external "
            f"action should occur. [config {original.config_fingerprint[:12]}]"
        )
    return (
        "Safe next step: send this candidate through the company's normal evidence "
        "and approval policy. PROPOSE is not an update or execution. "
        f"[evidence {original.evidence_id}; config "
        f"{original.config_fingerprint[:12]}]"
    )


try:
    page_policy = get_active_policy()
except PolicyConfigError:
    st.error("ContextGate stopped safely because the company policy is unavailable.")
    st.caption(
        "Fix the file named by CONTEXTGATE_POLICY_PATH, then reload this page. "
        "Run `python -m context_gate policy` for a read-only validation check."
    )
    st.stop()

if "cg_scenario" not in st.session_state:
    st.session_state.cg_scenario = "conflict"
if "cg_decision" not in st.session_state:
    start_run("conflict", "run-local-conflict-baseline", policy=page_policy)
elif st.session_state.cg_decision.policy_fingerprint != page_policy.policy_fingerprint:
    start_run(st.session_state.cg_scenario, policy=page_policy)
    st.session_state.cg_workbench_decision = None
    st.session_state.cg_workbench_request = None
    st.session_state.cg_workbench_review = None
if "cg_chat_history" not in st.session_state:
    st.session_state.cg_chat_history = [
        {
            "role": "assistant",
            "content": (
                "I can investigate nine synthetic cases, compare outcomes, find "
                "shared patterns, explain evidence, and recommend the safest next step."
            ),
            "case_ids": [],
            "evidence_event_ids": [],
            "rule_ids": [],
            "guidance_ids": [],
            "correction_ids": [],
            "suggested_followups": [
                "Why are there so many red items?",
                "Which items need my attention?",
                "Compare case B1 with case A1.",
            ],
            "abstained": False,
        }
    ]
if "cg_pending_chat_correction" not in st.session_state:
    st.session_state.cg_pending_chat_correction = None
if "cg_semantic_proposal" not in st.session_state:
    st.session_state.cg_semantic_proposal = None
if "cg_semantic_corrected" not in st.session_state:
    st.session_state.cg_semantic_corrected = None
if "cg_semantic_config" not in st.session_state:
    st.session_state.cg_semantic_config = None
if "cg_semantic_state" not in st.session_state:
    st.session_state.cg_semantic_state = None

operator_store: OperatorLearningStore | None = None
operator_tenant_id: str | None = None
operator_learning_error: str | None = None
try:
    operator_tenant_id = _operator_tenant_id()
    operator_store = operator_learning_store(str(_company_memory_path()))
except (MemoryStoreError, OperatorLearningStoreError, OSError, ValueError):
    operator_learning_error = (
        "Operator learning is unavailable. Dashboard enforcement remains active, "
        "but guidance and corrections are disabled."
    )

st.markdown(
    '<div class="cg-kicker">Real-time context firewall</div>', unsafe_allow_html=True
)
st.title("ContextGate")
st.markdown(
    '<div class="cg-subtitle">Evidence and approval control for AI agents — deterministic even when the model is unavailable.</div>',
    unsafe_allow_html=True,
)
if page_policy.policy_fingerprint == DEFAULT_POLICY.policy_fingerprint:
    st.caption(
        "Pattern Lab baseline: 9 synthetic cases · 3 ALLOW · 3 REVIEW · "
        "3 BLOCK · no credentials required"
    )
else:
    st.caption(
        "Pattern Lab evaluated under the active company policy; observed outcomes "
        "may differ from the built-in baseline labels."
    )
st.markdown(
    """
    <div class="cg-label">Deployment architecture (local lab runs the deterministic Python path)</div>
    <div class="cg-pipeline">
      <span class="cg-stage">Kafka evidence</span><span class="cg-arrow">→</span>
      <span class="cg-stage">Flink authority</span><span class="cg-arrow">→</span>
      <span class="cg-stage">Deterministic gate</span><span class="cg-arrow">→</span>
      <span class="cg-stage">Streaming Agent explanation</span><span class="cg-arrow">→</span>
      <span class="cg-stage">Human receipt</span>
    </div>
    """,
    unsafe_allow_html=True,
)

scenario_options = [scenario.name for scenario in iter_scenarios()]
current_index = scenario_options.index(st.session_state.cg_scenario)

with st.sidebar:
    st.header("Proof scenarios")
    selected_scenario = st.selectbox(
        "Choose a live decision path",
        options=scenario_options,
        index=current_index,
        format_func=lambda name: (
            f"{get_scenario(name).case_id} · {get_scenario(name).title}"
        ),
    )
    if selected_scenario != st.session_state.cg_scenario:
        start_run(selected_scenario, policy=page_policy)
        st.rerun()
    selected_metadata = get_scenario(selected_scenario)
    st.caption(selected_metadata.description)
    st.caption(
        f"Case {selected_metadata.case_id} · Built-in baseline: "
        f"{selected_metadata.expected_classification.value} / "
        f"{selected_metadata.expected_decision.value}"
    )
    st.caption(
        f"Observed now: {st.session_state.cg_decision.classification.value} / "
        f"{st.session_state.cg_decision.decision.value}"
    )
    if st.button("Replay current stream", type="primary", width="stretch"):
        start_run(selected_scenario, policy=page_policy)
        st.rerun()
    st.divider()
    st.success("Local fallback active")
    st.caption("No key, cloud call, or external action is required.")

dashboard_entries = []
for dashboard_scenario in iter_scenarios():
    dashboard_events, dashboard_request = load_scenario(dashboard_scenario.name)
    dashboard_decision = evaluate_request(
        dashboard_events,
        dashboard_request,
        run_id=(
            f"dashboard-{dashboard_scenario.case_id.lower()}-"
            f"{page_policy.policy_fingerprint[:12]}"
        ),
        policy=page_policy,
    )
    active_dashboard_correction = None
    if operator_store is not None and operator_tenant_id is not None:
        try:
            active_dashboard_correction = (
                operator_store.latest_active_decision_correction(
                    operator_tenant_id,
                    case_id=dashboard_scenario.case_id,
                    request_fingerprint=dashboard_decision.request_digest,
                    evidence_fingerprint=dashboard_decision.evidence_digest,
                    policy_fingerprint=dashboard_decision.policy_fingerprint,
                )
            )
        except OperatorLearningStoreError:
            operator_learning_error = (
                "One or more human corrections could not be read safely. Those "
                "cases use their original deterministic outcomes."
            )
    dashboard_entries.append(
        (
            dashboard_scenario,
            dashboard_request,
            dashboard_decision,
            active_dashboard_correction,
        )
    )

dashboard_counts = {
    outcome: sum(
        (
            entry_correction.corrected_outcome
            if entry_correction is not None
            else entry_decision.decision
        )
        == outcome
        for _, _, entry_decision, entry_correction in dashboard_entries
    )
    for outcome in EnforcementDecision
}
original_dashboard_counts = {
    outcome: sum(
        entry_decision.decision == outcome
        for _, _, entry_decision, _ in dashboard_entries
    )
    for outcome in EnforcementDecision
}
active_correction_count = sum(
    entry_correction is not None for _, _, _, entry_correction in dashboard_entries
)

st.subheader("Decision dashboard")
st.caption(
    "Live gate outcomes under the active company policy — these are safety "
    "decisions, not automated-test results. Open any case to inspect its evidence "
    "and deterministic receipt. Exact human corrections appear in this resolved "
    "view without rewriting the originals."
)
if operator_learning_error:
    st.warning(operator_learning_error)
dashboard_metric_columns = st.columns(4)
with dashboard_metric_columns[0], st.container(key="dashboard_total_card"):
    st.metric("Total evaluated", len(dashboard_entries))
    st.caption("All active-policy decisions")
with dashboard_metric_columns[1], st.container(key="dashboard_allow_card"):
    st.metric("Passed gate", dashboard_counts[EnforcementDecision.ALLOW])
    st.caption("✓ ALLOW · safe preview")
with dashboard_metric_columns[2], st.container(key="dashboard_block_card"):
    st.metric("Blocked", dashboard_counts[EnforcementDecision.BLOCK])
    st.caption("× BLOCK · stopped")
with dashboard_metric_columns[3], st.container(key="dashboard_review_card"):
    st.metric("Needs my attention", dashboard_counts[EnforcementDecision.REVIEW])
    st.caption("! REVIEW · human decision")
if active_correction_count:
    st.info(
        f"Resolved view applies {active_correction_count} active human correction(s). "
        "Original deterministic totals remain "
        f"{original_dashboard_counts[EnforcementDecision.ALLOW]} ALLOW / "
        f"{original_dashboard_counts[EnforcementDecision.REVIEW]} REVIEW / "
        f"{original_dashboard_counts[EnforcementDecision.BLOCK]} BLOCK."
    )
else:
    st.caption(
        "No active human corrections · resolved totals currently match the original "
        "deterministic receipts."
    )

selected_dashboard_queue = st.selectbox(
    "Dashboard queue",
    options=["REVIEW", "ALLOW", "BLOCK"],
    help="REVIEW is selected first so items needing a person are never buried.",
)
st.caption("REVIEW = needs my attention · ALLOW = passed gate · BLOCK = stopped")

visible_dashboard_entries = [
    entry
    for entry in dashboard_entries
    if (entry[3].corrected_outcome if entry[3] is not None else entry[2].decision).value
    == selected_dashboard_queue
]
if not visible_dashboard_entries:
    st.info("This queue has no cases under the active policy.")
else:
    for (
        entry_scenario,
        entry_request,
        entry_decision,
        entry_correction,
    ) in visible_dashboard_entries:
        effective_decision = (
            entry_correction.corrected_outcome
            if entry_correction is not None
            else entry_decision.decision
        )
        status_symbol = {
            EnforcementDecision.ALLOW: "✓",
            EnforcementDecision.REVIEW: "!",
            EnforcementDecision.BLOCK: "×",
        }[effective_decision]
        status_description = {
            EnforcementDecision.ALLOW: "Passed gate",
            EnforcementDecision.REVIEW: "Needs attention",
            EnforcementDecision.BLOCK: "Stopped",
        }[effective_decision]
        status_class = effective_decision.value.casefold()
        current_marker = (
            " · CURRENT" if entry_scenario.name == st.session_state.cg_scenario else ""
        )
        correction_marker = " · HUMAN-CORRECTED" if entry_correction else ""
        with st.expander(
            f"{status_symbol} {entry_scenario.case_id} · {entry_scenario.title} · "
            f"{effective_decision.value} · {status_description}{correction_marker}"
            f"{current_marker}"
        ):
            st.markdown(
                f'<div class="cg-status-strip {status_class}">'
                f"{status_symbol} {effective_decision.value} · "
                f"{status_description}</div>",
                unsafe_allow_html=True,
            )
            st.write(entry_scenario.description)
            detail_columns = st.columns(3)
            detail_columns[0].markdown(
                f"**Classification**  \n{entry_decision.classification.value}"
            )
            detail_columns[1].markdown(f"**Risk**  \n{entry_decision.risk.value}")
            detail_columns[2].markdown(
                f"**Review**  \n{entry_decision.review_status.value}"
            )
            st.markdown(
                f"**Request:** `{escaped(entry_request.action_type)}` → "
                f"`{escaped(entry_request.requested_value)}`"
            )
            st.write(entry_decision.explanation)
            evidence_summary = (
                ", ".join(entry_decision.evidence_event_ids)
                if entry_decision.evidence_event_ids
                else "None accepted"
            )
            rules_summary = ", ".join(entry_decision.deterministic_rule_ids)
            st.caption(f"Evidence: {evidence_summary}")
            st.caption(f"Rules: {rules_summary}")
            st.caption(
                "Human approval required: "
                f"{'yes' if entry_decision.requires_human_approval else 'no'} · "
                f"Policy {entry_decision.policy_version} / "
                f"{entry_decision.policy_fingerprint[:12]}"
            )
            if st.button(
                f"Open {entry_scenario.case_id} details",
                key=f"dashboard-open-{entry_scenario.name}",
                width="stretch",
            ):
                start_run(entry_scenario.name, policy=page_policy)
                st.rerun()
            _render_decision_correction_controls(
                operator_store,
                operator_tenant_id,
                entry_scenario,
                entry_decision,
                entry_correction,
            )

render_contextgate_chat(
    operator_store,
    operator_tenant_id,
    dashboard_entries,
)
st.divider()

scenario = get_scenario(st.session_state.cg_scenario)
raw_events, raw_request = scenario.load()
events = [normalize_event(event) for event in raw_events]
request = normalize_request(raw_request)
decision = st.session_state.cg_decision
display_review_status = (
    st.session_state.cg_review.resulting_status
    if st.session_state.cg_review is not None
    else decision.review_status
)
authoritative = next(
    (
        event
        for event in events
        if event.event_id in decision.evidence_event_ids
        and decision.authoritative_value is not None
        and event.field_value.casefold() == decision.authoritative_value.casefold()
    ),
    None,
)
audit_entries, audit_verified, audit_read_error = read_audit_state()
audit_error = st.session_state.get("cg_audit_error") or audit_read_error

with st.sidebar:
    st.write("**Run ID**")
    st.code(decision.run_id)
    st.write("**Audit chain**")
    if audit_error:
        st.error("Audit unavailable")
        st.caption(audit_error)
    elif audit_verified:
        st.success("Verified")
    else:
        st.error("Integrity failure")
    st.caption(f"{len(audit_entries)} append-only record(s)")

m1, m2, m3, m4 = st.columns(4)
m1.metric("Classification", decision.classification.value)
m2.metric("Enforcement", decision.decision.value)
m3.metric("Risk", decision.risk.value)
m4.metric("Review", display_review_status.value)

banner_class, banner_title, banner_subtitle = BANNER_CONTENT[decision.decision]
authoritative_line = (
    f"Authoritative value remains <strong>{escaped(decision.authoritative_value)}</strong>."
    if decision.authoritative_value is not None
    else "No authoritative value was accepted."
)
st.markdown(
    f"""
    <div class="cg-banner {banner_class}">
      <h2>{escaped(banner_title)}</h2>
      <p>{escaped(banner_subtitle)}</p>
      <p><strong>{escaped(request.action_type)}</strong> requested <strong>{escaped(request.requested_value)}</strong>.</p>
      <p>{escaped(decision.explanation)}</p>
      <p>{authoritative_line}</p>
    </div>
    """,
    unsafe_allow_html=True,
)

st.subheader("Evidence received over time")
if not events:
    st.warning("No matching evidence was received.")
else:
    columns = st.columns(len(events))
    for column, event in zip(columns, events, strict=True):
        is_authoritative = (
            authoritative is not None and event.event_id == authoritative.event_id
        )
        is_incomplete = any(
            value in (None, "")
            for value in (
                event.source_name,
                event.source_type,
                event.observed_at,
                event.effective_at,
                event.content_hash,
            )
        ) or not (event.evidence_uri or event.evidence_reference)
        css_classes = ["cg-source"]
        if is_authoritative:
            css_classes.append("authoritative")
        elif is_incomplete:
            css_classes.append("incomplete")
        authority_badge = (
            "AUTHORITATIVE"
            if is_authoritative
            else "INCOMPLETE"
            if is_incomplete
            else "COMPETING"
        )
        observed = (
            event.observed_at.strftime("%b %d, %H:%M UTC")
            if event.observed_at is not None
            else "Missing timestamp"
        )
        with column:
            st.markdown(
                f"""
                <div class="{" ".join(css_classes)}">
                  <div class="cg-label">{authority_badge} · {escaped(event.event_id)}</div>
                  <div class="cg-value">{escaped(event.field_value)}</div>
                  <div><strong>{escaped(event.source_name, missing="Source name missing")}</strong></div>
                  <div style="color:#94a3b8;margin:.25rem 0 .7rem 0">Observed {escaped(observed)}</div>
                  <span class="cg-pill">policy rank {policy_for(event, page_policy).rank}</span>
                  <span class="cg-pill">trust {effective_trust(event, page_policy):.2f}</span>
                  <span class="cg-pill">{escaped(event.status.value)}</span>
                  <p style="color:#cbd5e1;margin-top:.85rem">{escaped(policy_for(event, page_policy).label)}</p>
                </div>
                """,
                unsafe_allow_html=True,
            )

if page_policy.policy_fingerprint == DEFAULT_POLICY.policy_fingerprint:
    st.info(SCENARIO_LESSONS[scenario.name])
else:
    st.info(
        f"The active company policy produced {decision.classification.value} / "
        f"{decision.decision.value}. Use the receipt's rule IDs and policy "
        "fingerprint for review; built-in baseline lessons are intentionally hidden."
    )

with st.expander("Bring your own evidence · document and image intake"):
    st.caption(
        "Files are inspected in memory and are not saved by ContextGate. Extraction "
        "creates an untrusted candidate—not an authenticated fact."
    )
    uploaded_artifact = st.file_uploader(
        "Upload text, email, JSON, CSV, HTML, XML, PDF, DOCX, screenshot, or photo",
        type=[
            "txt",
            "md",
            "csv",
            "json",
            "html",
            "xml",
            "eml",
            "pdf",
            "docx",
            "png",
            "jpg",
            "jpeg",
            "webp",
            "gif",
        ],
        help="Maximum 10 MiB. Images and scanned PDFs require an explicit OCR step.",
    )
    if uploaded_artifact is None:
        st.info(
            "Try a synthetic note or screenshot. Images receive a hash receipt and "
            "an honest OCR_REQUIRED status; no text is guessed."
        )
    else:
        try:
            artifact_size = uploaded_artifact.size
            if not isinstance(artifact_size, int) or artifact_size < 0:
                raise ArtifactIntakeError("Artifact size metadata is invalid.")
            if artifact_size > MAX_ARTIFACT_BYTES:
                raise ArtifactIntakeError(
                    f"Artifact exceeds the {MAX_ARTIFACT_BYTES // (1024 * 1024)} MiB limit."
                )
            artifact_bytes = uploaded_artifact.getvalue()
            artifact_receipt = ingest_artifact(
                uploaded_artifact.name,
                uploaded_artifact.type,
                artifact_bytes,
            )
        except ArtifactIntakeError as exc:
            st.error(f"Artifact rejected safely: {exc}")
        else:
            if st.session_state.get("cg_upload_digest") != artifact_receipt.sha256:
                st.session_state.cg_upload_digest = artifact_receipt.sha256
                st.session_state.cg_upload_candidate = None

            i1, i2, i3 = st.columns(3)
            i1.metric("Intake status", artifact_receipt.status.value)
            i2.metric("Bytes", f"{artifact_receipt.size_bytes:,}")
            i3.metric("Extracted chars", f"{artifact_receipt.extracted_chars:,}")
            st.code(f"sha256:{artifact_receipt.sha256}")
            st.caption(
                f"{artifact_receipt.safe_filename} · {artifact_receipt.content_type} · "
                f"{artifact_receipt.extractor} · {artifact_receipt.message}"
            )

            if artifact_receipt.content_type.startswith("image/"):
                try:
                    st.image(
                        artifact_bytes,
                        caption=f"Local preview · {artifact_receipt.safe_filename}",
                    )
                except (OSError, RuntimeError, TypeError, ValueError):
                    st.warning(
                        "The image bytes were retained in memory for their hash receipt, "
                        "but a safe local preview could not be rendered."
                    )
            if artifact_receipt.status == ArtifactStatus.EXTRACTED:
                st.text_area(
                    "Locally extracted text",
                    value=artifact_receipt.extracted_text or "",
                    height=160,
                    disabled=True,
                )
            elif artifact_receipt.status == ArtifactStatus.OCR_REQUIRED:
                st.warning(
                    "No text was inferred. Use trusted OCR/vision upstream or enter "
                    "the visible claim explicitly below."
                )
            else:
                st.warning(artifact_receipt.message)

            st.write("**Create an unverified Kafka-ready candidate**")
            with st.form("artifact_candidate"):
                c1, c2 = st.columns(2)
                candidate_entity = c1.text_input("Entity ID", value=request.entity_id)
                candidate_field = c2.text_input("Field name", value=request.field_name)
                candidate_value = st.text_input(
                    "Claim value (explicitly supplied by you)",
                    placeholder="Enter only the claim visible in the artifact",
                )
                candidate_source = st.text_input(
                    "Source label", value="User-supplied artifact"
                )
                c3, c4 = st.columns(2)
                candidate_effective = c3.text_input(
                    "Effective time (RFC3339)",
                    value=(request.requested_effective_at or request.created_at)
                    .isoformat()
                    .replace("+00:00", "Z"),
                )
                candidate_sensitivity = c4.selectbox(
                    "Sensitivity",
                    options=[item.value for item in Sensitivity],
                )
                candidate_submitted = st.form_submit_button(
                    "Create unverified candidate", width="stretch"
                )

            if candidate_submitted:
                try:
                    if not candidate_value.strip():
                        raise ValueError("claim value is required")
                    effective_at = datetime.fromisoformat(candidate_effective)
                    if effective_at.tzinfo is None or effective_at.utcoffset() is None:
                        raise ValueError("effective time must include a timezone")
                    candidate = create_context_event_candidate(
                        artifact_receipt,
                        event_id=f"evt-upload-{artifact_receipt.sha256[:12]}",
                        entity_id=candidate_entity,
                        field_name=candidate_field,
                        claim_value=candidate_value,
                        source_name=candidate_source,
                        source_type="uploaded_document",
                        trust_score=0.0,
                        observed_at=datetime.now(UTC),
                        effective_at=effective_at,
                        sensitivity=Sensitivity(candidate_sensitivity),
                    )
                    st.session_state.cg_upload_candidate = candidate
                except ValueError as exc:
                    st.error(f"Candidate was not created: {exc}")

            candidate = st.session_state.get("cg_upload_candidate")
            if candidate is not None:
                st.warning(
                    "Candidate created as UNVERIFIED with unknown policy authority. "
                    "It has not been published or added to the current decision."
                )
                st.json(candidate.model_dump(mode="json"), expanded=False)
                st.download_button(
                    "Download candidate event JSON",
                    data=candidate.model_dump_json(indent=2),
                    file_name=f"{candidate.event_id}.json",
                    mime="application/json",
                )

with st.expander("Company Workbench · evaluate your own structured context"):
    st.caption(
        "Paste schema-valid ContextEvent[] and ActionRequest JSON to run the same "
        "deterministic gate under this installation's active company policy. "
        "Raw JSON stays in the current app session and is never published or executed. "
        "Each resulting decision receipt is appended automatically to plaintext "
        "runtime/audit.jsonl and can contain the evaluated company values."
    )
    st.caption(
        "Use only authorized or synthetic data; protect or clear runtime under your "
        "retention policy, and never paste passwords or tokens."
    )
    st.caption(
        f"Limits: {WORKBENCH_MAX_EVENTS} events and "
        f"{WORKBENCH_MAX_JSON_BYTES // 1024} KiB per JSON document."
    )
    workbench_policy = page_policy
    st.write(f"**Active policy:** `{workbench_policy.policy_version}`")
    st.code(workbench_policy.policy_fingerprint, language=None)

    default_events_json = json.dumps(
        [event.model_dump(mode="json") for event in events], indent=2
    )
    default_request_json = json.dumps(request.model_dump(mode="json"), indent=2)
    with st.form("company_workbench_form"):
        workbench_events_json = st.text_area(
            "ContextEvent[] JSON",
            value=default_events_json,
            height=230,
            max_chars=WORKBENCH_MAX_JSON_BYTES,
            key="company_workbench_events",
        )
        workbench_request_json = st.text_area(
            "ActionRequest JSON",
            value=default_request_json,
            height=230,
            max_chars=WORKBENCH_MAX_JSON_BYTES,
            key="company_workbench_request",
        )
        workbench_submitted = st.form_submit_button(
            "Evaluate safely",
            type="primary",
            width="stretch",
            disabled=workbench_policy is None,
        )

    if workbench_submitted:
        st.session_state.cg_workbench_decision = None
        st.session_state.cg_workbench_request = None
        st.session_state.cg_workbench_review = None
        try:
            workbench_events, workbench_request = _validate_workbench_inputs(
                workbench_events_json,
                workbench_request_json,
            )
            workbench_decision = evaluate_request(
                workbench_events,
                workbench_request,
                run_id=f"run-workbench-{uuid4().hex[:10]}",
                policy=workbench_policy,
            )
        except WorkbenchInputError as exc:
            st.error(str(exc))
        except PolicyConfigError:
            st.error(
                "The active company policy became unavailable. No decision was made."
            )
        except (TypeError, ValueError):
            st.error("Evaluation failed safely. Check the schema and try again.")
        else:
            st.session_state.cg_workbench_decision = workbench_decision
            st.session_state.cg_workbench_request = workbench_request
            try:
                audit_log().append_decision(workbench_decision)
                st.session_state.cg_audit_error = None
            except (OSError, ValueError) as exc:
                st.session_state.cg_audit_error = f"{type(exc).__name__}: {exc}"
            st.rerun()

    workbench_decision = st.session_state.get("cg_workbench_decision")
    if workbench_decision is not None:
        outcome = (
            st.success
            if workbench_decision.decision == EnforcementDecision.ALLOW
            else st.warning
            if workbench_decision.decision == EnforcementDecision.REVIEW
            else st.error
        )
        outcome(
            f"{workbench_decision.classification.value} / "
            f"{workbench_decision.decision.value} · "
            f"risk {workbench_decision.risk.value}"
        )
        st.write(f"**Policy version:** `{workbench_decision.policy_version}`")
        st.write("**Policy fingerprint:**")
        st.code(workbench_decision.policy_fingerprint, language=None)
        st.caption(
            "The decision receipt—not the supplied event documents—was appended to "
            "the local audit log. No external action was attempted."
        )
        st.json(workbench_decision.model_dump(mode="json"), expanded=False)
        st.download_button(
            "Download company workbench receipt",
            data=workbench_decision.model_dump_json(indent=2),
            file_name=f"{workbench_decision.decision_id}.json",
            mime="application/json",
            key="company_workbench_download",
        )
        workbench_request = st.session_state.get("cg_workbench_request")
        workbench_review = st.session_state.get("cg_workbench_review")
        if workbench_decision.requires_human_approval and workbench_request is not None:
            st.write("**Human review for this company decision**")
            if workbench_review is None:
                with st.form("company_workbench_review_form"):
                    wb_reviewer = st.text_input(
                        "Company reviewer", value="local-reviewer"
                    )
                    wb_rationale = st.text_area(
                        "Company review rationale",
                        value="Verify the evidence before any external action.",
                    )
                    wb_choice = st.selectbox(
                        "Company review disposition",
                        options=[item.value for item in ReviewAction],
                    )
                    wb_review_submitted = st.form_submit_button(
                        "Record company review receipt", width="stretch"
                    )
                if wb_review_submitted:
                    try:
                        candidate_review = record_review(
                            workbench_decision,
                            workbench_request,
                            action=ReviewAction(wb_choice),
                            reviewer=wb_reviewer,
                            rationale=wb_rationale,
                        )
                        audit_entry = audit_log().append_review(candidate_review)
                        stored_review = ReviewEvent.model_validate(audit_entry.payload)
                    except (OSError, ValueError) as exc:
                        st.error(f"Company review was not recorded: {exc}")
                    else:
                        st.session_state.cg_workbench_review = stored_review
                        st.rerun()
            else:
                st.success(
                    f"Company review recorded: {workbench_review.resulting_status.value}"
                )
                st.caption(
                    "The review records human intent only; ContextGate did not execute "
                    "the requested action."
                )
                st.download_button(
                    "Download company review receipt",
                    data=workbench_review.model_dump_json(indent=2),
                    file_name=f"{workbench_review.review_id}.json",
                    mime="application/json",
                    key="company_workbench_review_download",
                )
        elif not workbench_decision.requires_human_approval:
            st.success("This local preview does not require a human review receipt.")

memory_trace_groups = []
memory_trace_tenant: str | None = None
with st.expander(
    "Company Memory · patterns, anomalies, and corrections",
    expanded=True,
):
    st.warning(
        "Company Memory is persistent local SQLite stored as plaintext. A company "
        "deployment must add authenticated access controls, tenant authorization, "
        "encryption at rest, backups, and an explicit retention/deletion policy."
    )
    st.caption(
        "This feature makes no OAuth connection, inbox read, network lookup, message, "
        "or external action. An authorized administrator—not this screen—must manage "
        "database reset and retention."
    )

    try:
        memory_db_path = _company_memory_path()
        memory_store = company_memory_store(str(memory_db_path))
    except (MemoryStoreError, OSError, ValueError):
        memory_store = None
        st.error(
            "Company Memory is unavailable, so its controls are disabled. "
            "The rest of ContextGate remains usable."
        )
        st.caption(
            "Check CONTEXTGATE_MEMORY_PATH and local file permissions; no supplied "
            "path or database content is echoed here."
        )

    if memory_store is not None:
        tenant_id = st.text_input(
            "Company memory tenant ID",
            value="example-company",
            key="cg_memory_tenant_id",
        ).strip()
        st.caption(
            "This field is a local test selector only. In production, authentication "
            "must bind the tenant ID to the signed-in user's authorized company; never "
            "trust form text as the security boundary."
        )

        try:
            memory_store.list_observations(tenant_id, limit=1)
        except ValueError:
            tenant_is_valid = False
            st.error(
                "Enter a non-empty, display-safe tenant ID of at most 128 characters."
            )
        except MemoryStoreError:
            tenant_is_valid = False
            st.error(
                "Company Memory could not read this tenant safely. Memory controls "
                "are disabled for this run; the rest of ContextGate remains usable."
            )
        else:
            tenant_is_valid = True

        if tenant_is_valid:
            if st.button(
                "Load fictional 3 + 8 event dataset",
                key="cg_memory_seed",
                help=(
                    "Explicitly adds three events at 35 Main St and eight at "
                    "76 New Avenue for this test tenant."
                ),
            ):
                try:
                    inserted, unchanged = _seed_fictional_company_memory(
                        memory_store,
                        tenant_id,
                    )
                except MemoryStoreError:
                    st.error(
                        "The fictional observations could not be stored safely; "
                        "nothing was looked up or acted on."
                    )
                else:
                    st.success(
                        f"Fictional dataset ready: {inserted} inserted, "
                        f"{unchanged} already present. Repeated loads are idempotent."
                    )

            try:
                memory_analyzer = PatternAnalyzer(memory_store, policy=page_policy)
                memory_summary = memory_analyzer.summarize_patterns(
                    tenant_id,
                    category="company_event",
                )
            except (MemoryStoreError, ValueError):
                memory_summary = None
                st.error(
                    "Company patterns could not be calculated safely. Memory is "
                    "disabled for this run; the main decision lab is still available."
                )

            if memory_summary is not None:
                memory_trace_tenant = tenant_id
                memory_trace_groups = [
                    (
                        (
                            f"{pattern.parent_attribute} "
                            f"'{pattern.parent_display_value}' → "
                            f"{pattern.child_attribute} "
                            f"'{pattern.child_display_value}'"
                        ),
                        pattern.count,
                        pattern.trusted_count,
                        pattern.contributors,
                    )
                    for pattern in memory_summary.conditional_patterns
                ]
                memory_trace_groups.extend(
                    (
                        f"{pattern.attribute_key} '{pattern.display_value}'",
                        pattern.count,
                        pattern.trusted_count,
                        pattern.contributors,
                    )
                    for pattern in memory_summary.attribute_patterns
                )

                st.write(
                    f"**Remembered event observations for this tenant:** "
                    f"{memory_summary.observation_count}"
                )
                st.write("**Generic attribute pattern counts**")
                if not memory_summary.attribute_patterns:
                    st.info(
                        "No event observations are remembered for this tenant. "
                        "Load the explicitly fictional dataset or assess and remember "
                        "a confirmed observation below."
                    )
                else:
                    for pattern in memory_summary.attribute_patterns:
                        st.write(
                            f"{pattern.attribute_key} · {pattern.display_value} · "
                            f"{pattern.count} observation(s) · "
                            f"{pattern.trusted_count} trusted"
                        )
                if memory_summary.history_truncated:
                    st.warning(
                        "The bounded history window was full; these counts do not "
                        "represent records outside that window."
                    )

                st.divider()
                st.write("**Assess a possible new observation**")
                st.caption(
                    "Assessment is advisory. ALLOW_LIKE is not an execution approval, "
                    "and REVIEW never performs a lookup or stores the candidate."
                )
                with st.form("company_memory_candidate_form"):
                    candidate_id = st.text_input(
                        "Candidate observation ID",
                        value="candidate-suite-354",
                    )
                    candidate_address = st.text_input(
                        "Candidate address",
                        value="76 New Avenue",
                    )
                    candidate_suite = st.text_input(
                        "Candidate suite",
                        value="354",
                    )
                    candidate_source = st.text_input(
                        "Candidate source type",
                        value="official_email",
                        help=(
                            "Use a source type configured by the active company policy."
                        ),
                    )
                    candidate_reference = st.text_input(
                        "Candidate evidence reference",
                        value="fictional://official-email/candidate-suite-354",
                    )
                    candidate_trust = st.number_input(
                        "Candidate trust score",
                        min_value=0.0,
                        max_value=1.0,
                        value=0.95,
                        step=0.05,
                    )
                    optional_detail_name = st.text_input(
                        "Optional important detail name",
                        value="",
                        help="For example: confirmed crowd size.",
                    )
                    optional_detail_value = st.text_input(
                        "Optional important detail value",
                        value="",
                    )
                    candidate_submitted = st.form_submit_button(
                        "Assess candidate against company memory",
                        type="primary",
                        width="stretch",
                    )

                if candidate_submitted:
                    if bool(optional_detail_name.strip()) != bool(
                        optional_detail_value.strip()
                    ):
                        st.error(
                            "Provide both the optional important-detail name and value, "
                            "or leave both empty."
                        )
                    else:
                        candidate_attributes = {
                            "address": candidate_address,
                            "suite": candidate_suite,
                        }
                        if optional_detail_name.strip():
                            candidate_attributes[optional_detail_name] = (
                                optional_detail_value
                            )
                        try:
                            candidate_observation = CompanyObservation(
                                tenant_id=tenant_id,
                                observation_id=candidate_id,
                                category="company_event",
                                occurred_at=datetime.now(UTC),
                                attributes=candidate_attributes,
                                source_type=candidate_source,
                                trust_score=float(candidate_trust),
                                status=EvidenceStatus.CONFIRMED,
                                sensitivity=Sensitivity.INTERNAL,
                                evidence_reference=candidate_reference,
                            )
                            candidate_assessment = memory_analyzer.assess_candidate(
                                candidate_observation
                            )
                        except ValidationError:
                            st.error(
                                "The candidate is invalid. Check its IDs, attributes, "
                                "source, trust score, and evidence reference."
                            )
                        except (MemoryStoreError, ValueError):
                            st.error(
                                "The candidate could not be assessed safely. No lookup, "
                                "storage, or action occurred."
                            )
                        else:
                            st.session_state.cg_memory_candidate = candidate_observation
                            st.session_state.cg_memory_assessment = candidate_assessment

                saved_candidate = st.session_state.get("cg_memory_candidate")
                saved_assessment = st.session_state.get("cg_memory_assessment")
                assessment_is_current = (
                    saved_candidate is not None
                    and saved_assessment is not None
                    and saved_candidate.tenant_id == tenant_id
                    and saved_assessment.tenant_id == tenant_id
                )
                if assessment_is_current:
                    if saved_assessment.outcome == PatternOutcome.REVIEW:
                        st.warning(f"REVIEW · {saved_assessment.summary}")
                    else:
                        st.success(
                            "ALLOW_LIKE (advisory only; not an execution approval) · "
                            f"{saved_assessment.summary}"
                        )

                    same_address_patterns = [
                        pattern
                        for pattern in memory_summary.conditional_patterns
                        if pattern.parent_display_value.casefold()
                        == saved_candidate.attributes["address"].casefold()
                    ]
                    if same_address_patterns:
                        dominant_suite = max(
                            same_address_patterns,
                            key=lambda pattern: (
                                pattern.trusted_count,
                                pattern.count,
                                pattern.child_display_value,
                            ),
                        )
                        if (
                            dominant_suite.child_display_value.casefold()
                            != saved_candidate.attributes["suite"].casefold()
                        ):
                            st.write(
                                "**Historical comparison:** The same address "
                                f"historically used Suite "
                                f"{dominant_suite.child_display_value} "
                                f"{dominant_suite.count} times; the candidate says "
                                f"Suite {saved_candidate.attributes['suite']}."
                            )

                    if saved_assessment.reasons:
                        st.write("**Why**")
                        for reason in saved_assessment.reasons:
                            st.write(f"{reason.code} · {reason.detail}")
                    st.write(
                        "**Recommended confirmation:** Check a stronger source allowed "
                        "by the company policy, retain its exact evidence reference, "
                        "and ask a human before treating the change as confirmed."
                    )
                    st.write("**Questions for a human**")
                    for question in saved_assessment.human_questions:
                        st.write(question)
                    st.caption(
                        "Candidate stored: no · automatic lookup performed: no · "
                        "external action executed: no"
                    )

                    if st.button(
                        "Remember this assessed observation",
                        key="cg_memory_remember_candidate",
                        help=(
                            "Explicitly appends the assessed candidate. Remembering a "
                            "REVIEW candidate does not approve or execute anything."
                        ),
                    ):
                        try:
                            remember_result = memory_store.upsert(saved_candidate)
                        except MemoryStoreError:
                            st.error(
                                "The observation could not be remembered safely. "
                                "No original record was changed."
                            )
                        else:
                            st.success(
                                "Observation memory result: "
                                f"{remember_result}. This is storage, not approval."
                            )
                else:
                    st.button(
                        "Remember this assessed observation",
                        key="cg_memory_remember_candidate_disabled",
                        disabled=True,
                        help="Assess a candidate for this tenant first.",
                    )

                st.divider()
                st.write("**Append-only human corrections**")
                st.info(
                    "A correction is retained beside its original observation and "
                    "never overwrites it. The current pattern analyzer does not "
                    "recalculate counts from correction records. To affect future "
                    "counts, separately assess a corrected, source-confirmed "
                    "observation above and explicitly remember it."
                )
                try:
                    correction_targets = memory_store.list_observations(
                        tenant_id,
                        category="company_event",
                        limit=100,
                    )
                except MemoryStoreError:
                    correction_targets = []
                    st.error("Correction targets could not be read safely.")

                if correction_targets:
                    correction_target_ids = [
                        observation.observation_id for observation in correction_targets
                    ]
                    correction_clock_key = f"cg_memory_correction_time::{tenant_id}"
                    if correction_clock_key not in st.session_state:
                        st.session_state[correction_clock_key] = datetime.now(UTC)
                    with st.form("company_memory_correction_form"):
                        correction_target_id = st.text_input(
                            "Correction target observation ID",
                            value=correction_target_ids[0],
                            help=(
                                "Use one of the remembered observation IDs shown in "
                                "the contributor trace."
                            ),
                        )
                        correction_id = st.text_input(
                            "Correction record ID",
                            value="correction-suite-354",
                        )
                        correction_reviewer = st.text_input(
                            "Correction reviewer",
                            value="authorized-local-reviewer",
                        )
                        corrected_address = st.text_input(
                            "Corrected address",
                            value="76 New Avenue",
                        )
                        corrected_suite = st.text_input(
                            "Corrected suite",
                            value="354",
                        )
                        correction_rationale = st.text_area(
                            "Correction rationale",
                            value=(
                                "A human verified a newer exact source; preserve the "
                                "original while recording this correction."
                            ),
                        )
                        correction_reference = st.text_input(
                            "Correction evidence reference",
                            value="fictional://human-review/suite-354",
                        )
                        correction_submitted = st.form_submit_button(
                            "Append human correction",
                            width="stretch",
                        )
                    if correction_submitted:
                        try:
                            correction = HumanCorrection(
                                tenant_id=tenant_id,
                                correction_id=correction_id,
                                target_observation_id=correction_target_id,
                                submitted_at=st.session_state[correction_clock_key],
                                reviewer=correction_reviewer,
                                rationale=correction_rationale,
                                corrected_attributes={
                                    "address": corrected_address,
                                    "suite": corrected_suite,
                                },
                                evidence_reference=correction_reference,
                            )
                            correction_result = memory_store.append_correction(
                                correction
                            )
                        except ValidationError:
                            st.error(
                                "The correction is invalid. Check its record ID, "
                                "reviewer, rationale, attributes, and evidence reference."
                            )
                        except MemoryStoreError:
                            st.error(
                                "The correction could not be appended safely. The "
                                "original observation remains unchanged."
                            )
                        else:
                            st.success(
                                f"Human correction {correction_result}; original "
                                "observation retained unchanged."
                            )

                    try:
                        remembered_corrections = memory_store.list_corrections(
                            tenant_id,
                            limit=MAX_MEMORY_UI_CORRECTIONS,
                        )
                    except MemoryStoreError:
                        remembered_corrections = []
                        st.error("Append-only corrections could not be listed safely.")
                    if remembered_corrections:
                        observations_by_id = {
                            observation.observation_id: observation
                            for observation in correction_targets
                        }
                        st.write(
                            f"**Retained corrections (newest first, bounded to "
                            f"{MAX_MEMORY_UI_CORRECTIONS})**"
                        )
                        for correction in remembered_corrections:
                            original = observations_by_id.get(
                                correction.target_observation_id
                            )
                            st.write(
                                f"Correction {correction.correction_id} → "
                                f"{correction.target_observation_id} · reviewer "
                                f"{correction.reviewer}"
                            )
                            st.text(
                                "Corrected attributes: "
                                + json.dumps(
                                    correction.corrected_attributes,
                                    ensure_ascii=False,
                                    sort_keys=True,
                                )
                            )
                            if original is not None:
                                original_attributes = (
                                    "[redacted: sensitive observation]"
                                    if original.sensitivity == Sensitivity.SENSITIVE
                                    else json.dumps(
                                        original.attributes,
                                        ensure_ascii=False,
                                        sort_keys=True,
                                    )
                                )
                                st.text("Original retained: " + original_attributes)
                            st.text(
                                "Correction evidence: " + correction.evidence_reference
                            )
                else:
                    st.caption(
                        "Remember at least one observation before appending a correction."
                    )

with st.expander(
    "Company Memory · show your work contributor trace",
    expanded=False,
):
    st.warning(
        "Authorized local users only. Evidence references may identify internal "
        "records; sensitive references are redacted here."
    )
    if memory_trace_tenant is None or not memory_trace_groups:
        st.info("No tenant-scoped contributors are available to trace yet.")
    else:
        st.caption(
            f"Tenant-scoped trace for {memory_trace_tenant}. At most "
            f"{MAX_MEMORY_UI_TRACE_ROWS} contributor rows are shown."
        )
        shown_trace_rows = 0
        seen_trace_contributors: set[tuple[str, str | None]] = set()
        for label, count, trusted_count, contributors in memory_trace_groups:
            group_rows = []
            for contributor in contributors:
                contributor_key = (
                    contributor.observation_id,
                    contributor.evidence_reference,
                )
                if contributor_key in seen_trace_contributors:
                    continue
                if shown_trace_rows + len(group_rows) >= MAX_MEMORY_UI_TRACE_ROWS:
                    break
                seen_trace_contributors.add(contributor_key)
                group_rows.append(contributor)
            if group_rows:
                st.write(
                    f"**{label}** · count {count} · trusted support {trusted_count}"
                )
                for contributor in group_rows:
                    evidence_reference = (
                        "[redacted: sensitive]"
                        if contributor.sensitivity == Sensitivity.SENSITIVE
                        else contributor.evidence_reference or "[missing]"
                    )
                    st.code(
                        f"observation_id={contributor.observation_id} | "
                        f"source={contributor.source_type} | "
                        f"evidence_reference={evidence_reference}",
                        language=None,
                    )
                    shown_trace_rows += 1
            if shown_trace_rows >= MAX_MEMORY_UI_TRACE_ROWS:
                break
        if shown_trace_rows == 0:
            st.info("No contributor rows are available for the current tenant.")
        elif shown_trace_rows == MAX_MEMORY_UI_TRACE_ROWS:
            st.caption("Additional contributor rows were omitted by the UI bound.")

with st.expander(
    "Important Details · totals, additions, and corrections",
    expanded=True,
):
    st.caption(
        "A constrained deterministic interpreter for company-selected quantities. "
        "This lab uses fictional data and keeps proposals and corrections only in "
        "this Streamlit session; it does not write them to SQLite or any other disk."
    )
    st.warning(
        "PROPOSE is a candidate for downstream policy—not an update. This panel "
        "performs no inbox read, automatic lookup, state change, or external action."
    )
    st.write(
        "**Fictional accepted state:** Fictional Summit · 2026-09-03 · crowd size 35"
    )
    st.caption(
        "Try `78 more people are confirmed` for DELTA → 113, or replace it "
        "with `78 people are going` for TOTAL → 78. Change either incoming "
        "identity field to see the request stop in REVIEW."
    )

    semantic_profile_raw = st.text_area(
        "Important-details profile JSON",
        value=json.dumps(DEFAULT_SEMANTIC_PROFILE, indent=2),
        height=330,
        key="cg_semantic_profile_json",
        help=(
            "Customize important fields, metric nouns, total/delta/status markers, "
            "and plausible value/change bounds. The profile is capped at 16 KiB."
        ),
    )

    with st.form("important_details_interpret_form"):
        current_identity_col, incoming_identity_col = st.columns(2)
        with current_identity_col:
            st.write("**Current accepted entity identity**")
            semantic_current_event = st.text_input(
                "Current event name",
                value="Fictional Summit",
            )
            semantic_current_date = st.text_input(
                "Current event date",
                value="2026-09-03",
            )
            semantic_current_total = st.number_input(
                "Current accepted crowd size",
                min_value=0,
                max_value=999_999_999,
                value=35,
                step=1,
            )
        with incoming_identity_col:
            st.write("**Incoming claimed entity identity**")
            semantic_incoming_event = st.text_input(
                "Incoming event name",
                value="Fictional Summit",
            )
            semantic_incoming_date = st.text_input(
                "Incoming event date",
                value="2026-09-03",
            )
            semantic_evidence_id = st.text_input(
                "Important-detail evidence ID",
                value="fictional-email-002",
            )

        semantic_statement = st.text_area(
            "Incoming important-detail statement",
            value="78 more people are confirmed",
            height=90,
        )
        semantic_source_col, semantic_reference_col = st.columns(2)
        with semantic_source_col:
            semantic_source = st.text_input(
                "Important-detail source type",
                value="official_email",
            )
        with semantic_reference_col:
            semantic_reference = st.text_input(
                "Important-detail evidence reference",
                value="fictional://official-email/attendance-002",
            )
        semantic_submitted = st.form_submit_button(
            "Interpret important detail",
            type="primary",
            width="stretch",
        )

    if semantic_submitted:
        try:
            semantic_config = _validate_semantic_profile(semantic_profile_raw)
            tracked_field = semantic_config.quantity_fields[0].field_name
            semantic_state = EntityQuantityState(
                category=semantic_config.category,
                entity_id="fictional-summit-2026-09-03",
                identity={
                    "event_name": semantic_current_event,
                    "event_date": semantic_current_date,
                },
                quantity_values={
                    tracked_field: int(semantic_current_total),
                },
                as_of=SEMANTIC_STATE_AS_OF,
            )
            semantic_incoming = IncomingQuantityStatement(
                evidence_id=semantic_evidence_id,
                category=semantic_config.category,
                identity={
                    "event_name": semantic_incoming_event,
                    "event_date": semantic_incoming_date,
                },
                source_type=semantic_source,
                evidence_reference=semantic_reference or None,
                text=semantic_statement,
                observed_at=SEMANTIC_STATE_AS_OF + timedelta(minutes=5),
            )
            semantic_proposal = interpret_quantity_update(
                semantic_config,
                semantic_state,
                semantic_incoming,
            )
        except WorkbenchInputError as exc:
            st.error(str(exc))
        except ValidationError as exc:
            st.error(_safe_pydantic_errors("Important-detail input", exc))
        except (TypeError, ValueError):
            st.error(
                "The important detail could not be interpreted safely. Check the "
                "bounded profile, identities, source, reference, and statement."
            )
        else:
            st.session_state.cg_semantic_config = semantic_config
            st.session_state.cg_semantic_state = semantic_state
            st.session_state.cg_semantic_proposal = semantic_proposal
            st.session_state.cg_semantic_corrected = None
            st.rerun()

    stored_semantic_proposal = st.session_state.get("cg_semantic_proposal")
    stored_semantic_correction = st.session_state.get("cg_semantic_corrected")
    semantic_result = stored_semantic_correction or stored_semantic_proposal
    if semantic_result is not None:
        original_semantic = _semantic_original(semantic_result)
        proposed_display = (
            str(semantic_result.proposed_total)
            if semantic_result.proposed_total is not None
            else "not calculated"
        )
        result_line = (
            f"{semantic_result.outcome.value} · {semantic_result.mode.value} · "
            f"candidate total {proposed_display}"
        )
        if semantic_result.outcome == ProposalOutcome.PROPOSE:
            st.success(result_line)
        else:
            st.warning(result_line)

        st.write(
            f"**Identity matched:** {'yes' if semantic_result.identity_matched else 'no'}"
        )
        if semantic_result.matched_identity:
            st.json(semantic_result.matched_identity, expanded=False)
        st.write("**Calculation formula**")
        st.code(semantic_result.calculation_trace.formula, language=None)

        st.write("**Why**")
        for reason in semantic_result.reasons:
            st.write(f"{reason.code} · {reason.detail}")
        if semantic_result.human_questions:
            st.write("**Questions for a human**")
            for question in semantic_result.human_questions:
                st.write(question)

        st.write("**Evidence and immutable bindings**")
        st.code(
            "\n".join(
                [
                    f"evidence_id={original_semantic.evidence_id}",
                    f"source_type={original_semantic.source_type}",
                    "evidence_reference="
                    + (original_semantic.evidence_reference or "[not supplied]"),
                    f"input_digest={original_semantic.input_digest}",
                    f"state_digest={original_semantic.state_digest}",
                    f"config_fingerprint={original_semantic.config_fingerprint}",
                ]
            ),
            language=None,
        )

        st.write("**Full contribution trace**")
        st.dataframe(
            [
                {
                    "kind": contribution.kind.value,
                    "evidence_id": contribution.evidence_id,
                    "reference": contribution.evidence_reference or "[not supplied]",
                    "source": contribution.source_type,
                    "excerpt": contribution.interpreted_excerpt,
                    "mode": contribution.mode.value,
                    "quantity": contribution.stated_quantity,
                    "prior": contribution.prior_total,
                    "result": contribution.resulting_total,
                    "digest": contribution.content_digest,
                }
                for contribution in semantic_result.calculation_trace.contributions
            ],
            hide_index=True,
            width="stretch",
        )
        st.caption(
            "Automatic lookup performed: no · state updated: no · external action "
            "executed: no · session-only proposal/correction storage"
        )

        semantic_question = st.selectbox(
            "Ask about this important detail",
            options=[
                "How did you get that total?",
                "Is it the same event?",
                "What is the safe next step?",
            ],
        )
        st.info(_semantic_local_answer(semantic_question, semantic_result))
        st.caption(
            "This answer is generated only from the current deterministic trace; "
            "no LLM, network request, or inbox search is used."
        )

        st.divider()
        st.write("**Append a human interpretation correction**")
        st.caption(
            "A correction can clarify TOTAL versus DELTA or the quantity. It "
            "preserves the original proposal and does not update the accepted state."
        )
        default_correction_quantity = (
            semantic_result.corrected_quantity
            if isinstance(semantic_result, CorrectedSemanticUpdateProposal)
            else original_semantic.stated_quantity or 78
        )
        with st.form("important_details_correction_form"):
            semantic_correction_mode = st.selectbox(
                "Correction interpretation",
                options=[QuantityMode.TOTAL.value, QuantityMode.DELTA.value],
            )
            semantic_correction_quantity = st.number_input(
                "Corrected stated quantity",
                min_value=0,
                max_value=999_999_999,
                value=int(default_correction_quantity),
                step=1,
            )
            semantic_correction_reviewer = st.text_input(
                "Important-detail correction reviewer",
                value="authorized-local-reviewer",
            )
            semantic_correction_rationale = st.text_area(
                "Important-detail correction rationale",
                value=(
                    "The fictional source was clarified; retain the original "
                    "interpretation and record this correction."
                ),
            )
            semantic_correction_submitted = st.form_submit_button(
                "Append important-detail correction",
                width="stretch",
            )

        if semantic_correction_submitted:
            saved_config = st.session_state.get("cg_semantic_config")
            saved_state = st.session_state.get("cg_semantic_state")
            latest_result = st.session_state.get("cg_semantic_corrected") or (
                st.session_state.get("cg_semantic_proposal")
            )
            if saved_config is None or saved_state is None or latest_result is None:
                st.error("Re-interpret the important detail before correcting it.")
            else:
                prior_correction_count = (
                    len(latest_result.corrections)
                    if isinstance(latest_result, CorrectedSemanticUpdateProposal)
                    else 0
                )
                correction_number = prior_correction_count + 1
                try:
                    semantic_correction = HumanQuantityCorrection(
                        correction_id=f"semantic-correction-{correction_number:03d}",
                        field_name=(
                            latest_result.field_name
                            or saved_config.quantity_fields[0].field_name
                        ),
                        mode=QuantityMode(semantic_correction_mode),
                        quantity=int(semantic_correction_quantity),
                        reviewer=semantic_correction_reviewer,
                        rationale=semantic_correction_rationale,
                        created_at=(
                            saved_state.as_of
                            + timedelta(hours=2, seconds=correction_number)
                        ),
                        evidence_reference=(
                            f"review://session/semantic-correction-"
                            f"{correction_number:03d}"
                        ),
                    )
                    corrected_result = apply_human_correction(
                        saved_config,
                        saved_state,
                        latest_result,
                        semantic_correction,
                    )
                except ValidationError as exc:
                    st.error(_safe_pydantic_errors("Human correction", exc))
                except (TypeError, ValueError):
                    st.error(
                        "The correction could not be appended safely. The original "
                        "proposal remains unchanged."
                    )
                else:
                    st.session_state.cg_semantic_corrected = corrected_result
                    st.rerun()

        if isinstance(semantic_result, CorrectedSemanticUpdateProposal):
            st.write(
                f"**Correction history ({len(semantic_result.corrections)}) · "
                "original preserved**"
            )
            st.code(
                "Original: "
                f"{semantic_result.original_proposal.outcome.value} / "
                f"{semantic_result.original_proposal.mode.value} / "
                f"{semantic_result.original_proposal.calculation_trace.formula}",
                language=None,
            )
            for correction in semantic_result.corrections:
                st.write(
                    f"{correction.correction_id} · {correction.mode.value} "
                    f"{correction.quantity} · reviewer {correction.reviewer} · "
                    f"{correction.created_at.isoformat()}"
                )
                st.caption(correction.rationale)
    else:
        st.info(
            "Interpret the fictional statement to create a session-only proposal "
            "and its exact calculation trace."
        )

left, right = st.columns([1.35, 1])
with left:
    st.subheader("Human control")
    if not decision.requires_human_approval:
        st.success("No review required for this non-consequential preview.")
        st.caption(
            "ALLOW is scoped to this synthetic preview. ContextGate did not call an external service."
        )
    elif st.session_state.cg_review is None:
        st.warning(
            "The original decision remains enforced until a review receipt is recorded."
        )
        reviewer = st.text_input("Reviewer", value="local-reviewer")
        rationale = st.text_area(
            "Rationale",
            value=REVIEW_RATIONALES.get(
                scenario.name,
                "The evidence must be verified before any external action.",
            ),
        )
        use_review_as_guidance = st.checkbox(
            "Use this review as future operator guidance",
            value=True,
            help=(
                "The explicit review response becomes retractable, tenant-scoped "
                "chat guidance. A changed outcome also receives a correction receipt."
            ),
        )
        b1, b2, b3 = st.columns(3)
        hold = b1.button("Hold", width="stretch")
        reject = b2.button("Reject", type="primary", width="stretch")
        override = b3.button("Explicit override", width="stretch")
        chosen = (
            ReviewAction.HOLD
            if hold
            else ReviewAction.REJECT
            if reject
            else ReviewAction.APPROVE_OVERRIDE
            if override
            else None
        )
        if chosen:
            try:
                review = record_review(
                    decision,
                    request,
                    action=chosen,
                    reviewer=reviewer,
                    rationale=rationale,
                )
                audit_entry = audit_log().append_review(review)
                stored_review = ReviewEvent.model_validate(audit_entry.payload)
                st.session_state.cg_review = stored_review
                if use_review_as_guidance:
                    if operator_store is None or operator_tenant_id is None:
                        st.session_state.cg_operator_error = (
                            "The review receipt was saved, but operator learning is "
                            "unavailable."
                        )
                    else:
                        try:
                            learned_correction, guidance_saved = _learn_from_review(
                                operator_store,
                                operator_tenant_id,
                                scenario=scenario,
                                decision=decision,
                                review=stored_review,
                            )
                        except (
                            OperatorLearningStoreError,
                            ValidationError,
                            TypeError,
                            ValueError,
                        ):
                            st.session_state.cg_operator_error = (
                                "The review receipt was saved, but its operator lesson "
                                "could not be stored."
                            )
                        else:
                            resolution_text = (
                                f" Effective dashboard outcome: "
                                f"{learned_correction.corrected_outcome.value}."
                                if learned_correction is not None
                                else ""
                            )
                            st.session_state.cg_operator_notice = (
                                f"Review response learned for case {scenario.case_id}."
                                f"{resolution_text}"
                            )
                            if not guidance_saved:
                                st.session_state.cg_operator_error = (
                                    "The outcome correction was saved, but its chat "
                                    "guidance could not be stored."
                                )
                st.rerun()
            except (OSError, ValueError) as exc:
                st.error(f"Review was not recorded: {exc}")
    else:
        review = st.session_state.cg_review
        st.success(f"Review recorded: {review.resulting_status.value}")
        st.caption(
            "This receipt records intent only. No external calendar action was executed."
        )
        st.json(review.model_dump(mode="json"), expanded=False)
        st.download_button(
            "Download review receipt",
            data=json.dumps(review.model_dump(mode="json"), indent=2),
            file_name=f"{review.review_id}.json",
            mime="application/json",
            width="stretch",
        )

with right:
    st.subheader("Enforcement trace")
    st.write("**Deterministic rules — executed first**")
    for rule_id in decision.deterministic_rule_ids:
        st.code(rule_id)
    st.write("**Model path — explanation only**")
    st.write(
        "Validated Streaming Agent explanation"
        if decision.model_explanation_used
        else "Template fallback — deterministic result preserved"
    )
    st.write("**Bound evidence IDs**")
    st.code(" · ".join(decision.evidence_event_ids) or "None")
    st.download_button(
        "Download decision receipt",
        data=json.dumps(decision.model_dump(mode="json"), indent=2),
        file_name=f"{decision.decision_id}.json",
        mime="application/json",
        width="stretch",
    )

with st.expander("Structured evidence package"):
    st.json(build_evidence_package(events, request, policy=page_policy))

with st.expander("Replayable audit log"):
    if audit_error:
        st.error(f"Audit data could not be read: {audit_error}")
    else:
        st.dataframe(
            [entry.model_dump(mode="json") for entry in audit_entries],
            width="stretch",
            hide_index=True,
        )
        st.download_button(
            "Download audit chain",
            data=json.dumps(
                [entry.model_dump(mode="json") for entry in audit_entries], indent=2
            ),
            file_name="contextgate-audit.json",
            mime="application/json",
        )

with st.expander("How this maps to Confluent"):
    st.markdown(
        """
        - **Kafka topics** retain context, requests, decisions, and review receipts for replay.
        - **Flink SQL** normalizes events, resolves authority, and emits the deterministic gate.
        - **Confluent Streaming Agents** explain an already-made decision and write an observable system log.
        - **ContextGate never lets model prose override `BLOCK` or `REVIEW`.**
        """
    )
