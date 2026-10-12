"""Odysseus: durable goals, milestones, monitors and proactive work."""

from __future__ import annotations

from jarvis.core.models import GoalState, TaskState, Trigger, Verdict


def test_adopting_a_goal_queues_and_runs_its_milestones(permissive):
    goal = permissive.adopt_goal(
        "Reach a tested release", "Ship the project with a green test suite",
        milestones=["Inventory the workspace", "Run the test suite"], run_first=False)
    assert goal.state is GoalState.ACTIVE

    report = permissive.advance()
    assert report["executed"] >= 1
    summary = permissive.goal_report(goal.id)
    assert summary["completed"] == 2
    assert summary["verified"] == 2
    assert set(summary["milestones_done"]) == set(goal.milestones)


def test_milestones_are_not_duplicated(permissive):
    goal = permissive.adopt_goal("G", "objective", milestones=["Run the test suite"],
                                 run_first=False)
    permissive.advance()
    permissive.advance()

    tasks = permissive.store.list_tasks(goal_id=goal.id)
    assert len(tasks) == 1


def test_definition_of_done_completes_a_goal(permissive, workspace):
    (workspace / "RELEASE.txt").write_text("1.0.0\n", encoding="utf-8")
    goal = permissive.adopt_goal(
        "Publish", "Cut a release", run_first=False,
        definition_of_done=[{"kind": "file_exists", "args": {"path": "RELEASE.txt"},
                             "expect": True}])

    assert permissive.odysseus.check_definition_of_done(goal.id) is True
    assert permissive.store.get_goal(goal.id).state is GoalState.COMPLETED


def test_an_unmet_definition_of_done_keeps_the_goal_open(permissive):
    goal = permissive.adopt_goal(
        "Publish", "Cut a release", run_first=False,
        definition_of_done=[{"kind": "file_exists", "args": {"path": "MISSING.txt"},
                             "expect": True}])

    assert permissive.odysseus.check_definition_of_done(goal.id) is False
    assert permissive.store.get_goal(goal.id).state is not GoalState.COMPLETED


def test_an_event_triggers_proactive_work(permissive):
    goal = permissive.adopt_goal(
        "Keep main green", "React to failures", run_first=False,
        monitors=[{"name": "ci", "trigger": "event", "kind": "ci.failure",
                   "intent": "Investigate the CI failure", "cooldown_seconds": 0}])

    created = permissive.event("ci.failure", {"run": 42})
    assert len(created) == 1
    assert created[0]["goal_id"] == goal.id

    outcomes = permissive.executive.run()
    assert len(outcomes) == 1
    task = permissive.store.get_task(created[0]["id"])
    assert task.trigger is Trigger.PROACTIVE
    assert task.verdict == Verdict.VERIFIED.value


def test_unrelated_events_do_not_trigger_work(permissive):
    permissive.adopt_goal("G", "objective", run_first=False,
                          monitors=[{"name": "ci", "trigger": "event", "kind": "ci.failure",
                                     "intent": "Investigate"}])
    assert permissive.event("user.login", {}) == []


def test_monitor_cooldown_prevents_a_flood(permissive):
    permissive.adopt_goal("G", "objective", run_first=False,
                          monitors=[{"name": "ci", "trigger": "event", "kind": "ci.failure",
                                     "intent": "Investigate", "cooldown_seconds": 3600}])

    assert len(permissive.event("ci.failure", {})) == 1
    assert permissive.event("ci.failure", {}) == []      # still cooling down


def test_scheduled_monitors_fire_on_a_tick(permissive):
    goal = permissive.adopt_goal(
        "Watch the build", "Check periodically", run_first=False,
        monitors=[{"name": "tick", "trigger": "schedule", "interval_seconds": 0,
                   "intent": "Run the test suite"}])

    report = permissive.advance()
    assert report["queued"]
    assert permissive.goal_report(goal.id)["tasks"] >= 1


def test_a_blocked_task_marks_the_goal_blocked(permissive):
    goal = permissive.adopt_goal("G", "objective", run_first=False,
                                 milestones=["Run the test suite"])
    permissive.advance()
    task = permissive.store.list_tasks(goal_id=goal.id)[0]

    task.state = TaskState.BLOCKED
    task.verdict = Verdict.FAILED.value
    task.error = "simulated blocker"
    permissive.store.save_task(task)
    permissive.executive._update_goal(task)

    updated = permissive.store.get_goal(goal.id)
    assert updated.state is GoalState.BLOCKED
    assert any("simulated blocker" in note for note in updated.notes)


def test_cancelling_a_goal_cancels_its_work(permissive):
    goal = permissive.adopt_goal("G", "objective", milestones=["Run the test suite"],
                                 run_first=False)
    permissive.odysseus.cancel(goal.id, "changed my mind")

    assert permissive.store.get_goal(goal.id).state is GoalState.CANCELLED
    assert all(t.state is TaskState.CANCELLED
               for t in permissive.store.list_tasks(goal_id=goal.id))


def test_goal_progress_is_reported(permissive):
    goal = permissive.adopt_goal("G", "objective", milestones=["Run the test suite"],
                                 run_first=False)
    permissive.advance()
    completed, total, blocked = permissive.scheduler.goal_progress(goal)

    assert (completed, total, blocked) == (1, 1, 0)


def test_a_met_definition_of_done_does_not_strand_remaining_milestones(permissive, workspace):
    """Regression: the exit condition used to complete a goal after the first
    milestone, so later milestones (and the goal's monitors) were never run."""
    goal = permissive.adopt_goal(
        "Reach a tested release", "Ship with a green test suite",
        milestones=["Inventory the workspace", "Run the test suite"],
        monitors=[{"name": "ci", "trigger": "event", "kind": "ci.failure",
                   "intent": "Investigate the CI failure", "cooldown_seconds": 0}],
        definition_of_done=[{"kind": "file_exists", "args": {"path": "app.py"},
                             "expect": True}],
        run_first=False)

    permissive.executive.run(max_tasks=1)          # exactly one milestone completes
    partial = permissive.goal_report(goal.id)
    assert partial["completed"] == 1
    assert partial["state"] == "active", "the goal completed with work still outstanding"
    assert partial["milestones_done"] == ["Inventory the workspace"]

    # the goal is still open, so its monitor is still live
    assert len(permissive.event("ci.failure", {})) == 1

    permissive.executive.run(max_tasks=10)         # finish the outstanding work
    permissive.advance()
    final = permissive.goal_report(goal.id)

    assert set(final["milestones_done"]) == set(goal.milestones)
    assert final["state"] == "completed"
