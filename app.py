"""Streamlit demonstration for the ContextGate vertical slice."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import streamlit as st

from context_gate.approvals import record_review
from context_gate.audit_log import AppendOnlyAuditLog
from context_gate.authority import effective_trust, policy_for
from context_gate.decision_engine import evaluate_request
from context_gate.evidence import build_evidence_package
from context_gate.models import ReviewAction
from context_gate.normalization import normalize_event, normalize_request
from context_gate.scenario import load_demo_events, load_demo_request

ROOT = Path(__file__).resolve().parent
AUDIT_PATH = ROOT / "runtime" / "audit.jsonl"

st.set_page_config(page_title="ContextGate", page_icon="🛡️", layout="wide")
st.markdown(
    """
    <style>
    .block-container {max-width: 1220px; padding-top: 2rem;}
    .cg-kicker {color:#7dd3fc; font-size:.78rem; font-weight:700; letter-spacing:.14em; text-transform:uppercase;}
    .cg-subtitle {color:#94a3b8; font-size:1.02rem; margin-top:-.4rem;}
    .cg-banner {border:1px solid #ef4444; background:linear-gradient(135deg,#2a1115,#170d11);
      border-radius:14px; padding:1.25rem 1.4rem; margin:.6rem 0 1.2rem 0;}
    .cg-banner h2 {color:#fca5a5; margin:0 0 .2rem 0;}
    .cg-banner p {color:#e2e8f0; margin:.18rem 0;}
    .cg-source {border:1px solid #334155; background:#111827; color:#e2e8f0; border-radius:12px; padding:1rem; min-height:230px;}
    .cg-source.authoritative {border-color:#22c55e; box-shadow:0 0 0 1px #22c55e22;}
    .cg-label {color:#94a3b8; font-size:.75rem; text-transform:uppercase; letter-spacing:.08em;}
    .cg-source strong, .cg-value {color:#f8fafc;}
    .cg-value {font-size:1.3rem; font-weight:700; margin:.2rem 0 .65rem 0;}
    .cg-pill {display:inline-block; border-radius:999px; padding:.18rem .55rem; font-size:.73rem;
      margin-right:.25rem; background:#1e293b; color:#cbd5e1;}
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_resource
def audit_log() -> AppendOnlyAuditLog:
    return AppendOnlyAuditLog(AUDIT_PATH)


def start_run(run_id: str | None = None) -> None:
    events = load_demo_events()
    request = load_demo_request()
    run_id = run_id or f"run-{uuid4().hex[:10]}"
    decision = evaluate_request(events, request, run_id=run_id)
    audit_log().append_decision(decision)
    st.session_state.cg_run_id = run_id
    st.session_state.cg_decision = decision
    st.session_state.cg_review = None


if "cg_decision" not in st.session_state:
    start_run("run-local-baseline")

events = [normalize_event(event) for event in load_demo_events()]
request = normalize_request(load_demo_request())
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
        and event.field_value == decision.authoritative_value
    ),
    events[0],
)

st.markdown(
    '<div class="cg-kicker">Real-time context firewall</div>', unsafe_allow_html=True
)
st.title("ContextGate")
st.markdown(
    '<div class="cg-subtitle">Evidence and approval control for AI agents — deterministic even when the model is unavailable.</div>',
    unsafe_allow_html=True,
)

with st.sidebar:
    st.header("Demo controls")
    st.success("Local fallback active")
    st.caption("No cloud call, model credential, or external action is required.")
    if st.button("Replay incoming stream", width="stretch"):
        start_run()
        st.rerun()
    st.divider()
    st.write("**Run ID**")
    st.code(decision.run_id)
    st.write("**Audit chain**")
    if audit_log().verify_chain():
        st.success("Verified")
    else:
        st.error("Integrity failure")
    st.caption(f"{len(audit_log().read_entries())} append-only record(s)")

m1, m2, m3, m4 = st.columns(4)
m1.metric("Classification", decision.classification.value)
m2.metric("Enforcement", decision.decision.value)
m3.metric("Risk", decision.risk.value)
m4.metric("Review", display_review_status.value)

st.markdown(
    f"""
    <div class="cg-banner">
      <h2>Action blocked</h2>
      <p><strong>{request.action_type}</strong> requested <strong>{request.requested_value}</strong>.</p>
      <p>{decision.explanation}</p>
      <p>Authoritative value remains <strong>{decision.authoritative_value}</strong>.</p>
    </div>
    """,
    unsafe_allow_html=True,
)

st.subheader("Evidence received over time")
columns = st.columns(len(events))
for column, event in zip(columns, events, strict=True):
    is_authoritative = event.event_id == authoritative.event_id
    css_class = "cg-source authoritative" if is_authoritative else "cg-source"
    authority_badge = "AUTHORITATIVE" if is_authoritative else "COMPETING"
    with column:
        st.markdown(
            f"""
            <div class="{css_class}">
              <div class="cg-label">{authority_badge} · {event.event_id}</div>
              <div class="cg-value">{event.field_value}</div>
              <div><strong>{event.source_name}</strong></div>
              <div style="color:#94a3b8;margin:.25rem 0 .7rem 0">Observed {event.observed_at.strftime("%b %d, %H:%M UTC")}</div>
              <span class="cg-pill">policy rank {policy_for(event).rank}</span>
              <span class="cg-pill">trust {effective_trust(event):.2f}</span>
              <span class="cg-pill">{event.status.value}</span>
              <p style="color:#cbd5e1;margin-top:.85rem">{policy_for(event).label}</p>
            </div>
            """,
            unsafe_allow_html=True,
        )

st.info(
    "The later community listing does not replace the earlier official confirmation: "
    "source authority outranks simple arrival order. Rule CG-002 issued the BLOCK before any model explanation."
)

left, right = st.columns([1.35, 1])
with left:
    st.subheader("Human review")
    if st.session_state.cg_review is None:
        reviewer = st.text_input("Reviewer", value="demo-reviewer")
        rationale = st.text_area(
            "Rationale",
            value="Conflicting lower-authority evidence must be verified with the organizer.",
        )
        b1, b2, b3 = st.columns(3)
        hold = b1.button("Hold", width="stretch")
        reject = b2.button("Reject", type="primary", width="stretch")
        override = b3.button("Approve explicit override", width="stretch")
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
                audit_log().append_review(review)
                st.session_state.cg_review = review
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
    else:
        review = st.session_state.cg_review
        st.success(f"Review recorded: {review.resulting_status.value}")
        st.caption(
            "This receipt records intent only. No external calendar action was executed."
        )
        st.json(review.model_dump(mode="json"), expanded=False)

with right:
    st.subheader("Enforcement trace")
    st.write("**Deterministic rules**")
    for rule_id in decision.deterministic_rule_ids:
        st.code(rule_id)
    st.write("**Model path**")
    st.write("Template fallback — deterministic result preserved")
    st.write("**Evidence IDs**")
    st.code(" · ".join(decision.evidence_event_ids))

with st.expander("Structured evidence package"):
    st.json(build_evidence_package(events, request))

with st.expander("Replayable audit log"):
    st.dataframe(
        [entry.model_dump(mode="json") for entry in audit_log().read_entries()],
        width="stretch",
        hide_index=True,
    )
