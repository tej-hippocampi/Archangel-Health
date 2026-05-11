"""
Per-episode async lock registry for `apply_preop_retier`.

Mirrors `triage.postop.locks` exactly so the two stages have a uniform
`with_episode_lock(episode_id)` interface. Concurrent re-tier calls for
the same episode are serialized so the persisted `preop_retier_events`
rows write deterministically.

Single-process FastAPI deployment uses an `asyncio.Lock` keyed by
`episode_id` (== `patient_id` in the v1 single-episode-per-patient
model). A future multi-process deployment would swap this for a
Postgres advisory lock with the same `with_episode_lock` interface.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager


_LOCKS: dict[str, asyncio.Lock] = {}
_REGISTRY_LOCK = asyncio.Lock()


async def _get_lock(episode_id: str) -> asyncio.Lock:
    async with _REGISTRY_LOCK:
        lock = _LOCKS.get(episode_id)
        if lock is None:
            lock = asyncio.Lock()
            _LOCKS[episode_id] = lock
        return lock


@asynccontextmanager
async def with_episode_lock(episode_id: str):
    """Async context manager — serializes re-tier calls per episode."""
    lock = await _get_lock(episode_id)
    async with lock:
        yield


def reset_locks_for_test() -> None:
    """Clear the registry between tests so test isolation is preserved."""
    _LOCKS.clear()
