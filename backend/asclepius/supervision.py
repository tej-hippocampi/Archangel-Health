"""Supervision-type models (Buyer Response PRD §8, §9).

The buyer's two sharpest criticisms — "both candidate answers were model-generated"
(§8, the signal ceiling) and "single-turn pairwise preference, no verifiable outcome"
(§9, the RLHF-vs-RLVR ladder) — are met by making the ceiling and the ladder position
LEGIBLE per record, and by capturing the one physician-authored datum that turns a
preference label into a verifiable reward: the decisive action.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict


class TaskAnswerMode(str, Enum):
    """How the reference answer(s) for a task were produced (Buyer Response PRD §8)."""

    MODEL_PAIR = "model_pair"                    # current V3: A/B both model-generated
    PHYSICIAN_AUTHORED = "physician_authored"    # physician writes the reference answer
    PHYSICIAN_CORRECTED = "physician_corrected"  # physician rewrites the model's answer
    PHYSICIAN_FLAWED = "physician_flawed"        # physician writes the targeted wrong answer


class SupervisionType(str, Enum):
    """Where a record sits on the supervision ladder (Buyer Response PRD §9.3)."""

    PAIRWISE_PREFERENCE = "pairwise_preference"
    PROCESS_SUPERVISION = "process_supervision"
    ENVIRONMENT_VERIFIABLE = "environment_verifiable"


class DecisiveAction(BaseModel):
    """The action a correct trajectory MUST contain (Buyer Response PRD §9.2) — the
    verifiable outcome that turns a preference label into an RLVR reward.
    Physician-authored, one question at annotation time.

    For the PGNMID case: 'pronase-digested paraffin IF'. An agent that answers
    correctly without ordering it guessed, and a reward function that cannot tell those
    apart teaches guessing."""

    model_config = ConfigDict(extra="forbid")

    action: str
    tool_name: str
    must_precede_final_answer: bool = True
    rationale: str = ""
    physician_authored: bool = True


def decisive_action_satisfied(trajectory_tool_calls, action: DecisiveAction) -> bool:
    """True when the trajectory contains the decisive action before its final answer —
    the boolean, human-free verifier (Buyer Response PRD §9). ``trajectory_tool_calls``
    is an ordered list of tool-call names (the final answer is the sentinel
    ``"final_answer"`` when present)."""
    calls = list(trajectory_tool_calls or [])
    want = (action.tool_name or "").strip().lower()
    if not want:
        return False
    seen_action = False
    for call in calls:
        name = str(call).strip().lower()
        if name == want:
            seen_action = True
        if name in ("final_answer", "final", "answer") and action.must_precede_final_answer:
            return seen_action
    return seen_action
