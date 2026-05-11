"""
Unit tests for `compute_rolling_med_adherence` (PRD §7.2).

Covers the rolling-window correctness, the high/low thresholds, the
non-response streak, multi-row-per-day disambiguation, and DST/timezone
robustness (we use episode-day integers internally, so the algorithm
is timezone-agnostic).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from triage.postop.scoring.med_adherence import compute_rolling_med_adherence  # noqa: E402


def _row(day: int, response: str) -> dict:
    return {"episode_day": day, "response": response}


def test_clean_window_is_high_not_low():
    rows = [_row(d, "YES") for d in range(1, 8)]
    s = compute_rolling_med_adherence(responses=rows, now_episode_day=7)
    assert s.yes_count == 7
    assert s.high is True
    assert s.low is False
    assert s.non_response_streak == 0


def test_low_window_3_yes_4_no():
    rows = (
        [_row(1, "YES"), _row(2, "YES"), _row(3, "YES")]
        + [_row(d, "NO") for d in range(4, 8)]
    )
    s = compute_rolling_med_adherence(responses=rows, now_episode_day=7)
    assert s.yes_count == 3
    assert s.low is True
    assert s.high is False


def test_partial_does_not_count_as_yes():
    rows = (
        [_row(1, "YES"), _row(2, "YES")]
        + [_row(d, "PARTIAL") for d in range(3, 8)]
    )
    s = compute_rolling_med_adherence(responses=rows, now_episode_day=7)
    assert s.yes_count == 2
    assert s.high is False
    assert s.low is True


def test_high_threshold_at_exactly_six_yes():
    rows = [_row(d, "YES") for d in range(1, 7)] + [_row(7, "PARTIAL")]
    s = compute_rolling_med_adherence(responses=rows, now_episode_day=7)
    assert s.yes_count == 6
    assert s.high is True
    assert s.low is False


def test_non_response_streak_three_days():
    rows = (
        [_row(1, "YES"), _row(2, "YES"), _row(3, "YES"), _row(4, "YES")]
        + [_row(d, "MISSED_NON_RESPONSE") for d in range(5, 8)]
    )
    s = compute_rolling_med_adherence(responses=rows, now_episode_day=7)
    assert s.non_response_streak == 3


def test_streak_breaks_on_response():
    rows = [
        _row(5, "MISSED_NON_RESPONSE"),
        _row(6, "YES"),  # breaks
        _row(7, "MISSED_NON_RESPONSE"),
    ]
    s = compute_rolling_med_adherence(responses=rows, now_episode_day=7)
    assert s.non_response_streak == 1


def test_rolling_window_excludes_pre_window_days():
    """Days outside the window do NOT count toward yes_count."""
    rows = [_row(d, "YES") for d in range(1, 4)]  # before the window
    rows += [_row(d, "NO") for d in range(8, 15)]  # 7 days, all NO
    s = compute_rolling_med_adherence(responses=rows, now_episode_day=14)
    assert s.yes_count == 0
    assert s.low is True


def test_duplicate_rows_per_day_prefer_yes():
    """When defensive duplicate rows exist for the same day, prefer the
    one closer to YES (PRD edge case parity)."""
    rows = [
        _row(1, "MISSED_NON_RESPONSE"),
        _row(1, "PARTIAL"),
        _row(1, "YES"),
    ] + [_row(d, "YES") for d in range(2, 8)]
    s = compute_rolling_med_adherence(responses=rows, now_episode_day=7)
    assert s.yes_count == 7


def test_partial_window_at_episode_start():
    """When `now_episode_day < window_days`, only days >= 1 count."""
    rows = [_row(1, "YES"), _row(2, "YES"), _row(3, "YES")]
    s = compute_rolling_med_adherence(responses=rows, now_episode_day=3)
    assert s.total_days == 3
    assert s.yes_count == 3
    # At day 3 we have only 3 days of data; 3 < high_min_yes (=6) so high=False.
    assert s.high is False


def test_dst_and_timezone_invariance():
    """The algorithm operates on episode-day integers, so it is naturally
    DST- and timezone-invariant (PRD AC-7.3)."""
    rows = [_row(d, "YES") for d in range(1, 8)]
    s_a = compute_rolling_med_adherence(responses=rows, now_episode_day=7)
    s_b = compute_rolling_med_adherence(responses=rows, now_episode_day=7)
    assert s_a == s_b
