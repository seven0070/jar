"""Independent verification of completed work.

The executive never accepts a tool's own claim of success. Each task carries
``success_criteria`` - a list of :class:`~jarvis.core.models.Check` objects - and the
verifier re-derives the answer from the outside world: it re-reads the file, re-runs
the command, re-computes the value. The evidence it collects is stored with the
verification record so a human can audit the claim later.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from typing import Any, Dict, List, Optional

from jarvis.core.models import Check, CheckResult, Task, Verification, Verdict


class SandboxViolation(Exception):
    """A check tried to reach outside the configured workspace root."""


class Verifier:
    """Evaluates success criteria without trusting the executor."""

    def __init__(self, root: str, timeout: int = 120):
        self.root = os.path.abspath(root)
        self.timeout = timeout

    # -- path safety -------------------------------------------------------- #
    def resolve(self, path: str) -> str:
        candidate = path if os.path.isabs(path) else os.path.join(self.root, path)
        candidate = os.path.abspath(os.path.normpath(candidate))
        if candidate != self.root and not candidate.startswith(self.root + os.sep):
            raise SandboxViolation(f"path escapes workspace root: {path}")
        return candidate

    # -- public API --------------------------------------------------------- #
    def verify(self, task: Task, store=None) -> Verification:
        results: List[CheckResult] = []
        evidence: List[str] = []

        if not task.success_criteria:
            verdict = Verdict.UNVERIFIABLE
            evidence.append("no success criteria were declared for this task")
        else:
            for check in task.success_criteria:
                result = self.run_check(check, task)
                results.append(result)
                evidence.append(
                    f"[{'PASS' if result.passed else 'FAIL'}] {check.description or check.kind}: "
                    f"expected={_short(result.expected)} actual={_short(result.actual)}"
                )
            failed = [r for r in results if not r.passed]
            verdict = Verdict.VERIFIED if not failed else Verdict.FAILED

        verification = Verification(
            task_id=task.id, verdict=verdict, checks=results, evidence="\n".join(evidence)
        )
        if store is not None:
            store.save_verification(verification)
            store.audit(
                "verifier", f"verdict_{verdict.value}",
                {"task": task.id, "summary": verification.summary},
            )
        return verification

    # -- individual checks -------------------------------------------------- #
    def run_check(self, check: Check, task: Optional[Task] = None) -> CheckResult:
        kind = check.kind
        handler = getattr(self, f"_check_{kind}", None)
        if handler is None:
            return CheckResult(kind, False, check.expect, None, f"unknown check kind {kind!r}")
        try:
            return handler(check, task)
        except SandboxViolation as exc:
            return CheckResult(kind, False, check.expect, None, str(exc))
        except subprocess.TimeoutExpired:
            return CheckResult(kind, False, check.expect, "timeout",
                               f"command exceeded {self.timeout}s")
        except Exception as exc:  # a check must never crash the cycle
            return CheckResult(kind, False, check.expect, None, f"{type(exc).__name__}: {exc}")

    # -- check implementations --------------------------------------------- #
    def _check_file_exists(self, check: Check, task) -> CheckResult:
        path = self.resolve(str(check.args.get("path", "")))
        exists = os.path.exists(path)
        expect = True if check.expect is None else bool(check.expect)
        return CheckResult("file_exists", exists is expect, expect, exists, path)

    def _check_file_contains(self, check: Check, task) -> CheckResult:
        path = self.resolve(str(check.args.get("path", "")))
        needle = str(check.args.get("needle", check.expect or ""))
        regex = bool(check.args.get("regex", False))
        if not os.path.exists(path):
            return CheckResult("file_contains", False, needle, None, f"missing file {path}")
        text = _read(path)
        found = bool(re.search(needle, text, re.M)) if regex else needle in text
        return CheckResult("file_contains", found, needle, found, path)

    def _check_file_not_contains(self, check: Check, task) -> CheckResult:
        path = self.resolve(str(check.args.get("path", "")))
        needle = str(check.args.get("needle", check.expect or ""))
        if not os.path.exists(path):
            return CheckResult("file_not_contains", True, needle, None, f"missing file {path}")
        text = _read(path)
        return CheckResult("file_not_contains", needle not in text, f"not {needle}",
                           needle in text, path)

    def _check_command(self, check: Check, task) -> CheckResult:
        cmd = str(check.args.get("cmd", ""))
        cwd = self.resolve(str(check.args.get("cwd", ".")))
        expected_code = int(check.expect) if check.expect is not None else 0
        proc = subprocess.run(
            cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=self.timeout
        )
        return CheckResult(
            "command", proc.returncode == expected_code, f"exit {expected_code}",
            f"exit {proc.returncode}", _tail(proc.stdout or proc.stderr, 400),
        )

    def _check_command_output_contains(self, check: Check, task) -> CheckResult:
        cmd = str(check.args.get("cmd", ""))
        cwd = self.resolve(str(check.args.get("cwd", ".")))
        needle = str(check.args.get("needle", check.expect or ""))
        proc = subprocess.run(
            cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=self.timeout
        )
        hay = f"{proc.stdout}\n{proc.stderr}"
        return CheckResult("command_output_contains", needle in hay, needle,
                           needle in hay, _tail(hay, 400))

    def _check_command_output_not_contains(self, check: Check, task) -> CheckResult:
        cmd = str(check.args.get("cmd", ""))
        cwd = self.resolve(str(check.args.get("cwd", ".")))
        needle = str(check.args.get("needle", check.expect or ""))
        proc = subprocess.run(
            cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=self.timeout
        )
        hay = f"{proc.stdout}\n{proc.stderr}"
        return CheckResult("command_output_not_contains", needle not in hay, f"not {needle}",
                           needle in hay, _tail(hay, 400))

    def _check_result_field(self, check: Check, task) -> CheckResult:
        """Compare a field of the task's own result against an expected value.

        This is the weakest form of evidence and is only accepted when a task
        explicitly declares it; the verifier still reads the value itself rather
        than the step's narrative.
        """
        dotted = str(check.args.get("path", ""))
        actual = _dig(task.result if task else None, dotted)
        expected = check.expect
        passed = _loose_eq(actual, expected)
        return CheckResult("result_field", passed, expected, actual, dotted)

    def _check_tests_passed(self, check: Check, task) -> CheckResult:
        """Run the project's own test command and require a clean exit."""
        cwd = self.resolve(str(check.args.get("cwd", ".")))
        cmd = str(check.args.get("cmd", "python -m pytest -q"))
        proc = subprocess.run(
            cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=self.timeout
        )
        hay = f"{proc.stdout}\n{proc.stderr}"
        passed = proc.returncode == 0
        return CheckResult("tests_passed", passed, "exit 0", f"exit {proc.returncode}",
                           _tail(hay, 400))

    def _check_glob_count(self, check: Check, task) -> CheckResult:
        import glob as _glob

        pattern = self.resolve(str(check.args.get("pattern", "")))
        expected = int(check.expect if check.expect is not None else 1)
        actual = len(_glob.glob(pattern, recursive=True))
        return CheckResult("glob_count", actual >= expected, f">= {expected}", actual, pattern)

    def _check_value_equals(self, check: Check, task) -> CheckResult:
        actual = check.args.get("actual")
        expected = check.expect
        return CheckResult("value_equals", _loose_eq(actual, expected), expected, actual, "")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _read(path: str, limit: int = 200_000) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read(limit)


def _tail(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "..." + text[-limit:]


def _short(value: Any) -> str:
    text = str(value)
    return text if len(text) <= 120 else text[:117] + "..."


def _dig(obj: Any, dotted: str) -> Any:
    cur = obj
    for part in dotted.split("."):
        if part == "":
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, (list, tuple)):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            cur = getattr(cur, part, None)
        if cur is None:
            return None
    return cur


def _loose_eq(actual: Any, expected: Any) -> bool:
    if actual is None or expected is None:
        return actual is expected
    if isinstance(actual, bool) or isinstance(expected, bool):
        return str(actual).lower() == str(expected).lower()
    try:
        return float(actual) == float(expected)
    except (TypeError, ValueError):
        return str(actual) == str(expected)


def quote(value: str) -> str:
    return shlex.quote(value)


def checks_from_specs(specs: List[Dict[str, Any]]) -> List[Check]:
    return [Check.from_dict(spec) for spec in (specs or [])]
