from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any, AsyncIterator, Optional

from app.models import Event
from app.state.store import Store

_HEARTBEAT_INTERVAL_S = 15.0


class EventBus:
    """Per-session event fan-out.

    Every event is appended to the SQLite event log (so it's replayable for
    late SSE subscribers) and broadcast to any live in-process listeners on
    that session. Subscribers receive the assigned event id back so they can
    resume from a cursor on reconnect.
    """

    def __init__(self, store: Store) -> None:
        self.store = store
        self._queues: dict[str, list[asyncio.Queue[Event]]] = defaultdict(list)

    async def emit(self, session_id: str, type_: str, payload: dict[str, Any]) -> int:
        event = Event(session_id=session_id, type=type_, payload=payload)
        event.id = await self.store.append_event(event)
        for q in list(self._queues.get(session_id, [])):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass
        return event.id

    def subscribe(self, session_id: str) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=1024)
        self._queues[session_id].append(q)
        return q

    def unsubscribe(self, session_id: str, q: asyncio.Queue[Event]) -> None:
        try:
            self._queues[session_id].remove(q)
        except ValueError:
            pass


_bus: Optional[EventBus] = None


def init_bus(store: Store) -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus(store)
    return _bus


def get_bus() -> EventBus:
    assert _bus is not None, "EventBus not initialised"
    return _bus


def encode_sse(event: Event) -> str:
    """Encode one Event as an SSE frame.

    Frame fields:
      id    — monotonic per-session id (cursor)
      event — event type
      data  — JSON-encoded {ts, payload}
    """
    body = json.dumps({"ts": event.ts, "payload": event.payload})
    return f"id: {event.id}\nevent: {event.type}\ndata: {body}\n\n"


async def stream_session_events(
    session_id: str,
    cursor: int = 0,
) -> AsyncIterator[str]:
    """Yield SSE frames for one session.

    1. Replay events with id > cursor from the persistent log.
    2. Tail live events from the in-memory queue, deduping by id.
    3. Inject heartbeats every 15s when idle so proxies don't time out.
    """
    bus = get_bus()
    q = bus.subscribe(session_id)
    seen: set[int] = set()
    try:
        replay = await bus.store.events_after(session_id, cursor)
        for ev in replay:
            seen.add(ev.id)
            yield encode_sse(ev)

        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=_HEARTBEAT_INTERVAL_S)
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
                continue

            if ev.id in seen:
                continue
            seen.add(ev.id)
            yield encode_sse(ev)
            if ev.type in {"session.completed", "session.error"}:
                # Drain anything else buffered before exit.
                while not q.empty():
                    extra = q.get_nowait()
                    if extra.id not in seen:
                        seen.add(extra.id)
                        yield encode_sse(extra)
                break
    finally:
        bus.unsubscribe(session_id, q)
