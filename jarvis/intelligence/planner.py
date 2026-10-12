"""Planning: intent -> objective, steps and success criteria.

The planner is where "understands intent and decides what happens next" lives. It
asks the routed model for a plan, then *validates* it against the tool registry:
unknown tools are dropped, missing scopes are recorded so the authorization phase
can ask, and an empty or malformed plan falls back to the deterministic template.
A plan is only ever accepted if every step can actually be executed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from jarvis.core.models import Check, Decision, Step, StepState, new_id, now
from jarvis.errors import PlannerError
from jarvis.intelligence.providers import INTENT_KEYWORDS, default_plan_for
from jarvis.intelligence.router import ModelRouter

PLANNER_SYSTEM = """You are JARVIS, an autonomous assistant that executes work on a real machine.
Produce a plan as strict JSON with the keys:
  objective: one sentence describing the outcome that counts as success
  steps: list of {tool, args, title, produces, requires, optional}
  success_criteria: list of {kind, args, expect, description}
Only use tools that appear in the provided tool list. Keep plans short: 1 to 6 steps.
Success criteria must be independently checkable (a file, a command exit code, a
computed value) - never "the assistant believes it worked"."""

MAX_STEPS = 12


@dataclass
class PlanResult:
    objective: str
    steps: List[Step]
    success_criteria: List[Check]
    model: str = ""
    source: str = "model"              # model | template | explicit
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "objective": self.objective,
            "steps": [s.to_dict() for s in self.steps],
            "success_criteria": [c.to_dict() for c in self.success_criteria],
            "model": self.model,
            "source": self.source,
            "notes": self.notes,
        }


class Planner:
    def __init__(self, router: ModelRouter, tools, store=None, memory=None):
        self.router = router
        self.tools = tools
        self.store = store
        self.memory = memory

    # -- entry point -------------------------------------------------------- #
    def plan(self, intent: str, context: Optional[Dict[str, Any]] = None,
             goal_id: str = "", task_id: str = "") -> PlanResult:
        context = dict(context or {})

        explicit = context.get("plan")
        if explicit:
            result = self._from_spec(explicit, source="explicit")
            if result.steps:
                return result

        prompt = self._prompt(intent, context, goal_id)
        notes: List[str] = []
        raw: Any = None
        model_id = ""
        try:
            completion = self.router.complete(
                _request(prompt, task_class="planning"), task_id=task_id
            )
            model_id = completion.model
            raw = completion.json(default=None)
        except Exception as exc:
            notes.append(f"model planning failed ({exc}); using deterministic template")

        if isinstance(raw, dict) and raw.get("steps"):
            result = self._from_spec(raw, source="model")
            result.model = model_id
            result.notes.extend(notes)
            if result.steps:
                self._record(task_id, intent, result)
                return result
            notes.append("model plan contained no executable steps; using template")

        intent_key = classify_intent(intent)
        template = default_plan_for(intent_key, intent)
        result = self._from_spec(template, source="template")
        result.model = model_id or "jarvis-deterministic"
        result.notes.extend(notes)
        if not result.steps:
            raise PlannerError(f"no plan could be produced for intent: {intent[:120]}")
        self._record(task_id, intent, result)
        return result

    # -- construction ------------------------------------------------------- #
    def _from_spec(self, spec: Dict[str, Any], source: str) -> PlanResult:
        notes: List[str] = []
        known = set(self.tools.names()) if self.tools is not None else set()
        steps: List[Step] = []

        for index, raw in enumerate(list(spec.get("steps") or [])[:MAX_STEPS]):
            if not isinstance(raw, dict):
                notes.append(f"step {index} was not an object and was dropped")
                continue
            tool = str(raw.get("tool", "")).strip()
            if not tool:
                notes.append(f"step {index} had no tool and was dropped")
                continue
            if known and tool not in known:
                notes.append(f"step {index} referenced unknown tool {tool!r} and was dropped")
                continue
            requires = list(raw.get("requires") or [])
            if not requires and self.tools is not None and tool in known:
                requires = list(self.tools.get(tool).requires)
            steps.append(
                Step(
                    id=new_id("step"), ordinal=len(steps), tool=tool,
                    args=dict(raw.get("args") or {}), title=str(raw.get("title") or tool),
                    produces=str(raw.get("produces") or ""), requires=requires,
                    optional=bool(raw.get("optional", False)), state=StepState.PENDING,
                )
            )

        criteria = [
            Check.from_dict(item)
            for item in (spec.get("success_criteria") or [])
            if isinstance(item, dict) and item.get("kind")
        ]
        if not criteria:
            notes.append("the plan declared no success criteria; task cannot be verified")

        return PlanResult(
            objective=str(spec.get("objective") or "").strip(),
            steps=steps, success_criteria=criteria, source=source, notes=notes,
        )

    def _prompt(self, intent: str, context: Dict[str, Any], goal_id: str) -> str:
        tool_lines = []
        if self.tools is not None:
            for tool in self.tools.list():
                scopes = ", ".join(tool.requires) or "none"
                tool_lines.append(f"- {tool.name}: {tool.description} [scopes: {scopes}]")
        memory_block = ""
        if self.memory is not None:
            memory_block = self.memory.context_block(intent, goal_id=goal_id, limit=5)
        goal_block = ""
        if goal_id and self.store is not None:
            goal = self.store.get_goal(goal_id)
            if goal:
                goal_block = f"\nActive long-term goal: {goal.title} - {goal.objective}"

        return (
            f"User request: {intent}\n"
            f"{goal_block}\n"
            f"Working directory: {context.get('cwd', '.')}\n"
            f"Available tools:\n" + "\n".join(tool_lines) + "\n"
            + (f"\nRelevant memory:\n{memory_block}\n" if memory_block else "")
            + "\nReturn the JSON plan now."
        )

    def _record(self, task_id: str, intent: str, result: PlanResult) -> None:
        if self.store is None:
            return
        self.store.add_decision(
            Decision(
                id=new_id("dec"), task_id=task_id, kind="plan_chosen",
                rationale=f"plan source={result.source}, steps={len(result.steps)}, "
                          f"criteria={len(result.success_criteria)}",
                payload={"objective": result.objective, "notes": result.notes,
                         "steps": [s.tool for s in result.steps]},
            )
        )


def classify_intent(intent: str) -> str:
    lowered = (intent or "").lower()
    for keyword, name in INTENT_KEYWORDS:
        if keyword in lowered:
            return name
    return "assist"


def _request(prompt: str, task_class: str) -> Any:
    from jarvis.intelligence.providers import CompletionRequest

    return CompletionRequest(
        prompt=prompt, system=PLANNER_SYSTEM, task_class=task_class,
        max_tokens=1200, temperature=0.1, json_mode=True,
    )


def plan_to_json(result: PlanResult) -> str:
    return json.dumps(result.to_dict(), indent=2)
