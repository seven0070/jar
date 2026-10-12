"""Built-in toolset: files, shell/code, research, computer control, automation."""

from jarvis.tools.builtin import code, computer, filesystem, research, system
from jarvis.tools.registry import ToolRegistry

MODULES = (filesystem, code, research, computer, system)


def register_builtin_tools(registry: ToolRegistry) -> ToolRegistry:
    """Register every built-in tool on ``registry`` and return it."""
    for module in MODULES:
        module.register(registry)
    return registry


__all__ = ["register_builtin_tools", "MODULES"]
