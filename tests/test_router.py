"""Model routing: interchangeable local and cloud models with a safe fallback."""

from __future__ import annotations

import pytest

from jarvis.errors import ModelUnavailable
from jarvis.intelligence.providers import (
    Completion,
    CompletionRequest,
    DeterministicProvider,
    ModelSpec,
    Provider,
    extract_json,
)
from jarvis.intelligence.router import ModelRouter


class Stub(Provider):
    """A model that looks healthy to the probe but may fail when actually called."""

    def __init__(self, model_id, kind="cloud", text='{"ok": true}', fail=False,
                 offline=False,
                 task_classes=("planning", "reasoning", "summarising", "coding")):
        super().__init__(ModelSpec(id=model_id, name=model_id, kind=kind,
                                   task_classes=list(task_classes),
                                   cost=1.0 if kind == "cloud" else 0.0,
                                   latency=1.0))
        self.text = text
        self.fail = fail
        self.offline = offline
        self.calls = 0

    def available(self):
        return not self.offline

    def complete(self, request):
        self.calls += 1
        if self.fail:
            raise RuntimeError("upstream unavailable")
        return Completion(text=self.text, model=self.spec.id, provider=self.name)


def test_planning_prefers_a_real_model_over_the_rules(jarvis):
    local = Stub("local-llm", kind="local")
    router = ModelRouter([local, DeterministicProvider()])

    completion = router.complete(CompletionRequest(prompt="plan", task_class="planning"))
    assert completion.model == "local-llm"
    assert local.calls == 1


def test_a_failing_model_falls_back_instead_of_failing_the_task(jarvis):
    """Both models look healthy; the preferred one fails at call time."""
    broken = Stub("primary-llm", kind="cloud", fail=True)
    backup = Stub("secondary-llm", kind="cloud")
    router = ModelRouter([broken, backup, DeterministicProvider()])

    completion = router.complete(CompletionRequest(prompt="plan", task_class="planning"))

    assert completion.model == "secondary-llm"
    assert broken.calls == 1
    assert router.usage_summary()["fallbacks"] == 1


def test_the_deterministic_provider_is_always_the_last_resort():
    broken = Stub("cloud-llm", fail=True, offline=True)
    router = ModelRouter([broken, DeterministicProvider()])

    completion = router.complete(CompletionRequest(prompt="plan anything",
                                                   task_class="planning", json_mode=True))
    assert completion.model == "jarvis-deterministic"
    assert completion.json()["steps"]


def test_routing_is_recorded_as_a_decision(jarvis):
    router = ModelRouter([Stub("local-llm", kind="local"), DeterministicProvider()],
                         store=jarvis.store)
    router.complete(CompletionRequest(prompt="plan", task_class="planning"),
                    task_id="task_probe")

    decisions = [d for d in jarvis.store.decisions_for("task_probe")
                 if d.kind == "model_selected"]
    assert decisions and "local-llm" in decisions[0].rationale


def test_preference_changes_the_ranking():
    providers = [Stub("cloud", kind="cloud"), Stub("local", kind="local"),
                 DeterministicProvider()]
    assert ModelRouter(providers, prefer="cheap").rank("summarising")[0].spec.kind in (
        "deterministic", "local")
    assert ModelRouter(providers, prefer="strong").rank("planning")[0].spec.kind in (
        "local", "cloud")


def test_no_enabled_provider_is_reported_clearly():
    disabled = Stub("off", kind="local")
    disabled.spec.enabled = False
    router = ModelRouter([disabled])
    with pytest.raises(ModelUnavailable):
        router.complete(CompletionRequest(prompt="x"))


def test_json_is_extracted_from_prose():
    assert extract_json('here you go: {"a": 1} hope that helps') == {"a": 1}
    assert extract_json("```json\n[1, 2]\n```") == [1, 2]
    assert extract_json("no json here", default={"fallback": True}) == {"fallback": True}


def test_planner_falls_back_to_a_template_when_the_model_fails(jarvis):
    broken = Stub("broken-llm", kind="cloud", fail=True)
    jarvis.router.providers.insert(0, broken)

    plan = jarvis.planner.plan("run the tests in this project", context={}, task_id="")

    assert plan.steps, "a plan must always be produced"
    assert all(jarvis.tools.has(step.tool) for step in plan.steps)
    assert plan.success_criteria
