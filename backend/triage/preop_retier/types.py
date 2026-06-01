"""
Pydantic schemas for the Pre-Op Re-Tier algorithm.

Times throughout this module are expressed as **hours before surgery**
(positive integers; T-96 = 96 = 96 hours pre-op, T-0 = 0 = surgery start).
This matches the PRD §0 convention while making integer arithmetic clean.

The algorithm's input shape is intentionally pre-derived: signal-sourcing
(parsing intake responses, counting video sessions from event_logs,
binning surveys to green/orange/red) is upstream. The re-tier algorithm
consumes already-computed state.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from triage.types import Tier  # reuse the literal from the initial-tier types


# ─── PAM ────────────────────────────────────────────────────────────────────

PamLevel = Literal["LOW", "MODERATE", "HIGH"]
PamValue = Literal[1, 2, 3, 4, "N_A"]


class PamResponse(BaseModel):
    """A single response on the 13-item PAM-style proxy (PRD §4.1)."""
    item_index: int = Field(ge=1, le=13)
    value: PamValue


class PamResult(BaseModel):
    """Output of `score_pam` (PRD §4.2). Stored on `PamAssessment` in v1+."""
    raw_sum: int
    items_scored: int
    raw_average: float
    activation_score: float           # 0..100
    level: PamLevel
    is_complete: bool                 # items_scored >= 10
    completed_at_hours: Optional[int] = None  # T-N when finalized


# ─── Intake state ───────────────────────────────────────────────────────────

IntakeStatus = Literal["NOT_REQUIRED", "NOT_STARTED", "STARTED", "COMPLETE"]


class IntakeState(BaseModel):
    """Live state of the intake interview at re-tier compute time."""
    status: IntakeStatus = "NOT_STARTED"
    started_at_hours: Optional[int] = None     # T-N when first opened
    completed_at_hours: Optional[int] = None
    disclosures: list[str] = Field(default_factory=list)
    """Codes from `extract_disclosure_flags`. Subset of:
       INTAKE_DISCLOSURE_LIVES_ALONE_NO_CAREGIVER,
       INTAKE_DISCLOSURE_HOUSING_INSTABILITY,
       INTAKE_DISCLOSURE_FOOD_INSECURITY,
       INTAKE_DISCLOSURE_TRANSPORTATION_BARRIER_DAY_OF."""


# ─── Survey window state ────────────────────────────────────────────────────

SurveyWindow = Literal["T_96", "T_48", "T_24"]
SurveyStatus = Literal["PENDING", "GREEN", "ORANGE", "RED", "MISSED"]


class SurveyWindowState(BaseModel):
    """Per-window survey result, mapped from `backend/preop_survey.py` output."""

    # Pydantic protects the `model_*` namespace; nothing here actually conflicts
    # but `value` and similar names are kept clean for forward-compat.
    model_config = ConfigDict(protected_namespaces=())

    window: SurveyWindow
    status: SurveyStatus = "PENDING"
    has_critical_red_flag: bool = False
    """Only meaningful when `status == 'RED'`. Maps to the existing
    `red_flag=True` semantic in `preop_survey.py` for *critical* items
    (NPO violation, active red-flag symptom screen, no ride/caregiver)."""


# ─── Engagement state ───────────────────────────────────────────────────────

class VideoEngagement(BaseModel):
    """Pre-op video engagement state (PRD §6.1)."""
    sessions: list[int] = Field(default_factory=list)
    """Hours-before-surgery for each distinct viewing session
    (sessions separated by ≥ 60s as defined in `tuning.VIDEO_SESSION_GAP_SEC`)."""


class BattleCardEngagement(BaseModel):
    """Battle-card engagement state (PRD §6.2)."""
    views: list[int] = Field(default_factory=list)
    """Hours-before-surgery for each dedup'd view (30-minute dedup window)."""


# ─── Top-level input / output ───────────────────────────────────────────────

class PreOpReTierInput(BaseModel):
    """The full input the re-tier algorithm consumes — all five signal
    sources plus the initial-tier anchor (PRD §3)."""

    initial_tier: Tier
    initial_tier_was_hard_escalator: bool
    """Per PRD §13.13: when the initial tier was overridden by the
    coordinator, this should reflect the *original algorithmic basis*
    (not the override) so the sticky guard still respects the underlying
    clinical condition."""

    hours_until_surgery: int
    """Current 'now' as T-N. 0 = surgery start."""

    pam: Optional[PamResult] = None
    intake: IntakeState = Field(default_factory=IntakeState)
    surveys: list[SurveyWindowState] = Field(default_factory=list)
    video: VideoEngagement = Field(default_factory=VideoEngagement)
    battle_card: BattleCardEngagement = Field(default_factory=BattleCardEngagement)

    # Teach-back (post-loop outcomes only)
    teachback_completed: bool = False
    teachback_failed_med_hold: bool = False
    teachback_failed_fasting: bool = False
    teachback_failed_critical: bool = False
    teachback_not_completed_by_t24: bool = False
    teachback_passed_all: bool = False


ReTierReasonKind = Literal["HARD", "SOFT"]


class ReTierReason(BaseModel):
    """One contributing reason to the recomputed tier — itemized for audit."""
    kind: ReTierReasonKind
    code: str
    label: str
    weight: Optional[int] = None
    """Signed integer for SOFT contributors; None for HARD."""


class PreOpReTierResult(BaseModel):
    """Output of `re_tier_preop` (PRD §5.1)."""

    model_config = ConfigDict(protected_namespaces=())

    initial_tier: Tier
    initial_tier_was_hard: bool
    delta: int                          # 0 when a hard escalator fires
    soft_cap_applied: bool
    computed_tier: Tier
    reasons: list[ReTierReason]
    model_version: str
    tuning_version: int = 1
