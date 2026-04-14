"""Resolve Bearer token as either landing (demo) user or tenant health-system staff."""

from dataclasses import dataclass
from typing import Optional

from fastapi import Header

import auth as auth_module
from tenant_jwt import decode_tenant_staff_token


@dataclass
class StaffContext:
    source: str  # "tenant" | "landing"
    email: str
    name: Optional[str]
    role: str
    tenant_id: Optional[str]
    tenant_slug: Optional[str]
    health_system_code: Optional[str]


async def get_staff_context_optional(
    authorization: Optional[str] = Header(None),
) -> Optional[StaffContext]:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization.removeprefix("Bearer ").strip()
    td = decode_tenant_staff_token(token)
    if td:
        return StaffContext(
            source="tenant",
            email=str(td.get("sub") or ""),
            name=td.get("name"),
            role=str(td.get("role") or "doctor"),
            tenant_id=td.get("tid"),
            tenant_slug=td.get("slug"),
            health_system_code=(td.get("hcode") or None) or None,
        )
    sub = auth_module._decode_token(token)  # noqa: SLF001
    if not sub:
        return None
    users = auth_module._get_users()  # noqa: SLF001
    if sub not in users:
        return None
    u = users[sub]
    return StaffContext(
        source="landing",
        email=u.get("email", sub),
        name=u.get("name"),
        role=str(u.get("role") or "doctor"),
        tenant_id=None,
        tenant_slug=None,
        health_system_code=u.get("clinic_code"),
    )