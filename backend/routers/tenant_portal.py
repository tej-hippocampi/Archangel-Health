"""Tenant staff auth and director audit log."""

import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, EmailStr

from tenant_constants import TRIAGE_DEMO_SLUG
from tenant_jwt import create_tenant_staff_token, decode_tenant_staff_token

logger = logging.getLogger("tenant_portal")

router = APIRouter(prefix="/api/tenant", tags=["tenant"])


def _ts(request: Request):
    return request.app.state.team_store


def _require_tenant_staff(authorization: Optional[str], slug: str) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization.removeprefix("Bearer ").strip()
    td = decode_tenant_staff_token(token)
    if not td or (td.get("slug") or "").lower() != (slug or "").lower():
        raise HTTPException(status_code=401, detail="Invalid session")
    return td


class TenantLoginBody(BaseModel):
    email: EmailStr
    password: str


@router.post("/{slug}/auth/login")
async def tenant_auth_login(slug: str, body: TenantLoginBody, request: Request):
    ts = _ts(request)
    authd = ts.authenticate_team_member(slug, str(body.email), body.password)
    if not authd:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    m, hs = authd["member"], authd["health_system"]
    if (hs.get("slug") or "").lower() == TRIAGE_DEMO_SLUG:
        # Demo logins self-heal the seed: a process that has been up past the
        # seed max-age (or lost its in-memory patients) would otherwise serve a
        # stale/empty demo roster until restart. Never block sign-in on it.
        refresh = getattr(request.app.state, "refresh_triage_demo_seed", None)
        if refresh:
            try:
                refresh()
            except Exception:
                logger.exception("triage demo seed refresh failed during login")
    ts.append_audit_sign_in(
        health_system_id=hs["id"],
        user_email=m["email"],
        display_name=m.get("name") or "",
        role=m.get("role") or "surgeon",
    )
    token = create_tenant_staff_token(
        email=m["email"],
        name=m.get("name") or "",
        role=m.get("role") or "surgeon",
        health_system_id=hs["id"],
        tenant_slug=hs["slug"],
        health_system_code=hs.get("health_system_code") or "",
        is_team_director=bool(m.get("is_team_director") or 0),
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "email": m["email"],
            "name": m.get("name"),
            "role": m.get("role"),
            "is_team_director": bool(m.get("is_team_director") or 0),
            "tenant_slug": hs["slug"],
            "health_system_name": hs.get("name"),
        },
    }


@router.get("/{slug}/me")
async def tenant_me(slug: str, authorization: Optional[str] = Header(None)):
    _require_tenant_staff(authorization, slug)
    td = decode_tenant_staff_token(authorization.removeprefix("Bearer ").strip())
    return {
        "email": td.get("sub"),
        "name": td.get("name"),
        "role": td.get("role"),
        "is_team_director": bool(td.get("itd") or 0),
        "tenant_slug": td.get("slug"),
        "health_system_code": td.get("hcode"),
    }


@router.get("/{slug}/audit-log")
async def tenant_audit_log(slug: str, request: Request, authorization: Optional[str] = Header(None)):
    _require_tenant_staff(authorization, slug)
    ts = _ts(request)
    hs = ts.get_health_system_by_slug(slug)
    if not hs:
        raise HTTPException(status_code=404, detail="Health system not found")
    rows = ts.list_audit_sign_ins(hs["id"])
    return {"entries": rows}


# ─── Grounding (tenant-scoped; global demo data — TODO: filter per health system) ─

@router.get("/{slug}/grounding/reports")
async def tenant_list_grounding_reports(
    slug: str,
    request: Request,
    authorization: Optional[str] = Header(None),
    verdict: Optional[str] = None,
    track: Optional[str] = None,
    prompt_version: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 100,
):
    _require_tenant_staff(authorization, slug)
    team_store = _ts(request)
    patient_store = getattr(request.app.state, "patient_store", {}) or {}
    rows = team_store.list_grounding_reports(
        limit=min(limit, 500), verdict=verdict, track=track, prompt_version=prompt_version, since=since
    )
    for row in rows:
        pid = row.get("patient_id")
        pdata = patient_store.get(pid) or {}
        sd = pdata.get("structured_data") or {}
        row["patient_name"] = sd.get("patient_name") or pdata.get("name") or pid
    return {"reports": rows}


@router.get("/{slug}/grounding/reports/{report_id}")
async def tenant_get_grounding_report(
    slug: str,
    report_id: int,
    request: Request,
    authorization: Optional[str] = Header(None),
):
    _require_tenant_staff(authorization, slug)
    team_store = _ts(request)
    row = team_store.get_grounding_report(report_id)
    if not row:
        raise HTTPException(status_code=404, detail="Report not found")
    patient_store = getattr(request.app.state, "patient_store", {}) or {}
    pdata = patient_store.get(row.get("patient_id")) or {}
    sd = pdata.get("structured_data") or {}
    row["patient_name"] = sd.get("patient_name") or pdata.get("name") or row.get("patient_id")
    return row


@router.get("/{slug}/grounding/stats")
async def tenant_grounding_stats(
    slug: str,
    request: Request,
    authorization: Optional[str] = Header(None),
    window_days: int = 30,
):
    _require_tenant_staff(authorization, slug)
    team_store = _ts(request)
    return team_store.grounding_summary_stats(window_days=window_days)


@router.get("/{slug}/grounding/inspector-recall")
async def tenant_grounding_inspector_recall(
    slug: str,
    request: Request,
    authorization: Optional[str] = Header(None),
):
    _require_tenant_staff(authorization, slug)
    team_store = _ts(request)
    snap = team_store.get_latest_inspector_recall()
    if not snap:
        return {"available": False, "message": "No inspector recall snapshot yet — run validation suite"}
    return {"available": True, **snap}


# ─── AI Call Log (tenant-scoped; global demo data — TODO: filter per health system) ─

@router.get("/{slug}/ai-calls/stats")
async def tenant_ai_call_stats(
    slug: str,
    request: Request,
    authorization: Optional[str] = Header(None),
    window_days: int = 30,
):
    _require_tenant_staff(authorization, slug)
    team_store = _ts(request)
    return team_store.llm_call_stats(window_days=window_days)


@router.get("/{slug}/ai-calls")
async def tenant_ai_calls(
    slug: str,
    request: Request,
    authorization: Optional[str] = Header(None),
    role: Optional[str] = None,
    prompt_id: Optional[str] = None,
    prompt_version: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 200,
):
    _require_tenant_staff(authorization, slug)
    team_store = _ts(request)
    calls = team_store.list_llm_calls(
        limit=min(limit, 500),
        role=role,
        prompt_id=prompt_id,
        prompt_version=prompt_version,
        since=since,
    )
    patient_store = getattr(request.app.state, "patient_store", {}) or {}
    for row in calls:
        pid = row.get("patient_id")
        pdata = patient_store.get(pid) or {}
        sd = pdata.get("structured_data") or {}
        row["patient_name"] = sd.get("patient_name") or pdata.get("name") or pid
    return {"calls": calls}


@router.get("/{slug}/ai-calls/prompts")
async def tenant_ai_call_prompts(slug: str, authorization: Optional[str] = Header(None)):
    _require_tenant_staff(authorization, slug)
    from prompts.registry import PROMPT_REGISTRY, prompt_meta

    return {
        "prompts": [
            {"prompt_id": pid, "label": entry.get("label", pid), **prompt_meta(pid)}
            for pid, entry in PROMPT_REGISTRY.items()
        ]
    }
