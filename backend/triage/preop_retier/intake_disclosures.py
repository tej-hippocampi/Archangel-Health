"""
Extract re-tier hard-escalator disclosure flags from intake form data.

Pure function over a `form_data` dict. The exact upstream shape is owned
by `intake_form_parser.py`; here we accept either a flat-keyed dict or a
nested `social` block and look for boolean-or-enum signals matching the
four PRD §5.2 hard escalators.
"""

from __future__ import annotations

from typing import Any


_FLAG_LIVES_ALONE = "INTAKE_DISCLOSURE_LIVES_ALONE_NO_CAREGIVER"
_FLAG_HOUSING = "INTAKE_DISCLOSURE_HOUSING_INSTABILITY"
_FLAG_FOOD = "INTAKE_DISCLOSURE_FOOD_INSECURITY"
_FLAG_TRANSPORT = "INTAKE_DISCLOSURE_TRANSPORTATION_BARRIER_DAY_OF"


def _get(form_data: dict[str, Any], *path: str) -> Any:
    """Walk a nested-or-flat dict; first-hit wins."""
    cur: Any = form_data
    for p in path:
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            return None
    return cur


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in {"yes", "true", "y", "1"}
    return False


def extract_disclosure_flags(form_data: dict[str, Any]) -> set[str]:
    """Return the set of re-tier hard-escalator codes implied by `form_data`."""
    if not isinstance(form_data, dict):
        return set()

    flags: set[str] = set()

    # ─── Lives alone with no reliable caregiver ─────────────────────────────
    lives_alone = (
        _get(form_data, "social", "lives_alone")
        if _get(form_data, "social", "lives_alone") is not None
        else _get(form_data, "lives_alone")
    )
    has_caregiver = (
        _get(form_data, "social", "has_reliable_caregiver")
        if _get(form_data, "social", "has_reliable_caregiver") is not None
        else _get(form_data, "has_reliable_caregiver")
    )
    if _truthy(lives_alone) and has_caregiver is not None and not _truthy(has_caregiver):
        flags.add(_FLAG_LIVES_ALONE)

    # ─── Housing instability ────────────────────────────────────────────────
    housing = (
        _get(form_data, "social", "housing_status")
        or _get(form_data, "housing_status")
    )
    if isinstance(housing, str) and housing.strip().upper() in {"UNSTABLE", "HOMELESS"}:
        flags.add(_FLAG_HOUSING)

    # ─── Food insecurity ────────────────────────────────────────────────────
    food = (
        _get(form_data, "social", "food_security")
        or _get(form_data, "food_security")
    )
    if isinstance(food, str) and food.strip().upper() == "INSECURE":
        flags.add(_FLAG_FOOD)

    # ─── Transportation barrier on day of surgery ───────────────────────────
    transport_day_of = (
        _get(form_data, "logistics", "transportation_day_of_barrier")
        if _get(form_data, "logistics", "transportation_day_of_barrier") is not None
        else _get(form_data, "transportation_day_of_barrier")
    )
    if transport_day_of is None:
        # Compatibility: some forms encode this as `has_ride_day_of` (inverse).
        has_ride = (
            _get(form_data, "logistics", "has_ride_day_of")
            if _get(form_data, "logistics", "has_ride_day_of") is not None
            else _get(form_data, "has_ride_day_of")
        )
        if has_ride is not None and not _truthy(has_ride):
            flags.add(_FLAG_TRANSPORT)
    elif _truthy(transport_day_of):
        flags.add(_FLAG_TRANSPORT)

    return flags
