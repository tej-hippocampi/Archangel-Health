"""
Conservative-default overdue watcher (PRD §7.4).

Single iteration of the cron — broken out from `main.py` so the
behavior is testable in isolation.

Each run:
    1. Asks the store for forms whose `or_ended_at` is older than 24h
       and which are not LOCKED *and* have not yet been flagged with
       a conservative-default timestamp.
    2. For each such form, atomically sets the
       `conservative_default_applied_at` flag (CAS — idempotent under
       concurrent invocations) and, on success, drives one
       `apply_intraop_reassessment` cycle with `is_conservative_default=True`.

Late lock by the surgeon after the cron has fired is a no-op for the
cron — the lock path simply runs another reassessment cycle, and
`resolve_final_tier` keeps the higher tier (PRD edge case 6).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from triage.intraop.apply import apply_intraop_reassessment
from triage.intraop.tuning import CONSERVATIVE_DEFAULT


log = logging.getLogger("triage.intraop.overdue_watcher")


def run_overdue_pass(
    *,
    patient_store: dict[str, dict],
    team_store,
    now: Optional[datetime] = None,
    threshold_hours: int = CONSERVATIVE_DEFAULT["threshold_hours_after_or_end"],
) -> list[dict[str, Any]]:
    """Execute one cron pass; returns the list of applied reassessments."""
    now = now or datetime.utcnow().replace(microsecond=0)
    overdue = team_store.list_intraop_overdue_forms(
        now_iso=now.isoformat(),
        threshold_hours=threshold_hours,
    )
    applied: list[dict[str, Any]] = []
    for form in overdue:
        patient_id = form["patient_id"]
        # CAS — only one process actually applies the conservative default.
        if not team_store.mark_intraop_conservative_default_applied(patient_id=patient_id):
            continue
        if patient_id not in patient_store:
            log.warning("[INTRAOP_CRON] overdue patient %s missing from store; skipping", patient_id)
            continue
        try:
            ev = apply_intraop_reassessment(
                patient_id=patient_id,
                patient_store=patient_store,
                team_store=team_store,
                triggered_by="SYSTEM:CONSERVATIVE_DEFAULT",
                is_conservative_default=True,
            )
            applied.append({
                "patient_id":  patient_id,
                "final_tier":  ev.final_tier,
                "form_id":     form.get("id"),
                "triggered_at": ev.triggered_at,
            })
            log.info(
                "[INTRAOP_CRON] applied conservative default for %s → %s",
                patient_id, ev.final_tier,
            )
        except Exception:
            log.exception("[INTRAOP_CRON] conservative default failed for %s", patient_id)
    return applied
