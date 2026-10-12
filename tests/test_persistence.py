"""Durability: state survives restarts, and interrupted work resumes."""

from __future__ import annotations

from jarvis.application import Jarvis
from jarvis.core.models import Phase, Risk, StepState, TaskState, Trigger, Verdict
from jarvis.core.permissions import SCOPES
from jarvis.tools.base import ToolResult
from tests.conftest import plan_with, register_tool


def reopen(config, workspace, auto_approve=()):
    return Jarvis(config=config, root=str(workspace), auto_approve=auto_approve)


def test_tasks_goals_and_memory_survive_a_restart(permissive, config, workspace):
    permissive.request("run the tests")
    goal = permissive.adopt_goal("Ship it", "Reach a tested release", run_first=False)
    permissive.remember("the release branch is stable", tags=["release"])
    task_count = len(permissive.tasks())
    permissive.close()

    restarted = reopen(config, workspace)
    try:
        assert len(restarted.tasks()) == task_count
        assert restarted.store.get_goal(goal.id).title == "Ship it"
        assert any("release branch" in m["content"] for m in restarted.memories())
        assert restarted.store.get_setting("last_boot")
        assert restarted.boot_report["version"]
    finally:
        restarted.close()


def test_an_interrupted_task_is_requeued_on_boot(permissive, config, workspace):
    task = permissive.request("run the tests", run_immediately=False)
    task.state = TaskState.RUNNING
    task.phase = Phase.EXECUTED
    permissive.store.save_task(task)
    permissive.close()

    # The reopened runtime must carry the same approvals policy as the original.
    restarted = reopen(config, workspace, auto_approve=tuple(SCOPES))
    try:
        assert task.id in restarted.boot_report["resumed"]["resumed_tasks"]
        stored = restarted.store.get_task(task.id)
        assert stored.state is TaskState.QUEUED
        assert stored.attempts == 1
        restarted.executive.run(max_tasks=5)
        assert restarted.store.get_task(task.id).verdict == Verdict.VERIFIED.value
    finally:
        restarted.close()


def test_a_parked_task_survives_and_resumes_after_restart(jarvis, config, workspace):
    task = jarvis.request("run the tests")
    approval_id = jarvis.approvals()[0]["id"]
    assert task.state is TaskState.AWAITING_APPROVAL
    jarvis.close()

    restarted = reopen(config, workspace)
    try:
        stored = restarted.store.get_task(task.id)
        assert stored.state is TaskState.AWAITING_APPROVAL
        assert [a["id"] for a in restarted.approvals()] == [approval_id]

        result = restarted.approve(approval_id, True, note="after restart")
        assert restarted.store.get_task(task.id).state is TaskState.COMPLETED
        assert result["outcome"]["verdict"] == "verified"
    finally:
        restarted.close()


def test_checkpoints_let_you_replay_what_happened(permissive):
    task = permissive.request("run the tests")
    history = permissive.store.checkpoint_history(task.id)

    assert len(history) >= 9
    assert history[0][0] == "received"
    assert history[-1][0] == "memory_updated"
    snapshot = permissive.store.last_checkpoint(task.id)
    assert snapshot.verdict == Verdict.VERIFIED.value


def test_undispatched_events_are_replayed_on_boot(permissive, config, workspace):
    from jarvis.core.models import Event

    permissive.store.add_event(Event(kind="ci.failure", source="test",
                                     payload={"run": 7}))
    permissive.close()

    restarted = reopen(config, workspace)
    try:
        assert "ci.failure" in restarted.boot_report["resumed"]["replayed_events"]
        assert restarted.store.pending_events() == []
    finally:
        restarted.close()


def test_scheduled_work_is_picked_up_when_due(permissive):
    permissive.approver.auto_approve.add("schedule.manage")
    result = permissive.tools.execute(
        "schedule.create",
        {"intent": "run the tests", "delay_seconds": 0, "goal_id": ""},
        _context(permissive),
    )
    assert result.ok

    due = permissive.scheduler.due_scheduled()
    assert [t.trigger for t in due] == [Trigger.SCHEDULE]
    permissive.executive.run(max_tasks=5)
    assert permissive.store.get_task(due[0].id).verdict == Verdict.VERIFIED.value


def _context(runtime):
    from jarvis.tools.base import ToolContext

    return ToolContext(root=runtime.root, store=runtime.store, policy=runtime.policy,
                       approver=runtime.approver, bus=runtime.bus, memory=runtime.memory,
                       router=runtime.router)


def test_a_resumed_task_does_not_rerun_completed_steps(permissive):
    """Regression: phase markers used to rewind, so a resumed task re-planned and
    re-executed steps that had already succeeded."""
    calls = {"n": 0}

    def counted(context, **kwargs):
        calls["n"] += 1
        return ToolResult.success({"ok": True, "call": calls["n"]}, evidence="counted")

    register_tool(permissive, "flaky.counted", counted, requires=["code.run"],
                  risk=Risk.EXECUTE)
    task = permissive.request("two step job", plan=plan_with(
        [{"tool": "flaky.counted", "args": {}, "title": "first", "produces": "a"},
         {"tool": "flaky.counted", "args": {}, "title": "second", "produces": "b"}]))

    assert calls["n"] == 2
    assert task.state is TaskState.COMPLETED

    # Interrupt after the fact and resume: nothing may be executed a second time.
    stored = permissive.store.get_task(task.id)
    stored.state = TaskState.RUNNING
    permissive.store.save_task(stored)
    permissive.executive.resume_interrupted()
    permissive.executive.run_until_settled(task.id)

    assert calls["n"] == 2, "completed steps were executed again after a resume"


def test_phase_markers_never_move_backwards(permissive):
    task = permissive.request("run the tests")
    phases = [phase for phase, _ in permissive.store.checkpoint_history(task.id)]
    orders = [Phase(phase).order for phase in phases]

    # Every checkpoint is at or beyond the previous one, apart from the initial entry.
    assert orders == sorted(orders), phases
