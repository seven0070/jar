"""File tools, confined to the configured workspace root."""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List

from jarvis.core.models import Risk
from jarvis.tools.base import Tool, ToolContext, ToolResult
from jarvis.tools.registry import ToolRegistry

IGNORED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
                ".pytest_cache", "dist", "build"}


def safe_path(context: ToolContext, path: str) -> str:
    root = os.path.abspath(context.root)
    candidate = path if os.path.isabs(path) else os.path.join(root, path)
    candidate = os.path.abspath(os.path.normpath(candidate))
    if candidate != root and not candidate.startswith(root + os.sep):
        raise PermissionError(f"path escapes the workspace root: {path}")
    return candidate


def _walk(root: str, limit: int = 2000) -> List[str]:
    found: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
        for name in filenames:
            full = os.path.join(dirpath, name)
            found.append(os.path.relpath(full, root).replace(os.sep, "/"))
            if len(found) >= limit:
                return found
    return sorted(found)


def fs_list(context: ToolContext, path: str = ".", limit: int = 500) -> ToolResult:
    target = safe_path(context, path)
    if not os.path.isdir(target):
        return ToolResult.failure(f"not a directory: {path}")
    files = _walk(target, limit=limit)
    return ToolResult.success({"root": path, "count": len(files), "files": files},
                              evidence=f"{len(files)} entries under {path}")


def fs_read(context: ToolContext, path: str, max_bytes: int = 200_000) -> ToolResult:
    target = safe_path(context, path)
    if not os.path.isfile(target):
        return ToolResult.failure(f"no such file: {path}")
    with open(target, "r", encoding="utf-8", errors="replace") as handle:
        text = handle.read(max_bytes)
    return ToolResult.success({"path": path, "content": text, "bytes": len(text)},
                              evidence=f"read {len(text)} chars from {path}")


def fs_write(context: ToolContext, path: str, content: str, append: bool = False) -> ToolResult:
    target = safe_path(context, path)
    os.makedirs(os.path.dirname(target) or context.root, exist_ok=True)
    mode = "a" if append else "w"
    with open(target, mode, encoding="utf-8") as handle:
        handle.write(content)
    return ToolResult.success({"path": path, "bytes": len(content), "append": append},
                              evidence=f"wrote {len(content)} chars to {path}")


def fs_search(context: ToolContext, needle: str, path: str = ".", regex: bool = False,
              limit: int = 50) -> ToolResult:
    target = safe_path(context, path)
    matcher = (lambda text: re.search(needle, text, re.M)) if regex else (lambda text: needle in text)
    hits: List[Dict[str, Any]] = []
    for relative in _walk(target):
        full = os.path.join(target, relative)
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read(400_000)
        except OSError:
            continue
        if not matcher(text):
            continue
        lines = [i + 1 for i, line in enumerate(text.splitlines()) if matcher(line)]
        hits.append({"file": relative, "lines": lines[:10], "matches": len(lines)})
        if len(hits) >= limit:
            break
    return ToolResult.success({"needle": needle, "count": len(hits), "hits": hits},
                              evidence=f"{len(hits)} file(s) contain {needle!r}")


def fs_delete(context: ToolContext, path: str) -> ToolResult:
    target = safe_path(context, path)
    if not os.path.exists(target):
        return ToolResult.failure(f"no such path: {path}")
    if os.path.isdir(target):
        import shutil

        shutil.rmtree(target)
    else:
        os.remove(target)
    return ToolResult.success({"deleted": path}, evidence=f"deleted {path}")


def register(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="fs.list", description="List files inside the workspace",
        handler=fs_list, requires=["fs.read"], risk=Risk.READ,
        parameters={"path": "sub-directory to list", "limit": "max entries"},
    ))
    registry.register(Tool(
        name="fs.read", description="Read a text file from the workspace",
        handler=fs_read, requires=["fs.read"], risk=Risk.READ,
        parameters={"path": "file to read"},
    ))
    registry.register(Tool(
        name="fs.write", description="Create or overwrite a file in the workspace",
        handler=fs_write, requires=["fs.write"], risk=Risk.WRITE,
        parameters={"path": "file to write", "content": "text", "append": "append instead"},
    ))
    registry.register(Tool(
        name="fs.search", description="Search file contents inside the workspace",
        handler=fs_search, requires=["fs.read"], risk=Risk.READ,
        parameters={"needle": "text or regex", "path": "where to look"},
    ))
    registry.register(Tool(
        name="fs.delete", description="Delete a file or directory (irreversible)",
        handler=fs_delete, requires=["fs.write"], risk=Risk.IRREVERSIBLE,
        parameters={"path": "path to delete"},
    ))
