"""Sliding-window rate limits for the endpoints that call the model."""

from __future__ import annotations

import threading
import time
from collections import deque


class RateLimiter:
    """Sliding one-minute windows: at most `per_client` requests per client and `total` across all clients."""

    def __init__(self, per_client: int = 5, total: int = 60, window_s: float = 60.0) -> None:
        self.per_client, self.total, self.window_s = per_client, total, window_s
        self._clients: dict[str, deque[float]] = {}
        self._all: deque[float] = deque()
        self._lock = threading.Lock()  # FastAPI runs sync endpoints in a thread pool

    def allow(self, client: str, now: float | None = None) -> bool:
        """Record and allow the request, or refuse it (recording nothing) if either limit is reached."""
        now = time.monotonic() if now is None else now
        with self._lock:
            mine = self._clients.setdefault(client, deque())
            for times in (mine, self._all):
                while times and times[0] <= now - self.window_s:
                    times.popleft()
            if len(mine) >= self.per_client or len(self._all) >= self.total:
                return False
            mine.append(now)
            self._all.append(now)
            return True
