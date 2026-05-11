"""Resolve Bearer token as either landing (demo) user or tenant health-system staff.

Pass-4 role model (five roles): system_admin (separate admin JWT), surgeon,
rn_coordinator, np_pa, plus the implicit `patient` session. Legacy tokens
that still carry `doctor` / `nurse` / `director` are normalized to the new
five-role taxonomy at resolution time so old in-flight JWTs keep working.
"""

from dataclasses import dataclass
from typing import Optional

from fastapi import Header

import auth as auth_module
from tenant_jwt import decode_tenant_staff_token


_LEGACY_ROLE_MAP = {
    "doctor": "surgeon",
    "director": "surgeon",
    "nurse": "rn_coordinator",
}


def _normalize_legacy_role(raw: Optional[str]) -> str:
    """Map any legacy role token to the pass-4 taxonomy. Default: `surgeon`."""
    role = (raw or "").strip().lower()
    if not role:
        return "surgeon"
    return _LEGACY_ROLE_MAP.get(role, role)


@dataclass
class StaffContext:
    source: str  # "tenant" | "landing"
    email: str
    name: Optional[str]
    role: str
    tenant_id: Optional[str]
    tenant_slug: Optional[str]
    health_system_code: Optional[str]
    is_team_director: bool = False


async def get_staff_context_optional(
    authorization: Optional[str] = Header(None),
) -> Optional[StaffContext]:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization.removeprefix("Bearer ").strip()
    td = decode_tenant_staff_token(token)
    if td:
        raw_role = str(td.get("role") or "")
        role = _normalize_legacy_role(raw_role)
        # Director-ness can come from either the JWT claim (post-pass-4 tokens)
        # or the legacy `role: "director"` payload. We also fall back to the
        # team_members row when the JWT is silent (handled by callers via
        # `team_store.get_team_member` if they need authoritative truth).
        is_director = bool(td.get("itd"))
        if not is_director and (raw_role or "").strip().lower() == "director":
            is_director = True
        return StaffContext(
            source="tenant",
            email=str(td.get("sub") or ""),
            name=td.get("name"),
            role=role,
            tenant_id=td.get("tid"),
            tenant_slug=td.get("slug"),
            health_system_code=(td.get("hcode") or None) or None,
            is_team_director=is_director,
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
        role=_normalize_legacy_role(u.get("role")),
        tenant_id=None,
        tenant_slug=None,
        health_system_code=u.get("clinic_code"),
        is_team_director=False,
    )
