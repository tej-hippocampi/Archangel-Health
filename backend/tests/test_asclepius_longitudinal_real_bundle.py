"""The density gate, measured on the only REAL chart this repository contains.

Every other longitudinal test builds its chart from a fixture written to exercise
a specific branch. That proves the branches; it cannot tell you what the gate does
to clinical data nobody shaped for it. This file runs the shipped ingestion path
over ``nephrology_pgnmid_bundle.json`` — the committed masked-PGNMID partner
bundle the V4 ingestion tests already use — and pins what comes out.

**What comes out is zero decision points**, and that is the correct answer, not a
defect. The bundle is a cross-sectional diagnostic workup: five lab draws about
three weeks apart, and notes, medications and imaging that carry no timing at all
in the source FHIR. Three of its four encounters are a single lab draw; the fourth
has three dates and two resource types but only four recorded events against a
gate of eight. Nothing in it is a *decision followed by a later checkable
encounter*, so nothing in it is a trajectory point.

The reason to spend a test on a zero is that the zero is load-bearing in two
directions:

  * **Lowering the gate moves the headline number and nothing else.** Drop the
    event floor to four and encounter 3 qualifies, so the plan reports one
    decision point where it reported none — but ``verifiable_decision_points``
    stays at zero, because pairing needs a *second* qualifying encounter and
    there isn't one. That is the two-numbers rule doing its job, and it is also
    exactly how a lowered gate would mislead: the number that moves is the one
    quoted in a pitch, and the number that matters does not move at all.
  * **The published yield was not measured here.** ``LONGITUDINAL_CASES.md``
    quotes 59 encounters → 25 decision points → 21 verifiable, measured across
    ``patient-1`` … ``patient-4``. Those charts are not in this repository. This
    test is the only real-data measurement that lives with the code, and it says
    the yield of the real chart we do have is zero — so the published figures are
    inherited, not reproduced. A reader who assumes otherwise will over-promise
    to a buyer.

If a future change makes this bundle produce points, that is not automatically
progress: either the adapter learned to date the undated resources (real progress,
update the numbers here) or a gate moved (read the paragraph above first).
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import ingestion as I  # noqa: E402
from asclepius import real_cases as RC  # noqa: E402

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "nephrology_pgnmid_bundle.json"
_KEY = base64.urlsafe_b64encode(b"longitudinal-real-bundle-test032b").decode()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    A.fresh_store()
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))
    monkeypatch.setenv("ASCLEPIUS_ASSET_STORE", str(tmp_path / "assets"))
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", _KEY)
    yield


@pytest.fixture
def real_case():
    """The bundle through the SHIPPED ingestion path — adapters, timeline
    normalization, de-id — not ``json.load``. What the gate sees in production is
    the normalized case, and the undated resources that decide this outcome are a
    property of that normalization."""
    from asclepius.store import get_store

    store = get_store()
    raw = _FIXTURE.read_bytes()
    data = I.wrap_loose_files([{"filename": "bundle.json", "content": raw}],
                              specialty="nephrology")
    uid = store.new_upload_id()
    rp = I.store_raw(uid, data)
    store.insert_ingest_upload(upload_id=uid, link_id="L1", partner_id="P1",
                               filename="bundle.json", sha256=I.sha256_hex(data),
                               size_bytes=len(data), raw_path=rp, source_ip=None)
    summary = I.process_upload(store, uid)
    assert summary["status"] == "ingested", summary
    rows = store.list_ingest_cases(upload_id=uid)
    assert len(rows) == 1
    return rows[0]["case"]


# ── the measurement ───────────────────────────────────────────────────────────
def test_the_real_bundle_segments_into_four_encounters(real_case):
    encs = RC.segment_longitudinal_record(real_case)
    assert [e["offsets"] for e in encs] == [[-70], [-49], [-28], [-14, -7, 0]]


def test_the_real_bundle_yields_no_decision_point(real_case):
    """Zero, and every encounter says which threshold it missed."""
    encs = RC.segment_longitudinal_record(real_case)
    verdicts = [RC.qualify_encounter(real_case, e) for e in encs]
    assert [v["qualifies"] for v in verdicts] == [False, False, False, False]
    assert RC.pair_decision_points(real_case, encs) == []
    # The three single-draw contacts miss all three thresholds; the fourth misses
    # only the event count. A gate that reported a bare False could not say that.
    assert [len(v["reasons"]) for v in verdicts] == [3, 3, 3, 1]
    assert "recorded event(s); the gate is 8" in verdicts[-1]["reasons"][0]


def test_the_densest_encounter_is_one_threshold_away(real_case):
    """Named explicitly because it is the encounter a well-meaning change would
    admit. It clears dates and resource types and misses events 4-to-8."""
    encs = RC.segment_longitudinal_record(real_case)
    v = RC.qualify_encounter(real_case, encs[-1])
    assert v["n_distinct_dates"] >= RC.ENCOUNTER_MIN_DISTINCT_DATES
    assert v["n_resource_types"] >= RC.ENCOUNTER_MIN_RESOURCE_TYPES
    assert v["n_events"] == 4 < RC.ENCOUNTER_MIN_EVENTS
    # And admitting it would buy nothing: it is the LAST encounter, so it has no
    # later qualifying encounter to be verified against even if it qualified.
    assert encs[-1]["index"] == len(encs) - 1


def test_why_the_event_count_is_four_and_not_nineteen(real_case):
    """Eighteen items sit in the activity collections; seven carry a day offset.

    This is the ``_visible`` fail-closed rule seen from the other end: an item we
    cannot place on the axis is not counted as activity, because counting it would
    let a chart with one dated draw and six undated notes clear a density gate
    built to require observation over time. The source bundle's
    ``DocumentReference`` and ``Media`` entries carry no date element at all, so
    this is the data's property, not a lossy adapter."""
    timed = RC._timed_offsets(real_case, RC._ACTIVITY_COLLECTIONS)
    assert sorted(timed) == [-70, -49, -28, -14, -7, -7, 0]
    items = [it for key in RC._ACTIVITY_COLLECTIONS
             for it in (real_case.get(key) or [])]
    untimed = [it for it in items if RC._offset_of(it) is None]
    assert (len(items), len(untimed)) == (18, 11)
    assert len(untimed) > len(timed), "most of this chart is undated; that is the finding"


def test_lowering_the_event_floor_would_manufacture_an_unverifiable_point(monkeypatch,
                                                                          real_case):
    """The counterfactual, run rather than asserted in prose.

    With the floor at 4 the last encounter qualifies — and ``pair_decision_points``
    STILL returns nothing, because one qualifying encounter cannot be paired. The
    gate is not what stands between this chart and a sellable point; the chart is."""
    monkeypatch.setattr(RC, "ENCOUNTER_MIN_EVENTS", 4)
    encs = RC.segment_longitudinal_record(real_case)
    assert RC.qualify_encounter(real_case, encs[-1])["qualifies"] is True
    assert RC.pair_decision_points(real_case, encs) == []
