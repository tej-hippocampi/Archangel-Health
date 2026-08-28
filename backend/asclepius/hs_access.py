"""Access levels and surfaces for a HEALTH SYSTEM PORTAL account.

The structural mirror of ``capabilities.py``, for a different principal. That
file gates physicians: its rows come from ``users``, its axes are role, tier and
``verification_status``, and its exhaustiveness over the physician tiers is
asserted by a test. A portal account is a row in ``hs_portal_users`` with none of
those columns, so extending that module would make ``surfaces()`` ambiguous about
which kind of thing it was handed. Two small policy tables beat one clever one.

Kept free of the words the provider-facing grep forbids, so
``routers/asclepius_provider.py`` may import it.

The collapse in ``access_level`` is the load-bearing decision here, and it is the
same one ``capabilities.py`` makes for a NULL ``verification_status``: an account
with ``approval_status`` NULL was provisioned by an operator before this module
existed, nobody ever made an approval decision about it, and it reaches
everything. That is what makes adding approval a zero-backfill migration -- no
sweep stamps a decision on rows that predate the question, and no existing
hospital wakes up locked out of the door it has been uploading through.
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, Optional

# ─── Access levels ────────────────────────────────────────────────────────────
FULL = "full"                 # approved, or the pre-approval-era NULL
PROVISIONAL = "provisional"   # signed itself up, waiting on a human
NONE = "none"                 # refused, or deactivated
ACCESS_LEVELS = (FULL, PROVISIONAL, NONE)

# ─── Surfaces ─────────────────────────────────────────────────────────────────
# Endpoints declare the surface they serve, so adding one means naming its
# surface rather than appending to an allowlist somebody has to find first.
UPLOAD = "upload"      # hand us clinical data
PAYOUTS = "payouts"    # the ledger of what we have paid this organization
INTAKE = "intake"      # tell us who you are and what you hold
ACCOUNT = "account"    # your own password and session
HS_SURFACES = (UPLOAD, PAYOUTS, INTAKE, ACCOUNT)

#: A health system that just signed up gets the portal minus the one door that
#: matters: it cannot upload until a person has looked at it. Everything else is
#: open, for the reason capabilities.py already gives about provisional
#: physicians. Hiding the whole product at the exact moment someone has decided
#: to try it makes it look empty, and this is a CIO we spent months reaching.
#: The ledger they see reads zero, honestly, because we have not paid them yet.
#:
#: UPLOAD is the only thing withheld because it is the only irreversible one.
#: Everything else here is them telling us things; upload is them putting
#: clinical data into our ingestion pipeline, and an unvetted party doing that
#: is a problem we cannot take back.
_BY_ACCESS: Dict[str, FrozenSet[str]] = {
    FULL: frozenset(HS_SURFACES),
    PROVISIONAL: frozenset({PAYOUTS, INTAKE, ACCOUNT}),
    NONE: frozenset(),
}


def access_level(user: Optional[Dict[str, Any]]) -> str:
    """Map a portal-account row to its access level. Reads the dict, never SQL."""
    u = user or {}
    if not u:
        return NONE
    # Consulted only when present, since several call sites pass a partial row.
    if u.get("active") is not None and not u.get("active"):
        return NONE
    status = (u.get("approval_status") or "").strip().lower() or None
    if status == "rejected":
        return NONE
    if status == "pending":
        return PROVISIONAL
    # 'approved' and NULL both land here. See the module docstring.
    return FULL


def surfaces(user: Optional[Dict[str, Any]]) -> FrozenSet[str]:
    return _BY_ACCESS.get(access_level(user), frozenset())


def can_surface(user: Optional[Dict[str, Any]], surface: str) -> bool:
    return surface in surfaces(user)


def account_state(user: Optional[Dict[str, Any]]) -> str:
    """What the organization is told about itself.

    Partner words only, the same rule the upload status map follows: an operator
    token like 'provisional' is our vocabulary for our queue, and a hospital IT
    contact reading it learns nothing except that we have jargon. The raw
    ``approval_status`` stays on the wire for the ADMIN responses, which do need
    the four states distinguishable.
    """
    level = access_level(user)
    if level == PROVISIONAL:
        return "in review"
    if level == NONE:
        return "closed"
    return "active"
