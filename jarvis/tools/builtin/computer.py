"""Computer-control tools.

Real desktop control (keyboard, mouse, windows) is a *system* level capability. It
is off unless ``JARVIS_COMPUTER_CONTROL=1`` is set and a control backend is
importable, and even then every call still passes through the policy engine, so it
either runs under an explicit approval or is refused. When it is disabled the tool
returns an honest capability report instead of pretending to act.
"""

from __future__ import annotations

import os
import platform
import subprocess
from typing import Any, Dict

from jarvis.core.models import Risk
from jarvis.tools.base import Tool, ToolContext, ToolResult
from jarvis.tools.registry import ToolRegistry


def control_enabled() -> bool:
    return os.environ.get("JARVIS_COMPUTER_CONTROL", "").strip().lower() in ("1", "true", "yes")


def backend():
    try:
        import pyautogui  # type: ignore

        return pyautogui
    except Exception:
        return None


def capability_report() -> Dict[str, Any]:
    gui = backend()
    return {
        "enabled": control_enabled(),
        "backend": "pyautogui" if gui else None,
        "platform": platform.system(),
        "display": bool(os.environ.get("DISPLAY") or platform.system() == "Windows"),
        "reason": "" if control_enabled() else
                  "computer control is disabled; set JARVIS_COMPUTER_CONTROL=1 to enable",
    }


def computer_status(context: ToolContext) -> ToolResult:
    report = capability_report()
    return ToolResult.success(report, evidence=f"computer control enabled={report['enabled']}")


def computer_type(context: ToolContext, text: str, window: str = "") -> ToolResult:
    report = capability_report()
    if not report["enabled"] or not report["backend"]:
        return ToolResult.success(
            {"performed": False, "would_type": text, "window": window, **report},
            evidence="dry-run: computer control disabled, action recorded but not performed",
        )
    gui = backend()
    if window:
        _focus_window(window)
    gui.typewrite(text, interval=0.01)
    return ToolResult.success({"performed": True, "chars": len(text), "window": window},
                              evidence=f"typed {len(text)} characters")


def computer_click(context: ToolContext, x: int = 0, y: int = 0, window: str = "",
                   button: str = "left") -> ToolResult:
    report = capability_report()
    if not report["enabled"] or not report["backend"]:
        return ToolResult.success({"performed": False, "would_click": [x, y], **report},
                                  evidence="dry-run: computer control disabled")
    gui = backend()
    if window:
        _focus_window(window)
    gui.click(x=x, y=y, button=button)
    return ToolResult.success({"performed": True, "x": x, "y": y, "button": button},
                              evidence=f"clicked {button} at {x},{y}")


def computer_hotkey(context: ToolContext, keys: str) -> ToolResult:
    report = capability_report()
    if not report["enabled"] or not report["backend"]:
        return ToolResult.success({"performed": False, "would_press": keys, **report},
                                  evidence="dry-run: computer control disabled")
    gui = backend()
    gui.hotkey(*[k.strip() for k in keys.split("+") if k.strip()])
    return ToolResult.success({"performed": True, "keys": keys}, evidence=f"pressed {keys}")


def computer_screenshot(context: ToolContext, path: str = "screenshot.png") -> ToolResult:
    from jarvis.tools.builtin.filesystem import safe_path

    target = safe_path(context, path)
    report = capability_report()
    if not report["enabled"] or not report["backend"]:
        return ToolResult.success({"performed": False, "path": None, **report},
                                  evidence="dry-run: computer control disabled")
    backend().screenshot(target)
    return ToolResult.success({"performed": True, "path": path}, evidence=f"saved {path}")


def _focus_window(title: str) -> None:
    if platform.system() == "Windows":
        script = (
            "$w = Get-Process | Where-Object {$_.MainWindowTitle -like '*" + title.replace("'", "") +
            "*'} | Select-Object -First 1; "
            "if ($w) { (New-Object -ComObject WScript.Shell).AppActivate($w.Id) }"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", script],
                       capture_output=True, text=True, timeout=15)


def register(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="computer.status", description="Report whether desktop control is available",
        handler=computer_status, requires=[], risk=Risk.READ,
    ))
    registry.register(Tool(
        name="computer.type", description="Type text into the focused window",
        handler=computer_type, requires=["computer.control"], risk=Risk.SYSTEM,
        parameters={"text": "text to type", "window": "window title to focus first"},
    ))
    registry.register(Tool(
        name="computer.click", description="Click at screen coordinates",
        handler=computer_click, requires=["computer.control"], risk=Risk.SYSTEM,
        parameters={"x": "pixels", "y": "pixels", "button": "left|right"},
    ))
    registry.register(Tool(
        name="computer.hotkey", description="Press a key combination such as ctrl+s",
        handler=computer_hotkey, requires=["computer.control"], risk=Risk.SYSTEM,
        parameters={"keys": "keys joined with +"},
    ))
    registry.register(Tool(
        name="computer.screenshot", description="Capture the screen into the workspace",
        handler=computer_screenshot, requires=["computer.control"], risk=Risk.SYSTEM,
        parameters={"path": "output file"},
    ))
