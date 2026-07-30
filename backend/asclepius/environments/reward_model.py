"""The physician-trained process reward model (PRD §7.5).

Closes the loop: the physician step-labels + first-error + counterfactual data
(§7.1) are exactly the training set for a medical PRM — given a case + a partial
trajectory, predict the physician's step judgment. Once validated against a
held-out expert set, the PRM becomes the environment's RULER verifier for the
non-deterministic reasoning parts of §5, replacing the generic LLM-judge with a
*physician-aligned* reward that improves as annotation accrues.

Guardrails (PRD §7.5 — "or the reward gets hacked"):
  * the deterministic checks + critical-negative hard-fail (§5.1) remain a floor
    the PRM can NEVER override — the PRM only refines the reasoning subset;
  * a held-out physician-annotated test set gates the PRM: it may only score once
    its agreement with experts clears a threshold (``is_validated``);
  * the anti-gaming posture from ``grader_eval`` is preserved (the PRM scores
    reasoning engagement, not answer length).

This is a calibrated frequency model (Laplace-smoothed) rather than a heavy NN —
it is a real learn-from-ranked-expert-data reward model that improves with data,
trains in-process, and is transparent/auditable. A lab can swap a richer model
behind the same interface.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ..agreement import cohens_kappa
from ..constants import ENV_STEP_LABELS

_LABEL_VALUE = {"correct": 1.0, "suboptimal": 0.5, "wrong": 0.0}
# Minimum expert agreement (Cohen's κ) before the PRM is allowed to score (§7.5).
_MIN_KAPPA = 0.6
_MIN_TRAIN = 20  # step-labels required before a fit is meaningful


class PhysicianPRM:
    """A physician process-reward model fit from step-level annotations."""

    def __init__(self) -> None:
        # feature → {label: count}; a feature is a coarse bucket of a step.
        self._counts: Dict[str, Dict[str, float]] = {}
        self._n_train = 0
        self._kappa: Optional[float] = None
        self._holdout_accuracy: Optional[float] = None
        self._validated = False

    # ─── Feature extraction ────────────────────────────────────────────────────
    @staticmethod
    def _step_features(step: Dict[str, Any], action_judgment: Optional[str]) -> str:
        stype = step.get("type") or "thought"
        if stype == "tool_call":
            return f"tool_call|{step.get('tool')}|{action_judgment or 'na'}"
        return stype

    # ─── Training (PRD §7.5) ───────────────────────────────────────────────────
    def fit(self, samples: List[Tuple[Dict[str, Any], str, Optional[str]]]) -> "PhysicianPRM":
        """``samples`` = [(step_dict, physician_label, action_judgment)]. Builds
        the smoothed feature→label frequency table."""
        self._counts = {}
        n = 0
        for step, label, aj in samples:
            if label not in ENV_STEP_LABELS:
                continue
            feat = self._step_features(step, aj)
            self._counts.setdefault(feat, {l: 0.0 for l in ENV_STEP_LABELS})
            self._counts[feat][label] += 1.0
            n += 1
        self._n_train = n
        return self

    def _predict_label_dist(self, feat: str) -> Dict[str, float]:
        counts = self._counts.get(feat)
        if not counts:
            # Uninformative prior: mild optimism toward "correct".
            return {"correct": 0.5, "suboptimal": 0.3, "wrong": 0.2}
        total = sum(counts.values()) + len(ENV_STEP_LABELS)  # Laplace
        return {l: (counts.get(l, 0.0) + 1.0) / total for l in ENV_STEP_LABELS}

    def score_step(self, step: Dict[str, Any], action_judgment: Optional[str] = None) -> float:
        """Predicted physician value of one step in [0,1] (correct=1, wrong=0)."""
        dist = self._predict_label_dist(self._step_features(step, action_judgment))
        return round(sum(dist[l] * _LABEL_VALUE[l] for l in ENV_STEP_LABELS), 3)

    def score_trajectory(self, trajectory: List[Dict[str, Any]]) -> float:
        """The reasoning-subset reward for a whole trajectory in [0,1] — the mean
        predicted step value over the non-observation steps (observations aren't
        agent decisions)."""
        vals = [self.score_step(s) for s in trajectory if s.get("type") != "observation"]
        return round(sum(vals) / len(vals), 3) if vals else 0.5

    # ─── Validation gate (PRD §7.5) ────────────────────────────────────────────
    def evaluate(self, holdout: List[Tuple[Dict[str, Any], str, Optional[str]]]) -> Dict[str, Any]:
        """Measure agreement with a held-out expert-annotated set BEFORE the PRM is
        allowed to score (§7.5). Reports accuracy + Cohen's κ (reuse
        ``agreement.cohens_kappa``)."""
        pairs: List[Tuple[Optional[str], Optional[str]]] = []
        correct = 0
        for step, label, aj in holdout:
            dist = self._predict_label_dist(self._step_features(step, aj))
            pred = max(dist.items(), key=lambda kv: kv[1])[0]
            pairs.append((pred, label))
            if pred == label:
                correct += 1
        kappa = cohens_kappa(pairs) if pairs else None
        acc = round(correct / len(pairs), 3) if pairs else None
        self._kappa = kappa
        self._holdout_accuracy = acc
        self._validated = (self._n_train >= _MIN_TRAIN and kappa is not None and kappa >= _MIN_KAPPA)
        return {"n_train": self._n_train, "n_holdout": len(holdout),
                "kappa": kappa, "accuracy": acc, "validated": self._validated,
                "min_kappa": _MIN_KAPPA}

    def is_validated(self) -> bool:
        return self._validated

    # ─── The RULER verifier hook (behind the deterministic floor) ──────────────
    def as_rubric_score(self, env, verification: Dict[str, Any]) -> Optional[float]:
        """Return the PRM's reasoning-subset score for the env's trajectory, or
        ``None`` if the PRM is not yet validated (in which case the caller keeps
        the generic RULER judge). NEVER lifts a hard-failed episode — the
        deterministic floor wins (§7.5 guardrail)."""
        if not self._validated:
            return None
        if verification.get("hard_failed"):
            return 0.0
        return self.score_trajectory(getattr(env, "trajectory", []) or [])


# ─── Train from the annotation store (PRD §7.5) ───────────────────────────────
def build_training_samples(annotations: List[Dict[str, Any]]) -> List[Tuple[Dict[str, Any], str, Optional[str]]]:
    """Flatten stored physician annotations + their trajectories into
    (step, label, action_judgment) training samples."""
    samples: List[Tuple[Dict[str, Any], str, Optional[str]]] = []
    for ann in annotations:
        traj = {s.get("step"): s for s in (ann.get("trajectory") or [])}
        for sl in (ann.get("physician_annotation") or {}).get("step_labels") or []:
            step = traj.get(sl.get("step")) or {"type": "thought"}
            samples.append((step, sl.get("label"), sl.get("action_judgment")))
    return samples


def train_prm(annotations: List[Dict[str, Any]], *, holdout_frac: float = 0.3) -> Tuple[PhysicianPRM, Dict[str, Any]]:
    """Train + validate a PRM from a list of annotation records (each carrying its
    ``trajectory`` + ``physician_annotation``). Deterministic hash-free split by
    index so a re-run reproduces (PRD reproducibility bar)."""
    samples = build_training_samples(annotations)
    if not samples:
        prm = PhysicianPRM()
        return prm, {"n_train": 0, "n_holdout": 0, "kappa": None, "validated": False,
                     "note": "no annotation samples yet"}
    cut = max(1, int(len(samples) * (1 - holdout_frac)))
    train, holdout = samples[:cut], samples[cut:]
    prm = PhysicianPRM().fit(train)
    report = prm.evaluate(holdout or train)
    return prm, report
