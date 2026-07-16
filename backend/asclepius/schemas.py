"""Pydantic request/response models for Asclepius (PRD §6).

Kept deliberately permissive (free-text-tolerant) on the wire; the strict
gating lives in ``validation.py`` so a malformed submission is captured + routed
to QA rather than rejected at the HTTP boundary (PRD §5 — "no lost submissions").
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Dict, List, Optional

from pydantic import AfterValidator, BaseModel, Field, model_validator

from asclepius.cases import ClinicalCase

# Internal evaluation-portal accounts often use non-deliverable / reserved
# domains (e.g. ``evaluator@asclepius.local``). Pydantic's ``EmailStr`` rejects
# those via ``email-validator``'s special-use list, so we use a permissive
# shape check + normalization instead of strict deliverability validation.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _normalize_email(value: str) -> str:
    value = (value or "").strip().lower()
    if not _EMAIL_RE.match(value):
        raise ValueError("value is not a valid email address")
    return value


EmailLike = Annotated[str, AfterValidator(_normalize_email)]


def _normalize_login_identifier(value: str) -> str:
    """Login accepts an email OR a plain username/id (e.g. the ``mockadmin``
    sandbox account). We only normalize (strip + lowercase) — no email-format
    check, so a username without an ``@`` is a valid login. Account CREATION still
    uses ``EmailLike`` (real users need real emails); this leniency is login-only,
    and a bad identifier simply fails auth (401), never a 422."""
    return (value or "").strip().lower()


LoginIdentifier = Annotated[str, AfterValidator(_normalize_login_identifier)]


# ─── Auth ────────────────────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    # Named ``email`` for backward compatibility with existing clients; accepts an
    # email or a username/id (see ``_normalize_login_identifier``).
    email: LoginIdentifier
    password: str


class SsoRequest(BaseModel):
    """A doctor-portal ``tenant_staff`` JWT, exchanged for an Asclepius session."""

    token: str


class UserOut(BaseModel):
    id: str
    email: str
    role: str
    specialty: Optional[str] = None
    board_cert: Optional[str] = None
    years_experience: Optional[int] = None


class TokenOut(BaseModel):
    token: str
    user: UserOut


class CreateUserRequest(BaseModel):
    email: EmailLike
    password: str
    role: str = "evaluator"
    specialty: Optional[str] = None
    board_cert: Optional[str] = None
    years_experience: Optional[int] = None


class RealDataApprovalRequest(BaseModel):
    """Grant/revoke a contributor's V4 real-case access (EHR PRD §9.5)."""

    approved: bool


# ─── Real EHR ingestion (EHR PRD §4, §8, §9) ──────────────────────────────────
class UploadLinkRequest(BaseModel):
    """Mint a tokenized, expiring partner upload link."""

    partner_id: str
    partner_label: Optional[str] = None
    specialty: str = "nephrology"
    expires_hours: int = 72          # capped 1..720 server-side
    one_time: bool = True
    max_bytes: Optional[int] = None  # capped to the global ingest limit


class QuarantineOverrideRequest(BaseModel):
    """Documented admin override of verifier findings. The deidentify() hard
    guard still applies and cannot be overridden."""

    reason: str


class PromoteCaseRequest(BaseModel):
    """Promote an ingested real case to a gradable V4 task (EHR PRD §9)."""

    question: str
    max_labels: int = 1
    grounding_mode: Optional[str] = None
    independent_mode: Optional[str] = None


class UploadPromoteRequest(BaseModel):
    """Upload-scoped promotion (prepare a sample, then promote the rest). The
    clinical question is OPTIONAL — when blank a sensible per-specialty default is
    used, so the admin can promote a whole partner file in one click."""

    question: Optional[str] = None
    max_labels: int = 1
    grounding_mode: Optional[str] = None
    independent_mode: Optional[str] = None


# ─── Evidence anchors (opt §1.2 — the medical premium) ────────────────────────
class EvidenceAnchor(BaseModel):
    """A citation grounding a judgment/step in a clinical source.

    ``{citation_text, source_type, identifier}`` — e.g. KDIGO 2024 §3.2, a PMID,
    or a DOI. Captured with one keystroke in the UI; carried onto every record so
    grounded data can be filtered to a premium tier and verified by the critic.
    """

    citation_text: Optional[str] = None
    source_type: Optional[str] = None  # guideline | primary_literature | expert_consensus | fda_label | other
    identifier: Optional[str] = None  # e.g. "KDIGO 2024", "PMID:12345678", "DOI:..."
    # Library source URL, when the anchor came from an auto-suggested citation (WS3).
    url: Optional[str] = None
    # True when the clinician explicitly CONFIRMED an auto-suggested citation
    # (Seamless PRD WS3) — never set implicitly. Lets buyers/QA distinguish a
    # confirmed library citation from a hand-typed one.
    citation_confirmed: Optional[bool] = None


# ─── Tasks (admin-loaded input) ───────────────────────────────────────────────
class CandidateAnswer(BaseModel):
    id: str
    text: str
    # generator_model is stored server-side only and NEVER serialized to the
    # blinded eval screen (PRD §4.1, §6.1).
    generator_model: Optional[str] = None
    # Candidate source (FEAT-1): "generated" (our engine) | "baseline" (a real
    # frontier model's verbatim cold answer). ``baseline_model`` names it. BOTH are
    # SERVER-SIDE ONLY — never sent to the blinded evaluator screen, same rule as
    # generator_model, so the A/B stays blind.
    source: Optional[str] = None            # "generated" | "baseline"
    baseline_model: Optional[str] = None


class TaskIn(BaseModel):
    task_id: Optional[str] = None
    specialty: str = "general"
    difficulty: str = "medium"
    capture_reasoning: bool = False
    source: str = "lab_supplied"
    prompt: str
    candidate_answers: List[CandidateAnswer] = Field(default_factory=list)
    # Allow the same task to be labeled by N evaluators (>=2 enables IAA).
    max_labels: int = 1
    # Grounding Mode (opt §1.2): "optional" (default) | "required" (premium SKU).
    grounding_mode: str = "optional"
    # Stage-2 capture mode (Speed Optimization, Feature 1): "stance" (quick
    # pre-reveal take) | "full" (long-form blind ideal answer — premium/eval
    # batches). None → inherited from the batch/buyer-request constraint, else
    # the global default (stance) at insert time.
    independent_mode: Optional[str] = None
    # Links the task back to the buyer request that spawned it (opt §2.5).
    buyer_request_id: Optional[str] = None
    # Value-per-Minute (PRD B3): optional ADMIN routing hint ("premium" |
    # "on_policy" | "eval" | "standard"). Free-text-tolerant and never gates
    # capture — value-aware routing scores from the ESTIMATED value of the task's
    # attributes, not this label; the tier is a human annotation for the queue.
    value_tier: Optional[str] = None
    # Multimodal clinical case (Synthetic Multimodal Cases PRD §5). Optional +
    # fully back-compatible: no case → today's text behavior exactly. The
    # ``modality`` flag lets a buyer/frontend branch without inspecting the case.
    # ``case`` may carry internal ground_truth on the wire (admin upload); it is
    # stripped by ``cases.public_case`` before blinding/shipping.
    case: Optional[ClinicalCase] = None
    modality: str = "text"  # "text" | "multimodal"


class TaskUploadRequest(BaseModel):
    tasks: List[TaskIn]


class CandidateGenRequest(BaseModel):
    prompt: str
    specialty: str = "general"
    difficulty: str = "medium"
    capture_reasoning: bool = False
    max_labels: int = 1
    grounding_mode: str = "optional"


class GenerationRequest(BaseModel):
    """Admin "Generate N nephrology tasks" (Seedmaker, PRD §10)."""

    count: int = 10
    # Optional difficulty weighting, e.g. {"hard": 0.6, "medium": 0.4}.
    difficulty_mix: Optional[Dict[str, float]] = None
    capture_reasoning: bool = False
    grounding_mode: str = "optional"
    independent_mode: str = "stance"
    max_labels: int = 1
    # Stamp generated tasks back to the buyer request that asked for them.
    buyer_request_id: Optional[str] = None
    # Multimodal clinical cases (Synthetic Multimodal Cases PRD): when true, the
    # engine generates from the specialty's multimodal archetypes (case-gen +
    # Stage 3c case judge) instead of plain prompt-gen.
    multimodal: bool = False


# ─── Submission (raw, what the doctor produced) (PRD §6.2) ────────────────────
class ReasoningStep(BaseModel):
    step: int
    # The CONFIRMED-or-CORRECTED step the expert stands behind (the gold).
    text: str
    # Edit-to-Correct (Reasoning Capture v2): the AI's split step BEFORE the
    # doctor edited it — the negative half of a step-level preference pair. None
    # when the step was authored from scratch (AI omitted it).
    original_text: Optional[str] = None
    # The doctor edited this step to correct it (text diverged from original).
    corrected: bool = False
    # The doctor explicitly endorsed the AI's step as-is (silence ≠ endorsement).
    confirmed: bool = False
    # Manually inserted — an authored step the AI omitted entirely.
    added: bool = False
    # Why the edited step was wrong — one of STEP_CORRECTION_REASONS. Drives the
    # derived ``label`` (minor_wording → neutral, anything else → bad).
    correction_reason: Optional[str] = None
    # PRM800K-style per-step label (opt §1.1): good | neutral | bad. Now DERIVED
    # from the confirm/correct action rather than hand-tapped.
    label: Optional[str] = None
    # Optional numeric reward accompanying the label.
    step_reward: Optional[float] = None
    # Per-step evidence anchor (opt §1.2) — required for each step in
    # grounding_mode=required reasoning tasks. Kept as the back-compat SINGULAR
    # alias for ``evidence_anchors[0]`` (BUG-3b); ``evidence_anchors`` is the
    # multi-citation list the UI now writes ("+ Add another citation").
    evidence_anchor: Optional[EvidenceAnchor] = None
    evidence_anchors: List[EvidenceAnchor] = Field(default_factory=list)
    # One-line "what's off?" critique on a non-good step (Eval Flow Upgrade §4) —
    # the premium per-step error signal for PRM training.
    critique: Optional[str] = None
    # Model-suggested label from /reasoning/pregrade (Speed Optimization §2),
    # stored ALONGSIDE the human ``label`` so override rate is monitorable.
    # Never a substitute for the explicit confirm/correct action.
    suggested_label: Optional[str] = None
    # The pre-grader's one-line critique suggestion accompanying a suggested-bad
    # step (shown as a hint; the human's ``critique`` stays the final signal).
    suggested_critique: Optional[str] = None
    # Back-compat free-text tag (kept; ``label`` supersedes it for PRM data).
    tag: Optional[str] = None


class ChosenRevision(BaseModel):
    edited: bool = False
    revised_text: Optional[str] = None
    why_better_tags: List[str] = Field(default_factory=list)
    why_better_notes: Optional[str] = None
    # Evidence anchor on the "why it's better" rationale (opt §1.2). Singular is
    # the back-compat alias for ``evidence_anchors[0]`` (BUG-3b).
    evidence_anchor: Optional[EvidenceAnchor] = None
    evidence_anchors: List[EvidenceAnchor] = Field(default_factory=list)


class RejectedCritique(BaseModel):
    error_tags: List[str] = Field(default_factory=list)
    severities: Dict[str, str] = Field(default_factory=dict)
    why_worse: Optional[str] = None
    # Optional evidence anchor per error tag (opt §1.2): {error_tag: anchor}.
    error_tag_anchors: Dict[str, EvidenceAnchor] = Field(default_factory=dict)
    # Structured-first capture (Speed Optimization §6): one-tap reason per
    # selected error tag ({tag: reason}, reason from ERROR_TAG_REASONS) so the
    # diagnostic "why" is captured without typing.
    error_tag_reasons: Dict[str, str] = Field(default_factory=dict)


class FromScratch(BaseModel):
    ideal_answer: str = ""
    approach_notes: Optional[str] = None
    reasoning_steps: List[ReasoningStep] = Field(default_factory=list)
    # Evidence anchor on the approach/ideal-answer rationale (opt §1.2). Singular
    # is the back-compat alias for ``evidence_anchors[0]`` (BUG-3b).
    evidence_anchor: Optional[EvidenceAnchor] = None
    evidence_anchors: List[EvidenceAnchor] = Field(default_factory=list)


class RubricCriterion(BaseModel):
    """One weighted criterion of a HealthBench-shaped scoring rubric (FEAT-2).

    ``points`` is signed: POSITIVE for "a correct answer must include this",
    NEGATIVE for "a correct answer must never say this". ``axis`` is one of
    RUBRIC_AXES. ``source`` records how the criterion was seeded (e.g.
    ``error_tag:dosing_error``, ``why_better:safer``, ``good_step``,
    ``corrected_step``, or ``manual``) so a buyer can trace provenance. Nothing is
    auto-applied — the doctor confirms/edits every criterion before it ships.

    ``tier`` (Two-Model PRD Workstream B, V3/V4) is the criticality band —
    critical | important | helpful — derived from |points| when not supplied by the
    client. ``critical`` is the derived convenience flag (tier == "critical"); a
    "critical negative" (critical + points<0) is the failure a correct answer must
    never commit, and the grader hard-fails on it."""

    text: str = ""
    points: float = 0.0
    axis: Optional[str] = None
    source: Optional[str] = None
    # Criticality tier — filled from |points| in the validator when absent.
    tier: Optional[str] = None
    # Derived: True when tier == "critical". Never trusted from the wire (recomputed).
    critical: bool = False

    @model_validator(mode="after")
    def _derive_tier(self) -> "RubricCriterion":
        # Tier ALWAYS follows |points| — the wire ``tier`` is only a hint and is never
        # trusted (a client could send points=-9 with tier="helpful"). Recompute
        # unconditionally so tier/critical can't drift from the weight, matching
        # rubric.normalize_rubric / has_critical_negative / the exported score.py.
        from asclepius.constants import tier_for_points
        self.tier = tier_for_points(self.points)
        self.critical = self.tier == "critical"
        return self


class PromptReview(BaseModel):
    """Stage-1 clinician sign-off on the prompt itself (Eval Flow Upgrade §2).

    A ``valid`` verdict upgrades provenance from AI-drafted to clinician-reviewed
    and carries onto every shipped record. A ``flagged`` verdict short-circuits
    capture: the task is flagged for admin review and 0 records are produced.
    """

    reviewed: bool = False
    verdict: Optional[str] = None  # "valid" | "flagged"
    note: Optional[str] = None
    reviewed_at: Optional[str] = None


class IndependentAnswer(BaseModel):
    """Stage-2 blind capture — written BEFORE the A/B candidates are revealed
    (Eval Flow Upgrade §3). ``kind`` (Speed Optimization §1) distinguishes the
    default quick ``stance`` (anti-anchoring signal; the gold SFT answer comes
    from the refined chosen answer) from a ``full`` blind ideal answer
    (uncontaminated premium SFT). The server stamps ``kind`` from the task's
    ``independent_mode`` at reveal — it is never trusted from the client.
    """

    text: str = ""
    # "stance" | "full". None on the wire → resolved from the task's
    # ``independent_mode`` at packaging (the reveal commit stamps it anyway).
    kind: Optional[str] = None
    # Which portal flow captured this (Asclepius V2 launch): "v1" classic |
    # "v2" assisted. Sent at reveal so the server can stamp the capture kind
    # (V1 always writes the full blind answer, even on stance-default tasks).
    portal_version: Optional[str] = None
    # Singular is the back-compat alias for ``evidence_anchors[0]`` (BUG-3b).
    evidence_anchor: Optional[EvidenceAnchor] = None
    evidence_anchors: List[EvidenceAnchor] = Field(default_factory=list)
    captured_at: Optional[str] = None


class SubmissionIn(BaseModel):
    # Client-generated so submit is idempotent across mid-task refresh (PRD §10).
    submission_id: Optional[str] = None
    task_id: str
    # Optional so a flagged-prompt submission (no A/B judgment) validates on the
    # wire; the normal path still hard-checks ``verdict in VERDICTS`` in the router.
    verdict: Optional[str] = None
    # Stage-1/Stage-2 gated-capture fields (Eval Flow Upgrade §2, §3).
    prompt_review: Optional[PromptReview] = None
    independent_answer: Optional[IndependentAnswer] = None
    chosen_id: Optional[str] = None
    rejected_id: Optional[str] = None
    chosen_revision: Optional[ChosenRevision] = None
    rejected_critique: Optional[RejectedCritique] = None
    from_scratch: Optional[FromScratch] = None
    reasoning_steps: List[ReasoningStep] = Field(default_factory=list)
    # Rubric capture (FEAT-2): the weighted +/− criteria the doctor CONFIRMED
    # (auto-seeded from their tags, then edited). Optional + free-text-tolerant;
    # an empty list means no rubric was captured for this judgment.
    rubric: List[RubricCriterion] = Field(default_factory=list)
    confidence: str = "medium"
    time_spent_sec: int = 0
    # Which portal flow produced this submission ("v1" classic | "v2"
    # assisted) — stamped onto the submission row + every record so admin and
    # buyers can tell V1 data from V2 data.
    portal_version: Optional[str] = None
    # Model-assisted pre-labeling audit block (Speed Optimization §2):
    # {prelabeled, suggested_verdict, suggested_error_tags, suggested_rationale,
    #  suggested_step_labels}. Stored alongside the human finals so override
    # rate is monitorable and rubber-stamping is catchable. Suggestions are
    # NEVER applied server-side — this is provenance only.
    assist: Optional[Dict[str, Any]] = None


class PrelabelRequest(BaseModel):
    """Ask the critic for a pre-label suggestion on a task (Speed Optimization
    §2). Gated behind the independent-answer commit (anti-peeking)."""

    task_id: str


class CiteRequest(BaseModel):
    """Ask for auto-suggested citations for a rationale or reasoning step
    (Seamless PRD WS3). ``text`` is the clinician's written rationale/step;
    ``specialty`` scopes the citation library. Post-reveal, so no anti-peeking gate."""

    text: str
    specialty: str = "nephrology"
    k: int = 3


class ReasoningSplitRequest(BaseModel):
    """Split a chosen/ideal answer into ordered reasoning steps for tap-to-grade
    (Eval Flow Upgrade §4). ``prompt``/``specialty`` give the splitter context."""

    text: str
    prompt: str = ""
    specialty: str = "general"


class SubmissionResult(BaseModel):
    submission_id: str
    status: str
    issues: List[str] = Field(default_factory=list)
    record_count: int = 0
    critic: Optional[Dict[str, Any]] = None
    agreement_score: Optional[float] = None
    # Value-per-Minute (PRD Part A): the estimated sellable dollar value of this
    # judgment (realized = bankable to one buyer; projected = × reuse forecast).
    # Measurement only — never a buyer-facing record field.
    value_estimate_usd: Optional[float] = None
    value_estimate_projected_usd: Optional[float] = None


# ─── QA ───────────────────────────────────────────────────────────────────────
class QADecisionRequest(BaseModel):
    decision: str  # "approve" | "reject"
    notes: Optional[str] = None


# ─── Export ─────────────────────────────────────────────────────────────────--
class ExportRequest(BaseModel):
    # Target buyer profile (field-mapping + schema). Defaults to "default".
    profile: str = "default"
    specialty: Optional[str] = None
    difficulty: Optional[str] = None
    record_type: Optional[str] = None
    since: Optional[str] = None  # ISO date/datetime lower bound (created_at)
    until: Optional[str] = None  # ISO date/datetime upper bound
    # Premium-tier filter: only export grounded (evidence-anchored) records.
    grounded_only: bool = False
    # Confidence floor: low < medium < high.
    confidence_floor: Optional[str] = None
    # Minimum inter-annotator agreement score (0..1) on the record.
    min_agreement: Optional[float] = None
    buyer_request_id: Optional[str] = None
    # V1/V2 cohort filter (Asclepius V2): "v1" | "v2" | None (both).
    portal_version: Optional[str] = None
    # Modality cohort filter (Multimodal PRD §8): "text" | "multimodal" | None (both).
    modality: Optional[str] = None
    # Case provenance filter: "synthetic" | "real_deid" | None (any).
    case_source: Optional[str] = None
    # Benchmark opt-in (Multimodal PRD §7): bundle the held-out case answer key
    # under ``answer_key``. OFF by default — the answer key is withheld otherwise.
    include_answer_key: bool = False
    # Mock/sandbox contributor records are hard-excluded from every export by
    # default; set true only to deliberately include them (internal demo tool).
    include_mock: bool = False
    note: Optional[str] = None
    # Re-include already-shipped records so the bundle can be re-downloaded.
    include_exported: bool = False


# ─── Buyers & buyer requests (opt §2.5) ───────────────────────────────────────
class BuyerIn(BaseModel):
    name: str
    contact: Optional[str] = None
    export_profile: str = "default"
    notes: Optional[str] = None


class BuyerRequestIn(BaseModel):
    buyer_id: str
    source: str = "internal_prompt_bank"  # internal_prompt_bank | lab_supplied
    export_profile: str = "default"
    # Requested constraints (opt §2.5): specialty, difficulty mix,
    # capture_reasoning, grounding_mode, volume.
    specialty: Optional[str] = None
    difficulty: Optional[str] = None
    capture_reasoning: bool = False
    grounding_mode: str = "optional"
    # Premium/eval buyers can request the full blind ideal answer (Speed Opt §1).
    independent_mode: str = "stance"
    volume: Optional[int] = None
    max_labels: int = 1
    # Buyer-supplied prompts and/or A/B AI responses to grade (Mode B).
    prompts: List[TaskIn] = Field(default_factory=list)
    note: Optional[str] = None


class BuyerRequestStatusUpdate(BaseModel):
    status: str


class BatchFromRequest(BaseModel):
    """Spin up a task batch from a buyer request in one step (opt §2.5)."""

    # When the request carries no uploaded prompts, generate ``count`` tasks from
    # the internal prompt bank (still our prompts, their spec).
    count: int = 0
    prompts: List[TaskIn] = Field(default_factory=list)


# ─── Contributors view + tiered export ────────────────────────────────────────
class ContributorCredentialsIn(BaseModel):
    """Admin upsert of a contributor's credential profile. ``ship`` is the Tier A
    (buyer-facing) attribute block; ``verify`` is the Tier B private vault."""

    organization: Optional[str] = None
    role_title: Optional[str] = None
    blurb: Optional[str] = None
    credentials_verified: bool = False
    ship: Dict[str, Any] = Field(default_factory=dict)
    verify: Dict[str, Any] = Field(default_factory=dict)


class ScopedExportRequest(BaseModel):
    """Export Data scoped to one contributor or one organization. Always Tier A
    only; the buyer profile + Tier B leak gate enforce the wall."""

    profile: str = "default"
    note: Optional[str] = None
    # A scoped export means "package everything this contributor / organization
    # has labelled" — a complete, re-runnable corpus delivery, not the bulk
    # incremental shipping pipeline. So already-exported records are re-included
    # by default; otherwise the button would return "no export-ready records" the
    # moment any earlier export (incl. a prior org-wide "Export all org data")
    # marked those records exported. Callers can still pass false to scope to
    # only the not-yet-shipped delta.
    include_exported: bool = True
    # Time-window + single-task scoping (Exports rework): package just one task
    # the contributor completed (submission_id), or everything in a day / week /
    # all-time window (since/until, ISO created_at bounds).
    since: Optional[str] = None
    until: Optional[str] = None
    submission_id: Optional[str] = None


class CredentialSummaryRequest(BaseModel):
    """Generate a Further Credential Summary (verification dossier). ``acknowledged``
    must be true — the §9 non-circumvention notice is a click-through gate."""

    recipient: Optional[str] = None
    acknowledged: bool = False


# ─── Data Provider Portal — email+password door (EHR PRD §4) ──────────────────
class DataProviderInviteRequest(BaseModel):
    """Admin invites a data provider (Exports → Data Providers)."""

    email: EmailLike
    org_name: Optional[str] = None
    specialty: Optional[str] = None
    note: Optional[str] = None


class ProviderPasswordRequest(BaseModel):
    """Provider forced/normal password reset. ``current_password`` is required for a
    normal change; on the first forced reset the Bearer token is the proof (the
    temp password was already consumed at login), so it may be blank."""

    new_password: str
    current_password: str = ""


class BuyerDeliveryRequest(BaseModel):
    """Admin sends a dataset to a buyer's secure workspace. Scope the data by one
    or more organizations (checkbox multi-select) and/or a time window, package it
    with the chosen profile/format, and deliver it to the buyer by email."""

    buyer_name: str
    buyer_email: EmailLike
    organizations: List[str] = []
    since: Optional[str] = None
    until: Optional[str] = None
    profile: str = "default"
    data_format: Optional[str] = None
    note: Optional[str] = None
    include_exported: bool = True
