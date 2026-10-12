"""Model routing: pick the right model for the job, and fall back when it fails.

Selection is a ranked preference, not a hard binding: for each request the router
orders available providers by capability match, cost and latency, records *why* it
chose each one, and walks down the list until something answers. The deterministic
provider is always the last resort, so a routing failure is never a task failure.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence

from jarvis.core.models import Decision, new_id, now
from jarvis.errors import ModelUnavailable
from jarvis.intelligence.providers import (
    Completion,
    CompletionRequest,
    DeterministicProvider,
    Provider,
)

# Task classes that deserve the strongest available model.
HIGH_VALUE = {"planning", "reasoning", "coding"}


class ModelRouter:
    def __init__(self, providers: Sequence[Provider], store=None, prefer: str = "balanced"):
        if not providers:
            raise ValueError("at least one provider is required")
        self.providers: List[Provider] = list(providers)
        self.store = store
        self.prefer = prefer                     # balanced | cheap | fast | strong
        self.calls: List[Dict[str, Any]] = []

    # -- introspection ------------------------------------------------------ #
    def describe(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": p.spec.id, "name": p.spec.name, "kind": p.spec.kind,
                "task_classes": p.spec.task_classes, "cost": p.spec.cost,
                "latency": p.spec.latency, "enabled": p.spec.enabled,
                "available": p.available() if p.spec.enabled else False,
                "last_error": p.last_error,
            }
            for p in self.providers
        ]

    def add(self, provider: Provider) -> None:
        self.providers.append(provider)

    # -- selection ---------------------------------------------------------- #
    def rank(self, task_class: str) -> List[Provider]:
        candidates = [p for p in self.providers if p.spec.enabled]
        if not candidates:
            return []

        def score(provider: Provider) -> tuple:
            spec = provider.spec
            capability = 0 if provider.supports(task_class) else 1
            kind_pref = {
                "cheap": 0 if spec.kind == "deterministic" else (1 if spec.kind == "local" else 2),
                "fast": spec.latency,
                "strong": 0 if spec.kind in ("local", "cloud") else 2,
                "balanced": spec.cost + 0.5 * spec.latency,
            }.get(self.prefer, spec.cost)
            if spec.kind == "deterministic" and self.prefer != "cheap":
                # The rule engine is a safety net, not a preference: whenever a real
                # model is configured it should get the work.
                kind_pref += 3.0
            availability = 0 if provider.available() else 1
            return (capability, availability, kind_pref, spec.cost)

        return sorted(candidates, key=score)

    # -- execution ---------------------------------------------------------- #
    def complete(self, request: CompletionRequest, task_id: str = "") -> Completion:
        order = self.rank(request.task_class)
        if not order:
            raise ModelUnavailable("no enabled model providers are configured")

        errors: List[str] = []
        for provider in order:
            started = time.time()
            try:
                completion = provider.complete(request)
            except Exception as exc:
                provider.last_error = f"{type(exc).__name__}: {exc}"
                if isinstance(provider, DeterministicProvider):
                    raise
                errors.append(f"{provider.spec.id}: {exc}")
                continue
            self._record(provider, request, completion, errors, task_id)
            return completion

        raise ModelUnavailable("every configured model failed: " + "; ".join(errors))

    def generate_json(
        self, request: CompletionRequest, task_id: str = "", default: Any = None
    ) -> Any:
        request.json_mode = True
        completion = self.complete(request, task_id=task_id)
        parsed = completion.json(default=default)
        return parsed if parsed is not None else default

    # -- bookkeeping -------------------------------------------------------- #
    def _record(self, provider: Provider, request: CompletionRequest, completion: Completion,
                errors: List[str], task_id: str) -> None:
        entry = {
            "task_id": task_id, "task_class": request.task_class, "model": provider.spec.id,
            "provider": provider.name, "kind": provider.spec.kind,
            "latency": round(completion.latency, 4),
            "fallbacks": errors, "at": now(),
        }
        self.calls.append(entry)
        if len(self.calls) > 500:
            self.calls = self.calls[-500:]
        if self.store is not None:
            reason = (
                f"selected {provider.spec.name} ({provider.spec.kind}) for "
                f"{request.task_class}; preference={self.prefer}"
                + (f"; after {len(errors)} fallback(s)" if errors else "")
            )
            self.store.add_decision(
                Decision(id=new_id("dec"), task_id=task_id, kind="model_selected",
                         rationale=reason,
                         payload={"model": provider.spec.id, "fallbacks": errors,
                                  "preference": self.prefer})
            )

    def usage_summary(self) -> Dict[str, Any]:
        by_model: Dict[str, int] = {}
        fallbacks = 0
        for call in self.calls:
            by_model[call["model"]] = by_model.get(call["model"], 0) + 1
            fallbacks += len(call["fallbacks"])
        return {"total_calls": len(self.calls), "by_model": by_model, "fallbacks": fallbacks}
