"""V5 Clinical RL Environments — the trajectory / step / verification schema.

This is the Centaur contract (PRD §1). The top-level keys and the step ``type``
vocabulary are frozen — a record must be drop-in for a frontier lab's pipeline
(PRD §13: "Do not deviate from Centaur's ``type`` vocabulary or top-level keys").

``verification`` and ``provenance`` are OMITTABLE for the raw-first export
(PRD §1, §9): ``--mode raw`` ships only ``task_id/specialty/task_type/prompt/
trajectory``. ``graded`` adds ``verification``; ``expert`` adds the physician
annotation layer (a sibling key, not part of Centaur's frozen core).

Pydantic v2, mirroring ``cases.py`` style (BaseModel + ConfigDict + Field).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..constants import (
    ENV_ACTION_JUDGMENTS,
    ENV_STEP_LABELS,
    ENV_STEP_TYPES,
    ENV_TASK_TYPES,
)

# ─── Trajectory steps (PRD §1) ────────────────────────────────────────────────


class TrajectoryStep(BaseModel):
    """One step in the agent's trajectory. ``type`` ∈ {thought, tool_call,
    observation, final_output} — Centaur's exact vocabulary.

    * ``thought`` / ``observation`` / ``final_output`` carry ``content`` (a string).
    * ``tool_call`` carries ``tool`` (name) + ``input`` (dict of args).
    """

    model_config = ConfigDict(extra="allow")  # forward-compatible with lab extensions

    step: int
    type: str
    content: Optional[str] = None
    tool: Optional[str] = None
    input: Optional[Dict[str, Any]] = None

    def validate_type(self) -> "TrajectoryStep":
        if self.type not in ENV_STEP_TYPES:
            raise ValueError(f"invalid step type {self.type!r}; must be one of {ENV_STEP_TYPES}")
        return self


# ─── Verification (PRD §1, §5) ────────────────────────────────────────────────


class VerificationCheck(BaseModel):
    """One verifier sub-check. ``type`` ∈ {deterministic, critical_negative,
    rubric, outcome}. Deterministic/critical checks carry ``passed`` (bool);
    rubric/outcome checks carry ``score`` (0..1)."""

    model_config = ConfigDict(extra="allow")

    id: str
    type: str
    passed: Optional[bool] = None
    score: Optional[float] = None
    detail: Optional[str] = None


class Verification(BaseModel):
    """Post-episode scoring block (PRD §5). ``method`` names the reward tier;
    ``checks`` are the per-check results; ``reward`` is the composed scalar in
    [0,1] (0 on a critical-negative hard-fail)."""

    model_config = ConfigDict(extra="allow")

    method: str = "deterministic"
    checks: List[VerificationCheck] = Field(default_factory=list)
    reward: float = 0.0
    # Dense per-step credit (PRD §5 "support per-step rewards"): step_index → reward.
    step_rewards: Optional[List[float]] = None
    hard_failed: bool = False


# ─── Provenance (PRD §1) ──────────────────────────────────────────────────────


class DifficultyProvenance(BaseModel):
    model_config = ConfigDict(extra="allow")
    empirical: Optional[float] = None
    graded_models: List[str] = Field(default_factory=list)
    measured: bool = False
    passes_gate: Optional[bool] = None


class Provenance(BaseModel):
    model_config = ConfigDict(extra="allow")
    case_source: str = "gold"
    difficulty: DifficultyProvenance = Field(default_factory=DifficultyProvenance)
    provider: Optional[str] = None
    contributor_verified: bool = False
    ab_source: Optional[str] = None
    case_id: Optional[str] = None


# ─── The full trajectory record (PRD §1) ──────────────────────────────────────


class TrajectoryRecord(BaseModel):
    """The Centaur artifact. One environment run emits one of these.

    ``verification`` / ``provenance`` are Optional so ``export_env.raw`` can drop
    them (PRD §1). ``physician_annotation`` is the ``expert``-tier sidecar (PRD §7)
    — NOT part of Centaur's frozen core, so it is emitted only in ``expert`` mode.
    """

    model_config = ConfigDict(extra="allow")

    task_id: str
    specialty: str
    task_type: str
    prompt: str
    trajectory: List[TrajectoryStep] = Field(default_factory=list)
    verification: Optional[Verification] = None
    provenance: Optional[Provenance] = None
    physician_annotation: Optional["PhysicianAnnotation"] = None

    def raw_dict(self) -> Dict[str, Any]:
        """Centaur's raw-first shape — ONLY the frozen core keys (PRD §9 raw)."""
        return {
            "task_id": self.task_id,
            "specialty": self.specialty,
            "task_type": self.task_type,
            "prompt": self.prompt,
            "trajectory": [s.model_dump(exclude_none=True) for s in self.trajectory],
        }


# ─── Physician annotation (PRD §7.1 / §7.3) ───────────────────────────────────


class StepLabel(BaseModel):
    """Per-step process-reward label. ``action_judgment`` is only meaningful for a
    ``tool_call`` step (PRD §7.1.1)."""

    model_config = ConfigDict(extra="allow")
    step: int
    label: str  # ∈ ENV_STEP_LABELS
    action_judgment: Optional[str] = None  # ∈ ENV_ACTION_JUDGMENTS


class EndStateRatification(BaseModel):
    model_config = ConfigDict(extra="allow")
    correct: bool = False
    safe: bool = True
    note: Optional[str] = None


class RewardRatification(BaseModel):
    model_config = ConfigDict(extra="allow")
    value: float = 0.0
    overrode_auto: bool = False
    auto_value: Optional[float] = None


class TrajectoryPreference(BaseModel):
    """Two-frontier blinded preference (PRD §7.1.7). ``chosen`` ∈ {A, B}."""

    model_config = ConfigDict(extra="allow")
    chosen: Optional[str] = None
    why: Optional[str] = None


class PhysicianAnnotation(BaseModel):
    """The crown-jewel V5 data (PRD §7). Persisted to
    ``env_runs.physician_annotation`` and shipped in ``expert`` mode."""

    model_config = ConfigDict(extra="allow")

    step_labels: List[StepLabel] = Field(default_factory=list)
    first_error_step: Optional[int] = None
    counterfactual_text: Optional[str] = None
    missed_actions: List[str] = Field(default_factory=list)
    failure_tags: List[str] = Field(default_factory=list)
    end_state_ratified: Optional[EndStateRatification] = None
    reward_ratified: Optional[RewardRatification] = None
    trajectory_preference: Optional[TrajectoryPreference] = None
    annotator_credential_ref: Optional[str] = None
    kappa_subset: bool = False


TrajectoryRecord.model_rebuild()


# ─── Validation helpers ───────────────────────────────────────────────────────


def valid_step_label(label: str) -> bool:
    return label in ENV_STEP_LABELS


def valid_action_judgment(j: Optional[str]) -> bool:
    return j is None or j in ENV_ACTION_JUDGMENTS


def valid_task_type(t: str) -> bool:
    return t in ENV_TASK_TYPES
