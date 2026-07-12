"""Timeline normalization — the B1 bridge (Real EHR Ingestion PRD §7).

The PRD's explicit regression: after normalization, ZERO date strings survive
anywhere in the case (notes included) and ``deidentify()`` PASSES — where before
this module it rejected 100% of date-shifted partner data.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests._asclepius as A  # noqa: E402,F401  (env before imports)

from asclepius.timeline import (  # noqa: E402
    TimelineError,
    normalize_timeline,
    parse_datetime,
    rewrite_note_dates,
)


def _fragments(**over):
    base = {
        "demographics": {"age_band": "70-79", "sex": "M"},
        "lab_panels": [
            {"panel": "BMP", "collected_at": "2031-03-14", "results": [
                {"analyte": "Sodium", "value": 112, "unit": "mmol/L", "ref_low": 135, "ref_high": 145, "flag": "LL"}]},
            {"panel": "BMP", "collected_at": "2031-03-19T08:30:00Z", "results": [
                {"analyte": "Sodium", "value": 124, "unit": "mmol/L", "ref_low": 135, "ref_high": 145, "flag": "L"}]},
        ],
        "notes": [{"note_type": "Consult", "author_role": "nephrology",
                   "text": "Admitted 3/14/2031 with confusion; dialysis started 2031-03-19. Improving."}],
        "problem_list": [{"condition": "CKD", "since": "2027-06-02"}],
    }
    base.update(over)
    return base


# ─── parse_datetime ────────────────────────────────────────────────────────────
def test_parse_datetime_formats():
    from datetime import date
    assert parse_datetime("2031-03-14") == date(2031, 3, 14)
    assert parse_datetime("2031-03-14T09:30:00Z") == date(2031, 3, 14)
    assert parse_datetime("3/14/2031") == date(2031, 3, 14)
    assert parse_datetime("20310314") == date(2031, 3, 14)          # HL7 TS
    assert parse_datetime("203103140930") == date(2031, 3, 14)      # HL7 TS + time
    assert parse_datetime("not a date") is None
    assert parse_datetime(None) is None


# ─── structured conversion ─────────────────────────────────────────────────────
def test_offsets_anchor_to_latest_observation():
    case, report = normalize_timeline(_fragments())
    offs = [lp["collected_offset_days"] for lp in case["lab_panels"]]
    assert offs == [-5, 0]                       # intervals preserved exactly
    assert all(isinstance(o, int) for o in offs)
    assert all("collected_at" not in lp for lp in case["lab_panels"])
    assert report["index_source"] == "latest_observation"
    assert report["panels_converted"] == 2


def test_manifest_index_event_is_authoritative():
    case, report = normalize_timeline(_fragments(), index_event="2031-03-21")
    assert [lp["collected_offset_days"] for lp in case["lab_panels"]] == [-7, -2]
    assert report["index_source"] == "manifest"


def test_bad_manifest_index_raises():
    with pytest.raises(TimelineError):
        normalize_timeline(_fragments(), index_event="whenever")


def test_already_relative_offsets_untouched():
    frags = _fragments()
    frags["lab_panels"] = [{"panel": "BMP", "collected_offset_days": -3, "results": []}]
    frags["notes"] = [{"note_type": "Consult", "author_role": "neph", "text": "no dates here"}]
    case, report = normalize_timeline(frags)
    assert case["lab_panels"][0]["collected_offset_days"] == -3
    assert report["panels_converted"] == 0


def test_problem_since_generalizes_to_year():
    case, _ = normalize_timeline(_fragments())
    assert case["problem_list"][0]["since"] == "2027"


# ─── note rewriting ────────────────────────────────────────────────────────────
def test_note_dates_rewritten_to_relative():
    case, report = normalize_timeline(_fragments())
    text = case["notes"][0]["text"]
    assert "[day -5]" in text and "[day 0]" in text
    assert "3/14/2031" not in text and "2031-03-19" not in text
    assert report["note_dates_rewritten"] == 2


def test_month_name_dates_rewritten():
    from datetime import date
    out, k, unres = rewrite_note_dates("Seen March 14, 2031 and again Mar 19.", date(2031, 3, 19))
    assert out == "Seen [day -5] and again [day 0]."
    assert k == 2 and unres == []


def test_age_90_plus_collapses():
    from datetime import date
    out, _, _ = rewrite_note_dates("A 94-year-old with fatigue; also 97 yo sibling.", date(2031, 1, 1))
    assert "94" not in out and "97" not in out
    assert out.count("90+") == 2


def test_ambiguous_datelike_reported_masked_not_guessed():
    from datetime import date
    out, k, unres = rewrite_note_dates("Symptoms since 3/14 per family.", date(2031, 3, 19))
    assert "3/14" in out            # NOT rewritten — we never guess
    assert k == 0
    assert unres and all("3" not in u.replace("•", "") or "/" in u for u in unres)
    assert all("•" in u for u in unres)   # masked, never cleartext digits
    assert "3/14" not in unres[0]


# ─── THE B1 regression (PRD §11) ──────────────────────────────────────────────
def test_b1_normalized_case_passes_deidentify():
    """Before timeline.py, deidentify() rejected every date-shifted partner case.
    After normalization: zero date strings anywhere, guard PASSES."""
    from asclepius import case_formats as cf
    case, report = normalize_timeline(_fragments())
    assert report["unresolved"] == []
    clean = cf.deidentify(case)                       # must NOT raise
    assert clean["lab_panels"][0]["collected_offset_days"] == -5


def test_b1_unnormalized_case_still_rejected():
    """The guard itself stays hard: the same case WITHOUT normalization rejects
    (proves the guard wasn't loosened to make B1 pass)."""
    from asclepius import case_formats as cf
    frags = _fragments()
    for lp in frags["lab_panels"]:
        lp["collected_offset_days"] = lp.pop("collected_at")   # date string offsets
    with pytest.raises(cf.CaseIngestError):
        cf.deidentify(frags)


# ─── re-identification safety ─────────────────────────────────────────────────
def test_report_never_contains_the_index_date():
    """The resolved index date is a re-identification key — it must die with the
    ingest job. The report carries provenance only."""
    import json
    _, report = normalize_timeline(_fragments())
    blob = json.dumps(report)
    assert "2031" not in blob
    assert report["index_source"] == "latest_observation"
