"""Evolver: self-evaluation and governed improvement."""

from __future__ import annotations

import pytest

from jarvis.core.models import Risk, TaskState, Verdict
from jarvis.tools.base import ToolResult
from tests.conftest import plan_with, register_tool


def failing_tool():
    def handler(context, **kwargs):
        return ToolResult.failure("the widget service is unreachable", kind="tool_error")

    return handler


def make_failures(runtime, count=3):
    register_tool(runtime, "flaky.widget", failing_tool(), requires=["code.run"],
                  risk=Risk.EXECUTE)
    for index in range(count):
        runtime.request(f"talk to the widget {index}", plan=plan_with(
            [{"tool": "flaky.widget", "args": {}, "title": "call widget",
              "produces": "out"}]))
        runtime.executive.run(max_tasks=10)


def test_verified_work_scores_higher_than_blocked(permissive):
    good = permissive.request("run the tests")
    scores = {}
    scores["good"] = permissive.evolver.evaluate(permissive.store.get_task(good.id))["score"]

    register_tool(permissive, "flaky.bad", failing_tool(), requires=["code.run"],
                  risk=Risk.EXECUTE)
    bad = permissive.request("do something broken", plan=plan_with(
        [{"tool": "flaky.bad", "args": {}, "title": "break", "produces": "out"}]))
    scores["bad"] = permissive.evolver.evaluate(permissive.store.get_task(bad.id))["score"]

    assert scores["good"] > scores["bad"]
    assert scores["good"] > 0.8


def test_unverified_work_produces_a_postmortem(permissive):
    register_tool(permissive, "flaky.bad", failing_tool(), requires=["code.run"],
                  risk=Risk.EXECUTE)
    task = permissive.request("do something broken", plan=plan_with(
        [{"tool": "flaky.bad", "args": {}, "title": "break", "produces": "out"}]))

    text = permissive.evolver.postmortem(permissive.store.get_task(task.id))
    assert text and "Postmortem" in text
    assert any(m.kind == "reflection" for m in permissive.memory.all())


def test_repeated_tool_failures_produce_a_proposal(permissive):
    make_failures(permissive, count=3)
    review = permissive.review()

    assert review["evaluated"] >= 3
    assert review["proposals"] >= 1
    kinds = {p["kind"] for p in permissive.proposals()}
    assert kinds & {"step_retry_limit", "planner_guidance"}


def test_a_proposal_does_nothing_until_it_is_approved(permissive):
    make_failures(permissive, count=3)
    permissive.review()
    proposal = [p for p in permissive.proposals() if p["kind"] == "step_retry_limit"][0]
    before = permissive.executive.step_retry_limit

    rejected = permissive.decide_proposal(proposal["id"], False, note="not now")

    assert rejected["state"] == "rejected"
    assert permissive.executive.step_retry_limit == before


def test_an_approved_proposal_changes_configuration_only(permissive):
    make_failures(permissive, count=3)
    permissive.review()
    proposal = [p for p in permissive.proposals() if p["kind"] == "step_retry_limit"][0]

    applied = permissive.decide_proposal(proposal["id"], True, note="agreed")

    assert applied["state"] == "applied"
    assert permissive.executive.step_retry_limit == proposal["payload"]["step_retry_limit"]
    assert "step retry limit" in applied["effect"]
    assert any(m.kind == "reflection" and "Improvement applied" in m.content
               for m in permissive.memory.all())


def test_evolver_cannot_apply_changes_outside_its_allowlist(permissive):
    permissive.store.add_proposal({
        "id": "prop_test", "kind": "rewrite_source_code", "title": "patch the executive",
        "rationale": "it would be faster", "payload": {}, "state": "pending",
    })

    with pytest.raises(PermissionError):
        permissive.decide_proposal("prop_test", True)


def test_review_is_idempotent(permissive):
    permissive.request("run the tests")
    first = permissive.review()
    second = permissive.review()

    assert first["evaluated"] == 1
    assert second["evaluated"] == 0


def test_model_fallbacks_trigger_a_routing_proposal(permissive):
    class Failing:
        def __init__(self):
            from jarvis.intelligence.providers import ModelSpec

            self.spec = ModelSpec(id="broken", name="broken", kind="cloud")
            self.last_error = ""

        def available(self):
            return True

        def supports(self, task_class):
            return True

        def complete(self, request):
            raise RuntimeError("upstream is down")

    failing = Failing()
    permissive.router.providers.insert(0, failing)
    for index in range(3):
        permissive.request(f"question {index}")

    usage = permissive.router.usage_summary()
    assert usage["fallbacks"] >= 3
    permissive.review()
    assert any(p["kind"] == "router_preference" for p in permissive.proposals())


def test_metrics_summary_reports_tool_health(permissive):
    permissive.request("run the tests")
    summary = permissive.evolver.metrics_summary()

    assert summary["tasks_evaluated"] == 0            # nothing reviewed yet
    permissive.review()
    summary = permissive.evolver.metrics_summary()
    assert summary["tasks_evaluated"] == 1
    assert summary["verification_rate"] == 1.0
    assert summary["tools"]["tool:code.run_tests"]["success_rate"] == 1.0
