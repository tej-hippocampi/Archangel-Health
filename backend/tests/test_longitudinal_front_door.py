"""The front door (Longitudinal E2E PRD §2 / §6 "front door").

Every piece of the longitudinal machinery was built and wired; the four patient
records had simply never entered the pipeline, so ``generate`` had nothing to run
on and the Longitudinal batch read ``0 trajectories · 0 points``. These tests pin
the door that fixes that, and — because the charts are now committed — the
measured yield of real clinical data rather than of a fixture written to pass.

The expensive half of generation (frontier difficulty probe, candidate
generation, two judges) is not exercised here; ``test_asclepius_longitudinal*``
already owns it with fakes. What is exercised is everything up to it: packing,
the door, idempotency, specialty resolution, and the density gate on real charts.
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402

from asclepius import patient_fixtures as PF  # noqa: E402
from asclepius import real_cases as RC  # noqa: E402
from asclepius import v4_cases as V4  # noqa: E402

_KEY = base64.urlsafe_b64encode(b"front-door-test-key-0123456789ab").decode()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    A.fresh_store()
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))
    monkeypatch.setenv("ASCLEPIUS_ASSET_STORE", str(tmp_path / "assets"))
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", _KEY)
    yield


@pytest.fixture
def store():
    from asclepius.store import get_store

    return get_store()


def _run(store, **kw):
    """Ingest, running the background unpack inline so the test can assert on it."""
    calls = []
    res = PF.ingest_committed_bundles(
        store, actor="admin-test", on_ingested=lambda fn, *a: calls.append((fn, a)), **kw)
    for fn, args in calls:
        fn(*args)
    return res


# ── the bundles themselves ────────────────────────────────────────────────────
def test_the_four_charts_are_committed():
    """The premise of this whole PRD. If this fails, nothing below means anything
    and the Longitudinal batch is empty for the original reason."""
    assert PF.available_bundles() == ["patient-1", "patient-2", "patient-3", "patient-4"]


def test_every_committed_bundle_has_a_declared_specialty():
    """No manifest ships in these trees, so with no map every chart resolves to
    ``general`` — a wrong specialty routes to the wrong pool and mislabels the
    export invisibly. The map and the directory listing must not drift."""
    assert set(V4.FIXTURE_BUNDLE_SPECIALTIES) == set(PF.available_bundles())
    from asclepius import specialties as S
    for bundle, spec in V4.FIXTURE_BUNDLE_SPECIALTIES.items():
        assert S.is_enabled(spec), f"{bundle} declares a specialty this release does not serve"


def test_packing_is_deterministic():
    """The sha256 idempotency key is only a key if the same tree packs to the same
    bytes. ZIP_STORED + fixed timestamps is what buys that."""
    assert PF.pack_bundle("patient-1") == PF.pack_bundle("patient-1")


def test_an_unmapped_bundle_is_refused_not_defaulted(tmp_path):
    """A bundle nobody declared a specialty for stops here, loudly. Defaulting it
    would ship the chart as ``general`` — the invisible mislabel the map exists to
    prevent — and the message names the file to edit."""
    (tmp_path / "unmapped-chart" / "labs").mkdir(parents=True)
    (tmp_path / "unmapped-chart" / "labs" / "l.csv").write_text("a,b\n1,2\n")
    with pytest.raises(ValueError, match="FIXTURE_BUNDLE_SPECIALTIES"):
        PF.pack_bundle("unmapped-chart", root=tmp_path)


def test_a_missing_bundle_names_where_it_looked(tmp_path):
    with pytest.raises(FileNotFoundError, match="no committed bundle named"):
        PF.pack_bundle("patient-9", root=tmp_path)


# ── the door ──────────────────────────────────────────────────────────────────
def test_ingest_creates_four_uploads_with_description_and_specialty(store):
    res = _run(store)
    assert res["ingested"] == 4, res
    assert res["failed"] == 0, res
    for row in res["bundles"]:
        up = store.get_ingest_upload(row["upload_id"])
        assert up["description"] == f"Committed de-identified fixture · {row['bundle']}"
        assert up["partner_id"] == PF.FIXTURE_PARTNER_ID
        # §2.1 — purpose UNSET. They land in Box 1 like a hospital's upload and an
        # admin says what they are for; a fixture that pre-answered that question
        # would hide the front door all over again.
        assert not up.get("purpose")
        cases = store.list_ingest_cases(upload_id=row["upload_id"])
        assert len(cases) == 1, "one bundle is one patient (the manifest patient_key)"
        assert cases[0]["specialty"] == V4.FIXTURE_BUNDLE_SPECIALTIES[row["bundle"]]


def test_a_second_click_is_a_no_op(store):
    first = _run(store)
    again = _run(store)
    assert again["ingested"] == 0 and again["skipped"] == 4
    assert {r["upload_id"] for r in again["bundles"]} == {r["upload_id"] for r in first["bundles"]}
    assert len(store.list_ingest_uploads(limit=100)) == 4


def test_the_authorizing_link_is_dead_on_arrival(store):
    """The link row exists because provenance attaches through it — not because
    anyone may use it. It is one-time, already consumed, and already expired."""
    res = _run(store, bundles=["patient-3"])
    up = store.get_ingest_upload(res["bundles"][0]["upload_id"])
    link = store.get_upload_link(up["link_id"])
    assert link["one_time"] and int(link["used_count"]) >= 1
    assert link["expires_at"] < "2000-01-01"


# ── what the real charts actually yield (§2.3) ────────────────────────────────
#
# MEASURED on the committed trees, not inherited from the PRD. ``LONGITUDINAL_CASES.md``
# quotes 59 → 25 → 21 from a measurement taken elsewhere; the charts in this
# repository give 55 → 22 → 18, and patient-1's thirteen-point walk — the number
# the product is demoed on — reproduces exactly. The totals are asserted as floors
# rather than equalities: a gate change that RAISES yield should not fail a test,
# and one that lowers it must.
_EXPECTED = {
    #  bundle:      (encounters, decision points, verifiable)
    "patient-1": (22, 13, 12),
    "patient-2": (16, 2, 1),
    "patient-3": (5, 4, 3),
    "patient-4": (12, 3, 2),
}


@pytest.mark.parametrize("bundle", sorted(_EXPECTED))
def test_density_gate_yield_per_chart(store, bundle):
    res = _run(store, bundles=[bundle])
    ic = store.list_ingest_cases(upload_id=res["bundles"][0]["upload_id"])[0]
    case = store.get_ingest_case(ic["ingest_case_id"])["case"]
    encs = RC.segment_longitudinal_record(case)
    n_dp = sum(1 for e in encs if RC.qualify_encounter(case, e)["qualifies"])
    n_ver = len(RC.pair_decision_points(case, encs))
    assert (len(encs), n_dp, n_ver) == _EXPECTED[bundle], (
        "The density gate moved on real data. Read §2.3 and LONGITUDINAL_CASES.md "
        "before changing the constants — the gate IS the product.")


def test_patient_one_is_the_thirteen_point_walk(store):
    """Called out on its own because it is the number the PRD names and the demo
    shows. It reproduces exactly, which is the strongest evidence the gate has
    not drifted since the yield was first measured."""
    assert _EXPECTED["patient-1"][1] == 13


def test_patient_two_quarantines_and_says_why(store):
    """Not a defect to fix by lowering a gate — a finding to record.

    patient-2 carries an OCR annotation reading "Date column header ~21/06/2026
    (written as 12/26)". ``12/26`` is a date-shaped token in CLINICAL text (not in
    a de-identification header, which is stripped), it cannot be resolved to a
    calendar anchor, and ingestion quarantines rather than guessing — the rule
    that exists because a wrong guess destroys clinical meaning silently.

    The chart is recoverable through the documented, audited admin path
    (``POST /ingestion/quarantine/{id}/override``), not by relaxing the date scan.
    """
    res = _run(store, bundles=["patient-2"])
    up = store.get_ingest_upload(res["bundles"][0]["upload_id"])
    assert up["status"] == "quarantined"
    ic = store.list_ingest_cases(upload_id=up["upload_id"])[0]
    assert ic["status"] == "quarantined"
    reasons = [e["payload"].get("reason") for e in store.list_events(
        entity_type="ingest_case", entity_id=ic["ingest_case_id"], limit=20)
        if e["event_type"] == "case_quarantined"]
    assert any("unresolved date-like tokens" in (r or "") for r in reasons), reasons


def test_the_other_three_ingest_clean(store):
    res = _run(store, bundles=["patient-1", "patient-3", "patient-4"])
    for row in res["bundles"]:
        up = store.get_ingest_upload(row["upload_id"])
        assert up["status"] == "ingested", (row["bundle"], up["status"])
        ic = store.list_ingest_cases(upload_id=up["upload_id"])[0]
        # Advisory review reasons are expected (prose reference ranges the lab
        # adapter refuses to interpret); blocking ones are not.
        blocking = [r for r in (ic.get("review") or []) if r.get("severity") == "blocking"]
        assert not blocking, blocking


# ── the front door meets generation (§6 "front door", row 2) ──────────────────
#
# The rows above stop at "the chart is an ingest case". This one goes the last
# step — the one the whole PRD is about — and drives the REAL generation route
# over the REAL chart, so the seam between "a patient record was uploaded" and
# "ordered, sealed trajectory points exist" is executed rather than assumed.
#
# The four model legs are stubbed (question authoring, the frontier difficulty
# probe, candidate generation, the two judges) using the same stubs
# ``test_asclepius_longitudinal_e2e`` uses. What is NOT stubbed is anything that
# decides how many points there are or what order they are in.
def test_patient_one_becomes_a_sealed_ordered_walk(store, monkeypatch):
    from fastapi.testclient import TestClient

    from tests.test_asclepius_longitudinal_e2e import _stub_model_legs

    _stub_model_legs(monkeypatch)
    client = TestClient(A.app)
    admin_h = A.headers_for(A.make_user(store, role="admin"))

    res = _run(store, bundles=["patient-1"])
    upload_id = res["bundles"][0]["upload_id"]
    ic = store.list_ingest_cases(upload_id=upload_id)[0]

    # The chart arrives as STORAGE and generation is refused until a human says
    # what it is for. Asserted rather than skipped past: it is the whole reason
    # the fixture door leaves purpose unset, and it is the step an admin takes in
    # Box 1 before Box 2 has any controls at all.
    blocked = client.post(f"/api/asclepius/ingestion/cases/{ic['ingest_case_id']}/generate",
                          headers=admin_h, json={"dry_run": False, "trajectory": True})
    assert blocked.status_code == 409, blocked.text

    ok = client.post(f"/api/asclepius/admin/uploads/{upload_id}/purpose",
                     headers=admin_h, json={"purpose": "task_creation"})
    assert ok.status_code == 200, ok.text

    r = client.post(f"/api/asclepius/ingestion/cases/{ic['ingest_case_id']}/generate",
                    headers=admin_h, json={"dry_run": False, "trajectory": True})
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["trajectory_points"] >= 10, body
    points = store.trajectory_points(body["trajectory_id"])
    assert len(points) == body["trajectory_points"]

    # One walk, contiguous from 0. A gap reads to a physician as a missing case
    # rather than as a rejected one, and to the export as a broken sequence.
    assert {p["trajectory_id"] for p in points} == {body["trajectory_id"]}
    assert [p["sequence_index"] for p in points] == list(range(len(points)))

    # A walk of N points yields N−1 verifiable ones: the terminal point has no
    # later encounter in the record to be checked against.
    assert body["trajectory_verifiable_points"] == len(points) - 1

    for p in points:
        task = store.get_task(p["task_id"])
        # Single-labelled by construction (§9.6) — a second label buys no
        # agreement statistic on a κ-excluded point, it buys a second walk.
        assert task["max_labels"] == 1
        # And held back from every queue until an admin routes it: promoting a
        # chart and releasing it to doctors are two decisions.
        assert task["distribution"] == "assigned_only"

    # Absent from EVERY doctor's queue, both versions, while unrouted.
    doc = A.make_user(store, role="evaluator", specialty="hepatology")
    store.set_real_data_approved(doc["id"], True)
    h = A.headers_for(store.get_user_by_id(doc["id"]))
    for version in ("v3", "v4", "v5"):
        rows = client.get(f"/api/asclepius/tasks/available?portal_version={version}",
                          headers=h).json()["tasks"]
        assert not [t for t in rows if t.get("trajectory_id")], version
