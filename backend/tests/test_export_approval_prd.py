"""Export & Approval PRD — one approval, one export tab.

The bug this PRD closed: a physician's submission carried THREE statuses that
never spoke to each other. `earnings.status` (the ledger), `submissions.status`
(the QA pipeline) and `records.status` (the only thing export reads). Approving
payment never touched the third; QA approval never touched the first; and no
admin action moved all three. So a case could be approved, paid, and permanently
unshippable, and the export tab would quietly ship a different case instead
without saying why.

The tests are grouped as §6 lists them: approve · ui · export · migration ·
buyers.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A                       # noqa: E402
from asclepius import export_backfill                   # noqa: E402
from asclepius import export_inventory                  # noqa: E402
from asclepius import payments as asc_payments          # noqa: E402
from asclepius import pipeline as asc_pipeline          # noqa: E402
from asclepius import profiles as asc_profiles          # noqa: E402
from routers import asclepius_admin as admin_router     # noqa: E402
from routers import asclepius_payments as pay_router    # noqa: E402

client = TestClient(A.app)

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
_DOM_SHIM = Path(__file__).resolve().parent / "_asclepius_dom.js"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    A.fresh_store()
    asc_profiles.clear_cache()

    async def _ok_critic(task, submission):
        return {"consistent": True, "issues": [], "skipped": True}

    async def _ok_grounding(task, submission):
        return {"grounding_ok": True, "issues": [], "skipped": True, "checked_anchors": 0}

    monkeypatch.setattr(asc_pipeline, "run_critic", _ok_critic)
    monkeypatch.setattr(asc_pipeline, "run_grounding_check", _ok_grounding)
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


def _admin_h():
    return A.headers_for(A.make_user(_store(), role="admin"))


def _evaluator(specialty="nephrology"):
    return A.make_user(_store(), role="evaluator", specialty=specialty,
                       board_cert="board_certified_nephrology", years_experience=12)


def _task_body(**kw):
    base = {
        "specialty": "nephrology", "difficulty": "hard", "max_labels": 1,
        "prompt": f"Hyperkalemia case {A.uniq(8)}?",
        "candidate_answers": [{"id": "A", "text": "Calcium then dialyze."},
                              {"id": "B", "text": "Dialysate K+ 1.0."}],
    }
    base.update(kw)
    return base


def _submit(admin_h, ev, **task_kw):
    """One graded case. Returns (task_id, submission_id)."""
    ev_h = A.headers_for(ev)
    tid = client.post("/api/asclepius/tasks", json={"tasks": [_task_body(**task_kw)]},
                      headers=admin_h).json()["created"][0]
    sid = "s-" + uuid.uuid4().hex[:12]
    r = client.post("/api/asclepius/submissions", json={
        "submission_id": sid, "task_id": tid, "verdict": "A_better",
        "chosen_id": "A", "rejected_id": "B", "time_spent_sec": 130,
        "prompt_review": {"reviewed": True, "verdict": "valid"},
        "independent_answer": {"text": "Stabilize with IV calcium, shift potassium "
                                       "with insulin and dextrose, then dialyze."},
        "chosen_revision": {"edited": False, "why_better_notes": "B over-lowers K+"},
        "rejected_critique": {"error_tags": ["dosing_error"], "why_worse": "too aggressive"},
    }, headers=ev_h)
    assert r.status_code == 200, r.text
    return tid, sid


def _hold_back(sid: str, status: str = "submitted") -> None:
    """Put a submission and its records back in a pre-approval state.

    The happy path drives a clean submission straight to ``export_ready``, which
    is precisely the state these tests need NOT to be in: the population this
    PRD is about is work that stalled short of it.
    """
    store = _store()
    store.update_submission(sid, status=status)
    store.update_records_status_for_submission(sid, status)


def _accrue(user, sid, *, status="accrued", cents=7500, kind="task"):
    """A ledger row accrued JUST NOW.

    Not a fixed date: `reconcile_task_accruals` runs an auto-approve sweep over
    anything older than the window, so a hardcoded timestamp turns into a test
    that silently changes meaning as the calendar moves past it. The tests that
    WANT the sweep set an old date explicitly.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    return _store().insert_earning(
        earning_id="e-" + uuid.uuid4().hex[:10], user_id=user["id"], kind=kind,
        ref_id=sid, amount_cents=cents, rate_cents=cents, status=status,
        accrued_at=now, resolved_at=None if status == "accrued" else now)


def _statuses(sid):
    store = _store()
    sub = store.get_submission(sid)
    recs = [r["status"] for r in store.records_for_submission(sid)] \
        if hasattr(store, "records_for_submission") else None
    if recs is None:
        with store._conn() as conn:
            recs = [r[0] for r in conn.execute(
                "SELECT status FROM records WHERE submission_id = ?", (sid,)).fetchall()]
    earn = store.get_earning(kind="task", ref_id=sid)
    return {"submission": sub["status"], "records": set(recs),
            "ledger": (earn or {}).get("status")}


def _events(entity_id, event_type):
    store = _store()
    with store._conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM events WHERE entity_id = ? AND event_type = ?",
            (entity_id, event_type)).fetchone()[0]


# ═══════════════════════════════════════════════════════════════════════════
#  approve
# ═══════════════════════════════════════════════════════════════════════════
def test_approve_moves_the_ledger_the_submission_and_the_records_together():
    """§1.1 — the whole point. One action, three writes, one meaning."""
    admin_h = _admin_h()
    ev = _evaluator()
    _tid, sid = _submit(admin_h, ev)
    _hold_back(sid)
    earning = _accrue(ev, sid)

    before = _statuses(sid)
    assert before == {"submission": "submitted", "records": {"submitted"},
                      "ledger": "accrued"}

    r = client.post(f"/api/asclepius/admin/earnings/{earning['earning_id']}/approve",
                    json={"note": ""}, headers=admin_h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["approved"] is True
    # The response SAYS whether the case can now ship. An approval that pays and
    # leaves the case unshippable is the silence this PRD removes.
    assert body["exportable"] is True
    assert body["submission_status"] == "export_ready"

    assert _statuses(sid) == {"submission": "export_ready",
                              "records": {"export_ready"}, "ledger": "approved"}
    # Auditable, and it says out loud that QA sampling was bypassed.
    assert _events(earning["earning_id"], "earning_admin_approved") == 1


def test_the_admin_approval_event_records_what_it_bypassed_and_where_it_came_from():
    admin_h = _admin_h()
    ev = _evaluator()
    _tid, sid = _submit(admin_h, ev)
    _hold_back(sid, "needs_qa")
    earning = _accrue(ev, sid)
    client.post(f"/api/asclepius/admin/earnings/{earning['earning_id']}/approve",
                json={}, headers=admin_h)
    store = _store()
    with store._conn() as conn:
        row = conn.execute(
            "SELECT payload_json FROM events WHERE entity_id = ? "
            "AND event_type = 'earning_admin_approved'",
            (earning["earning_id"],)).fetchone()
    payload = json.loads(row[0])
    assert payload["prior_ledger"] == "accrued"
    assert payload["prior_qa"] == "needs_qa"
    assert payload["submission_id"] == sid
    assert payload["bypassed_qa_sampling"] is True


def test_a_second_approve_is_a_409_not_a_double_write():
    admin_h = _admin_h()
    ev = _evaluator()
    _tid, sid = _submit(admin_h, ev)
    _hold_back(sid)
    earning = _accrue(ev, sid)
    eid = earning["earning_id"]
    assert client.post(f"/api/asclepius/admin/earnings/{eid}/approve",
                       json={}, headers=admin_h).status_code == 200
    second = client.post(f"/api/asclepius/admin/earnings/{eid}/approve",
                         json={}, headers=admin_h)
    assert second.status_code == 409
    assert "already approved" in second.json()["detail"].lower()
    assert _events(eid, "earning_admin_approved") == 1


def test_approve_never_downgrades_an_exported_case():
    """`exported` means the bytes are with a buyer. A payment decision does not
    reach back and change that."""
    admin_h = _admin_h()
    ev = _evaluator()
    _tid, sid = _submit(admin_h, ev)
    _hold_back(sid, "exported")
    earning = _accrue(ev, sid)
    r = client.post(f"/api/asclepius/admin/earnings/{earning['earning_id']}/approve",
                    json={}, headers=admin_h)
    assert r.status_code == 200
    # The LEDGER moved (the money is owed either way); the export gate did not.
    assert r.json()["records_outcome"] == "terminal"
    st = _statuses(sid)
    assert st["ledger"] == "approved"
    assert st["submission"] == "exported"
    assert st["records"] == {"exported"}


def test_approve_never_resurrects_a_rejected_case():
    """A rejected case is Void's business, not Approve's."""
    admin_h = _admin_h()
    ev = _evaluator()
    _tid, sid = _submit(admin_h, ev)
    _hold_back(sid, "rejected")
    earning = _accrue(ev, sid)
    r = client.post(f"/api/asclepius/admin/earnings/{earning['earning_id']}/approve",
                    json={}, headers=admin_h)
    assert r.status_code == 200
    assert r.json()["records_outcome"] == "terminal"
    st = _statuses(sid)
    assert st["submission"] == "rejected" and st["records"] == {"rejected"}


@pytest.mark.parametrize("ledger_status,fragment", [
    ("paid", "already been paid"),
    ("void", "voided"),
])
def test_only_from_accrued_is_respected(ledger_status, fragment):
    admin_h = _admin_h()
    ev = _evaluator()
    _tid, sid = _submit(admin_h, ev)
    _hold_back(sid)
    earning = _accrue(ev, sid, status=ledger_status)
    r = client.post(f"/api/asclepius/admin/earnings/{earning['earning_id']}/approve",
                    json={}, headers=admin_h)
    assert r.status_code == 409
    assert fragment in r.json()["detail"].lower()
    # And nothing moved.
    assert _statuses(sid)["records"] == {"submitted"}


def test_a_quality_held_row_is_refused_and_points_at_release():
    """The hold is the promise that an automated pay cut never applies without a
    person deciding it. Approving through here would apply the reduced amount
    while looking like a plain approval."""
    admin_h = _admin_h()
    ev = _evaluator()
    _tid, sid = _submit(admin_h, ev)
    _hold_back(sid)
    earning = _accrue(ev, sid)
    _store().set_earning_quality(earning["earning_id"], multiplier=0.8,
                                 reasons=["late"], version="v1", hold=True)
    r = client.post(f"/api/asclepius/admin/earnings/{earning['earning_id']}/approve",
                    json={}, headers=admin_h)
    assert r.status_code == 409
    assert "release" in r.json()["detail"].lower()
    assert _statuses(sid)["ledger"] == "accrued"


def test_every_approve_refusal_has_an_http_answer():
    """A refusal token with no mapping would reach the console as a 500."""
    assert set(asc_payments.APPROVE_REFUSALS) == set(pay_router._APPROVE_HTTP)


def test_void_mirrors_approve():
    """§1.1 — one button each way. Void moves the ledger AND the export gate."""
    admin_h = _admin_h()
    ev = _evaluator()
    _tid, sid = _submit(admin_h, ev)
    earning = _accrue(ev, sid)     # left at export_ready by the happy path
    assert _statuses(sid)["records"] == {"export_ready"}

    r = client.post(f"/api/asclepius/admin/earnings/{earning['earning_id']}/void",
                    json={"reason": "duplicate of an earlier submission"},
                    headers=admin_h)
    assert r.status_code == 200, r.text
    assert r.json()["voided"] is True
    assert _statuses(sid) == {"submission": "rejected", "records": {"rejected"},
                              "ledger": "void"}


def test_void_refuses_a_paid_row_and_leaves_the_records_alone():
    admin_h = _admin_h()
    ev = _evaluator()
    _tid, sid = _submit(admin_h, ev)
    _accrue(ev, sid, status="paid")
    earning = _store().get_earning(kind="task", ref_id=sid)
    r = client.post(f"/api/asclepius/admin/earnings/{earning['earning_id']}/void",
                    json={"reason": "changed my mind"}, headers=admin_h)
    assert r.status_code == 409
    assert _statuses(sid)["records"] == {"export_ready"}


def test_void_does_not_recall_an_already_exported_case():
    admin_h = _admin_h()
    ev = _evaluator()
    _tid, sid = _submit(admin_h, ev)
    _hold_back(sid, "exported")
    earning = _accrue(ev, sid)
    r = client.post(f"/api/asclepius/admin/earnings/{earning['earning_id']}/void",
                    json={"reason": "buyer complained about this case"},
                    headers=admin_h)
    assert r.status_code == 200
    assert r.json()["records_outcome"] == "terminal"
    assert _statuses(sid)["records"] == {"exported"}


def test_auto_approve_now_makes_the_case_exportable():
    """The 14-day sweep used to pay a case and silently leave it unshippable."""
    admin_h = _admin_h()
    ev = _evaluator()
    _tid, sid = _submit(admin_h, ev)
    _hold_back(sid)
    store = _store()
    store.insert_earning(
        earning_id="e-old", user_id=ev["id"], kind="task", ref_id=sid,
        amount_cents=7500, rate_cents=7500, status="accrued",
        accrued_at="2020-01-01T00:00:00", resolved_at=None)
    now = datetime.now(timezone.utc) + timedelta(days=1)
    moved = asc_payments._auto_approve(store, now=now)
    assert moved >= 1
    assert _statuses(sid) == {"submission": "export_ready",
                              "records": {"export_ready"}, "ledger": "approved"}


def test_reviewer_accept_now_makes_the_case_exportable():
    """A reviewer's accept approves the money; from now on it approves the
    export too."""
    admin_h = _admin_h()
    ev = _evaluator()
    _tid, sid = _submit(admin_h, ev)
    _hold_back(sid)
    store = _store()
    store.insert_earning(
        earning_id="e-rev", user_id=ev["id"], kind="task", ref_id=sid,
        amount_cents=7500, rate_cents=7500, status="accrued",
        accrued_at="2026-08-01T00:00:00", resolved_at=None)
    moved = asc_payments.apply_ledger_decision_to_records(
        store, submission_id=sid, decision="approve", reason="reviewer_accepted")
    assert moved["moved"] is True
    assert _statuses(sid)["records"] == {"export_ready"}


def test_the_four_paths_all_write_both_tables():
    """§3 — a record ships iff `records.status ∈ {export_ready, exported}`, and
    exactly FOUR events set it. This enumerates them and asserts each one moves
    the records table, not just the ledger. A fifth path is a bug."""
    admin_h = _admin_h()
    store = _store()
    outcomes = {}

    # 1. admin Approve (§1.1)
    ev1 = _evaluator()
    _t1, s1 = _submit(admin_h, ev1)
    _hold_back(s1)
    e1 = _accrue(ev1, s1)
    client.post(f"/api/asclepius/admin/earnings/{e1['earning_id']}/approve",
                json={}, headers=admin_h)
    outcomes["admin_approve"] = _statuses(s1)

    # 2. reviewer accept — the records write inside reconcile pass 2
    ev2 = _evaluator()
    _t2, s2 = _submit(admin_h, ev2)
    _hold_back(s2)
    asc_payments.apply_ledger_decision_to_records(
        store, submission_id=s2, decision="approve", reason="reviewer_accepted")
    outcomes["reviewer_accept"] = _statuses(s2)

    # 3. the 14-day auto-approve
    ev3 = _evaluator()
    _t3, s3 = _submit(admin_h, ev3)
    _hold_back(s3)
    store.insert_earning(earning_id="e-auto", user_id=ev3["id"], kind="task",
                         ref_id=s3, amount_cents=7500, rate_cents=7500,
                         status="accrued", accrued_at="2020-01-01T00:00:00",
                         resolved_at=None)
    asc_payments._auto_approve(store, now=datetime.now(timezone.utc) + timedelta(days=1))
    outcomes["auto_approve"] = _statuses(s3)

    # 4. the QA tab
    ev4 = _evaluator()
    _t4, s4 = _submit(admin_h, ev4)
    _hold_back(s4, "needs_qa")
    r = client.post(f"/api/asclepius/qa/{s4}/decision", json={"decision": "approve"},
                    headers=admin_h)
    assert r.status_code == 200, r.text
    outcomes["qa_approve"] = _statuses(s4)

    assert set(outcomes) == {"admin_approve", "reviewer_accept", "auto_approve",
                             "qa_approve"}
    for name, st in outcomes.items():
        assert st["submission"] == "export_ready", name
        assert st["records"] == {"export_ready"}, name


def test_a_review_session_earning_has_no_case_to_ship():
    """Only a task earning is one case. A review session spans several and a
    referral bounty is not casework — neither may touch a records row."""
    assert asc_payments.submission_ref("task", "s-1") == "s-1"
    assert asc_payments.submission_ref("review_session", "sess-1") is None
    assert asc_payments.submission_ref("referral", "ref-1") is None
    res = asc_payments.apply_ledger_decision_to_records(
        _store(), submission_id=None, decision="approve", reason="x")
    assert res == {"moved": False, "submission_id": None, "prior_status": None,
                   "status": None, "outcome": "not_a_case"}


# ═══════════════════════════════════════════════════════════════════════════
#  export
# ═══════════════════════════════════════════════════════════════════════════
def _preview(admin_h, **params):
    qs = "&".join(f"{k}={v}" for k, v in params.items() if v)
    r = client.get("/api/asclepius/admin/export/case-preview"
                   + ("?" + qs if qs else ""), headers=admin_h)
    assert r.status_code == 200, r.text
    return r.json()


def test_every_scope_resolves_through_one_function():
    """§2.1 — five scopes, one resolver. The preview and the bundle call the
    same function, so they cannot disagree about what ships."""
    admin_h = _admin_h()
    ev = _evaluator()
    tid, sid = _submit(admin_h, ev)
    store = _store()
    hashed = store.get_user_by_id(ev["id"])["id_hashed"]

    for params in ({"scope": "case", "case_ids": tid},
                   {"scope": "specialty", "specialty": "nephrology"},
                   {"scope": "version", "version": "V3"},
                   {"scope": "physician", "annotator_id_hashed": hashed},
                   {"scope": "all"}):
        slice_ = admin_router._resolve_case_slice(
            store, scope=params.get("scope"),
            case_ids=[params["case_ids"]] if params.get("case_ids") else None,
            specialty=params.get("specialty"), version=params.get("version"),
            annotator_id_hashed=params.get("annotator_id_hashed"))
        p = _preview(admin_h, **params)
        # Same numbers from the endpoint and from the resolver directly.
        assert p["cases"] == len(slice_["task_ids"]), params
        assert p["labeler_submissions"] == len(slice_["submission_ids"]), params


def test_the_preview_says_what_is_being_excluded_and_why():
    """§2.2 — the sentence that is the whole fix.

    "1 case ships. 1 submission on <case> is awaiting approval and will not
    ship." Before this, the preview said "0 cases" and stopped, and the operator
    concluded the export had shipped the wrong case.
    """
    admin_h = _admin_h()
    ev = _evaluator()
    tid, sid = _submit(admin_h, ev)
    _hold_back(sid)
    _accrue(ev, sid)

    p = _preview(admin_h, scope="case", case_ids=tid)
    assert p["cases"] == 0                      # nothing ships…
    ex = p["excluded"]
    assert ex["unapproved_count"] == 1          # …and here is why
    assert ex["approvable_count"] == 1
    row = ex["unapproved"][0]
    assert row["case_id"] == tid
    assert row["submission_id"] == sid
    assert row["approvable"] is True
    assert "awaiting approval" in row["reason"].lower()


def test_the_preview_reports_the_exclusions_that_were_always_silent():
    """Mock-annotator records were filtered and never counted."""
    admin_h = _admin_h()
    store = _store()
    mock = A.make_user(store, role="evaluator", specialty="nephrology", is_mock=1)
    _tid, sid = _submit(admin_h, mock)
    p = _preview(admin_h, scope="all")
    assert p["excluded"]["mock"] >= 1
    assert p["cases"] == 0


def test_approve_all_from_the_preview_then_the_case_ships():
    admin_h = _admin_h()
    ev = _evaluator()
    tid, sid = _submit(admin_h, ev)
    _hold_back(sid)
    _accrue(ev, sid)

    r = client.post("/api/asclepius/admin/export/approve",
                    json={"submission_ids": [sid]}, headers=admin_h)
    assert r.status_code == 200, r.text
    assert r.json()["approved"] == 1

    after = _preview(admin_h, scope="case", case_ids=tid)
    assert after["cases"] == 1
    assert after["excluded"]["unapproved_count"] == 0
    assert after["exportable"] is True


def test_approve_all_works_for_a_contributor_with_no_ledger_row():
    """An advisor on the equity-only model accrues no payment. The work is still
    real and must still be able to ship — "approved money ⇔ exportable record"
    holds vacuously when there is no money either way."""
    admin_h = _admin_h()
    ev = _evaluator()
    _tid, sid = _submit(admin_h, ev)
    _hold_back(sid)
    store = _store()
    with store._conn() as conn:
        conn.execute("UPDATE users SET compensation_model = 'equity_only' WHERE id = ?",
                     (ev["id"],))
    assert store.get_earning(kind="task", ref_id=sid) is None
    r = client.post("/api/asclepius/admin/export/approve",
                    json={"submission_ids": [sid]}, headers=admin_h)
    assert r.status_code == 200, r.text
    assert _statuses(sid)["records"] == {"export_ready"}


def test_the_bundle_refuses_with_the_reason_rather_than_nothing_matches():
    admin_h = _admin_h()
    ev = _evaluator()
    tid, sid = _submit(admin_h, ev)
    _hold_back(sid)
    _accrue(ev, sid)
    r = client.post("/api/asclepius/admin/export/case-bundle",
                    json={"scope": "case", "case_ids": [tid]}, headers=admin_h)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "not approved" in detail and "will not ship" in detail


def test_the_v5_note_is_preserved():
    admin_h = _admin_h()
    p = _preview(admin_h, scope="version", version="V5")
    assert p["exportable"] is False
    assert "environments pipeline" in (p["note"] or "")


def test_physician_scope_ships_the_hash_and_never_the_name():
    """§2.1 — the bundle carries `annotator_id_hashed`. The physician's name
    never enters records.jsonl, the datasheet, batch.json, or the filename."""
    admin_h = _admin_h()
    store = _store()
    ev = _evaluator()
    with store._conn() as conn:
        conn.execute("UPDATE users SET full_name = 'Kalpesh Patel' WHERE id = ?",
                     (ev["id"],))
    _tid, sid = _submit(admin_h, ev)
    hashed = store.get_user_by_id(ev["id"])["id_hashed"]

    r = client.post("/api/asclepius/admin/export/case-bundle",
                    json={"scope": "physician", "annotator_id_hashed": hashed},
                    headers=admin_h)
    assert r.status_code == 200, r.text
    body = r.json()
    out_dir = Path(store.get_export(body["export_id"])["dir_path"])

    assert "Kalpesh" not in (body["filename"] or "")
    seen_hash = False
    for name in ("records.jsonl", "batch.json", "datasheet.md", "cases.jsonl",
                 "quality_report.md", "data_dictionary.md"):
        path = out_dir / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        assert "Kalpesh" not in text, name
        assert "Patel" not in text, name
        if hashed in text:
            seen_hash = True
    assert seen_hash, "the bundle must carry annotator_id_hashed"


def test_the_license_is_commercial_in_every_record_and_document():
    """§2.3 — `CC-BY-NC-4.0-clinical-eval` said NON-commercial on data sold to
    train commercial models. A buyer's counsel stops at that."""
    from asclepius.constants import DEFAULT_LICENSE

    assert DEFAULT_LICENSE == "archangel-commercial-v1"
    admin_h = _admin_h()
    ev = _evaluator()
    tid, _sid = _submit(admin_h, ev)
    r = client.post("/api/asclepius/admin/export/case-bundle",
                    json={"scope": "case", "case_ids": [tid]}, headers=admin_h)
    assert r.status_code == 200, r.text
    out_dir = Path(_store().get_export(r.json()["export_id"])["dir_path"])

    lines = (out_dir / "records.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert lines
    for line in lines:
        assert json.loads(line)["license"] == "archangel-commercial-v1"
    datasheet = (out_dir / "datasheet.md").read_text(encoding="utf-8")
    assert "archangel-commercial-v1" in datasheet
    assert "CC-BY-NC" not in datasheet


def test_an_older_record_is_re_stamped_at_ship_time_not_rewritten_in_place():
    """The whole back catalogue was stamped NC at capture. Re-stamping happens
    at emit, so the stored payload is never rewritten (§0: no destructive
    migration) and every shipped line still carries one correct license."""
    admin_h = _admin_h()
    ev = _evaluator()
    tid, sid = _submit(admin_h, ev)
    store = _store()
    with store._conn() as conn:
        rid = conn.execute("SELECT record_id FROM records WHERE submission_id = ?",
                           (sid,)).fetchone()[0]
    store.patch_record_payload(rid, {"license": "CC-BY-NC-4.0-clinical-eval"})

    r = client.post("/api/asclepius/admin/export/case-bundle",
                    json={"scope": "case", "case_ids": [tid]}, headers=admin_h)
    assert r.status_code == 200, r.text
    out_dir = Path(store.get_export(r.json()["export_id"])["dir_path"])
    for line in (out_dir / "records.jsonl").read_text(encoding="utf-8").strip().splitlines():
        assert json.loads(line)["license"] == "archangel-commercial-v1"
    # …and the stored record is untouched.
    with store._conn() as conn:
        stored = json.loads(conn.execute(
            "SELECT payload_json FROM records WHERE record_id = ?", (rid,)).fetchone()[0])
    assert stored["license"] == "CC-BY-NC-4.0-clinical-eval"


def test_the_manifest_renames_synthetic_prompts_and_states_case_provenance():
    """§2.3 — "synthetic" read as "made-up case" and never meant that."""
    admin_h = _admin_h()
    ev = _evaluator()
    tid, _sid = _submit(admin_h, ev)
    r = client.post("/api/asclepius/admin/export/case-bundle",
                    json={"scope": "case", "case_ids": [tid]}, headers=admin_h)
    out_dir = Path(_store().get_export(r.json()["export_id"])["dir_path"])
    batch = json.loads((out_dir / "batch.json").read_text(encoding="utf-8"))
    assert "model_generated_question_count" in batch
    assert isinstance(batch["case_provenance"], dict)
    # The old key rides along for one release so a buyer's ingest script that
    # reads it does not break on the rename.
    assert batch["synthetic_prompt_count"] == batch["model_generated_question_count"]


def test_the_datasheet_says_how_the_bundle_was_cut():
    admin_h = _admin_h()
    ev = _evaluator()
    tid, _sid = _submit(admin_h, ev)
    r = client.post("/api/asclepius/admin/export/case-bundle",
                    json={"scope": "case", "case_ids": [tid]}, headers=admin_h)
    out_dir = Path(_store().get_export(r.json()["export_id"])["dir_path"])
    datasheet = (out_dir / "datasheet.md").read_text(encoding="utf-8")
    assert "- Scope: **case**" in datasheet
    assert "1 case" in datasheet


def test_the_scope_is_persisted_on_the_export_row():
    """§2.4 — History could say how big an export was and never what it was."""
    admin_h = _admin_h()
    ev = _evaluator()
    tid, _sid = _submit(admin_h, ev)
    r = client.post("/api/asclepius/admin/export/case-bundle",
                    json={"scope": "case", "case_ids": [tid]}, headers=admin_h)
    row = _store().get_export(r.json()["export_id"])
    assert row["scope"]["type"] == "case"
    assert row["scope"]["case_ids"] == [tid]


def test_an_export_row_written_before_scopes_renders_as_legacy():
    """NULL stays None, so the UI can say `legacy` instead of inventing a scope
    for a bundle that really did ship."""
    store = _store()
    store.insert_export(export_id="exp-legacy", created_by=None, record_count=3,
                        filters={}, dir_path="/tmp/x", manifest={})
    assert store.get_export("exp-legacy")["scope"] is None
    assert [e for e in store.list_exports()
            if e["export_id"] == "exp-legacy"][0]["scope"] is None


def test_legacy_combinable_filters_still_intersect():
    """The pre-scope API took three COMBINABLE filters, and
    `?case_id=x&specialty=y` legitimately matched nothing. Turning that into
    "case only" would silently widen somebody's export."""
    admin_h = _admin_h()
    ev = _evaluator()
    tid, _sid = _submit(admin_h, ev)
    p = _preview(admin_h, case_id=tid, specialty="cardiology")
    assert p["cases"] == 0


def test_the_excluded_count_stays_exact_when_the_list_is_truncated(monkeypatch):
    """The COUNT is what the warning line quotes and what an operator acts on.
    A number capped by a display limit is a wrong number."""
    monkeypatch.setattr(admin_router, "_MAX_EXCLUDED_ROWS", 2)
    admin_h = _admin_h()
    ev = _evaluator()
    for _ in range(5):
        _tid, sid = _submit(admin_h, ev)
        _hold_back(sid)
        _accrue(ev, sid)

    p = _preview(admin_h, scope="all")
    ex = p["excluded"]
    assert ex["unapproved_count"] == 5      # exact
    assert ex["listed"] == 2                # capped
    assert ex["truncated"] is True
    assert len(ex["unapproved"]) == 2


def test_a_non_case_scope_never_binds_a_case_id_list(monkeypatch):
    """Materialising "all" as twenty thousand ids and binding them into an
    `IN (…)` blows SQLite's host-parameter ceiling on exactly the slice an
    operator is most likely to reach for."""
    store = _store()
    seen = {}
    real = store.submissions_not_shipping

    def _spy(*a, **kw):
        seen["task_ids"] = kw.get("task_ids", "not-passed")
        return real(*a, **kw)

    monkeypatch.setattr(store, "submissions_not_shipping", _spy)
    admin_h = _admin_h()
    _preview(admin_h, scope="all")
    assert seen["task_ids"] is None


def test_the_case_scope_bounds_a_pasted_id_list(monkeypatch):
    monkeypatch.setattr(admin_router, "_MAX_CASE_IDS", 3)
    store = _store()
    slice_ = admin_router._resolve_case_slice(
        store, scope="case", case_ids=[f"t-{i}" for i in range(50)])
    assert len(slice_["scope_json"]["case_ids"]) == 50   # what was asked for…
    # …but the exclusion query is bounded.
    assert slice_["excluded"]["unapproved_count"] == 0


def test_the_case_options_call_serves_every_picker():
    admin_h = _admin_h()
    ev = _evaluator()
    tid, _sid = _submit(admin_h, ev)
    r = client.get("/api/asclepius/admin/export/case-options", headers=admin_h)
    assert r.status_code == 200
    body = r.json()
    assert "nephrology" in body["specialties"]
    assert tid in [c["case_id"] for c in body["cases"]]
    people = body["physicians"]
    assert people and all(p["annotator_id_hashed"] for p in people)
    assert set(body["scopes"]) == set(admin_router.EXPORT_SCOPES)


def test_the_physician_picker_never_offers_a_mock_contributor():
    admin_h = _admin_h()
    store = _store()
    mock = A.make_user(store, role="evaluator", specialty="nephrology", is_mock=1)
    _submit(admin_h, mock)
    hashed = store.get_user_by_id(mock["id"])["id_hashed"]
    body = client.get("/api/asclepius/admin/export/case-options",
                      headers=admin_h).json()
    assert hashed not in [p["annotator_id_hashed"] for p in body["physicians"]]


# ═══════════════════════════════════════════════════════════════════════════
#  migration
# ═══════════════════════════════════════════════════════════════════════════
def test_the_backfill_makes_an_already_paid_case_exportable():
    """§4.2 — cases we have already paid for that cannot ship. That is the
    population the three-status split created."""
    admin_h = _admin_h()
    ev = _evaluator()
    _tid, sid = _submit(admin_h, ev)
    _hold_back(sid, "qa_checked")
    _accrue(ev, sid, status="paid")

    report = export_backfill.backfill_records_from_ledger(_store())
    assert report["candidates"] == 1
    assert report["moved"] == 1
    assert _statuses(sid)["records"] == {"export_ready"}
    assert _events(sid, export_backfill.EVENT_TYPE) == 1


def test_the_backfill_is_idempotent():
    admin_h = _admin_h()
    ev = _evaluator()
    _tid, sid = _submit(admin_h, ev)
    _hold_back(sid, "auto_validated")
    _accrue(ev, sid, status="approved")
    store = _store()
    export_backfill.backfill_records_from_ledger(store)
    second = export_backfill.backfill_records_from_ledger(store)
    assert second["candidates"] == 0 and second["moved"] == 0
    assert _events(sid, export_backfill.EVENT_TYPE) == 1


def test_the_backfill_leaves_needs_qa_alone():
    """A `needs_qa` submission is a human decision that is still PENDING. A
    migration does not get to make it."""
    admin_h = _admin_h()
    ev = _evaluator()
    _tid, sid = _submit(admin_h, ev)
    _hold_back(sid, "needs_qa")
    _accrue(ev, sid, status="approved")
    report = export_backfill.backfill_records_from_ledger(_store())
    assert report["candidates"] == 0
    assert _statuses(sid)["records"] == {"needs_qa"}


def test_the_backfill_never_retroactively_rejects_a_voided_case():
    """§4.3 — a void may have been a payment decision, not a quality one.
    Reported, never changed."""
    admin_h = _admin_h()
    ev = _evaluator()
    _tid, sid = _submit(admin_h, ev)
    _hold_back(sid, "qa_checked")
    _accrue(ev, sid, status="void")
    report = export_backfill.backfill_records_from_ledger(_store())
    assert report["candidates"] == 0
    assert report["voided_untouched"] == 1
    assert _statuses(sid)["records"] == {"qa_checked"}


def test_a_dry_run_writes_nothing():
    admin_h = _admin_h()
    ev = _evaluator()
    _tid, sid = _submit(admin_h, ev)
    _hold_back(sid, "qa_checked")
    _accrue(ev, sid, status="paid")
    report = export_backfill.backfill_records_from_ledger(_store(), dry_run=True)
    assert report["candidates"] == 1 and report["moved"] == 0
    assert _statuses(sid)["records"] == {"qa_checked"}


# ── The structural guarantee ──────────────────────────────────────────────
# The runtime check (id digests taken around the sweep) proves no row was lost
# on ONE run. These prove it cannot happen on ANY run, by asserting the
# migration path has no way to express it — and they keep proving it on every
# CI run, which is the only guarantee that costs an operator nothing at all.

_DESTRUCTIVE_SQL = re.compile(
    r"\b(DELETE\s+FROM|DROP\s+TABLE|DROP\s+COLUMN|TRUNCATE|REPLACE\s+INTO)\b",
    re.IGNORECASE)


def _sql_literals(fn) -> list:
    """Every string constant in a function's body except its docstring.

    Parsed, not grepped. This file and the modules it checks discuss deletion at
    length in prose — "no row is ever deleted" is the rule, not a violation of
    it — and a grep that cannot tell an explanation from a statement is a test
    that gets deleted the first time somebody documents the rule. An AST walk
    sees only the strings that could actually reach a cursor.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                             ast.Module)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and n.value not in docstrings]


#: Every function the boot migration can reach that touches the database. If the
#: sweep grows a new call, add it here — a write path absent from this list is a
#: write path nobody proved is non-destructive.
def _migration_write_path() -> dict:
    from asclepius.store import AsclepiusStore

    return {
        "export_backfill.backfill_records_from_ledger":
            export_backfill.backfill_records_from_ledger,
        "export_backfill.run_once_at_boot": export_backfill.run_once_at_boot,
        "export_inventory.inventory": export_inventory.inventory,
        "payments.apply_ledger_decision_to_records":
            asc_payments.apply_ledger_decision_to_records,
        "payments.approve_earning": asc_payments.approve_earning,
        "store.ledger_approved_but_unshippable":
            AsclepiusStore.ledger_approved_but_unshippable,
        "store.voided_with_live_records": AsclepiusStore.voided_with_live_records,
        "store.update_submission": AsclepiusStore.update_submission,
        "store.update_records_status_for_submission":
            AsclepiusStore.update_records_status_for_submission,
        "store.resolve_earning": AsclepiusStore.resolve_earning,
        "store.log_event": AsclepiusStore.log_event,
        "store.set_export_scope": AsclepiusStore.set_export_scope,
    }


def test_the_migration_cannot_delete_anything():
    """§0 — "No DELETE, no DROP, no ALTER ... DROP COLUMN."

    Asserted against the SQL of every function the boot migration can reach,
    rather than trusted. The sweep moves a `status` column and writes an event
    row; nothing in its reach can remove a submission, a record, an earning, a
    task or an export, so the id sets cannot change no matter what the data
    looks like.

    This is the half of the no-data-loss promise that needs nobody to check it:
    the runtime digest comparison proves one run was clean, and this proves
    every run must be.
    """
    for name, fn in _migration_write_path().items():
        for sql in _sql_literals(fn):
            hit = _DESTRUCTIVE_SQL.search(sql)
            assert hit is None, f"{name} can execute {hit.group(0)!r}: {sql!r}"


def test_the_migration_only_ever_moves_a_status():
    """The one UPDATE the sweep performs on `records` sets `status` and nothing
    else — so even a bug in it cannot corrupt a payload, an export id, or a
    record's link to its submission."""
    from asclepius.store import AsclepiusStore

    sql = " ".join(_sql_literals(
        AsclepiusStore.update_records_status_for_submission))
    assert "UPDATE records SET status = ?" in sql
    # No second assignment smuggled into the same statement.
    assert sql.count("SET") == 1


def test_the_approve_path_cannot_delete_anything_either():
    """The same proof for the button an operator presses all day. Approve and
    Void move statuses; neither is a way to remove work."""
    from routers import asclepius_payments as _pay

    for fn in (_pay.admin_approve_earning, _pay.admin_void_earning,
               admin_router.export_approve_unapproved):
        for sql in _sql_literals(fn):
            assert _DESTRUCTIVE_SQL.search(sql) is None, sql


def test_the_no_deletion_check_can_actually_fail():
    """A guarantee that cannot fail proves nothing. This is the canary."""
    def _hypothetical_bad_migration(conn):
        conn.execute("DELETE FROM records WHERE status = 'submitted'")

    assert any(_DESTRUCTIVE_SQL.search(sql)
               for sql in _sql_literals(_hypothetical_bad_migration))


def test_the_no_deletion_check_does_not_trip_on_prose():
    """…and one that fires on its own documentation gets deleted. The modules
    under test say "no row is ever deleted" out loud; that is the rule, not a
    breach of it."""
    def _well_documented(conn):
        """Never DELETE FROM records — see PRD §0. DROP TABLE is forbidden too."""
        # DELETE FROM submissions would be a bug.
        conn.execute("UPDATE records SET status = ?", ("export_ready",))

    assert not any(_DESTRUCTIVE_SQL.search(sql)
                   for sql in _sql_literals(_well_documented))


def test_the_boot_sweep_takes_the_contract_snapshot_itself():
    """§0's before-snapshot cannot be taken by hand.

    The inventory script ships WITH this change, so at the moment a
    before-snapshot is needed it is not deployed yet — and by the time it is,
    the boot sweep has already run. Both snapshots would be "after". So the
    sweep takes its own, in-process, immediately before its first write.
    """
    admin_h = _admin_h()
    ev = _evaluator()
    _tid, sid = _submit(admin_h, ev)
    _hold_back(sid, "qa_checked")
    _accrue(ev, sid, status="paid")

    report = export_backfill.run_once_at_boot(_store())
    assert report["candidates"] == 1 and report["moved"] == 1
    contract = report["contract"]
    assert contract is not None and contract["ok"] is True
    assert contract["problems"] == []
    # The before-snapshot really is from BEFORE the sweep: the ids are identical
    # (nothing was created or destroyed) and the sweep did move a status.
    assert contract["before"]["records"] == contract["after"]["records"]
    assert _statuses(sid)["records"] == {"export_ready"}


def test_the_boot_sweep_never_raises():
    """A migration that can take the portal down is a worse problem than the
    drift it fixes."""
    class _Broken:
        def ledger_approved_but_unshippable(self, **kw):
            raise RuntimeError("database on fire")

        def _conn(self):
            raise RuntimeError("database on fire")

    report = export_backfill.run_once_at_boot(_Broken())
    assert report["error"] is True
    assert report["moved"] == 0


def test_the_migration_report_endpoint_reads_the_boot_run():
    """An operator reads this in a browser instead of SSHing into a container."""
    admin_h = _admin_h()
    ev = _evaluator()
    _tid, sid = _submit(admin_h, ev)
    _hold_back(sid, "qa_checked")
    _accrue(ev, sid, status="paid")

    # Before the sweep has reported, the endpoint says so rather than 404ing or
    # implying nothing was stranded.
    prior = getattr(A.app.state, "asclepius_export_backfill", None)
    if hasattr(A.app.state, "asclepius_export_backfill"):
        del A.app.state.asclepius_export_backfill
    r = client.get("/api/asclepius/admin/export/migration-report", headers=admin_h)
    assert r.status_code == 200 and r.json()["ran"] is False

    A.app.state.asclepius_export_backfill = export_backfill.run_once_at_boot(_store())
    try:
        r = client.get("/api/asclepius/admin/export/migration-report", headers=admin_h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ran"] is True
        assert body["cases_stranded"] == 1
        assert body["cases_now_exportable"] == 1
        assert body["no_data_loss"]["checked"] is True
        assert body["no_data_loss"]["ok"] is True
        assert body["cases"][0]["submission_id"] == sid
    finally:
        if prior is None:
            del A.app.state.asclepius_export_backfill
        else:
            A.app.state.asclepius_export_backfill = prior


def test_the_migration_report_needs_an_admin():
    r = client.get("/api/asclepius/admin/export/migration-report")
    assert r.status_code in (401, 403)


def test_the_no_data_loss_contract_holds_across_the_migration():
    """§0 — counts may only go UP; the id sets must be IDENTICAL. A status may
    move, a row may not."""
    admin_h = _admin_h()
    store = _store()
    for _ in range(3):
        ev = _evaluator()
        _tid, sid = _submit(admin_h, ev)
        _hold_back(sid, "qa_checked")
        _accrue(ev, sid, status="paid")

    before = export_inventory.inventory(store)
    export_backfill.backfill_records_from_ledger(store)
    after = export_inventory.inventory(store)

    assert export_inventory.violations(before, after) == []
    # The ids are untouched…
    for table, _col in export_inventory.ID_SETS:
        assert after["id_sets"][table] == before["id_sets"][table], table
    # …and the statuses did move, which is the point.
    def by_status(inv):
        return {r["key"]: r["n"] for r in inv["counts"]["submissions_by_status"]}
    assert by_status(after).get("export_ready", 0) == 3
    assert by_status(before).get("export_ready", 0) == 0


def test_the_contract_catches_a_deleted_row():
    """The check has to be able to FAIL, or it proves nothing."""
    admin_h = _admin_h()
    ev = _evaluator()
    _tid, sid = _submit(admin_h, ev)
    store = _store()
    before = export_inventory.inventory(store)
    with store._conn() as conn:
        conn.execute("DELETE FROM records WHERE submission_id = ?", (sid,))
    problems = export_inventory.violations(before, export_inventory.inventory(store))
    assert problems
    assert any("records" in p for p in problems)


# ═══════════════════════════════════════════════════════════════════════════
#  storage durability — the failure that makes every other guarantee moot
# ═══════════════════════════════════════════════════════════════════════════
def test_the_durability_endpoint_covers_all_four_stores():
    """The three Asclepius stores AND the tenant database. The tenant db holds
    every onboarding in flight and was in none of the boot checks, which is how
    a green banner could sit above a signup funnel being erased each deploy."""
    r = client.get("/api/asclepius/admin/storage/durability", headers=_admin_h())
    assert r.status_code == 200, r.text
    body = r.json()
    names = {s["store"] for s in body["stores"]}
    assert names == {"Asclepius database", "raw ingest", "asset store",
                     "tenant database"}
    for s in body["stores"]:
        assert isinstance(s["durable"], bool)
        assert s["detail"]


def test_an_unarmed_gate_is_reported_even_when_storage_is_fine(monkeypatch):
    """Durable today is not a guarantee. Without ENV=production nothing stops a
    future variable change from putting the database back on ephemeral disk."""
    from routers import asclepius_admin as _admin

    monkeypatch.setenv("ENV", "")
    r = client.get("/api/asclepius/admin/storage/durability", headers=_admin_h())
    body = r.json()
    assert body["gate_armed"] is False
    # A remedy always accompanies a problem — an operator reading this at 2am
    # should not also have to go find the runbook.
    assert body["remedy"] and "ENV=production" in body["remedy"]


def test_an_armed_gate_over_durable_storage_reports_no_remedy(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    from asclepius import assets as asc_assets
    from asclepius import ingestion as asc_ingestion_mod
    from asclepius import store as asc_store_mod
    from routers import asclepius_admin as _admin

    monkeypatch.setattr(asc_store_mod, "_db_storage_durable", lambda: (True, "ok"))
    monkeypatch.setattr(asc_ingestion_mod, "ingest_storage_durable", lambda: (True, "ok"))
    monkeypatch.setattr(asc_assets, "asset_storage_durable", lambda: (True, "ok"))
    monkeypatch.setenv("TEAM_DB_PATH", "/data/team.db")
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", "/data")

    body = client.get("/api/asclepius/admin/storage/durability",
                      headers=_admin_h()).json()
    assert body["all_durable"] is True
    assert body["gate_armed"] is True
    assert body["remedy"] is None


def test_a_durability_check_that_raises_counts_as_a_failure(monkeypatch):
    """A check that cannot run has not passed. Reporting it as durable is the
    one answer that is worse than no answer."""
    from asclepius import assets as asc_assets

    def _boom():
        raise RuntimeError("volume gone")

    monkeypatch.setattr(asc_assets, "asset_storage_durable", _boom)
    body = client.get("/api/asclepius/admin/storage/durability",
                      headers=_admin_h()).json()
    assert body["all_durable"] is False
    bad = [s for s in body["stores"] if s["store"] == "asset store"][0]
    assert bad["durable"] is False and "volume gone" in bad["detail"]


def test_the_durability_endpoint_needs_an_admin():
    assert client.get("/api/asclepius/admin/storage/durability").status_code in (401, 403)


def test_the_console_shows_the_storage_banner_on_every_admin_tab():
    """A log line is read only by someone who already suspects a problem — the
    wrong medium for a failure whose whole signature is that nobody suspects
    anything."""
    src = (_FRONTEND / "asclepius.js").read_text(encoding="utf-8")
    view = src.split("function renderAdminView() {")[1].split("\n  }")[0]
    # Mounted in the shell, above the per-tab body, so it is not something a
    # section can forget to render.
    assert "ascStorageBanner" in view
    assert "refreshStorageBanner()" in view
    assert "/admin/storage/durability" in src
    # Silent when there is genuinely nothing to say.
    banner = src.split("async function refreshStorageBanner()")[1].split("\n  }")[0]
    assert "if (s.all_durable && s.gate_armed) return;" in banner


# ═══════════════════════════════════════════════════════════════════════════
#  buyers
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("path,method", [
    ("/api/asclepius/buyers", "get"),
    ("/api/asclepius/buyers", "post"),
    ("/api/asclepius/buyer-requests", "get"),
    ("/api/asclepius/buyer-requests", "post"),
    ("/api/asclepius/buyer-requests/r-1", "get"),
    ("/api/asclepius/buyer-requests/r-1/status", "post"),
    ("/api/asclepius/buyer-requests/r-1/batch", "post"),
])
def test_the_retired_crm_routes_are_gone(path, method):
    admin_h = _admin_h()
    call = getattr(client, method)
    r = call(path, json={}, headers=admin_h) if method == "post" \
        else call(path, headers=admin_h)
    assert r.status_code == 404, f"{method.upper()} {path} -> {r.status_code}"


def test_the_buyer_tables_are_intact_and_still_writable():
    """§5 — the CRM is retired; the TABLES stay. `tasks.buyer_request_id` still
    points into them, so records packaged for a past request keep their
    provenance."""
    store = _store()
    buyer = store.create_buyer(name="LabCo", contact="a@b.co",
                               export_profile="default", notes=None)
    req = store.create_buyer_request(
        buyer_id=buyer["buyer_id"], source="internal_prompt_bank",
        export_profile="default", constraints={}, uploaded=[], note=None,
        created_by=None)
    assert store.get_buyer_request(req["request_id"])["buyer_id"] == buyer["buyer_id"]
    assert [b["buyer_id"] for b in store.list_buyers()] == [buyer["buyer_id"]]


def test_export_and_send_creates_a_buyer_delivery(monkeypatch):
    """The one thing the CRM did that mattered, now attached to the export."""
    from routers import asclepius_buyer as buyer_router

    monkeypatch.setattr(buyer_router, "_email_configured", lambda: True)
    sent = {}

    async def _send(to, subject, html, **kw):
        sent["to"] = to
        return True

    monkeypatch.setattr(buyer_router, "send_html_email", _send)

    admin_h = _admin_h()
    ev = _evaluator()
    tid, _sid = _submit(admin_h, ev)
    r = client.post("/api/asclepius/admin/export/case-bundle",
                    json={"scope": "case", "case_ids": [tid],
                          "buyer_email": "buyer@lab.example"}, headers=admin_h)
    assert r.status_code == 200, r.text
    delivery = r.json()["delivery"]
    assert delivery["buyer_email"] == "buyer@lab.example"
    assert sent["to"] == "buyer@lab.example"
    store = _store()
    rows = store.list_buyer_deliveries()
    assert len(rows) == 1
    # The buyer receives the EXACT bundle that was previewed, not a second one
    # rebuilt from different filters.
    assert rows[0]["export_id"] == r.json()["export_id"]


def test_the_buyer_portal_is_untouched():
    """The delivery rail and the buyer's own door are explicitly kept (§5)."""
    # A real endpoint refusing an unauthenticated caller, not a 404. The buyer
    # portal (login, workspace, delivery download) is explicitly out of scope for
    # the CRM retirement.
    for path in ("/api/asclepius/buyer/me", "/api/asclepius/buyer/deliveries"):
        r = client.get(path)
        assert r.status_code in (401, 403), f"{path} -> {r.status_code}"
    assert client.get("/api/asclepius/admin/buyer-deliveries",
                      headers=_admin_h()).status_code == 200


# ═══════════════════════════════════════════════════════════════════════════
#  ui
# ═══════════════════════════════════════════════════════════════════════════
def _run_node(script: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed in this environment")
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True,
                          timeout=30)
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\n{proc.stderr}\n{proc.stdout}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


_HARNESS = """
require(%(shim)s);
function h(tag, attrs) {
  var el = document.createElement(tag);
  if (attrs) for (var k in attrs) {
    var v = attrs[k];
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v; else el.setAttribute(k, v);
  }
  for (var i = 2; i < arguments.length; i++) appendChild_(el, arguments[i]);
  return el;
}
function isText_(c) { return typeof c === 'string' || typeof c === 'number'; }
function appendChild_(el, c) {
  if (c == null || c === '' || c === false) return;
  if (Array.isArray(c)) { c.forEach(function (x) { appendChild_(el, x); }); return; }
  el.appendChild(isText_(c) ? document.createTextNode(String(c)) : c);
}
var CALLS = [];
var COPIED = [];
var RESPONSES = %(responses)s;
var ctx = {
  h: h,
  clear: function (el) { while (el.firstChild) el.removeChild(el.firstChild); },
  api: function (path, opts) {
    CALLS.push({ path: path, method: (opts && opts.method) || 'GET',
                 body: (opts && opts.body) || null });
    if (!(path in RESPONSES)) return Promise.reject({ message: 'no stub for ' + path });
    return Promise.resolve(RESPONSES[path]);
  },
  toast: function () {},
  loadingCard: function (t) { return h('div', {}, t); },
  downloadBlob: function () {},
  fmtDate: function (d) { return String(d); },
  // The real one lives in asclepius.js; the shim records what it was asked to
  // render so a test can assert the FULL id reached it.
  copyableId: function (id) {
    COPIED.push(id);
    return h('code', { class: 'asc-id-text', title: String(id) }, String(id));
  },
  copyTextToClipboard: function (t, cb) { COPIED.push(t); if (cb) cb(true); },
  exportHistory: function (card) { ctx.clear(card); },
  contributorBrowser: function () {},
};
eval(require('fs').readFileSync(%(module)s, 'utf8'));
function textOf(el) {
  if (el.nodeValue != null) return el.nodeValue;
  return (el.childNodes || []).map(textOf).join(' ');
}
function find(el, pred, acc) {
  acc = acc || [];
  if (pred(el)) acc.push(el);
  (el.childNodes || []).forEach(function (c) { if (c.tagName) find(c, pred, acc); });
  return acc;
}
function buttons(root) {
  return find(root, function (e) { return e.tagName === 'BUTTON'; })
    .map(function (b) { return textOf(b).trim(); });
}
function later(fn, n) { setTimeout(n > 1 ? function () { later(fn, n - 1); } : fn, 0); }
var body = document.createElement('div');
"""


def _ledger_row(status, **kw):
    row = {"earning_id": "e-" + status, "kind": "task", "ref_id": "s-1",
           "case_id": "v4real-v4-neph-001-a-very-long-case-id",
           "specialty": "nephrology", "seconds": 130, "quality": None,
           "quality_reasons": None, "amount_cents": 7500, "status": status}
    row.update(kw)
    return row


def _render_ledger(rows: list) -> dict:
    responses = {
        "/admin/earnings": {"rows": [], "by_user": {}, "totals": {}},
        "/admin/physicians": {"physicians": [
            {"id": "u1", "name": "Kalpesh Patel", "specialty": "nephrology",
             "tier_word": "Labeler", "verification_status": "approved"}]},
        "/admin/earnings?user_id=u1": {
            "rows": rows,
            "by_user": {"u1": {"outstanding_cents": 7500, "paid_cents": 0,
                               "n_rows": len(rows), "n_void": 0}}},
    }
    script = (_HARNESS % {
        "shim": json.dumps(str(_DOM_SHIM)),
        "module": json.dumps(str(_FRONTEND / "admin_earnings.js")),
        "responses": json.dumps(responses),
    }) + """
window.AdminEarningsSection.reset();
window.AdminEarningsSection.render(body, ctx, 'earnings');
later(function () {
  find(body, function (e) { return e.tagName === 'TR'; })
    .filter(function (r) { return textOf(r).indexOf('Kalpesh') !== -1; })[0]
    .dispatch('click');
  later(function () {
    console.log(JSON.stringify({ buttons: buttons(body), text: textOf(body),
                                 copied: COPIED, calls: CALLS }));
  }, 8);
}, 8);
"""
    return _run_node(script)


def test_an_accrued_row_offers_approve_and_void():
    out = _render_ledger([_ledger_row("accrued")])
    assert "Approve" in out["buttons"]
    assert "Void" in out["buttons"]


def test_an_approved_row_offers_only_void():
    out = _render_ledger([_ledger_row("approved")])
    assert "Approve" not in out["buttons"]
    assert "Void" in out["buttons"]


def test_a_paid_row_offers_neither():
    out = _render_ledger([_ledger_row("paid")])
    assert "Approve" not in out["buttons"]
    assert "Void" not in out["buttons"]


def test_pending_review_is_gone_from_the_status_column():
    """"Pending review" was never true — there was no review queue behind it and
    no reviewer coming. It sent operators looking for a person instead of a
    button."""
    out = _render_ledger([_ledger_row("accrued")])
    assert "Awaiting approval" in out["text"]
    assert "Pending review" not in out["text"]


def test_a_held_row_says_so_instead_of_offering_a_silent_pay_cut():
    out = _render_ledger([_ledger_row("accrued", quality_hold=1)])
    assert "reduced rate proposed" in out["text"]


def test_the_ledger_hands_the_full_case_id_to_the_copy_helper():
    """§1.3 — the id used to render as `slice(0, 10) + '…'`, so the one value an
    operator has to move to the export box was the one thing this screen would
    not give them. Width-independent by construction: nothing truncates."""
    long_id = "v4real-v4-neph-001-a-very-long-case-id"
    out = _render_ledger([_ledger_row("accrued", case_id=long_id)])
    assert long_id in out["copied"]
    assert long_id in out["text"]
    assert "…" not in out["text"]


def test_approving_a_row_posts_to_the_approve_endpoint():
    responses = {
        "/admin/earnings": {"rows": [], "by_user": {}, "totals": {}},
        "/admin/physicians": {"physicians": [
            {"id": "u1", "name": "Kalpesh Patel", "specialty": "nephrology",
             "tier_word": "Labeler", "verification_status": "approved"}]},
        "/admin/earnings?user_id=u1": {
            "rows": [_ledger_row("accrued")],
            "by_user": {"u1": {"outstanding_cents": 7500, "paid_cents": 0,
                               "n_rows": 1, "n_void": 0}}},
        "/admin/earnings/e-accrued/approve": {
            "ok": True, "approved": True, "exportable": True,
            "records_outcome": "moved", "submission_status": "export_ready",
            "totals": {"outstanding_cents": 7500}},
    }
    script = (_HARNESS % {
        "shim": json.dumps(str(_DOM_SHIM)),
        "module": json.dumps(str(_FRONTEND / "admin_earnings.js")),
        "responses": json.dumps(responses),
    }) + """
window.AdminEarningsSection.reset();
window.AdminEarningsSection.render(body, ctx, 'earnings');
later(function () {
  find(body, function (e) { return e.tagName === 'TR'; })
    .filter(function (r) { return textOf(r).indexOf('Kalpesh') !== -1; })[0]
    .dispatch('click');
  later(function () {
    find(body, function (e) {
      return e.tagName === 'BUTTON' && textOf(e).trim() === 'Approve'; })[0]
      .dispatch('click');
    later(function () {
      console.log(JSON.stringify({ calls: CALLS, text: textOf(body) }));
    }, 8);
  }, 8);
}, 8);
"""
    out = _run_node(script)
    approves = [c for c in out["calls"]
                if c["path"] == "/admin/earnings/e-accrued/approve"]
    assert len(approves) == 1
    assert approves[0]["method"] == "POST"
    # And the operator is told the case can now ship.
    assert "now exportable" in out["text"]


def _render_export(preview: dict, extra: dict = None) -> dict:
    responses = {
        "/admin/export/case-options": {
            "specialties": ["nephrology"], "versions": ["V3", "V4", "V5"],
            "scopes": ["case", "specialty", "version", "physician", "all"],
            "cases": [{"case_id": "v4real-v4-neph-001", "specialty": "nephrology",
                       "portal_version": "v4", "submissions": 2, "shippable": 0}],
            "physicians": [{"name": "Kalpesh Patel", "annotator_id_hashed": "hash-abc",
                            "specialty": "nephrology", "cases": 7, "submissions": 9}]},
        "/admin/buyer-deliveries": {"deliveries": [], "buyers": []},
    }
    responses.setdefault("/admin/export/migration-report", {"ran": False})
    responses.update(extra or {})
    responses["/admin/export/case-preview?scope=all"] = preview
    script = (_HARNESS % {
        "shim": json.dumps(str(_DOM_SHIM)),
        "module": json.dumps(str(_FRONTEND / "admin_export.js")),
        "responses": json.dumps(responses),
    }) + """
window.AdminExportSection.render(body, ctx);
later(function () {
  find(body, function (e) {
    return e.tagName === 'BUTTON' && textOf(e).trim() === 'All'; })[0]
    .dispatch('click');
  later(function () {
    console.log(JSON.stringify({ text: textOf(body), buttons: buttons(body),
                                 calls: CALLS, copied: COPIED }));
  }, 10);
}, 10);
"""
    return _run_node(script)


def test_the_export_preview_renders_the_excluded_warning():
    """The sentence that is the whole fix, rendered."""
    out = _render_export({
        "scope": "all", "cases": 1, "labeler_submissions": 1, "reviews": 0,
        "specialty_count": 1, "estimated_bytes": 4096, "exportable": True,
        "note": None,
        "excluded": {"unapproved_count": 4, "approvable_count": 4, "dropped": 0,
                     "mock": 0, "unapproved": []},
    })
    assert "4 submissions on these cases are not approved and will not ship" in out["text"]
    assert "Approve all 4" in out["buttons"]


def test_the_export_preview_reports_the_quiet_exclusions():
    out = _render_export({
        "scope": "all", "cases": 2, "labeler_submissions": 2, "reviews": 0,
        "specialty_count": 1, "estimated_bytes": 4096, "exportable": True,
        "note": None,
        "excluded": {"unapproved_count": 0, "approvable_count": 0, "dropped": 3,
                     "mock": 2, "unapproved": []},
    })
    assert "cannot be mapped to the buyer profile" in out["text"]
    assert "mock-annotator record" in out["text"]


def test_the_export_button_is_disabled_when_nothing_ships():
    out = _render_export({
        "scope": "all", "cases": 0, "labeler_submissions": 0, "reviews": 0,
        "specialty_count": 0, "estimated_bytes": 0, "exportable": False,
        "note": None,
        "excluded": {"unapproved_count": 1, "approvable_count": 1, "dropped": 0,
                     "mock": 0, "unapproved": [
                         {"submission_id": "s-1", "case_id": "v4real-v4-neph-001",
                          "earning_id": "e-1", "status": "submitted",
                          "ledger_status": "accrued", "approvable": True,
                          "reason": "Awaiting approval — approved money is what "
                                    "makes a record exportable."}]},
    })
    assert "0 cases" in out["text"]
    assert "Approve all 1" in out["buttons"]


def test_the_export_tab_shows_the_migration_verdict_without_being_asked():
    """"Did the migration lose anything?" should not require reading a deploy
    log. The answer renders on the screen the operator is already on."""
    out = _render_export({
        "scope": "all", "cases": 1, "labeler_submissions": 1, "reviews": 0,
        "specialty_count": 1, "estimated_bytes": 4096, "exportable": True,
        "note": None,
        "excluded": {"unapproved_count": 0, "approvable_count": 0, "dropped": 0,
                     "mock": 0, "unapproved": []},
    }, {"/admin/export/migration-report": {
        "ran": True, "cases_stranded": 12, "cases_now_exportable": 12,
        "voided_left_untouched": 3,
        "no_data_loss": {"checked": True, "ok": True, "problems": []}}})
    assert "12 cases" in out["text"]
    assert "could not ship, and now can" in out["text"]
    assert "3 voided earnings" in out["text"]
    assert "No rows were lost" in out["text"]


def test_a_failed_no_data_loss_check_is_impossible_to_miss():
    out = _render_export({
        "scope": "all", "cases": 1, "labeler_submissions": 1, "reviews": 0,
        "specialty_count": 1, "estimated_bytes": 4096, "exportable": True,
        "note": None,
        "excluded": {"unapproved_count": 0, "approvable_count": 0, "dropped": 0,
                     "mock": 0, "unapproved": []},
    }, {"/admin/export/migration-report": {
        "ran": True, "cases_stranded": 1, "cases_now_exportable": 1,
        "voided_left_untouched": 0,
        "no_data_loss": {"checked": True, "ok": False,
                         "problems": ["records: 40 rows before, 39 after"]}}})
    assert "NO-DATA-LOSS CHECK FAILED" in out["text"]
    assert "restore from a backup" in out["text"]
    assert "records: 40 rows before, 39 after" in out["text"]


def test_a_quiet_migration_says_nothing():
    """A permanent green "all clear" badge is a badge people stop seeing."""
    out = _render_export({
        "scope": "all", "cases": 1, "labeler_submissions": 1, "reviews": 0,
        "specialty_count": 1, "estimated_bytes": 4096, "exportable": True,
        "note": None,
        "excluded": {"unapproved_count": 0, "approvable_count": 0, "dropped": 0,
                     "mock": 0, "unapproved": []},
    }, {"/admin/export/migration-report": {
        "ran": True, "cases_stranded": 0, "cases_now_exportable": 0,
        "voided_left_untouched": 0,
        "no_data_loss": {"checked": True, "ok": True, "problems": []}}})
    assert "Export migration" not in out["text"]


def test_the_export_source_has_no_subnav_and_no_buyer_crm():
    src = (_FRONTEND / "asclepius.js").read_text(encoding="utf-8")
    section = src.split("function renderAdminExportSection(body) {")[1].split("\n  }")[0]
    assert "adminSubnav" not in section
    assert "renderAdminBuyers" not in section
    # The screen itself is gone, not merely unmounted.
    assert "function renderAdminBuyers(" not in src
    assert "function renderAdminExports(" not in src


def test_the_id_helper_is_defined_once_and_shared():
    """§1.3 — "one `copyableId(id)` helper, used everywhere an id renders"."""
    src = (_FRONTEND / "asclepius.js").read_text(encoding="utf-8")
    assert src.count("function copyableId(") == 1
    assert "copyableId, copyTextToClipboard," in src        # exposed on ctx
    for module in ("admin_earnings.js", "admin_export.js"):
        text = (_FRONTEND / module).read_text(encoding="utf-8")
        assert "ctx.copyableId" in text, module
