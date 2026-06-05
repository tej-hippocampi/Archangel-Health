"""TEAM telehealth HCPCS G-code ladder (PRD §10.7)."""

from __future__ import annotations

from typing import Optional, Tuple

TYPE_OF_BILL = "13X"
REVENUE_CODE = "0780"
DEMO_CODE = "A9"
POS_HOME = "10"
POS_FACILITY = "02"
RIDE_ALONE = True

L45_GATED = frozenset({"G0663", "G0664", "G0668"})

_NEW_LADDER: Tuple[Tuple[int, str], ...] = (
    (10, "G0660"),
    (20, "G0661"),
    (30, "G0662"),
    (45, "G0663"),
    (60, "G0664"),
)

_ESTABLISHED_LADDER: Tuple[Tuple[int, str], ...] = (
    (10, "G0665"),
    (15, "G0666"),
    (25, "G0667"),
    (40, "G0668"),
)


class RideAloneViolation(Exception):
    """Raised when a TEAM telehealth claim would include a second/FFS line item."""


def pos_from_location(location: str) -> str:
    loc = (location or "").strip().upper()
    if loc == "FACILITY_OTHER":
        return POS_FACILITY
    return POS_HOME


def requires_l45_gate(hcpcs: str) -> bool:
    return (hcpcs or "").upper() in L45_GATED


def map_gcode(patient_type: str, duration_minutes: int) -> Optional[str]:
    pt = (patient_type or "").strip().upper()
    ladder = _NEW_LADDER if pt == "NEW" else _ESTABLISHED_LADDER
    code: Optional[str] = None
    for threshold, gcode in ladder:
        if duration_minutes >= threshold:
            code = gcode
    return code


def next_threshold(patient_type: str, duration_minutes: int) -> Optional[Tuple[str, int]]:
    pt = (patient_type or "").strip().upper()
    ladder = _NEW_LADDER if pt == "NEW" else _ESTABLISHED_LADDER
    for threshold, gcode in ladder:
        if duration_minutes < threshold:
            return gcode, threshold
    return None


def enforce_ride_alone(line_items: int) -> None:
    if line_items > 1:
        raise RideAloneViolation("RIDE_ALONE_VIOLATION: TEAM telehealth claims allow only one line item.")
