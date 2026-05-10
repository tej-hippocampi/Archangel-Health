"""In-memory stores + audit log + rate limiter for the eligibility module.

Mirrors the existing ``_patient_store`` pattern in ``main.py``. Not persisted —
data is reset on server restart. Intentional per PRD §13 (out of scope: DB
migration).
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from typing import Any, Dict, List, Optional

# ─── Module-level stores ────────────────────────────────────────────────────
ELIGIBILITY_CHECKS: Dict[str, Dict[str, Any]] = {}
ELIGIBILITY_DOCS: Dict[str, Dict[str, Any]] = {}
BATCHES: Dict[str, Dict[str, Any]] = {}
AUDIT_LOG: List[Dict[str, Any]] = []
AUDIT_LOG_MAX = 10_000  # FIFO trim once we hit the cap; older entries fall off

# ─── Rate limiting (PRD §10.2: 30/hour/coordinator) ─────────────────────────
_RATE_LIMIT_WINDOW_SEC = 3600
_RATE_LIMIT_MAX = 30
_RATE_BUCKETS: Dict[str, deque] = defaultdict(deque)
_RATE_LOCK = threading.Lock()


def _utc_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


# ─── Document records ───────────────────────────────────────────────────────
def save_doc(doc_id: str, record: Dict[str, Any]) -> None:
    ELIGIBILITY_DOCS[doc_id] = record


def get_doc(doc_id: str) -> Optional[Dict[str, Any]]:
    return ELIGIBILITY_DOCS.get(doc_id)


def delete_doc(doc_id: str) -> Optional[Dict[str, Any]]:
    return ELIGIBILITY_DOCS.pop(doc_id, None)


def list_docs_for_patient(patient_id: str) -> List[Dict[str, Any]]:
    return [d for d in ELIGIBILITY_DOCS.values() if d.get("patient_id") == patient_id]


# ─── Eligibility check records ──────────────────────────────────────────────
def save_check(check_id: str, record: Dict[str, Any]) -> None:
    ELIGIBILITY_CHECKS[check_id] = record


def update_check(check_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    rec = ELIGIBILITY_CHECKS.get(check_id)
    if not rec:
        return None
    rec.update(patch)
    rec["updated_at"] = _utc_iso()
    return rec


def get_check(check_id: str) -> Optional[Dict[str, Any]]:
    return ELIGIBILITY_CHECKS.get(check_id)


# ─── Batches ────────────────────────────────────────────────────────────────
def save_batch(batch_id: str, record: Dict[str, Any]) -> None:
    BATCHES[batch_id] = record


def get_batch(batch_id: str) -> Optional[Dict[str, Any]]:
    return BATCHES.get(batch_id)


def update_batch(batch_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    rec = BATCHES.get(batch_id)
    if not rec:
        return None
    rec.update(patch)
    rec["updated_at"] = _utc_iso()
    return rec


# ─── Audit log (PRD §10.1) ──────────────────────────────────────────────────
def append_audit(
    *,
    action: str,
    actor: str,
    patient_id: Optional[str] = None,
    check_id: Optional[str] = None,
    before: Any = None,
    after: Any = None,
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    AUDIT_LOG.append(
        {
            "ts": _utc_iso(),
            "action": action,
            "actor": actor,
            "patient_id": patient_id,
            "check_id": check_id,
            "before": before,
            "after": after,
            "meta": meta or {},
        }
    )
    overflow = len(AUDIT_LOG) - AUDIT_LOG_MAX
    if overflow > 0:
        del AUDIT_LOG[:overflow]


def list_audit(*, limit: int = 500) -> List[Dict[str, Any]]:
    # newest first
    return list(reversed(AUDIT_LOG[-limit:]))


# ─── Rate limit helper ──────────────────────────────────────────────────────
def rate_limit_check(actor_id: str) -> bool:
    """Return True if the actor is within budget; False if limit exceeded.

    Also records the attempt (only counts on True). Uses a simple sliding
    window over ``_RATE_LIMIT_WINDOW_SEC``.
    """
    if not actor_id:
        return True  # anonymous/demo — don't block
    now = time.time()
    cutoff = now - _RATE_LIMIT_WINDOW_SEC
    with _RATE_LOCK:
        bucket = _RATE_BUCKETS[actor_id]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= _RATE_LIMIT_MAX:
            return False
        bucket.append(now)
    return True


# ─── Queue helpers for SSE (Phase 2) ────────────────────────────────────────
def new_check_queue() -> asyncio.Queue:
    return asyncio.Queue(maxsize=128)


def ring_buffer() -> deque:
    """Ring buffer for SSE replay on reconnect (PRD §11.13).

    Sized for large group-batch upload events (50 patients × ~5 events each
    + parent batch events). Keeping it ≥500 prevents the SSE replay from
    silently dropping early ``patient_created`` events when a doctor
    reconnects mid-batch.
    """
    return deque(maxlen=512)
