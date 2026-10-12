"""Domain model: goals, tasks, steps, decisions, checkpoints and verdicts.

Every object that crosses the executive boundary is a plain dataclass so it can be
persisted to SQLite, replayed after a restart and shown in the desktop UI.
"""

from __future__ import annotations

import enum
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now() -> float:
    return time.time()


def iso(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts if ts is not None else now()))


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #
class GoalState(str, enum.Enum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"

    @property
    def open(self) -> bool:
        return self in (GoalState.PROPOSED, GoalState.ACTIVE, GoalState.PAUSED, GoalState.BLOCKED)


class Phase(str, enum.Enum):
    """The nine stages of the JARVIS operating cycle, in execution order."""

    RECEIVED = "received"
    RETRIEVED = "retrieved"
    OBJECTIVE = "objective_defined"
    PLANNED = "planned"
    AUTHORIZED = "authorized"
    EXECUTED = "executed"
    VERIFIED = "verified"
    REPAIRED = "repaired"
    COMMITTED = "memory_updated"

    @property
    def order(self) -> int:
        return list(Phase).index(self)


class TaskState(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in (
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.BLOCKED,
            TaskState.CANCELLED,
        )

    @property
    def resumable(self) -> bool:
        """States that survive a restart and must be re-queued by Odysseus."""
        return self in (TaskState.QUEUED, TaskState.RUNNING, TaskState.VERIFYING)


class StepState(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class Verdict(str, enum.Enum):
    VERIFIED = "verified"
    FAILED = "failed"
    UNVERIFIABLE = "unverifiable"

    @property
    def ok(self) -> bool:
        return self is Verdict.VERIFIED


class Risk(str, enum.Enum):
    READ = "read"          # 1 - observe only
    WRITE = "write"        # 2 - mutate workspace state
    EXECUTE = "execute"    # 3 - run code / shell
    SYSTEM = "system"      # 4 - control the computer, install software
    IRREVERSIBLE = "irreversible"  # 5 - publish, delete, spend, self-modify

    @property
    def level(self) -> int:
        return _RISK_LEVELS[self]


_RISK_LEVELS: Dict[Risk, int] = {
    Risk.READ: 1,
    Risk.WRITE: 2,
    Risk.EXECUTE: 3,
    Risk.SYSTEM: 4,
    Risk.IRREVERSIBLE: 5,
}


class Trigger(str, enum.Enum):
    """How a task entered the system."""

    USER_REQUEST = "user_request"
    GOAL = "goal"
    EVENT = "event"
    SCHEDULE = "schedule"
    PROACTIVE = "proactive"


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #
@dataclass
class Check:
    """A success criterion that the *verifier* evaluates independently."""

    kind: str                                   # file_exists | file_contains | command | equals | ...
    expect: Any = None
    args: Dict[str, Any] = field(default_factory=dict)
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Check":
        return cls(
            kind=data.get("kind", ""),
            expect=data.get("expect"),
            args=dict(data.get("args") or {}),
            description=data.get("description", ""),
        )


def as_check(value: Any) -> "Check":
    """Accept either a :class:`Check` or its dict form."""
    if isinstance(value, Check):
        return value
    if isinstance(value, dict):
        return Check.from_dict(value)
    raise TypeError(f"expected a Check or a dict, got {type(value).__name__}")


@dataclass
class CheckResult:
    kind: str
    passed: bool
    expected: Any = None
    actual: Any = None
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Step:
    """One executable unit of a plan."""

    id: str = field(default_factory=lambda: new_id("step"))
    ordinal: int = 0
    tool: str = ""
    args: Dict[str, Any] = field(default_factory=dict)
    title: str = ""
    produces: str = ""                          # name this step's output is stored under
    requires: List[str] = field(default_factory=list)
    optional: bool = False
    state: StepState = StepState.PENDING
    output: Any = None
    error: str = ""
    attempts: int = 0

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["state"] = self.state.value
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Step":
        return cls(
            id=data.get("id") or new_id("step"),
            ordinal=int(data.get("ordinal", 0)),
            tool=data.get("tool", ""),
            args=dict(data.get("args") or {}),
            title=data.get("title", ""),
            produces=data.get("produces", ""),
            requires=list(data.get("requires") or []),
            optional=bool(data.get("optional", False)),
            state=StepState(data.get("state", "pending")),
            output=data.get("output"),
            error=data.get("error", ""),
            attempts=int(data.get("attempts", 0)),
        )


@dataclass
class Task:
    """A unit of work carried through the operating cycle."""

    id: str = field(default_factory=lambda: new_id("task"))
    intent: str = ""
    goal_id: str = ""
    trigger: Trigger = Trigger.USER_REQUEST
    state: TaskState = TaskState.QUEUED
    phase: Phase = Phase.RECEIVED
    objective: str = ""
    success_criteria: List[Check] = field(default_factory=list)
    plan: List[Step] = field(default_factory=list)
    priority: int = 5
    attempts: int = 0
    max_attempts: int = 3
    result: Any = None
    verdict: str = ""
    error: str = ""
    error_kind: str = ""
    created_at: float = field(default_factory=now)
    updated_at: float = field(default_factory=now)
    started_at: float = 0.0
    finished_at: float = 0.0
    context: Dict[str, Any] = field(default_factory=dict)
    model: str = ""
    approvals: List[str] = field(default_factory=list)

    # -- serialisation ------------------------------------------------------ #
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "intent": self.intent,
            "goal_id": self.goal_id,
            "trigger": self.trigger.value,
            "state": self.state.value,
            "phase": self.phase.value,
            "objective": self.objective,
            "success_criteria": [c.to_dict() for c in self.success_criteria],
            "plan": [s.to_dict() for s in self.plan],
            "priority": self.priority,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "result": self.result,
            "verdict": self.verdict,
            "error": self.error,
            "error_kind": self.error_kind,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "context": self.context,
            "model": self.model,
            "approvals": self.approvals,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Task":
        return cls(
            id=data.get("id") or new_id("task"),
            intent=data.get("intent", ""),
            goal_id=data.get("goal_id", ""),
            trigger=Trigger(data.get("trigger", "user_request")),
            state=TaskState(data.get("state", "queued")),
            phase=Phase(data.get("phase", "received")),
            objective=data.get("objective", ""),
            success_criteria=[Check.from_dict(c) for c in (data.get("success_criteria") or [])],
            plan=[Step.from_dict(s) for s in (data.get("plan") or [])],
            priority=int(data.get("priority", 5)),
            attempts=int(data.get("attempts", 0)),
            max_attempts=int(data.get("max_attempts", 3)),
            result=data.get("result"),
            verdict=data.get("verdict", ""),
            error=data.get("error", ""),
            error_kind=data.get("error_kind", ""),
            created_at=float(data.get("created_at", now())),
            updated_at=float(data.get("updated_at", now())),
            started_at=float(data.get("started_at", 0.0)),
            finished_at=float(data.get("finished_at", 0.0)),
            context=dict(data.get("context") or {}),
            model=data.get("model", ""),
            approvals=list(data.get("approvals") or []),
        )

    @property
    def pending_steps(self) -> List[Step]:
        return [s for s in self.plan if s.state in (StepState.PENDING, StepState.FAILED)]

    @property
    def duration(self) -> float:
        if not self.started_at:
            return 0.0
        return (self.finished_at or now()) - self.started_at


@dataclass
class Goal:
    """A durable objective managed across sessions by Odysseus."""

    id: str = field(default_factory=lambda: new_id("goal"))
    title: str = ""
    objective: str = ""
    definition_of_done: List[Check] = field(default_factory=list)
    state: GoalState = GoalState.PROPOSED
    priority: int = 5
    autonomy: str = "standard"           # readonly | standard | autonomous
    milestones: List[str] = field(default_factory=list)
    monitors: List[Dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=now)
    updated_at: float = field(default_factory=now)
    deadline: float = 0.0
    notes: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["state"] = self.state.value
        data["definition_of_done"] = [c.to_dict() for c in self.definition_of_done]
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Goal":
        return cls(
            id=data.get("id") or new_id("goal"),
            title=data.get("title", ""),
            objective=data.get("objective", ""),
            definition_of_done=[Check.from_dict(c) for c in (data.get("definition_of_done") or [])],
            state=GoalState(data.get("state", "proposed")),
            priority=int(data.get("priority", 5)),
            autonomy=data.get("autonomy", "standard"),
            milestones=list(data.get("milestones") or []),
            monitors=list(data.get("monitors") or []),
            created_at=float(data.get("created_at", now())),
            updated_at=float(data.get("updated_at", now())),
            deadline=float(data.get("deadline", 0.0)),
            notes=list(data.get("notes") or []),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass
class Event:
    """Anything JARVIS can react to: a user message, a file change, a schedule tick."""

    id: str = field(default_factory=lambda: new_id("evt"))
    kind: str = ""
    source: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=now)
    consumed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Decision:
    """An auditable record of a choice the executive made."""

    id: str = field(default_factory=lambda: new_id("dec"))
    task_id: str = ""
    kind: str = ""                     # model_selected | plan_chosen | escalation | approval ...
    rationale: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=now)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Memory:
    """A durable memory: fact, episode, preference or reflection."""

    id: str = field(default_factory=lambda: new_id("mem"))
    kind: str = "fact"                 # fact | episode | preference | reflection
    content: str = ""
    tags: List[str] = field(default_factory=list)
    goal_id: str = ""
    importance: float = 0.5
    created_at: float = field(default_factory=now)
    accessed_at: float = 0.0
    access_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ApprovalRequest:
    """A request that blocks a task until the user decides."""

    id: str = field(default_factory=lambda: new_id("apr"))
    task_id: str = ""
    action: str = ""
    scope: str = ""
    risk: str = "execute"
    detail: str = ""
    state: str = "pending"             # pending | approved | denied | expired
    requested_at: float = field(default_factory=now)
    decided_at: float = 0.0
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Verification:
    id: str = field(default_factory=lambda: new_id("ver"))
    task_id: str = ""
    verdict: Verdict = Verdict.UNVERIFIABLE
    checks: List[CheckResult] = field(default_factory=list)
    evidence: str = ""
    created_at: float = field(default_factory=now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "verdict": self.verdict.value,
            "checks": [c.to_dict() for c in self.checks],
            "evidence": self.evidence,
            "created_at": self.created_at,
        }

    @property
    def summary(self) -> str:
        passed = sum(1 for c in self.checks if c.passed)
        return f"{passed}/{len(self.checks)} checks passed -> {self.verdict.value}"


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False)


def loads(text: str, default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default
