"""
Per-patient async lock registry for `apply_postop_retier`.

PRD §17.15 / README §7.4 require that concurrent re-tier calls for the
same episode be serialized so two `PostOpReTierEvent` rows write
deterministically. The PRD's Postgres advisory-lock pattern would
serve in a multi-process deployment; in this single-process FastAPI
app an `asyncio.Lock` keyed by `patient_id` is sufficient and avoids
SQLite write contention.

The registry is intentionally process-local — for the in-memory CareGuide
demo this matches the lifecycle of `_patient_store`. A future
multi-process deployment would swap this for a Redis-or-Postgres
advisory lock with the same `with_patient_lock(patient_id)` interface.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager


_LOCKS: dict[str, asyncio.Lock] = {}
_REGISTRY_LOCK = asyncio.Lock()


async def _get_lock(patient_id: str) -> asyncio.Lock:
    async with _REGISTRY_LOCK:
        lock = _LOCKS.get(patient_id)
        if lock is None:
            lock = asyncio.Lock()
            _LOCKS[patient_id] = lock
        return lock


@asynccontextmanager
async def with_patient_lock(patient_id: str):
    """Async context manager — serializes re-tier calls per patient."""
    lock = await _get_lock(patient_id)
    async with lock:
        yield


def reset_locks_for_test() -> None:
    """Clear the registry between tests so test isolation is preserved."""
    _LOCKS.clear()
