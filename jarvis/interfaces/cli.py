"""Command-line interface to JARVIS.

One assistant, one runtime: every command opens the same persistent store, so a
request made here is visible to the desktop app and the voice interface.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

from jarvis.application import Jarvis
from jarvis.core.models import Trigger
from jarvis.errors import Halted
from jarvis.version import banner


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, default=str))


def _table(rows: List[Dict[str, Any]], columns: Sequence[str]) -> None:
    if not rows:
        print("(nothing)")
        return
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in columns}
    header = "  ".join(c.ljust(widths[c]) for c in columns)
    print(header)
    print("-" * len(header))
    for row in rows:
        print("  ".join(str(row.get(c, ""))[: widths[c]].ljust(widths[c]) for c in columns))


def _short(text: Any, limit: int = 60) -> str:
    text = str(text or "").replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "..."


def _yes_no(value: str) -> bool:
    return value.strip().lower() in ("yes", "y", "true", "1", "approve", "approved")


def _payload(text: Optional[str]) -> Dict[str, Any]:
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise SystemExit(f"--payload must be valid JSON: {exc}")
    return data if isinstance(data, dict) else {"value": data}


def _jarvis(args: argparse.Namespace) -> Jarvis:
    kwargs: Dict[str, Any] = {}
    if getattr(args, "root", None):
        kwargs["root"] = args.root
    if getattr(args, "mode", None):
        os.environ.setdefault("JARVIS_MODE", args.mode)
    if getattr(args, "wait", False):
        kwargs["approval_blocking"] = True
    return Jarvis(**kwargs)


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_status(jarvis: Jarvis, args: argparse.Namespace) -> int:
    status = jarvis.status()
    if args.json:
        _print_json(status)
        return 0
    print(banner())
    print(f"state      : {'HALTED' if status['halted'] else 'running'}   mode: "
          f"{status['config']['mode']}   workspace: {status['config']['workspace']}")
    counts = status["task_counts"] or {"none": 0}
    print("tasks      : " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print("goals      : " + (", ".join(f"{g['title']} ({g['state']})" for g in status["goals"])
                            or "(none)"))
    print("approvals  : " + str(status["pending_approvals"]) + " pending")
    memory = status["memory"]
    print(f"memory     : {memory['total']} items {memory['by_kind']}")
    print("models     : " + ", ".join(
        f"{m['id']}[{m['kind']}]{'*' if m['available'] else ''}" for m in status["models"]))
    print(f"tools      : {len(status['tools'])} registered")
    return 0


def cmd_ask(jarvis: Jarvis, args: argparse.Namespace) -> int:
    criteria = [json.loads(c) for c in (args.check or [])]
    try:
        task = jarvis.request(args.intent, goal_id=args.goal or "", priority=args.priority,
                              run_immediately=not args.no_run, success_criteria=criteria,
                              plan=json.loads(args.plan) if args.plan else None)
    except Halted as exc:
        print(f"refused: {exc}")
        return 2
    view = jarvis.task_view(task.id)
    if args.json:
        _print_json(view)
        return 0
    _print_task(view)
    if view["state"] in ("queued", "running", "verifying", "awaiting_approval"):
        return 0                     # accepted and in progress is not a failure
    return 0 if view["verdict"] == "verified" else 1


def cmd_request(jarvis: Jarvis, args: argparse.Namespace) -> int:
    try:
        trigger = Trigger(args.trigger)
    except ValueError:
        raise SystemExit(f"unknown trigger {args.trigger!r}")
    task = jarvis.request(args.intent, goal_id=args.goal or "", priority=args.priority,
                          run_immediately=False, trigger=trigger)
    print(f"queued {task.id}: {task.intent}")
    return 0


def cmd_tasks(jarvis: Jarvis, args: argparse.Namespace) -> int:
    states = [s.strip() for s in args.state.split(",")] if args.state else None
    rows = jarvis.tasks(states=states, goal_id=args.goal or "", limit=args.limit)
    if args.json:
        _print_json(rows)
        return 0
    _table([{"id": r["id"], "state": r["state"], "verdict": r["verdict"] or "-",
             "phase": r["phase"], "intent": _short(r["intent"], 50)} for r in rows],
           ["id", "state", "verdict", "phase", "intent"])
    return 0


def cmd_task(jarvis: Jarvis, args: argparse.Namespace) -> int:
    view = jarvis.task_view(args.task_id)
    if args.json:
        _print_json(view)
        return 0
    _print_task(view)
    return 0


def cmd_goals(jarvis: Jarvis, args: argparse.Namespace) -> int:
    goals = jarvis.goals()
    if args.json:
        _print_json(goals)
        return 0
    _table([{"id": g["id"], "state": g["state"], "title": _short(g["title"], 40),
             "milestones": f"{len(g['milestones'])}",
             "progress": json.dumps(g.get("progress") or {})} for g in goals],
           ["id", "state", "title", "milestones", "progress"])
    return 0


def cmd_goal(jarvis: Jarvis, args: argparse.Namespace) -> int:
    _print_json(jarvis.goal_report(args.goal_id))
    return 0


def cmd_adopt_goal(jarvis: Jarvis, args: argparse.Namespace) -> int:
    monitors = [json.loads(m) for m in (args.monitor or [])]
    criteria = [json.loads(c) for c in (args.check or [])]
    goal = jarvis.adopt_goal(args.title, args.objective, milestones=args.milestone or [],
                             monitors=monitors, definition_of_done=criteria,
                             autonomy=args.autonomy, priority=args.priority,
                             run_first=not args.no_run)
    print(f"adopted goal {goal.id}: {goal.title} [{goal.state.value}]")
    return 0


def cmd_advance(jarvis: Jarvis, args: argparse.Namespace) -> int:
    _print_json(jarvis.advance(max_tasks=args.max_tasks))
    return 0


def cmd_event(jarvis: Jarvis, args: argparse.Namespace) -> int:
    created = jarvis.event(args.kind, _payload(args.payload), run_immediately=args.run)
    print(f"event {args.kind} -> {len(created)} proactive task(s)")
    for task in created:
        print(f"  {task['id']}: {task['intent']}")
    return 0


def cmd_approvals(jarvis: Jarvis, args: argparse.Namespace) -> int:
    rows = jarvis.approvals()
    if args.json:
        _print_json(rows)
        return 0
    _table([{"id": r["id"], "scope": r["scope"], "risk": r["risk"],
             "action": _short(r["action"], 44)} for r in rows],
           ["id", "scope", "risk", "action"])
    return 0


def cmd_approve(jarvis: Jarvis, args: argparse.Namespace) -> int:
    result = jarvis.approve(args.approval_id, _yes_no(args.decision), note=args.note or "")
    print(f"approval {result['id']} -> {result['state']}")
    outcome = result.get("outcome")
    if outcome:
        print(f"task {outcome['task_id']} -> {outcome['state']} / {outcome['verdict'] or '-'}")
    return 0


def cmd_halt(jarvis: Jarvis, args: argparse.Namespace) -> int:
    jarvis.halt()
    print("emergency stop engaged - all execution is refused until you resume")
    return 0


def cmd_resume(jarvis: Jarvis, args: argparse.Namespace) -> int:
    jarvis.resume()
    resumed = jarvis.executive.resume_interrupted()
    print(f"resumed; {len(resumed)} interrupted task(s) re-queued")
    return 0


def cmd_cancel(jarvis: Jarvis, args: argparse.Namespace) -> int:
    ok = jarvis.cancel(args.task_id)
    print(f"cancelled {args.task_id}" if ok else f"could not cancel {args.task_id}")
    return 0 if ok else 1


def cmd_remember(jarvis: Jarvis, args: argparse.Namespace) -> int:
    memory = jarvis.remember(args.content, kind=args.kind,
                             tags=[t.strip() for t in (args.tags or "").split(",") if t.strip()],
                             importance=args.importance)
    print(f"remembered {memory['id']}")
    return 0


def cmd_recall(jarvis: Jarvis, args: argparse.Namespace) -> int:
    hits = jarvis.recall(args.query, limit=args.limit)
    if args.json:
        _print_json(hits)
        return 0
    for hit in hits:
        print(f"[{hit['score']:.2f}] ({hit['kind']}) {hit['content']}")
    if not hits:
        print("(no relevant memories)")
    return 0


def cmd_memories(jarvis: Jarvis, args: argparse.Namespace) -> int:
    rows = jarvis.memories()
    if args.json:
        _print_json(rows)
        return 0
    _table([{"id": m["id"], "kind": m["kind"], "content": _short(m["content"], 64)}
            for m in rows[: args.limit]], ["id", "kind", "content"])
    return 0


def cmd_forget(jarvis: Jarvis, args: argparse.Namespace) -> int:
    print("forgotten" if jarvis.forget(args.memory_id) else "no such memory")
    return 0


def cmd_review(jarvis: Jarvis, args: argparse.Namespace) -> int:
    _print_json(jarvis.review())
    return 0


def cmd_proposals(jarvis: Jarvis, args: argparse.Namespace) -> int:
    rows = jarvis.proposals()
    if args.json:
        _print_json(rows)
        return 0
    _table([{"id": p["id"], "kind": p["kind"], "state": p["state"],
             "title": _short(p["title"], 56)} for p in rows],
           ["id", "kind", "state", "title"])
    return 0


def cmd_decide_proposal(jarvis: Jarvis, args: argparse.Namespace) -> int:
    result = jarvis.decide_proposal(args.proposal_id, _yes_no(args.decision),
                                    note=args.note or "")
    print(f"proposal {result['id']} -> {result['state']}"
          + (f" ({result['effect']})" if result.get("effect") else ""))
    return 0


def cmd_tools(jarvis: Jarvis, args: argparse.Namespace) -> int:
    rows = jarvis.status()["tools"]
    if args.json:
        _print_json(rows)
        return 0
    _table([{"name": t["name"], "risk": t["risk"], "scopes": ",".join(t["requires"]) or "-",
             "description": _short(t["description"], 48)} for t in rows],
           ["name", "risk", "scopes", "description"])
    return 0


def cmd_models(jarvis: Jarvis, args: argparse.Namespace) -> int:
    _print_json({"models": jarvis.status()["models"], "usage": jarvis.router.usage_summary()})
    return 0


def cmd_audit(jarvis: Jarvis, args: argparse.Namespace) -> int:
    rows = jarvis.store.audit_log(args.limit)
    _table([{"actor": r["actor"], "action": r["action"], "detail": _short(r["detail"], 70)}
            for r in rows], ["actor", "action", "detail"])
    return 0


def cmd_serve(jarvis: Jarvis, args: argparse.Namespace) -> int:
    from jarvis.interfaces.desktop.app import run_server

    return run_server(jarvis, host=args.host, port=args.port, reload=False)


def cmd_chat(jarvis: Jarvis, args: argparse.Namespace) -> int:
    """A simple conversation loop; goals and approvals still apply."""
    print(banner())
    print("Type a request. Commands: /goals /tasks /approvals /halt /resume /quit")
    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in ("/quit", "/exit"):
            break
        if line == "/goals":
            cmd_goals(jarvis, args)
            continue
        if line == "/tasks":
            args.state = ""
            args.goal = ""
            args.limit = 20
            cmd_tasks(jarvis, args)
            continue
        if line == "/approvals":
            cmd_approvals(jarvis, args)
            continue
        if line == "/halt":
            cmd_halt(jarvis, args)
            continue
        if line == "/resume":
            cmd_resume(jarvis, args)
            continue
        try:
            task = jarvis.request(line)
        except Halted as exc:
            print(f"jarvis> refused: {exc}")
            continue
        if task.state.value == "awaiting_approval":
            pending = jarvis.approvals()
            print(f"jarvis> I need approval for: {pending[-1]['action']} "
                  f"(scope {pending[-1]['scope']}, risk {pending[-1]['risk']})")
            answer = input("approve? [y/N] ").strip()
            jarvis.approve(pending[-1]["id"], _yes_no(answer), note="chat")
            task = jarvis.store.get_task(task.id)
        verdict = task.verdict or "unverified"
        print(f"jarvis> {task.state.value} / {verdict}")
        if isinstance(task.result, dict):
            for key in ("answer", "summary"):
                value = task.result.get(key)
                if isinstance(value, str) and value:
                    print(f"        {value[:400]}")
                    break
        if task.error:
            print(f"        blocked: {task.error[:300]}")
    return 0


def _print_task(view: Dict[str, Any]) -> None:
    print(f"task       : {view['id']}")
    print(f"intent     : {view['intent']}")
    print(f"objective  : {view['objective']}")
    print(f"state      : {view['state']} / phase {view['phase']} / verdict "
          f"{view['verdict'] or '-'}")
    print(f"model      : {view['model'] or '-'}   attempts: {view['attempts']}   "
          f"duration: {view['duration']}s")
    if view["plan"]:
        print("plan       :")
        for step in view["plan"]:
            flag = {"succeeded": "ok", "failed": "FAIL", "skipped": "skip"}.get(
                step["state"], step["state"])
            print(f"  [{flag}] {step['tool']} - {step['title']}")
            if step.get("error"):
                print(f"         {step['error'][:160]}")
    verification = view.get("verification")
    if verification:
        print(f"verification: {verification['verdict']}")
        for line in (verification.get("evidence") or "").splitlines():
            print(f"  {line}")
    if view["error"]:
        print(f"error      : [{view['error_kind']}] {view['error'][:300]}")
    if view["decisions"]:
        print("decisions  :")
        for decision in view["decisions"]:
            print(f"  - {decision['kind']}: {decision['rationale'][:120]}")
    if view["checkpoints"]:
        print("checkpoints: " + " -> ".join(p for p, _ in view["checkpoints"]))


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jarvis", description=banner())
    parser.add_argument("--root", help="workspace root JARVIS may act on")
    parser.add_argument("--mode", choices=["readonly", "standard", "autonomous"],
                        help="permission posture")
    parser.add_argument("--wait", action="store_true",
                        help="block for approval decisions instead of parking the task")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, func, help_text: str) -> argparse.ArgumentParser:
        item = sub.add_parser(name, help=help_text)
        item.set_defaults(func=func)
        return item

    add("status", cmd_status, "show system status")
    ask = add("ask", cmd_ask, "make an immediate request and show the verified result")
    ask.add_argument("intent")
    ask.add_argument("--goal", help="attach to a goal")
    ask.add_argument("--priority", type=int, default=5)
    ask.add_argument("--check", action="append", help="success criterion as JSON")
    ask.add_argument("--plan", help="explicit plan as JSON")
    ask.add_argument("--no-run", action="store_true", help="queue without running")
    req = add("request", cmd_request, "queue a request without running it")
    req.add_argument("intent")
    req.add_argument("--goal")
    req.add_argument("--priority", type=int, default=5)
    req.add_argument("--trigger", default="user_request")

    tasks = add("tasks", cmd_tasks, "list tasks")
    tasks.add_argument("--state", default="")
    tasks.add_argument("--goal", default="")
    tasks.add_argument("--limit", type=int, default=25)
    task = add("task", cmd_task, "show one task in full")
    task.add_argument("task_id")

    add("goals", cmd_goals, "list long-term goals")
    goal = add("goal", cmd_goal, "show a goal report")
    goal.add_argument("goal_id")
    adopt = add("adopt-goal", cmd_adopt_goal, "adopt a long-running goal")
    adopt.add_argument("title")
    adopt.add_argument("objective")
    adopt.add_argument("--milestone", action="append")
    adopt.add_argument("--monitor", action="append", help="monitor spec as JSON")
    adopt.add_argument("--check", action="append", help="definition of done as JSON")
    adopt.add_argument("--autonomy", default="standard",
                       choices=["readonly", "standard", "autonomous"])
    adopt.add_argument("--priority", type=int, default=5)
    adopt.add_argument("--no-run", action="store_true")
    advance = add("advance", cmd_advance, "run one supervision pass over all goals")
    advance.add_argument("--max-tasks", type=int, default=None)

    event = add("event", cmd_event, "inject an event for proactive handling")
    event.add_argument("kind")
    event.add_argument("--payload", help="JSON payload")
    event.add_argument("--run", action="store_true")

    add("approvals", cmd_approvals, "list pending approvals")
    approve = add("approve", cmd_approve, "decide a pending approval")
    approve.add_argument("approval_id")
    approve.add_argument("decision", nargs="?", default="yes")
    approve.add_argument("--note", default="")

    add("halt", cmd_halt, "emergency stop")
    add("resume", cmd_resume, "lift the emergency stop")
    cancel = add("cancel", cmd_cancel, "cancel a task")
    cancel.add_argument("task_id")

    remember = add("remember", cmd_remember, "store a memory")
    remember.add_argument("content")
    remember.add_argument("--kind", default="fact")
    remember.add_argument("--tags", default="")
    remember.add_argument("--importance", type=float, default=0.6)
    recall = add("recall", cmd_recall, "recall relevant memories")
    recall.add_argument("query")
    recall.add_argument("--limit", type=int, default=8)
    memories = add("memories", cmd_memories, "list memories")
    memories.add_argument("--limit", type=int, default=25)
    forget = add("forget", cmd_forget, "delete a memory")
    forget.add_argument("memory_id")

    add("review", cmd_review, "self-evaluate finished work and propose improvements")
    add("proposals", cmd_proposals, "list improvement proposals")
    decide = add("decide-proposal", cmd_decide_proposal, "approve or reject a proposal")
    decide.add_argument("proposal_id")
    decide.add_argument("decision", nargs="?", default="yes")
    decide.add_argument("--note", default="")

    add("tools", cmd_tools, "list registered tools")
    add("models", cmd_models, "show model routing and usage")
    audit = add("audit", cmd_audit, "show the audit log")
    audit.add_argument("--limit", type=int, default=25)

    serve = add("serve", cmd_serve, "start the desktop interface")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)

    add("chat", cmd_chat, "interactive conversation loop")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "json"):
        args.json = False
    jarvis = _jarvis(args)
    try:
        return int(args.func(jarvis, args) or 0)
    finally:
        jarvis.close()


if __name__ == "__main__":      # pragma: no cover
    raise SystemExit(main())
