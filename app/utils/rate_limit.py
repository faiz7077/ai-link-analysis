"""Minimal per-client sliding-window rate limiter (in-memory, single process).

Protects the LLM quota on a public demo. For multi-instance deployments swap
this for a Redis-backed limiter.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import Request


class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> int | None:
        """Record a hit. Returns None if allowed, else seconds until retry."""
        if self.max_requests <= 0:
            return None
        now = time.monotonic()
        q = self._hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.max_requests:
            return max(1, int(self.window - (now - q[0])) + 1)
        q.append(now)
        return None

    def reset(self) -> None:
        self._hits.clear()


def client_ip(request: Request) -> str:
    # Behind Render / most PaaS proxies the real client is the first X-Forwarded-For entry.
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
