"""Configuration and platform paths.

JARVIS keeps its state in a per-user data directory (``%APPDATA%\\JARVIS`` on
Windows, ``~/.jarvis`` elsewhere) and does its file work inside a workspace root.
Both can be overridden by environment variables, which is what the tests use.
"""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def data_dir() -> str:
    override = os.environ.get("JARVIS_HOME", "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "JARVIS")
    if system == "Darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", "JARVIS")
    return os.path.join(
        os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share"),
        "jarvis",
    )


def default_workspace() -> str:
    override = os.environ.get("JARVIS_WORKSPACE", "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.abspath(os.path.join(data_dir(), "workspace"))


@dataclass
class Config:
    """Everything the application needs to boot."""

    home: str = field(default_factory=data_dir)
    workspace: str = field(default_factory=default_workspace)
    mode: str = "standard"                 # readonly | standard | autonomous
    denied_scopes: List[str] = field(default_factory=list)
    model_preference: str = "balanced"     # balanced | cheap | fast | strong
    step_retry_limit: int = 3
    task_max_attempts: int = 3
    scheduler_tick_seconds: float = 60.0
    desktop_host: str = "127.0.0.1"
    desktop_port: int = 8765
    voice_enabled: bool = False
    computer_control: bool = False
    approval_blocking: bool = False      # True = block the caller until a decision arrives
    approval_timeout: float = 120.0
    auto_approve: List[str] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def db_path(self) -> str:
        return os.path.join(self.home, "jarvis.db")

    def ensure_dirs(self) -> "Config":
        os.makedirs(self.home, exist_ok=True)
        os.makedirs(self.workspace, exist_ok=True)
        return self

    def to_dict(self) -> Dict[str, Any]:
        return {
            "home": self.home, "workspace": self.workspace, "mode": self.mode,
            "denied_scopes": list(self.denied_scopes),
            "model_preference": self.model_preference,
            "step_retry_limit": self.step_retry_limit,
            "task_max_attempts": self.task_max_attempts,
            "scheduler_tick_seconds": self.scheduler_tick_seconds,
            "desktop_host": self.desktop_host, "desktop_port": self.desktop_port,
            "voice_enabled": self.voice_enabled, "computer_control": self.computer_control,
            "approval_blocking": self.approval_blocking,
            "approval_timeout": self.approval_timeout,
            "auto_approve": list(self.auto_approve),
            "db_path": self.db_path,
        }

    @classmethod
    def from_env(cls) -> "Config":
        config = cls()
        config.mode = os.environ.get("JARVIS_MODE", config.mode).strip() or config.mode
        config.model_preference = os.environ.get("JARVIS_MODEL_PREFERENCE",
                                                 config.model_preference)
        config.desktop_host = os.environ.get("JARVIS_HOST", config.desktop_host)
        config.desktop_port = int(os.environ.get("JARVIS_PORT", config.desktop_port))
        config.voice_enabled = os.environ.get("JARVIS_VOICE", "").lower() in ("1", "true", "yes")
        config.computer_control = os.environ.get("JARVIS_COMPUTER_CONTROL", "").lower() in (
            "1", "true", "yes")
        config.approval_blocking = os.environ.get("JARVIS_APPROVAL_BLOCKING", "").lower() in (
            "1", "true", "yes")
        config.approval_timeout = float(os.environ.get("JARVIS_APPROVAL_TIMEOUT",
                                                       config.approval_timeout))
        auto = os.environ.get("JARVIS_AUTO_APPROVE", "").strip()
        if auto:
            config.auto_approve = [s.strip() for s in auto.split(",") if s.strip()]
        denied = os.environ.get("JARVIS_DENY", "").strip()
        if denied:
            config.denied_scopes = [s.strip() for s in denied.split(",") if s.strip()]
        return config.ensure_dirs()
