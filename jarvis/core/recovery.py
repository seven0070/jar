"""Failure classification, repair and escalation.

The executive asks the recovery manager three questions after every failure:

1. What kind of failure is this?
2. Can it be retried, and how many times?
3. Is there a repair that changes the approach rather than repeating it?

Anything that cannot be recovered becomes a *blocker* - a persisted state with a
human-readable reason, never a silent drop.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from jarvis.core.models import Task, now
from jarvis.errors import (
    Halted,
    JarvisError,
    ModelUnavailable,
    PermissionDenied,
    PlannerError,
    ToolError,
    TransientError,
    Unverifiable,
    VerificationFailed,
)

RETRYABLE_KINDS = {"transient", "tool_error", "model_unavailable", "verification_failed"}
TERMINAL_KINDS = {"permission_denied", "halted", "unverifiable", "planner_error"}


@dataclass
class RecoveryAction:
    action: str                       # retry | repair | escalate | abort
    reason: str
    delay: float = 0.0
    repair: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RepairResult:
    repaired: bool
    detail: str
    applied: List[str] = field(default_factory=list)


class RecoveryManager:
    def __init__(self, base_delay: float = 0.0, max_delay: float = 30.0,
                 repairs: Optional[Dict[str, Callable[[Task, str], RepairResult]]] = None):
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.repairs: Dict[str, Callable[[Task, str], RepairResult]] = repairs or {}
        self.history: List[Dict[str, Any]] = []

    # -- classification ----------------------------------------------------- #
    @staticmethod
    def classify(error: BaseException | str) -> str:
        if isinstance(error, JarvisError):
            return error.kind
        text = str(error).lower()
        for needle, kind in (
            ("timeout", "transient"), ("timed out", "transient"),
            ("rate limit", "transient"), ("temporarily", "transient"),
            ("connection", "transient"), ("locked", "transient"),
            ("permission", "permission_denied"), ("denied", "permission_denied"),
        ):
            if needle in text:
                return kind
        return "tool_error"

    def retryable(self, kind: str) -> bool:
        return kind in RETRYABLE_KINDS and kind not in TERMINAL_KINDS

    # -- planning ----------------------------------------------------------- #
    def decide(self, task: Task, error: BaseException | str) -> RecoveryAction:
        kind = self.classify(error)
        record = {"task": task.id, "kind": kind, "attempts": task.attempts, "at": now()}

        if kind == "halted":
            record["action"] = "abort"
            self.history.append(record)
            return RecoveryAction("abort", "emergency stop engaged")

        if kind in TERMINAL_KINDS:
            record["action"] = "escalate"
            self.history.append(record)
            return RecoveryAction("escalate", f"{kind} is not recoverable automatically")

        if task.attempts >= task.max_attempts:
            repair = self.try_repair(task, str(error))
            if repair.repaired:
                record.update(action="repair", repair="; ".join(repair.applied))
                self.history.append(record)
                return RecoveryAction("repair", repair.detail, repair="; ".join(repair.applied))
            record["action"] = "escalate"
            self.history.append(record)
            return RecoveryAction("escalate", f"exhausted {task.attempts}/{task.max_attempts} attempts")

        if self.repairs and task.attempts >= 1:
            repair = self.try_repair(task, str(error))
            if repair.repaired:
                record.update(action="repair", repair="; ".join(repair.applied))
                self.history.append(record)
                return RecoveryAction("repair", repair.detail, repair="; ".join(repair.applied))

        delay = min(self.base_delay * (2 ** max(task.attempts - 1, 0)), self.max_delay)
        record.update(action="retry", delay=delay)
        self.history.append(record)
        return RecoveryAction("retry", f"retryable {kind}; attempt {task.attempts + 1}", delay=delay)

    # -- repairs ------------------------------------------------------------ #
    def register_repair(self, name: str, handler: Callable[[Task, str], RepairResult]) -> None:
        self.repairs[name] = handler

    def try_repair(self, task: Task, error_text: str) -> RepairResult:
        applied: List[str] = []
        details: List[str] = []
        for name, handler in self.repairs.items():
            try:
                result = handler(task, error_text)
            except Exception as exc:  # a repair must never make things worse
                details.append(f"{name} failed: {exc}")
                continue
            if result.repaired:
                applied.append(name)
                details.append(result.detail)
        return RepairResult(bool(applied), "; ".join(details) or "no repair applied", applied)

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)
