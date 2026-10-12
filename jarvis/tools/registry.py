"""Tool registry: discovery, authorization, execution and audit.

Every call goes through :meth:`ToolRegistry.execute`, which means there is exactly
one place where scopes are checked, the emergency stop is honoured, the audit log
is written and the desktop UI is notified. No tool can be reached around it.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from jarvis.core.models import Risk
from jarvis.errors import Halted, JarvisError, PermissionDenied, ToolError
from jarvis.tools.base import Tool, ToolContext, ToolResult


class ToolRegistry:
    def __init__(self, store=None, policy=None, bus=None):
        self._tools: Dict[str, Tool] = {}
        self.store = store
        self.policy = policy
        self.bus = bus
        self.calls: List[Dict[str, Any]] = []

    # -- registration ------------------------------------------------------- #
    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} is already registered")
        self._tools[tool.name] = tool
        return tool

    def register_function(self, func: Callable, name: Optional[str] = None,
                          description: str = "", requires: Optional[List[str]] = None,
                          risk: Risk = Risk.READ) -> Tool:
        return self.register(
            Tool(name=name or func.__name__, description=description or (func.__doc__ or ""),
                 handler=func, requires=list(requires or []), risk=risk)
        )

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    # -- discovery ---------------------------------------------------------- #
    def names(self) -> List[str]:
        return sorted(self._tools)

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise ToolError(f"unknown tool {name!r}") from None

    def has(self, name: str) -> bool:
        return name in self._tools

    def list(self) -> List[Tool]:
        return [self._tools[name] for name in self.names()]

    def describe(self) -> List[Dict[str, Any]]:
        return [tool.describe() for tool in self.list()]

    # -- execution ---------------------------------------------------------- #
    def execute(self, name: str, args: Optional[Dict[str, Any]] = None,
                context: Optional[ToolContext] = None) -> ToolResult:
        args = dict(args or {})
        context = context or ToolContext()
        started = time.time()
        tool = self.get(name)

        if self.policy is not None:
            self.policy.assert_running()
            already = set(context.granted_scopes)
            for scope in tool.requires:
                if scope in already:
                    continue
                self.policy.authorize(
                    scope=scope, risk=tool.risk, action=f"{name}",
                    detail=format_args(args), approver=context.approver,
                    task_id=context.task_id,
                )

        if self.bus is not None:
            self.bus.publish("tool.started", source=name,
                             payload={"tool": name, "task": context.task_id})
        if self.store is not None:
            self.store.audit("tool", "invoke", {"tool": name, "task": context.task_id,
                                                "args": _summarize_args(args)})

        try:
            value = tool.handler(context=context, **args)
            result = value if isinstance(value, ToolResult) else ToolResult.success(value)
        except PermissionDenied:
            raise
        except Halted:
            raise
        except JarvisError as exc:
            result = ToolResult.failure(str(exc), kind=exc.kind)
        except Exception as exc:
            result = ToolResult.failure(f"{type(exc).__name__}: {exc}", kind="tool_error")

        result.duration = time.time() - started
        self._record(name, args, result, context)
        if self.bus is not None:
            self.bus.publish(
                "tool.finished", source=name,
                payload={"tool": name, "ok": result.ok, "task": context.task_id,
                         "error": result.error},
            )
        if not result.ok and result.error_kind in ("permission_denied", "halted"):
            if result.error_kind == "halted":
                raise Halted(result.error)
            raise PermissionDenied(result.error)
        return result

    def _record(self, name: str, args: Dict[str, Any], result: ToolResult,
                context: ToolContext) -> None:
        entry = {"tool": name, "ok": result.ok, "duration": round(result.duration, 4),
                 "task": context.task_id, "error": result.error, "at": time.time()}
        self.calls.append(entry)
        if len(self.calls) > 1000:
            self.calls = self.calls[-1000:]
        if self.store is not None:
            self.store.audit(
                "tool", "result",
                {"tool": name, "ok": result.ok, "error": result.error[:300],
                 "duration": entry["duration"], "task": context.task_id},
            )

    def usage_summary(self) -> Dict[str, Any]:
        counts: Dict[str, int] = {}
        failures = 0
        for call in self.calls:
            counts[call["tool"]] = counts.get(call["tool"], 0) + 1
            if not call["ok"]:
                failures += 1
        return {"total_calls": len(self.calls), "by_tool": counts, "failures": failures}


def format_args(args: Dict[str, Any], limit: int = 240) -> str:
    """Render call arguments as text suitable for an approval prompt or audit row."""
    return ", ".join(f"{key}={value}" for key, value in _summarize_args(args, limit).items())


def _summarize_args(args: Dict[str, Any], limit: int = 240) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in args.items():
        text = str(value)
        out[key] = text if len(text) <= limit else text[:limit] + "..."
    return out
