"""Event bus: the reactive half of JARVIS.

Events are persisted first (so nothing is lost across restarts) and then fanned
out to in-process subscribers - the desktop UI stream, Odysseus monitors and the
executive itself.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List

from jarvis.core.models import Event, new_id, now


class EventBus:
    def __init__(self, store=None):
        self.store = store
        self._subscribers: Dict[str, List[Callable[[Event], None]]] = {}
        self._all: List[Callable[[Event], None]] = []
        self._lock = threading.RLock()

    # -- subscriptions ------------------------------------------------------ #
    def subscribe(self, kind: str, handler: Callable[[Event], None]) -> None:
        with self._lock:
            self._subscribers.setdefault(kind, []).append(handler)

    def subscribe_all(self, handler: Callable[[Event], None]) -> None:
        with self._lock:
            self._all.append(handler)

    # -- publishing --------------------------------------------------------- #
    def publish(self, kind: str, source: str = "system", payload: Any = None) -> Event:
        event = Event(id=new_id("evt"), kind=kind, source=source,
                      payload=dict(payload or {}), created_at=now())
        if self.store is not None:
            self.store.add_event(event)
        self.dispatch(event)
        return event

    def dispatch(self, event: Event) -> None:
        with self._lock:
            handlers = list(self._subscribers.get(event.kind, [])) + list(self._all)
        for handler in handlers:
            try:
                handler(event)
            except Exception:  # one bad subscriber must not break the bus
                pass
        if self.store is not None:
            self.store.mark_event_consumed(event.id)

    def drain_pending(self) -> List[Event]:
        """Replay events that were persisted but never dispatched (after a crash)."""
        if self.store is None:
            return []
        pending = self.store.pending_events()
        for event in pending:
            self.dispatch(event)
        return pending
