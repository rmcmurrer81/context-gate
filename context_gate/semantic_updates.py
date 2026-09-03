"""Deterministic interpretation of configured quantitative company details.

This module intentionally handles a constrained language subset.  It does not
attempt general natural-language understanding, perform lookups, persist state,
or execute an update.  Callers provide structured entity identity alongside a
bounded excerpt; uncertain identity or wording is routed to human review.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_TEXT_LENGTH = 8_192
MAX_IDENTITY_KEYS = 16
MAX_IDENTITY_CANDIDATES = 5
MAX_QUANTITY_FIELDS = 16
MAX_TERMS = 32
MAX_QUANTITY = 999_999_999

SemanticKey = Annotated[
    str,
    Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]{0,63}$"),
]
BoundedText = Annotated[str, Field(min_length=1, max_length=512)]
IdentityCandidate = str | list[str]


class _SemanticModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
        hide_input_in_errors=True,
        validate_assignment=True,
    )


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include an explicit timezone")
    return value.astimezone(UTC)


def _contains_unsafe_control(value: str) -> bool:
    return any(
        unicodedata.category(character) == "Cc" and character not in {"\t", "\n", "\r"}
        for character in value
    )


def _comparison_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _validate_visible_text(value: str, *, allow_lines: bool = False) -> str:
    if _contains_unsafe_control(value):
        raise ValueError("must not contain unsupported control characters")
    if not allow_lines and any(character in value for character in "\r\n"):
        raise ValueError("must fit on one line")
    return value


def _validate_unique_terms(terms: list[str]) -> list[str]:
    normalized = [_comparison_text(term) for term in terms]
    if any(not term for term in normalized):
        raise ValueError("terms must contain visible text")
    if len(set(normalized)) != len(normalized):
        raise ValueError("terms must be unique after normalization")
    for term in terms:
        _validate_visible_text(term)
    return terms


class QuantityMode(StrEnum):
    TOTAL = "TOTAL"
    DELTA = "DELTA"
    AMBIGUOUS = "AMBIGUOUS"


class ProposalOutcome(StrEnum):
    PROPOSE = "PROPOSE"
    REVIEW = "REVIEW"


class ContributionKind(StrEnum):
    DETERMINISTIC_INTERPRETATION = "DETERMINISTIC_INTERPRETATION"
    HUMAN_CORRECTION = "HUMAN_CORRECTION"


class QuantityFieldConfig(_SemanticModel):
    """Company vocabulary and safety bounds for one quantitative detail."""

    field_name: SemanticKey
    metric_nouns: list[BoundedText] = Field(min_length=1, max_length=MAX_TERMS)
    delta_markers: list[BoundedText] = Field(min_length=1, max_length=MAX_TERMS)
    total_markers: list[BoundedText] = Field(min_length=1, max_length=MAX_TERMS)
    status_markers: list[BoundedText] = Field(min_length=1, max_length=MAX_TERMS)
    negation_markers: list[BoundedText] = Field(
        default_factory=lambda: [
            "not",
            "no longer",
            "cancelled",
            "canceled",
            "void",
            "ignore",
        ],
        min_length=1,
        max_length=MAX_TERMS,
    )
    maximum_plausible_value: int = Field(ge=0, le=MAX_QUANTITY)
    maximum_absolute_change: int = Field(ge=0, le=MAX_QUANTITY)

    _validate_nouns = field_validator("metric_nouns")(_validate_unique_terms)
    _validate_delta = field_validator("delta_markers")(_validate_unique_terms)
    _validate_total = field_validator("total_markers")(_validate_unique_terms)
    _validate_status = field_validator("status_markers")(_validate_unique_terms)
    _validate_negation = field_validator("negation_markers")(_validate_unique_terms)

    @model_validator(mode="after")
    def mode_markers_do_not_overlap(self) -> QuantityFieldConfig:
        delta = {_comparison_text(item) for item in self.delta_markers}
        total = {_comparison_text(item) for item in self.total_markers}
        status = {_comparison_text(item) for item in self.status_markers}
        negation = {_comparison_text(item) for item in self.negation_markers}
        if delta & total:
            raise ValueError("delta and total markers must not overlap")
        if negation & (delta | total | status):
            raise ValueError("negation markers must not overlap positive markers")
        return self


class CategorySemanticConfig(_SemanticModel):
    """Company-selected identity and important-detail rules for one category."""

    category: SemanticKey
    identity_keys: list[SemanticKey] = Field(
        min_length=1,
        max_length=MAX_IDENTITY_KEYS,
    )
    important_fields: list[SemanticKey] = Field(
        min_length=1,
        max_length=MAX_QUANTITY_FIELDS,
    )
    quantity_fields: list[QuantityFieldConfig] = Field(
        min_length=1,
        max_length=MAX_QUANTITY_FIELDS,
    )

    @field_validator("identity_keys", "important_fields")
    @classmethod
    def keys_are_unique(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("keys must be unique")
        return value

    @model_validator(mode="after")
    def quantity_fields_are_declared_important(self) -> CategorySemanticConfig:
        field_names = [item.field_name for item in self.quantity_fields]
        if len(set(field_names)) != len(field_names):
            raise ValueError("quantity field names must be unique")
        undeclared = set(field_names) - set(self.important_fields)
        if undeclared:
            raise ValueError("every quantity field must be declared important")
        return self


class EntityQuantityState(_SemanticModel):
    """Previously accepted state supplied by the caller; never mutated here."""

    category: SemanticKey
    entity_id: str = Field(min_length=1, max_length=128)
    identity: dict[SemanticKey, BoundedText] = Field(
        min_length=1,
        max_length=MAX_IDENTITY_KEYS,
    )
    quantity_values: dict[SemanticKey, int] = Field(
        default_factory=dict,
        max_length=MAX_QUANTITY_FIELDS,
    )
    as_of: datetime

    _normalize_as_of = field_validator("as_of", mode="after")(_aware_utc)

    @field_validator("entity_id")
    @classmethod
    def entity_id_is_safe(cls, value: str) -> str:
        return _validate_visible_text(value)

    @field_validator("identity")
    @classmethod
    def identity_is_safe(cls, value: dict[str, str]) -> dict[str, str]:
        for item in value.values():
            _validate_visible_text(item)
        return value

    @field_validator("quantity_values")
    @classmethod
    def quantities_are_bounded(cls, value: dict[str, int]) -> dict[str, int]:
        if any(
            isinstance(item, bool) or not 0 <= item <= MAX_QUANTITY
            for item in value.values()
        ):
            raise ValueError("quantity values must be bounded non-negative integers")
        return value


class IncomingQuantityStatement(_SemanticModel):
    """A bounded excerpt plus caller-extracted identity candidates."""

    evidence_id: str = Field(min_length=1, max_length=128)
    category: SemanticKey
    identity: dict[SemanticKey, IdentityCandidate] = Field(
        default_factory=dict,
        max_length=MAX_IDENTITY_KEYS,
    )
    source_type: SemanticKey = "unspecified"
    evidence_reference: str | None = Field(default=None, max_length=2_048)
    text: str = Field(min_length=1, max_length=MAX_TEXT_LENGTH)
    observed_at: datetime

    _normalize_observed_at = field_validator("observed_at", mode="after")(_aware_utc)

    @field_validator("evidence_id", "evidence_reference")
    @classmethod
    def reference_text_is_safe(cls, value: str | None) -> str | None:
        return _validate_visible_text(value) if value is not None else None

    @field_validator("text")
    @classmethod
    def text_is_bounded_and_safe(cls, value: str) -> str:
        return _validate_visible_text(value, allow_lines=True)

    @field_validator("identity")
    @classmethod
    def identity_candidates_are_safe(
        cls,
        value: dict[str, IdentityCandidate],
    ) -> dict[str, IdentityCandidate]:
        for candidate in value.values():
            candidates = [candidate] if isinstance(candidate, str) else candidate
            if not candidates or len(candidates) > MAX_IDENTITY_CANDIDATES:
                raise ValueError(
                    "identity candidate lists must contain between one and five items"
                )
            for item in candidates:
                if not item or len(item) > 512:
                    raise ValueError("identity candidates must be bounded visible text")
                _validate_visible_text(item)
        return value


class ProposalReason(_SemanticModel):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z0-9_]+$")
    detail: str = Field(min_length=1, max_length=1_000)


class CalculationContribution(_SemanticModel):
    """One bounded, attributable input to a displayed calculation."""

    kind: ContributionKind
    evidence_id: str = Field(min_length=1, max_length=128)
    evidence_reference: str | None = Field(default=None, max_length=2_048)
    source_type: SemanticKey
    content_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    interpreted_excerpt: str = Field(min_length=1, max_length=320)
    mode: QuantityMode
    stated_quantity: int | None = Field(default=None, ge=0, le=MAX_QUANTITY)
    prior_total: int | None = Field(default=None, ge=0, le=MAX_QUANTITY)
    resulting_total: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def explicit_modes_require_a_quantity(self) -> CalculationContribution:
        if self.mode != QuantityMode.AMBIGUOUS and self.stated_quantity is None:
            raise ValueError("an explicit contribution mode requires a quantity")
        if self.resulting_total is not None and self.stated_quantity is None:
            raise ValueError("a resulting total requires a stated quantity")
        return self


class CalculationTrace(_SemanticModel):
    """Compact derivation suitable for answering "how was this total derived?"""

    formula: str = Field(min_length=1, max_length=256)
    contributions: list[CalculationContribution] = Field(min_length=1, max_length=64)
    explanation: str = Field(min_length=1, max_length=1_000)


class SemanticUpdateProposal(_SemanticModel):
    """Advisory interpretation; all side-effect flags are hard-false."""

    outcome: ProposalOutcome
    entity_id: str = Field(min_length=1, max_length=128)
    evidence_id: str = Field(min_length=1, max_length=128)
    evidence_reference: str | None = Field(default=None, max_length=2_048)
    source_type: SemanticKey
    input_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    config_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    state_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    mode: QuantityMode
    field_name: SemanticKey | None = None
    field_is_important: bool
    stated_quantity: int | None = Field(default=None, ge=0, le=MAX_QUANTITY)
    prior_total: int | None = Field(default=None, ge=0, le=MAX_QUANTITY)
    proposed_total: int | None = Field(default=None, ge=0)
    identity_matched: bool
    matched_identity: dict[SemanticKey, BoundedText] = Field(
        max_length=MAX_IDENTITY_KEYS
    )
    reasons: list[ProposalReason] = Field(min_length=1, max_length=32)
    human_questions: list[str] = Field(max_length=16)
    calculation_trace: CalculationTrace
    summary: str = Field(min_length=1, max_length=1_000)
    automatic_lookup_performed: Literal[False] = False
    state_updated: Literal[False] = False
    external_action_executed: Literal[False] = False


class HumanQuantityCorrection(_SemanticModel):
    """Append-only human correction of the interpreted field, mode, or number."""

    correction_id: str = Field(min_length=1, max_length=128)
    field_name: SemanticKey
    mode: QuantityMode
    quantity: int = Field(ge=0, le=MAX_QUANTITY)
    reviewer: str = Field(min_length=1, max_length=128)
    rationale: str = Field(min_length=3, max_length=1_000)
    created_at: datetime
    evidence_reference: str | None = Field(default=None, max_length=2_048)

    _normalize_created_at = field_validator("created_at", mode="after")(_aware_utc)

    @field_validator("correction_id", "reviewer", "rationale", "evidence_reference")
    @classmethod
    def correction_text_is_safe(cls, value: str | None) -> str | None:
        return _validate_visible_text(value) if value is not None else None

    @model_validator(mode="after")
    def correction_mode_must_be_explicit(self) -> HumanQuantityCorrection:
        if self.mode == QuantityMode.AMBIGUOUS:
            raise ValueError("a human correction must explicitly choose TOTAL or DELTA")
        return self


class CorrectedSemanticUpdateProposal(_SemanticModel):
    """A recalculated result that preserves the original and every correction."""

    original_proposal: SemanticUpdateProposal
    corrections: list[HumanQuantityCorrection] = Field(min_length=1, max_length=32)
    outcome: ProposalOutcome
    entity_id: str = Field(min_length=1, max_length=128)
    config_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    state_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    mode: QuantityMode
    field_name: SemanticKey
    prior_total: int | None = Field(default=None, ge=0, le=MAX_QUANTITY)
    corrected_quantity: int = Field(ge=0, le=MAX_QUANTITY)
    proposed_total: int | None = Field(default=None, ge=0)
    identity_matched: bool
    matched_identity: dict[SemanticKey, BoundedText] = Field(
        max_length=MAX_IDENTITY_KEYS
    )
    reasons: list[ProposalReason] = Field(min_length=1, max_length=32)
    human_questions: list[str] = Field(max_length=16)
    calculation_trace: CalculationTrace
    summary: str = Field(min_length=1, max_length=1_000)
    automatic_lookup_performed: Literal[False] = False
    state_updated: Literal[False] = False
    external_action_executed: Literal[False] = False

    @field_validator("corrections")
    @classmethod
    def correction_history_is_append_ordered(
        cls,
        value: list[HumanQuantityCorrection],
    ) -> list[HumanQuantityCorrection]:
        identifiers = [item.correction_id for item in value]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("correction identifiers must be unique")
        timestamps = [item.created_at for item in value]
        if timestamps != sorted(timestamps):
            raise ValueError("corrections must be in chronological append order")
        return value


class _TextInterpretation:
    __slots__ = ("codes", "field_name", "mode", "quantity")

    def __init__(
        self,
        field_name: str | None,
        mode: QuantityMode,
        quantity: int | None,
        codes: list[str],
    ) -> None:
        self.field_name = field_name
        self.mode = mode
        self.quantity = quantity
        self.codes = codes


_CLAUSE_SPLIT = re.compile(r"(?<!\d)[.!?;]|[.!?;](?!\d)|[\r\n]+")
_NUMBER = re.compile(r"(?<![\w])(?:\d{1,3}(?:,\d{3})+|\d{1,9})(?![\w])")
_DECIMAL_NUMBER = re.compile(r"(?<![\w])\d+[.]\d+(?![\w])")
_NEGATIVE_NUMBER = re.compile(r"(?<![\w])[-−﹣－]\s*\d")
_UNSUPPORTED_DECREASE = re.compile(
    r"(?<!\w)(?:fewer|less|minus|decrease|decreased|decreasing|down\s+by)(?!\w)"
)


def _contains_phrase(text: str, phrase: str) -> bool:
    normalized = _comparison_text(phrase)
    if normalized == "+":
        return "+" in text
    pattern = rf"(?<!\w){re.escape(normalized)}(?!\w)"
    return re.search(pattern, text) is not None


def _statement_comparison_text(value: str) -> str:
    """Normalize comparison text without discarding clause-separating newlines."""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[\t\v\f ]+", " ", normalized)


def _field_interpretation(
    rule: QuantityFieldConfig,
    normalized_text: str,
) -> tuple[bool, list[tuple[QuantityMode, int]], list[str]]:
    clauses = [item.strip() for item in _CLAUSE_SPLIT.split(normalized_text)]
    relevant = [
        clause
        for clause in clauses
        if clause and any(_contains_phrase(clause, noun) for noun in rule.metric_nouns)
    ]
    if not relevant:
        return False, [], []

    codes: list[str] = []
    interpretations: list[tuple[QuantityMode, int]] = []
    if any(
        _contains_phrase(normalized_text, marker) for marker in rule.negation_markers
    ):
        codes.append("NEGATED_OR_CANCELLED")

    for clause in relevant:
        raw_numbers = _NUMBER.findall(clause)
        numbers = [int(item.replace(",", "")) for item in raw_numbers]
        if _DECIMAL_NUMBER.search(clause):
            codes.append("NON_INTEGER_QUANTITY")
            continue
        if _NEGATIVE_NUMBER.search(clause) or _UNSUPPORTED_DECREASE.search(clause):
            codes.append("UNSUPPORTED_DECREASE_WORDING")
            continue
        if not numbers:
            codes.append("QUANTITY_MISSING")
            continue
        if len(set(numbers)) > 1:
            codes.append("MULTIPLE_CONFLICTING_QUANTITIES")
            continue

        quantity = numbers[0]
        if quantity > MAX_QUANTITY:
            codes.append("QUANTITY_EXCEEDS_PARSER_LIMIT")
            continue
        has_delta = (
            any(_contains_phrase(clause, marker) for marker in rule.delta_markers)
            or re.search(rf"\+\s*{re.escape(raw_numbers[0])}(?!\d)", clause) is not None
        )
        has_total = any(
            _contains_phrase(clause, marker) for marker in rule.total_markers
        )
        has_status = any(
            _contains_phrase(clause, marker) for marker in rule.status_markers
        )
        if has_delta and has_total:
            codes.append("CONFLICTING_MODE_MARKERS")
        elif has_delta:
            interpretations.append((QuantityMode.DELTA, quantity))
        elif has_total or has_status:
            interpretations.append((QuantityMode.TOTAL, quantity))
        else:
            codes.append("UNSUPPORTED_QUANTITY_WORDING")

    return True, interpretations, _stable_unique(codes)


def _interpret_text(
    config: CategorySemanticConfig,
    text: str,
) -> _TextInterpretation:
    normalized = _statement_comparison_text(text)
    matches: list[
        tuple[QuantityFieldConfig, list[tuple[QuantityMode, int]], list[str]]
    ] = []
    for rule in config.quantity_fields:
        mentioned, interpretations, codes = _field_interpretation(rule, normalized)
        if mentioned:
            matches.append((rule, interpretations, codes))

    if not matches:
        return _TextInterpretation(
            None, QuantityMode.AMBIGUOUS, None, ["METRIC_NOT_FOUND"]
        )
    if len(matches) > 1:
        return _TextInterpretation(
            None,
            QuantityMode.AMBIGUOUS,
            None,
            ["MULTIPLE_IMPORTANT_FIELDS"],
        )

    rule, interpretations, codes = matches[0]
    unique = set(interpretations)
    if codes or len(unique) != 1:
        if len(unique) > 1:
            codes.append("MULTIPLE_CONFLICTING_QUANTITIES")
        if not codes:
            codes.append("UNSUPPORTED_QUANTITY_WORDING")
        return _TextInterpretation(
            rule.field_name,
            QuantityMode.AMBIGUOUS,
            None,
            _stable_unique(codes),
        )
    mode, quantity = unique.pop()
    return _TextInterpretation(rule.field_name, mode, quantity, [])


def _identity_check(
    config: CategorySemanticConfig,
    current: EntityQuantityState,
    incoming: IncomingQuantityStatement,
) -> tuple[bool, dict[str, str], list[str]]:
    matched: dict[str, str] = {}
    codes: list[str] = []
    for key in config.identity_keys:
        current_value = current.identity.get(key)
        incoming_value = incoming.identity.get(key)
        if current_value is None or incoming_value is None:
            codes.append("IDENTITY_MISSING")
            continue
        raw_candidates = (
            [incoming_value] if isinstance(incoming_value, str) else incoming_value
        )
        normalized_candidates = {
            _comparison_text(candidate): candidate for candidate in raw_candidates
        }
        if len(normalized_candidates) != 1:
            codes.append("IDENTITY_AMBIGUOUS")
            continue
        if _comparison_text(current_value) not in normalized_candidates:
            codes.append("IDENTITY_MISMATCH")
            continue
        matched[key] = current_value
    return (
        len(matched) == len(config.identity_keys) and not codes,
        matched,
        _stable_unique(codes),
    )


_REASON_DETAILS = {
    "CATEGORY_MISMATCH": "The configured category, current state, and incoming category do not match.",
    "IDENTITY_MISSING": "One or more configured identity keys are missing.",
    "IDENTITY_AMBIGUOUS": "An identity key has more than one distinct candidate.",
    "IDENTITY_MISMATCH": "The incoming identity does not exactly match the current entity identity.",
    "STALE_STATEMENT": "The incoming statement predates the current accepted state.",
    "METRIC_NOT_FOUND": "No configured metric noun was found in the bounded statement.",
    "MULTIPLE_IMPORTANT_FIELDS": "The statement mentions more than one configured quantitative field.",
    "NEGATED_OR_CANCELLED": "The statement contains configured negation or cancellation wording.",
    "QUANTITY_MISSING": "A configured metric is mentioned without a supported numeric quantity.",
    "MULTIPLE_CONFLICTING_QUANTITIES": "More than one conflicting quantity or interpretation is present.",
    "CONFLICTING_MODE_MARKERS": "Both incremental and replacement-total markers are present.",
    "UNSUPPORTED_QUANTITY_WORDING": "The wording does not deterministically identify a delta or total.",
    "NON_INTEGER_QUANTITY": "The constrained interpreter accepts only whole-number quantities.",
    "UNSUPPORTED_DECREASE_WORDING": "A decrease or negative quantity requires explicit human interpretation.",
    "QUANTITY_EXCEEDS_PARSER_LIMIT": "The stated quantity exceeds the interpreter's bounded numeric limit.",
    "PRIOR_TOTAL_MISSING": "An incremental statement cannot be applied without a trusted prior total.",
    "PRIOR_TOTAL_OUT_OF_BOUNDS": "The prior total exceeds the configured plausible maximum.",
    "PROPOSED_TOTAL_OUT_OF_BOUNDS": "The candidate total exceeds the configured plausible maximum.",
    "CHANGE_EXCEEDS_LIMIT": "The candidate change exceeds the configured absolute-change limit.",
    "IDENTITY_MATCHED": "All configured identity keys exactly match the current entity.",
    "INTERPRETED_AS_TOTAL": "The number is interpreted as a replacement total.",
    "INTERPRETED_AS_DELTA": "The number is interpreted as an increment to the prior total.",
    "WITHIN_CONFIGURED_BOUNDS": "The candidate total and change are within configured bounds.",
    "HUMAN_CORRECTION_APPLIED": "An explicit human correction replaces the earlier language interpretation while preserving it in the record.",
    "CORRECTED_FIELD_NOT_CONFIGURED": "The corrected field is not a configured quantitative important field.",
    "IDENTITY_NOT_VERIFIED_AFTER_CORRECTION": "A language correction cannot replace the required entity-identity verification.",
    "CURRENT_STATE_MISMATCH_AFTER_CORRECTION": "The supplied state is not the exact state snapshot bound to the original proposal.",
    "CORRECTION_PREDATES_STATE": "The correction timestamp predates the state snapshot it would recalculate.",
    "CONFIG_MISMATCH_AFTER_CORRECTION": "The supplied semantic configuration differs from the one bound to the original proposal.",
}


def _questions_for(codes: list[str], identity_keys: list[str]) -> list[str]:
    questions: list[str] = []
    identity_label = ", ".join(identity_keys)
    if any(
        code.startswith("IDENTITY_") or code == "CATEGORY_MISMATCH" for code in codes
    ):
        questions.append(
            f"Which single entity is this, using the configured identity keys: {identity_label}?"
        )
    if any(
        code
        in {
            "METRIC_NOT_FOUND",
            "MULTIPLE_IMPORTANT_FIELDS",
            "QUANTITY_MISSING",
            "MULTIPLE_CONFLICTING_QUANTITIES",
            "CONFLICTING_MODE_MARKERS",
            "UNSUPPORTED_QUANTITY_WORDING",
            "NON_INTEGER_QUANTITY",
            "UNSUPPORTED_DECREASE_WORDING",
            "QUANTITY_EXCEEDS_PARSER_LIMIT",
        }
        for code in codes
    ):
        questions.append(
            "Does the stated number replace the current total, or is it an amount to add?"
        )
    if "NEGATED_OR_CANCELLED" in codes:
        questions.append(
            "Should this negated or cancelled statement be ignored, and which stronger source confirms that?"
        )
    if "PRIOR_TOTAL_MISSING" in codes:
        questions.append("What trusted prior total should the increment use?")
    if "CORRECTED_FIELD_NOT_CONFIGURED" in codes:
        questions.append("Which configured important field should this correction use?")
    if "IDENTITY_NOT_VERIFIED_AFTER_CORRECTION" in codes:
        questions.append(
            f"Which single entity is this, using the configured identity keys: {identity_label}?"
        )
    if "CURRENT_STATE_MISMATCH_AFTER_CORRECTION" in codes:
        questions.append(
            "Can the correction be reapplied to a fresh proposal for the current exact state snapshot?"
        )
    if "CORRECTION_PREDATES_STATE" in codes:
        questions.append(
            "Can the reviewer create a new timestamped correction for the current state?"
        )
    if "CONFIG_MISMATCH_AFTER_CORRECTION" in codes:
        questions.append(
            "Can the correction be reapplied to a fresh proposal using the active semantic configuration?"
        )
    if any(
        code
        in {
            "PRIOR_TOTAL_OUT_OF_BOUNDS",
            "PROPOSED_TOTAL_OUT_OF_BOUNDS",
            "CHANGE_EXCEEDS_LIMIT",
            "STALE_STATEMENT",
        }
        for code in codes
    ):
        questions.append(
            "Can an approved, newer source confirm this unexpected value before any update?"
        )
    return _stable_unique(questions)


def _bounded_interpreted_excerpt(
    incoming: IncomingQuantityStatement,
    field_rule: QuantityFieldConfig | None,
) -> str:
    for raw_clause in _CLAUSE_SPLIT.split(incoming.text):
        excerpt = " ".join(raw_clause.split())
        normalized = _comparison_text(excerpt)
        is_relevant = field_rule is None or any(
            _contains_phrase(normalized, noun) for noun in field_rule.metric_nouns
        )
        if excerpt and is_relevant:
            if len(excerpt) <= 320:
                return excerpt
            return f"{excerpt[:317].rstrip()}..."
    return "Bounded input contained no displayable quantitative clause"


def _formula(
    mode: QuantityMode,
    prior: int | None,
    quantity: int | None,
    result: int | None,
) -> str:
    if quantity is None:
        return "AMBIGUOUS: no candidate total"
    if result is None:
        return f"{mode.value} {quantity}: no candidate total"
    if mode == QuantityMode.DELTA and prior is not None:
        return f"{prior} + {quantity} = {result}"
    return f"TOTAL {quantity} = {result}"


def _statement_trace(
    incoming: IncomingQuantityStatement,
    field_rule: QuantityFieldConfig | None,
    mode: QuantityMode,
    quantity: int | None,
    prior: int | None,
    result: int | None,
    input_digest: str,
) -> CalculationTrace:
    contribution = CalculationContribution(
        kind=ContributionKind.DETERMINISTIC_INTERPRETATION,
        evidence_id=incoming.evidence_id,
        evidence_reference=incoming.evidence_reference,
        source_type=incoming.source_type,
        content_digest=input_digest,
        interpreted_excerpt=_bounded_interpreted_excerpt(incoming, field_rule),
        mode=mode,
        stated_quantity=quantity,
        prior_total=prior,
        resulting_total=result,
    )
    if mode == QuantityMode.DELTA:
        mode_explanation = (
            "The configured delta marker identified an increment; review gates may "
            "still prevent a candidate total."
        )
    elif mode == QuantityMode.TOTAL:
        mode_explanation = (
            "The configured total or status marker identified a replacement total; "
            "review gates may still prevent a candidate total."
        )
    else:
        mode_explanation = (
            "The constrained interpreter abstained; the input digest and bounded "
            "excerpt are retained for review."
        )
    return CalculationTrace(
        formula=_formula(mode, prior, quantity, result),
        contributions=[contribution],
        explanation=mode_explanation,
    )


def _stable_unique(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _state_digest(current: EntityQuantityState) -> str:
    payload = json.dumps(
        current.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _model_fingerprint(model: _SemanticModel) -> str:
    payload = json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def interpret_quantity_update(
    config: CategorySemanticConfig,
    current: EntityQuantityState,
    incoming: IncomingQuantityStatement,
) -> SemanticUpdateProposal:
    """Interpret one bounded statement without looking up, storing, or acting.

    ``PROPOSE`` means only that this constrained interpreter produced a candidate
    for the caller's normal evidence and approval policy.  It never means an
    external change was executed.
    """

    if not isinstance(config, CategorySemanticConfig):
        raise TypeError("config must be a validated CategorySemanticConfig")
    if not isinstance(current, EntityQuantityState):
        raise TypeError("current must be a validated EntityQuantityState")
    if not isinstance(incoming, IncomingQuantityStatement):
        raise TypeError("incoming must be a validated IncomingQuantityStatement")

    blocker_codes: list[str] = []
    categories_match = config.category == current.category == incoming.category
    if not categories_match:
        blocker_codes.append("CATEGORY_MISMATCH")

    identity_matched, matched_identity, identity_codes = _identity_check(
        config,
        current,
        incoming,
    )
    blocker_codes.extend(identity_codes)
    if incoming.observed_at < current.as_of:
        blocker_codes.append("STALE_STATEMENT")

    text_result = _interpret_text(config, incoming.text)
    blocker_codes.extend(text_result.codes)
    prior_total = (
        current.quantity_values.get(text_result.field_name)
        if text_result.field_name is not None
        else None
    )
    proposed_total: int | None = None

    field_rule = next(
        (
            item
            for item in config.quantity_fields
            if item.field_name == text_result.field_name
        ),
        None,
    )
    if (
        field_rule is not None
        and prior_total is not None
        and prior_total > field_rule.maximum_plausible_value
    ):
        blocker_codes.append("PRIOR_TOTAL_OUT_OF_BOUNDS")

    identity_ready = (
        categories_match and identity_matched and incoming.observed_at >= current.as_of
    )
    if (
        identity_ready
        and field_rule is not None
        and text_result.quantity is not None
        and text_result.mode != QuantityMode.AMBIGUOUS
    ):
        if text_result.mode == QuantityMode.DELTA:
            if prior_total is None:
                blocker_codes.append("PRIOR_TOTAL_MISSING")
            else:
                proposed_total = prior_total + text_result.quantity
        else:
            proposed_total = text_result.quantity

    if proposed_total is not None and field_rule is not None:
        if proposed_total > field_rule.maximum_plausible_value:
            blocker_codes.append("PROPOSED_TOTAL_OUT_OF_BOUNDS")
        if (
            prior_total is not None
            and abs(proposed_total - prior_total) > field_rule.maximum_absolute_change
        ):
            blocker_codes.append("CHANGE_EXCEEDS_LIMIT")

    blocker_codes = _stable_unique(blocker_codes)
    if blocker_codes:
        reasons = [
            ProposalReason(code=code, detail=_REASON_DETAILS[code])
            for code in blocker_codes
        ]
        outcome = ProposalOutcome.REVIEW
        summary = (
            "Human review or stronger confirmation is required. No lookup, state "
            "change, or external action was performed."
        )
    else:
        interpretation_code = (
            "INTERPRETED_AS_DELTA"
            if text_result.mode == QuantityMode.DELTA
            else "INTERPRETED_AS_TOTAL"
        )
        reasons = [
            ProposalReason(
                code="IDENTITY_MATCHED",
                detail=_REASON_DETAILS["IDENTITY_MATCHED"],
            ),
            ProposalReason(
                code=interpretation_code,
                detail=_REASON_DETAILS[interpretation_code],
            ),
            ProposalReason(
                code="WITHIN_CONFIGURED_BOUNDS",
                detail=_REASON_DETAILS["WITHIN_CONFIGURED_BOUNDS"],
            ),
        ]
        outcome = ProposalOutcome.PROPOSE
        summary = (
            "A bounded deterministic candidate was produced for downstream evidence "
            "and approval checks; no state change or action was performed."
        )

    input_digest = _model_fingerprint(incoming)
    config_fingerprint = _model_fingerprint(config)
    calculation_trace = _statement_trace(
        incoming,
        field_rule,
        text_result.mode,
        text_result.quantity,
        prior_total,
        proposed_total,
        input_digest,
    )

    return SemanticUpdateProposal(
        outcome=outcome,
        entity_id=current.entity_id,
        evidence_id=incoming.evidence_id,
        evidence_reference=incoming.evidence_reference,
        source_type=incoming.source_type,
        input_digest=input_digest,
        config_fingerprint=config_fingerprint,
        state_digest=_state_digest(current),
        mode=text_result.mode,
        field_name=text_result.field_name,
        field_is_important=(
            text_result.field_name in config.important_fields
            if text_result.field_name is not None
            else False
        ),
        stated_quantity=text_result.quantity,
        prior_total=prior_total,
        proposed_total=proposed_total,
        identity_matched=identity_matched and categories_match,
        matched_identity=matched_identity,
        reasons=reasons,
        human_questions=_questions_for(blocker_codes, config.identity_keys),
        calculation_trace=calculation_trace,
        summary=summary,
    )


_CORRECTION_OVERRIDABLE_CODES = {
    "METRIC_NOT_FOUND",
    "MULTIPLE_IMPORTANT_FIELDS",
    "NEGATED_OR_CANCELLED",
    "QUANTITY_MISSING",
    "MULTIPLE_CONFLICTING_QUANTITIES",
    "CONFLICTING_MODE_MARKERS",
    "UNSUPPORTED_QUANTITY_WORDING",
    "NON_INTEGER_QUANTITY",
    "UNSUPPORTED_DECREASE_WORDING",
    "QUANTITY_EXCEEDS_PARSER_LIMIT",
    "PRIOR_TOTAL_MISSING",
    "PRIOR_TOTAL_OUT_OF_BOUNDS",
    "PROPOSED_TOTAL_OUT_OF_BOUNDS",
    "CHANGE_EXCEEDS_LIMIT",
}


def _correction_digest(correction: HumanQuantityCorrection) -> str:
    payload = json.dumps(
        correction.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def apply_human_correction(
    config: CategorySemanticConfig,
    current: EntityQuantityState,
    proposal: SemanticUpdateProposal | CorrectedSemanticUpdateProposal,
    correction: HumanQuantityCorrection,
) -> CorrectedSemanticUpdateProposal:
    """Append a human interpretation correction and recalculate deterministically.

    A correction can resolve constrained-language ambiguity, but cannot bypass
    entity matching, stale-evidence checks, or configured numeric safety bounds.
    The returned value embeds the untouched original proposal and the full bounded
    correction history.  It still performs no lookup, persistence, or action.
    """

    if not isinstance(config, CategorySemanticConfig):
        raise TypeError("config must be a validated CategorySemanticConfig")
    if not isinstance(current, EntityQuantityState):
        raise TypeError("current must be a validated EntityQuantityState")
    if not isinstance(
        proposal,
        (SemanticUpdateProposal, CorrectedSemanticUpdateProposal),
    ):
        raise TypeError("proposal must be a validated semantic update proposal")
    if not isinstance(correction, HumanQuantityCorrection):
        raise TypeError("correction must be a validated HumanQuantityCorrection")

    if isinstance(proposal, CorrectedSemanticUpdateProposal):
        original = proposal.original_proposal
        prior_corrections = list(proposal.corrections)
        prior_trace = proposal.calculation_trace
    else:
        original = proposal
        prior_corrections = []
        prior_trace = proposal.calculation_trace

    if len(prior_corrections) >= 32:
        raise ValueError("correction history limit reached")
    if correction.correction_id in {item.correction_id for item in prior_corrections}:
        raise ValueError("correction identity must be unique")
    if prior_corrections and correction.created_at < prior_corrections[-1].created_at:
        raise ValueError("corrections must be appended in chronological order")
    if any(reason.code not in _REASON_DETAILS for reason in original.reasons):
        raise ValueError("proposal contains unsupported reason codes")

    corrections = [*prior_corrections, correction]
    field_rule = next(
        (
            item
            for item in config.quantity_fields
            if item.field_name == correction.field_name
        ),
        None,
    )
    blocker_codes = [
        reason.code
        for reason in original.reasons
        if reason.code not in _CORRECTION_OVERRIDABLE_CODES
        and reason.code
        not in {
            "IDENTITY_MATCHED",
            "INTERPRETED_AS_TOTAL",
            "INTERPRETED_AS_DELTA",
            "WITHIN_CONFIGURED_BOUNDS",
        }
    ]
    if not original.identity_matched:
        blocker_codes.append("IDENTITY_NOT_VERIFIED_AFTER_CORRECTION")
    state_snapshot_matches = _state_digest(current) == original.state_digest
    if not state_snapshot_matches:
        blocker_codes.append("CURRENT_STATE_MISMATCH_AFTER_CORRECTION")
    if current.category != config.category:
        blocker_codes.append("CATEGORY_MISMATCH")
    config_matches = _model_fingerprint(config) == original.config_fingerprint
    if not config_matches:
        blocker_codes.append("CONFIG_MISMATCH_AFTER_CORRECTION")
    if correction.created_at < current.as_of:
        blocker_codes.append("CORRECTION_PREDATES_STATE")
    if field_rule is None:
        blocker_codes.append("CORRECTED_FIELD_NOT_CONFIGURED")

    prior_total = (
        current.quantity_values.get(correction.field_name)
        if state_snapshot_matches
        else original.prior_total
    )
    proposed_total: int | None = None
    if (
        field_rule is not None
        and original.identity_matched
        and state_snapshot_matches
        and config_matches
    ):
        if correction.mode == QuantityMode.DELTA:
            if prior_total is None:
                blocker_codes.append("PRIOR_TOTAL_MISSING")
            else:
                proposed_total = prior_total + correction.quantity
        else:
            proposed_total = correction.quantity

    if proposed_total is not None and field_rule is not None:
        if proposed_total > field_rule.maximum_plausible_value:
            blocker_codes.append("PROPOSED_TOTAL_OUT_OF_BOUNDS")
        if (
            prior_total is not None
            and abs(proposed_total - prior_total) > field_rule.maximum_absolute_change
        ):
            blocker_codes.append("CHANGE_EXCEEDS_LIMIT")

    blocker_codes = _stable_unique(blocker_codes)
    contribution = CalculationContribution(
        kind=ContributionKind.HUMAN_CORRECTION,
        evidence_id=correction.correction_id,
        evidence_reference=correction.evidence_reference,
        source_type="human_correction",
        content_digest=_correction_digest(correction),
        interpreted_excerpt=(
            "Human explicitly classified "
            f"{correction.quantity} as {correction.mode.value}."
        ),
        mode=correction.mode,
        stated_quantity=correction.quantity,
        prior_total=prior_total,
        resulting_total=proposed_total,
    )
    contributions = [*prior_trace.contributions, contribution]
    trace = CalculationTrace(
        formula=_formula(
            correction.mode,
            prior_total,
            correction.quantity,
            proposed_total,
        ),
        contributions=contributions,
        explanation=(
            "The latest explicit human correction outranks the earlier language "
            "interpretation when identity, state, configuration, and safety gates "
            "pass; the original remains in this record."
        ),
    )

    if blocker_codes:
        outcome = ProposalOutcome.REVIEW
        reasons = [
            ProposalReason(code=code, detail=_REASON_DETAILS[code])
            for code in blocker_codes
        ]
        summary = (
            "The correction was recorded, but additional review is still required; "
            "no state change or action was performed."
        )
    else:
        outcome = ProposalOutcome.PROPOSE
        reasons = [
            ProposalReason(
                code="HUMAN_CORRECTION_APPLIED",
                detail=_REASON_DETAILS["HUMAN_CORRECTION_APPLIED"],
            ),
            ProposalReason(
                code="WITHIN_CONFIGURED_BOUNDS",
                detail=_REASON_DETAILS["WITHIN_CONFIGURED_BOUNDS"],
            ),
        ]
        summary = (
            "The appended human correction produced a recalculated candidate for "
            "downstream approval; no state change or action was performed."
        )

    return CorrectedSemanticUpdateProposal(
        original_proposal=original,
        corrections=corrections,
        outcome=outcome,
        entity_id=original.entity_id,
        config_fingerprint=original.config_fingerprint,
        state_digest=original.state_digest,
        mode=correction.mode,
        field_name=correction.field_name,
        prior_total=prior_total,
        corrected_quantity=correction.quantity,
        proposed_total=proposed_total,
        identity_matched=original.identity_matched and state_snapshot_matches,
        matched_identity=original.matched_identity,
        reasons=reasons,
        human_questions=_questions_for(blocker_codes, config.identity_keys),
        calculation_trace=trace,
        summary=summary,
    )
