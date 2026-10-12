"""Shell and code execution tools."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional

from jarvis.core.models import Risk
from jarvis.errors import ToolError
from jarvis.tools.base import Tool, ToolContext, ToolResult
from jarvis.tools.builtin.filesystem import safe_path
from jarvis.tools.registry import ToolRegistry

# Commands that must never be run, whatever the plan says.
BLOCKED_PATTERNS = (
    r"\brm\s+-[a-z]*r[a-z]*f[a-z]*\s+/(?!home|tmp|var/tmp)",
    r"\bmkfs\b", r"\bdd\s+.*of=/dev/", r"\bshutdown\b", r"\breboot\b",
    r":\(\)\s*\{.*\};\s*:",                # fork bomb
    r"\bcurl\b[^|]*\|\s*(ba)?sh",          # curl | sh
    r"\bwget\b[^|]*\|\s*(ba)?sh",
    r"\bformat\s+[a-z]:", r"\brmdir\s+/s\s+/q\s+[a-z]:\\?$",
)


def _blocked(command: str) -> Optional[str]:
    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, command, re.I):
            return pattern
    return None


def _run(command: str, cwd: str, timeout: int, env: Optional[Dict[str, str]] = None
         ) -> Dict[str, Any]:
    environment = dict(os.environ)
    environment["PYTHONUNBUFFERED"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment.update(env or {})
    proc = subprocess.run(
        command, shell=True, cwd=cwd, capture_output=True, text=True,
        timeout=timeout, env=environment,
    )
    return {
        "command": command, "cwd": cwd, "exit_code": proc.returncode,
        "stdout": proc.stdout[-20_000:], "stderr": proc.stderr[-20_000:],
        "output": f"{proc.stdout}\n{proc.stderr}".strip()[-20_000:],
    }


def shell_run(context: ToolContext, cmd: str, cwd: str = ".", timeout: Optional[int] = None
              ) -> ToolResult:
    if not cmd or not cmd.strip():
        return ToolResult.failure("no command supplied")
    blocked = _blocked(cmd)
    if blocked:
        return ToolResult.failure(f"command refused by safety policy (matched {blocked})",
                                  kind="permission_denied")
    workdir = safe_path(context, cwd)
    if not os.path.isdir(workdir):
        return ToolResult.failure(f"no such directory: {cwd}")
    limit = int(timeout or context.timeout or 120)
    try:
        result = _run(cmd, workdir, limit)
    except subprocess.TimeoutExpired:
        return ToolResult.failure(f"command timed out after {limit}s", kind="transient")
    return ToolResult(
        ok=result["exit_code"] == 0, value=result,
        error="" if result["exit_code"] == 0 else f"exit code {result['exit_code']}",
        error_kind="" if result["exit_code"] == 0 else "tool_error",
        evidence=f"$ {cmd} -> exit {result['exit_code']}",
    )


# --------------------------------------------------------------------------- #
# test runners
# --------------------------------------------------------------------------- #
def detect_test_command(root: str) -> str:
    if os.path.exists(os.path.join(root, "pytest.ini")) or os.path.exists(
        os.path.join(root, "pyproject.toml")
    ) or os.path.isdir(os.path.join(root, "tests")):
        return f"{sys.executable} -m pytest -q"
    if os.path.isdir(os.path.join(root, "test")):
        return f"{sys.executable} -m unittest discover -s test -q"
    if os.path.exists(os.path.join(root, "package.json")):
        return "npm test --silent"
    return f"{sys.executable} -m unittest discover -q"


def code_run_tests(context: ToolContext, cwd: str = ".", cmd: str = "",
                   timeout: Optional[int] = None) -> ToolResult:
    workdir = safe_path(context, cwd)
    command = cmd or detect_test_command(workdir)
    limit = int(timeout or context.timeout or 600)
    try:
        result = _run(command, workdir, limit)
    except subprocess.TimeoutExpired:
        return ToolResult.failure(f"test run timed out after {limit}s", kind="transient")

    passed = result["exit_code"] == 0
    summary = summarise_test_output(result["output"], passed)
    return ToolResult(
        ok=True,   # the tool succeeded; whether tests passed is data, not a tool failure
        value={"passed": passed, "command": command, "exit_code": result["exit_code"],
               "summary": summary, "output": result["output"][-8_000:]},
        evidence=f"{command} -> exit {result['exit_code']}: {summary}",
    )


def summarise_test_output(output: str, passed: bool) -> str:
    text = output or ""
    patterns = (
        r"(\d+) passed", r"(\d+) failed", r"(\d+) error", r"(\d+) skipped",
        r"Ran (\d+) test",
    )
    parts = []
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            parts.append(f"{match.group(1)} {pattern.split(')')[1].strip()}")
    verdict = "PASS" if passed else "FAIL"
    return f"{verdict} " + (", ".join(parts) if parts else text.strip().splitlines()[-1][:160]
                            if text.strip() else "(no output)")


def code_run_python(context: ToolContext, code: str, cwd: str = ".",
                    timeout: Optional[int] = None) -> ToolResult:
    workdir = safe_path(context, cwd)
    command = f'{sys.executable} -c {_quote(code)}'
    limit = int(timeout or context.timeout or 300)
    try:
        result = _run(command, workdir, limit)
    except subprocess.TimeoutExpired:
        return ToolResult.failure(f"python run timed out after {limit}s", kind="transient")
    return ToolResult(
        ok=result["exit_code"] == 0, value=result,
        error=result["output"][-2_000:] if result["exit_code"] else "",
        error_kind="tool_error" if result["exit_code"] else "",
        evidence=f"python -c -> exit {result['exit_code']}",
    )


def code_apply_patch(context: ToolContext, path: str, find: str, replace: str,
                     count: int = 1) -> ToolResult:
    """Deterministic source edit: replace ``find`` with ``replace`` in one file."""
    target = safe_path(context, path)
    if not os.path.isfile(target):
        return ToolResult.failure(f"no such file: {path}")
    with open(target, "r", encoding="utf-8") as handle:
        original = handle.read()
    if find not in original:
        return ToolResult.failure(f"pattern not found in {path}", kind="tool_error")
    updated = original.replace(find, replace, count)
    if updated == original:
        return ToolResult.failure("replacement produced no change", kind="tool_error")
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(updated)
    return ToolResult.success(
        {"path": path, "replacements": original.count(find) if count < 0 else count},
        evidence=f"patched {path}",
    )


def _quote(text: str) -> str:
    import shlex

    return shlex.quote(text)


def register(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="shell.run", description="Run a shell command inside the workspace",
        handler=shell_run, requires=["shell.exec"], risk=Risk.EXECUTE,
        parameters={"cmd": "command line", "cwd": "working directory", "timeout": "seconds"},
    ))
    registry.register(Tool(
        name="code.run_tests", description="Run the project's test suite and report the result",
        handler=code_run_tests, requires=["code.run"], risk=Risk.EXECUTE,
        parameters={"cwd": "project directory", "cmd": "override the detected test command"},
    ))
    registry.register(Tool(
        name="code.run_python", description="Execute a short Python snippet",
        handler=code_run_python, requires=["code.run"], risk=Risk.EXECUTE,
        parameters={"code": "source to execute", "cwd": "working directory"},
    ))
    registry.register(Tool(
        name="code.apply_patch", description="Replace exact text in one source file",
        handler=code_apply_patch, requires=["fs.write"], risk=Risk.WRITE,
        parameters={"path": "file", "find": "exact text to replace", "replace": "new text"},
    ))
