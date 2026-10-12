"""Scheduling: which eligible work runs next, and what proactive work appears.

Two responsibilities:

* **Selection** - order queued tasks by priority, then age, honouring goal priority.
* **Triggers** - turn monitors attached to durable goals (schedule ticks, incoming
  events, thresholds) into new tasks, de-duplicated so a noisy event cannot flood
  the queue.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from jarvis.core.models import Goal, Task, Trigger, now


class Scheduler:
    def __init__(self, store, max_concurrent: int = 1, tick_seconds: float = 60.0):
        self.store = store
        self.max_concurrent = max_concurrent
        self.tick_seconds = tick_seconds
        self._last_trigger: Dict[str, float] = {}

    # -- selection ---------------------------------------------------------- #
    def eligible(self, limit: Optional[int] = None) -> List[Task]:
        tasks = self.store.list_tasks(states=["queued", "running", "verifying"])
        goal_priority = {g.id: g.priority for g in self.store.list_goals()}
        tasks.sort(
            key=lambda t: (
                -(t.priority * 10 + goal_priority.get(t.goal_id, 5)),
                t.created_at,
            )
        )
        return tasks[: limit or self.max_concurrent]

    def next_task(self) -> Optional[Task]:
        eligible = self.eligible(limit=1)
        return eligible[0] if eligible else None

    def due_scheduled(self) -> List[Task]:
        """Tasks queued by a schedule trigger whose due time has passed."""
        ready = now()
        out = []
        for task in self.store.list_tasks(states=["queued"]):
            if task.trigger is not Trigger.SCHEDULE:
                continue
            due = float(task.context.get("due_at") or 0.0)
            if due <= ready:
                out.append(task)
        return out

    # -- triggers ----------------------------------------------------------- #
    def evaluate_monitors(self, event_kind: str, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Return task specs for every monitor that this event satisfies."""
        specs: List[Dict[str, Any]] = []
        for goal in self.store.list_goals(states=["active", "blocked", "paused"]):
            for monitor in goal.monitors:
                if not monitor.get("enabled", True):
                    continue
                if monitor.get("trigger") not in ("event", "any", event_kind):
                    continue
                pattern = monitor.get("kind")
                if pattern and pattern != "*" and pattern != event_kind:
                    continue
                key = f"{goal.id}:{monitor.get('name', pattern or 'event')}"
                cooldown = float(monitor.get("cooldown_seconds", self.tick_seconds))
                last = self._last_trigger.get(key, 0.0)
                if time.time() - last < cooldown:
                    continue
                self._last_trigger[key] = time.time()
                specs.append(
                    {
                        "goal_id": goal.id,
                        "intent": monitor.get("intent", f"Investigate {event_kind}"),
                        "trigger": Trigger.PROACTIVE,
                        "priority": int(monitor.get("priority", goal.priority)),
                        "context": {"monitor": monitor.get("name", ""), "event": event_kind,
                                    "event_payload": payload},
                    }
                )
        return specs

    def tick_monitors(self) -> List[Dict[str, Any]]:
        """Time-based monitors: due when ``interval_seconds`` elapsed since last run."""
        specs: List[Dict[str, Any]] = []
        current = time.time()
        for goal in self.store.list_goals(states=["active", "blocked"]):
            for monitor in goal.monitors:
                if monitor.get("trigger") != "schedule" or not monitor.get("enabled", True):
                    continue
                interval = float(monitor.get("interval_seconds", 3600))
                key = f"{goal.id}:{monitor.get('name', 'tick')}"
                last = self._last_trigger.get(key, 0.0)
                if current - last < interval:
                    continue
                self._last_trigger[key] = current
                specs.append(
                    {
                        "goal_id": goal.id,
                        "intent": monitor.get("intent", f"Run scheduled check for {goal.title}"),
                        "trigger": Trigger.SCHEDULE,
                        "priority": int(monitor.get("priority", goal.priority)),
                        "context": {"monitor": monitor.get("name", ""), "due_at": current},
                    }
                )
        return specs

    def goal_progress(self, goal: Goal) -> Tuple[int, int, int]:
        """(completed, total, blocked) task counts for a goal."""
        tasks = self.store.list_tasks(goal_id=goal.id, limit=10_000)
        completed = sum(1 for t in tasks if t.state.value == "completed")
        blocked = sum(1 for t in tasks if t.state.value in ("blocked", "failed"))
        return completed, len(tasks), blocked
