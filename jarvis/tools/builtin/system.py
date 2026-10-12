"""Automation tools: scheduling, notifications, memory, goals and the answer tool."""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from jarvis.core.models import Goal, GoalState, Risk, Task, TaskState, Trigger
from jarvis.intelligence.providers import CompletionRequest
from jarvis.tools.base import Tool, ToolContext, ToolResult
from jarvis.tools.registry import ToolRegistry


# --------------------------------------------------------------------------- #
# scheduling
# --------------------------------------------------------------------------- #
def schedule_create(context: ToolContext, intent: str, delay_seconds: float = 0.0,
                    goal_id: str = "", priority: int = 5, repeat: bool = False) -> ToolResult:
    due = time.time() + max(float(delay_seconds), 0.0)
    task = Task(
        intent=intent, goal_id=goal_id or context.goal_id, trigger=Trigger.SCHEDULE,
        state=TaskState.QUEUED, priority=int(priority),
        context={"due_at": due, "repeat": bool(repeat),
                 "interval_seconds": float(delay_seconds) if repeat else 0.0},
    )
    if context.store is None:
        return ToolResult.failure("no store available to schedule work")
    context.store.save_task(task)
    return ToolResult.success({"task_id": task.id, "due_at": due, "repeat": repeat},
                              evidence=f"scheduled {intent[:60]!r} in {delay_seconds}s")


def schedule_cancel(context: ToolContext, task_id: str) -> ToolResult:
    if context.store is None:
        return ToolResult.failure("no store available")
    task = context.store.get_task(task_id)
    if task is None:
        return ToolResult.failure(f"no such task: {task_id}")
    task.state = TaskState.CANCELLED
    task.finished_at = time.time()
    context.store.save_task(task)
    return ToolResult.success({"cancelled": task_id}, evidence=f"cancelled {task_id}")


# --------------------------------------------------------------------------- #
# notifications
# --------------------------------------------------------------------------- #
def notify_send(context: ToolContext, message: str, level: str = "info",
                title: str = "JARVIS") -> ToolResult:
    payload = {"title": title, "message": message, "level": level}
    if context.bus is not None:
        context.bus.publish("notification", source="notify", payload=payload)
    if context.store is not None:
        from jarvis.core.models import Memory, new_id

        context.store.save_memory(
            Memory(id=new_id("mem"), kind="notification", content=message,
                   tags=["notification", level])
        )
        context.store.audit("notify", "sent", payload)
    return ToolResult.success(payload, evidence=f"notified: {message[:80]}")


# --------------------------------------------------------------------------- #
# memory
# --------------------------------------------------------------------------- #
def memory_remember(context: ToolContext, content: str, kind: str = "fact",
                    tags: str = "", importance: float = 0.5) -> ToolResult:
    if context.memory is None:
        return ToolResult.failure("memory is not available in this context")
    memory = context.memory.remember(
        content, kind=kind, tags=[t.strip() for t in tags.split(",") if t.strip()],
        goal_id=context.goal_id, importance=importance,
    )
    return ToolResult.success({"memory_id": memory.id}, evidence=f"remembered {content[:60]!r}")


def memory_recall(context: ToolContext, query: str, limit: int = 5) -> ToolResult:
    if context.memory is None:
        return ToolResult.failure("memory is not available in this context")
    hits = context.memory.recall(query, limit=limit, goal_id=context.goal_id)
    return ToolResult.success(
        {"count": len(hits),
         "hits": [{"id": h["memory"].id, "content": h["memory"].content,
                   "kind": h["memory"].kind, "score": h["score"]} for h in hits]},
        evidence=f"recalled {len(hits)} memories for {query[:40]!r}",
    )


# --------------------------------------------------------------------------- #
# goals
# --------------------------------------------------------------------------- #
def goal_create(context: ToolContext, title: str, objective: str, autonomy: str = "standard",
                priority: int = 5, deadline_hours: float = 0.0) -> ToolResult:
    if context.store is None:
        return ToolResult.failure("no store available")
    goal = Goal(
        title=title, objective=objective, state=GoalState.ACTIVE, priority=int(priority),
        autonomy=autonomy, deadline=time.time() + deadline_hours * 3600 if deadline_hours else 0.0,
    )
    context.store.save_goal(goal)
    return ToolResult.success({"goal_id": goal.id}, evidence=f"created goal {title!r}")


def goal_update(context: ToolContext, goal_id: str, state: str = "", note: str = "") -> ToolResult:
    if context.store is None:
        return ToolResult.failure("no store available")
    goal = context.store.get_goal(goal_id)
    if goal is None:
        return ToolResult.failure(f"no such goal: {goal_id}")
    if state:
        goal.state = GoalState(state)
    if note:
        goal.notes.append(note)
    context.store.save_goal(goal)
    return ToolResult.success({"goal_id": goal.id, "state": goal.state.value},
                              evidence=f"goal {goal_id} -> {goal.state.value}")


# --------------------------------------------------------------------------- #
# assistant
# --------------------------------------------------------------------------- #
def assistant_answer(context: ToolContext, question: str = "") -> ToolResult:
    question = question or (context.variables.get("intent") or "")
    memory_block = ""
    if context.memory is not None:
        memory_block = context.memory.context_block(question, goal_id=context.goal_id, limit=5)

    prompt = (
        f"Question: {question}\n"
        + (f"Relevant memory:\n{memory_block}\n" if memory_block else "")
        + "Answer in three sentences or fewer. If you do not know, say so plainly."
    )
    answer = ""
    model = ""
    if context.router is not None:
        try:
            completion = context.router.complete(
                CompletionRequest(prompt=prompt, task_class="summarising", max_tokens=400),
                task_id=context.task_id,
            )
            answer = completion.text.strip()
            model = completion.model
        except Exception as exc:
            answer = f"I could not reach a model ({exc}). " + (memory_block or "")
    else:
        answer = memory_block or "No model is configured, so I have nothing to add."

    return ToolResult.success(
        {"answered": True, "answer": answer, "model": model, "memory_used": bool(memory_block)},
        evidence=f"answer produced ({len(answer)} chars) by {model or 'fallback'}",
    )


def register(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="schedule.create", description="Queue work to run later",
        handler=schedule_create, requires=["schedule.manage"], risk=Risk.WRITE,
        parameters={"intent": "what to do", "delay_seconds": "when", "repeat": "repeat"},
    ))
    registry.register(Tool(
        name="schedule.cancel", description="Cancel scheduled or queued work",
        handler=schedule_cancel, requires=["schedule.manage"], risk=Risk.WRITE,
        parameters={"task_id": "task to cancel"},
    ))
    registry.register(Tool(
        name="notify.send", description="Send a notification to the user",
        handler=notify_send, requires=[], risk=Risk.READ,
        parameters={"message": "text", "level": "info|warning|critical"},
    ))
    registry.register(Tool(
        name="memory.remember", description="Store a durable memory",
        handler=memory_remember, requires=["memory.manage"], risk=Risk.WRITE,
        parameters={"content": "text", "kind": "fact|episode|preference|reflection"},
    ))
    registry.register(Tool(
        name="memory.recall", description="Recall relevant memories",
        handler=memory_recall, requires=[], risk=Risk.READ,
        parameters={"query": "what to recall"},
    ))
    registry.register(Tool(
        name="goal.create", description="Create a durable long-term goal",
        handler=goal_create, requires=["goal.manage"], risk=Risk.WRITE,
        parameters={"title": "short name", "objective": "what done looks like"},
    ))
    registry.register(Tool(
        name="goal.update", description="Change the state of a goal or add a note",
        handler=goal_update, requires=["goal.manage"], risk=Risk.WRITE,
        parameters={"goal_id": "goal", "state": "new state", "note": "note to append"},
    ))
    registry.register(Tool(
        name="assistant.answer", description="Compose an answer from memory and models",
        handler=assistant_answer, requires=[], risk=Risk.READ,
        parameters={"question": "what to answer"},
    ))
