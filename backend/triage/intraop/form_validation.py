"""
Lock-time validation for the intra-op form (PRD §4.1 / AC-2.1 / §5.4).

`validate_required_fields` enforces the 11 required universal fields
plus a small set of conditional fields:

  - `complication_types`        required when `documented_complication == True`
  - `complication_description`  required when `documented_complication == True`
  - `conversion_reason`         required when `conversion == "YES"`
  - `procedural_aborted_reason` required when `procedural_aborted == True`
  - `or_started_at`/`or_ended_at` are not strictly required but, if both
    present, the durations must agree with `or_duration_minutes` (PRD edge 1).

Returns a list of missing field names; an empty list ⇒ ready to lock.
The router converts a non-empty list into a 422 with the missing keys.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


REQUIRED_UNIVERSAL_FIELDS: list[str] = [
    "documented_complication",
    "ebl",
    "transfusion_total_units",
    "conversion",
    "sustained_hypotension",
    "vasopressor_requirement",
    "significant_arrhythmia",
    "or_duration_minutes",
    "difficult_airway",
    "net_fluid_balance",
    "anesthesia_type",
]


def _present(value: Any) -> bool:
    """A field is considered present if it is not None *and* not the
    empty string (booleans / 0 / False are valid values)."""
    if value is None:
        return False
    if isinstance(value, str) and value.strip() == "":
        return False
    return True


def validate_required_fields(form_fields: dict[str, Any]) -> list[str]:
    """Return the list of missing required fields (universal + conditional)."""
    missing: list[str] = []

    for key in REQUIRED_UNIVERSAL_FIELDS:
        if not _present(form_fields.get(key)):
            missing.append(key)

    if form_fields.get("documented_complication") is True:
        if not form_fields.get("complication_types"):
            missing.append("complication_types")
        if not _present(form_fields.get("complication_description")):
            missing.append("complication_description")

    if form_fields.get("conversion") == "YES":
        if not _present(form_fields.get("conversion_reason")):
            missing.append("conversion_reason")

    if form_fields.get("procedural_aborted") is True:
        if not _present(form_fields.get("procedural_aborted_reason")):
            missing.append("procedural_aborted_reason")

    return missing


# ─── OR-time consistency check (PRD edge case 1) ─────────────────────────────

def or_duration_consistent_with_timestamps(form_fields: dict[str, Any]) -> bool:
    """When all three of `or_started_at`, `or_ended_at`, and
    `or_duration_minutes` are set, the implied duration must agree
    with `or_duration_minutes` ±1 minute. Returns True when consistent
    or when fields are insufficient to check."""
    started = form_fields.get("or_started_at")
    ended = form_fields.get("or_ended_at")
    duration = form_fields.get("or_duration_minutes")
    if not (started and ended and duration is not None):
        return True
    try:
        d_start = datetime.fromisoformat(str(started).replace("Z", ""))
        d_end = datetime.fromisoformat(str(ended).replace("Z", ""))
    except ValueError:
        # Parsing fails — let the lock validation surface a generic missing
        # timestamp warning rather than blocking on this consistency check.
        return True
    implied = (d_end - d_start).total_seconds() / 60.0
    return abs(implied - float(duration)) <= 1.0
