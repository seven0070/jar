"""Odysseus: long-running goal management.

Odysseus is the part of JARVIS that keeps working when nobody is watching. It owns
the durable goals, turns milestones into tasks, reacts to events through monitors,
resumes after a crash or a restart, and reports honest progress against a goal's
definition of done.

It deliberately holds no intelligence of its own - it schedules and supervises, and
asks the Executive to do the actual work.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence

from jarvis.core.models import (
    Check,
    as_check,
    Decision,
    Goal,
    GoalState,
    Task,
    TaskState,
    Trigger,
    Verdict,
    new_id,
    now,
)


class Odysseus:
    def __init__(self, store, executive, scheduler, memory, bus=None):
        self.store = store
        self.executive = executive
        self.scheduler = scheduler
        self.memory = memory
        self.bus = bus

    # ------------------------------------------------------------------ #
    # goal lifecycle
    # ------------------------------------------------------------------ #
    def adopt_goal(
        self,
        title: str,
        objective: str,
        definition_of_done: Optional[Sequence[Dict[str, Any]]] = None,
        milestones: Optional[Sequence[str]] = None,
        monitors: Optional[Sequence[Dict[str, Any]]] = None,
        autonomy: str = "standard",
        priority: int = 5,
        deadline_hours: float = 0.0,
        run_first: bool = True,
    ) -> Goal:
        """Register a durable objective and start working it."""
        goal = Goal(
            id=new_id("goal"), title=title, objective=objective,
            definition_of_done=[as_check(c) for c in (definition_of_done or [])],
            milestones=list(milestones or []), monitors=list(monitors or []),
            state=GoalState.ACTIVE, priority=int(priority), autonomy=autonomy,
            deadline=time.time() + deadline_hours * 3600 if deadline_hours else 0.0,
        )
        self.store.save_goal(goal)
        self.store.audit("odysseus", "goal_adopted",
                         {"goal": goal.id, "title": title, "milestones": goal.milestones})
        self.memory.remember(
            f"Long-term goal adopted: {title} - {objective}",
            kind="fact", tags=["goal", title.lower().replace(" ", "-")],
            goal_id=goal.id, importance=0.9,
        )
        self._publish("goal.adopted", {"goal": goal.id, "title": title})

        queued = self.queue_milestones(goal, limit=1)
        if run_first:
            for task in queued:
                self.executive.run_cycle(task.id)
        return self.store.get_goal(goal.id) or goal

    def queue_milestones(self, goal: Goal, limit: Optional[int] = None) -> List[Task]:
        """Create tasks for milestones that have not been completed yet."""
        tasks = self.store.list_tasks(goal_id=goal.id, limit=10_000)
        done = {t.intent for t in tasks if t.state is TaskState.COMPLETED}
        live = {t.intent for t in tasks if not t.state.terminal}
        created: List[Task] = []
        for milestone in goal.milestones:
            if milestone in done or milestone in live:
                continue
            task = Task(
                intent=milestone, goal_id=goal.id, trigger=Trigger.GOAL,
                state=TaskState.QUEUED, priority=goal.priority,
                context={"milestone": milestone},
            )
            self.store.save_task(task)
            created.append(task)
            self.store.audit("odysseus", "milestone_queued",
                             {"goal": goal.id, "task": task.id, "milestone": milestone})
            if limit and len(created) >= limit:
                break
        return created

    def set_state(self, goal_id: str, state: GoalState, note: str = "") -> Goal:
        goal = self.store.get_goal(goal_id)
        if goal is None:
            raise KeyError(goal_id)
        goal.state = state
        if note:
            goal.notes.append(note)
        self.store.save_goal(goal)
        self.store.audit("odysseus", "goal_state", {"goal": goal_id, "state": state.value})
        self._publish("goal.state", {"goal": goal_id, "state": state.value})
        return goal

    def cancel(self, goal_id: str, reason: str = "cancelled by user") -> Goal:
        for task in self.store.list_tasks(goal_id=goal_id, limit=10_000):
            self.executive.cancel(task.id)
        return self.set_state(goal_id, GoalState.CANCELLED, reason)

    # ------------------------------------------------------------------ #
    # supervision
    # ------------------------------------------------------------------ #
    def advance(self, max_tasks: Optional[int] = None) -> Dict[str, Any]:
        """One supervision pass: queue due work, run it, then re-score every goal."""
        queued: List[str] = []
        for goal in self.store.list_goals(states=["active", "blocked"]):
            for task in self.queue_milestones(goal, limit=1):
                queued.append(task.id)
        for spec in self.scheduler.tick_monitors():
            task = Task(
                intent=spec["intent"], goal_id=spec.get("goal_id", ""),
                trigger=spec.get("trigger", Trigger.SCHEDULE), state=TaskState.QUEUED,
                priority=int(spec.get("priority", 5)), context=dict(spec.get("context") or {}),
            )
            self.store.save_task(task)
            queued.append(task.id)

        outcomes = self.executive.run(max_tasks=max_tasks)
        report = {"queued": queued, "executed": len(outcomes), "goals": []}
        for goal in self.store.list_goals(states=["active", "blocked", "proposed"]):
            report["goals"].append(self.report(goal.id))
        return report

    def report(self, goal_id: str) -> Dict[str, Any]:
        goal = self.store.get_goal(goal_id)
        if goal is None:
            raise KeyError(goal_id)
        tasks = self.store.list_tasks(goal_id=goal_id, limit=10_000)
        completed = [t for t in tasks if t.state is TaskState.COMPLETED]
        verified = [t for t in completed if t.verdict == Verdict.VERIFIED.value]
        blocked = [t for t in tasks if t.state in (TaskState.BLOCKED, TaskState.FAILED)]
        milestones_done = [m for m in goal.milestones
                           if any(t.intent == m and t.state is TaskState.COMPLETED for t in tasks)]
        return {
            "goal_id": goal.id,
            "title": goal.title,
            "state": goal.state.value,
            "objective": goal.objective,
            "tasks": len(tasks),
            "completed": len(completed),
            "verified": len(verified),
            "blocked": len(blocked),
            "milestones_done": milestones_done,
            "milestones_total": len(goal.milestones),
            "definition_of_done": [c.to_dict() for c in goal.definition_of_done],
            "notes": goal.notes[-5:],
            "progress": goal.metadata.get("progress", {}),
        }

    def check_definition_of_done(self, goal_id: str) -> bool:
        """Independently verify a goal's definition of done."""
        goal = self.store.get_goal(goal_id)
        if goal is None or not goal.definition_of_done:
            return False
        probe = Task(id=new_id("task"), intent=f"definition of done for {goal.title}",
                     success_criteria=[Check.from_dict(c.to_dict())
                                       for c in goal.definition_of_done])
        verification = self.executive.verifier.verify(probe, store=self.store)
        done = verification.verdict.ok
        if done and goal.state is not GoalState.COMPLETED:
            self.set_state(goal_id, GoalState.COMPLETED, "definition of done verified by Odysseus")
        return done

    # ------------------------------------------------------------------ #
    # durability
    # ------------------------------------------------------------------ #
    def resume(self) -> Dict[str, Any]:
        """Re-establish state after a restart: re-queue work, replay events."""
        resumed = self.executive.resume_interrupted()
        replayed = []
        if self.bus is not None:
            replayed = [e.kind for e in self.bus.drain_pending()]
        requeued = []
        for goal in self.store.list_goals(states=["active", "blocked"]):
            for task in self.queue_milestones(goal):
                requeued.append(task.id)
        report = {"resumed_tasks": resumed, "replayed_events": replayed, "requeued": requeued}
        self.store.audit("odysseus", "resumed", report)
        self._publish("odysseus.resumed", report)
        return report

    def _publish(self, kind: str, payload: Dict[str, Any]) -> None:
        if self.bus is not None:
            self.bus.publish(kind, source="odysseus", payload=payload)
