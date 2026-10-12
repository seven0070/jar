"""Tool contract shared by every executable capability."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from jarvis.core.models import Risk


@dataclass
class ToolContext:
    """Everything a tool may need, injected by the executive."""

    root: str = "."
    store: Any = None
    policy: Any = None
    approver: Any = None
    bus: Any = None
    memory: Any = None
    router: Any = None
    task_id: str = ""
    goal_id: str = ""
    variables: Dict[str, Any] = field(default_factory=dict)
    timeout: int = 120
    granted_scopes: List[str] = field(default_factory=list)

    def emit(self, kind: str, payload: Optional[Dict[str, Any]] = None) -> None:
        if self.bus is not None:
            self.bus.publish(kind, source="tools", payload=payload or {})


@dataclass
class ToolResult:
    ok: bool
    value: Any = None
    error: str = ""
    error_kind: str = ""
    evidence: str = ""
    duration: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok, "value": self.value, "error": self.error,
            "error_kind": self.error_kind, "evidence": self.evidence,
            "duration": round(self.duration, 4),
        }

    @staticmethod
    def success(value: Any = None, evidence: str = "") -> "ToolResult":
        return ToolResult(True, value=value, evidence=evidence)

    @staticmethod
    def failure(error: str, kind: str = "tool_error", evidence: str = "") -> "ToolResult":
        return ToolResult(False, error=error, error_kind=kind, evidence=evidence)


@dataclass
class Tool:
    """A named capability with declared scopes and risk."""

    name: str
    description: str
    handler: Callable[..., Any]
    requires: List[str] = field(default_factory=list)
    risk: Risk = Risk.READ
    parameters: Dict[str, Any] = field(default_factory=dict)
    read_only: bool = False

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name, "description": self.description,
            "requires": list(self.requires), "risk": self.risk.value,
            "parameters": self.parameters,
        }


def tool(name: str, description: str, requires: List[str], risk: Risk = Risk.READ,
         parameters: Optional[Dict[str, Any]] = None) -> Callable[[Callable], Tool]:
    """Decorator that attaches JARVIS metadata to a plain function."""

    def decorate(func: Callable) -> Tool:
        return Tool(
            name=name, description=description, handler=func, requires=list(requires),
            risk=risk, parameters=parameters or {}, read_only=risk is Risk.READ,
        )

    return decorate
