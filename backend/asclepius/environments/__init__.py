"""Asclepius V5 — Clinical RL Environments (agentic tier).

A runnable, Gymnasium-standard clinical RL environment (PRD §4.5) built ON the
existing V4 case pipeline. An agent works a case with tools, is scored on its
trajectory + end state by a physician-trained reward function, and is annotated
step-by-step by a board-certified physician. Emits Centaur-format
prompt+trajectory JSON as a byproduct.

Additive to the codebase — V1–V4 flows are byte-for-byte unchanged. Every V5
surface gates on ``portal_version == "v5"`` (constants.ENV_PORTAL_VERSION),
NEVER on ``isAssisted()``.

Build order (PRD §11): schema → state/tools/env → rollout → verify →
two-frontier + difficulty → physician annotation → reward_model → catalog →
real-de-identified/outcome.
"""

from __future__ import annotations

from .env import ClinicalEnv, VectorClinicalEnv
from .schema import PhysicianAnnotation, TrajectoryRecord, Verification

__all__ = [
    "ClinicalEnv",
    "VectorClinicalEnv",
    "TrajectoryRecord",
    "Verification",
    "PhysicianAnnotation",
    "build_environment",
    "compile_environment",
    "CompileError",
]

from .compile_env import CompileError, compile_environment  # noqa: E402


def build_environment(case, *, task_type: str, question: str = "", **kw):
    """Convenience wrapper: validate a case dict → compile it into a runnable
    environment spec (PRD §8.4). Raises ``CompileError`` for a real case that
    cannot be honestly graded deterministically."""
    return compile_environment(case, task_type=task_type, question=question, **kw)
