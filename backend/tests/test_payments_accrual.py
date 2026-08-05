"""PRD-P Phase 3 — TL task accrual, driven entirely through the real routes.

Every submission in this file is created by POSTing to
``/api/asclepius/submissions`` as a signed-in physician, and every review verdict
by drawing and POSTing through ``/api/asclepius/review/{id}``. Nothing constructs
state by calling the store: the last three audits of this codebase found the same
failure — a green suite over a broken feature, because the test built its state
behind the route it was supposed to be testing.

Accrual itself is DERIVED rather than hooked (see the module docstring of
``asclepius/payments.py``): the submit route lives in a file this surface does not
own, so ``reconcile_task_accruals`` reads ``submissions`` and ``case_reviews`` and
materialises the ledger. These tests therefore assert the observable contract —
"a physician who submitted sees $75 pending on their Earnings page" — which is the
claim that matters and is true under either implementation.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import payments as asc_payments  # noqa: E402
from asclepius import pipeline as asc_pipeline  # noqa: E402
from asclepius import profiles as asc_profiles  # noqa: E402

client = TestClient(A.app)

_ANCHOR = {"citation_text": "KDIGO 2024 hyperkalemia", "source_type": "guideline",
           "identifier": "KDIGO-2024-3.2"}
_IDEAL = {"text": "Stabilize the myocardium with IV calcium, shift potassium "
                  "intracellularly with insulin and dextrose plus a beta-agonist, then "
                  "remove it with dialysis given the ESRD."}
_DIMENSIONS = {k: "agree" for k in
               ("clinical_accuracy", "reasoning_quality", "completeness", "rubric_quality")}


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


def _admin():
    return A.make_user(_store(), role="admin")


def _labeler():
    return A.make_user(_store(), role="evaluator", specialty="nephrology",
                       board_cert="board_certified_nephrology", years_experience=12)


def _reviewer():
    store = _store()
    u = A.make_user(store, role="evaluator", specialty="nephrology",
                    board_cert="board_certified_nephrology", years_experience=15)
    with store._conn() as conn:
        conn.execute("UPDATE users SET tier = 'reviewer' WHERE id = ?", (u["id"],))
    return store.get_user_by_id(u["id"])


def _advisor():
    store = _store()
    u = A.make_user(store, role="evaluator", specialty="nephrology",
                    board_cert="board_certified_nephrology", years_experience=20)
    return store.appoint_advisor(u["id"], agreement_ref="AGR-2026-02",
                                 appointed_by="admin@asclepius.test")


def _create_task(admin_h):
    body = {
        "specialty": "nephrology", "difficulty": "hard", "capture_reasoning": False,
        "source": "lab_supplied", "max_labels": 1, "grounding_mode": "optional",
        "prompt": "72yo on HD, K+ 6.4 with peaked T-waves. Adjust dialysate and meds?",
        "candidate_answers": [
            {"id": "A", "text": "Give calcium gluconate, then dialyze with K+ 2.0.",
             "generator_model": "model_x"},
            {"id": "B", "text": "Set dialysate K+ to 1.0 immediately.", "generator_model": "model_y"},
        ],
    }
    r = client.post("/api/asclepius/tasks", json={"tasks": [body]}, headers=admin_h)
    assert r.status_code == 200, r.text
    return r.json()["created"][0]


def _submit(task_id, user, submission_id=None):
    """A real submission, through the real route the doctor's browser uses."""
    h = A.headers_for(user)
    client.get("/api/asclepius/tasks/next", headers=h)
    sid = submission_id or ("s-" + uuid.uuid4().hex[:12])
    r = client.post("/api/asclepius/submissions", json={
        "submission_id": sid, "task_id": task_id, "verdict": "A_better",
        "chosen_id": "A", "rejected_id": "B", "confidence": "high", "time_spent_sec": 140,
        "prompt_review": {"reviewed": True, "verdict": "valid"},
        "independent_answer": _IDEAL,
        "chosen_revision": {"edited": False, "why_better_notes": "B over-lowers K+"},
        "rejected_critique": {"error_tags": ["dosing_error"], "severities": {},
                              "why_worse": "too aggressive"},
    }, headers=h)
    assert r.status_code == 200, r.text
    return sid


def _review(submission_id, reviewer, verdict, notes=None):
    """A real review verdict, drawn and submitted through the real routes.

    The draw is what mints the claim the submit endpoint requires, and the queue
    serves the OLDEST reviewable submission rather than one of our choosing — so
    keep drawing (each draw claims a distinct row) until the one under test is
    claimed by this reviewer. That is what a reviewer working a queue actually
    does; it is not a way around the route."""
    h = A.headers_for(reviewer)
    for _ in range(10):
        drawn = client.get("/api/asclepius/review/next", headers=h).json().get("submission")
        if drawn is None:
            break
        if drawn["submission_id"] == submission_id:
            break
    body = {
        "verdict": verdict, "dimensions": _DIMENSIONS,
        "reviewer_notes": notes or "Reviewed.", "time_spent_sec": 300,
    }
    # The review route requires corrections on accept_with_edits — that is its
    # rule, not ours, and satisfying it is part of taking the real path.
    if verdict == "accept_with_edits":
        body["corrections"] = {"notes": "Tightened the dialysate target."}
    r = client.post(f"/api/asclepius/review/{submission_id}", json=body, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def _earnings(user):
    r = client.get("/api/asclepius/earnings", headers=A.headers_for(user))
    assert r.status_code == 200, r.text
    return r.json()


def _task_rows(payload):
    return [row for row in payload["recent"] if row["kind"] == "task"]


# ─── Accrual ──────────────────────────────────────────────────────────────────
def test_a_submission_accrues_seventy_five_dollars_pending_review():
    admin_h = A.headers_for(_admin())
    doctor = _labeler()
    sid = _submit(_create_task(admin_h), doctor)

    payload = _earnings(doctor)
    rows = _task_rows(payload)
    assert len(rows) == 1
    assert rows[0]["ref_id"] == sid
    assert rows[0]["amount_cents"] == 7500
    assert rows[0]["rate_cents"] == 7500
    assert rows[0]["status"] == "accrued"
    # The doctor is shown the state in WORDS, never a raw token.
    assert rows[0]["status_word"] == "Pending review"
    assert payload["pending_cents"] == 7500
    assert payload["approved_cents"] == 0


def test_three_tasks_read_two_twenty_five_pending_then_move_to_approved():
    """PRD-P §7.1, exactly as written."""
    admin_h = A.headers_for(_admin())
    doctor = _labeler()
    reviewer = _reviewer()
    sids = [_submit(_create_task(admin_h), doctor) for _ in range(3)]

    assert _earnings(doctor)["pending_cents"] == 22500
    assert _earnings(doctor)["approved_cents"] == 0

    _review(sids[0], reviewer, "accept")
    payload = _earnings(doctor)
    assert payload["approved_cents"] == 7500
    assert payload["pending_cents"] == 15000

    _review(sids[1], reviewer, "accept_with_edits")
    _review(sids[2], reviewer, "accept")
    payload = _earnings(doctor)
    assert payload["approved_cents"] == 22500
    assert payload["pending_cents"] == 0

    line = next(l for l in payload["lines"] if l["kind"] == "task")
    assert line["count"] == 3
    assert line["rate_cents"] == 7500
    assert line["cents"] == 22500


def test_a_reject_verdict_voids_the_earning_with_the_reason_attached():
    """A voided task is 'Not approved' with an explanation next to it — never a
    number that silently went down."""
    admin_h = A.headers_for(_admin())
    doctor = _labeler()
    sid = _submit(_create_task(admin_h), doctor)
    assert _earnings(doctor)["pending_cents"] == 7500

    _review(sid, _reviewer(), "reject", notes="Dialysate potassium is unsafe here.")

    payload = _earnings(doctor)
    row = _task_rows(payload)[0]
    assert row["status"] == "void"
    assert row["status_word"] == "Not approved"
    assert "Not approved on review" in row["note"]
    assert "unsafe" in row["note"]
    assert payload["void_cents"] == 7500
    assert payload["pending_cents"] == 0
    assert payload["approved_cents"] == 0


def test_a_later_accept_can_restore_money_but_a_later_reject_never_takes_it_back():
    """The resolution rule when a submission carries SEVERAL verdicts: money may
    go up on a later accept, and never goes down once approved (PRD-P §1.2).

    Stated honestly about its own reach: today's review route serves each
    submission to exactly one reviewer and then marks it ``reviewed``, so a second
    verdict cannot be produced over HTTP yet. This exercises the resolution rule
    directly against real ``case_reviews`` rows written through the store's own
    insert — the shape paired review (Agent R) will start producing. The
    single-verdict paths above are the ones driven end to end through the route.
    """
    admin_h = A.headers_for(_admin())
    doctor = _labeler()
    sid = _submit(_create_task(admin_h), doctor)
    store = _store()
    sub = store.get_submission(sid)

    def _verdict(verdict):
        r = _reviewer()
        store.insert_case_review(
            task_id=sub["task_id"], submission_id=sid, reviewer_user_id=r["id"],
            reviewer_id_hashed=r.get("id_hashed") or "", verdict=verdict,
            dimensions=_DIMENSIONS, corrections=None, reviewer_notes="Reviewed.",
            time_spent_sec=300, blinded=1,
        )
        asc_payments.reconcile_task_accruals(store)

    _verdict("reject")
    assert _earnings(doctor)["void_cents"] == 7500

    _verdict("accept")                            # a second reviewer disagrees
    payload = _earnings(doctor)
    assert payload["approved_cents"] == 7500
    assert payload["void_cents"] == 0

    _verdict("reject")                            # a third rejects
    payload = _earnings(doctor)
    assert payload["approved_cents"] == 7500      # money already approved stays
    assert payload["void_cents"] == 0


# ─── The double-payment guard ─────────────────────────────────────────────────
def test_resubmitting_the_same_submission_never_creates_a_second_row():
    """The route is idempotent, the reconciliation is idempotent, and
    UNIQUE(kind, ref_id) is the guarantee under both."""
    admin_h = A.headers_for(_admin())
    doctor = _labeler()
    task_id = _create_task(admin_h)
    sid = _submit(task_id, doctor)

    h = A.headers_for(doctor)
    for _ in range(3):
        r = client.post("/api/asclepius/submissions", json={
            "submission_id": sid, "task_id": task_id, "verdict": "A_better",
            "chosen_id": "A", "rejected_id": "B", "confidence": "high", "time_spent_sec": 140,
            "prompt_review": {"reviewed": True, "verdict": "valid"},
            "independent_answer": _IDEAL,
        }, headers=h)
        assert r.status_code == 200, r.text

    for _ in range(5):                            # and the sweep, repeatedly
        asc_payments.reconcile_task_accruals(_store())

    payload = _earnings(doctor)
    assert len(_task_rows(payload)) == 1
    assert payload["pending_cents"] == 7500


def test_the_ledger_date_is_when_the_work_happened_not_when_the_sweep_ran():
    """A backfill must not restart every auto-approve clock or date a physician's
    ledger by our deploy schedule."""
    admin_h = A.headers_for(_admin())
    doctor = _labeler()
    sid = _submit(_create_task(admin_h), doctor)
    submission = _store().get_submission(sid)

    row = _task_rows(_earnings(doctor))[0]
    # Same instant, to the second, as the submission itself.
    assert row["accrued_at"][:19] == submission["created_at"][:19]


def test_reconciliation_does_not_rescan_settled_work():
    """Reconciliation runs on every Earnings page load, so its working set must
    be bounded by OUTSTANDING work rather than by total history. Once a row is
    approved it is terminal and must drop out of both queries."""
    admin_h = A.headers_for(_admin())
    doctor = _labeler()
    reviewer = _reviewer()
    sids = [_submit(_create_task(admin_h), doctor) for _ in range(3)]
    for sid in sids:
        _review(sid, reviewer, "accept")
    asc_payments.reconcile_task_accruals(_store())

    store = _store()
    assert store.unaccrued_submissions() == []
    assert store.unresolved_task_earnings() == []
    # ...and the money is still there.
    assert _earnings(doctor)["approved_cents"] == 22500


def test_an_advisors_submissions_never_occupy_the_scan_window():
    """An advisor never gains a ledger row by design, so filtering payability in
    Python would leave their submissions in the scan set forever, eventually
    starving real work against the limit. They are excluded in SQL."""
    admin_h = A.headers_for(_admin())
    advisor = _advisor()
    for _ in range(3):
        _submit(_create_task(admin_h), advisor)
    asc_payments.reconcile_task_accruals(_store())

    assert _store().unaccrued_submissions() == []
    assert _earnings(advisor)["recent"] == []


def test_one_physicians_read_does_not_materialize_everyone_elses_ledger():
    """Audit M1. ``earnings_summary`` ran an unfiltered reconciliation, so one
    physician opening Earnings wrote ledger rows for the whole company and ran the
    14-day auto-approve sweep — in their request path. One user's READ mutating
    every other user's money is wrong on its own terms, and it makes the cost of a
    page load grow with the size of the business."""
    admin_h = A.headers_for(_admin())
    mine, theirs = _labeler(), _labeler()
    my_sid = _submit(_create_task(admin_h), mine)
    their_sid = _submit(_create_task(admin_h), theirs)

    payload = _earnings(mine)
    assert {r["ref_id"] for r in _task_rows(payload)} == {my_sid}

    # The other physician's work is untouched until THEY look, or until an admin
    # sweep runs. Their earning does not exist yet.
    assert _store().get_earning(kind="task", ref_id=their_sid) is None

    assert {r["ref_id"] for r in _task_rows(_earnings(theirs))} == {their_sid}
    assert _store().get_earning(kind="task", ref_id=their_sid) is not None


def test_a_scoped_reconciliation_only_sweeps_that_users_backlog():
    """The auto-approve sweep is scoped too. A physician opening Earnings must not
    be the thing that decides another physician's fourteen-day deadline."""
    admin_h = A.headers_for(_admin())
    mine, theirs = _labeler(), _labeler()
    _submit(_create_task(admin_h), mine)
    their_sid = _submit(_create_task(admin_h), theirs)
    asc_payments.reconcile_task_accruals(_store())        # unscoped: seed both

    later = datetime.now(timezone.utc) + timedelta(
        days=asc_payments.tl_auto_approve_days() + 1)
    asc_payments.reconcile_task_accruals(_store(), user_id=mine["id"], now=later)

    assert _store().get_earning(kind="task", ref_id=their_sid)["status"] == "accrued"
    assert [r["status"] for r in _task_rows(_earnings(mine))] == ["approved"]


def test_the_admin_sweep_is_still_company_wide():
    """The unscoped pass has to keep existing — it is what makes the auto-approve
    deadline a real promise rather than one that only fires for physicians who
    happen to check their earnings page."""
    admin_h = A.headers_for(_admin())
    a, b = _labeler(), _labeler()
    a_sid = _submit(_create_task(admin_h), a)
    b_sid = _submit(_create_task(admin_h), b)

    client.get("/api/asclepius/admin/earnings", headers=admin_h)

    assert _store().get_earning(kind="task", ref_id=a_sid) is not None
    assert _store().get_earning(kind="task", ref_id=b_sid) is not None


def test_reconciliation_is_stable_under_repetition():
    admin_h = A.headers_for(_admin())
    doctor = _labeler()
    _submit(_create_task(admin_h), doctor)

    first = asc_payments.reconcile_task_accruals(_store())
    assert first["accrued"] == 1
    for _ in range(4):
        again = asc_payments.reconcile_task_accruals(_store())
        assert again["accrued"] == 0
        assert again["approved"] == 0
        assert again["voided"] == 0


# ─── The advisor rule ─────────────────────────────────────────────────────────
def test_an_advisor_submission_accrues_nothing():
    """An equity-holding advisor labels a case. The work counts everywhere
    quality is measured; the money does not exist."""
    admin_h = A.headers_for(_admin())
    advisor = _advisor()
    sid = _submit(_create_task(admin_h), advisor)

    payload = _earnings(advisor)
    assert payload["recent"] == []
    assert payload["pending_cents"] == 0
    assert payload["approved_cents"] == 0
    assert payload["accrues_payment"] is False

    # ...and the submission itself is real and intact.
    assert _store().get_submission(sid) is not None


def test_an_advisor_accrues_nothing_even_after_an_accepting_review():
    admin_h = A.headers_for(_admin())
    advisor = _advisor()
    sid = _submit(_create_task(admin_h), advisor)
    _review(sid, _reviewer(), "accept")

    assert _earnings(advisor)["recent"] == []
    assert _store().get_earning(kind="task", ref_id=sid) is None


# ─── Auto-approval ────────────────────────────────────────────────────────────
def test_an_unreviewed_task_auto_approves_after_the_window():
    """A labeler is never held hostage by a review backlog."""
    admin_h = A.headers_for(_admin())
    doctor = _labeler()
    _submit(_create_task(admin_h), doctor)
    assert _earnings(doctor)["pending_cents"] == 7500

    later = datetime.now(timezone.utc) + timedelta(
        days=asc_payments.tl_auto_approve_days() + 1)
    asc_payments.reconcile_task_accruals(_store(), now=later)

    payload = _earnings(doctor)
    assert payload["approved_cents"] == 7500
    assert payload["pending_cents"] == 0
    row = _task_rows(payload)[0]
    assert row["status"] == "approved"
    assert "Auto-approved after" in (row["note"] or "")


def test_the_window_is_not_reached_a_day_early():
    admin_h = A.headers_for(_admin())
    doctor = _labeler()
    _submit(_create_task(admin_h), doctor)

    early = datetime.now(timezone.utc) + timedelta(
        days=asc_payments.tl_auto_approve_days() - 1)
    asc_payments.reconcile_task_accruals(_store(), now=early)
    assert _earnings(doctor)["pending_cents"] == 7500


def test_a_voided_task_is_never_swept_into_approved():
    """The auto-approve sweep moves ``accrued`` rows and only ``accrued`` rows. A
    task a physician reviewed and rejected must not become payable by waiting."""
    admin_h = A.headers_for(_admin())
    doctor = _labeler()
    sid = _submit(_create_task(admin_h), doctor)
    _review(sid, _reviewer(), "reject")

    later = datetime.now(timezone.utc) + timedelta(days=90)
    asc_payments.reconcile_task_accruals(_store(), now=later)
    assert _earnings(doctor)["void_cents"] == 7500
    assert _earnings(doctor)["approved_cents"] == 0


# ─── Rates ────────────────────────────────────────────────────────────────────
def test_the_rate_is_read_from_env_and_stamped_on_the_row(monkeypatch):
    monkeypatch.setenv("ASCLEPIUS_TL_RATE_CENTS", "9000")
    admin_h = A.headers_for(_admin())
    doctor = _labeler()
    _submit(_create_task(admin_h), doctor)

    row = _task_rows(_earnings(doctor))[0]
    assert row["amount_cents"] == 9000
    assert row["rate_cents"] == 9000

    # Lowering the rate afterwards must not restate what was already accrued.
    monkeypatch.setenv("ASCLEPIUS_TL_RATE_CENTS", "1000")
    asc_payments.reconcile_task_accruals(_store())
    row = _task_rows(_earnings(doctor))[0]
    assert row["amount_cents"] == 9000


def test_a_nonsense_rate_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("ASCLEPIUS_TL_RATE_CENTS", "seventy-five dollars")
    assert asc_payments.tl_rate_cents() == 7500
    monkeypatch.setenv("ASCLEPIUS_TL_RATE_CENTS", "-500")
    assert asc_payments.tl_rate_cents() == 7500


# ─── Scoping ──────────────────────────────────────────────────────────────────
def test_a_physician_sees_only_their_own_ledger():
    admin_h = A.headers_for(_admin())
    mine, theirs = _labeler(), _labeler()
    my_sid = _submit(_create_task(admin_h), mine)
    their_sid = _submit(_create_task(admin_h), theirs)

    my_refs = {r["ref_id"] for r in _task_rows(_earnings(mine))}
    assert my_refs == {my_sid}
    their_refs = {r["ref_id"] for r in _task_rows(_earnings(theirs))}
    assert their_refs == {their_sid}


def test_there_is_no_user_id_parameter_on_any_doctor_facing_route():
    """The scoping is a property of the ROUTES, not of a check somebody
    remembered. If a ``user_id`` ever appears on a doctor-facing payments path,
    this fails before it ships."""
    doctor_paths = [r.path for r in A.app.routes
                    if getattr(r, "path", "").startswith("/api/asclepius/")
                    and ("earnings" in r.path or "sessions" in r.path)
                    and "admin" not in r.path]
    assert doctor_paths, "the payments routes are not mounted"
    for path in doctor_paths:
        assert "user_id" not in path, path

    from routers import asclepius_payments as router_mod
    import inspect
    for name in ("my_earnings", "open_session", "session_heartbeat",
                 "close_session", "session_state"):
        params = inspect.signature(getattr(router_mod, name)).parameters
        assert "user_id" not in params, name

    # And the body models cannot smuggle one in either.
    assert "user_id" not in router_mod.OpenSessionBody.model_fields
    assert "user_id" not in router_mod.HeartbeatBody.model_fields
    assert "user_id" not in router_mod.CloseSessionBody.model_fields


# ─── Admin ────────────────────────────────────────────────────────────────────
def test_the_admin_ledger_is_admin_only_and_filterable():
    admin = _admin()
    admin_h = A.headers_for(admin)
    doctor = _labeler()
    _submit(_create_task(admin_h), doctor)

    assert client.get("/api/asclepius/admin/earnings",
                      headers=A.headers_for(doctor)).status_code == 403

    payload = client.get("/api/asclepius/admin/earnings", headers=admin_h).json()
    assert len(payload["rows"]) == 1
    assert payload["rows"][0]["user_email"] == doctor["email"]
    assert payload["totals"]["accrued"]["cents"] == 7500
    assert payload["rates"]["tl_rate_cents"] == 7500

    filtered = client.get("/api/asclepius/admin/earnings?status=approved",
                          headers=admin_h).json()
    assert filtered["rows"] == []
    assert client.get("/api/asclepius/admin/earnings?status=nonsense",
                      headers=admin_h).status_code == 422


# ─── Boundaries ───────────────────────────────────────────────────────────────
def test_payments_never_imports_review_or_routing():
    """PRD-P §8, asserted rather than remembered. P owns the billable session; R
    decides what a reviewer sees. If P ever needs to know what a review pair is,
    the boundary has slipped."""
    for path in (Path(__file__).resolve().parents[1] / "asclepius" / "payments.py",
                 Path(__file__).resolve().parents[1] / "routers" / "asclepius_payments.py"):
        source = path.read_text(encoding="utf-8")
        for banned in ("import review", "from asclepius.review", "asclepius import review",
                       "import routing", "from asclepius.routing", "asclepius import routing"):
            assert banned not in source, f"{path.name} imports {banned}"


def test_the_payable_verdict_set_is_pinned_and_is_not_the_acceptance_statistic():
    """PRD-P holds one verdict set: the verdicts under which a labeler is PAID.

    ``agreement.review_acceptance`` owns the expert-acceptance STATISTIC and is a
    different number that must never be reported as this one (context pack §3.2).
    The two coincide today only because "a reviewer signed off" and "we owe $75"
    are currently the same event. Pinning the set here means a future divergence
    has to be a deliberate edit rather than a silent drift, and asserting the
    separation means nobody can merge them by accident."""
    assert asc_payments.PAYABLE_VERDICTS == frozenset({"accept", "accept_with_edits"})
    assert asc_payments.REJECTING_VERDICTS == frozenset({"reject"})
    assert not (asc_payments.PAYABLE_VERDICTS & asc_payments.REJECTING_VERDICTS)

    # The classifier is a pure function of the raw verdict list, and the
    # asymmetry is the point: any accept wins, a reject only decides when no
    # accept exists.
    assert asc_payments._verdict_status(None) is None
    assert asc_payments._verdict_status("") is None
    assert asc_payments._verdict_status("reject") == "void"
    assert asc_payments._verdict_status("accept") == "approved"
    assert asc_payments._verdict_status("accept_with_edits") == "approved"
    assert asc_payments._verdict_status("reject,accept") == "approved"
    assert asc_payments._verdict_status("accept,reject") == "approved"
    assert asc_payments._verdict_status("reject,reject") == "void"


def test_payments_uses_the_shared_compensation_predicate():
    """``accrues_payment`` exists specifically so nobody writes their own
    equity-holder check. Asserting the import keeps a future refactor from
    quietly reinventing it."""
    source = (Path(__file__).resolve().parents[1] / "asclepius" / "payments.py").read_text(
        encoding="utf-8")
    assert "from asclepius import compensation" in source
    assert "compensation.accrues_payment" in source
    # And no hand-rolled copy of the predicate alongside it.
    assert 'compensation_model") != "equity_only"' not in source
    assert "compensation_model'] != 'equity_only'" not in source
