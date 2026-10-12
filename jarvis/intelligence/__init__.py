"""Hybrid intelligence: routing, planning and reasoning."""

from jarvis.intelligence.providers import (
    Completion,
    CompletionRequest,
    DeterministicProvider,
    ModelSpec,
    OllamaProvider,
    OpenAICompatibleProvider,
    Provider,
    providers_from_env,
)
from jarvis.intelligence.router import ModelRouter
from jarvis.intelligence.planner import Planner, PlanResult

__all__ = [
    "Completion", "CompletionRequest", "DeterministicProvider", "ModelSpec",
    "OllamaProvider", "OpenAICompatibleProvider", "Provider", "providers_from_env",
    "ModelRouter", "Planner", "PlanResult",
]
