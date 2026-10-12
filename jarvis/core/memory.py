"""Persistent memory: facts, episodes, preferences and reflections.

Recall is deliberately simple and inspectable - token overlap plus tag/goal
matches, weighted by importance and recency - because a memory system you cannot
explain is a memory system you cannot trust. The scoring is deterministic, so the
same query always returns the same context.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

from jarvis.core.models import Memory, new_id, now

_TOKEN = re.compile(r"[a-z0-9][a-z0-9'\-_]{1,}")
_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are", "be", "it",
    "this", "that", "with", "as", "at", "by", "from", "you", "your", "my", "me", "i",
    "please", "can", "will", "would", "should", "do", "does", "not", "no", "yes",
}


def tokenize(text: str) -> List[str]:
    return [t for t in _TOKEN.findall((text or "").lower()) if t not in _STOP and len(t) > 1]


class MemoryStore:
    """Durable memory with keyword recall."""

    def __init__(self, store, half_life_hours: float = 24 * 30):
        self.store = store
        self.half_life = half_life_hours * 3600.0

    # -- writing ------------------------------------------------------------ #
    def remember(
        self,
        content: str,
        kind: str = "fact",
        tags: Sequence[str] = (),
        goal_id: str = "",
        importance: float = 0.5,
    ) -> Memory:
        memory = Memory(
            id=new_id("mem"), kind=kind, content=content.strip(), tags=list(tags),
            goal_id=goal_id, importance=max(0.0, min(1.0, float(importance))),
        )
        self.store.save_memory(memory)
        self.store.audit("memory", "remembered", {"id": memory.id, "kind": kind})
        return memory

    def forget(self, memory_id: str) -> bool:
        ok = self.store.forget(memory_id)
        self.store.audit("memory", "forgot", {"id": memory_id, "result": ok})
        return ok

    # -- reading ------------------------------------------------------------ #
    def all(self) -> List[Memory]:
        return self.store.all_memories()

    def recall(
        self,
        query: str,
        limit: int = 8,
        goal_id: str = "",
        kinds: Optional[Iterable[str]] = None,
        min_score: float = 0.02,
    ) -> List[Dict[str, Any]]:
        """Return the memories most relevant to ``query``, best first."""
        q_tokens = tokenize(query)
        q_tags = set(q_tokens)
        kinds = set(kinds) if kinds else None
        scored: List[Dict[str, Any]] = []
        age = now()

        for memory in self.store.all_memories():
            if kinds and memory.kind not in kinds:
                continue
            m_tokens = tokenize(memory.content)
            if not q_tokens and not goal_id:
                score = memory.importance * 0.5
            else:
                overlap = len(q_tags & set(m_tokens))
                denom = math.sqrt(len(q_tokens)) * math.sqrt(max(len(m_tokens), 1)) or 1.0
                lexical = overlap / denom
                tag_match = len(q_tags & {t.lower() for t in memory.tags}) * 0.15
                goal_match = 0.25 if goal_id and memory.goal_id == goal_id else 0.0
                recency = math.exp(-max(age - memory.created_at, 0.0) / self.half_life)
                # Access frequency is deliberately NOT part of the score: letting
                # recall feed itself would make the same query return different
                # context on the second call.
                score = (
                    0.75 * lexical
                    + tag_match
                    + goal_match
                    + 0.20 * memory.importance * recency
                )
            if score < min_score:
                continue
            scored.append({"memory": memory, "score": round(score, 4)})

        scored.sort(key=lambda item: (-item["score"], -item["memory"].created_at))
        top = scored[:limit]
        for item in top:
            self.store.touch_memory(item["memory"].id)
        return top

    def context_block(self, query: str, goal_id: str = "", limit: int = 6) -> str:
        """Render recalled memory as text that can be dropped into a prompt."""
        hits = self.recall(query, limit=limit, goal_id=goal_id)
        if not hits:
            return ""
        lines = []
        for item in hits:
            memory: Memory = item["memory"]
            tags = f" [{', '.join(memory.tags)}]" if memory.tags else ""
            lines.append(f"- ({memory.kind}, {item['score']:.2f}){tags} {memory.content}")
        return "\n".join(lines)

    # -- bookkeeping -------------------------------------------------------- #
    def summarize(self) -> Dict[str, Any]:
        memories = self.store.all_memories()
        by_kind: Dict[str, int] = {}
        for memory in memories:
            by_kind[memory.kind] = by_kind.get(memory.kind, 0) + 1
        return {
            "total": len(memories),
            "by_kind": by_kind,
            "most_recent": memories[0].content if memories else "",
        }

    def prune(self, max_items: int = 5000) -> int:
        """Drop the least valuable memories when the store grows unbounded."""
        memories = self.store.all_memories()
        if len(memories) <= max_items:
            return 0
        age = now()
        ranked = sorted(
            memories,
            key=lambda m: (
                m.importance * 0.6
                + math.exp(-max(age - m.created_at, 0.0) / self.half_life) * 0.4
                + min(m.access_count, 10) * 0.01
            ),
        )
        removed = 0
        for memory in ranked[: len(memories) - max_items]:
            if self.store.forget(memory.id):
                removed += 1
        self.store.audit("memory", "pruned", {"removed": removed})
        return removed
