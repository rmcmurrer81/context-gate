"""Validate optional agent output against the immutable deterministic result."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from .models import AgentExplanation, RuleResult


def validated_explanation(
    result: RuleResult,
    model_output: Mapping[str, Any] | str | None,
) -> tuple[str, bool, bool]:
    """Return explanation, model-used flag, model-valid flag.

    A model may improve wording only. It cannot change enforcement, risk,
    classification, authority, evidence IDs, or approval requirements.
    """

    if model_output is None:
        return result.explanation, False, False
    try:
        raw = (
            json.loads(model_output)
            if isinstance(model_output, str)
            else dict(model_output)
        )
        candidate = AgentExplanation.model_validate(raw)
    except (json.JSONDecodeError, TypeError, ValueError, ValidationError):
        return result.explanation, False, False

    matches = (
        candidate.decision == result.decision
        and candidate.risk == result.risk
        and candidate.classification == result.classification
        and candidate.authoritative_value == result.authoritative_value
        and set(candidate.evidence_event_ids) == set(result.evidence_event_ids)
        and candidate.requires_human_approval == result.requires_human_approval
    )
    if not matches:
        return result.explanation, False, False
    return candidate.explanation, True, True
