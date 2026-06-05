"""Lightweight per-endpoint rate limiting (PRD-2).

A self-contained sliding-window limiter keyed on client IP, exposed as a FastAPI
dependency factory so brute-force-sensitive routes (code entry, login, OTP) can
opt in without changing their signatures:

    @app.post("/api/auth/login", dependencies=[Depends(rate_limiter("auth_login", 10, 60))])

Global / volumetric rate limiting and DDoS protection are intentionally left to
the edge (Cloudflare is already in the deployment path) — this module only adds
application-layer brute-force throttles on the sensitive surfaces.
"""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Tuple

from fastapi import HTTPException, Request

_BUCKETS: Dict[str, Deque[float]] = defaultdict(deque)
_LOCK = threading.Lock()


def is_enabled() -> bool:
    return os.getenv("RATE_LIMIT_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")


def reset() -> None:
    """Clear all buckets (used by tests)."""
    with _LOCK:
        _BUCKETS.clear()


def client_ip(request: Request) -> str:
    """Resolve the caller IP, honoring the first X-Forwarded-For hop when behind
    a TLS-terminating proxy (Railway/Render)."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


def check(bucket_key: str, max_requests: int, window_sec: int) -> Tuple[bool, int]:
    """Return (allowed, retry_after_sec). Records the attempt only when allowed."""
    now = time.time()
    cutoff = now - window_sec
    with _LOCK:
        bucket = _BUCKETS[bucket_key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= max_requests:
            retry_after = int(bucket[0] + window_sec - now) + 1
            return False, max(retry_after, 1)
        bucket.append(now)
    return True, 0


def rate_limiter(scope: str, max_requests: int, window_sec: int = 60):
    """Build a FastAPI dependency that throttles `scope` to `max_requests` per
    `window_sec` per client IP. Raises 429 with Retry-After when exceeded."""

    async def _dependency(request: Request) -> None:
        if not is_enabled():
            return
        key = f"{scope}:{client_ip(request)}"
        allowed, retry_after = check(key, max_requests, window_sec)
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Please slow down and try again shortly.",
                headers={"Retry-After": str(retry_after)},
            )

    return _dependency
