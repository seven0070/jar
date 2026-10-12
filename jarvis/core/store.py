"""SQLite-backed persistence for goals, tasks, events, decisions, checkpoints and audit.

This is the substrate that makes JARVIS durable: nothing important lives only in
process memory. The store is safe for the desktop server thread and the executive
thread, which share one connection guarded by a re-entrant lock.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from typing import Any, Dict, Iterable, List, Optional, Tuple

from jarvis.core.models import (
    ApprovalRequest,
    Decision,
    Event,
    Goal,
    Memory,
    Task,
    Verification,
    Verdict,
    dumps,
    loads,
    now,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS goals (
    id TEXT PRIMARY KEY, title TEXT, objective TEXT, state TEXT, priority INTEGER,
    autonomy TEXT, payload TEXT, created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, goal_id TEXT, intent TEXT, state TEXT, phase TEXT,
    priority INTEGER, attempts INTEGER, verdict TEXT, payload TEXT,
    created_at REAL, updated_at REAL, finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_tasks_goal ON tasks(goal_id);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY, kind TEXT, source TEXT, payload TEXT,
    created_at REAL, consumed INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_consumed ON events(consumed);
CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY, task_id TEXT, kind TEXT, rationale TEXT,
    payload TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, phase TEXT,
    payload TEXT, created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_ckpt_task ON checkpoints(task_id, id);
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY, kind TEXT, content TEXT, tags TEXT, goal_id TEXT,
    importance REAL, created_at REAL, accessed_at REAL, access_count INTEGER
);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY, task_id TEXT, action TEXT, scope TEXT, risk TEXT,
    detail TEXT, state TEXT, requested_at REAL, decided_at REAL, note TEXT
);
CREATE TABLE IF NOT EXISTS verifications (
    id TEXT PRIMARY KEY, task_id TEXT, verdict TEXT, checks TEXT,
    evidence TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT, action TEXT,
    detail TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY, kind TEXT, title TEXT, rationale TEXT, payload TEXT,
    state TEXT, created_at REAL, decided_at REAL
);
CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, name TEXT,
    value REAL, payload TEXT, created_at REAL
);
"""


class Store:
    """Durable operational state."""

    def __init__(self, path: str):
        self.path = path
        if path != ":memory:" and path not in ("", None):
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)

    # -- primitives --------------------------------------------------------- #
    @property
    def closed(self) -> bool:
        return self._closed

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        if self._closed:
            raise RuntimeError("the JARVIS store is closed")
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> List[sqlite3.Row]:
        if self._closed:
            raise RuntimeError("the JARVIS store is closed")
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)).fetchall())

    def one(self, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._conn.close()

    # -- settings ----------------------------------------------------------- #
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return loads(row["value"], default) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, dumps(value)),
        )

    # -- audit -------------------------------------------------------------- #
    def audit(self, actor: str, action: str, detail: Any = "") -> None:
        self.execute(
            "INSERT INTO audit(actor, action, detail, created_at) VALUES(?,?,?,?)",
            (actor, action, dumps(detail) if not isinstance(detail, str) else detail, now()),
        )

    def audit_log(self, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self.query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # -- goals -------------------------------------------------------------- #
    def save_goal(self, goal: Goal) -> None:
        goal.updated_at = now()
        self.execute(
            "INSERT INTO goals(id,title,objective,state,priority,autonomy,payload,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET title=excluded.title, objective=excluded.objective, "
            "state=excluded.state, priority=excluded.priority, autonomy=excluded.autonomy, "
            "payload=excluded.payload, updated_at=excluded.updated_at",
            (
                goal.id, goal.title, goal.objective, goal.state.value, goal.priority,
                goal.autonomy, dumps(goal.to_dict()), goal.created_at, goal.updated_at,
            ),
        )

    def get_goal(self, goal_id: str) -> Optional[Goal]:
        row = self.one("SELECT payload FROM goals WHERE id=?", (goal_id,))
        return Goal.from_dict(loads(row["payload"], {})) if row else None

    def list_goals(self, states: Optional[Iterable[str]] = None) -> List[Goal]:
        if states:
            marks = ",".join("?" * len(list(states)))
            rows = self.query(
                f"SELECT payload FROM goals WHERE state IN ({marks}) ORDER BY priority DESC, created_at",
                list(states),
            )
        else:
            rows = self.query("SELECT payload FROM goals ORDER BY priority DESC, created_at")
        return [Goal.from_dict(loads(r["payload"], {})) for r in rows]

    # -- tasks -------------------------------------------------------------- #
    def save_task(self, task: Task) -> None:
        task.updated_at = now()
        self.execute(
            "INSERT INTO tasks(id,goal_id,intent,state,phase,priority,attempts,verdict,payload,"
            "created_at,updated_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET goal_id=excluded.goal_id, intent=excluded.intent, "
            "state=excluded.state, phase=excluded.phase, priority=excluded.priority, "
            "attempts=excluded.attempts, verdict=excluded.verdict, payload=excluded.payload, "
            "updated_at=excluded.updated_at, finished_at=excluded.finished_at",
            (
                task.id, task.goal_id, task.intent, task.state.value, task.phase.value,
                task.priority, task.attempts, task.verdict, dumps(task.to_dict()),
                task.created_at, task.updated_at, task.finished_at,
            ),
        )

    def get_task(self, task_id: str) -> Optional[Task]:
        row = self.one("SELECT payload FROM tasks WHERE id=?", (task_id,))
        return Task.from_dict(loads(row["payload"], {})) if row else None

    def list_tasks(
        self,
        states: Optional[Iterable[str]] = None,
        goal_id: Optional[str] = None,
        limit: int = 200,
    ) -> List[Task]:
        sql = "SELECT payload FROM tasks WHERE 1=1"
        params: List[Any] = []
        if states:
            states = list(states)
            sql += f" AND state IN ({','.join('?' * len(states))})"
            params.extend(states)
        if goal_id:
            sql += " AND goal_id=?"
            params.append(goal_id)
        sql += " ORDER BY priority DESC, created_at DESC LIMIT ?"
        params.append(limit)
        return [Task.from_dict(loads(r["payload"], {})) for r in self.query(sql, params)]

    def count_tasks(self) -> Dict[str, int]:
        rows = self.query("SELECT state, COUNT(*) AS n FROM tasks GROUP BY state")
        return {r["state"]: r["n"] for r in rows}

    # -- checkpoints -------------------------------------------------------- #
    def checkpoint(self, task: Task) -> None:
        self.execute(
            "INSERT INTO checkpoints(task_id, phase, payload, created_at) VALUES(?,?,?,?)",
            (task.id, task.phase.value, dumps(task.to_dict()), now()),
        )

    def last_checkpoint(self, task_id: str) -> Optional[Task]:
        row = self.one(
            "SELECT payload FROM checkpoints WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,)
        )
        return Task.from_dict(loads(row["payload"], {})) if row else None

    def checkpoint_history(self, task_id: str) -> List[Tuple[str, float]]:
        rows = self.query(
            "SELECT phase, created_at FROM checkpoints WHERE task_id=? ORDER BY id", (task_id,)
        )
        return [(r["phase"], r["created_at"]) for r in rows]

    # -- events ------------------------------------------------------------- #
    def add_event(self, event: Event) -> Event:
        self.execute(
            "INSERT INTO events(id,kind,source,payload,created_at,consumed) VALUES(?,?,?,?,?,?)",
            (event.id, event.kind, event.source, dumps(event.payload), event.created_at,
             1 if event.consumed else 0),
        )
        return event

    def pending_events(self, limit: int = 50) -> List[Event]:
        rows = self.query(
            "SELECT * FROM events WHERE consumed=0 ORDER BY created_at LIMIT ?", (limit,)
        )
        return [
            Event(id=r["id"], kind=r["kind"], source=r["source"],
                  payload=loads(r["payload"], {}) or {}, created_at=r["created_at"],
                  consumed=bool(r["consumed"]))
            for r in rows
        ]

    def mark_event_consumed(self, event_id: str) -> None:
        self.execute("UPDATE events SET consumed=1 WHERE id=?", (event_id,))

    def recent_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self.query("SELECT * FROM events ORDER BY created_at DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = loads(d["payload"], {}) or {}
            out.append(d)
        return out

    # -- decisions ---------------------------------------------------------- #
    def add_decision(self, decision: Decision) -> Decision:
        self.execute(
            "INSERT INTO decisions(id,task_id,kind,rationale,payload,created_at) VALUES(?,?,?,?,?,?)",
            (decision.id, decision.task_id, decision.kind, decision.rationale,
             dumps(decision.payload), decision.created_at),
        )
        return decision

    def decisions_for(self, task_id: str) -> List[Decision]:
        rows = self.query("SELECT * FROM decisions WHERE task_id=? ORDER BY created_at", (task_id,))
        return [
            Decision(id=r["id"], task_id=r["task_id"], kind=r["kind"], rationale=r["rationale"],
                     payload=loads(r["payload"], {}) or {}, created_at=r["created_at"])
            for r in rows
        ]

    # -- memories ----------------------------------------------------------- #
    def save_memory(self, memory: Memory) -> Memory:
        self.execute(
            "INSERT INTO memories(id,kind,content,tags,goal_id,importance,created_at,"
            "accessed_at,access_count) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET content=excluded.content, tags=excluded.tags, "
            "importance=excluded.importance, accessed_at=excluded.accessed_at, "
            "access_count=excluded.access_count",
            (memory.id, memory.kind, memory.content, dumps(memory.tags), memory.goal_id,
             memory.importance, memory.created_at, memory.accessed_at, memory.access_count),
        )
        return memory

    def all_memories(self) -> List[Memory]:
        rows = self.query("SELECT * FROM memories ORDER BY created_at DESC")
        return [self._memory(r) for r in rows]

    def get_memory(self, memory_id: str) -> Optional[Memory]:
        row = self.one("SELECT * FROM memories WHERE id=?", (memory_id,))
        return self._memory(row) if row else None

    def forget(self, memory_id: str) -> bool:
        cur = self.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        return cur.rowcount > 0

    def touch_memory(self, memory_id: str) -> None:
        self.execute(
            "UPDATE memories SET accessed_at=?, access_count=access_count+1 WHERE id=?",
            (now(), memory_id),
        )

    @staticmethod
    def _memory(row: sqlite3.Row) -> Memory:
        return Memory(
            id=row["id"], kind=row["kind"], content=row["content"],
            tags=loads(row["tags"], []) or [], goal_id=row["goal_id"] or "",
            importance=row["importance"], created_at=row["created_at"],
            accessed_at=row["accessed_at"] or 0.0, access_count=row["access_count"] or 0,
        )

    # -- approvals ---------------------------------------------------------- #
    def save_approval(self, request: ApprovalRequest) -> ApprovalRequest:
        self.execute(
            "INSERT INTO approvals(id,task_id,action,scope,risk,detail,state,requested_at,"
            "decided_at,note) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET state=excluded.state, decided_at=excluded.decided_at, "
            "note=excluded.note",
            (request.id, request.task_id, request.action, request.scope, request.risk,
             request.detail, request.state, request.requested_at, request.decided_at, request.note),
        )
        return request

    def get_approval(self, approval_id: str) -> Optional[ApprovalRequest]:
        row = self.one("SELECT * FROM approvals WHERE id=?", (approval_id,))
        return self._approval(row) if row else None

    def pending_approvals(self) -> List[ApprovalRequest]:
        rows = self.query("SELECT * FROM approvals WHERE state='pending' ORDER BY requested_at")
        return [self._approval(r) for r in rows]

    def approvals_for(self, task_id: str) -> List[ApprovalRequest]:
        rows = self.query("SELECT * FROM approvals WHERE task_id=? ORDER BY requested_at", (task_id,))
        return [self._approval(r) for r in rows]

    @staticmethod
    def _approval(row: sqlite3.Row) -> ApprovalRequest:
        return ApprovalRequest(
            id=row["id"], task_id=row["task_id"], action=row["action"], scope=row["scope"],
            risk=row["risk"], detail=row["detail"], state=row["state"],
            requested_at=row["requested_at"], decided_at=row["decided_at"] or 0.0,
            note=row["note"] or "",
        )

    # -- verifications ------------------------------------------------------ #
    def save_verification(self, verification: Verification) -> Verification:
        self.execute(
            "INSERT INTO verifications(id,task_id,verdict,checks,evidence,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (verification.id, verification.task_id, verification.verdict.value,
             dumps([c.to_dict() for c in verification.checks]), verification.evidence,
             verification.created_at),
        )
        return verification

    def verification_for(self, task_id: str) -> Optional[Verification]:
        row = self.one(
            "SELECT * FROM verifications WHERE task_id=? ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        )
        if not row:
            return None
        from jarvis.core.models import CheckResult

        return Verification(
            id=row["id"], task_id=row["task_id"], verdict=Verdict(row["verdict"]),
            checks=[CheckResult(**c) for c in (loads(row["checks"], []) or [])],
            evidence=row["evidence"] or "", created_at=row["created_at"],
        )

    # -- proposals (Evolver) ------------------------------------------------ #
    def add_proposal(self, proposal: Dict[str, Any]) -> Dict[str, Any]:
        self.execute(
            "INSERT INTO proposals(id,kind,title,rationale,payload,state,created_at,decided_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (proposal["id"], proposal["kind"], proposal["title"], proposal["rationale"],
             dumps(proposal.get("payload") or {}), proposal.get("state", "pending"),
             proposal.get("created_at", now()), proposal.get("decided_at", 0.0)),
        )
        return proposal

    def list_proposals(self, state: Optional[str] = None) -> List[Dict[str, Any]]:
        if state:
            rows = self.query("SELECT * FROM proposals WHERE state=? ORDER BY created_at DESC", (state,))
        else:
            rows = self.query("SELECT * FROM proposals ORDER BY created_at DESC")
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = loads(d["payload"], {}) or {}
            out.append(d)
        return out

    def set_proposal_state(self, proposal_id: str, state: str) -> None:
        self.execute(
            "UPDATE proposals SET state=?, decided_at=? WHERE id=?", (state, now(), proposal_id)
        )

    # -- metrics (Evolver) -------------------------------------------------- #
    def add_metric(self, task_id: str, name: str, value: float, payload: Any = None) -> None:
        self.execute(
            "INSERT INTO metrics(task_id,name,value,payload,created_at) VALUES(?,?,?,?,?)",
            (task_id, name, value, dumps(payload or {}), now()),
        )

    def metrics(self, name: Optional[str] = None, limit: int = 500) -> List[Dict[str, Any]]:
        if name:
            rows = self.query(
                "SELECT * FROM metrics WHERE name=? ORDER BY created_at DESC LIMIT ?", (name, limit)
            )
        else:
            rows = self.query("SELECT * FROM metrics ORDER BY created_at DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = loads(d["payload"], {}) or {}
            out.append(d)
        return out
