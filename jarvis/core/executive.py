"""JARVIS Executive: the operating cycle.

One task walks nine phases, and every phase is persisted before the next one
starts, so an interrupted run can be resumed exactly where it stopped:

    received -> retrieved -> objective_defined -> planned -> authorized
             -> executed -> verified -> repaired -> memory_updated

Nothing is marked complete on the strength of a tool's own report: the
``verified`` phase re-derives the answer from the outside world, and a task that
cannot be verified becomes a blocker with evidence attached.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from jarvis.core.models import (
    Check,
    Decision,
    as_check,
    Goal,
    GoalState,
    Memory,
    Phase,
    Step,
    StepState,
    Task,
    TaskState,
    Trigger,
    Verdict,
    new_id,
    now,
)
from jarvis.core.recovery import RecoveryAction, RecoveryManager
from jarvis.errors import Halted, JarvisError, PermissionDenied, PlannerError
from jarvis.tools.base import ToolContext, ToolResult


@dataclass
class CycleOutcome:
    """What one pass of the operating cycle produced."""

    task_id: str
    state: str
    phase: str
    verdict: str = ""
    deferred: bool = False
    halted: bool = False
    message: str = ""
    result: Any = None
    evidence: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id, "state": self.state, "phase": self.phase,
            "verdict": self.verdict, "deferred": self.deferred, "halted": self.halted,
            "message": self.message, "evidence": self.evidence,
        }

    def update(self, **changes: Any) -> "CycleOutcome":
        """Apply partial updates; unknown keys are rejected loudly."""
        for key, value in changes.items():
            if not hasattr(self, key):
                raise AttributeError(f"CycleOutcome has no field {key!r}")
            setattr(self, key, value)
        return self


class StepFailure(JarvisError):
    """A required step could not be completed after recovery attempts."""

    def __init__(self, message: str, kind: str = "tool_error"):
        super().__init__(message)
        self.kind = kind
        self.recoverable = kind in ("transient", "tool_error", "verification_failed")


class Executive:
    """Understands intent, manages goals and decides what happens next."""

    def __init__(self, store, policy, tools, planner, verifier, recovery: RecoveryManager,
                 scheduler, memory, bus=None, approver=None, root: str = ".",
                 router=None, step_retry_limit: int = 3):
        self.store = store
        self.policy = policy
        self.tools = tools
        self.planner = planner
        self.verifier = verifier
        self.recovery = recovery
        self.scheduler = scheduler
        self.memory = memory
        self.bus = bus
        self.approver = approver
        self.root = root
        self.router = router
        self.step_retry_limit = step_retry_limit
        self.outcomes: List[CycleOutcome] = []

    # ------------------------------------------------------------------ #
    # intake
    # ------------------------------------------------------------------ #
    def submit(self, intent: str, goal_id: str = "", priority: int = 5,
               trigger: Trigger = Trigger.USER_REQUEST,
               context: Optional[Dict[str, Any]] = None,
               success_criteria: Optional[List[Check]] = None,
               run_immediately: bool = True) -> Task:
        """Accept a request, persist it, and (by default) run it now."""
        task = Task(
            id=new_id("task"), intent=intent.strip(), goal_id=goal_id, trigger=trigger,
            state=TaskState.QUEUED, phase=Phase.RECEIVED, priority=int(priority),
            context=dict(context or {}), success_criteria=list(success_criteria or []),
        )
        self.store.save_task(task)
        self.store.audit("executive", "task_received",
                         {"task": task.id, "intent": intent[:200], "goal": goal_id})
        self._publish("task.received", {"task": task.id, "intent": intent,
                                        "trigger": trigger.value, "goal": goal_id})
        if run_immediately:
            self.run_until_settled(task.id)
        return self.store.get_task(task.id) or task

    def handle_event(self, kind: str, payload: Optional[Dict[str, Any]] = None,
                     source: str = "external", run_immediately: bool = False) -> List[Task]:
        """React to an event: persist it, then create any proactive work it triggers."""
        payload = dict(payload or {})
        self._publish(kind, payload, source=source)
        created: List[Task] = []
        for spec in self.scheduler.evaluate_monitors(kind, payload):
            task = Task(
                intent=spec["intent"], goal_id=spec.get("goal_id", ""),
                trigger=spec.get("trigger", Trigger.PROACTIVE),
                state=TaskState.QUEUED, phase=Phase.RECEIVED,
                priority=int(spec.get("priority", 5)), context=dict(spec.get("context") or {}),
            )
            self.store.save_task(task)
            self.store.audit("executive", "proactive_task",
                             {"task": task.id, "event": kind, "goal": task.goal_id})
            created.append(task)
            if run_immediately:
                self.run_until_settled(task.id)
        return created

    # ------------------------------------------------------------------ #
    # the cycle
    # ------------------------------------------------------------------ #
    def run_cycle(self, task_id: str) -> CycleOutcome:
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        self.policy.assert_running()

        outcome = CycleOutcome(task_id=task.id, state=task.state.value, phase=task.phase.value)
        try:
            self._phase_received(task)
            self._phase_retrieved(task)
            self._phase_objective(task)
            self._phase_planned(task)
            self._phase_authorized(task)
            deferred = self._phase_executed(task)
            if deferred:
                outcome.update(deferred=True, message="re-queued for another attempt")
                return self._finish(outcome, task)
            self._phase_verified(task)
            self._phase_repaired(task)
            self._phase_committed(task)
        except _AwaitingApproval as exc:
            outcome.update(state=task.state.value, phase=task.phase.value,
                           message=str(exc))
            return self._finish(outcome, task)
        except _Deferred:
            outcome.update(deferred=True, state=task.state.value, phase=task.phase.value,
                           verdict=task.verdict, message="verification failed; re-queued")
            return self._finish(outcome, task)
        except Halted as exc:
            task.error = str(exc)
            task.error_kind = "halted"
            task.state = TaskState.QUEUED      # resumable once the stop is lifted
            self.store.save_task(task)
            self.store.checkpoint(task)
            self._publish("task.halted", {"task": task.id, "phase": task.phase.value})
            outcome.update(halted=True, state=task.state.value, phase=task.phase.value,
                           message=str(exc))
            return self._finish(outcome, task)
        except JarvisError as exc:
            self._escalate(task, exc)
            outcome.update(state=task.state.value, phase=task.phase.value, message=str(exc))
            return self._finish(outcome, task)
        except Exception as exc:                       # never leave a task unaccounted for
            wrapped = JarvisError(f"{type(exc).__name__}: {exc}")
            wrapped.kind = "tool_error"
            self._escalate(task, wrapped)
            outcome.update(state=task.state.value, phase=task.phase.value, message=str(exc))
            return self._finish(outcome, task)

        outcome.update(state=task.state.value, phase=task.phase.value, verdict=task.verdict,
                       result=task.result, evidence=self._evidence_for(task))
        return self._finish(outcome, task)

    def run_until_settled(self, task_id: str, max_passes: int = 8) -> Optional[CycleOutcome]:
        """Keep cycling one task until it is terminal, parked for approval, or halted.

        Retries and repairs re-queue the task, so a single pass is not enough to
        answer the user. The pass count bounds the loop independently of the
        attempt budget, so a mis-specified plan cannot spin forever.
        """
        outcome: Optional[CycleOutcome] = None
        for _ in range(max(max_passes, 1)):
            task = self.store.get_task(task_id)
            if task is None or task.state.terminal:
                break
            if task.state is TaskState.AWAITING_APPROVAL:
                break
            outcome = self.run_cycle(task_id)
            if self.policy.halted or outcome.halted:
                break
        return outcome

    def run_once(self) -> Optional[CycleOutcome]:
        """Run the next eligible task, if any."""
        task = self.scheduler.next_task()
        if task is None:
            return None
        return self.run_cycle(task.id)

    def run(self, max_tasks: Optional[int] = None, idle_poll: float = 0.0,
            stop_when_idle: bool = True) -> List[CycleOutcome]:
        """Run work until the queue is empty (or the emergency stop engages)."""
        outcomes: List[CycleOutcome] = []
        while not self.policy.halted:
            outcome = self.run_once()
            if outcome is None:
                if stop_when_idle:
                    break
                if idle_poll:
                    time.sleep(idle_poll)
                continue
            outcomes.append(outcome)
            if max_tasks and len(outcomes) >= max_tasks:
                break
        return outcomes

    # ------------------------------------------------------------------ #
    # phases
    # ------------------------------------------------------------------ #
    def _phase_received(self, task: Task) -> None:
        if task.started_at == 0.0:
            task.started_at = now()
        task.state = TaskState.RUNNING
        # A resumed task must keep the phase it reached. Rewinding it here would
        # make the planning phase rebuild the plan and throw away finished steps.
        if task.phase.order < Phase.RETRIEVED.order:
            task.phase = Phase.RECEIVED
        self._persist(task, "received")

    def _phase_retrieved(self, task: Task) -> None:
        """Pull the memory that matters for this request."""
        hits = self.memory.recall(task.intent, limit=8, goal_id=task.goal_id)
        task.context["memory"] = [
            {"id": h["memory"].id, "kind": h["memory"].kind, "content": h["memory"].content,
             "score": h["score"]}
            for h in hits
        ]
        task.context["memory_block"] = self.memory.context_block(
            task.intent, goal_id=task.goal_id, limit=5
        )
        goal = self.store.get_goal(task.goal_id) if task.goal_id else None
        if goal is not None:
            task.context["goal"] = {"title": goal.title, "objective": goal.objective,
                                    "autonomy": goal.autonomy}
        self.store.add_decision(Decision(
            task_id=task.id, kind="memory_retrieved",
            rationale=f"recalled {len(hits)} memories for this request",
            payload={"ids": [h["memory"].id for h in hits]},
        ))
        self._advance(task, Phase.RETRIEVED,
                      {"recalled": len(hits), "goal": bool(goal)})

    def _phase_objective(self, task: Task) -> None:
        """Decide what success means before deciding how to get there."""
        goal = self.store.get_goal(task.goal_id) if task.goal_id else None
        if not task.objective:
            if goal is not None and goal.objective:
                task.objective = f"{goal.objective} :: {task.intent}"
            else:
                task.objective = task.intent
        self.store.add_decision(Decision(
            task_id=task.id, kind="objective_defined",
            rationale="objective fixed before planning so success can be judged independently",
            payload={"objective": task.objective,
                     "criteria_from_user": len(task.success_criteria)},
        ))
        self._advance(task, Phase.OBJECTIVE, {"objective": task.objective})

    def _phase_planned(self, task: Task) -> None:
        if task.plan and task.phase.order >= Phase.PLANNED.order:
            return
        plan = self.planner.plan(
            task.intent, context=task.context, goal_id=task.goal_id, task_id=task.id
        )
        task.plan = plan.steps
        task.objective = task.objective or plan.objective
        task.model = plan.model
        task.context["plan_source"] = plan.source
        task.context["plan_notes"] = plan.notes
        # Criteria supplied by the caller are authoritative and always kept.
        for check in plan.success_criteria:
            if not any(_same_check(check, existing) for existing in task.success_criteria):
                task.success_criteria.append(check)
        self._advance(task, Phase.PLANNED,
                      {"steps": [s.tool for s in task.plan], "source": plan.source,
                       "criteria": len(task.success_criteria)})

    def _phase_authorized(self, task: Task) -> None:
        """Ask once, up front, for the most sensitive capability the plan needs."""
        self.policy.assert_running()
        scopes: Dict[str, int] = {}
        for step in task.plan:
            tool = self.tools.get(step.tool)
            for scope in tool.requires:
                scopes[scope] = max(scopes.get(scope, 0), tool.risk.level)
        if not scopes:
            self._advance(task, Phase.AUTHORIZED, {"scopes": [], "approved": True})
            return

        top_scope, top_level = max(scopes.items(), key=lambda item: item[1])
        decision = self.policy.evaluate(top_scope, _risk_for_level(top_level))
        if not decision.allow:
            raise PermissionDenied(f"{top_scope}: {decision.reason}")

        if decision.needs_approval:
            previous = self._prior_approval(task, top_scope)
            if previous is not None and previous.state == "denied":
                raise PermissionDenied(
                    f"{top_scope}: approval was denied - {previous.note or 'no reason given'}"
                )
            if previous is None and self.approver is None:
                raise PermissionDenied(
                    f"{top_scope}: approval required ({decision.reason}) "
                    "but no approver is available"
                )
            approval = previous if previous is not None and previous.state == "approved" \
                else self.approver.request(
                task_id=task.id, action=f"execute plan for {task.intent[:80]}",
                scope=top_scope, risk=_risk_for_level(top_level).value,
                detail="; ".join(sorted(scopes)),
            )
            if approval.id not in task.approvals:
                task.approvals.append(approval.id)
            if approval.state == "pending":
                # Park the task; it resumes the moment a decision is recorded.
                task.state = TaskState.AWAITING_APPROVAL
                task.context["awaiting_approval"] = approval.id
                self.store.save_task(task)
                self.store.checkpoint(task)
                self._publish("task.awaiting_approval",
                              {"task": task.id, "approval": approval.id, "scope": top_scope})
                raise _AwaitingApproval(approval.id)
            if approval.state != "approved":
                raise PermissionDenied(
                    f"{top_scope}: approval {approval.state} - {approval.note or decision.reason}"
                )
        task.context.pop("awaiting_approval", None)
        task.context["approved_scopes"] = sorted(scopes)
        self._advance(task, Phase.AUTHORIZED, {"scopes": sorted(scopes), "approved": True})

    def _phase_executed(self, task: Task) -> bool:
        """Run the plan. Returns True when the task was re-queued for another attempt."""
        task.state = TaskState.RUNNING
        self._persist(task, "executing")
        outputs: Dict[str, Any] = dict(task.context.get("outputs") or {})
        task.context.setdefault("variables", {})
        task.context["variables"].update({"intent": task.intent, "outputs": outputs})

        for step in task.plan:
            if step.state in (StepState.SUCCEEDED, StepState.SKIPPED):
                continue
            failure = self._run_step(task, step, outputs)
            if failure is None:
                continue
            # Recovery decides whether the whole task gets another pass.
            action = self.recovery.decide(task, failure)
            self.store.add_decision(Decision(
                task_id=task.id, kind="recovery", rationale=f"{action.action}: {action.reason}",
                payload={"step": step.tool, "error": failure[:200], "action": action.action},
            ))
            # A retry repeats the step; a repair changes the approach. Both deserve
            # another pass, but the attempt budget still bounds the loop.
            if action.action in ("retry", "repair") and task.attempts <= task.max_attempts:
                self.recovery.sleep(action.delay)
                task.attempts += 1
                task.context["outputs"] = outputs
                task.phase = Phase.EXECUTED
                task.state = TaskState.QUEUED
                task.context.setdefault("recovery_log", []).append(
                    {"action": action.action, "reason": action.reason,
                     "repair": action.repair, "attempt": task.attempts}
                )
                self.store.save_task(task)
                self.store.checkpoint(task)
                self._publish("task.deferred", {"task": task.id, "attempt": task.attempts,
                                                "action": action.action,
                                                "reason": action.reason})
                return True
            step.state = StepState.FAILED
            step.error = failure
            self._publish("step.failed", {"task": task.id, "step": step.tool, "error": failure})
            if step.optional:
                step.state = StepState.SKIPPED
                continue
            task.error = failure
            task.error_kind = self.recovery.classify(failure)
            self._advance(task, Phase.EXECUTED, {"completed_steps": _done_count(task),
                                                 "failed_step": step.tool})
            return False

        task.context["outputs"] = outputs
        task.result = _build_result(task, outputs)
        self._advance(task, Phase.EXECUTED, {"completed_steps": _done_count(task)})
        return False

    def _run_step(self, task: Task, step: Step, outputs: Dict[str, Any]) -> Optional[str]:
        """Execute one step with local retries. Returns an error string, or None on success."""
        step.state = StepState.RUNNING
        step.attempts += 1
        self.store.save_task(task)
        self._publish("step.started", {"task": task.id, "step": step.tool, "title": step.title})

        context = ToolContext(
            root=self.root, store=self.store, policy=self.policy, approver=self.approver,
            bus=self.bus, memory=self.memory, router=self.router, task_id=task.id,
            goal_id=task.goal_id, variables=task.context.get("variables", {}),
            timeout=int(task.context.get("timeout", 300)),
            granted_scopes=list(task.context.get("approved_scopes") or []),
        )
        args = _resolve_args(step.args, outputs)
        try:
            result: ToolResult = self.tools.execute(step.tool, args, context)
        except PermissionDenied as exc:
            step.state = StepState.FAILED
            step.error = str(exc)
            raise
        except Halted:
            raise

        if result.ok:
            step.state = StepState.SUCCEEDED
            step.output = _shrink(result.value)
            step.error = ""
            if step.produces:
                outputs[step.produces] = result.value
            self.store.save_task(task)
            self._publish("step.finished", {"task": task.id, "step": step.tool, "ok": True,
                                            "evidence": result.evidence})
            return None

        step.error = result.error
        self.store.save_task(task)
        self._publish("step.finished", {"task": task.id, "step": step.tool, "ok": False,
                                        "error": result.error})
        return result.error or f"{step.tool} failed"

    def _phase_verified(self, task: Task) -> None:
        task.state = TaskState.VERIFYING
        self._persist(task, "verifying")
        verification = self.verifier.verify(task, store=self.store)
        task.verdict = verification.verdict.value
        task.context["verification"] = verification.to_dict()
        self._publish("task.verified", {"task": task.id, "verdict": verification.verdict.value,
                                        "summary": verification.summary})
        if verification.verdict.ok:
            self._advance(task, Phase.VERIFIED, {"verdict": verification.verdict.value})
            return

        task.attempts += 1
        action = self.recovery.decide(task, task.error or verification.summary)
        will_retry = action.action == "retry" and self._has_work_left(task)
        taken = "retry" if will_retry else "escalate"
        if taken == "escalate" and action.action == "retry":
            action.reason += " (no unfinished steps left to retry)"
        self.store.add_decision(Decision(
            task_id=task.id, kind="verification_recovery",
            rationale=f"{taken}: {action.reason}",
            payload={"verdict": verification.verdict.value, "action": taken},
        ))
        self.recovery.history.append({"task": task.id, "kind": "verification_failed",
                                      "attempts": task.attempts, "action": taken})
        if will_retry:
            self._reset_failed_steps(task)
            task.phase = Phase.EXECUTED
            task.state = TaskState.QUEUED
            self.store.save_task(task)
            self.store.checkpoint(task)
            self._publish("task.deferred", {"task": task.id, "reason": "verification failed"})
            raise _Deferred()
        if not task.success_criteria:
            # The blocker reason must say why, not just that the verdict was not green.
            task.error = "unverifiable: no success criteria were declared for this task"
            task.error_kind = "unverifiable"
        else:
            # Keep the original cause: a tool error that also fails verification is
            # still a tool error, and the user needs the real reason.
            if not task.error:
                task.error = f"verification failed: {verification.summary}"
            if not task.error_kind:
                task.error_kind = "verification_failed"
        self._advance(task, Phase.VERIFIED, {"verdict": verification.verdict.value})

    def _phase_repaired(self, task: Task) -> None:
        """Record whether the work is done, blocked, or was repaired on the way."""
        if task.verdict == Verdict.VERIFIED.value:
            task.state = TaskState.COMPLETED
            task.error = ""
            task.error_kind = ""
            self._advance(task, Phase.REPAIRED, {"outcome": "completed"})
        else:
            task.state = TaskState.BLOCKED
            self._advance(task, Phase.REPAIRED,
                          {"outcome": "blocked", "reason": task.error[:300]})
            self._publish("task.blocked", {"task": task.id, "reason": task.error[:300]})
        task.finished_at = now()

    def _phase_committed(self, task: Task) -> None:
        """Update memory and the owning goal, then hand control back."""
        # The result summary was drafted during execution, before the verdict existed.
        summary = _summary_for(task)
        if isinstance(task.result, dict):
            task.result["summary"] = summary
            self.store.save_task(task)
        self.memory.remember(
            summary, kind="episode",
            tags=[task.trigger.value] + ([task.goal_id] if task.goal_id else []),
            goal_id=task.goal_id,
            importance=0.9 if task.verdict == Verdict.VERIFIED.value else 0.6,
        )
        if isinstance(task.result, dict):
            answer = task.result.get("answer")
            if isinstance(answer, str) and answer and answer != summary:
                self.memory.remember(answer[:2000], kind="fact",
                                     tags=["result", task.trigger.value],
                                     goal_id=task.goal_id, importance=0.7)
        self._update_goal(task)
        self._advance(task, Phase.COMMITTED, {"state": task.state.value})
        self._publish("task.completed" if task.state is TaskState.COMPLETED else "task.blocked",
                      {"task": task.id, "state": task.state.value, "verdict": task.verdict,
                       "summary": summary})

    # ------------------------------------------------------------------ #
    # control
    # ------------------------------------------------------------------ #
    def halt(self) -> None:
        """Emergency stop: refuse everything, keep state, wait for a human."""
        self.policy.halt()
        self.store.set_setting("halted", True)
        self.store.audit("executive", "halt", "emergency stop engaged")
        self._publish("system.halted", {"at": now()})

    def resume(self) -> None:
        self.policy.resume()
        self.store.set_setting("halted", False)
        self.store.audit("executive", "resume", "emergency stop lifted")
        self._publish("system.resumed", {"at": now()})

    @property
    def halted(self) -> bool:
        return self.policy.halted

    def resume_interrupted(self) -> List[str]:
        """After a restart, put anything mid-flight back on the queue."""
        resumed: List[str] = []
        for task in self.store.list_tasks(states=["running", "verifying"], limit=1000):
            task.state = TaskState.QUEUED
            task.attempts += 1
            task.context["resumed_at"] = now()
            if task.phase.order >= Phase.EXECUTED.order:
                task.phase = Phase.EXECUTED
            else:
                task.phase = Phase.RECEIVED
            for step in task.plan:
                if step.state is StepState.RUNNING:
                    step.state = StepState.PENDING     # nothing is actually running now
                    step.error = ""
            self.store.save_task(task)
            resumed.append(task.id)
        if resumed:
            self.store.audit("executive", "resumed", {"tasks": resumed})
            self._publish("system.resumed_tasks", {"tasks": resumed})
        return resumed

    def resume_task(self, task_id: str) -> CycleOutcome:
        """Continue a parked or interrupted task from its last checkpoint."""
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.state is TaskState.AWAITING_APPROVAL:
            approval_id = task.context.get("awaiting_approval")
            approval = self.store.get_approval(approval_id) if approval_id else None
            if approval is None or approval.state == "pending":
                return CycleOutcome(task_id=task.id, state=task.state.value,
                                    phase=task.phase.value,
                                    message="still awaiting an approval decision")
            if approval.state != "approved":
                task.state = TaskState.BLOCKED
                task.error = f"approval {approval.state}: {approval.note}"
                task.error_kind = "permission_denied"
                task.finished_at = now()
                self.store.save_task(task)
                self._publish("task.blocked",
                              {"task": task.id, "reason": task.error})
                return CycleOutcome(task_id=task.id, state=task.state.value,
                                    phase=task.phase.value, message=task.error)
        return self.run_cycle(task_id)

    def cancel(self, task_id: str) -> bool:
        task = self.store.get_task(task_id)
        if task is None or task.state.terminal:
            return False
        task.state = TaskState.CANCELLED
        task.finished_at = now()
        task.error = task.error or "cancelled by user"
        self.store.save_task(task)
        self.store.audit("user", "task_cancelled", {"task": task_id})
        self._publish("task.cancelled", {"task": task_id})
        return True

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _run_step_safely(self, *args, **kwargs):  # pragma: no cover - kept for symmetry
        return self._run_step(*args, **kwargs)

    def _prior_approval(self, task: Task, scope: str):
        """An approval already recorded for this task that covers ``scope``."""
        for approval in self.store.approvals_for(task.id):
            if approval.state not in ("approved", "denied"):
                continue
            covered = approval.scope == scope or scope in (approval.detail or "").split("; ")
            if covered:
                return approval
        return None

    def _has_work_left(self, task: Task) -> bool:
        return any(s.state in (StepState.PENDING, StepState.FAILED) for s in task.plan)

    def _reset_failed_steps(self, task: Task) -> None:
        for step in task.plan:
            if step.state is StepState.FAILED:
                step.state = StepState.PENDING
                step.error = ""

    def _update_goal(self, task: Task) -> None:
        if not task.goal_id:
            return
        goal = self.store.get_goal(task.goal_id)
        if goal is None:
            return
        completed, total, blocked = self.scheduler.goal_progress(goal)
        goal.metadata["progress"] = {"completed": completed, "total": total, "blocked": blocked}
        if task.state is TaskState.BLOCKED and goal.state is GoalState.ACTIVE:
            goal.state = GoalState.BLOCKED
            goal.notes.append(f"blocked by task {task.id}: {task.error[:120]}")
        elif goal.state is GoalState.BLOCKED and blocked == 0 and completed:
            goal.state = GoalState.ACTIVE
        if goal.definition_of_done and completed and self._milestones_finished(goal):
            # Only judge the exit condition once the planned work is actually done.
            # Checking earlier would complete the goal and strand the milestones
            # (and the monitors) that still belonged to it.
            probe = Task(id=new_id("task"), intent=f"definition of done for {goal.title}",
                         success_criteria=[as_check(c) for c in goal.definition_of_done])
            check = self.verifier.verify(probe, store=None)
            if check.verdict.ok:
                if goal.state is not GoalState.COMPLETED:      # record the transition once
                    goal.state = GoalState.COMPLETED
                    goal.notes.append("definition of done verified")
        self.store.save_goal(goal)

    def _milestones_finished(self, goal: Goal) -> bool:
        """True when every milestone of ``goal`` has a completed task (or there are none)."""
        if not goal.milestones:
            return True
        finished = {
            task.intent
            for task in self.store.list_tasks(goal_id=goal.id, limit=10_000)
            if task.state is TaskState.COMPLETED
        }
        return all(milestone in finished for milestone in goal.milestones)

    def _escalate(self, task: Task, error: JarvisError) -> None:
        kind = getattr(error, "kind", "tool_error")
        task.error = str(error)
        task.error_kind = kind
        task.state = TaskState.BLOCKED if kind != "halted" else TaskState.QUEUED
        task.finished_at = now() if task.state is TaskState.BLOCKED else 0.0
        self.store.save_task(task)
        self.store.checkpoint(task)
        self.store.add_decision(Decision(
            task_id=task.id, kind="escalation",
            rationale=f"{kind}: {str(error)[:200]}",
            payload={"phase": task.phase.value, "attempts": task.attempts},
        ))
        self.memory.remember(
            f"Blocker on '{task.intent[:80]}': {kind}: {str(error)[:160]}",
            kind="reflection", tags=["blocker", kind], goal_id=task.goal_id, importance=0.8,
        )
        self._publish("task.blocked", {"task": task.id, "error": str(error)[:300], "kind": kind})
        self.store.audit("executive", "escalated",
                         {"task": task.id, "kind": kind, "error": str(error)[:200]})

    def _evidence_for(self, task: Task) -> List[str]:
        evidence: List[str] = []
        verification = task.context.get("verification") or {}
        if verification.get("evidence"):
            evidence.append(str(verification["evidence"]))
        for step in task.plan:
            if step.state is StepState.SUCCEEDED:
                evidence.append(f"[step] {step.title or step.tool}: ok")
            elif step.state is StepState.FAILED:
                evidence.append(f"[step] {step.title or step.tool}: FAILED - {step.error[:120]}")
        return evidence

    def _advance(self, task: Task, phase: Phase, payload: Dict[str, Any]) -> None:
        """Move the phase marker forward only.

        A resumed task re-enters the cycle from the top, and re-running the early
        phases must not rewind the marker - otherwise the planning phase rebuilds
        the plan and finished steps are lost.
        """
        if phase.order >= task.phase.order:
            task.phase = phase
        self._persist(task, phase.value, payload)

    def _persist(self, task: Task, label: str, payload: Optional[Dict[str, Any]] = None) -> None:
        self.store.save_task(task)
        self.store.checkpoint(task)
        self._publish("task.phase", {"task": task.id, "phase": task.phase.value,
                                     "label": label, **(payload or {})})

    def _publish(self, kind: str, payload: Dict[str, Any], source: str = "executive") -> None:
        if self.bus is not None:
            self.bus.publish(kind, source=source, payload=payload)

    def _finish(self, outcome: CycleOutcome, task: Task) -> CycleOutcome:
        outcome.state = task.state.value
        outcome.phase = task.phase.value
        outcome.verdict = task.verdict
        self.outcomes.append(outcome)
        if len(self.outcomes) > 200:
            self.outcomes = self.outcomes[-200:]
        return outcome


class _Deferred(JarvisError):
    """Internal signal: the task was re-queued during verification."""

    kind = "deferred"
    recoverable = True


class _AwaitingApproval(JarvisError):
    """Internal signal: the task is parked until the user decides."""

    kind = "awaiting_approval"
    recoverable = True

    def __init__(self, approval_id: str = ""):
        super().__init__(f"awaiting approval {approval_id}")
        self.approval_id = approval_id


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _same_check(a: Check, b: Check) -> bool:
    return a.kind == b.kind and a.args == b.args and a.expect == b.expect


def _risk_for_level(level: int):
    from jarvis.core.models import Risk

    for risk in Risk:
        if risk.level == level:
            return risk
    return Risk.READ


def _done_count(task: Task) -> int:
    return sum(1 for s in task.plan if s.state in (StepState.SUCCEEDED, StepState.SKIPPED))


def _resolve_args(args: Dict[str, Any], outputs: Dict[str, Any]) -> Dict[str, Any]:
    """Substitute ``${outputs.name.field}`` references from earlier steps."""
    resolved: Dict[str, Any] = {}
    for key, value in args.items():
        resolved[key] = _resolve_value(value, outputs)
    return resolved


def _resolve_value(value: Any, outputs: Dict[str, Any]) -> Any:
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        path = value[2:-1]
        parts = path.split(".")
        if parts and parts[0] == "outputs":
            parts = parts[1:]
        current: Any = outputs
        for part in parts:
            if isinstance(current, dict):
                current = current.get(part)
            else:
                return value
        return current
    if isinstance(value, dict):
        return {k: _resolve_value(v, outputs) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_value(v, outputs) for v in value]
    return value


def _shrink(value: Any, limit: int = 4000) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "..."
    if isinstance(value, dict):
        return {k: _shrink(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_shrink(v, limit) for v in value[:50]]
    return value


def _build_result(task: Task, outputs: Dict[str, Any]) -> Dict[str, Any]:
    """Assemble the task result, flattening one level so checks can address fields."""
    result: Dict[str, Any] = {"steps": {}, "outputs": outputs}
    for step in task.plan:
        if step.produces:
            result["steps"][step.produces] = step.output
    for value in outputs.values():
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(item, (dict, list)) or key in ("summary",):
                    result.setdefault(key, item)
    result["objective"] = task.objective
    result["steps_completed"] = _done_count(task)
    result["steps_total"] = len(task.plan)
    summary = _summary_for(task)
    result["summary"] = summary
    return result


def _summary_for(task: Task) -> str:
    verdict = task.verdict or "unverified"
    detail = ""
    if isinstance(task.result, dict):
        # Only a produced answer is worth quoting; the drafted summary would nest itself.
        value = task.result.get("answer")
        if isinstance(value, str) and value:
            detail = value[:200]
    if not detail and task.error:
        detail = task.error[:200]
    return (f"{task.intent[:100]} -> {task.state.value}/{verdict}"
            + (f" ({detail})" if detail else ""))


# `Check` is re-exported for callers that build tasks with explicit criteria.
__all__ = ["Executive", "CycleOutcome", "StepFailure", "Check"]
