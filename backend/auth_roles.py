"""
Pass-4 role-based access control helpers.

Five roles, no others (PRD Pass-4 §1):

  - `system_admin`      → admin JWT (`routers/admin._verify_token`)
                            or shared `X-Admin-Token` (`_verify_admin`)
  - `surgeon`           → tenant JWT, role claim "surgeon"
  - `rn_coordinator`    → tenant JWT, role claim "rn_coordinator"
  - `np_pa`             → tenant JWT, role claim "np_pa"
  - `patient`           → implicit (no Bearer token); enforced by
                          `require_patient_session(staff)` which 403s when
                          a staff token is present on a patient-only route

Reads (e.g. `GET /api/triage/tuning/<stage>/current`) accept the full
clinical set so NP/PAs can see everything; writes exclude NP/PA.
"""

from __future__ import annotations

from typing import Iterable, Optional

from fastapi import HTTPException

from staff_context import StaffContext


# Convenience role-set constants used by the triage routers.
ALL_CLINICAL = {"surgeon", "rn_coordinator", "np_pa"}
WRITE_CLINICAL = {"surgeon", "rn_coordinator"}  # no NP/PA on writes
ALL_STAFF = {"system_admin", "surgeon", "rn_coordinator", "np_pa"}


def require_roles(staff: Optional[StaffContext], allowed: Iterable[str]) -> None:
    """Raise 401 if no staff context, 403 if their role is not in `allowed`."""
    if staff is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    role = (staff.role or "").strip().lower()
    if role not in set(allowed):
        raise HTTPException(status_code=403, detail="Insufficient role.")


def require_patient_session(staff: Optional[StaffContext]) -> None:
    """Raise 403 if a staff token is present on a patient-only endpoint.

    Patient endpoints intentionally accept anonymous (the patient app does
    not currently mint a session token). When a clinician hits one of these
    routes, we reject with 403 to keep clinical-source provenance honest —
    these events represent the patient's own actions.
    """
    if staff is not None:
        raise HTTPException(
            status_code=403,
            detail="Patient-session only — staff cannot submit on a patient's behalf here.",
        )
