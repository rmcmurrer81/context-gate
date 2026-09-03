"""Fail-closed deterministic enforcement. Model output is never consulted here."""

from __future__ import annotations

from .authority import choose_authoritative, policy_for, sources_are_near_peers
from .evidence import relevant_events, unique_events
from .models import (
    ActionRequest,
    Classification,
    ContextEvent,
    EnforcementDecision,
    Risk,
    RuleResult,
    Sensitivity,
)

RULE_DUPLICATE = "CG-001-DUPLICATE-CONTENT"
RULE_LOWER_AUTHORITY_CONFLICT = "CG-002-LOWER-AUTHORITY-CONFLICT"
RULE_PEER_CONFLICT = "CG-003-PEER-CONFLICT"
RULE_STALE = "CG-004-STALE-EFFECTIVE-TIME"
RULE_INSUFFICIENT = "CG-005-REQUIRED-PROVENANCE"
RULE_SENSITIVE = "CG-006-SENSITIVE-APPROVAL"
RULE_CONSEQUENTIAL = "CG-007-CONSEQUENTIAL-APPROVAL"
RULE_SAFE = "CG-008-SAFE"


def missing_evidence_fields(event: ContextEvent) -> list[str]:
    missing: list[str] = []
    for field in (
        "source_name",
        "source_type",
        "observed_at",
        "effective_at",
        "content_hash",
    ):
        if getattr(event, field) in (None, ""):
            missing.append(field)
    if not event.evidence_uri and not event.evidence_reference:
        missing.append("evidence_reference")
    return missing


def classify_incoming_event(
    candidate: ContextEvent, history: list[ContextEvent]
) -> Classification:
    if any(
        candidate.content_hash and candidate.content_hash == item.content_hash
        for item in history
    ):
        return Classification.DUPLICATE
    comparable = [
        item
        for item in history
        if item.entity_id == candidate.entity_id
        and item.field_name == candidate.field_name
    ]
    if comparable and candidate.effective_at:
        newest = max(
            (item.effective_at for item in comparable if item.effective_at),
            default=None,
        )
        if newest and candidate.effective_at < newest:
            return Classification.STALE
    return Classification.SAFE


def evaluate_rules(events: list[ContextEvent], request: ActionRequest) -> RuleResult:
    scoped = relevant_events(events, request)
    if not scoped:
        return RuleResult(
            classification=Classification.INSUFFICIENT_EVIDENCE,
            decision=EnforcementDecision.REVIEW,
            risk=Risk.HIGH,
            explanation="No evidence exists for the requested entity and field.",
            deterministic_rule_ids=[RULE_INSUFFICIENT],
            requires_human_approval=True,
        )

    incomplete = {event.event_id: missing_evidence_fields(event) for event in scoped}
    incomplete = {event_id: fields for event_id, fields in incomplete.items() if fields}
    if incomplete:
        details = "; ".join(
            f"{event_id}: {', '.join(fields)}"
            for event_id, fields in incomplete.items()
        )
        return RuleResult(
            classification=Classification.INSUFFICIENT_EVIDENCE,
            decision=EnforcementDecision.REVIEW,
            risk=Risk.HIGH,
            competing_values=sorted({event.field_value for event in scoped}),
            evidence_event_ids=[event.event_id for event in scoped],
            explanation=f"Required provenance is missing ({details}); ContextGate will not infer it.",
            deterministic_rule_ids=[RULE_INSUFFICIENT],
            requires_human_approval=True,
        )

    scoped = unique_events(scoped)
    authoritative = choose_authoritative(scoped)
    values = sorted({event.field_value for event in scoped})

    if (
        request.requested_effective_at
        and authoritative.effective_at
        and request.requested_effective_at < authoritative.effective_at
    ):
        return RuleResult(
            classification=Classification.STALE,
            decision=EnforcementDecision.BLOCK,
            risk=Risk.HIGH,
            authoritative_value=authoritative.field_value,
            competing_values=values,
            evidence_event_ids=[authoritative.event_id],
            explanation="The requested effective time is older than the authoritative record.",
            deterministic_rule_ids=[RULE_STALE],
            requires_human_approval=True,
        )

    if request.requested_value.casefold() != authoritative.field_value.casefold():
        supporters = [
            event
            for event in scoped
            if event.field_value.casefold() == request.requested_value.casefold()
        ]
        if supporters:
            strongest_supporter = choose_authoritative(supporters)
            is_peer_conflict = sources_are_near_peers(
                authoritative, strongest_supporter
            )
            decision = (
                EnforcementDecision.REVIEW
                if is_peer_conflict
                else EnforcementDecision.BLOCK
            )
            rule = (
                RULE_PEER_CONFLICT
                if is_peer_conflict
                else RULE_LOWER_AUTHORITY_CONFLICT
            )
            risk = Risk.HIGH
            if is_peer_conflict:
                explanation = "Near-peer authoritative sources disagree, so the action requires review."
            else:
                explanation = (
                    f"The requested value is supported only by a lower-authority source "
                    f"({policy_for(strongest_supporter).label}) and conflicts with "
                    f"{policy_for(authoritative).label}."
                )
            return RuleResult(
                classification=Classification.CONFLICT,
                decision=decision,
                risk=risk,
                authoritative_value=authoritative.field_value,
                competing_values=values,
                evidence_event_ids=[
                    authoritative.event_id,
                    strongest_supporter.event_id,
                ],
                explanation=explanation,
                deterministic_rule_ids=[rule],
                requires_human_approval=True,
            )

        return RuleResult(
            classification=Classification.UNTRUSTED,
            decision=EnforcementDecision.REVIEW,
            risk=Risk.HIGH,
            authoritative_value=authoritative.field_value,
            competing_values=values + [request.requested_value],
            evidence_event_ids=[event.event_id for event in scoped],
            explanation="No submitted evidence supports the requested value.",
            deterministic_rule_ids=[RULE_INSUFFICIENT],
            requires_human_approval=True,
        )

    if request.sensitivity != Sensitivity.PUBLIC or any(
        event.sensitivity != Sensitivity.PUBLIC for event in scoped
    ):
        return RuleResult(
            classification=Classification.SENSITIVE,
            decision=EnforcementDecision.REVIEW,
            risk=Risk.HIGH,
            authoritative_value=authoritative.field_value,
            competing_values=values,
            evidence_event_ids=[event.event_id for event in scoped],
            explanation="Sensitive context requires explicit human approval.",
            deterministic_rule_ids=[RULE_SENSITIVE],
            requires_human_approval=True,
        )

    if request.consequential:
        return RuleResult(
            classification=Classification.SAFE,
            decision=EnforcementDecision.REVIEW,
            risk=Risk.MEDIUM,
            authoritative_value=authoritative.field_value,
            competing_values=values,
            evidence_event_ids=[event.event_id for event in scoped],
            explanation="The value is supported, but this external action requires explicit approval.",
            deterministic_rule_ids=[RULE_SAFE, RULE_CONSEQUENTIAL],
            requires_human_approval=True,
        )

    return RuleResult(
        classification=Classification.SAFE,
        decision=EnforcementDecision.ALLOW,
        risk=Risk.LOW,
        authoritative_value=authoritative.field_value,
        competing_values=values,
        evidence_event_ids=[event.event_id for event in scoped],
        explanation="The requested value matches the best-supported current evidence.",
        deterministic_rule_ids=[RULE_SAFE],
        requires_human_approval=False,
    )
