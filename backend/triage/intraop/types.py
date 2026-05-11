"""
Pydantic schemas for the Intra-Op Reassessment algorithm and form.

The form shape is *flat* — procedure-family-specific fields are exposed
as optional attributes on `IntraopForm` so the delta algorithm reads
them uniformly. The persistence layer is free to fold them into a
`procedure_specific` JSON blob on disk.

`field_origins` records per-field provenance (manual / AIMS / PDF) so
the UI can render confidence pills and the audit trail captures who
populated each value.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from triage.types import ProcedureFamily, Tier


# ─── Form lifecycle ──────────────────────────────────────────────────────────

IntraopFormStatus = Literal[
    "NEW",                           # created server-side at OR_ENDED, never opened
    "IN_PROGRESS",                 # RN draft; autosave active
    "READY_FOR_SURGEON_REVIEW",    # RN handed off; surgeon may edit + lock
    "LOCKED",                      # surgeon locked; reassessment fired
    "REOPENED",                    # admin / locking surgeon reopened
]


# ─── Field origin tracking (PRD §4.3) ────────────────────────────────────────

FieldOriginKind = Literal["MANUAL", "AUTO_POP_AIMS", "AUTO_POP_PDF"]


class FieldOrigin(BaseModel):
    """Provenance metadata recorded for every field write."""

    origin: FieldOriginKind
    source: Optional[str] = None
    """Free-form source identifier — e.g. 'aims:case-id-12345',
    'pdf:upload-id-abc', or 'manual'."""
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    """Set only for AUTO_POP_PDF. 1.0 = certain, 0.0 = absent."""
    populated_at: str
    confirmed_by: Optional[str] = None
    """User id of the surgeon who confirmed; required when origin is
    not MANUAL and confidence < 0.85 (PRD §6.4 / AC-6.3)."""
    confirmed_at: Optional[str] = None
    original_value: Optional[Any] = None
    """When a surgeon edits an auto-populated field, the auto value is
    preserved here (PRD edge case 3 / AC-6.4)."""


# ─── Form (flat shape; procedure-family fields are optional) ─────────────────

ConversionAnswer = Literal["YES", "NO", "N_A"]
VasopressorAnswer = Literal["NONE", "BRIEF", "SUSTAINED"]
AnesthesiaType = Literal["GENERAL", "REGIONAL", "MAC", "COMBINED"]
WeaningFromBypass = Literal["YES", "DIFFICULT", "REQUIRED_MECHANICAL_SUPPORT"]
PumpStrategy = Literal["ON_PUMP", "OFF_PUMP"]
SpinalApproach = Literal["ANTERIOR", "POSTERIOR", "COMBINED", "LATERAL"]
BoneGraftSource = Literal["AUTOGRAFT", "ALLOGRAFT", "SYNTHETIC", "COMBINED"]
LejrJoint = Literal["HIP", "KNEE"]
LejrSide = Literal["LEFT", "RIGHT", "BILATERAL"]
LejrFixation = Literal["CEMENTED", "CEMENTLESS", "HYBRID"]
HipFracturePattern = Literal[
    "INTRACAPSULAR", "INTERTROCHANTERIC", "SUBTROCHANTERIC", "FEMORAL_SHAFT",
]
HipFixation = Literal[
    "DYNAMIC_HIP_SCREW", "INTRAMEDULLARY_NAIL", "HEMIARTHROPLASTY", "TOTAL_HIP", "ORIF_OTHER",
]
WeightBearing = Literal["FULL", "PARTIAL", "TOE_TOUCH", "NON_WEIGHT_BEARING"]
BowelProcedureType = Literal[
    "PARTIAL_COLECTOMY", "TOTAL_COLECTOMY", "SMALL_BOWEL_RESECTION", "OTHER",
]
BowelApproach = Literal["OPEN", "LAPAROSCOPIC", "ROBOTIC"]


class IntraopForm(BaseModel):
    """The full intra-op form snapshot consumed by `compute_intraop_delta`.

    All clinical fields are Optional so the form can be saved (autosave)
    in any partial state. The lock endpoint enforces the 11 universal
    fields (PRD §4.1) before allowing transition to LOCKED.
    """

    model_config = ConfigDict(protected_namespaces=())

    # ─── 11 required universal fields (PRD §4.1) ───────────────────────────
    documented_complication: Optional[bool] = None
    ebl: Optional[int] = Field(default=None, ge=0, le=10000)
    transfusion_total_units: Optional[int] = Field(default=None, ge=0)
    conversion: Optional[ConversionAnswer] = None
    sustained_hypotension: Optional[bool] = None
    vasopressor_requirement: Optional[VasopressorAnswer] = None
    significant_arrhythmia: Optional[bool] = None
    or_duration_minutes: Optional[int] = Field(default=None, ge=0)
    difficult_airway: Optional[bool] = None
    net_fluid_balance: Optional[int] = None
    anesthesia_type: Optional[AnesthesiaType] = None

    # ─── Extended optional fields (PRD §4.2) ───────────────────────────────
    or_started_at: Optional[str] = None
    or_ended_at: Optional[str] = None
    asa_class: Optional[str] = None

    prbc_units: Optional[int] = Field(default=None, ge=0)
    platelet_units: Optional[int] = Field(default=None, ge=0)
    ffp_units: Optional[int] = Field(default=None, ge=0)
    cryo_units: Optional[int] = Field(default=None, ge=0)

    fluid_in: Optional[int] = None
    fluid_out: Optional[int] = None

    conversion_reason: Optional[str] = None
    hypoxia_event: Optional[bool] = None

    complication_types: Optional[list[str]] = None
    complication_description: Optional[str] = None

    procedural_aborted: Optional[bool] = None
    procedural_aborted_reason: Optional[str] = None

    # ─── Procedure-family-specific (flat, optional) ────────────────────────
    # LEJR
    lejr_joint: Optional[LejrJoint] = None
    lejr_side: Optional[LejrSide] = None
    lejr_fixation_type: Optional[LejrFixation] = None
    lejr_prosthesis_model: Optional[str] = None
    lejr_component_sizes: Optional[str] = None
    intraoperative_fracture: Optional[bool] = None
    fracture_location: Optional[str] = None

    # CABG
    number_of_grafts: Optional[int] = Field(default=None, ge=0, le=6)
    pump_strategy: Optional[PumpStrategy] = None
    aortic_cross_clamp_minutes: Optional[int] = Field(default=None, ge=0)
    cpb_time_minutes: Optional[int] = Field(default=None, ge=0)
    aortic_manipulation: Optional[bool] = None
    grafts_used: Optional[list[str]] = None
    weaning_from_bypass: Optional[WeaningFromBypass] = None

    # SPINAL_FUSION
    spinal_approach: Optional[SpinalApproach] = None
    number_of_levels_fused: Optional[int] = Field(default=None, ge=0, le=10)
    spinal_levels: Optional[list[str]] = None
    spinal_instrumentation: Optional[bool] = None
    bone_graft_source: Optional[BoneGraftSource] = None
    dural_tear: Optional[bool] = None
    neuromonitoring_used: Optional[bool] = None
    neuromonitoring_changes: Optional[bool] = None

    # HIP_FEMUR_FRACTURE
    hip_fracture_pattern: Optional[HipFracturePattern] = None
    hip_fixation_method: Optional[HipFixation] = None
    time_to_or_hours: Optional[float] = Field(default=None, ge=0)
    weight_bearing_status: Optional[WeightBearing] = None

    # MAJOR_BOWEL
    bowel_procedure_type: Optional[BowelProcedureType] = None
    bowel_approach: Optional[BowelApproach] = None
    anastomosis_performed: Optional[bool] = None
    anastomosis_location: Optional[str] = None
    ostomy_created: Optional[bool] = None
    contamination_class: Optional[int] = Field(default=None, ge=1, le=4)

    # ─── Origin tracking ───────────────────────────────────────────────────
    field_origins: dict[str, FieldOrigin] = Field(default_factory=dict)


# ─── Algorithm inputs / outputs ──────────────────────────────────────────────

class HospitalProcedureStats(BaseModel):
    """Per-hospital, per-family OR-time benchmarks (PRD §5.2).

    Ships as national-benchmark P90s; once 50+ cases per family are
    observed, the hospital's observed P90 replaces the default.
    """
    or_duration_p90_minutes: dict[str, int]
    """Map of family code (`LEJR`, `CABG`, …) to P90 in minutes."""


IntraopReasonKind = Literal["HARD", "SOFT", "INFO"]


class IntraopReason(BaseModel):
    """One contributor to the intra-op proposed tier — itemized for audit."""
    kind: IntraopReasonKind
    code: str
    label: str
    detail: Optional[str] = None


class IntraopDeltaResult(BaseModel):
    """Output of `compute_intraop_delta` (PRD §5.1)."""

    model_config = ConfigDict(protected_namespaces=())

    proposed_tier: Tier
    hard_upgrade_applied: bool
    upgrade_steps: int
    is_conservative_default: bool = False
    reasons: list[IntraopReason]
    model_version: str
    tuning_version: int = 1


# ─── Reassessment event (audit row written on every lock / cron) ────────────

ReassessmentTrigger = Literal["SURGEON_LOCK", "SYSTEM:CONSERVATIVE_DEFAULT", "ADMIN_REOPEN_RELOCK"]


class ReassessmentEvent(BaseModel):
    """Snapshot of one reassessment cycle. Persisted in
    `intraop_reassessments` and returned by the lock / cron paths."""

    model_config = ConfigDict(protected_namespaces=())

    id: str
    patient_id: str
    intraop_form_id: str

    form_snapshot: dict[str, Any]
    pre_or_current_tier: Tier
    proposed_tier: Tier
    final_tier: Tier
    hard_upgrade_applied: bool
    upgrade_steps: int
    reasons: list[IntraopReason]
    is_conservative_default: bool = False

    model_version: str
    tuning_version: int

    triggered_by: str
    triggered_at: str

    procedure_family: Optional[ProcedureFamily] = None
