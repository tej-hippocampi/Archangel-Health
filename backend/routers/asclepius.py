"""Asclepius admin API — storage + export for the Expert Evaluation Portal.

Prefix: /admin/asclepius
Auth:   reuses the admin Bearer token (same as the rest of the admin portal).

No PHI. See docs/prd/asclepius-expert-evaluation-portal-v1.md.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from asclepius.store import get_store
from asclepius.buyer_profiles import list_profiles
from asclepius.export import build_export
from asclepius.seed import seed_samples
from routers.admin import _verify_token

router = APIRouter(prefix="/admin/asclepius", tags=["asclepius"])


def require_admin(authorization: Optional[str] = Header(None)) -> None:
    _verify_token(authorization)


# ── Reads ──────────────────────────────────────────────────────────────────
@router.get("/stats")
def stats(_: None = Depends(require_admin)) -> dict[str, Any]:
    return get_store().stats()


@router.get("/submissions")
def submissions(_: None = Depends(require_admin), limit: int = 500) -> dict[str, Any]:
    return {"submissions": get_store().list_submissions(limit=limit)}


@router.get("/records")
def records(
    _: None = Depends(require_admin),
    type: str = "all",
    specialty: str = "all",
    grounded: bool = False,
    limit: int = 5000,
) -> dict[str, Any]:
    recs = get_store().list_records(
        record_type=type, specialty=specialty, grounded_only=grounded, limit=limit
    )
    return {"records": recs, "count": len(recs)}


@router.get("/profiles")
def profiles(_: None = Depends(require_admin)) -> dict[str, Any]:
    return {"profiles": list_profiles()}


@router.get("/specialties")
def specialties(_: None = Depends(require_admin)) -> dict[str, Any]:
    return {"specialties": get_store().specialties()}


@router.get("/exports")
def exports(_: None = Depends(require_admin)) -> dict[str, Any]:
    return {"exports": get_store().list_exports()}


# ── Ingest ─────────────────────────────────────────────────────────────────
class IngestBody(BaseModel):
    task: dict[str, Any]
    submission: dict[str, Any]


@router.post("/ingest")
def ingest(body: IngestBody, _: None = Depends(require_admin)) -> dict[str, Any]:
    return get_store().ingest_submission(body.submission, body.task)


@router.post("/seed")
def seed(_: None = Depends(require_admin)) -> dict[str, Any]:
    return seed_samples(get_store())


# ── Export ─────────────────────────────────────────────────────────────────
class ExportBody(BaseModel):
    profile: str = "default"
    type: str = "all"
    specialty: str = "all"
    grounded: bool = False


@router.post("/export")
def export(body: ExportBody, _: None = Depends(require_admin)) -> dict[str, Any]:
    store = get_store()
    recs = store.list_records(
        record_type=body.type, specialty=body.specialty, grounded_only=body.grounded
    )
    filters = {"type": body.type, "specialty": body.specialty, "grounded": body.grounded}
    bundle = build_export(recs, body.profile, filters=filters)
    store.log_export(bundle["batch_id"], bundle["profile"], bundle["count"], filters)
    return bundle
