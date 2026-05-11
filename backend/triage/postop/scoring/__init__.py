"""
Scoring helpers for the Post-Op Scoring & Re-Tiering algorithm.

Each submodule encapsulates one signal source from PRD §4–§7 + §10.2:

  - daily_checkin     : 10-item daily symptom check-in scoring (PRD §4.2)
  - day_survey        : D7 / D14 / D30 surveys with sections A–D (PRD §5)
  - med_adherence     : rolling 7-day adherence summary (PRD §7.2)
  - video_engagement  : multi-session view tracking + contributor flags (PRD §6)
  - lost_contact      : 24h-Tier3 / 72h-general silence detector (PRD §10.2)
"""

from triage.postop.scoring.daily_checkin import score_daily_checkin
from triage.postop.scoring.day_survey import score_day_survey
from triage.postop.scoring.med_adherence import compute_rolling_med_adherence
from triage.postop.scoring.video_engagement import (
    count_postop_video_sessions,
    determine_video_flags,
    last_postop_video_session_at,
)
from triage.postop.scoring.lost_contact import lost_contact_status

__all__ = [
    "score_daily_checkin",
    "score_day_survey",
    "compute_rolling_med_adherence",
    "count_postop_video_sessions",
    "last_postop_video_session_at",
    "determine_video_flags",
    "lost_contact_status",
]
