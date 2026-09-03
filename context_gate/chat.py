"""Small, deterministic chat layer over ContextGate's bundled case catalog.

The engine deliberately does not use a model, embeddings, a vector database, or
the network.  It selects a few relevant cases, computes answers from their
decision records and evidence, and abstains when the catalog cannot support an
answer.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .authority import effective_trust, policy_for
from .decision_engine import evaluate_request
from .models import (
    ActionRequest,
    Classification,
    ContextEvent,
    DecisionRecord,
    EnforcementDecision,
    Sensitivity,
)
from .policy_config import ActivePolicy, get_active_policy
from .scenario import iter_scenarios

MAX_HISTORY_MESSAGES = 12
MAX_CITED_CASES = 3
MAX_ANSWER_CHARS = 2200

OUTCOME_LANGUAGE = {
    EnforcementDecision.ALLOW: (
        "allow",
        "allowed",
        "allowing",
        "allows",
        "green",
        "pass",
        "passed",
        "passing",
        "passed gate",
    ),
    EnforcementDecision.REVIEW: (
        "review",
        "reviewed",
        "reviewing",
        "reviews",
        "amber",
        "yellow",
        "attention",
        "needs attention",
        "need attention",
    ),
    EnforcementDecision.BLOCK: (
        "block",
        "blocked",
        "blocking",
        "blocks",
        "red",
        "stopped",
        "failed",
        "fail",
    ),
}


class GroundedChatAnswer(BaseModel):
    """Strict, machine-readable result for the local UI or another adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    text: str = Field(min_length=1, max_length=MAX_ANSWER_CHARS)
    case_ids: list[str] = Field(default_factory=list, max_length=MAX_CITED_CASES)
    evidence_event_ids: list[str] = Field(default_factory=list)
    rule_ids: list[str] = Field(default_factory=list)
    suggested_followups: list[str] = Field(default_factory=list, max_length=3)
    abstained: bool = False

    @field_validator(
        "case_ids", "evidence_event_ids", "rule_ids", "suggested_followups"
    )
    @classmethod
    def values_are_unique(cls, values: list[str]) -> list[str]:
        """Reject duplicated citations instead of silently presenting ambiguity."""

        if len(values) != len(dict.fromkeys(values)):
            raise ValueError("answer lists must contain unique values")
        return values


@dataclass(frozen=True, slots=True)
class IndexedCase:
    """One evaluated scenario and the evidence used to reach its outcome."""

    case_id: str
    name: str
    title: str
    description: str
    events: tuple[ContextEvent, ...]
    request: ActionRequest
    decision: DecisionRecord

    @property
    def aliases(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                alias
                for alias in (self.case_id, self.name, self.title)
                if alias.strip()
            )
        )


def _normalized(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()


def _phrase_pattern(value: object) -> str:
    return re.escape(_normalized(value)).replace(r"\ ", r"\s+")


def _unique(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _clip(value: object, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", str(value)).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _scenario_case_id(scenario: Any) -> str:
    """Honor a future explicit case ID while remaining compatible today."""

    for attribute in ("case_id", "id"):
        candidate = getattr(scenario, attribute, None)
        if candidate is not None and str(candidate).strip():
            return str(candidate).strip()
    return str(scenario.name)


def build_case_index(
    policy: ActivePolicy | None = None,
) -> tuple[IndexedCase, ...]:
    """Load and deterministically evaluate every currently registered scenario."""

    active_policy = policy or get_active_policy()
    indexed: list[IndexedCase] = []
    seen_ids: set[str] = set()
    for scenario in iter_scenarios():
        case_id = _scenario_case_id(scenario)
        if _normalized(case_id) in seen_ids:
            raise ValueError(f"duplicate chat case ID: {case_id}")
        seen_ids.add(_normalized(case_id))
        events, request = scenario.load()
        decision = evaluate_request(
            events,
            request,
            run_id=f"chat-{scenario.name}",
            policy=active_policy,
        )
        indexed.append(
            IndexedCase(
                case_id=case_id,
                name=scenario.name,
                title=scenario.title,
                description=scenario.description,
                events=tuple(events),
                request=request,
                decision=decision,
            )
        )
    return tuple(indexed)


def _history_rows(
    history: Sequence[Mapping[str, Any] | str] | None,
) -> list[str]:
    """Return only the bounded recent conversational context."""

    rows: list[str] = []
    recent = (history or ())[-MAX_HISTORY_MESSAGES:]
    for item in recent:
        if isinstance(item, str):
            content = item
        else:
            content = str(item.get("content") or item.get("text") or "")
            cited = item.get("case_ids", ())
            if isinstance(cited, Sequence) and not isinstance(cited, (str, bytes)):
                content = f"{content} {' '.join(str(value) for value in cited)}"
        if content.strip():
            rows.append(content.strip())
    return rows


class GroundedChatEngine:
    """Deterministic question answering over evaluated ContextGate cases."""

    def __init__(
        self,
        cases: Sequence[IndexedCase] | None = None,
        *,
        policy: ActivePolicy | None = None,
    ) -> None:
        self.policy = policy or get_active_policy()
        self.cases = tuple(
            cases if cases is not None else build_case_index(self.policy)
        )
        if not self.cases:
            raise ValueError("the grounded chat requires at least one case")

    def answer(
        self,
        question: str,
        history: Sequence[Mapping[str, Any] | str] | None = None,
    ) -> GroundedChatAnswer:
        """Answer a supported catalog question or explicitly abstain."""

        safe_question = _clip(question, 2000)
        if not safe_question:
            return self._abstain()

        normalized = _normalized(safe_question)
        explicit = self._cases_in_text(normalized, permissive=False)
        named_rules = self._named_rules(safe_question)
        rule_cases = self._cases_for_rules(named_rules)
        selected = self._limit_cases(explicit or rule_cases)
        if not selected and self._looks_like_followup(normalized):
            for row in reversed(_history_rows(history)):
                selected = self._limit_cases(
                    self._cases_in_text(_normalized(row), permissive=True)
                )
                if selected:
                    break

        if self._is_comparison(normalized):
            permissive = self._cases_in_text(normalized, permissive=True)
            return self._compare(self._limit_cases(permissive or selected))

        if named_rules and self._asks_for_cases_or_patterns(normalized):
            return self._rule_cases(named_rules)

        requested_outcome = self._requested_outcome(normalized)
        if requested_outcome and self._asks_for_outcome_overview(
            normalized,
            has_explicit_case=bool(explicit),
        ):
            return self._outcome_cases(requested_outcome)

        if self._asks_for_catalog_patterns(normalized):
            return self._catalog_patterns()

        if self._asks_for_related(normalized):
            if not selected:
                return self._abstain(
                    "Name a case before asking for cases with the same pattern."
                )
            return self._related(selected[0])

        if selected and normalized.startswith(("explain ", "describe ")):
            return self._explain(selected)

        if self._asks_for_evidence(normalized):
            if not selected:
                return self._abstain(
                    "Name a case before asking about its sources or evidence."
                )
            return self._evidence(selected)

        if self._asks_for_next_step(normalized):
            if not selected:
                return self._abstain(
                    "Name a case before asking for its safe next step."
                )
            return self._next_steps(selected)

        if selected:
            return self._explain(selected)

        return self._abstain()

    def _cases_in_text(
        self, normalized_text: str, *, permissive: bool
    ) -> list[IndexedCase]:
        scored: list[tuple[int, int, IndexedCase]] = []
        for position, case in enumerate(self.cases):
            best = 0
            for alias in case.aliases:
                normalized_alias = _normalized(alias)
                if not normalized_alias:
                    continue
                escaped = re.escape(normalized_alias).replace(r"\ ", r"\s+")
                if normalized_text == normalized_alias:
                    best = max(best, 100)
                if _normalized(case.title) == normalized_alias and re.search(
                    rf"\b{escaped}\b", normalized_text
                ):
                    best = max(best, 95)
                if re.search(
                    rf"\b(?:case|scenario|item|about|for|of|with|compare)\s+(?:the\s+)?{escaped}\b",
                    normalized_text,
                ):
                    best = max(best, 90)
                if re.search(
                    rf"\b{escaped}\s+(?:case|scenario|item)\b",
                    normalized_text,
                ):
                    best = max(best, 90)
                looks_like_id = bool(
                    re.fullmatch(r"[a-z]*\d+[a-z0-9]*", normalized_alias)
                )
                if looks_like_id and re.search(rf"\b{escaped}\b", normalized_text):
                    best = max(best, 92)
                if permissive and re.search(rf"\b{escaped}\b", normalized_text):
                    best = max(best, 70)
                if len(normalized_alias) >= 9 and re.search(
                    rf"\b{escaped}\b", normalized_text
                ):
                    best = max(best, 75)
            if best:
                scored.append((-best, position, case))
        scored.sort()
        return [case for _, _, case in scored]

    def _named_rules(self, question: str) -> list[str]:
        normalized = question.casefold()
        known = _unique(
            [
                rule
                for case in self.cases
                for rule in case.decision.deterministic_rule_ids
            ]
        )
        return [rule for rule in known if rule.casefold() in normalized]

    def _cases_for_rules(self, rules: Sequence[str]) -> list[IndexedCase]:
        selected = set(rules)
        return [
            case
            for case in self.cases
            if selected.intersection(case.decision.deterministic_rule_ids)
        ]

    @staticmethod
    def _limit_cases(cases: Sequence[IndexedCase]) -> list[IndexedCase]:
        result: list[IndexedCase] = []
        seen: set[str] = set()
        for case in cases:
            normalized_id = _normalized(case.case_id)
            if normalized_id in seen:
                continue
            seen.add(normalized_id)
            result.append(case)
            if len(result) == MAX_CITED_CASES:
                break
        return result

    @staticmethod
    def _looks_like_followup(normalized: str) -> bool:
        return normalized in {"why", "how", "what happened", "tell me more"} or bool(
            re.search(
                r"\b(it|its|that|this|those|them|they|the case|why did|what next|what should happen|more details)\b",
                normalized,
            )
        )

    @staticmethod
    def _is_comparison(normalized: str) -> bool:
        return bool(
            re.search(
                r"\b(compare|comparison|versus|vs|difference|different)\b", normalized
            )
        )

    @staticmethod
    def _asks_for_catalog_patterns(normalized: str) -> bool:
        return bool(
            re.search(
                r"\b(how many|counts?|distribution|breakdown|overall|catalog|all outcomes|rule patterns?)\b",
                normalized,
            )
        )

    @staticmethod
    def _asks_for_cases_or_patterns(normalized: str) -> bool:
        return bool(
            re.search(
                r"\b(which|show|list|patterns?|how many|count)\b",
                normalized,
            )
        )

    @classmethod
    def _asks_for_outcome_overview(
        cls,
        normalized: str,
        *,
        has_explicit_case: bool,
    ) -> bool:
        if cls._asks_for_cases_or_patterns(normalized):
            return True
        if has_explicit_case:
            return False
        return bool(
            re.search(
                r"\b(why (?:are|were)(?: there| the)?|why so many|go over|"
                r"walk me through|"
                r"what (?:was|were|got|is|failed|passed)|explain the)\b",
                normalized,
            )
            or re.search(r"\b(all|every)\b", normalized)
            or re.search(
                r"\b(items?|cases?|decisions?|outcomes?|actions?)\b.*\bwhy\b",
                normalized,
            )
        )

    @staticmethod
    def _asks_for_related(normalized: str) -> bool:
        return bool(
            re.search(r"\b(related|similar|same rule|share|in common)\b", normalized)
        )

    @staticmethod
    def _asks_for_evidence(normalized: str) -> bool:
        return bool(
            re.search(
                r"\b(source|sources|evidence|provenance|authoritative|authority|why did .* win|which .* won)\b",
                normalized,
            )
        )

    @staticmethod
    def _asks_for_next_step(normalized: str) -> bool:
        return bool(
            re.search(
                r"\b(next step|what next|should (?:i|we|the agent) do|proceed|fix|resolve|remediat|safely do)\b",
                normalized,
            )
            or "what should happen" in normalized
        )

    @staticmethod
    def _requested_outcome(normalized: str) -> EnforcementDecision | None:
        matches = [
            outcome
            for outcome, aliases in OUTCOME_LANGUAGE.items()
            if any(
                re.search(
                    rf"\b{_phrase_pattern(alias)}\b",
                    normalized,
                )
                for alias in aliases
            )
        ]
        return matches[0] if len(matches) == 1 else None

    def _catalog_patterns(self) -> GroundedChatAnswer:
        decision_counts = Counter(case.decision.decision.value for case in self.cases)
        class_counts = Counter(
            case.decision.classification.value for case in self.cases
        )
        rule_counts = Counter(
            rule for case in self.cases for rule in case.decision.deterministic_rule_ids
        )
        outcome_text = ", ".join(
            f"{outcome.value}={decision_counts[outcome.value]}"
            for outcome in EnforcementDecision
        )
        common_rules = ", ".join(
            f"{rule} ({count})"
            for rule, count in sorted(
                rule_counts.items(), key=lambda item: (-item[1], item[0])
            )[:5]
        )
        classes = ", ".join(
            f"{name}={count}" for name, count in sorted(class_counts.items())
        )
        representatives: list[IndexedCase] = []
        for outcome in EnforcementDecision:
            match = next(
                (case for case in self.cases if case.decision.decision == outcome),
                None,
            )
            if match:
                representatives.append(match)
        text = (
            f"The live catalog contains {len(self.cases)} cases: {outcome_text}. "
            f"Classifications are {classes}. Most frequent rules: "
            f"{common_rules or 'none'}. Counts are computed from the deterministic "
            "decision records, not inferred by a model."
        )
        return self._make_answer(
            text,
            representatives,
            suggestions=self._default_suggestions(representatives),
            additional_rules=list(rule_counts),
        )

    def _rule_cases(self, rules: Sequence[str]) -> GroundedChatAnswer:
        matches = self._cases_for_rules(rules)
        cited = matches[:MAX_CITED_CASES]
        descriptions = " ".join(
            f"[case {case.case_id}] {case.title}: {case.decision.decision.value}."
            for case in cited
        )
        rule_text = ", ".join(rules)
        text = f"{len(matches)} case(s) use {rule_text}. {descriptions}"
        if len(matches) > len(cited):
            text += f" Showing {len(cited)} representative cases."
        return self._make_answer(
            text,
            cited,
            suggestions=self._default_suggestions(cited),
            additional_rules=rules,
        )

    def _outcome_cases(self, outcome: EnforcementDecision) -> GroundedChatAnswer:
        matches = [case for case in self.cases if case.decision.decision == outcome]
        cited = matches[:MAX_CITED_CASES]
        if not matches:
            return self._make_answer(
                f"No current catalog case reaches {outcome.value}.",
                (),
                suggestions=["Show the overall outcome breakdown."],
            )
        lines = []
        for case in cited:
            evidence = ", ".join(case.decision.evidence_event_ids) or "none accepted"
            rules = ", ".join(case.decision.deterministic_rule_ids)
            lines.append(
                f"[case {case.case_id}] {case.title}: "
                f"{case.decision.classification.value}. "
                f"{_clip(case.decision.explanation, 220)} "
                f"Evidence: {evidence}. Rules: {rules}."
            )
        prefix = f"{len(matches)} case(s) reach {outcome.value}."
        if len(matches) > len(cited):
            prefix += f" Showing {len(cited)} representative cases."
        return self._make_answer(
            " ".join([prefix, *lines]),
            cited,
            suggestions=self._default_suggestions(cited),
        )

    def _explain(self, cases: Sequence[IndexedCase]) -> GroundedChatAnswer:
        lines: list[str] = []
        for case in cases:
            decision = case.decision
            value = self._display_value(case, decision.authoritative_value)
            lines.append(
                f"[case {case.case_id}] {case.title} is {decision.decision.value} "
                f"({decision.classification.value}, {decision.risk.value}). "
                f"{_clip(decision.explanation, 360)} "
                f"Authoritative value: {value}. Rules: "
                f"{', '.join(decision.deterministic_rule_ids)}."
            )
        return self._make_answer(
            " ".join(lines),
            cases,
            suggestions=self._default_suggestions(cases),
        )

    def _compare(self, cases: Sequence[IndexedCase]) -> GroundedChatAnswer:
        if len(cases) < 2:
            return self._abstain("Name at least two cases to compare.")
        lines = [
            f"[case {case.case_id}] {case.title}: {case.decision.decision.value}/"
            f"{case.decision.classification.value}; rules "
            f"{', '.join(case.decision.deterministic_rule_ids)}; "
            f"human approval={'yes' if case.decision.requires_human_approval else 'no'}."
            for case in cases
        ]
        outcomes = {case.decision.decision for case in cases}
        approvals = {case.decision.requires_human_approval for case in cases}
        if len(outcomes) > 1 and len(approvals) == 1:
            approval_text = (
                "all require approval"
                if approvals == {True}
                else "none require approval"
            )
            contrast = (
                "Their deterministic rules produce different enforcement outcomes, "
                f"even though {approval_text}."
            )
        elif len(outcomes) > 1:
            contrast = (
                "Their deterministic rules and approval requirements produce "
                "different enforcement outcomes."
            )
        else:
            contrast = (
                "They share an enforcement outcome, but their classifications or "
                "rules may differ."
            )
        return self._make_answer(
            " ".join([*lines, contrast]),
            cases,
            suggestions=self._default_suggestions(cases),
        )

    def _evidence(self, cases: Sequence[IndexedCase]) -> GroundedChatAnswer:
        lines: list[str] = []
        for case in cases:
            by_id = {event.event_id: event for event in case.events}
            cited_events = [
                by_id[event_id]
                for event_id in case.decision.evidence_event_ids
                if event_id in by_id
            ][:4]
            if not cited_events:
                lines.append(
                    f"[case {case.case_id}] No event was accepted as decision evidence."
                )
                continue
            redact = case.request.sensitivity != Sensitivity.PUBLIC
            details = "; ".join(
                self._event_summary(event, redact=redact) for event in cited_events
            )
            lines.append(f"[case {case.case_id}] {details}")
        return self._make_answer(
            " ".join(lines),
            cases,
            suggestions=self._default_suggestions(cases),
        )

    def _related(self, anchor: IndexedCase) -> GroundedChatAnswer:
        anchor_rules = set(anchor.decision.deterministic_rule_ids)
        same_rule = [
            case
            for case in self.cases
            if case.case_id != anchor.case_id
            and anchor_rules.intersection(case.decision.deterministic_rule_ids)
        ]
        same_outcome = [
            case
            for case in self.cases
            if case.case_id != anchor.case_id
            and case not in same_rule
            and case.decision.decision == anchor.decision.decision
        ]
        cases = self._limit_cases([anchor, *same_rule, *same_outcome])
        if len(cases) == 1:
            text = (
                f"[case {anchor.case_id}] has no other catalog case with the same "
                "rule or enforcement outcome."
            )
        else:
            related = ", ".join(
                f"[case {case.case_id}] {case.title}" for case in cases[1:]
            )
            text = (
                f"[case {anchor.case_id}] is related to {related}. "
                "Rule matches are preferred; otherwise the shared enforcement "
                f"outcome is {anchor.decision.decision.value}."
            )
        return self._make_answer(
            text,
            cases,
            suggestions=self._default_suggestions(cases),
        )

    def _next_steps(self, cases: Sequence[IndexedCase]) -> GroundedChatAnswer:
        lines = [
            f"[case {case.case_id}] {self._safe_next_step(case)}" for case in cases
        ]
        return self._make_answer(
            " ".join(lines),
            cases,
            suggestions=self._default_suggestions(cases),
        )

    @staticmethod
    def _safe_next_step(case: IndexedCase) -> str:
        decision = case.decision
        if decision.decision == EnforcementDecision.ALLOW:
            return (
                "Keep the action reversible and limited to the preview. Re-run the "
                "gate if the evidence changes or the action becomes consequential."
            )
        if decision.classification == Classification.INSUFFICIENT_EVIDENCE:
            return (
                "Do not execute yet. Supply the missing source identity, timestamps, "
                "content hash, and evidence reference, then re-run the gate."
            )
        if decision.classification == Classification.STALE:
            return (
                "Do not execute the older request. Refresh it from the current "
                "authoritative record and re-run the gate."
            )
        if decision.classification == Classification.CONFLICT:
            if decision.decision == EnforcementDecision.BLOCK:
                return (
                    "Do not execute the conflicting value. Keep the authoritative "
                    "value and verify the weaker source before creating a new request."
                )
            return (
                "Hold the action while a person compares the near-peer sources and "
                "records which value was verified."
            )
        if decision.classification == Classification.SENSITIVE:
            return (
                "Keep the value redacted and obtain explicit human approval through "
                "the review receipt before any external action."
            )
        if decision.decision == EnforcementDecision.REVIEW:
            return (
                "Present the exact value and evidence to a human and require an "
                "explicit approval receipt before execution."
            )
        return "Do not execute; correct the request or evidence and re-run the gate."

    @staticmethod
    def _display_value(case: IndexedCase, value: str | None) -> str:
        if value is None:
            return "none accepted"
        if case.request.sensitivity != Sensitivity.PUBLIC or any(
            event.sensitivity != Sensitivity.PUBLIC for event in case.events
        ):
            return "redacted sensitive value"
        return _clip(value, 120)

    def _event_summary(self, event: ContextEvent, *, redact: bool = False) -> str:
        value = (
            "redacted sensitive value"
            if redact or event.sensitivity != Sensitivity.PUBLIC
            else _clip(event.field_value, 100)
        )
        source = _clip(event.source_name or "source name missing", 100)
        authority = _clip(policy_for(event, self.policy).label, 100)
        reference = (
            "reference present"
            if event.evidence_uri or event.evidence_reference
            else "reference missing"
        )
        return (
            f"[event {event.event_id}] {source} ({authority}, effective trust "
            f"{effective_trust(event, self.policy):.2f}) reports {value}; {reference}."
        )

    def _make_answer(
        self,
        text: str,
        cases: Sequence[IndexedCase],
        *,
        suggestions: Sequence[str],
        additional_rules: Sequence[str] = (),
        abstained: bool = False,
    ) -> GroundedChatAnswer:
        cited_cases = self._limit_cases(cases)
        evidence = _unique(
            [
                event_id
                for case in cited_cases
                for event_id in case.decision.evidence_event_ids
            ]
        )
        rules = _unique(
            [
                rule
                for case in cited_cases
                for rule in case.decision.deterministic_rule_ids
            ]
            + list(additional_rules)
        )
        return GroundedChatAnswer(
            text=_clip(text, MAX_ANSWER_CHARS),
            case_ids=[case.case_id for case in cited_cases],
            evidence_event_ids=evidence,
            rule_ids=rules,
            suggested_followups=_unique(list(suggestions))[:3],
            abstained=abstained,
        )

    def _abstain(self, reason: str | None = None) -> GroundedChatAnswer:
        example = self.cases[0]
        text = reason or (
            "I cannot answer that from the current ContextGate case evidence. "
            "I will not invent outside facts."
        )
        return self._make_answer(
            text,
            (),
            suggestions=[
                "Show the overall outcome breakdown.",
                f"Explain case {example.case_id}.",
                f"Show the evidence for case {example.case_id}.",
            ],
            abstained=True,
        )

    @staticmethod
    def _default_suggestions(cases: Sequence[IndexedCase]) -> list[str]:
        if not cases:
            return ["Show the overall outcome breakdown."]
        first = cases[0].case_id
        suggestions = [
            f"Show the evidence for case {first}.",
            f"What is the safe next step for case {first}?",
        ]
        if len(cases) > 1:
            suggestions.append(
                f"Compare case {cases[0].case_id} with case {cases[1].case_id}."
            )
        else:
            suggestions.append(f"Which cases are related to case {first}?")
        return suggestions


def answer_question(
    question: str,
    history: Sequence[Mapping[str, Any] | str] | None = None,
) -> GroundedChatAnswer:
    """Convenience entry point for callers that do not retain an engine."""

    return GroundedChatEngine().answer(question, history)
