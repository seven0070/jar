"""Command-line interface."""

from __future__ import annotations

import pytest

from jarvis.interfaces import cli


@pytest.fixture
def env(tmp_path, workspace, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_WORKSPACE", str(workspace))
    monkeypatch.setenv("JARVIS_AUTO_APPROVE", "code.run,fs.read,fs.write")
    return workspace


def run(argv):
    return cli.main(argv)


def test_status_reports_a_healthy_runtime(env, capsys):
    assert run(["status"]) == 0
    out = capsys.readouterr().out
    assert "JARVIS v" in out
    assert "running" in out
    assert "jarvis-deterministic" in out


def test_ask_runs_and_verifies(env, capsys):
    code = run(["ask", "run the tests in this project"])
    out = capsys.readouterr().out

    assert code == 0
    assert "completed" in out
    assert "verification: verified" in out
    assert "PASS" in out


def test_ask_returns_nonzero_when_work_is_not_verified(env, capsys):
    code = run(["ask", "run the tests", "--check",
                '{"kind": "file_exists", "args": {"path": "nope.txt"}, "expect": true}'])
    out = capsys.readouterr().out

    assert code == 1
    assert "blocked" in out


def test_queue_then_list_then_inspect(env, capsys):
    assert run(["request", "run the tests"]) == 0
    capsys.readouterr()
    assert run(["tasks", "--state", "queued"]) == 0
    listed = capsys.readouterr().out
    assert "queued" in listed

    task_id = [line.split()[0] for line in listed.splitlines()
               if line.startswith("task_")][0]
    assert run(["task", task_id]) == 0
    assert "intent" in capsys.readouterr().out


def test_approval_flow_from_the_cli(env, capsys, monkeypatch):
    """Queue work, let it park for approval, approve it, see it verified."""
    import json

    monkeypatch.setenv("JARVIS_AUTO_APPROVE", "")
    assert run(["ask", "run the tests in this project"]) == 0
    capsys.readouterr()

    assert run(["--json", "approvals"]) == 0
    approvals = json.loads(capsys.readouterr().out)
    assert len(approvals) == 1
    assert approvals[0]["scope"] == "code.run"

    assert run(["approve", approvals[0]["id"], "yes", "--note", "cli"]) == 0
    decided = capsys.readouterr().out
    assert "approved" in decided
    assert "verified" in decided


def test_memory_commands(env, capsys):
    assert run(["remember", "the release branch is stable", "--tags", "release"]) == 0
    capsys.readouterr()
    assert run(["recall", "release branch"]) == 0
    assert "stable" in capsys.readouterr().out


def test_tools_and_models_list(env, capsys):
    assert run(["tools"]) == 0
    assert "code.run_tests" in capsys.readouterr().out
    assert run(["models"]) == 0
    assert "jarvis-deterministic" in capsys.readouterr().out


def test_halt_blocks_and_resume_releases(env, capsys):
    assert run(["halt"]) == 0
    assert "emergency stop" in capsys.readouterr().out
    assert run(["ask", "run the tests"]) == 2
    assert "refused" in capsys.readouterr().out
    assert run(["resume"]) == 0
    assert "resumed" in capsys.readouterr().out


def test_json_output_is_machine_readable(env, capsys):
    import json

    assert run(["--json", "status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["halted"] is False
    assert isinstance(payload["tools"], list)
