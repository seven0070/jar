"""Shared fixtures: an isolated JARVIS runtime per test."""

from __future__ import annotations

import os
import pytest

from jarvis.application import Jarvis
from jarvis.config import Config
from jarvis.core.models import Risk
from jarvis.core.permissions import SCOPES
from jarvis.tools.base import Tool, ToolResult


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    """No JARVIS_* environment variable may leak into a test."""
    for key in list(os.environ):
        if key.startswith("JARVIS_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / "README.md").write_text("# demo project\n", encoding="utf-8")
    return root


@pytest.fixture
def config(tmp_path, workspace):
    return Config(
        home=str(tmp_path / "home"),
        workspace=str(workspace),
        mode="standard",
        approval_blocking=False,
        scheduler_tick_seconds=1.0,
    )


@pytest.fixture
def jarvis(config, workspace):
    """A runtime that asks before anything above read-level risk."""
    runtime = Jarvis(config=config, root=str(workspace))
    yield runtime
    runtime.close()


@pytest.fixture
def permissive(config, workspace):
    """A runtime with every scope auto-approved, for exercising execution paths."""
    runtime = Jarvis(config=config, root=str(workspace), auto_approve=tuple(SCOPES))
    yield runtime
    runtime.close()


def register_tool(runtime: Jarvis, name: str, handler, requires=(), risk=Risk.READ,
                  description: str = "test tool") -> Tool:
    """Register an ad-hoc tool on a running runtime."""
    tool = Tool(name=name, description=description, handler=handler,
                requires=list(requires), risk=risk)
    runtime.tools.register(tool)
    runtime.policy.grant(name_scope(name))
    return tool


def name_scope(tool_name: str) -> str:
    """Map a test tool name onto an existing capability scope."""
    return SCOPES_BY_PREFIX.get(tool_name.split(".")[0], "fs.read")


SCOPES_BY_PREFIX = {
    "flaky": "code.run",
    "probe": "fs.read",
    "danger": "system.install",
}


DEFAULT_CRITERION = {"kind": "result_field", "args": {"path": "ok"}, "expect": True,
                     "description": "the tool reported success"}


def plan_with(steps, criteria=None, objective: str = "test plan"):
    """Build a plan spec. Pass ``criteria=[]`` to declare *no* success criteria."""
    return {
        "objective": objective,
        "steps": steps,
        "success_criteria": list(DEFAULT_CRITERION and [DEFAULT_CRITERION])
        if criteria is None else list(criteria),
    }


def ok_tool(**value):
    def handler(context, **kwargs):
        return ToolResult.success({**{"ok": True}, **value}, evidence="tool succeeded")

    return handler
