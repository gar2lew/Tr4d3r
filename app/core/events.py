"""Lightweight in-process event bus for SSE."""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Any, AsyncIterator, Deque, Dict


class EventBus:
    def __init__(self, history: int = 200) -> None:
        self._subscribers: list[asyncio.Queue] = []
        self._history: Deque[Dict[str, Any]] = deque(maxlen=history)
        self._lock = asyncio.Lock()

    async def publish(self, event_type: str, data: Any) -> None:
        ev = {"type": event_type, "ts": time.time(), "data": data}
        async with self._lock:
            self._history.append(ev)
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass

    async def subscribe(self) -> AsyncIterator[str]:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        async with self._lock:
            self._subscribers.append(q)
            history_snapshot = list(self._history)
        try:
            # Send recent history first
            for ev in history_snapshot[-50:]:
                yield _format_sse(ev)
            yield _format_sse({"type": "ready", "ts": time.time(), "data": {}})
            while True:
                ev = await q.get()
                yield _format_sse(ev)
        finally:
            async with self._lock:
                if q in self._subscribers:
                    self._subscribers.remove(q)


def _format_sse(ev: Dict[str, Any]) -> str:
    return f"event: {ev['type']}\ndata: {json.dumps(ev, default=str)}\n\n"


bus = EventBus()
