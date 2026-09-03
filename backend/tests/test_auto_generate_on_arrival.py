"""Auto-generate on arrival (Longitudinal E2E PRD §3, §6 "auto-generate").

Box 2 needed a click per upload. These tests pin what the flag removes — and,
more carefully, everything it must NOT remove:

  * the trigger is all three conditions or nothing;
  * the run fires ONCE, atomically claimed, whichever of the three requests
    completes the condition;
  * auto-created is never auto-served — points still land ``assigned_only``;
  * a per-case failure isolates and is COUNTED where an operator can read it.

The expensive half (frontier probe, candidate generation, judges) is faked: this
file is about the trigger and the isolation, not about generation quality, which
``test_asclepius_longitudinal*`` already owns.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests._asclepius as A  # noqa: E402

from asclepius import auto_generate as AG  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh():
    A.fresh_store()
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


def _upload(store, *, purpose=None, mode=None, armed=False):
    """An ingest upload row in whatever state the test needs."""
    uid = store.new_upload_id()
    link = store.create_upload_link(
        token_hash="th-" + uid, partner_id="P1", partner_label="Partner",
        specialty="hepatology", expires_at="2099-01-01T00:00:00", one_time=False,
        max_bytes=1_000_000, created_by="admin", purpose=purpose)
    store.insert_ingest_upload(upload_id=uid, link_id=link["link_id"], partner_id="P1",
                               filename="b.zip", sha256="sha-" + uid, size_bytes=10,
                               raw_path="/dev/null", source_ip=None)
    if purpose:
        store.set_upload_purpose(uid, purpose)
    if mode:
        store.set_upload_task_mode(uid, mode)
    if armed:
        store.set_upload_auto_generate(uid, True)
    return uid


class _Scheduler:
    """Stands in for ``BackgroundTasks.add_task`` and records what was queued."""

    def __init__(self):
        self.calls = []

    def __call__(self, fn, *args):
        self.calls.append((fn, args))


# ═══ the trigger — all three, or nothing ═════════════════════════════════════
@pytest.mark.parametrize("purpose,mode,armed,expected", [
    ("task_creation", "longitudinal", True, True),
    ("task_creation", "longitudinal", False, False),   # not armed
    ("task_creation", None, True, False),              # no mode
    (None, "longitudinal", True, False),               # no purpose
    ("brokering", "longitudinal", True, False),        # never becomes tasks
])
def test_the_trigger_requires_all_three_conditions(purpose, mode, armed, expected):
    store = _store()
    uid = _upload(store, purpose=purpose, mode=mode, armed=armed)
    sched = _Scheduler()
    res = AG.maybe_start(store, uid, actor="admin", schedule=sched)
    assert res["started"] is expected, res
    assert bool(sched.calls) is expected


def test_the_run_is_claimed_once_however_many_requests_race():
    """Three separate admin requests can complete the trigger, and each one calls
    ``maybe_start``. The claim is a conditional UPDATE, so exactly one wins — a
    check-then-write in Python would bill a 25-encounter chart twice."""
    store = _store()
    uid = _upload(store, purpose="task_creation", mode="longitudinal", armed=True)
    scheds = [_Scheduler() for _ in range(3)]
    results = [AG.maybe_start(store, uid, actor="admin", schedule=s) for s in scheds]
    assert sum(1 for r in results if r["started"]) == 1, results
    assert sum(len(s.calls) for s in scheds) == 1


def test_a_scheduling_failure_releases_the_claim():
    """Without this, a run that could not be scheduled would leave the upload
    permanently marked as having run and the only fix would be editing the DB."""
    store = _store()
    uid = _upload(store, purpose="task_creation", mode="static", armed=True)
    res = AG.maybe_start(store, uid, actor="admin", schedule=None)
    assert res["started"] is False
    assert store.get_ingest_upload(uid)["auto_generate_started_at"] is None
    # …and a later, working attempt still fires.
    sched = _Scheduler()
    assert AG.maybe_start(store, uid, actor="admin", schedule=sched)["started"] is True


def test_arming_an_already_run_bundle_is_refused_by_the_endpoint():
    """Re-arming would bill the whole chart a second time, so it is a 409 rather
    than a silent no-op — an admin who clicks it needs to know why nothing
    happened.

    This asserts the RULE (refused, and the flag is not flipped). Which of the
    two sentences comes back — finished, or started-and-never-recorded — is the
    companion test's business."""
    from fastapi.testclient import TestClient

    store = _store()
    uid = _upload(store, purpose="task_creation", mode="static", armed=True)
    AG.maybe_start(store, uid, actor="admin", schedule=_Scheduler())
    client = TestClient(A.app)
    admin_h = A.headers_for(A.make_user(store, role="admin"))
    r = client.post(f"/api/asclepius/admin/uploads/{uid}/auto-generate",
                    json={"enabled": True}, headers=admin_h)
    assert r.status_code == 409
    assert "bill the whole chart" in r.text or "billed twice" in r.text
    # Refused means refused: the flag must not have been flipped on the way out.
    assert AG.has_run(store.get_ingest_upload(uid))


def test_a_run_killed_by_a_deploy_says_so_rather_than_claiming_it_finished():
    """Two situations reach the same 409, and naming the wrong one wastes an
    operator's time.

    A run that FINISHED wrote a report. A run claimed and never finished — the
    process was redeployed mid-flight, an ordinary event — did not. The second
    reads as "nothing happened and the button is refusing me", so it says that
    and names the recovery. Neither re-arms: the claim is what stops a
    25-encounter chart being billed twice, and the manual path does the whole job.
    """
    from fastapi.testclient import TestClient

    store = _store()
    uid = _upload(store, purpose="task_creation", mode="static", armed=True)
    AG.maybe_start(store, uid, actor="admin", schedule=_Scheduler())
    client = TestClient(A.app)
    admin_h = A.headers_for(A.make_user(store, role="admin"))

    r = client.post(f"/api/asclepius/admin/uploads/{uid}/auto-generate",
                    json={"enabled": True}, headers=admin_h)
    assert r.status_code == 409
    assert "never recorded an outcome" in r.text, r.text
    assert "nothing about them is blocked" in r.text.lower()

    # …and once a run HAS recorded an outcome, the message is the other one.
    store.set_upload_auto_generate_report(uid, {"generated": 5, "cases": []})
    r = client.post(f"/api/asclepius/admin/uploads/{uid}/auto-generate",
                    json={"enabled": True}, headers=admin_h)
    assert r.status_code == 409
    assert "already had its automatic run" in r.text


# ═══ the per-partner default ═════════════════════════════════════════════════
def test_the_health_system_default_arms_a_new_upload():
    store = _store()
    hs = store.ensure_health_system("St Mary's", contact_email="a@b.c")
    store.set_health_system_auto_generate_default(hs["hs_id"], True)
    uid = store.new_upload_id()
    link = store.create_upload_link(
        token_hash="th-" + uid, partner_id=hs["hs_id"], partner_label="St Mary's",
        specialty="hepatology", expires_at="2099-01-01T00:00:00", one_time=False,
        max_bytes=1_000_000, created_by="admin", purpose="task_creation")
    store.insert_ingest_upload(upload_id=uid, link_id=link["link_id"],
                               partner_id=hs["hs_id"], filename="b.zip",
                               sha256="sha-" + uid, size_bytes=10,
                               raw_path="/dev/null", source_ip=None)
    store.attach_upload_provenance(uid, link_id=link["link_id"])
    assert store.get_ingest_upload(uid)["auto_generate"] == 1


def test_the_default_never_disarms_a_bundle_an_admin_armed():
    """An admin who armed this specific bundle must not have it undone by a
    partner-level 0."""
    store = _store()
    hs = store.ensure_health_system("Other General", contact_email="a@b.c")
    store.set_health_system_auto_generate_default(hs["hs_id"], False)
    uid = _upload(store, purpose="task_creation", mode="static", armed=True)
    assert store.apply_auto_generate_default(uid) is True
    assert store.get_ingest_upload(uid)["auto_generate"] == 1


# ═══ the run itself ══════════════════════════════════════════════════════════
def test_a_per_case_failure_isolates_and_the_batch_continues(monkeypatch):
    """One chart that cannot be planned must not stop the other two in the same
    bundle — and the failure must be READABLE afterwards, which is the half that
    was missing: a run reports success having dropped cases, and the chart is
    quietly short."""
    store = _store()
    uid = _upload(store, purpose="task_creation", mode="longitudinal", armed=True)
    ids = []
    for i in range(3):
        ic = store.insert_ingest_case(upload_id=uid, patient_key=f"pk-{i}",
                                      specialty="hepatology", status="ingested",
                                      case={"case_source": "real_deid"}, report={})
        ids.append(ic["ingest_case_id"] if isinstance(ic, dict) else ic)

    calls = {"n": 0}

    async def _fake_generate(ingest_case_id, body, background, admin):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("case judge rejected every encounter")
        return {"generated": 2, "gated": 1, "failed": 0,
                "trajectory_id": f"traj-{calls['n']}", "trajectory_points": 2,
                "details": {"gated": [{"encounter_index": 4, "failures": ["thin"]}],
                            "failed": []}}

    import routers.asclepius as R
    monkeypatch.setattr(R, "generate_real_cases", _fake_generate)

    report = asyncio.run(AG.run_upload(store, uid, "admin"))
    assert calls["n"] == 3, "the run must not stop at the failing case"
    assert report["generated"] == 4          # two cases × two points
    # Two counts, not one. A whole CHART that could not be planned and an
    # ENCOUNTER a case judge rejected are different events with different fixes,
    # and an operator reading a single number cannot tell which happened.
    assert report["cases_failed"] == 1       # the chart that raised
    assert report["failed"] == 0             # no per-encounter rejections here
    assert len(report["trajectories"]) == 2

    # …and it is on the row, as a count with the detail behind it.
    upload = store.get_ingest_upload(uid)
    summary = AG.failure_summary(upload)
    assert summary is not None
    assert summary["count"] >= 3, summary   # two gated encounters + the dead chart
    assert any(d.get("encounter_index") == 4 for d in summary["dropped"])
    # The dead chart is in the chip too. It lived only in the per-case entry, so a
    # bundle where EVERY chart failed produced no chip at all and read as clean.
    assert any("case judge rejected" in e for e in summary["errors"]), summary


def test_a_clean_run_grows_no_failure_chip(monkeypatch):
    store = _store()
    uid = _upload(store, purpose="task_creation", mode="static", armed=True)
    store.insert_ingest_case(upload_id=uid, patient_key="pk-0", specialty="hepatology",
                             status="ingested", case={"case_source": "real_deid"}, report={})

    async def _fake_generate(ingest_case_id, body, background, admin):
        return {"generated": 3, "gated": 0, "failed": 0, "details": {}}

    import routers.asclepius as R
    monkeypatch.setattr(R, "generate_real_cases", _fake_generate)
    asyncio.run(AG.run_upload(store, uid, "admin"))
    assert AG.failure_summary(store.get_ingest_upload(uid)) is None


def test_the_run_passes_the_uploads_declared_mode(monkeypatch):
    """A longitudinal upload must build a WALK, not a batch of static cases —
    the mode is stored on the row precisely so a resumed or unattended run
    continues the same way."""
    store = _store()
    seen = {}

    async def _fake_generate(ingest_case_id, body, background, admin):
        seen["trajectory"] = body.trajectory
        seen["dry_run"] = body.dry_run
        return {"generated": 1, "gated": 0, "failed": 0, "details": {}}

    import routers.asclepius as R
    monkeypatch.setattr(R, "generate_real_cases", _fake_generate)

    for mode, expected in (("longitudinal", True), ("static", False)):
        uid = _upload(store, purpose="task_creation", mode=mode, armed=True)
        store.insert_ingest_case(upload_id=uid, patient_key="pk", specialty="hepatology",
                                 status="ingested", case={"case_source": "real_deid"},
                                 report={})
        asyncio.run(AG.run_upload(store, uid, "admin"))
        assert seen["trajectory"] is expected
        assert seen["dry_run"] is False, "an unattended run must actually write"


def test_is_armed_and_has_run_describe_the_same_condition_as_the_claim():
    """The UI reads ``is_armed``; the authority is ``claim_auto_generate``. If
    they described different conditions a row would say "will build itself" and
    then not."""
    store = _store()
    armed = _upload(store, purpose="task_creation", mode="static", armed=True)
    not_armed = _upload(store, purpose="task_creation", mode="static", armed=False)
    assert AG.is_armed(store.get_ingest_upload(armed)) is True
    assert AG.is_armed(store.get_ingest_upload(not_armed)) is False
    assert store.claim_auto_generate(armed) is True
    assert store.claim_auto_generate(not_armed) is False
    assert AG.has_run(store.get_ingest_upload(armed)) is True
