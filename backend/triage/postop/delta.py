"""
Unsigned positive-only delta computation for post-op re-tier (PRD §10.3).

Each contributor either fires (adds its weight) or does not. The total
is clamped to `POSTOP_DELTA_CAP` (default 12). Engagement-audit flags
are detected and emitted as `kind: 'ENGAGEMENT_AUDIT'` reasons but
contribute 0 — they cannot move the tier in v1.

Wound-photo-related contributors are intentionally absent (PRD §8 out
of scope v1).
"""

from __future__ import annotations

from typing import Any, Optional

from triage.postop.tuning import (
    CHECKIN_MISSED_ROLLING_CAP_POINTS,
    POSTOP_DELTA_CAP,
    POSTOP_ENGAGEMENT_AUDIT_FLAGS,
    POSTOP_ENGAGEMENT_AUDIT_LABELS,
    POSTOP_POSITIVE_LABELS,
    POSTOP_POSITIVE_WEIGHTS,
)
from triage.postop.types import PostOpReTierInput, PostOpReTierReason


def _pos_reason(code: str, *, detail: Optional[str] = None, weight_override: Optional[int] = None) -> PostOpReTierReason:
    weight = int(weight_override if weight_override is not None else POSTOP_POSITIVE_WEIGHTS.get(code, 0))
    return PostOpReTierReason(
        kind="POSITIVE",
        code=code,
        label=POSTOP_POSITIVE_LABELS.get(code, code),
        weight=weight,
        detail=detail,
    )


def _audit_reason(code: str) -> PostOpReTierReason:
    return PostOpReTierReason(
        kind="ENGAGEMENT_AUDIT",
        code=code,
        label=POSTOP_ENGAGEMENT_AUDIT_LABELS.get(code, code),
        weight=0,
    )


def compute_postop_delta(state: PostOpReTierInput) -> tuple[int, bool, list[PostOpReTierReason]]:
    """Walk every positive contributor + every engagement-audit flag.

    Returns `(delta, capped, reasons)` where:
      - `delta` is the unsigned soft sum after clamping to POSTOP_DELTA_CAP.
      - `capped` is True when the raw uncapped sum exceeded the cap.
      - `reasons` is the ordered audit trail (positive contributors
        first, engagement-audit flags after).
    """
    reasons: list[PostOpReTierReason] = []
    raw_sum = 0

    # ─── Daily check-in (PRD §10.3.a) ──────────────────────────────────────
    if state.last_checkin_tier == "RED":
        r = _pos_reason("CHECKIN_TIER_RED")
        reasons.append(r); raw_sum += r.weight
    elif state.last_checkin_tier == "ORANGE":
        r = _pos_reason("CHECKIN_TIER_ORANGE")
        reasons.append(r); raw_sum += r.weight

    if state.checkin_missed_count_7d > 0:
        per_day = int(POSTOP_POSITIVE_WEIGHTS["CHECKIN_MISSED"])
        unclamped = per_day * int(state.checkin_missed_count_7d)
        capped = min(unclamped, int(CHECKIN_MISSED_ROLLING_CAP_POINTS))
        if capped > 0:
            reasons.append(_pos_reason(
                "CHECKIN_MISSED",
                detail=f"{state.checkin_missed_count_7d} day(s) missed in rolling 7-day window (capped at +{CHECKIN_MISSED_ROLLING_CAP_POINTS})",
                weight_override=capped,
            ))
            raw_sum += capped

    if state.checkin_missed_streak >= 3:
        r = _pos_reason("CHECKIN_MISSED_STREAK_3")
        reasons.append(r); raw_sum += r.weight

    if state.wound_concern_today:
        r = _pos_reason("WOUND_CONCERN_FROM_CHECKIN")
        reasons.append(r); raw_sum += r.weight

    if state.pain_trajectory_abnormal:
        r = _pos_reason("PAIN_TRAJECTORY_WORSE")
        reasons.append(r); raw_sum += r.weight

    # ─── Day-X surveys (PRD §10.3.a) ───────────────────────────────────────
    for day, tier_field, missed_field in (
        (7,  "day7_tier",  "day7_missed"),
        (14, "day14_tier", "day14_missed"),
        (30, "day30_tier", "day30_missed"),
    ):
        tier_val = getattr(state, tier_field)
        missed = getattr(state, missed_field)
        if tier_val == "RED":
            r = _pos_reason(f"SURVEY_DAY_{day}_RED")
            reasons.append(r); raw_sum += r.weight
        elif tier_val == "ORANGE":
            r = _pos_reason(f"SURVEY_DAY_{day}_ORANGE")
            reasons.append(r); raw_sum += r.weight
        if missed:
            r = _pos_reason(f"SURVEY_DAY_{day}_MISSED")
            reasons.append(r); raw_sum += r.weight

    # ─── Video engagement (PRD §10.3.a) ────────────────────────────────────
    if state.red_flag_video_viewed_by_d2:
        reasons.append(_audit_reason("RED_FLAG_VIDEO_VIEWED_BY_D2"))

    if not state.red_flag_video_viewed_by_d5 and state.days_since_discharge > 5:
        r = _pos_reason("RED_FLAG_VIDEO_NOT_VIEWED_BY_D5")
        reasons.append(r); raw_sum += r.weight

    if state.diag_treat_video_viewed_by_d5:
        reasons.append(_audit_reason("DIAGNOSIS_TREATMENT_VIDEO_VIEWED_BY_D5"))

    if state.diag_treat_video_sessions_total >= 3 and state.days_since_discharge >= 14:
        reasons.append(_audit_reason("DIAGNOSIS_TREATMENT_VIDEO_VIEWED_3_PLUS_BY_D14"))

    if not state.diag_treat_video_viewed_by_d14 and state.days_since_discharge > 14:
        r = _pos_reason("DIAGNOSIS_TREATMENT_VIDEO_NOT_VIEWED_BY_D14")
        reasons.append(r); raw_sum += r.weight

    # ─── Med adherence (PRD §10.3.a) ───────────────────────────────────────
    if state.med_adherence_high:
        reasons.append(_audit_reason("MED_ADHERENCE_HIGH"))
    if state.med_adherence_low:
        r = _pos_reason("MED_ADHERENCE_LOW")
        reasons.append(r); raw_sum += r.weight
    if state.med_adherence_non_response_streak_3:
        r = _pos_reason("MED_ADHERENCE_NON_RESPONSE_STREAK_3")
        reasons.append(r); raw_sum += r.weight

    # ─── Care Companion (Triage Suite Pass 3 §3.3) ─────────────────────────
    # Soft +2: tier-2 semantic escalation in the last 24 hours.
    if state.care_companion_tier2_within_24h:
        r = _pos_reason("CARE_COMPANION_SEMANTIC_ESCALATION_TIER_2")
        reasons.append(r); raw_sum += r.weight

    # Audit-only: actively engaged (≥2 chat sessions in 7 days).
    if state.care_companion_chat_sessions_last_7d >= 2:
        reasons.append(_audit_reason("CARE_COMPANION_ACTIVE_LAST_7D"))

    # Soft +1: never used the Care Companion by day 7 post-discharge.
    if (
        state.care_companion_episode_past_d7
        and state.care_companion_chat_sessions_total == 0
    ):
        r = _pos_reason("CARE_COMPANION_NEVER_USED_BY_D7")
        reasons.append(r); raw_sum += r.weight

    # Wound-photo engagement contributors (PRD §10.3.a) intentionally
    # omitted in v1.

    # ─── Care-goal change suppresses engagement-missed contributors (PRD §17.7)
    # We reduce only the missed-engagement portion (videos + missed surveys
    # + non-response streak), preserving safety contributors intact.
    if state.care_goal_changed:
        suppress_codes = {
            "RED_FLAG_VIDEO_NOT_VIEWED_BY_D5",
            "DIAGNOSIS_TREATMENT_VIDEO_NOT_VIEWED_BY_D14",
            "SURVEY_DAY_7_MISSED", "SURVEY_DAY_14_MISSED", "SURVEY_DAY_30_MISSED",
            "MED_ADHERENCE_NON_RESPONSE_STREAK_3", "CHECKIN_MISSED", "CHECKIN_MISSED_STREAK_3",
        }
        suppressed = [r for r in reasons if r.kind == "POSITIVE" and r.code in suppress_codes]
        for s in suppressed:
            raw_sum -= int(s.weight)
        reasons = [r for r in reasons if r not in suppressed]

    capped = max(0, raw_sum) > POSTOP_DELTA_CAP
    delta = min(max(0, raw_sum), POSTOP_DELTA_CAP)

    return delta, capped, reasons
