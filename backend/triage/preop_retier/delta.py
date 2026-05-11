"""
Soft-delta computation for pre-op re-tier (PRD §5.3).

Each category emits its contributor(s); PRD §5.3 rule 1 demands
"single contributor per category" with the most-specific T-N replacing
the less-specific one in non-stacking ladders. Two PAM-completion
contributors stack per the PRD's explicit "additional, on top of T-72
penalty" comment. Total raw delta is clamped to ±SOFT_CAP per rule 3.

`compute_preop_delta(state)` is pure and deterministic — it consumes
already-derived state and emits a signed delta plus the itemized
reasons that produced it.
"""

from __future__ import annotations

from triage.preop_retier.tuning import SOFT_CAP, SOFT_LABELS, WEIGHTS
from triage.preop_retier.types import (
    PreOpReTierInput,
    ReTierReason,
    SurveyWindow,
    SurveyWindowState,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _reason(code: str) -> ReTierReason:
    """Build a SOFT reason from a weight code; weight pulled from tuning."""
    return ReTierReason(
        kind="SOFT",
        code=code,
        label=SOFT_LABELS.get(code, code),
        weight=WEIGHTS[code],
    )


def _find_survey(state: PreOpReTierInput, window: SurveyWindow) -> SurveyWindowState | None:
    for s in state.surveys:
        if s.window == window:
            return s
    return None


# ─── Category contributors ───────────────────────────────────────────────────

def _pam_contributors(state: PreOpReTierInput) -> list[ReTierReason]:
    """PAM emits a level contributor when complete; otherwise one or two
    (stacking) not-completed-by penalties — PRD §5.3 marks T-24 as
    'additional, on top of T-72 penalty'."""
    out: list[ReTierReason] = []
    pam = state.pam

    if pam is not None and pam.is_complete:
        if pam.level == "LOW":
            out.append(_reason("PAM_LEVEL_LOW"))
        elif pam.level == "MODERATE":
            out.append(_reason("PAM_LEVEL_MODERATE"))
        else:
            out.append(_reason("PAM_LEVEL_HIGH"))
        return out

    # Not complete (or absent) — apply the not-completed-by ladder.
    if state.hours_until_surgery <= 72:
        out.append(_reason("PAM_NOT_COMPLETED_BY_T_72"))
    if state.hours_until_surgery <= 24:
        out.append(_reason("PAM_NOT_COMPLETED_BY_T_24"))   # additional
    return out


def _intake_contributors(state: PreOpReTierInput) -> list[ReTierReason]:
    """Intake emits a single contributor reflecting the most-specific
    applicable state. NOT_REQUIRED short-circuits (PRD §13.10)."""
    intake = state.intake

    if intake.status == "NOT_REQUIRED":
        return []

    if intake.status == "COMPLETE":
        return [_reason("INTAKE_COMPLETE")]

    h = state.hours_until_surgery

    # Most-recent rule wins. T-24 contributor applies regardless of started/not_started.
    if h <= 24:
        return [_reason("INTAKE_NOT_COMPLETE_BY_T_24")]
    if intake.status == "STARTED" and h <= 48:
        return [_reason("INTAKE_STARTED_NOT_COMPLETE_BY_T_48")]
    if intake.status == "NOT_STARTED" and h <= 72:
        return [_reason("INTAKE_NOT_STARTED_BY_T_72")]
    if intake.status == "NOT_STARTED" and h <= 96:
        return [_reason("INTAKE_NOT_STARTED_BY_T_96")]
    return []


def _survey_contributor(window: SurveyWindow, state_window: SurveyWindowState | None) -> list[ReTierReason]:
    """Each window emits exactly one contributor reflecting its scorer outcome.
    PENDING (window not yet evaluated) emits nothing."""
    if state_window is None or state_window.status == "PENDING":
        return []
    code = f"SURVEY_{window}_{state_window.status}"
    if code not in WEIGHTS:
        return []
    return [_reason(code)]


def _video_contributors(state: PreOpReTierInput) -> list[ReTierReason]:
    """Video has two independent ladders:

    - Positive (rewards) — both can fire (PRD §5.3 marks the 3+ contributor as 'additional').
    - Negative (penalties) — T-24 *replaces* T-48 per PRD §5.3 rule 1.

    Engagement contributors evaluate against actual session timestamps
    (PRD §13.4), not 'as of now'.
    """
    out: list[ReTierReason] = []
    sessions = state.video.sessions

    # Positive ladder — viewed before each milestone (stacking).
    if any(s >= 72 for s in sessions):
        out.append(_reason("VIDEO_VIEWED_AT_LEAST_ONCE_BY_T_72"))
    if sum(1 for s in sessions if s >= 48) >= 3:
        out.append(_reason("VIDEO_VIEWED_3_OR_MORE_BY_T_48"))

    # Negative ladder — missed-by milestones (T-24 replaces T-48).
    h = state.hours_until_surgery
    missed_by_t24 = h <= 24 and not any(s >= 24 for s in sessions)
    missed_by_t48 = h <= 48 and not any(s >= 48 for s in sessions)
    if missed_by_t24:
        out.append(_reason("VIDEO_NOT_VIEWED_BY_T_24"))      # replaces +1
    elif missed_by_t48:
        out.append(_reason("VIDEO_NOT_VIEWED_BY_T_48"))
    return out


def _battlecard_contributors(state: PreOpReTierInput) -> list[ReTierReason]:
    """Battle-card: a single positive (viewed-by-T-48) and a single negative
    (not-viewed-by-T-24). They cannot both fire for the same patient state."""
    out: list[ReTierReason] = []
    views = state.battle_card.views
    h = state.hours_until_surgery

    if any(v >= 48 for v in views):
        out.append(_reason("BATTLECARD_VIEWED_AT_LEAST_ONCE_BY_T_48"))
    elif h <= 24 and not any(v >= 24 for v in views):
        out.append(_reason("BATTLECARD_NOT_VIEWED_BY_T_24"))
    return out


def _cumulative_contributor(state: PreOpReTierInput) -> list[ReTierReason]:
    """ENGAGEMENT_FULLY_COMPLETE_BY_T_24 — the cumulative reward.

    Fires only when all of:
      - intake complete
      - PAM complete with level in {HIGH, MODERATE}
      - all three surveys submitted (any non-PENDING / non-MISSED tier)
      - video viewed at least once (any timestamp)
      - battle-card viewed at least once (any timestamp)
      - currently at or past T-24
    """
    if state.hours_until_surgery > 24:
        return []
    if state.intake.status != "COMPLETE":
        return []
    if state.pam is None or not state.pam.is_complete or state.pam.level == "LOW":
        return []

    submitted_windows = {
        s.window for s in state.surveys
        if s.status in ("GREEN", "ORANGE", "RED")
    }
    if submitted_windows != {"T_96", "T_48", "T_24"}:
        return []

    if not state.video.sessions:
        return []
    if not state.battle_card.views:
        return []

    return [_reason("ENGAGEMENT_FULLY_COMPLETE_BY_T_24")]


# ─── Top-level ───────────────────────────────────────────────────────────────

def compute_preop_delta(
    state: PreOpReTierInput,
) -> tuple[int, list[ReTierReason], bool]:
    """Compute the signed soft delta, the contributing reasons, and whether
    the soft cap was applied. Pure function — no side effects.

    Returns:
      (clamped_delta, reasons, soft_cap_applied)
    """
    reasons: list[ReTierReason] = []
    reasons += _pam_contributors(state)
    reasons += _intake_contributors(state)
    for window in ("T_96", "T_48", "T_24"):
        reasons += _survey_contributor(window, _find_survey(state, window))  # type: ignore[arg-type]
    reasons += _video_contributors(state)
    reasons += _battlecard_contributors(state)
    reasons += _cumulative_contributor(state)

    raw_delta = sum((r.weight or 0) for r in reasons)
    if raw_delta > SOFT_CAP:
        return SOFT_CAP, reasons, True
    if raw_delta < -SOFT_CAP:
        return -SOFT_CAP, reasons, True
    return raw_delta, reasons, False
