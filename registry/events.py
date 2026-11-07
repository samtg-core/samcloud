"""Event bus — in-process pub/sub for real-time notifications via SSE.

Events are scoped: subscribers only see events relevant to their auth scope.
Supports: ticket.*, service.*, user.*, health.*
"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Event:
    type: str          # e.g. "ticket.created", "service.activated", "health.reported"
    target: str        # e.g. "test-host/mediafactory", "ticket:18", "user:5"
    data: dict         # event payload
    scope: str = ""    # e.g. "device:test-host" — empty means global
    timestamp: float = field(default_factory=time.time)
    seq: int = 0       # monotonic per-process id — enables Last-Event-ID resume (doc #17)

    def to_sse(self) -> str:
        payload = {
            "type": self.type,
            "target": self.target,
            "data": self.data,
            "timestamp": self.timestamp,
        }
        return f"id: {self.seq}\nevent: {self.type}\ndata: {json.dumps(payload)}\n\n"


class EventBus:
    def __init__(self):
        self._subscribers: list[asyncio.Queue] = []
        self._history: list[Event] = []
        self._max_history = 100
        self._seq = 0

    def emit(self, event_type: str, target: str, data: dict, scope: str = ""):
        """Emit an event to all subscribers."""
        self._seq += 1
        event = Event(type=event_type, target=target, data=data, scope=scope,
                      seq=self._seq)
        self._history.append(event)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

        for queue in self._subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass  # slow consumer, drop event

    def subscribe(self) -> asyncio.Queue:
        """Create a new subscription queue."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue):
        """Remove a subscription."""
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    def since(self, last_seq: int) -> list[Event]:
        """Events after a Last-Event-ID (best effort — bounded by history window;
        clients must still reconcile their durable inbox on reconnect)."""
        return [e for e in self._history if e.seq > last_seq]

    def recent(self, limit: int = 20, scope: Optional[str] = None) -> list[Event]:
        """Get recent events, optionally filtered by scope."""
        events = self._history
        if scope and scope != "*":
            events = [e for e in events if not e.scope or e.scope == scope or scope == "*"]
        return events[-limit:]


# Singleton
bus = EventBus()
