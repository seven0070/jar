"""The JARVIS application: one object that wires the whole system together.

This is what the desktop app, the voice interface and the CLI all talk to, so there
is exactly one executive runtime, one store and one policy engine per process.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional, Sequence

from jarvis.config import Config
from jarvis.core.events import EventBus
from jarvis.core.executive import CycleOutcome, Executive
from jarvis.core.memory import MemoryStore
from jarvis.core.models import (
    Check,
    as_check,
    Goal,
    GoalState,
    Task,
    TaskState,
    Trigger,
    now,
)
from jarvis.core.permissions import Approver, PolicyEngine, SCOPES
from jarvis.core.recovery import RecoveryManager
from jarvis.core.scheduler import Scheduler
from jarvis.core.store import Store
from jarvis.core.verification import Verifier
from jarvis.errors import Halted
from jarvis.evolver.selfeval import Evolver
from jarvis.intelligence.planner import Planner
from jarvis.intelligence.providers import Provider, providers_from_env
from jarvis.intelligence.router import ModelRouter
from jarvis.odysseus.manager import Odysseus
from jarvis.tools.base import ToolContext
from jarvis.tools.builtin import register_builtin_tools
from jarvis.tools.registry import ToolRegistry
from jarvis.version import __version__, banner


class Jarvis:
    """Persistent personal AI operating system and autonomous digital assistant."""

    def __init__(
        self,
        config: Optional[Config] = None,
        store: Optional[Store] = None,
        providers: Optional[Sequence[Provider]] = None,
        root: Optional[str] = None,
        approver_waiter: Optional[Callable] = None,
        auto_approve: Sequence[str] = (),
        approval_blocking: Optional[bool] = None,
        register_tools: bool = True,
        resume_on_start: bool = True,
    ):
        # Environment configuration is honoured unless an explicit Config is supplied,
        # so the CLI, the desktop app and the voice interface all boot the same way.
        self.config = (config or Config.from_env()).ensure_dirs()
        if approval_blocking is not None:
            self.config.approval_blocking = approval_blocking
        self.root = os.path.abspath(root or self.config.workspace)
        os.makedirs(self.root, exist_ok=True)

        self.store = store or Store(self.config.db_path)
        self.bus = EventBus(self.store)
        self.policy = PolicyEngine(mode=self.config.mode, deny=set(self.config.denied_scopes))
        if self.store.get_setting("halted", False):
            self.policy.halt()
        self.approver = Approver(
            self.store, waiter=approver_waiter,
            auto_approve=tuple(auto_approve) + tuple(self.config.auto_approve),
            blocking=self.config.approval_blocking, timeout=self.config.approval_timeout,
        )

        self.router = ModelRouter(
            list(providers) if providers is not None else providers_from_env(),
            store=self.store, prefer=self.config.model_preference,
        )
        self.memory = MemoryStore(self.store)
        self.tools = ToolRegistry(store=self.store, policy=self.policy, bus=self.bus)
        if register_tools:
            register_builtin_tools(self.tools)
        self.verifier = Verifier(self.root)
        self.recovery = RecoveryManager()
        self._register_repairs()
        self.scheduler = Scheduler(self.store, tick_seconds=self.config.scheduler_tick_seconds)
        self.planner = Planner(self.router, self.tools, store=self.store, memory=self.memory)
        self.executive = Executive(
            store=self.store, policy=self.policy, tools=self.tools, planner=self.planner,
            verifier=self.verifier, recovery=self.recovery, scheduler=self.scheduler,
            memory=self.memory, bus=self.bus, approver=self.approver, root=self.root,
            router=self.router, step_retry_limit=self.config.step_retry_limit,
        )
        self.odysseus = Odysseus(self.store, self.executive, self.scheduler, self.memory,
                                 bus=self.bus)
        self.evolver = Evolver(self.store, self.memory, executive=self.executive,
                               router=self.router, policy=self.policy)

        self.boot_report: Dict[str, Any] = {"version": __version__, "resumed": {}}
        if resume_on_start:
            self.boot_report["resumed"] = self.odysseus.resume()
        self.store.set_setting("last_boot", now())
        self.bus.publish("system.booted", source="jarvis",
                         payload={"version": __version__, "root": self.root})

    # ------------------------------------------------------------------ #
    # conversation and tasks
    # ------------------------------------------------------------------ #
    def request(self, intent: str, goal_id: str = "", priority: int = 5,
                run_immediately: bool = True,
                success_criteria: Optional[Sequence[Dict[str, Any]]] = None,
                plan: Optional[Dict[str, Any]] = None,
                context: Optional[Dict[str, Any]] = None,
                trigger: Trigger = Trigger.USER_REQUEST) -> Task:
        """An immediate request: 'investigate this bug, fix it and show me the result'."""
        payload = dict(context or {})
        if plan:
            payload["plan"] = plan
        criteria = [as_check(c) for c in (success_criteria or [])]
        return self.executive.submit(
            intent, goal_id=goal_id, priority=priority, run_immediately=run_immediately,
            success_criteria=criteria, context=payload, trigger=trigger,
        )

    def ask(self, intent: str, **kwargs: Any) -> Dict[str, Any]:
        """Run a request and return a compact, human-readable outcome."""
        task = self.request(intent, **kwargs)
        return self.task_view(task.id)

    def task_view(self, task_id: str) -> Dict[str, Any]:
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        verification = self.store.verification_for(task_id)
        return {
            "id": task.id, "intent": task.intent, "state": task.state.value,
            "phase": task.phase.value, "verdict": task.verdict, "objective": task.objective,
            "goal_id": task.goal_id, "model": task.model, "attempts": task.attempts,
            "error": task.error, "error_kind": task.error_kind,
            "duration": round(task.duration, 3),
            "plan": [s.to_dict() for s in task.plan],
            "result": task.result,
            "verification": verification.to_dict() if verification else None,
            "decisions": [d.to_dict() for d in self.store.decisions_for(task_id)],
            "approvals": [a.to_dict() for a in self.store.approvals_for(task_id)],
            "checkpoints": self.store.checkpoint_history(task_id),
        }

    def tasks(self, states: Optional[Sequence[str]] = None, goal_id: str = "",
              limit: int = 100) -> List[Dict[str, Any]]:
        tasks = self.store.list_tasks(states=states, goal_id=goal_id or None, limit=limit)
        return [
            {
                "id": t.id, "intent": t.intent, "state": t.state.value, "phase": t.phase.value,
                "verdict": t.verdict, "goal_id": t.goal_id, "priority": t.priority,
                "trigger": t.trigger.value, "error": t.error, "attempts": t.attempts,
                "created_at": t.created_at, "updated_at": t.updated_at,
                "model": t.model, "duration": round(t.duration, 3),
            }
            for t in tasks
        ]

    def cancel(self, task_id: str) -> bool:
        return self.executive.cancel(task_id)

    # ------------------------------------------------------------------ #
    # long-running goals
    # ------------------------------------------------------------------ #
    def adopt_goal(self, title: str, objective: str, **kwargs: Any) -> Goal:
        """A long-term goal: 'manage this project until it reaches a tested release'."""
        return self.odysseus.adopt_goal(title, objective, **kwargs)

    def goals(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": g.id, "title": g.title, "objective": g.objective,
                "state": g.state.value, "priority": g.priority, "autonomy": g.autonomy,
                "milestones": g.milestones, "monitors": g.monitors,
                "notes": g.notes[-3:], "progress": g.metadata.get("progress", {}),
            }
            for g in self.store.list_goals()
        ]

    def goal_report(self, goal_id: str) -> Dict[str, Any]:
        return self.odysseus.report(goal_id)

    def cancel_goal(self, goal_id: str, reason: str = "cancelled by user") -> Goal:
        return self.odysseus.cancel(goal_id, reason)

    def advance(self, max_tasks: Optional[int] = None) -> Dict[str, Any]:
        """One supervision pass over every durable goal."""
        return self.odysseus.advance(max_tasks=max_tasks)

    # ------------------------------------------------------------------ #
    # proactive operation
    # ------------------------------------------------------------------ #
    def event(self, kind: str, payload: Optional[Dict[str, Any]] = None,
              run_immediately: bool = False) -> List[Dict[str, Any]]:
        created = self.executive.handle_event(kind, payload, run_immediately=run_immediately)
        return [{"id": t.id, "intent": t.intent, "goal_id": t.goal_id} for t in created]

    def tick(self) -> Dict[str, Any]:
        """Called by the scheduler loop: due work, due monitors, one review."""
        queued: List[str] = []
        for task in self.scheduler.due_scheduled():
            queued.append(task.id)
        for spec in self.scheduler.tick_monitors():
            task = Task(
                intent=spec["intent"], goal_id=spec.get("goal_id", ""),
                trigger=spec.get("trigger", Trigger.SCHEDULE), state=TaskState.QUEUED,
                priority=int(spec.get("priority", 5)), context=dict(spec.get("context") or {}),
            )
            self.store.save_task(task)
            queued.append(task.id)
        outcomes = self.executive.run()
        return {"queued": queued, "executed": len(outcomes)}

    # ------------------------------------------------------------------ #
    # control and permissions
    # ------------------------------------------------------------------ #
    def halt(self) -> None:
        self.executive.halt()

    def resume(self) -> None:
        self.executive.resume()

    def set_mode(self, mode: str) -> None:
        self.policy.set_mode(mode)
        self.config.mode = mode
        self.store.set_setting("mode", mode)
        self.store.audit("user", "mode_changed", mode)

    def approvals(self) -> List[Dict[str, Any]]:
        return [a.to_dict() for a in self.store.pending_approvals()]

    def approve(self, approval_id: str, approve: bool, note: str = "") -> Dict[str, Any]:
        """Decide an approval, then continue the parked task it was blocking."""
        record = self.approver.decide(approval_id, approve, note).to_dict()
        task_id = record.get("task_id") or ""
        task = self.store.get_task(task_id) if task_id else None
        if task is not None and task.state.value == "awaiting_approval":
            record["outcome"] = self.executive.resume_task(task_id).to_dict()
        return record

    # ------------------------------------------------------------------ #
    # memory
    # ------------------------------------------------------------------ #
    def remember(self, content: str, kind: str = "fact", tags: Sequence[str] = (),
                 importance: float = 0.5) -> Dict[str, Any]:
        return self.memory.remember(content, kind=kind, tags=tags,
                                    importance=importance).to_dict()

    def recall(self, query: str, limit: int = 8) -> List[Dict[str, Any]]:
        return [
            {"id": h["memory"].id, "kind": h["memory"].kind, "content": h["memory"].content,
             "tags": h["memory"].tags, "score": h["score"]}
            for h in self.memory.recall(query, limit=limit)
        ]

    def forget(self, memory_id: str) -> bool:
        return self.memory.forget(memory_id)

    def memories(self) -> List[Dict[str, Any]]:
        return [m.to_dict() for m in self.memory.all()]

    # ------------------------------------------------------------------ #
    # self-evaluation
    # ------------------------------------------------------------------ #
    def review(self) -> Dict[str, Any]:
        return self.evolver.review()

    def proposals(self) -> List[Dict[str, Any]]:
        return self.store.list_proposals()

    def decide_proposal(self, proposal_id: str, approve: bool, note: str = "user") -> Dict[str, Any]:
        return self.evolver.apply(proposal_id, approve, note)

    # ------------------------------------------------------------------ #
    # status
    # ------------------------------------------------------------------ #
    def status(self) -> Dict[str, Any]:
        counts = self.store.count_tasks()
        return {
            "version": __version__,
            "banner": banner(),
            "halted": self.policy.halted,
            "config": self.config.to_dict(),
            "policy": self.policy.describe(),
            "task_counts": counts,
            "pending_approvals": len(self.store.pending_approvals()),
            "goals": [
                {"id": g.id, "title": g.title, "state": g.state.value}
                for g in self.store.list_goals()
            ],
            "memory": self.memory.summarize(),
            "models": self.router.describe(),
            "model_usage": self.router.usage_summary(),
            "tools": self.tools.describe(),
            "tool_usage": self.tools.usage_summary(),
            "recovery_history": self.recovery.history[-10:],
            "recent_events": self.store.recent_events(20),
            "audit": self.store.audit_log(20),
            "proposals": self.store.list_proposals("pending"),
        }

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    def _register_repairs(self) -> None:
        """Repairs change the approach instead of repeating the same call."""

        def extend_timeout(task: Task, error: str):
            from jarvis.core.recovery import RepairResult

            if "timed out" not in error.lower() and "timeout" not in error.lower():
                return RepairResult(False, "not a timeout failure")
            current = int(task.context.get("timeout", 300))
            task.context["timeout"] = min(current * 2, 3600)
            self.store.save_task(task)
            return RepairResult(True, f"timeout raised from {current}s to "
                                      f"{task.context['timeout']}s", ["extend_timeout"])

        def clear_failed_optional_steps(task: Task, error: str):
            from jarvis.core.recovery import RepairResult

            changed = []
            for step in task.plan:
                if step.optional and step.state.value == "failed":
                    step.state = step.state.__class__("skipped")
                    changed.append(step.tool)
            if not changed:
                return RepairResult(False, "no optional steps to skip")
            self.store.save_task(task)
            return RepairResult(True, f"skipped optional steps: {', '.join(changed)}",
                                ["clear_failed_optional_steps"])

        self.recovery.register_repair("extend_timeout", extend_timeout)
        self.recovery.register_repair("skip_optional", clear_failed_optional_steps)

    def close(self) -> None:
        """Idempotent shutdown: safe to call from a fixture after an explicit close."""
        if self.store.closed:
            return
        self.store.audit("jarvis", "shutdown", {"version": __version__})
        self.store.close()

    def __enter__(self) -> "Jarvis":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


__all__ = ["Jarvis", "Halted", "ToolContext"]
