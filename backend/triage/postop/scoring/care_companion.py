"""
Care Companion post-op risk signals (Triage Suite Pass 3 §3).

Reads three TeamStore surfaces:

  1. `event_logs` rows of type `avatar_chat`        → session count
  2. `event_logs` rows of type
     `care_companion_semantic_escalation`           → tier-2/3 verdicts
  3. `escalations` rows whose `trigger_type` starts
     with `chat:semantic`                           → resolved-state

Resolution semantics (per the user's chosen path — no per-conversation
ID linkage): a semantic escalation is "unresolved" whenever the patient
has at least one open `escalations` row with `trigger_type LIKE
'chat:semantic%'`. Closing that single row clears the contributor.

All functions are pure read-side; the heavy lifting lives in
`triage.postop.apply._gather_state` which composes these into the
`PostOpReTierInput` flags consumed by the algorithm.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def count_chat_sessions_total(team_store: Any, patient_id: str) -> int:
    """Total number of `avatar_chat` event_logs rows for the patient.

    A session here = one chat-handler invocation, which is the same
    grain the patient-facing chat surface logs today.
    """
    try:
        events = team_store.get_events(patient_id) or []
    except Exception:
        return 0
    return sum(1 for e in events if e.get("event_type") == "avatar_chat")


def count_chat_sessions_last_7d(
    team_store: Any,
    patient_id: str,
    *,
    now: Optional[datetime] = None,
) -> int:
    """Sliding 7-day count of `avatar_chat` event_logs rows."""
    now = now or datetime.utcnow()
    cutoff = now - timedelta(days=7)
    try:
        events = team_store.get_events(patient_id) or []
    except Exception:
        return 0
    n = 0
    for e in events:
        if e.get("event_type") != "avatar_chat":
            continue
        occ = _parse_iso(e.get("occurred_at"))
        if occ is None:
            continue
        if occ >= cutoff:
            n += 1
    return n


def latest_semantic_escalation(
    team_store: Any,
    patient_id: str,
    *,
    since: Optional[datetime] = None,
) -> Optional[dict]:
    """Return the most recent `care_companion_semantic_escalation` event,
    or None if there isn't one (optionally on or after `since`).

    Output shape mirrors the persisted row:
        {
          "tier": 2 | 3,
          "reason": str,
          "trigger_type": str,
          "occurred_at": iso-str,
          "message_excerpt": str,
        }
    """
    try:
        events = team_store.get_events(patient_id) or []
    except Exception:
        return None
    candidates = [
        e for e in events
        if e.get("event_type") == "care_companion_semantic_escalation"
    ]
    if since is not None:
        candidates = [
            e for e in candidates
            if (occ := _parse_iso(e.get("occurred_at"))) is not None and occ >= since
        ]
    if not candidates:
        return None
    candidates.sort(key=lambda e: e.get("occurred_at") or "", reverse=True)
    top = candidates[0]
    payload = top.get("payload") or {}
    return {
        "tier": int(payload.get("tier") or 0),
        "reason": payload.get("reason", ""),
        "trigger_type": payload.get("trigger_type", ""),
        "occurred_at": top.get("occurred_at"),
        "message_excerpt": payload.get("message_excerpt", ""),
    }


def has_open_chat_semantic_escalation(team_store: Any, patient_id: str) -> bool:
    """True if the patient has at least one unresolved `escalations` row
    whose `trigger_type` starts with `chat:semantic`.

    Uses `list_escalations()` and filters in-process (Pass 3 deliberate
    simplification — no per-conversation linkage). This is fine at our
    scale because the escalations table is small per patient.
    """
    try:
        rows = team_store.list_escalations() or []
    except Exception:
        return False
    for r in rows:
        if r.get("patient_id") != patient_id:
            continue
        tt = str(r.get("trigger_type") or "")
        if not tt.startswith("chat:semantic"):
            continue
        if bool(r.get("resolved")):
            continue
        return True
    return False
