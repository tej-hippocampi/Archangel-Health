"""
Unit tests for the Pre-Op Re-Tier algorithm.

Covers PRD §5.5 worked examples A–F plus the algorithmic edge cases
identified in the PRD (delta-threshold boundaries, mutual-exclusion
ladders, non-critical red, intake-not-required short-circuit, soft
cap, PAM LOW hard-vs-soft, and idempotent recompute).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from triage.preop_retier import re_tier_preop  # noqa: E402
from triage.preop_retier.mapping import apply_delta_with_guard  # noqa: E402
from triage.preop_retier.types import (  # noqa: E402
    BattleCardEngagement,
    IntakeState,
    PamResult,
    PreOpReTierInput,
    SurveyWindowState,
    VideoEngagement,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _pam(level: str = "HIGH", *, complete: bool = True) -> PamResult:
    """Build a synthetic PamResult bypassing the scoring math."""
    score_for_level = {"LOW": 30.0, "MODERATE": 60.0, "HIGH": 90.0}
    return PamResult(
        raw_sum=39, items_scored=13, raw_average=3.0,
        activation_score=score_for_level.get(level, 60.0),
        level=level, is_complete=complete,
    )


def _surveys(t96: str = "PENDING", t48: str = "PENDING", t24: str = "PENDING",
             *, t48_critical: bool = False, t24_critical: bool = False) -> list[SurveyWindowState]:
    return [
        SurveyWindowState(window="T_96", status=t96),
        SurveyWindowState(window="T_48", status=t48, has_critical_red_flag=t48_critical),
        SurveyWindowState(window="T_24", status=t24, has_critical_red_flag=t24_critical),
    ]


def _state(**overrides):
    """Build a default-quiet PreOpReTierInput; override individual fields per test."""
    base = dict(
        initial_tier="TIER_1",
        initial_tier_was_hard_escalator=False,
        hours_until_surgery=72,
        pam=None,
        intake=IntakeState(status="NOT_STARTED"),
        surveys=_surveys(),
        video=VideoEngagement(sessions=[]),
        battle_card=BattleCardEngagement(views=[]),
    )
    base.update(overrides)
    return PreOpReTierInput(**base)


# ─── PRD §5.5 — Worked examples A–F ──────────────────────────────────────────

def test_example_a_t1_stays_t1_floor():
    """Initial T1 (soft), HIGH PAM, intake complete, T-96/T-48 green,
    video 2×, battle-card 1× → delta = -6 → would downgrade but T1 is floor."""
    state = _state(
        initial_tier="TIER_1",
        hours_until_surgery=48,
        pam=_pam("HIGH"),
        intake=IntakeState(status="COMPLETE"),
        surveys=_surveys(t96="GREEN", t48="GREEN"),
        video=VideoEngagement(sessions=[80, 60]),
        battle_card=BattleCardEngagement(views=[60]),
    )
    out = re_tier_preop(state)
    assert out.computed_tier == "TIER_1"
    assert out.delta == -6
    assert not out.soft_cap_applied


def test_example_b_t2_to_t1_downgrade_allowed():
    """Initial T2 (soft) + max engagement + cumulative reward → delta -8 → T1."""
    state = _state(
        initial_tier="TIER_2",
        hours_until_surgery=20,
        pam=_pam("HIGH"),
        intake=IntakeState(status="COMPLETE"),
        surveys=_surveys(t96="GREEN", t48="GREEN", t24="GREEN"),
        video=VideoEngagement(sessions=[80, 60, 50, 30]),
        battle_card=BattleCardEngagement(views=[60]),
    )
    out = re_tier_preop(state)
    assert out.delta == -8
    assert out.computed_tier == "TIER_1"


def test_example_c_t3_sticky_blocks_downgrade():
    """Initial T3 by hard escalator + max engagement → delta -8 but sticky guard → T3."""
    state = _state(
        initial_tier="TIER_3",
        initial_tier_was_hard_escalator=True,
        hours_until_surgery=20,
        pam=_pam("HIGH"),
        intake=IntakeState(status="COMPLETE"),
        surveys=_surveys(t96="GREEN", t48="GREEN", t24="GREEN"),
        video=VideoEngagement(sessions=[80, 60, 50, 30]),
        battle_card=BattleCardEngagement(views=[60]),
    )
    out = re_tier_preop(state)
    assert out.delta == -8
    assert out.computed_tier == "TIER_3"


def test_sticky_hard_guard_blocks_minus_5_downgrade():
    """Triage Suite Pass 2 §5 explicit case: an initial-T3 hard-escalated
    patient with a soft delta of exactly −5 must still clamp at TIER_3.

    Build the −5 from: PAM HIGH (-3), video viewed by T-72 (-1),
    battle-card viewed by T-48 (-1). Intake is NOT_REQUIRED to avoid
    extra completion / not-completed-by contributors at this hour."""
    state = _state(
        initial_tier="TIER_3",
        initial_tier_was_hard_escalator=True,
        hours_until_surgery=72,
        pam=_pam("HIGH"),
        intake=IntakeState(status="NOT_REQUIRED"),
        surveys=_surveys(),  # all PENDING — 0 contribution
        video=VideoEngagement(sessions=[80]),
        battle_card=BattleCardEngagement(views=[60]),
    )
    out = re_tier_preop(state)
    assert out.delta == -5
    assert out.computed_tier == "TIER_3", (
        "Sticky-hard guard must clamp the downgrade at TIER_3"
    )


def test_sticky_hard_guard_does_not_apply_when_initial_was_soft():
    """Same −5 delta, but the initial tier was *soft* T_3 (e.g. score
    based) — sticky guard does not apply, so the downgrade lands."""
    state = _state(
        initial_tier="TIER_3",
        initial_tier_was_hard_escalator=False,
        hours_until_surgery=72,
        pam=_pam("HIGH"),
        intake=IntakeState(status="NOT_REQUIRED"),
        surveys=_surveys(),
        video=VideoEngagement(sessions=[80]),
        battle_card=BattleCardEngagement(views=[60]),
    )
    out = re_tier_preop(state)
    assert out.delta == -5
    # Algorithmic downgrade allowed (one step) when the floor isn't sticky.
    assert out.computed_tier in ("TIER_2", "TIER_1")


def test_example_d_t1_to_t3_via_upgrade_2():
    """Initial T1, intake never started by T-72 (+3), PAM not completed by T-72 (+2),
    T-96 missed (+2), video not viewed by T-48 (+1) → delta +8 → upgrade 2 → TIER_3."""
    state = _state(
        initial_tier="TIER_1",
        hours_until_surgery=48,
        pam=None,
        intake=IntakeState(status="NOT_STARTED"),
        surveys=_surveys(t96="MISSED"),
        video=VideoEngagement(sessions=[]),
        battle_card=BattleCardEngagement(views=[]),
    )
    out = re_tier_preop(state)
    assert out.delta == 8
    assert out.computed_tier == "TIER_3"

    codes = {r.code for r in out.reasons}
    assert "INTAKE_NOT_STARTED_BY_T_72" in codes
    assert "INTAKE_NOT_STARTED_BY_T_96" not in codes  # mutual exclusion
    assert "PAM_NOT_COMPLETED_BY_T_72" in codes
    assert "PAM_NOT_COMPLETED_BY_T_24" not in codes   # not yet at T-24
    assert "SURVEY_T_96_MISSED" in codes
    assert "VIDEO_NOT_VIEWED_BY_T_48" in codes


def test_example_e_t1_to_t3_via_intake_disclosure():
    """INTAKE_DISCLOSURE_LIVES_ALONE_NO_CAREGIVER → hard → TIER_3."""
    state = _state(
        initial_tier="TIER_1",
        hours_until_surgery=72,
        intake=IntakeState(
            status="COMPLETE",
            disclosures=["INTAKE_DISCLOSURE_LIVES_ALONE_NO_CAREGIVER"],
        ),
    )
    out = re_tier_preop(state)
    assert out.computed_tier == "TIER_3"
    assert out.delta == 0
    assert len(out.reasons) == 1
    assert out.reasons[0].kind == "HARD"
    assert out.reasons[0].code == "INTAKE_DISCLOSURE_LIVES_ALONE_NO_CAREGIVER"


def test_example_f_t1_to_t3_via_critical_red_flag():
    """T-24 RED with critical red flag → SURVEY_RED_FLAG_CRITICAL hard → TIER_3."""
    state = _state(
        initial_tier="TIER_1",
        hours_until_surgery=20,
        intake=IntakeState(status="COMPLETE"),
        surveys=_surveys(t24="RED", t24_critical=True),
    )
    out = re_tier_preop(state)
    assert out.computed_tier == "TIER_3"
    assert out.delta == 0
    assert out.reasons[0].kind == "HARD"
    assert out.reasons[0].code == "SURVEY_RED_FLAG_CRITICAL"


# ─── §5.4 — Delta → tier mapping boundaries ──────────────────────────────────

def test_apply_delta_dead_zone_no_change():
    assert apply_delta_with_guard("TIER_2", False, 0) == "TIER_2"
    assert apply_delta_with_guard("TIER_2", False, 2) == "TIER_2"
    assert apply_delta_with_guard("TIER_2", False, -2) == "TIER_2"


def test_apply_delta_upgrade_1():
    assert apply_delta_with_guard("TIER_1", False, 3) == "TIER_2"
    assert apply_delta_with_guard("TIER_1", False, 5) == "TIER_2"
    assert apply_delta_with_guard("TIER_2", False, 5) == "TIER_3"


def test_apply_delta_upgrade_2():
    assert apply_delta_with_guard("TIER_1", False, 6) == "TIER_3"
    assert apply_delta_with_guard("TIER_1", False, 8) == "TIER_3"
    assert apply_delta_with_guard("TIER_2", False, 12) == "TIER_3"  # capped at TIER_3


def test_apply_delta_downgrade_blocked_by_sticky():
    assert apply_delta_with_guard("TIER_3", True, -8) == "TIER_3"
    assert apply_delta_with_guard("TIER_2", True, -3) == "TIER_2"


def test_apply_delta_downgrade_floor_at_t1():
    assert apply_delta_with_guard("TIER_1", False, -6) == "TIER_1"


# ─── §5.3 rule 1 — Mutual exclusion ladders ──────────────────────────────────

def test_mutual_exclusion_video_t24_replaces_t48():
    """No views ever, currently at T-20: only VIDEO_NOT_VIEWED_BY_T_24 fires."""
    state = _state(
        initial_tier="TIER_1",
        hours_until_surgery=20,
        intake=IntakeState(status="COMPLETE"),
        video=VideoEngagement(sessions=[]),
    )
    out = re_tier_preop(state)
    codes = {r.code for r in out.reasons}
    assert "VIDEO_NOT_VIEWED_BY_T_24" in codes
    assert "VIDEO_NOT_VIEWED_BY_T_48" not in codes


def test_pam_not_completed_t24_stacks_with_t72():
    """PRD §5.3 explicitly: T-24 PAM penalty is *additional* on top of T-72."""
    state = _state(
        initial_tier="TIER_1",
        hours_until_surgery=20,
        pam=None,
    )
    out = re_tier_preop(state)
    codes_with_weights = {r.code: r.weight for r in out.reasons}
    assert codes_with_weights.get("PAM_NOT_COMPLETED_BY_T_72") == 2
    assert codes_with_weights.get("PAM_NOT_COMPLETED_BY_T_24") == 3


# ─── §13.3 — Non-critical red does not escalate ──────────────────────────────

def test_non_critical_red_does_not_hard_escalate():
    """Survey RED without critical flag: SURVEY_T_48_RED soft fires, no hard."""
    state = _state(
        initial_tier="TIER_1",
        hours_until_surgery=40,
        intake=IntakeState(status="COMPLETE"),
        surveys=_surveys(t48="RED", t48_critical=False),
    )
    out = re_tier_preop(state)
    assert out.computed_tier != "TIER_3" or out.reasons[0].kind != "HARD"
    codes = {r.code for r in out.reasons}
    assert "SURVEY_T_48_RED" in codes
    assert all(r.kind != "HARD" for r in out.reasons)


# ─── §13.10 — Intake not required short-circuits intake contributors ─────────

def test_intake_not_required_short_circuits():
    state = _state(
        initial_tier="TIER_1",
        hours_until_surgery=20,
        intake=IntakeState(status="NOT_REQUIRED"),
        pam=_pam("HIGH"),
    )
    out = re_tier_preop(state)
    intake_codes = {
        "INTAKE_NOT_STARTED_BY_T_96", "INTAKE_NOT_STARTED_BY_T_72",
        "INTAKE_STARTED_NOT_COMPLETE_BY_T_48", "INTAKE_NOT_COMPLETE_BY_T_24",
        "INTAKE_COMPLETE",
    }
    fired = {r.code for r in out.reasons}
    assert intake_codes.isdisjoint(fired)


# ─── §5.3 rule 3 — Soft cap ──────────────────────────────────────────────────

def test_soft_cap_clamps_extreme_positive_delta():
    """Force a synthetic stack of upgrade signals exceeding +12."""
    state = _state(
        initial_tier="TIER_1",
        hours_until_surgery=20,                    # past T-24
        pam=_pam("LOW"),                           # but in v1 hard-escalates at T-24, so test below
    )
    # Need to avoid the PAM-LOW-at-T-24 hard. Use PAM MODERATE plus stacked penalties.
    state = _state(
        initial_tier="TIER_1",
        hours_until_surgery=20,
        pam=_pam("MODERATE"),                                          # +1
        intake=IntakeState(status="NOT_STARTED"),                      # +4 (T-24 not complete)
        surveys=_surveys(t96="RED", t48="RED", t24="RED"),             # +9 (3 × +3)
        video=VideoEngagement(sessions=[]),                            # +2 (NOT_VIEWED_BY_T_24)
        battle_card=BattleCardEngagement(views=[]),                    # +1 (NOT_VIEWED_BY_T_24)
    )
    out = re_tier_preop(state)
    raw_total = sum(r.weight or 0 for r in out.reasons)
    assert raw_total > 12   # raw exceeds cap
    assert out.delta == 12  # clamped
    assert out.soft_cap_applied is True


# ─── PAM LOW: hard at T-24, soft elsewhere ───────────────────────────────────

def test_pam_low_at_t24_is_hard():
    state = _state(
        initial_tier="TIER_1",
        hours_until_surgery=20,
        pam=_pam("LOW"),
        intake=IntakeState(status="COMPLETE"),
    )
    out = re_tier_preop(state)
    assert out.computed_tier == "TIER_3"
    assert out.delta == 0
    assert out.reasons[0].kind == "HARD"
    assert out.reasons[0].code == "PAM_LEVEL_LOW_AT_T_24"


def test_pam_low_pre_t24_is_soft():
    """Same PAM but earlier: PAM_LEVEL_LOW soft contributor (+5), no hard."""
    state = _state(
        initial_tier="TIER_1",
        hours_until_surgery=72,
        pam=_pam("LOW"),
        intake=IntakeState(status="COMPLETE"),
    )
    out = re_tier_preop(state)
    assert all(r.kind != "HARD" for r in out.reasons)
    codes_with_weights = {r.code: r.weight for r in out.reasons}
    assert codes_with_weights.get("PAM_LEVEL_LOW") == 5
    assert "PAM_LEVEL_LOW_AT_T_24" not in codes_with_weights


# ─── §5.1 / §13.14 — Idempotent recompute ────────────────────────────────────

def test_idempotent_recompute_same_input():
    """Two calls with identical state must produce identical results."""
    state = _state(
        initial_tier="TIER_1",
        hours_until_surgery=48,
        pam=_pam("HIGH"),
        intake=IntakeState(status="COMPLETE"),
        surveys=_surveys(t96="GREEN"),
        video=VideoEngagement(sessions=[80]),
        battle_card=BattleCardEngagement(views=[60]),
    )
    a = re_tier_preop(state).model_dump()
    b = re_tier_preop(state).model_dump()
    assert a == b


def test_idempotent_lifts_after_hard_resolves():
    """§13.14: hard escalator dominates while true; lifts cleanly when removed."""
    base = _state(
        initial_tier="TIER_1",
        hours_until_surgery=20,
        intake=IntakeState(status="COMPLETE"),
        surveys=_surveys(t48="RED", t48_critical=True),
    )
    while_hard = re_tier_preop(base)
    assert while_hard.computed_tier == "TIER_3"

    # Critical flag removed; same engagement otherwise.
    resolved = base.model_copy(update={
        "surveys": _surveys(t48="ORANGE"),  # downgrade red → orange after resolution
    })
    after = re_tier_preop(resolved)
    assert all(r.kind != "HARD" for r in after.reasons)


# ─── Sticky guard for non-hard initial tiers ─────────────────────────────────

def test_soft_initial_can_downgrade():
    """Initial T2 (soft) with strong engagement → downgrade allowed."""
    state = _state(
        initial_tier="TIER_2",
        initial_tier_was_hard_escalator=False,
        hours_until_surgery=48,
        pam=_pam("HIGH"),
        intake=IntakeState(status="COMPLETE"),
        surveys=_surveys(t96="GREEN", t48="GREEN"),
        video=VideoEngagement(sessions=[80, 60]),
        battle_card=BattleCardEngagement(views=[60]),
    )
    out = re_tier_preop(state)
    assert out.computed_tier == "TIER_1"
