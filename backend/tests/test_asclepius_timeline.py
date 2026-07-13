"""Timeline normalization (Data Provider Portal PRD §2 B1, §7.4).

The B1 killer bug: real de-identified data is date-shifted but still carries date
STRINGS, which ``deidentify()`` rejects and ``collected_offset_days`` (an int)
can't hold. ``timeline.normalize_case_timeline`` must convert every timestamp to a
relative integer offset and rewrite dates inside note text — so that afterwards
``deidentify()`` PASSES and zero date strings survive.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asclepius.case_formats import deidentify  # noqa: E402
from asclepius.timeline import (  # noqa: E402
    normalize_case_timeline,
    offset_days,
    remaining_date_strings,
    rewrite_text_dates,
)
from asclepius.validation import residual_identifiers  # noqa: E402


def _shifted_case():
    """A realistic date-shifted fragment: three lab panels a week apart and a note
    that references calendar dates — exactly the shape an adapter emits."""
    return {
        "specialty": "nephrology",
        "demographics": {"age_band": "70-79", "sex": "F"},
        "lab_panels": [
            {"panel": "BMP", "collected_at": "2025-03-01",
             "results": [{"analyte": "Creatinine", "value": 1.1, "unit": "mg/dL"}]},
            {"panel": "BMP", "collected_at": "2025-03-08",
             "results": [{"analyte": "Creatinine", "value": 2.4, "unit": "mg/dL", "flag": "H"}]},
            {"panel": "BMP", "collected_at": "2025-03-15",
             "results": [{"analyte": "Creatinine", "value": 3.9, "unit": "mg/dL", "flag": "HH"}]},
        ],
        "notes": [{
            "note_type": "Progress", "author_role": "nephrology",
            "text": "Baseline labs on 3/1/2025 were normal. By 2025-03-15 creatinine had tripled.",
        }],
    }


# ─── unit helpers ─────────────────────────────────────────────────────────────
def test_offset_days_relative_and_signed():
    anchor = date(2025, 3, 15)
    assert offset_days("2025-03-15", anchor) == 0
    assert offset_days("2025-03-08", anchor) == -7
    assert offset_days("2025-03-01", anchor) == -14
    assert offset_days("3/1/2025", anchor) == -14


def test_rewrite_text_dates_to_relative_tokens():
    anchor = date(2025, 3, 15)
    text = "Labs on 3/1/2025 normal; by 2025-03-15 tripled."
    out, n = rewrite_text_dates(text, anchor)
    assert n == 2
    assert "[day -14]" in out
    assert "[day 0]" in out
    assert not residual_identifiers(out)  # no date survives


# ─── the core normalization ───────────────────────────────────────────────────
def test_latest_collection_is_day_zero():
    case, report = normalize_case_timeline(_shifted_case())
    offsets = [p["collected_offset_days"] for p in case["lab_panels"]]
    assert offsets == [-14, -7, 0]              # latest panel is the index
    assert all(isinstance(o, int) for o in offsets)
    assert report["anchor_source"] == "latest_collection"
    assert report["panels_converted"] == 3


def test_collected_at_is_dropped():
    case, _ = normalize_case_timeline(_shifted_case())
    assert all("collected_at" not in p for p in case["lab_panels"])


def test_note_dates_rewritten_relative():
    case, report = normalize_case_timeline(_shifted_case())
    note = case["notes"][0]["text"]
    assert "[day -14]" in note and "[day 0]" in note
    assert report["dates_rewritten"] >= 2


def test_manifest_index_event_overrides_anchor():
    # Pin day 0 to the first panel instead of the latest → all offsets ≥ 0.
    case, report = normalize_case_timeline(_shifted_case(), index_event="2025-03-01")
    offsets = [p["collected_offset_days"] for p in case["lab_panels"]]
    assert offsets == [0, 7, 14]
    assert report["anchor_source"] == "manifest_index_event"


def test_no_dates_leaves_case_welltyped():
    case = {"lab_panels": [{"panel": "BMP", "collected_offset_days": -3, "results": []}],
            "notes": [{"text": "no dates here"}]}
    out, report = normalize_case_timeline(case)
    assert report["anchor_source"] == "none"
    assert out["lab_panels"][0]["collected_offset_days"] == -3


# ─── the B1 regression: normalize → deidentify() PASSES ───────────────────────
def test_b1_regression_deidentify_passes_after_normalize():
    """Before normalization the raw fragment is FULL of date strings that
    ``deidentify()`` rejects. After normalization: zero date strings survive and
    ``deidentify()`` passes. This is the acceptance criterion from PRD §10."""
    raw = _shifted_case()

    # Precondition: raw data really is rejected (dates present).
    assert remaining_date_strings(raw)  # the raw fragment carries date strings

    normalized, _ = normalize_case_timeline(raw)

    # Post-condition #1: zero date strings anywhere, notes included.
    assert remaining_date_strings(normalized) == []

    # Post-condition #2: the de-id guard now passes and returns a clean case.
    safe = deidentify(normalized)
    assert safe["lab_panels"][0]["collected_offset_days"] == -14
    assert isinstance(safe["lab_panels"][0]["collected_offset_days"], int)


def test_does_not_mutate_caller():
    raw = _shifted_case()
    normalize_case_timeline(raw)
    assert raw["lab_panels"][0]["collected_at"] == "2025-03-01"  # original untouched
