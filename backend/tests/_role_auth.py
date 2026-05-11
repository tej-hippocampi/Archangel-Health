"""
Test helpers for minting pass-4 staff JWTs.

Triage routers now enforce per-role gates (see `auth_roles.require_roles`).
Tests that previously posted anonymously to write endpoints must mint a
tenant JWT with the appropriate role claim. Read endpoints that previously
ran anonymous now also require a staff Bearer.

The mint helpers use the production JWT factories (`auth._create_token`
for landing JWTs and `tenant_jwt.create_tenant_staff_token` for tenant
JWTs) so the wire format matches reality.
"""

from __future__ import annotations

from typing import Dict, Optional

import auth as auth_module
from tenant_jwt import create_tenant_staff_token


def landing_token(role: str = "surgeon", *, email: str = "tester@example.com") -> str:
    """Mint a landing-flavored JWT with the given role.

    Inserts the user into the in-memory landing user store first so
    `staff_context._normalize_legacy_role` resolves it.
    """
    users = auth_module._get_users()  # noqa: SLF001
    users[email] = {
        "email": email,
        "name": "Tester",
        "role": role,
        "password_hash": "x",
    }
    return auth_module._create_token(email)  # noqa: SLF001


def tenant_token(
    role: str = "surgeon",
    *,
    email: Optional[str] = None,
    is_team_director: bool = False,
    health_system_id: str = "demo_hs",
) -> str:
    """Mint a tenant_staff JWT with the given role + director flag."""
    return create_tenant_staff_token(
        email=email or f"{role}@hs.com",
        name=f"Test {role}",
        role=role,
        health_system_id=health_system_id,
        tenant_slug="demo",
        health_system_code="DEMO",
        is_team_director=is_team_director,
    )


def auth_headers(
    role: str = "surgeon",
    *,
    source: str = "tenant",
    email: Optional[str] = None,
) -> Dict[str, str]:
    """Bearer-prefixed `Authorization` header for the requested role.

    `source="tenant"` is the production-realistic path; `source="landing"`
    is supported for legacy tests like `test_intraop_router._auth_headers`.

    For `source="landing"`, pass a distinct `email` per actor when a
    TestClient default header pins one sub but tests interleave roles —
    otherwise the in-memory user row for that email gets overwritten.
    """
    if source == "landing":
        token = landing_token(role, email=email or "tester@example.com")
    else:
        if email:
            token = tenant_token(role, email=email)
        else:
            token = tenant_token(role)
    return {"Authorization": f"Bearer {token}"}


def admin_token() -> str:
    """Mint an admin JWT for `routers.admin._verify_token`."""
    from routers.admin import _create_token  # local: avoid cycle on import

    return _create_token()


def admin_headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {admin_token()}"}
