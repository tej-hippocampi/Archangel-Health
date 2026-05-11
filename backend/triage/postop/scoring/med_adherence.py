"""
Rolling 7-day medication adherence summary (PRD §7.2).

Computes the four-state response set into a window summary the post-op
re-tier consumes:

  - high:                ≥6 of last 7 days = "YES"
  - low:                 ≤4 of last 7 days = "YES" (Partial / No /
                         non-response collapse to "not Yes")
  - non_response_streak: consecutive trailing days that ended in
                         MISSED_NON_RESPONSE

The day labels are *episode-day integers*, so the window is closed at
`now_episode_day` and we step back `rolling_window_days` (default 7).
This keeps the algorithm DST- and timezone-agnostic — the cron emits
`MISSED_NON_RESPONSE` rows in episode-day terms, and the rolling
window is just integer arithmetic.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from triage.postop.tuning import MED_ADHERENCE_CONFIG
from triage.postop.types import MedAdherenceWindowSummary


_YES = "YES"
_NOT_YES = {"PARTIAL", "NO", "MISSED_NON_RESPONSE", "REPLY_LATER"}
_NON_RESPONSE = "MISSED_NON_RESPONSE"


def compute_rolling_med_adherence(
    *,
    responses: Iterable[dict[str, Any]],
    now_episode_day: int,
    window_days: Optional[int] = None,
    high_min_yes: Optional[int] = None,
    low_max_yes: Optional[int] = None,
    non_response_streak_days: Optional[int] = None,
) -> MedAdherenceWindowSummary:
    """Compute the rolling-window summary (PRD §7.2).

    `responses` is the raw `med_adherence_responses` row list from
    `TeamStore.list_med_adherence_responses(...)`. It is filtered down
    to the window `[now_episode_day - window_days + 1, now_episode_day]`.
    """
    cfg = MED_ADHERENCE_CONFIG
    window = int(window_days if window_days is not None else cfg["rolling_window_days"])
    high_min = int(high_min_yes if high_min_yes is not None else cfg["high_min_yes"])
    low_max = int(low_max_yes if low_max_yes is not None else cfg["low_max_yes"])
    streak_min = int(non_response_streak_days if non_response_streak_days is not None else cfg["non_response_streak_days"])

    end_day = int(now_episode_day)
    start_day = end_day - window + 1

    by_day: dict[int, str] = {}
    for r in responses or []:
        try:
            d = int(r.get("episode_day"))
        except (TypeError, ValueError):
            continue
        if d < start_day or d > end_day:
            continue
        resp = str(r.get("response") or "").upper()
        # When multiple rows exist for the same day (defensive), prefer
        # the one closer to YES (PARTIAL > NO > MISSED).
        prior = by_day.get(d)
        if prior is None or _rank_response(resp) > _rank_response(prior):
            by_day[d] = resp

    total = sum(1 for d in range(start_day, end_day + 1) if d >= 1)
    yes_count = sum(1 for resp in by_day.values() if resp == _YES)

    high = yes_count >= high_min
    low = (total > 0) and (yes_count <= low_max)

    # Streak — consecutive trailing days (back from end_day) that ended
    # in MISSED_NON_RESPONSE. Days outside the window count toward the
    # streak only if `responses` contains them; days for which we have
    # no row at all do not extend the streak.
    streak = 0
    for d in range(end_day, end_day - 30, -1):  # look back up to 30 days
        if d < 1:
            break
        # Re-scan responses for `d` outside the window.
        if d in by_day:
            resp = by_day[d]
        else:
            resp = next(
                (
                    str(r.get("response") or "").upper()
                    for r in responses or []
                    if int(r.get("episode_day", -1)) == d
                ),
                "",
            )
        if resp == _NON_RESPONSE:
            streak += 1
        else:
            break

    return MedAdherenceWindowSummary(
        yes_count=int(yes_count),
        total_days=int(total),
        high=bool(high),
        low=bool(low and total > 0),
        non_response_streak=int(streak),
    )


def _rank_response(resp: str) -> int:
    """Used to disambiguate same-day duplicate rows (defensive)."""
    return {"YES": 4, "PARTIAL": 3, "NO": 2, "REPLY_LATER": 1, "MISSED_NON_RESPONSE": 0}.get(resp, 0)
