"""Tenant staff auth and director audit log."""

from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, EmailStr

from tenant_jwt import create_tenant_staff_token, decode_tenant_staff_token

router = APIRouter(prefix="/api/tenant", tags=["tenant"])


def _ts(request: Request):
    return request.app.state.team_store


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
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization.removeprefix("Bearer ").strip()
    td = decode_tenant_staff_token(token)
    if not td or (td.get("slug") or "").lower() != (slug or "").lower():
        raise HTTPException(status_code=401, detail="Invalid session")
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
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization.removeprefix("Bearer ").strip()
    td = decode_tenant_staff_token(token)
    if not td or (td.get("slug") or "").lower() != (slug or "").lower():
        raise HTTPException(status_code=401, detail="Invalid session")
    if not bool(td.get("itd") or 0):
        raise HTTPException(status_code=403, detail="Audit log is only available to the Director of TEAM Initiative.")
    ts = _ts(request)
    hs = ts.get_health_system_by_slug(slug)
    if not hs:
        raise HTTPException(status_code=404, detail="Health system not found")
    rows = ts.list_audit_sign_ins(hs["id"])
    return {"entries": rows}
