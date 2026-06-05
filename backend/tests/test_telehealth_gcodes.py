from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telehealth.gcodes import (  # noqa: E402
    RideAloneViolation,
    enforce_ride_alone,
    map_gcode,
    next_threshold,
    pos_from_location,
    requires_l45_gate,
)


def test_map_gcode_established_17_min():
    assert map_gcode("ESTABLISHED", 17) == "G0666"


def test_map_gcode_established_47_min_requires_l45():
    code = map_gcode("ESTABLISHED", 47)
    assert code == "G0668"
    assert requires_l45_gate(code)


def test_map_gcode_new_25_min():
    assert map_gcode("NEW", 25) == "G0661"
    assert map_gcode("NEW", 35) == "G0662"


def test_next_threshold():
    nxt = next_threshold("ESTABLISHED", 12)
    assert nxt == ("G0666", 15)


def test_pos_from_location():
    assert pos_from_location("HOME") == "10"
    assert pos_from_location("FACILITY_OTHER") == "02"


def test_ride_alone_violation():
    try:
        enforce_ride_alone(2)
        assert False, "expected RideAloneViolation"
    except RideAloneViolation as exc:
        assert "RIDE_ALONE_VIOLATION" in str(exc)
