"""JWTs for health-system staff (pass-4 roles: surgeon | rn_coordinator | np_pa).

The `itd` (is_team_director) claim is set when the staff member is the surgical
pod's director; only one director per pod. Legacy tokens with `role: "doctor"`
or `role: "director"` are still accepted by `staff_context._normalize_legacy_role`
so in-flight sessions keep working across the role-token migration.
"""

import os
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from jose import JWTError, jwt

AUTH_SECRET = os.getenv("AUTH_SECRET", "change-me-in-production-elysium")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7


def create_tenant_staff_token(
    *,
    email: str,
    name: str,
    role: str,
    health_system_id: str,
    tenant_slug: str,
    health_system_code: str,
    is_team_director: bool = False,
) -> str:
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload: Dict[str, Any] = {
        "typ": "tenant_staff",
        "sub": email.lower().strip(),
        "name": name or "",
        "role": role,
        "tid": health_system_id,
        "slug": tenant_slug,
        "hcode": health_system_code or "",
        "itd": 1 if is_team_director else 0,
        "exp": expire,
    }
    return jwt.encode(payload, AUTH_SECRET, algorithm=ALGORITHM)


def decode_tenant_staff_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        payload = jwt.decode(token, AUTH_SECRET, algorithms=[ALGORITHM])
        if payload.get("typ") != "tenant_staff":
            return None
        return payload
    except JWTError:
        return None
