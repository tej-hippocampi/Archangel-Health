"""
Lost-contact detector (PRD §10.2).

Two thresholds:

  - LOST_CONTACT_TIER3   — Tier-3 patient silent for ≥24 h
  - LOST_CONTACT_GENERAL — any patient silent for ≥72 h

"Silent" = no row in any of the post-op signal channels:
daily check-in / med adherence / D-X survey / post-op video / self-flag.

The `last_response_at` lookup is owned by
`TeamStore.last_response_timestamp_across_channels`; this helper is a
thin policy wrapper that converts the timestamp into the boolean flags
the post-op re-tier consumes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from triage.postop.tuning import LOST_CONTACT_CONFIG
from triage.postop.types import LostContactStatus
from triage.types import Tier


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def lost_contact_status(
    *,
    current_tier: Tier,
    last_response_at_iso: Optional[str],
    now: Optional[datetime] = None,
    tier3_hours: Optional[int] = None,
    general_hours: Optional[int] = None,
    discharge_at_iso: Optional[str] = None,
) -> LostContactStatus:
    """Returns both `tier3_24h` and `general_72h` flags.

    The silence anchor is `max(discharge_at, last_response_at)`. If both
    are None we conservatively treat the patient as silent — caller is
    responsible for not invoking lost-contact computation on patients
    with no episode timeline yet.
    """
    now = now or datetime.utcnow()
    cfg = LOST_CONTACT_CONFIG
    t3h = int(tier3_hours if tier3_hours is not None else cfg["tier3_hours"])
    gh = int(general_hours if general_hours is not None else cfg["general_hours"])

    last = _parse_iso(last_response_at_iso)
    discharge = _parse_iso(discharge_at_iso)

    # Anchor: most recent of (discharge, last_response).
    candidates = [c for c in (last, discharge) if c is not None]
    if not candidates:
        return LostContactStatus(
            tier3_24h=current_tier == "TIER_3",
            general_72h=True,
            last_response_at=None,
        )
    anchor = max(candidates)

    silent_hours = (now - anchor).total_seconds() / 3600.0

    return LostContactStatus(
        tier3_24h=(current_tier == "TIER_3" and silent_hours >= t3h),
        general_72h=silent_hours >= gh,
        last_response_at=last_response_at_iso,
    )
