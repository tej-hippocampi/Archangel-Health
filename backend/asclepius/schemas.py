"""Pydantic request/response models for Asclepius (PRD §6).

Kept deliberately permissive (free-text-tolerant) on the wire; the strict
gating lives in ``validation.py`` so a malformed submission is captured + routed
to QA rather than rejected at the HTTP boundary (PRD §5 — "no lost submissions").
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Dict, List, Optional

from pydantic import AfterValidator, BaseModel, Field

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


# ─── Auth ────────────────────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    email: EmailLike
    password: str


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


# ─── Evidence anchors (opt §1.2 — the medical premium) ────────────────────────
class EvidenceAnchor(BaseModel):
    """A citation grounding a judgment/step in a clinical source.

    ``{citation_text, source_type, identifier}`` — e.g. KDIGO 2024 §3.2, a PMID,
    or a DOI. Captured with one keystroke in the UI; carried onto every record so
    grounded data can be filtered to a premium tier and verified by the critic.
    """

    citation_text: Optional[str] = None
    source_type: Optional[str] = None  # guideline | primary_literature | expert_consensus | other
    identifier: Optional[str] = None  # e.g. "KDIGO 2024", "PMID:12345678", "DOI:..."


# ─── Tasks (admin-loaded input) ───────────────────────────────────────────────
class CandidateAnswer(BaseModel):
    id: str
    text: str
    # generator_model is stored server-side only and NEVER serialized to the
    # blinded eval screen (PRD §4.1, §6.1).
    generator_model: Optional[str] = None


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
    # Links the task back to the buyer request that spawned it (opt §2.5).
    buyer_request_id: Optional[str] = None


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
    max_labels: int = 1
    # Stamp generated tasks back to the buyer request that asked for them.
    buyer_request_id: Optional[str] = None


# ─── Submission (raw, what the doctor produced) (PRD §6.2) ────────────────────
class ReasoningStep(BaseModel):
    step: int
    text: str
    # PRM800K-style per-step label (opt §1.1): good | neutral | bad.
    label: Optional[str] = None
    # Optional numeric reward accompanying the label.
    step_reward: Optional[float] = None
    # Per-step evidence anchor (opt §1.2) — required for each step in
    # grounding_mode=required reasoning tasks.
    evidence_anchor: Optional[EvidenceAnchor] = None
    # Back-compat free-text tag (kept; ``label`` supersedes it for PRM data).
    tag: Optional[str] = None


class ChosenRevision(BaseModel):
    edited: bool = False
    revised_text: Optional[str] = None
    why_better_tags: List[str] = Field(default_factory=list)
    why_better_notes: Optional[str] = None
    # Evidence anchor on the "why it's better" rationale (opt §1.2).
    evidence_anchor: Optional[EvidenceAnchor] = None


class RejectedCritique(BaseModel):
    error_tags: List[str] = Field(default_factory=list)
    severities: Dict[str, str] = Field(default_factory=dict)
    why_worse: Optional[str] = None
    # Optional evidence anchor per error tag (opt §1.2): {error_tag: anchor}.
    error_tag_anchors: Dict[str, EvidenceAnchor] = Field(default_factory=dict)


class FromScratch(BaseModel):
    ideal_answer: str = ""
    approach_notes: Optional[str] = None
    reasoning_steps: List[ReasoningStep] = Field(default_factory=list)
    # Evidence anchor on the approach/ideal-answer rationale (opt §1.2).
    evidence_anchor: Optional[EvidenceAnchor] = None


class SubmissionIn(BaseModel):
    # Client-generated so submit is idempotent across mid-task refresh (PRD §10).
    submission_id: Optional[str] = None
    task_id: str
    verdict: str
    chosen_id: Optional[str] = None
    rejected_id: Optional[str] = None
    chosen_revision: Optional[ChosenRevision] = None
    rejected_critique: Optional[RejectedCritique] = None
    from_scratch: Optional[FromScratch] = None
    reasoning_steps: List[ReasoningStep] = Field(default_factory=list)
    confidence: str = "medium"
    time_spent_sec: int = 0


class SubmissionResult(BaseModel):
    submission_id: str
    status: str
    issues: List[str] = Field(default_factory=list)
    record_count: int = 0
    critic: Optional[Dict[str, Any]] = None
    agreement_score: Optional[float] = None


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
    note: Optional[str] = None


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
