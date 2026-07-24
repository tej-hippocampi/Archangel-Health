"""V5 Clinical RL Environments — HTTP surface (PRD §10).

Additive router mounted alongside ``routers/asclepius.py`` (kept byte-for-byte
unchanged so V1–V4 are untouched, PRD §12.9). All routes are under
``/api/asclepius/environments`` and admin-gated, except the physician-annotation
surface which any authenticated evaluator can reach (it IS the crown-jewel V5
data, PRD §7). Every V5 surface gates on ``portal_version == 'v5'`` via
``constants.is_env_portal_version`` — never ``isAssisted()`` (PRD §12.9).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from asclepius import auth as asc_auth
from asclepius.constants import (
    ENV_EXPORT_MODES,
    ENV_PORTAL_VERSION,
    is_env_portal_version,
    normalize_env_export_mode,
)
from asclepius.environments import service
from asclepius.store import get_store

router = APIRouter(prefix="/api/asclepius/environments", tags=["asclepius-v5"])


def _store():
    return get_store()


# ─── Request models (inline — isolated from the V1–V4 schemas) ────────────────
class GenerateRequest(BaseModel):
    n: int = Field(default=5, ge=1, le=50)
    task_type: Optional[str] = None
    source: str = "gold"  # gold | synthetic


class RolloutRequest(BaseModel):
    two_frontier: bool = False
    seed: Optional[int] = None
    run_rubric: bool = True
    measure_difficulty: bool = False


class VerifyRequest(BaseModel):
    run_id: Optional[str] = None


class AnnotateRequest(BaseModel):
    run_id: str
    portal_version: Optional[str] = None
    annotation: Dict[str, Any] = Field(default_factory=dict)


# ─── Generate (PRD §10) ───────────────────────────────────────────────────────
@router.post("/{specialty}/generate")
async def generate_environments(
    specialty: str, body: GenerateRequest,
    _admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    store = _store()
    if body.source == "gold":
        return service.generate_from_gold(store, specialty, n=body.n, task_type=body.task_type)
    raise HTTPException(400, "synthetic generation must be seeded via /tasks/generate then compiled; "
                             "gold + real cases are source priority #1 (PRD §0.5)")


# ─── Rollout (PRD §6, §10) ────────────────────────────────────────────────────
@router.post("/{task_id}/rollout")
async def rollout_environment(
    task_id: str, body: RolloutRequest,
    _admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    store = _store()
    try:
        return await service.run_rollout(
            store, task_id, two_frontier=body.two_frontier, seed=body.seed,
            run_rubric=body.run_rubric, measure_gate=body.measure_difficulty,
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc))


# ─── Verify (PRD §5, §10) ─────────────────────────────────────────────────────
@router.post("/{task_id}/verify")
async def verify_environment(
    task_id: str, body: VerifyRequest,
    _admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    store = _store()
    run_id = body.run_id
    if not run_id:
        runs = store.list_env_runs(task_id=task_id, mode="rollout", limit=1)
        if not runs:
            raise HTTPException(404, "no rollout to verify; run a rollout first")
        run_id = runs[0]["run_id"]
    try:
        verification = await service.verify_run(store, run_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return {"run_id": run_id, "verification": verification}


# ─── Export (PRD §9, §10) ─────────────────────────────────────────────────────
@router.get("/export")
async def export_environments(
    mode: str = Query("raw"), specialty: Optional[str] = None,
    _admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    if mode not in ENV_EXPORT_MODES:
        mode = normalize_env_export_mode(mode)
    return service.export(_store(), mode=mode, specialty=specialty)


# ─── Reward model (PRD §7.5, §10) ─────────────────────────────────────────────
@router.post("/reward-model/train")
async def train_reward_model(
    specialty: Optional[str] = None,
    _admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    return service.train_reward_model(_store(), specialty=specialty)


# ─── Physician annotation (PRD §7, §10) — evaluator-reachable ─────────────────
@router.get("/annotation-queue")
async def annotation_queue(
    portal_version: str = Query(ENV_PORTAL_VERSION),
    specialty: Optional[str] = None,
    user: Dict[str, Any] = Depends(asc_auth.get_current_user),
):
    """The V5 annotation queue (PRD §7.2). Gated on ``portal_version == 'v5'``."""
    if not is_env_portal_version(portal_version):
        raise HTTPException(400, "the environment-annotation queue requires portal_version='v5'")
    store = _store()
    runs = store.list_env_runs(specialty=specialty, mode="rollout", has_annotation=False, limit=200)
    return {"portal_version": ENV_PORTAL_VERSION,
            "queue": [_annotation_task_view(r) for r in runs]}


@router.post("/{task_id}/annotate")
async def annotate_environment(
    task_id: str, body: AnnotateRequest,
    user: Dict[str, Any] = Depends(asc_auth.get_current_user),
):
    """A board-certified physician submits the §7 annotation for one trajectory
    (run). Gated on ``portal_version == 'v5'`` (PRD §12.9)."""
    if body.portal_version is not None and not is_env_portal_version(body.portal_version):
        raise HTTPException(400, "V5 annotation requires portal_version='v5'")
    store = _store()
    row = store.get_env_run(body.run_id)
    if not row or row.get("task_id") != task_id:
        raise HTTPException(404, "run not found for this environment")
    try:
        result = service.save_annotation(
            store, body.run_id, body.annotation,
            annotator_ref=user.get("id") or user.get("email"),
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return result


# ─── Inspect / list (PRD §10) ─────────────────────────────────────────────────
@router.get("/{task_id}")
async def get_environment(
    task_id: str, _admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    store = _store()
    env = store.get_environment(task_id)
    if not env:
        raise HTTPException(404, "environment not found")
    runs = store.list_env_runs(task_id=task_id, mode="rollout", limit=50)
    return {"environment": _public_env(env), "runs": [_run_view(r) for r in runs]}


@router.get("")
async def list_environments(
    specialty: Optional[str] = None, mode: str = "generated",
    _admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    store = _store()
    rows = store.list_env_runs(specialty=specialty, mode=mode, limit=500)
    view = _public_env if mode == "generated" else _run_view
    return {"environments": [view(r) for r in rows]}


# ─── view helpers ─────────────────────────────────────────────────────────────
def _public_env(row: Dict[str, Any]) -> Dict[str, Any]:
    compiled = row.get("compiled") or {}
    return {
        "task_id": row.get("task_id"), "specialty": row.get("specialty"),
        "task_type": row.get("task_type"), "case_id": row.get("case_id"),
        "case_source": row.get("case_source"),
        "decision_point": compiled.get("decision_point"),
        "allowed_tools": compiled.get("allowed_tools"),
        "n_critical_negatives": len(compiled.get("critical_negatives") or []),
        "ground_truth_source": (compiled.get("ground_truth") or {}).get("source"),
        "created_at": row.get("created_at"),
    }


def _run_view(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "run_id": row.get("run_id"), "task_id": row.get("task_id"),
        "provider": row.get("provider"), "ab_source": row.get("ab_source"),
        "reward": (row.get("verification") or {}).get("reward"),
        "empirical_difficulty": row.get("empirical_difficulty"),
        "passes_difficulty_gate": row.get("passes_difficulty_gate"),
        "annotated": bool(row.get("physician_annotation")),
        "n_steps": len(row.get("trajectory") or []),
        "created_at": row.get("created_at"),
    }


def _annotation_task_view(row: Dict[str, Any]) -> Dict[str, Any]:
    """The context a physician needs to annotate one trajectory (PRD §7.2)."""
    compiled = row.get("compiled") or {}
    view = _run_view(row)
    view.update({
        "prompt": service.catalog.build_prompt(
            compiled.get("case") or {}, compiled.get("question") or "",
            row.get("task_type") or "diagnostic_workup"),
        "trajectory": row.get("trajectory") or [],
        "auto_reward": (row.get("verification") or {}).get("reward"),
        "verification": row.get("verification"),
        "case_context": _case_context(compiled.get("case") or {}),
    })
    return view


def _case_context(case: Dict[str, Any]) -> Dict[str, Any]:
    """The sticky case-context panel (labs/notes/studies) the doctor keeps in view
    while scrolling the trajectory (PRD §7.6). De-identified fields only."""
    return {
        "demographics": case.get("demographics") or {},
        "problem_list": case.get("problem_list") or [],
        "medications": case.get("medications") or [],
        "lab_panels": case.get("lab_panels") or [],
        "notes": case.get("notes") or [],
        "studies": case.get("studies") or [],
    }
