"""End-to-end and operating-cycle behaviour."""

from __future__ import annotations

from jarvis.core.models import Check, Phase, Risk, StepState, TaskState, Verdict
from tests.conftest import ok_tool, plan_with, register_tool


def test_immediate_request_runs_the_full_cycle_and_verifies(permissive):
    task = permissive.request("run the tests in this project")

    assert task.state is TaskState.COMPLETED
    assert task.verdict == Verdict.VERIFIED.value
    assert task.phase is Phase.COMMITTED
    assert task.result["passed"] is True


def test_every_phase_is_checkpointed_in_order(permissive):
    task = permissive.request("run the tests")
    phases = [phase for phase, _ in permissive.store.checkpoint_history(task.id)]

    for expected in ("received", "retrieved", "objective_defined", "planned",
                     "authorized", "executed", "verified", "repaired", "memory_updated"):
        assert expected in phases, f"{expected} missing from {phases}"
    assert phases.index("authorized") < phases.index("executed")
    assert phases.index("executed") < phases.index("verified")
    assert phases.index("verified") < phases.index("memory_updated")


def test_decisions_are_auditable(permissive):
    task = permissive.request("run the tests")
    kinds = [decision.kind for decision in permissive.store.decisions_for(task.id)]

    assert "memory_retrieved" in kinds
    assert "objective_defined" in kinds
    assert "plan_chosen" in kinds
    assert "model_selected" in kinds


def test_success_is_never_claimed_without_verification(permissive):
    """No success criteria means no evidence, which means no 'completed'."""
    task = permissive.request("run the tests", plan=plan_with(
        [{"tool": "code.run_tests", "args": {}, "title": "run", "produces": "tests"}],
        criteria=[],
    ))

    assert task.state is TaskState.BLOCKED
    assert task.verdict == Verdict.UNVERIFIABLE.value
    assert "no success criteria" in task.error


def test_caller_supplied_criteria_are_authoritative(permissive):
    """The user's own criterion is kept even if the planner proposes another."""
    task = permissive.request(
        "run the tests",
        success_criteria=[Check(kind="file_exists", args={"path": "app.py"}, expect=True,
                                description="the module still exists")],
    )

    kinds = [check.kind for check in task.success_criteria]
    assert "file_exists" in kinds
    assert task.verdict == Verdict.VERIFIED.value


def test_verification_reads_the_world_not_the_report(permissive):
    """A step can succeed while the claimed outcome is false - verification must catch it."""
    register_tool(permissive, "probe.lies", ok_tool(created=True), requires=[],
                  risk=Risk.READ)
    task = permissive.request("create the release artifact", plan=plan_with(
        [{"tool": "probe.lies", "args": {}, "title": "pretend to build", "produces": "build"}],
        criteria=[{"kind": "file_exists", "args": {"path": "release.zip"}, "expect": True,
                   "description": "the artifact exists on disk"}],
    ))

    assert task.state is TaskState.BLOCKED
    assert task.verdict == Verdict.FAILED.value
    verification = permissive.store.verification_for(task.id)
    assert verification.checks[0].passed is False
    assert verification.checks[0].actual is False


def test_unknown_tools_are_dropped_from_a_plan(permissive):
    """The planner validates against the registry; invented tools cannot be executed."""
    plan = permissive.planner.plan("run the tests", context={}, task_id="")
    assert all(permissive.tools.has(step.tool) for step in plan.steps)

    explicit = permissive.planner.plan(
        "run the tests",
        context={"plan": {"objective": "x", "steps": [
            {"tool": "not.a.real.tool", "args": {}},
            {"tool": "code.run_tests", "args": {}},
        ]}},
        task_id="",
    )
    assert [step.tool for step in explicit.steps] == ["code.run_tests"]
    assert any("not.a.real.tool" in note for note in explicit.notes)


def test_result_fields_are_flattened_for_checks(permissive):
    task = permissive.request("run the tests")
    result = task.result

    assert result["passed"] is True
    assert result["steps_completed"] == 1
    assert "tests" in result["steps"]


def test_objective_takes_the_goal_context(permissive):
    goal = permissive.adopt_goal("Ship it", "Reach a tested release", run_first=False)
    task = permissive.request("run the tests", goal_id=goal.id)

    assert task.objective.startswith("Reach a tested release")
    assert task.context["goal"]["title"] == "Ship it"


def test_memory_is_recalled_into_the_next_request(permissive):
    permissive.remember("the release branch is called stable", tags=["release"], importance=0.9)
    task = permissive.request("what is the release branch")

    recalled = [entry["content"] for entry in task.context["memory"]]
    assert any("release branch" in item for item in recalled)


def test_request_can_be_queued_without_running(permissive):
    task = permissive.request("run the tests", run_immediately=False)

    assert task.state is TaskState.QUEUED
    assert task.phase is Phase.RECEIVED
    outcomes = permissive.executive.run()
    assert len(outcomes) == 1
    assert permissive.store.get_task(task.id).verdict == Verdict.VERIFIED.value


def test_cancellation_stops_queued_work(permissive):
    task = permissive.request("run the tests", run_immediately=False)
    assert permissive.cancel(task.id) is True
    assert permissive.store.get_task(task.id).state is TaskState.CANCELLED
    assert permissive.executive.run() == []


def test_status_snapshot_describes_the_runtime(permissive):
    permissive.request("run the tests")
    status = permissive.status()

    assert status["task_counts"]["completed"] == 1
    assert status["halted"] is False
    assert len(status["tools"]) == len(permissive.tools.names())
    assert status["memory"]["total"] > 0
    assert status["policy"]["mode"] == "standard"
    assert status["model_usage"]["total_calls"] >= 1
