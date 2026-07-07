"""In-process pub/sub used to push live events (new articles, alert hits) to
connected dashboard clients over Server-Sent Events."""
from __future__ import annotations

import asyncio
import json


class Broadcaster:
    def __init__(self):
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish(self, event: dict) -> None:
        data = json.dumps(event, default=str)
        for q in list(self._subscribers):
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                pass  # slow client; drop rather than block ingestion


broadcaster = Broadcaster()
