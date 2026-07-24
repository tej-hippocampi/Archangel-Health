"""Service layer (PRD §10) — orchestrates compile → rollout → verify → persist.

Sits between the router and the environment primitives so the HTTP layer stays
thin. All persistence goes through ``AsclepiusStore.env_runs`` (PRD §10).

Source priority is enforced here (PRD §0.5): gold + real first; a raw synthetic
case is NEVER compiled into a shippable environment until it clears BOTH the
physician-validity and empirical-difficulty gates.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import catalog
from .compile_env import CompileError, compile_environment
from .rollout import measure_difficulty, rollout as _rollout, two_frontier_rollout
from .env import ClinicalEnv


# ─── Generate environments from source cases (PRD §3, §11.8) ──────────────────
def generate_from_gold(
    store, specialty: str, *, n: int = 5, task_type: Optional[str] = None,
) -> Dict[str, Any]:
    """Build N task environments from §0.5-validated GOLD cases (source priority
    #1 — what the first Centaur deliverable is built from). Persists one
    ``mode='generated'`` env_run per compiled environment."""
    from ..gold_cases import GOLD_CASE_SETS

    entries = GOLD_CASE_SETS.get(specialty, [])
    built: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for entry in entries[: max(1, n)]:
        case = entry.get("case") or {}
        tt = task_type or catalog.infer_default_task_type(entry)
        # The gold wrapper names the unsafe path (ai_failure_mode) and carries the
        # intended-flawed candidate answer — high-signal material for deriving the
        # per-case critical-negative flags (PRD §5.1) the verifier hard-fails on.
        hint = _critical_hint(entry)
        try:
            compiled = compile_environment(case, task_type=tt, question=entry.get("question") or "",
                                           critical_hint_text=hint)
        except CompileError as exc:
            skipped.append({"case_id": entry.get("case_id"), "reason": str(exc)})
            continue
        task_id = _env_task_id(specialty, tt, entry.get("case_id"))
        if store.get_environment(task_id):
            built.append({"task_id": task_id, "status": "exists"})
            continue
        row = store.insert_env_run(
            task_id=task_id, specialty=specialty, task_type=tt,
            case_id=entry.get("case_id"), case_source="gold",
            mode="generated", compiled=compiled,
        )
        built.append({"task_id": task_id, "run_id": row["run_id"], "status": "generated"})
    return {"specialty": specialty, "built": built, "skipped": skipped,
            "n_built": len([b for b in built if b.get("status") == "generated"])}


def generate_from_synthetic(
    store, specialty: str, cases: List[Dict[str, Any]], *, task_type: Optional[str] = None,
) -> Dict[str, Any]:
    """Compile synthetic-generated CANDIDATE cases into environments — the scaling
    path (PRD §0.5 source #3). Each must clear BOTH gates before it ships; here we
    compile + flag them ``needs_gates`` (the difficulty gate runs on rollout, the
    physician-validity gate is the annotation step). A raw generator output is a
    candidate, never a shippable environment (PRD §0.5, §13)."""
    built, skipped = [], []
    for case in cases:
        tt = task_type or "diagnostic_workup"
        try:
            compiled = compile_environment(case, task_type=tt, require_deterministic=False)
        except CompileError as exc:
            skipped.append({"case_id": case.get("case_id"), "reason": str(exc)})
            continue
        compiled["needs_gates"] = True  # physician-validity + measured difficulty pending
        task_id = _env_task_id(specialty, tt, case.get("case_id") or _rand_suffix())
        row = store.insert_env_run(
            task_id=task_id, specialty=specialty, task_type=tt,
            case_id=case.get("case_id"), case_source="synthetic",
            mode="generated", compiled=compiled,
        )
        built.append({"task_id": task_id, "run_id": row["run_id"], "status": "candidate"})
    return {"specialty": specialty, "built": built, "skipped": skipped}


# ─── Rollout (PRD §6, §11.3/§11.5) ────────────────────────────────────────────
async def run_rollout(
    store, task_id: str, *, two_frontier: bool = False, seed: Optional[int] = None,
    run_rubric: bool = True, measure_gate: bool = False,
) -> Dict[str, Any]:
    """Drive the environment with one provider (default) or both (two-frontier).
    Persists a ``mode='rollout'`` env_run per trajectory and returns the §1 records."""
    env_row = store.get_environment(task_id)
    if not env_row:
        raise ValueError(f"no environment for task_id {task_id!r} (generate it first)")
    compiled = env_row.get("compiled") or {}

    difficulty = None
    if measure_gate:
        difficulty = await measure_difficulty(compiled)

    persisted: List[Dict[str, Any]] = []
    if two_frontier:
        result = await two_frontier_rollout(compiled, seed=seed, run_rubric=run_rubric)
        for rec in result["records"]:
            persisted.append(_persist_rollout(store, env_row, rec, difficulty, ab_source="two_frontier"))
        return {"task_id": task_id, "records": [_clean(r) for r in result["records"]],
                "reward_divergence": result["reward_divergence"],
                "providers": result["providers"], "runs": persisted,
                "difficulty": difficulty}
    env = ClinicalEnv(compiled)
    rec = await _rollout(env, seed=seed, run_rubric=run_rubric)
    persisted.append(_persist_rollout(store, env_row, rec, difficulty))
    return {"task_id": task_id, "records": [_clean(rec)], "runs": persisted,
            "difficulty": difficulty}


def _persist_rollout(store, env_row, rec, difficulty, *, ab_source: Optional[str] = None) -> Dict[str, Any]:
    ed = (difficulty or {}).get("value") if difficulty else None
    measured = bool((difficulty or {}).get("measured")) if difficulty else False
    gate = (difficulty or {}).get("passes_gate") if difficulty else None
    prov = rec.get("provenance") or {}
    if ed is not None:
        prov.setdefault("difficulty", {})
        prov["difficulty"].update({"empirical": ed, "measured": measured, "passes_gate": gate})
    # The run carries the CANONICAL environment task_id (not the rollout's locally
    # derived one) so queue / annotate / get_environment all align on one id.
    rec["task_id"] = env_row["task_id"]
    row = store.insert_env_run(
        task_id=env_row["task_id"],
        specialty=env_row["specialty"], task_type=env_row["task_type"],
        case_id=env_row.get("case_id"), case_source=env_row.get("case_source") or "gold",
        provider=rec.get("_provider"), ab_source=ab_source or prov.get("ab_source"),
        mode="rollout", compiled=env_row.get("compiled"),
        trajectory=rec.get("trajectory"), verification=rec.get("verification"),
        provenance=prov, empirical_difficulty=ed, difficulty_measured=measured,
        passes_difficulty_gate=gate,
    )
    return {"run_id": row["run_id"], "provider": rec.get("_provider"),
            "reward": (rec.get("verification") or {}).get("reward")}


# ─── Verify an existing run (PRD §10) ─────────────────────────────────────────
async def verify_run(store, run_id: str) -> Dict[str, Any]:
    """Re-score a stored rollout (rebuild the env from compiled_json + replay the
    trajectory is not needed — we re-run the deterministic verifier over the
    stored trajectory via a lightweight env shim)."""
    row = store.get_env_run(run_id)
    if not row or row.get("mode") != "rollout":
        raise ValueError("run not found or not a rollout")
    from .verify import score

    shim = _ReplayEnv(row.get("compiled") or {}, row.get("trajectory") or [])
    verification = score(shim)
    store.update_env_run(run_id, verification=verification)
    return verification


# ─── Physician annotation (PRD §7) ────────────────────────────────────────────
def save_annotation(store, run_id: str, annotation: Dict[str, Any],
                    *, annotator_ref: Optional[str] = None) -> Dict[str, Any]:
    row = store.get_env_run(run_id)
    if not row:
        raise ValueError("run not found")
    ann = dict(annotation or {})
    if annotator_ref and not ann.get("annotator_credential_ref"):
        ann["annotator_credential_ref"] = annotator_ref
    # Reward validation (PRD §7.1.6): stamp the auto-reward the physician ratified.
    auto = (row.get("verification") or {}).get("reward")
    rr = ann.get("reward_ratified") or {}
    if auto is not None and "auto_value" not in rr:
        rr["auto_value"] = auto
        rr["overrode_auto"] = bool(rr.get("value") is not None and rr.get("value") != auto)
        ann["reward_ratified"] = rr
    store.update_env_run(run_id, physician_annotation=ann)
    return {"run_id": run_id, "physician_annotation": ann}


# ─── Reward model (PRD §7.5) ──────────────────────────────────────────────────
def train_reward_model(store, *, specialty: Optional[str] = None) -> Dict[str, Any]:
    from .reward_model import train_prm

    annotations = store.env_annotation_records(specialty=specialty)
    _prm, report = train_prm(annotations)
    report["n_annotated_runs"] = len(annotations)
    return report


# ─── Export (PRD §9) ──────────────────────────────────────────────────────────
def export(store, *, mode: str = "raw", specialty: Optional[str] = None,
           watermark: Optional[str] = None) -> Dict[str, Any]:
    from .export_env import export_bundle

    runs = store.list_env_runs(specialty=specialty, mode="rollout", limit=10000)
    records = [_row_to_record(r) for r in runs if r.get("trajectory")]
    return export_bundle(records, mode=mode, specialty=specialty, watermark=watermark)


# ─── helpers ──────────────────────────────────────────────────────────────────
def _row_to_record(row: Dict[str, Any]) -> Dict[str, Any]:
    rec = {
        "task_id": row.get("task_id"),
        "specialty": row.get("specialty"),
        "task_type": row.get("task_type"),
        "prompt": (row.get("compiled") or {}).get("question") or "",
        "trajectory": row.get("trajectory") or [],
        "verification": row.get("verification"),
        "provenance": row.get("provenance"),
        "physician_annotation": row.get("physician_annotation"),
    }
    # Rebuild the prompt from the compiled env so raw export carries the real stem.
    compiled = row.get("compiled") or {}
    if compiled.get("case"):
        rec["prompt"] = catalog.build_prompt(compiled.get("case"), compiled.get("question") or "",
                                             row.get("task_type") or "diagnostic_workup")
    return rec


def _clean(rec: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in rec.items() if not k.startswith("_")}


def _env_task_id(specialty: str, task_type: str, case_id: Optional[str]) -> str:
    tt_short = {"information_retrieval": "ir", "diagnostic_workup": "dx",
                "medication_management": "med", "test_referral_ordering": "ord",
                "escalation_safety": "esc", "longitudinal_management": "long"}.get(task_type, "task")
    return f"{specialty[:4]}-{tt_short}-{case_id or _rand_suffix()}"


def _rand_suffix() -> str:
    import uuid

    return uuid.uuid4().hex[:8]


def _critical_hint(entry: Dict[str, Any]) -> str:
    """Assemble critical-negative hint text from a gold wrapper: the failure-mode
    label + the intended-flawed candidate answer (the unsafe path the correct
    answer avoids)."""
    parts = [entry.get("ai_failure_mode") or ""]
    flawed_id = entry.get("intended_flawed_id")
    for cand in entry.get("candidate_answers") or []:
        if cand.get("id") == flawed_id:
            parts.append(cand.get("text") or "")
    return " ".join(p for p in parts if p)


class _ReplayEnv:
    """A minimal env shim so ``verify.score`` can re-score a STORED trajectory
    (no LLM/agent needed) — reconstructs ground_truth, checks, and emitted
    resources from the compiled spec + trajectory."""

    def __init__(self, compiled: Dict[str, Any], trajectory: List[Dict[str, Any]]):
        self.compiled = compiled
        self.trajectory = trajectory
        self.step_rewards = [0.0] * len(trajectory)
        self.specialty = compiled.get("specialty") or "general"
        self.task_type = compiled.get("task_template") or "diagnostic_workup"
        self.emitted = self._rebuild_emitted(trajectory)
        self.state = None

    def _rebuild_emitted(self, trajectory) -> List[Dict[str, Any]]:
        from .tools import build_service_request, build_medication_request, fhir_resource_valid

        out = []
        for s in trajectory:
            if s.get("type") != "tool_call":
                continue
            tool, inp = s.get("tool"), s.get("input") or {}
            fhir = None
            if tool == "order_test":
                fhir = build_service_request(code=inp.get("code", ""), category="laboratory")
            elif tool == "order_medication":
                fhir = build_medication_request(drug=inp.get("drug", ""), dose=inp.get("dose"),
                                                route=inp.get("route"), freq=inp.get("freq"))
            elif tool == "place_referral":
                fhir = build_service_request(code=f"referral-{inp.get('specialty','')}", category="referral")
            if fhir is not None:
                out.append({"tool": tool, "input": inp, "fhir": fhir,
                            "valid": fhir_resource_valid(fhir)})
        return out

    def ground_truth(self) -> Dict[str, Any]:
        return self.compiled.get("ground_truth") or {}

    def checks(self) -> List[Dict[str, Any]]:
        return [dict(c) for c in (self.compiled.get("checks") or [])]

    def final_action(self) -> Optional[Dict[str, Any]]:
        for s in reversed(self.trajectory):
            if s.get("type") == "final_output":
                # reconstruct from the preceding tool_call
                pass
        # The final submit tool_call carries the decision input.
        for s in reversed(self.trajectory):
            if s.get("type") == "tool_call" and s.get("tool", "").startswith(("submit", "escalate")):
                return {"tool": s.get("tool"), "input": s.get("input") or {}}
        return None

    def prompt(self) -> str:
        return catalog.build_prompt(self.compiled.get("case") or {},
                                    self.compiled.get("question") or "", self.task_type)
