"""Evolver: self-evaluation and governed improvement.

After work finishes, Evolver scores what actually happened - not what was claimed -
and turns repeated problems into *proposals*. A proposal is inert until a human
approves it, and the only things a proposal may change are configuration values and
planner guidance. Evolver cannot rewrite its own code, and it never applies a change
silently. That boundary is what keeps "governed improvement" honest.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from jarvis.core.models import Decision, Task, TaskState, Verdict, new_id, now

# Thresholds that trigger a proposal.
TOOL_FAILURE_RATE = 0.4
TOOL_FAILURE_MIN_CALLS = 3
VERIFICATION_FAILURE_RATE = 0.5
MODEL_FALLBACK_RATE = 0.5

ALLOWED_CHANGE_KINDS = (
    "router_preference",       # change how models are ranked
    "step_retry_limit",        # change how hard the executive retries
    "revoke_scope",            # take a capability away
    "grant_scope",             # give a capability back (still needs approval at runtime)
    "planner_guidance",        # add durable guidance the planner reads
)


class Evolver:
    def __init__(self, store, memory, executive=None, router=None, policy=None):
        self.store = store
        self.memory = memory
        self.executive = executive
        self.router = router
        self.policy = policy

    # ------------------------------------------------------------------ #
    # evaluation
    # ------------------------------------------------------------------ #
    def evaluate(self, task: Task) -> Dict[str, Any]:
        """Score one finished task from its persisted evidence."""
        verified = task.verdict == Verdict.VERIFIED.value
        steps_total = len(task.plan) or 1
        steps_done = sum(1 for s in task.plan if s.state.value in ("succeeded", "skipped"))
        efficiency = steps_done / steps_total
        duration = task.duration
        attempts = task.attempts

        score = 0.0
        score += 0.55 if verified else 0.0
        score += 0.25 * efficiency
        score += 0.10 * max(0.0, 1.0 - (attempts / max(task.max_attempts, 1)))
        score += 0.10 * (1.0 if duration and duration < 30 else 0.5 if duration else 0.0)
        score = round(min(score, 1.0), 3)

        self.store.add_metric(task.id, "task_score", score,
                              {"verified": verified, "attempts": attempts})
        self.store.add_metric(task.id, "task_duration_seconds", round(duration, 3))
        self.store.add_metric(task.id, "task_attempts", float(attempts))
        self.store.add_metric(task.id, "task_verified", 1.0 if verified else 0.0)
        for step in task.plan:
            self.store.add_metric(
                task.id, f"tool:{step.tool}", 1.0 if step.state.value == "succeeded" else 0.0,
                {"step": step.title, "error": step.error[:200]},
            )
        return {
            "task_id": task.id, "score": score, "verified": verified,
            "attempts": attempts, "duration": round(duration, 3),
            "efficiency": round(efficiency, 3),
        }

    def postmortem(self, task: Task) -> Optional[str]:
        """Write a reflection for work that did not verify, and return it."""
        if task.verdict == Verdict.VERIFIED.value:
            return None
        failed_steps = [f"{s.tool}: {s.error[:120]}" for s in task.plan
                        if s.state.value in ("failed",)]
        verification = (task.context.get("verification") or {}).get("evidence", "")
        text = (
            f"Postmortem for '{task.intent[:100]}': verdict={task.verdict or 'none'}, "
            f"kind={task.error_kind or 'n/a'}, attempts={task.attempts}. "
            f"Failed steps: {failed_steps or 'none'}. Evidence: {str(verification)[:300]}"
        )
        self.memory.remember(text, kind="reflection",
                             tags=["postmortem", task.error_kind or "unverified"],
                             goal_id=task.goal_id, importance=0.8)
        self.store.audit("evolver", "postmortem", {"task": task.id, "verdict": task.verdict})
        return text

    # ------------------------------------------------------------------ #
    # analysis
    # ------------------------------------------------------------------ #
    def metrics_summary(self) -> Dict[str, Any]:
        verified = self.store.metrics("task_verified")
        scores = self.store.metrics("task_score")
        tool_rows = [m for m in self.store.metrics() if m["name"].startswith("tool:")]
        tools: Dict[str, Dict[str, float]] = {}
        for row in tool_rows:
            bucket = tools.setdefault(row["name"], {"calls": 0.0, "ok": 0.0})
            bucket["calls"] += 1
            bucket["ok"] += row["value"]
        return {
            "tasks_evaluated": len(scores),
            "average_score": round(sum(m["value"] for m in scores) / len(scores), 3) if scores else 0.0,
            "verification_rate": round(
                sum(m["value"] for m in verified) / len(verified), 3) if verified else 0.0,
            "tools": {
                name: {"calls": int(b["calls"]), "success_rate": round(b["ok"] / b["calls"], 3)}
                for name, b in sorted(tools.items())
            },
            "model_usage": self.router.usage_summary() if self.router else {},
            "tool_usage": self.executive.tools.usage_summary()
            if self.executive and self.executive.tools else {},
        }

    def propose_improvements(self) -> List[Dict[str, Any]]:
        """Turn observed problems into proposals. Nothing is applied here."""
        summary = self.metrics_summary()
        proposals: List[Dict[str, Any]] = []

        for name, stats in summary["tools"].items():
            if stats["calls"] < TOOL_FAILURE_MIN_CALLS:
                continue
            if stats["success_rate"] <= 1.0 - TOOL_FAILURE_RATE:
                tool = name.split("tool:", 1)[1]
                proposals.append(self._proposal(
                    kind="step_retry_limit",
                    title=f"Increase retry budget because {tool} fails often",
                    rationale=(f"{tool} succeeded in {stats['success_rate']:.0%} of "
                               f"{stats['calls']} calls"),
                    payload={"tool": tool, "step_retry_limit": 5},
                ))

        if summary["tasks_evaluated"] >= 2 and summary["verification_rate"] < (
            1.0 - VERIFICATION_FAILURE_RATE
        ):
            proposals.append(self._proposal(
                kind="planner_guidance",
                title="Require independently checkable success criteria in every plan",
                rationale=(f"only {summary['verification_rate']:.0%} of "
                           f"{summary['tasks_evaluated']} evaluated tasks verified"),
                payload={"guidance": (
                    "Every plan must declare at least one success criterion that reads the "
                    "outside world: a file, a command exit code, or a computed value.")},
            ))

        model_usage = summary["model_usage"]
        total = model_usage.get("total_calls", 0)
        if total >= 3 and model_usage.get("fallbacks", 0) / total > MODEL_FALLBACK_RATE:
            proposals.append(self._proposal(
                kind="router_preference",
                title="Prefer local models: the primary model keeps failing",
                rationale=(f"{model_usage['fallbacks']} fallbacks across {total} model calls"),
                payload={"preference": "cheap"},
            ))
        return proposals

    def _proposal(self, kind: str, title: str, rationale: str,
                  payload: Dict[str, Any]) -> Dict[str, Any]:
        proposal = {
            "id": new_id("prop"), "kind": kind, "title": title, "rationale": rationale,
            "payload": payload, "state": "pending", "created_at": now(), "decided_at": 0.0,
        }
        self.store.add_proposal(proposal)
        self.store.audit("evolver", "proposal_created",
                         {"id": proposal["id"], "kind": kind, "title": title})
        return proposal

    # ------------------------------------------------------------------ #
    # governed application
    # ------------------------------------------------------------------ #
    def apply(self, proposal_id: str, approve: bool, note: str = "user") -> Dict[str, Any]:
        """Apply an approved proposal. Configuration only - never source code."""
        proposals = {p["id"]: p for p in self.store.list_proposals()}
        if proposal_id not in proposals:
            raise KeyError(proposal_id)
        proposal = proposals[proposal_id]
        if not approve:
            self.store.set_proposal_state(proposal_id, "rejected")
            self.store.audit("evolver", "proposal_rejected", {"id": proposal_id, "note": note})
            proposal["state"] = "rejected"
            return proposal
        if proposal["kind"] not in ALLOWED_CHANGE_KINDS:
            raise PermissionError(
                f"proposal kind {proposal['kind']!r} may not be applied automatically"
            )

        applied = self._apply_change(proposal)
        self.store.set_proposal_state(proposal_id, "applied")
        self.store.add_decision(Decision(
            task_id="", kind="improvement_applied",
            rationale=f"{proposal['title']} ({proposal['rationale']})",
            payload={"id": proposal_id, "kind": proposal["kind"], "effect": applied},
        ))
        self.store.audit("evolver", "proposal_applied",
                         {"id": proposal_id, "kind": proposal["kind"], "by": note})
        self.memory.remember(
            f"Improvement applied: {proposal['title']} - {applied}",
            kind="reflection", tags=["improvement", proposal["kind"]], importance=0.7,
        )
        proposal["state"] = "applied"
        proposal["effect"] = applied
        return proposal

    def _apply_change(self, proposal: Dict[str, Any]) -> str:
        kind = proposal["kind"]
        payload = proposal.get("payload") or {}
        if kind == "router_preference" and self.router is not None:
            preference = str(payload.get("preference", "balanced"))
            self.router.prefer = preference
            return f"model preference set to {preference!r}"
        if kind == "step_retry_limit" and self.executive is not None:
            limit = int(payload.get("step_retry_limit", 5))
            self.executive.step_retry_limit = limit
            return f"step retry limit set to {limit}"
        if kind == "revoke_scope" and self.policy is not None:
            scope = str(payload.get("scope", ""))
            self.policy.revoke(scope)
            return f"scope {scope} revoked"
        if kind == "grant_scope" and self.policy is not None:
            scope = str(payload.get("scope", ""))
            self.policy.grant(scope)
            return f"scope {scope} granted"
        if kind == "planner_guidance":
            guidance = str(payload.get("guidance", ""))
            if self.memory is not None and guidance:
                self.memory.remember(guidance, kind="preference",
                                     tags=["planner", "guidance"], importance=0.9)
            return "planner guidance stored in memory"
        raise PermissionError(f"no handler for proposal kind {kind!r}")

    def review(self) -> Dict[str, Any]:
        """Evaluate everything finished since the last review."""
        evaluated = []
        for task in self.store.list_tasks(states=["completed", "blocked", "failed"], limit=500):
            if task.context.get("evolver_reviewed"):
                continue
            task.context["evolver_reviewed"] = now()
            self.store.save_task(task)
            evaluated.append(self.evaluate(task))
            self.postmortem(task)
        proposals = self.propose_improvements()
        return {"evaluated": len(evaluated), "proposals": len(proposals),
                "summary": self.metrics_summary()}
