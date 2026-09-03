"""Deterministic source authority policy; producers cannot self-assign authority."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from .models import ContextEvent, EvidenceStatus


@dataclass(frozen=True, slots=True)
class AuthorityPolicy:
    rank: int
    trust_cap: float
    label: str


SOURCE_AUTHORITY: dict[str, AuthorityPolicy] = {
    "registration_confirmation": AuthorityPolicy(
        100, 1.00, "Official registration confirmation"
    ),
    "organizer_api": AuthorityPolicy(98, 1.00, "Organizer-controlled API"),
    "organizer_website": AuthorityPolicy(95, 0.98, "Organizer website"),
    "official_email": AuthorityPolicy(92, 0.98, "Verified organizer email"),
    "partner_website": AuthorityPolicy(70, 0.85, "Named event partner"),
    "copied_webpage": AuthorityPolicy(50, 0.70, "Copied or community webpage"),
    "user_report": AuthorityPolicy(40, 0.65, "Unverified user report"),
    "unknown": AuthorityPolicy(10, 0.40, "Unknown source"),
}


def policy_for(event: ContextEvent) -> AuthorityPolicy:
    return SOURCE_AUTHORITY.get(
        event.source_type or "unknown", SOURCE_AUTHORITY["unknown"]
    )


def effective_trust(event: ContextEvent) -> float:
    return min(event.trust_score, policy_for(event).trust_cap)


def authority_key(event: ContextEvent) -> tuple[int, int, float, datetime, datetime]:
    earliest = datetime.min.replace(tzinfo=UTC)
    status_weight = {
        EvidenceStatus.CONFIRMED: 3,
        EvidenceStatus.UNVERIFIED: 2,
        EvidenceStatus.INFERRED: 1,
        EvidenceStatus.UNKNOWN: 0,
    }[event.status]
    return (
        policy_for(event).rank,
        status_weight,
        effective_trust(event),
        event.effective_at or earliest,
        event.observed_at or earliest,
    )


def choose_authoritative(events: list[ContextEvent]) -> ContextEvent:
    if not events:
        raise ValueError("at least one event is required")
    return max(events, key=authority_key)


def sources_are_near_peers(left: ContextEvent, right: ContextEvent) -> bool:
    return (
        abs(policy_for(left).rank - policy_for(right).rank) <= 5
        and abs(effective_trust(left) - effective_trust(right)) <= 0.10
    )
