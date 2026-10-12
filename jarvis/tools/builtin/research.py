"""Research tools: fetch, search and evidence collection.

Network access is optional. When a fetch fails the tool reports it as a transient
failure so recovery can retry, and ``research.collect`` still produces a usable
evidence bundle from local memory so the task does not stall.
"""

from __future__ import annotations

import html
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List

from jarvis.core.models import Risk
from jarvis.tools.base import Tool, ToolContext, ToolResult
from jarvis.tools.registry import ToolRegistry

USER_AGENT = "JARVIS/0.1 (+personal assistant)"
_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\n{3,}")


def _fetch(url: str, timeout: int = 20) -> Dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(1_000_000)
        charset = response.headers.get_content_charset() or "utf-8"
    text = raw.decode(charset, errors="replace")
    if "<html" in text[:2000].lower():
        text = _strip_html(text)
    return {"url": url, "status": 200, "text": text[:40_000], "chars": len(text)}


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    return _WHITESPACE.sub("\n\n", text).strip()


def research_fetch(context: ToolContext, url: str, timeout: int = 20) -> ToolResult:
    if not url.lower().startswith(("http://", "https://")):
        return ToolResult.failure("only http(s) URLs may be fetched", kind="permission_denied")
    try:
        return ToolResult.success(_fetch(url, timeout), evidence=f"fetched {url}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return ToolResult.failure(f"could not fetch {url}: {exc}", kind="transient")


def research_search(context: ToolContext, query: str, limit: int = 5) -> ToolResult:
    endpoint = os.environ.get("JARVIS_SEARCH_URL", "").strip()
    if not endpoint:
        return ToolResult.failure(
            "no search provider configured (set JARVIS_SEARCH_URL); "
            "falling back to local memory", kind="transient",
        )
    url = endpoint.replace("{query}", urllib.parse.quote_plus(query))
    result = research_fetch(context, url, timeout=20)
    if not result.ok:
        return result
    return ToolResult.success({"query": query, "results": result.value},
                              evidence=f"searched {query!r}")


def research_collect(context: ToolContext, question: str = "", limit: int = 5) -> ToolResult:
    """Gather whatever evidence is available: memory first, network if permitted."""
    evidence: List[Dict[str, Any]] = []
    if context.memory is not None:
        for hit in context.memory.recall(question or "context", limit=limit):
            memory = hit["memory"]
            evidence.append({"source": "memory", "score": hit["score"],
                             "content": memory.content})
    sources = context.variables.get("sources") or []
    for url in list(sources)[:3]:
        fetched = research_fetch(context, url)
        if fetched.ok:
            evidence.append({"source": url, "content": fetched.value["text"][:4_000]})
        else:
            evidence.append({"source": url, "error": fetched.error})
    return ToolResult.success(
        {"question": question, "count": len(evidence), "evidence": evidence},
        evidence=f"collected {len(evidence)} evidence item(s)",
    )


def register(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="research.fetch", description="Fetch a URL and return its text",
        handler=research_fetch, requires=["network.fetch"], risk=Risk.EXECUTE,
        parameters={"url": "http(s) URL"},
    ))
    registry.register(Tool(
        name="research.search", description="Query the configured search provider",
        handler=research_search, requires=["network.search"], risk=Risk.EXECUTE,
        parameters={"query": "search text"},
    ))
    registry.register(Tool(
        name="research.collect", description="Collect evidence from memory and permitted sources",
        handler=research_collect, requires=[], risk=Risk.READ,
        parameters={"question": "what to gather evidence about"},
    ))
