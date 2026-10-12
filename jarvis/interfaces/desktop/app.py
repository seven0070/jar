"""Desktop interface: a local HTTP service plus a monitoring UI.

On Windows this is what the packaged application runs - the same process hosts the
executive runtime, the scheduler loop and this API. Every endpoint is a thin window
onto the one :class:`~jarvis.application.Jarvis` instance, so what the UI shows is
what the runtime is doing: live phases, approval requests, verification evidence,
memory and the emergency stop.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional

from jarvis.application import Jarvis
from jarvis.core.models import Event, Trigger
from jarvis.version import __version__, banner

try:  # the desktop extra is optional; the CLI and runtime work without it
    from fastapi import Body, FastAPI, HTTPException
    from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
    from fastapi.staticfiles import StaticFiles
except ImportError:  # pragma: no cover
    FastAPI = None

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def create_app(jarvis: Optional[Jarvis] = None, scheduler_interval: float = 15.0):
    """Build the FastAPI application around a Jarvis runtime."""
    if FastAPI is None:  # pragma: no cover
        raise RuntimeError("the desktop interface needs the 'desktop' extra: "
                           "pip install jarvis[desktop]")
    jarvis = jarvis or Jarvis()
    cycle_lock = threading.Lock()
    queues: List[asyncio.Queue] = []
    loop_ref: Dict[str, Any] = {"loop": None}

    # -- live event stream -------------------------------------------------- #
    def on_event(event: Event) -> None:
        loop = loop_ref.get("loop")
        if loop is None:
            return
        payload = {"kind": event.kind, "source": event.source, "payload": event.payload,
                   "at": event.created_at}
        for queue in list(queues):
            try:
                loop.call_soon_threadsafe(_offer, queue, payload)
            except RuntimeError:  # loop closed while a worker was publishing
                pass

    def _offer(queue: asyncio.Queue, payload: Dict[str, Any]) -> None:
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            pass

    # -- background work ---------------------------------------------------- #
    async def _scheduler_loop() -> None:
        """The proactive heartbeat: due work, monitors and periodic self-review."""
        ticks = 0
        while True:
            await asyncio.sleep(scheduler_interval)
            try:
                await asyncio.to_thread(_tick)
            except Exception as exc:  # keep the heartbeat alive
                if not jarvis.store.closed:
                    jarvis.store.audit("desktop", "scheduler_error", str(exc)[:300])
                continue
            ticks += 1
            if ticks % 8 == 0:
                try:
                    await asyncio.to_thread(jarvis.review)
                except Exception as exc:
                    if not jarvis.store.closed:
                        jarvis.store.audit("desktop", "review_error", str(exc)[:300])

    def _tick() -> None:
        with cycle_lock:
            jarvis.tick()

    def _run(callspec) -> None:
        """Run a cycle off the request thread so the UI never blocks."""
        def work() -> None:
            with cycle_lock:
                try:
                    callspec()
                except Exception as exc:
                    if not jarvis.store.closed:
                        jarvis.store.audit("desktop", "background_error", str(exc)[:300])
        threading.Thread(target=work, daemon=True).start()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        loop_ref["loop"] = asyncio.get_running_loop()
        heartbeat = (asyncio.create_task(_scheduler_loop())
                     if scheduler_interval > 0 else None)
        try:
            yield
        finally:
            if heartbeat is not None:
                heartbeat.cancel()

    app = FastAPI(title="JARVIS", version=__version__, lifespan=lifespan)
    jarvis.bus.subscribe_all(on_event)

    # -- read endpoints ----------------------------------------------------- #
    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        with open(os.path.join(STATIC_DIR, "index.html"), "r", encoding="utf-8") as handle:
            return handle.read()

    @app.get("/api/status")
    async def status() -> Dict[str, Any]:
        return await asyncio.to_thread(jarvis.status)

    @app.get("/api/tasks")
    async def tasks(state: str = "", goal_id: str = "", limit: int = 50) -> List[Dict[str, Any]]:
        states = [s.strip() for s in state.split(",") if s.strip()] or None
        return await asyncio.to_thread(jarvis.tasks, states, goal_id, limit)

    @app.get("/api/tasks/{task_id}")
    async def task(task_id: str) -> Dict[str, Any]:
        try:
            return await asyncio.to_thread(jarvis.task_view, task_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown task")

    @app.get("/api/goals")
    async def goals() -> List[Dict[str, Any]]:
        return await asyncio.to_thread(jarvis.goals)

    @app.get("/api/goals/{goal_id}")
    async def goal(goal_id: str) -> Dict[str, Any]:
        try:
            return await asyncio.to_thread(jarvis.goal_report, goal_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown goal")

    @app.get("/api/approvals")
    async def approvals() -> List[Dict[str, Any]]:
        return await asyncio.to_thread(jarvis.approvals)

    @app.get("/api/memory")
    async def memory(query: str = "", limit: int = 25) -> List[Dict[str, Any]]:
        if query:
            return await asyncio.to_thread(jarvis.recall, query, limit)
        return await asyncio.to_thread(lambda: jarvis.memories()[:limit])

    @app.get("/api/proposals")
    async def proposals() -> List[Dict[str, Any]]:
        return await asyncio.to_thread(jarvis.proposals)

    @app.get("/api/events")
    async def events(limit: int = 50) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(jarvis.store.recent_events, limit)

    @app.get("/api/stream")
    async def stream() -> StreamingResponse:
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        queues.append(queue)

        async def generate() -> AsyncIterator[str]:
            try:
                yield f"data: {json.dumps({'kind': 'connected'})}\n\n"
                while True:
                    item = await queue.get()
                    yield f"data: {json.dumps(item, default=str)}\n\n"
            finally:
                if queue in queues:
                    queues.remove(queue)

        return StreamingResponse(generate(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # -- write endpoints ---------------------------------------------------- #
    @app.post("/api/request")
    async def request(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        intent = str(body.get("intent", "")).strip()
        if not intent:
            raise HTTPException(status_code=400, detail="intent is required")
        run = bool(body.get("run", True))
        try:
            trigger = Trigger(str(body.get("trigger", "user_request")))
        except ValueError:
            raise HTTPException(status_code=400, detail="unknown trigger")
        task = await asyncio.to_thread(
            jarvis.request, intent,
            goal_id=str(body.get("goal_id", "") or ""),
            priority=int(body.get("priority", 5)),
            run_immediately=run,
            plan=body.get("plan"),
            success_criteria=body.get("success_criteria"),
            trigger=trigger,
        )
        return await asyncio.to_thread(jarvis.task_view, task.id)

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel(task_id: str) -> Dict[str, Any]:
        ok = await asyncio.to_thread(jarvis.cancel, task_id)
        return {"cancelled": ok, "task_id": task_id}

    @app.post("/api/tasks/{task_id}/resume")
    async def resume_task(task_id: str) -> Dict[str, Any]:
        outcome = await asyncio.to_thread(jarvis.executive.resume_task, task_id)
        return outcome.to_dict()

    @app.post("/api/goals")
    async def create_goal(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        title = str(body.get("title", "")).strip()
        objective = str(body.get("objective", "")).strip()
        if not title or not objective:
            raise HTTPException(status_code=400, detail="title and objective are required")
        goal = await asyncio.to_thread(
            jarvis.adopt_goal, title, objective,
            definition_of_done=body.get("definition_of_done") or [],
            milestones=body.get("milestones") or [],
            monitors=body.get("monitors") or [],
            autonomy=str(body.get("autonomy", "standard")),
            priority=int(body.get("priority", 5)),
            run_first=bool(body.get("run_first", False)),
        )
        _run(lambda: jarvis.advance(max_tasks=int(body.get("max_tasks", 1))))
        return await asyncio.to_thread(jarvis.goal_report, goal.id)

    @app.post("/api/goals/{goal_id}/cancel")
    async def cancel_goal(goal_id: str) -> Dict[str, Any]:
        goal = await asyncio.to_thread(jarvis.cancel_goal, goal_id)
        return {"goal_id": goal.id, "state": goal.state.value}

    @app.post("/api/goals/advance")
    async def advance(body: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        _run(lambda: jarvis.advance(max_tasks=body.get("max_tasks")))
        return {"started": True}

    @app.post("/api/approvals/{approval_id}/decide")
    async def decide(approval_id: str, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        approve = bool(body.get("approve", True))
        return await asyncio.to_thread(jarvis.approve, approval_id, approve,
                                       str(body.get("note", "") or ""))

    @app.post("/api/halt")
    async def halt() -> Dict[str, Any]:
        await asyncio.to_thread(jarvis.halt)
        return {"halted": True}

    @app.post("/api/resume")
    async def resume() -> Dict[str, Any]:
        await asyncio.to_thread(jarvis.resume)
        resumed = await asyncio.to_thread(jarvis.executive.resume_interrupted)
        return {"halted": False, "resumed": resumed}

    @app.post("/api/mode")
    async def set_mode(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        mode = str(body.get("mode", "standard"))
        try:
            await asyncio.to_thread(jarvis.set_mode, mode)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"mode": mode}

    @app.post("/api/memory")
    async def remember(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        content = str(body.get("content", "")).strip()
        if not content:
            raise HTTPException(status_code=400, detail="content is required")
        return await asyncio.to_thread(jarvis.remember, content,
                                       str(body.get("kind", "fact")),
                                       body.get("tags") or [],
                                       float(body.get("importance", 0.6)))

    @app.delete("/api/memory/{memory_id}")
    async def forget(memory_id: str) -> Dict[str, Any]:
        return {"forgotten": await asyncio.to_thread(jarvis.forget, memory_id)}

    @app.post("/api/events")
    async def inject_event(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        kind = str(body.get("kind", "")).strip()
        if not kind:
            raise HTTPException(status_code=400, detail="kind is required")
        created = await asyncio.to_thread(jarvis.event, kind, body.get("payload") or {})
        if created:
            _run(lambda: jarvis.executive.run())
        return {"created": created}

    @app.post("/api/review")
    async def review() -> Dict[str, Any]:
        return await asyncio.to_thread(jarvis.review)

    @app.post("/api/proposals/{proposal_id}/decide")
    async def decide_proposal(proposal_id: str, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        try:
            return await asyncio.to_thread(jarvis.decide_proposal, proposal_id,
                                           bool(body.get("approve", True)),
                                           str(body.get("note", "") or "desktop"))
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown proposal")

    @app.get("/api/health")
    async def health() -> Dict[str, Any]:
        return {"ok": True, "version": __version__, "halted": jarvis.policy.halted,
                "banner": banner()}

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.state.jarvis = jarvis
    return app


def run_server(jarvis: Optional[Jarvis] = None, host: Optional[str] = None,
               port: Optional[int] = None, reload: bool = False,
               scheduler_interval: float = 15.0) -> int:
    """Start the desktop interface."""
    import uvicorn

    jarvis = jarvis or Jarvis()
    host = host or jarvis.config.desktop_host
    port = int(port or jarvis.config.desktop_port)
    app = create_app(jarvis, scheduler_interval=scheduler_interval)
    print(f"{banner()}\ndesktop interface on http://{host}:{port}  workspace="
          f"{jarvis.root}")
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


def main() -> int:  # pragma: no cover - entry point for `jarvis-desktop`
    import argparse

    parser = argparse.ArgumentParser(description=banner())
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--no-scheduler", action="store_true")
    args = parser.parse_args()
    return run_server(host=args.host, port=args.port,
                      scheduler_interval=0.0 if args.no_scheduler else 15.0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
