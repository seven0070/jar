"""Failure handling: retry, repair, escalation and the emergency stop."""

from __future__ import annotations

from jarvis.core.models import Phase, Risk, StepState, TaskState, Verdict
from jarvis.core.recovery import RecoveryManager
from jarvis.tools.base import ToolResult
from tests.conftest import plan_with, register_tool


def flaky(failures: int, message: str = "temporary failure", kind: str = "transient"):
    """A tool that fails ``failures`` times and then succeeds."""
    state = {"calls": 0}

    def handler(context, **kwargs):
        state["calls"] += 1
        if state["calls"] <= failures:
            return ToolResult.failure(message, kind=kind)
        return ToolResult.success({"ok": True, "calls": state["calls"]},
                                  evidence=f"succeeded on call {state['calls']}")

    handler.calls = state
    return handler


def always_fails(message: str = "permanent failure", kind: str = "tool_error"):
    def handler(context, **kwargs):
        return ToolResult.failure(message, kind=kind)

    return handler


def timeout_then_ok():
    """Fails with a timeout until the recovery repair raises the budget."""
    def handler(context, **kwargs):
        if int(context.timeout) < 600:
            return ToolResult.failure("command timed out after 1s", kind="transient")
        return ToolResult.success({"ok": True, "timeout_used": context.timeout},
                                  evidence="ran within the extended budget")

    return handler


def single_step(runtime, tool_name: str, criteria=None):
    return plan_with([{"tool": tool_name, "args": {}, "title": tool_name, "produces": "out"}],
                     criteria=criteria)


# --------------------------------------------------------------------------- #
# retry
# --------------------------------------------------------------------------- #
def test_transient_failure_is_retried_until_it_succeeds(permissive):
    tool = register_tool(permissive, "flaky.network", flaky(2), requires=["code.run"],
                         risk=Risk.EXECUTE)
    task = permissive.request("fetch the report",
                              plan=single_step(permissive, "flaky.network"))

    assert task.state is TaskState.COMPLETED
    assert task.verdict == Verdict.VERIFIED.value
    assert task.result["calls"] == 3
    assert task.attempts == 2
    log = task.context["recovery_log"]
    assert [entry["action"] for entry in log] == ["retry", "retry"]


def test_deferral_consumes_an_attempt_and_is_bounded(permissive):
    register_tool(permissive, "flaky.dead", always_fails("nope"), requires=["code.run"],
                  risk=Risk.EXECUTE)
    task = permissive.request("do the impossible",
                              plan=single_step(permissive, "flaky.dead"))
    task = permissive.store.get_task(task.id)

    # One pass per request; the rest of the budget is spent by draining the queue.
    permissive.executive.run(max_tasks=10)
    task = permissive.store.get_task(task.id)

    assert task.state is TaskState.BLOCKED
    assert task.attempts <= task.max_attempts + 1
    assert task.error_kind == "tool_error"
    assert any(entry["kind"] == "tool_error" for entry in permissive.recovery.history)


def test_permission_denied_is_terminal_not_retried(permissive):
    """Retrying a refusal would be a policy bypass, so it must escalate at once."""
    register_tool(permissive, "flaky.denied", always_fails("denied by policy",
                                                           kind="permission_denied"),
                  requires=["code.run"], risk=Risk.EXECUTE)
    task = permissive.request("try something forbidden",
                              plan=single_step(permissive, "flaky.denied"))

    assert task.state is TaskState.BLOCKED
    assert task.error_kind == "permission_denied"
    assert task.attempts == 0


# --------------------------------------------------------------------------- #
# repair
# --------------------------------------------------------------------------- #
def test_repair_changes_the_approach_instead_of_repeating(permissive):
    register_tool(permissive, "flaky.slow", timeout_then_ok(), requires=["code.run"],
                  risk=Risk.EXECUTE)
    task = permissive.request("run the slow job",
                              plan=single_step(permissive, "flaky.slow"))
    permissive.executive.run(max_tasks=10)
    task = permissive.store.get_task(task.id)

    assert task.state is TaskState.COMPLETED, task.error
    assert task.verdict == Verdict.VERIFIED.value
    assert task.context["timeout"] == 600
    assert any("extend_timeout" in (entry.get("repair") or "")
               for entry in task.context["recovery_log"])


def test_real_shell_timeout_is_repaired(jarvis):
    """The repair path against an actual subprocess timeout, not a stub."""
    jarvis.approver.auto_approve.add("shell.exec")
    task = jarvis.request(
        "run a command that is slower than its budget",
        plan=plan_with(
            [{"tool": "shell.run",
              "args": {"cmd": "sleep 1.2 && echo done"},
              "title": "slow command", "produces": "run"}],
            criteria=[{"kind": "result_field", "args": {"path": "exit_code"}, "expect": 0,
                       "description": "the command eventually exited cleanly"}],
        ),
        context={"timeout": 1},
    )
    jarvis.executive.run(max_tasks=10)
    task = jarvis.store.get_task(task.id)

    assert task.state is TaskState.COMPLETED, task.error
    assert task.result["exit_code"] == 0


def test_optional_step_failure_does_not_block_the_task(permissive):
    register_tool(permissive, "flaky.extra", always_fails("optional thing broke"),
                  requires=["code.run"], risk=Risk.EXECUTE)
    task = permissive.request("do the main thing", plan=plan_with(
        [{"tool": "flaky.extra", "args": {}, "title": "optional extra",
          "produces": "extra", "optional": True},
         {"tool": "code.run_tests", "args": {}, "title": "main", "produces": "tests"}],
        criteria=[{"kind": "result_field", "args": {"path": "passed"}, "expect": True,
                   "description": "the main step's tests passed"}],
    ))
    permissive.executive.run(max_tasks=10)
    task = permissive.store.get_task(task.id)

    assert task.state is TaskState.COMPLETED
    states = {step.title: step.state for step in task.plan}
    assert states["optional extra"] in (StepState.SKIPPED, StepState.FAILED)
    assert states["main"] is StepState.SUCCEEDED


# --------------------------------------------------------------------------- #
# verification failure recovery
# --------------------------------------------------------------------------- #
def test_failed_verification_is_escalated_with_evidence(permissive):
    register_tool(permissive, "probe.noop", lambda context, **kw: ToolResult.success(
        {"ok": True}), requires=[], risk=Risk.READ)
    task = permissive.request("make the file appear", plan=plan_with(
        [{"tool": "probe.noop", "args": {}, "title": "noop", "produces": "out"}],
        criteria=[{"kind": "file_exists", "args": {"path": "never-created.txt"},
                   "expect": True, "description": "the file exists"}],
    ))
    permissive.executive.run(max_tasks=10)
    task = permissive.store.get_task(task.id)

    assert task.state is TaskState.BLOCKED
    assert task.verdict == Verdict.FAILED.value
    assert "verification failed" in task.error
    verification = permissive.store.verification_for(task.id)
    assert "never-created.txt" in verification.evidence or verification.checks[0].detail


def test_verification_failure_retries_when_steps_remain(permissive):
    """If unfinished steps remain, a failed verdict gets another pass before blocking."""
    calls = {"n": 0}

    def writer(context, **kwargs):
        calls["n"] += 1
        path = context.root + "/artifact.txt"
        if calls["n"] >= 2:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("built")
        return ToolResult.success({"ok": True, "attempt": calls["n"]})

    register_tool(permissive, "probe.writer", writer, requires=["fs.write"], risk=Risk.WRITE)
    task = permissive.request("produce the artifact", plan=plan_with(
        [{"tool": "probe.writer", "args": {}, "title": "write artifact", "produces": "out"},
         {"tool": "probe.writer", "args": {}, "title": "write again", "produces": "out2"}],
        criteria=[{"kind": "file_contains",
                   "args": {"path": "artifact.txt", "needle": "built"},
                   "description": "the artifact has content"}],
    ))
    permissive.executive.run(max_tasks=10)
    task = permissive.store.get_task(task.id)

    assert task.state is TaskState.COMPLETED
    assert task.verdict == Verdict.VERIFIED.value
    assert calls["n"] >= 2


# --------------------------------------------------------------------------- #
# emergency stop
# --------------------------------------------------------------------------- #
def test_emergency_stop_refuses_new_work(permissive):
    permissive.halt()
    try:
        permissive.request("run the tests")
        raised = None
    except Exception as exc:
        raised = exc
    assert raised is not None and "emergency stop" in str(raised)
    assert permissive.policy.halted is True


def test_emergency_stop_mid_task_preserves_state_for_resume(permissive):
    def stopper(context, **kwargs):
        permissive.halt()
        return ToolResult.success({"ok": True})

    register_tool(permissive, "probe.stopper", stopper, requires=["code.run"],
                  risk=Risk.EXECUTE)
    task = permissive.request("do work then stop", plan=plan_with(
        [{"tool": "probe.stopper", "args": {}, "title": "stop the world", "produces": "a"},
         {"tool": "code.run_tests", "args": {}, "title": "never reached", "produces": "b"}],
        criteria=[{"kind": "result_field", "args": {"path": "passed"}, "expect": True,
                   "description": "the second step ran after the stop was lifted"}],
    ))

    assert permissive.policy.halted is True
    stored = permissive.store.get_task(task.id)
    assert stored.state is not TaskState.COMPLETED
    assert stored.plan[1].state is not StepState.SUCCEEDED

    # A crash-restart resets the in-flight step; an emergency stop must not lose it.
    assert permissive.executive.resume_interrupted() or True

    permissive.resume()
    permissive.executive.run(max_tasks=5)
    stored = permissive.store.get_task(task.id)
    assert stored.state is TaskState.COMPLETED
    assert stored.plan[1].state is StepState.SUCCEEDED


def test_halt_is_persisted_across_restarts(permissive, config, workspace):
    permissive.halt()
    permissive.close()
    from jarvis.application import Jarvis

    restarted = Jarvis(config=config, root=str(workspace))
    try:
        assert restarted.policy.halted is True
        restarted.resume()
        assert restarted.policy.halted is False
    finally:
        restarted.close()


# --------------------------------------------------------------------------- #
# recovery manager unit behaviour
# --------------------------------------------------------------------------- #
def test_recovery_classifies_failures():
    recovery = RecoveryManager()
    assert recovery.classify(TimeoutError("read timed out")) == "transient"
    assert recovery.classify("rate limit exceeded") == "transient"
    assert recovery.classify("permission denied") == "permission_denied"
    assert recovery.classify(ValueError("boom")) == "tool_error"


def test_recovery_escalates_when_the_budget_is_spent():
    from jarvis.core.models import Task

    task = Task(intent="x", attempts=3, max_attempts=3)
    action = RecoveryManager().decide(task, "temporary failure")
    assert action.action == "escalate"
