"""ContextGate: deterministic evidence controls for event-driven AI agents."""

from .decision_engine import evaluate_request
from .models import ActionRequest, ContextEvent, DecisionRecord

__all__ = ["ActionRequest", "ContextEvent", "DecisionRecord", "evaluate_request"]
__version__ = "0.1.0"
