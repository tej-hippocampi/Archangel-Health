"""
Pydantic schemas for the Post-Op Scoring & Re-Tiering algorithm (PRD v1.0).

Wound-photo signals and types are intentionally absent from this module
(PRD §8 is excluded from v1 per author guidance).
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from triage.types import ProcedureFamily, Tier


# ─── Daily check-in (PRD §4) ─────────────────────────────────────────────────

DailyCheckinTier = Literal["GREEN", "ORANGE", "RED"]
PainTrajectory = Literal["BETTER", "SAME", "WORSE"]
FeverAnswer = Literal["NO", "YES_FELT", "YES_MEASURED"]
IncisionChange = Literal["BETTER", "SAME", "WORSE"]
NauseaLevel = Literal["NONE", "MILD", "MODERATE", "SEVERE"]
EatingLevel = Literal["YES", "SOME", "ALMOST_NOTHING"]
WalkingLevel = Literal["YES", "SOME", "NO"]
WorryLevel = Literal["NOT_AT_ALL", "A_LITTLE", "MODERATELY", "VERY", "EXTREMELY"]


# Item-5 incision flag chip codes (PRD §4.1 item 5).
IncisionFlag = Literal[
    "NEW_REDNESS_SPREADING",
    "NEW_DRAINAGE",
    "OPENING_OR_GAPING",
    "BAD_SMELL",
    "INCREASED_PAIN_AT_INCISION",
]


# Item-8 red-flag symptom chip codes (PRD §4.1 item 8).
RedFlagSymptom = Literal[
    "CHEST_PAIN",
    "SUDDEN_TROUBLE_BREATHING",
    "SUDDEN_WEAKNESS_ONE_SIDE",
    "SEVERE_OR_NEW_BLEEDING",
    "CONFUSION_MENTAL_CHANGE",
    "CALF_SWELLING_OR_PAIN",
    "SEVERE_HEADACHE",
    "FAINTING_OR_NEAR_FAINTING",
]


class DailyCheckinAnswers(BaseModel):
    """Patient submission shape (PRD §4.1)."""

    model_config = ConfigDict(protected_namespaces=())

    pain_nrs: int = Field(ge=0, le=10)
    pain_trajectory: PainTrajectory
    fever: FeverAnswer
    incision_change: IncisionChange
    incision_flags: list[IncisionFlag] = Field(default_factory=list)
    nausea: NauseaLevel
    eating_drinking: EatingLevel
    red_flag_symptoms: list[RedFlagSymptom] = Field(default_factory=list)
    walking: WalkingLevel
    worry_level: WorryLevel
    free_text: Optional[str] = None


class DailyCheckinScored(BaseModel):
    """Output of `score_daily_checkin` (PRD §4.2)."""

    model_config = ConfigDict(protected_namespaces=())

    raw_total: float
    tier: DailyCheckinTier
    red_flags: list[str]
    new_red_flag_symptom: bool
    wound_concern: bool
    pain_nrs: int
    pain_trajectory: PainTrajectory
    item_scores: dict[str, float]


# ─── Day-X surveys (PRD §5) ──────────────────────────────────────────────────

DayXTier = Literal["GREEN", "ORANGE", "RED"]
DayXNumber = Literal[7, 14, 30]


class DayXSurveyAnswers(BaseModel):
    """Patient submission shape (PRD §5.1).

    Each section is an opaque dict the scorer interprets via the
    procedure-family Section B mapping. We keep this loose because the
    section content evolves with the procedure family without changing
    the algorithm signature.
    """

    model_config = ConfigDict(protected_namespaces=())

    section_a: dict[str, Any] = Field(default_factory=dict)   # Pain & symptoms + red-flag screen
    section_b: dict[str, Any] = Field(default_factory=dict)   # Function (procedure-family-specific)
    section_c: dict[str, Any] = Field(default_factory=dict)   # Engagement & adherence
    section_d: dict[str, Any] = Field(default_factory=dict)   # Recovery confidence
    free_text: Optional[str] = None


class DayXSurveyScored(BaseModel):
    """Output of `score_day_survey` (PRD §5.2)."""

    model_config = ConfigDict(protected_namespaces=())

    day: int
    section_scores: dict[str, float]   # {"A": 0..100, "B": ..., "C": ..., "D": ...}
    total_score: float
    tier: DayXTier
    red_flags: list[str]
    procedure_family: Optional[ProcedureFamily] = None


# ─── Med adherence (PRD §7) ──────────────────────────────────────────────────

MedAdherenceResponseValue = Literal["YES", "PARTIAL", "NO", "REPLY_LATER", "MISSED_NON_RESPONSE"]


class MedAdherenceWindowSummary(BaseModel):
    """Output of `compute_rolling_med_adherence` over a 7-day window."""

    yes_count: int
    total_days: int
    high: bool
    low: bool
    non_response_streak: int


# ─── Lost contact (PRD §10.2) ────────────────────────────────────────────────

class LostContactStatus(BaseModel):
    tier3_24h: bool
    general_72h: bool
    last_response_at: Optional[str] = None


# ─── Video engagement (PRD §6) ───────────────────────────────────────────────

VideoKind = Literal["DIAGNOSIS_TREATMENT", "RED_FLAG"]
VideoEventType = Literal["PLAYED", "COMPLETED"]


# ─── Re-tier algorithm (PRD §10) ─────────────────────────────────────────────

PostOpReasonKind = Literal["HARD", "POSITIVE", "ENGAGEMENT_AUDIT", "INFO"]


class PostOpReTierReason(BaseModel):
    """One contributor to the post-op recompute — itemized for audit."""

    kind: PostOpReasonKind
    code: str
    label: str
    weight: int = 0
    detail: Optional[str] = None


class PostOpReTierInput(BaseModel):
    """Inputs to `re_tier_post_op` (PRD §10.1).

    All fields are pre-aggregated by the apply layer so the algorithm
    is a pure function of this snapshot. Wound-photo-related fields
    are intentionally omitted (out of scope v1).
    """

    model_config = ConfigDict(protected_namespaces=())

    patient_id: str
    procedure_family: Optional[ProcedureFamily] = None
    post_intraop_tier: Tier
    current_tier: Tier
    days_since_discharge: int = 0
    care_goal_changed: bool = False
    has_active_self_flag: bool = False

    # Daily check-in summary
    last_checkin_tier: Optional[DailyCheckinTier] = None
    checkin_red_count_7d: int = 0
    checkin_orange_count_7d: int = 0
    checkin_missed_count_7d: int = 0
    checkin_missed_streak: int = 0
    wound_concern_today: bool = False
    pain_trajectory_abnormal: bool = False
    new_red_flag_symptom_today: bool = False
    multiple_incision_flags_today: bool = False
    incision_flag_streak: int = 0   # consecutive days with >=1 chip

    # Day-X surveys (PRD §5)
    day7_tier: Optional[DayXTier] = None
    day7_red_flag: bool = False
    day7_missed: bool = False
    day14_tier: Optional[DayXTier] = None
    day14_red_flag: bool = False
    day14_missed: bool = False
    day30_tier: Optional[DayXTier] = None
    day30_red_flag: bool = False
    day30_missed: bool = False

    # Videos (PRD §6)
    red_flag_video_viewed_by_d2: bool = False
    red_flag_video_viewed_by_d5: bool = False
    diag_treat_video_viewed_by_d5: bool = False
    diag_treat_video_sessions_total: int = 0
    diag_treat_video_viewed_by_d14: bool = False

    # Med adherence (PRD §7)
    med_adherence_high: bool = False
    med_adherence_low: bool = False
    med_adherence_non_response_streak_3: bool = False

    # Teach-back (post-loop only; first-attempt misses carry zero weight)
    teachback_completed: bool = False
    teachback_failed_critical: bool = False
    teachback_failed_red_flag: bool = False
    teachback_failed_med: bool = False
    teachback_not_completed_by_d5: bool = False

    # Lost contact (PRD §10.2)
    lost_contact_tier3_24h: bool = False
    lost_contact_general_72h: bool = False

    # ─── Care Companion (Triage Suite Pass 3 §3.3) ─────────────────────────
    # Resolution semantics: a "tier-3 verdict" is `unresolved` whenever the
    # patient has at least one open `escalations` row whose
    # `trigger_type` LIKE 'chat:semantic%'. Closing that row clears the
    # contributor on the next re-tier.
    care_companion_red_flag_unresolved: bool = False
    care_companion_tier2_within_24h: bool = False
    care_companion_chat_sessions_last_7d: int = 0
    care_companion_chat_sessions_total: int = 0
    care_companion_episode_past_d7: bool = False


class PostOpReTierResult(BaseModel):
    """Output of `re_tier_post_op` (PRD §10.1)."""

    model_config = ConfigDict(protected_namespaces=())

    floor: Tier
    proposed_tier: Tier
    delta: int
    delta_capped: bool
    hard_escalator_fired: bool
    reasons: list[PostOpReTierReason]
    model_version: str
    tuning_version: int


class PostOpReTierEvent(BaseModel):
    """Pydantic mirror of a row in `postop_retier_events`."""

    model_config = ConfigDict(protected_namespaces=())

    id: str
    patient_id: str
    triggered_by: str
    inputs_snapshot: dict[str, Any]
    post_intraop_tier: Tier
    computed_delta: int
    computed_tier: Tier
    tier_before: Tier
    tier_after: Tier
    changed: bool
    reasons: list[PostOpReTierReason]
    model_version: str
    tuning_version: int
    created_at: str
