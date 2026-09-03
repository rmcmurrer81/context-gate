"""Deterministic configurable source-authority policy.

Production deployments must bind source identity at authenticated ingestion instead
of trusting a producer-supplied source_type field.
"""

from __future__ import annotations

from datetime import UTC, datetime

from .models import ContextEvent, EvidenceStatus
from .policy_config import (
    DEFAULT_POLICY,
    ActivePolicy,
    AuthorityPolicy,
    get_active_policy,
)

# Backward-compatible snapshot of the built-in zero-configuration policy. Runtime
# evaluation uses get_active_policy(), including when a company policy is configured.
SOURCE_AUTHORITY = DEFAULT_POLICY.sources


def policy_for(
    event: ContextEvent, policy: ActivePolicy | None = None
) -> AuthorityPolicy:
    active = policy or get_active_policy()
    return active.sources.get(event.source_type or "unknown", active.sources["unknown"])


def effective_trust(event: ContextEvent, policy: ActivePolicy | None = None) -> float:
    return min(event.trust_score, policy_for(event, policy).trust_cap)


def authority_key(
    event: ContextEvent,
    policy: ActivePolicy | None = None,
) -> tuple[int, int, float, datetime, datetime, str]:
    earliest = datetime.min.replace(tzinfo=UTC)
    status_weight = {
        EvidenceStatus.CONFIRMED: 3,
        EvidenceStatus.UNVERIFIED: 2,
        EvidenceStatus.INFERRED: 1,
        EvidenceStatus.UNKNOWN: 0,
    }[event.status]
    return (
        policy_for(event, policy).rank,
        status_weight,
        effective_trust(event, policy),
        event.effective_at or earliest,
        event.observed_at or earliest,
        event.event_id,
    )


def choose_authoritative(
    events: list[ContextEvent], policy: ActivePolicy | None = None
) -> ContextEvent:
    if not events:
        raise ValueError("at least one event is required")
    return max(events, key=lambda event: authority_key(event, policy))


def sources_are_near_peers(
    left: ContextEvent,
    right: ContextEvent,
    policy: ActivePolicy | None = None,
) -> bool:
    active = policy or get_active_policy()
    return (
        abs(policy_for(left, active).rank - policy_for(right, active).rank)
        <= active.near_peer_max_authority_rank_gap
        and abs(effective_trust(left, active) - effective_trust(right, active))
        <= active.near_peer_max_trust_gap
    )
