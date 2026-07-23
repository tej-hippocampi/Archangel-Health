"""Eval UX Overhaul backend contracts: §11 citation-link validity and §13
step_note → step_error_tag derivation."""

from __future__ import annotations

import glob
import json
import os

from asclepius import citations
from asclepius.constants import STEP_CORRECTION_REASONS
from asclepius.packaging import apply_step_notes, derive_step_error_tag
from asclepius.validation import validate_submission  # noqa: F401  (import sanity)

_CIT_DIR = os.path.join(os.path.dirname(citations.__file__), "citations")


# ─── §11: every shipped citation-library entry has a well-formed URL ─────────
def test_every_shipped_citation_entry_has_a_well_formed_url():
    libs = glob.glob(os.path.join(_CIT_DIR, "*.json"))
    assert libs, "no citation libraries found"
    for path in libs:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        entries = data.get("citations") if isinstance(data, dict) else data
        assert entries, f"{os.path.basename(path)} has no entries"
        for c in entries:
            url = c.get("url")
            assert url and str(url).strip(), (
                f"{os.path.basename(path)} entry {c.get('id')} has no url — "
                "'Open source' must never dead-end")
            assert citations.is_well_formed_url(url), (
                f"{os.path.basename(path)} entry {c.get('id')} has malformed url {url!r}")


def test_validate_library_reports_clean_for_shipped_specialties():
    assert citations.validate_library("nephrology") == []


def test_public_strips_malformed_url_so_ui_renders_reference_only():
    bad = {"id": "x", "title": "T", "url": "not a link", "snippet": "s"}
    out = citations._public(bad)  # noqa: SLF001
    assert "url" not in out
    good = {"id": "y", "title": "T", "url": "https://kdigo.org/guidelines/ckd", "snippet": "s"}
    assert citations._public(good)["url"] == good["url"]  # noqa: SLF001


def test_is_well_formed_url():
    assert citations.is_well_formed_url("https://kdigo.org/guidelines/ckd-evaluation/")
    assert citations.is_well_formed_url("http://example.org")
    assert not citations.is_well_formed_url("")
    assert not citations.is_well_formed_url(None)
    assert not citations.is_well_formed_url("kdigo.org/guidelines")
    assert not citations.is_well_formed_url("javascript:alert(1)")
    assert not citations.is_well_formed_url("https://no spaces allowed.org/x")


# ─── §13: step_note → step_error_tag derivation ──────────────────────────────
def test_derive_step_error_tag_maps_onto_controlled_vocab():
    cases = {
        "this dose is unsafe with an eGFR of 20": "unsafe",
        "cites an outdated guideline; KDIGO has since updated this": "outdated_guideline",
        "checks potassium too late — wrong order of operations": "wrong_order",
        "omits the DDAVP clamp entirely": "incomplete",
        "just awkward phrasing, content is fine": "minor_wording",
        "treats the creatinine bump as intrinsic AKI": "factual_error",
    }
    for note, expected in cases.items():
        tag = derive_step_error_tag(note)
        assert tag == expected, f"{note!r} -> {tag}, expected {expected}"
        assert tag in STEP_CORRECTION_REASONS


def test_derive_step_error_tag_blank_is_none():
    assert derive_step_error_tag("") is None
    assert derive_step_error_tag("   ") is None
    assert derive_step_error_tag(None) is None


def test_apply_step_notes_backfills_reason_label_and_critique():
    steps = [
        {  # note-only corrected step (the V3/V4 flow)
            "step": 1, "text": "edited", "original_text": "orig", "corrected": True,
            "confirmed": False, "added": False, "correction_reason": None,
            "step_note": "omits the potassium recheck", "label": None, "step_reward": None,
            "critique": None,
        },
        {  # untouched confirmed step — must not be modified
            "step": 2, "text": "fine", "original_text": "fine", "corrected": False,
            "confirmed": True, "added": False, "correction_reason": None,
            "step_note": "", "label": "good", "step_reward": 1, "critique": None,
        },
        {  # corrected step that already picked a reason — reason kept
            "step": 3, "text": "edited2", "original_text": "orig2", "corrected": True,
            "confirmed": False, "added": False, "correction_reason": "unsafe",
            "step_note": "phrasing tweak", "label": "bad", "step_reward": 0,
            "critique": "explicit critique",
        },
    ]
    apply_step_notes(steps)

    s1 = steps[0]
    assert s1["step_error_tag"] == "incomplete"
    assert s1["correction_reason"] == "incomplete"
    assert s1["label"] == "bad" and s1["step_reward"] == 0
    assert s1["critique"] == "omits the potassium recheck"  # note doubles as critique

    s2 = steps[1]
    assert s2["step_error_tag"] is None
    assert s2["label"] == "good" and s2["step_reward"] == 1

    s3 = steps[2]
    assert s3["correction_reason"] == "unsafe"        # explicit pick kept
    assert s3["step_error_tag"] == "minor_wording"    # note still classified
    assert s3["critique"] == "explicit critique"      # never overwritten


def test_apply_step_notes_minor_wording_is_neutral():
    steps = [{
        "step": 1, "text": "e", "original_text": "o", "corrected": True,
        "confirmed": False, "added": False, "correction_reason": None,
        "step_note": "minor wording only", "label": None, "step_reward": None,
        "critique": None,
    }]
    apply_step_notes(steps)
    assert steps[0]["correction_reason"] == "minor_wording"
    assert steps[0]["label"] == "neutral"


def test_apply_step_notes_note_only_step_passes_validation_reason_check():
    """The §13 contract: after derivation, a note-only corrected step must not
    read as missing/unknown correction_reason downstream."""
    steps = [{
        "step": 1, "text": "edited", "original_text": "orig", "corrected": True,
        "confirmed": False, "added": False, "correction_reason": None,
        "step_note": "treats hemoconcentration as intrinsic AKI",
        "label": None, "step_reward": None, "critique": None,
    }]
    apply_step_notes(steps)
    reason = steps[0]["correction_reason"]
    assert reason and reason in STEP_CORRECTION_REASONS
