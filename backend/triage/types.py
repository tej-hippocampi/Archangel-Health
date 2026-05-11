"""
Pydantic schemas for the Initial Pre-Op Triage algorithm.

These mirror PRD §4 (Six input categories — canonical schemas) and the
output types used by `assign_initial_tier`.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# ─── Tier output ─────────────────────────────────────────────────────────────

Tier = Literal["TIER_1", "TIER_2", "TIER_3"]
TierReasonKind = Literal["HARD", "BASE", "SOFT"]


class TierReason(BaseModel):
    """One contributing reason to the final tier — itemized in the audit log."""
    kind: TierReasonKind
    code: str
    label: str
    weight: Optional[int] = None  # None for HARD; 0+ for BASE/SOFT


class TierAssignment(BaseModel):
    """Output of `assign_initial_tier`."""

    # `model_version` would otherwise collide with Pydantic's `model_*` namespace.
    model_config = ConfigDict(protected_namespaces=())

    tier: Tier
    score: Optional[int]  # None when a hard escalator triggered
    reasons: list[TierReason]
    model_version: str
    tuning_version: int = 1


# ─── 4.1 Procedure ───────────────────────────────────────────────────────────

ProcedureFamily = Literal[
    "LEJR",
    "CABG",
    "SPINAL_FUSION",
    "HIP_FEMUR_FRACTURE",
    "MAJOR_BOWEL",
]


class ProcedureInput(BaseModel):
    cpt_code: str
    anchor_procedure_family: ProcedureFamily
    scheduled_date: str  # ISO date
    is_emergency: bool = False
    bilateral: Optional[bool] = None
    laterality: Optional[Literal["LEFT", "RIGHT", "BILATERAL", "N_A"]] = None
    approach: Optional[Literal["OPEN", "MIS", "ROBOTIC", "UNKNOWN"]] = None
    notes: Optional[str] = None


# ─── 4.2 Active Problems / Medical History ───────────────────────────────────

class ActiveProblem(BaseModel):
    icd10: str
    description: str = ""
    status: Literal["ACTIVE", "RESOLVED", "CHRONIC"] = "ACTIVE"
    onset_date: Optional[str] = None
    severity_note: Optional[str] = None


class ActiveProblemsInput(BaseModel):
    problems: list[ActiveProblem] = Field(default_factory=list)
    functional_status: Literal[
        "INDEPENDENT",
        "PARTIALLY_DEPENDENT",
        "TOTALLY_DEPENDENT",
        "UNKNOWN",
    ] = "INDEPENDENT"
    bmi: Optional[float] = None
    asa_class_if_documented: Optional[Literal[1, 2, 3, 4, 5]] = None


# ─── 4.3 Current Medications ─────────────────────────────────────────────────

class Medication(BaseModel):
    rxnorm_code: Optional[str] = None
    name: str
    dose: Optional[str] = None
    route: Optional[str] = None
    frequency: Optional[str] = None
    start_date: Optional[str] = None
    indication: Optional[str] = None


class MedicationsInput(BaseModel):
    medications: list[Medication] = Field(default_factory=list)


# ─── 4.4 Allergies ───────────────────────────────────────────────────────────

class Allergy(BaseModel):
    substance: str
    reaction_type: Literal[
        "ANAPHYLAXIS",
        "RASH",
        "GI",
        "ANGIOEDEMA",
        "OTHER",
        "UNKNOWN",
    ] = "UNKNOWN"
    severity: Optional[Literal["MILD", "MODERATE", "SEVERE"]] = None
    notes: Optional[str] = None


class AllergiesInput(BaseModel):
    allergies: list[Allergy] = Field(default_factory=list)


# ─── 4.5 Social History ──────────────────────────────────────────────────────

class SubstanceUse(BaseModel):
    substance: Literal["OPIOIDS", "STIMULANTS", "CANNABIS", "OTHER"]
    status: Literal["ACTIVE", "IN_RECOVERY", "PRIOR", "UNKNOWN"] = "UNKNOWN"


class SocialHistoryInput(BaseModel):
    smoking_status: Literal["NEVER", "FORMER", "CURRENT", "UNKNOWN"] = "UNKNOWN"
    pack_years: Optional[float] = None
    alcohol_use: Literal[
        "NONE",
        "OCCASIONAL",
        "MODERATE",
        "HEAVY",
        "AT_RISK_OR_AUDIT_POSITIVE",
        "UNKNOWN",
    ] = "UNKNOWN"
    substance_use: list[SubstanceUse] = Field(default_factory=list)
    lives_alone: Optional[bool] = None
    has_reliable_caregiver: Optional[bool] = None
    housing_status: Literal["STABLE", "UNSTABLE", "HOMELESS", "UNKNOWN"] = "STABLE"
    food_security: Literal["SECURE", "INSECURE", "UNKNOWN"] = "SECURE"
    transportation_barrier: Optional[bool] = None
    employment_status: Optional[Literal[
        "EMPLOYED", "UNEMPLOYED", "RETIRED", "DISABLED", "UNKNOWN"
    ]] = None
    primary_language: Optional[str] = None
    needs_interpreter: Optional[bool] = None
    age: int = 0  # ACS-NSQIP soft factor (≥75)


# ─── 4.6 Recent Labs and Studies ─────────────────────────────────────────────

class LabResult(BaseModel):
    loinc: Optional[str] = None
    name: str
    value: float
    unit: str = ""
    drawn_at: str = ""
    reference_range: Optional[str] = None
    is_abnormal: Optional[bool] = None


class StudyResult(BaseModel):
    type: Literal[
        "ECHO", "ECG", "PFT", "CXR", "STRESS_TEST", "CARDIAC_CATH", "OTHER"
    ] = "OTHER"
    performed_at: str = ""
    summary: str = ""
    ejection_fraction: Optional[float] = None
    significant_findings: list[str] = Field(default_factory=list)


class RecentLabsInput(BaseModel):
    labs: list[LabResult] = Field(default_factory=list)
    studies: list[StudyResult] = Field(default_factory=list)


# ─── Top-level input ─────────────────────────────────────────────────────────

class InitialTierInput(BaseModel):
    """The full input the algorithm consumes — six categories per PRD §4."""

    procedure: ProcedureInput
    active_problems: ActiveProblemsInput = Field(default_factory=ActiveProblemsInput)
    medications: MedicationsInput = Field(default_factory=MedicationsInput)
    allergies: AllergiesInput = Field(default_factory=AllergiesInput)
    social_history: SocialHistoryInput = Field(default_factory=SocialHistoryInput)
    recent_labs: RecentLabsInput = Field(default_factory=RecentLabsInput)

    @property
    def studies_summary(self) -> dict[str, Any]:
        """Tiny derived view used by procedure-family rules (e.g. CABG × low EF)."""
        ef_values = [
            s.ejection_fraction for s in self.recent_labs.studies
            if s.type == "ECHO" and s.ejection_fraction is not None
        ]
        lowest_ef = min(ef_values) if ef_values else None
        return {
            "lowest_ef": lowest_ef,
            "low_ef_30": lowest_ef is not None and lowest_ef < 30,
            "low_ef_40": lowest_ef is not None and lowest_ef < 40,
        }
