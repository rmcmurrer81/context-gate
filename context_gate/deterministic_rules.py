"""Fail-closed deterministic enforcement. Model output is never consulted here."""

from __future__ import annotations

from .authority import (
    authority_key,
    choose_authoritative,
    effective_trust,
    policy_for,
    sources_are_near_peers,
)
from .evidence import (
    events_available_as_of,
    missing_evidence_fields,
    relevant_events,
    unique_events,
)
from .models import (
    ActionRequest,
    Classification,
    ContextEvent,
    EnforcementDecision,
    EvidenceStatus,
    Risk,
    RuleResult,
    Sensitivity,
)
from .policy_config import DEFAULT_POLICY, ActivePolicy, get_active_policy

RULE_DUPLICATE = "CG-001-DUPLICATE-CONTENT"
RULE_LOWER_AUTHORITY_CONFLICT = "CG-002-LOWER-AUTHORITY-CONFLICT"
RULE_PEER_CONFLICT = "CG-003-PEER-CONFLICT"
RULE_STALE = "CG-004-STALE-EFFECTIVE-TIME"
RULE_INSUFFICIENT = "CG-005-REQUIRED-PROVENANCE"
RULE_SENSITIVE = "CG-006-SENSITIVE-APPROVAL"
RULE_CONSEQUENTIAL = "CG-007-CONSEQUENTIAL-APPROVAL"
RULE_SAFE = "CG-008-SAFE"
RULE_UNTRUSTED = "CG-009-UNTRUSTED-VALUE"

MIN_AUTOMATIC_AUTHORITY_RANK = DEFAULT_POLICY.minimum_automatic_authority_rank
MIN_AUTOMATIC_TRUST = DEFAULT_POLICY.minimum_automatic_trust


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


def evaluate_rules(
    events: list[ContextEvent],
    request: ActionRequest,
    *,
    policy: ActivePolicy | None = None,
) -> RuleResult:
    active_policy = policy or get_active_policy()
    related = relevant_events(events, request)
    scoped = events_available_as_of(related, request)
    if not scoped:
        future_ids = sorted(
            event.event_id
            for event in related
            if event.observed_at is not None and event.observed_at > request.created_at
        )
        explanation = "No evidence exists for the requested entity and field."
        if future_ids:
            explanation = (
                "No evidence was available as of the request time; later evidence "
                f"cannot be used ({', '.join(future_ids)})."
            )
        return RuleResult(
            classification=Classification.INSUFFICIENT_EVIDENCE,
            decision=EnforcementDecision.REVIEW,
            risk=Risk.HIGH,
            evidence_event_ids=future_ids,
            explanation=explanation,
            deterministic_rule_ids=[RULE_INSUFFICIENT],
            requires_human_approval=True,
        )

    if not request.supporting_event_id:
        return RuleResult(
            classification=Classification.INSUFFICIENT_EVIDENCE,
            decision=EnforcementDecision.REVIEW,
            risk=Risk.HIGH,
            competing_values=sorted({event.field_value for event in scoped}),
            evidence_event_ids=sorted(event.event_id for event in scoped),
            explanation="The action request does not identify its supporting evidence.",
            deterministic_rule_ids=[RULE_INSUFFICIENT],
            requires_human_approval=True,
        )

    supporting = [
        event for event in scoped if event.event_id == request.supporting_event_id
    ]
    if len(supporting) != 1:
        matching_related = [
            event for event in related if event.event_id == request.supporting_event_id
        ]
        reason = (
            "was observed after the request and was unavailable at decision time"
            if matching_related
            else "does not exist in the matching evidence set"
        )
        return RuleResult(
            classification=Classification.INSUFFICIENT_EVIDENCE,
            decision=EnforcementDecision.REVIEW,
            risk=Risk.HIGH,
            competing_values=sorted({event.field_value for event in scoped}),
            evidence_event_ids=sorted(event.event_id for event in scoped),
            explanation=(f"Supporting event {request.supporting_event_id} {reason}."),
            deterministic_rule_ids=[RULE_INSUFFICIENT],
            requires_human_approval=True,
        )

    if supporting[0].field_value.casefold() != request.requested_value.casefold():
        return RuleResult(
            classification=Classification.INSUFFICIENT_EVIDENCE,
            decision=EnforcementDecision.REVIEW,
            risk=Risk.HIGH,
            competing_values=sorted(
                {event.field_value for event in scoped} | {request.requested_value}
            ),
            evidence_event_ids=[supporting[0].event_id],
            explanation=(
                f"Supporting event {supporting[0].event_id} does not support the "
                "requested value."
            ),
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

    scoped = unique_events(scoped, active_policy)
    authoritative = choose_authoritative(scoped, active_policy)
    values = sorted({event.field_value for event in scoped})

    value_representatives: dict[str, ContextEvent] = {}
    for event in scoped:
        key = event.field_value.casefold()
        current = value_representatives.get(key)
        if current is None or authority_key(event, active_policy) > authority_key(
            current, active_policy
        ):
            value_representatives[key] = event
    ranked_values = sorted(
        value_representatives.values(),
        key=lambda event: authority_key(event, active_policy),
        reverse=True,
    )
    peer = next(
        (
            event
            for event in ranked_values[1:]
            if sources_are_near_peers(authoritative, event, active_policy)
        ),
        None,
    )
    if peer is not None:
        return RuleResult(
            classification=Classification.CONFLICT,
            decision=EnforcementDecision.REVIEW,
            risk=Risk.HIGH,
            authoritative_value=authoritative.field_value,
            competing_values=values,
            evidence_event_ids=[authoritative.event_id, peer.event_id],
            explanation=(
                "Near-peer authoritative sources disagree, so the action requires review."
            ),
            deterministic_rule_ids=[RULE_PEER_CONFLICT],
            requires_human_approval=True,
        )

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
            strongest_supporter = choose_authoritative(supporters, active_policy)
            is_peer_conflict = sources_are_near_peers(
                authoritative, strongest_supporter, active_policy
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
                    f"({policy_for(strongest_supporter, active_policy).label}) and conflicts with "
                    f"{policy_for(authoritative, active_policy).label}."
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
            deterministic_rule_ids=[RULE_UNTRUSTED],
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

    if (
        policy_for(authoritative, active_policy).rank
        < active_policy.minimum_automatic_authority_rank
        or authoritative.status != EvidenceStatus.CONFIRMED
        or effective_trust(authoritative, active_policy)
        < active_policy.minimum_automatic_trust
    ):
        return RuleResult(
            classification=Classification.UNTRUSTED,
            decision=EnforcementDecision.REVIEW,
            risk=Risk.HIGH,
            authoritative_value=authoritative.field_value,
            competing_values=values,
            evidence_event_ids=[authoritative.event_id],
            explanation=(
                "The requested value matches available evidence, but that evidence is "
                "not strong enough for automatic action."
            ),
            deterministic_rule_ids=[RULE_UNTRUSTED],
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
