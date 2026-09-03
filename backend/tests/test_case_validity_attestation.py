"""Gap U2: the per-case clinical-validity attestation and what it costs.

The Sep 1 meeting's chain of reasoning: cases may be tweaked as long as they stay
clinically valid, and the control that makes that acceptable is the physician's
own attestation. If a doctor says a case is clinically valid before labeling it
and in reality it is not, that is on them, and the case is not paid.

Three properties, and none of them is the checkbox:

  1. The attestation is RECORDED, per case, at the moment of labeling, against
     the agreement version that physician actually signed.
  2. REJECTING is available and reaches an admin. That is the honest path, and
     the attestation is only fair to enforce because rejecting is free.
  3. The unpaid consequence is REAL, applied by the ledger and not by copy, and
     it can never restate a payment already settled.

Everything here runs through the real routes, on the reasoning
``test_payments_accrual`` states: this suite has been burned before by tests that
built state behind the route they were meant to exercise.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import payments as asc_payments  # noqa: E402
from asclepius import physician_agreement as PA  # noqa: E402
from asclepius import pipeline as asc_pipeline  # noqa: E402
from asclepius import profiles as asc_profiles  # noqa: E402

client = TestClient(A.app)

_IDEAL = {"text": "Stabilize the myocardium with IV calcium, shift potassium "
                  "intracellularly, then remove it with dialysis given the ESRD."}
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
    monkeypatch.setenv("ASCLEPIUS_DOUBLE_LABEL_HALT", "1")
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


def _sign(doc):
    """Sign the contributor agreement as this physician. A 'false' finding is
    only recordable against a signed version (no terms, no consequence), so
    every test that records one signs first, the way a real labeler has."""
    r = client.post("/api/asclepius/me/agreement/sign",
                    json={"typed_name": "Dr. Test Signer", "signed_initials": "TS",
                          "consent_esign": True},
                    headers=A.headers_for(doc))
    assert r.status_code == 200, r.text


def _create_task(admin_h):
    body = {
        "specialty": "nephrology", "difficulty": "hard", "capture_reasoning": False,
        "source": "lab_supplied", "max_labels": 1, "grounding_mode": "optional",
        "prompt": "72yo on HD, K+ 6.4 with peaked T-waves. Adjust dialysate and meds?",
        "candidate_answers": [
            {"id": "A", "text": "Give calcium gluconate, then dialyze with K+ 2.0.",
             "generator_model": "model_x"},
            {"id": "B", "text": "Set dialysate K+ to 1.0 immediately.",
             "generator_model": "model_y"},
        ],
    }
    r = client.post("/api/asclepius/tasks", json={"tasks": [body]}, headers=admin_h)
    assert r.status_code == 200, r.text
    return r.json()["created"][0]


def _submit(task_id, user, *, prompt_review=None):
    h = A.headers_for(user)
    client.get("/api/asclepius/tasks/next", headers=h)
    sid = "s-" + uuid.uuid4().hex[:12]
    payload = {
        "submission_id": sid, "task_id": task_id, "verdict": "A_better",
        "chosen_id": "A", "rejected_id": "B", "confidence": "high",
        "time_spent_sec": 140,
        "independent_answer": _IDEAL,
        "chosen_revision": {"edited": False, "why_better_notes": "B over-lowers K+"},
        "rejected_critique": {"error_tags": ["dosing_error"], "severities": {},
                              "why_worse": "too aggressive"},
    }
    if prompt_review is not None:
        payload["prompt_review"] = prompt_review
    r = client.post("/api/asclepius/submissions", json=payload, headers=h)
    assert r.status_code == 200, r.text
    return sid, r.json()


def _review(submission_id, reviewer, verdict):
    h = A.headers_for(reviewer)
    for _ in range(10):
        drawn = client.get("/api/asclepius/review/next", headers=h).json().get("submission")
        if drawn is None or drawn["submission_id"] == submission_id:
            break
    body = {"verdict": verdict, "dimensions": _DIMENSIONS,
            "reviewer_notes": "Reviewed.", "time_spent_sec": 300}
    if verdict == "accept_with_edits":
        body["corrections"] = {"notes": "Tightened the dialysate target."}
    r = client.post(f"/api/asclepius/review/{submission_id}", json=body, headers=h)
    assert r.status_code == 200, r.text


def _earnings(user):
    r = client.get("/api/asclepius/earnings", headers=A.headers_for(user))
    assert r.status_code == 200, r.text
    return r.json()


def _task_rows(payload):
    return [row for row in payload["recent"] if row["kind"] == "task"]


# ─── The attestation is recorded with the submission ─────────────────────────
def test_the_attestation_is_stored_on_the_submission_that_it_covers():
    """Per case, at the moment of labeling. Stored on the submission rather than
    in a side table because it is a property of that label and has to travel
    with it into any audit that asks who said this case was valid."""
    admin_h = A.headers_for(_admin())
    doc = _labeler()
    sid, _ = _submit(_create_task(admin_h), doc,
                     prompt_review={"reviewed": True, "verdict": "valid",
                                    "attest_clinically_valid": True})
    row = _store().get_submission(sid)
    assert row["validity_attested"] == 1
    assert row["validity_attested_at"]


def test_the_attestation_names_the_agreement_version_the_physician_signed():
    """An attestation means what the terms they READ said it meant. Without the
    version, a finding made under a later document's language could be applied
    to a physician who only ever signed the earlier one."""
    admin_h = A.headers_for(_admin())
    doc = _labeler()
    assert client.post("/api/asclepius/me/agreement/sign",
                       json={"typed_name": "Dr. Tej Patel", "signed_initials": "TP",
                             "consent_esign": True},
                       headers=A.headers_for(doc)).status_code == 200

    sid, _ = _submit(_create_task(admin_h), doc,
                     prompt_review={"reviewed": True, "verdict": "valid",
                                    "attest_clinically_valid": True})
    assert _store().get_submission(sid)["validity_agreement_version"] == PA.CURRENT_VERSION


def test_a_client_that_asserts_nothing_records_unknown_and_never_false():
    """None and False are different facts and only one of them can be found
    false later. A legacy client sending no assertion has not made a statement,
    and recording it as a refusal would invent a fact about a physician."""
    from asclepius.schemas import PromptReview, attested_validity

    assert attested_validity(None) is None
    assert attested_validity(PromptReview()) is None
    assert attested_validity(PromptReview(verdict="valid")) is True
    assert attested_validity(PromptReview(verdict="flagged")) is False
    # The explicit field wins over the verdict wherever the client sent one.
    assert attested_validity(
        PromptReview(verdict="valid", attest_clinically_valid=False)) is False


def test_a_case_with_no_attestation_cannot_be_found_falsely_attested():
    """There is nothing to find true or false, and an unpaid case whose reason
    nobody can explain to its author is worse than no finding at all."""
    admin_h = A.headers_for(_admin())
    doc = _labeler()
    sid, _ = _submit(_create_task(admin_h), doc)   # no prompt_review at all
    assert _store().get_submission(sid)["validity_attested"] is None

    r = client.post(f"/api/asclepius/admin/submissions/{sid}/validity-finding",
                    json={"finding": "false", "note": "The potassium is impossible."},
                    headers=admin_h)
    assert r.status_code == 409
    assert _store().get_submission(sid)["validity_finding"] is None


# ─── Rejecting is available, free, and reaches an admin ──────────────────────
def test_rejecting_a_case_produces_no_labels_and_reaches_the_admin_flagged_list():
    """The honest path the meeting described. It has to be at least as easy as
    attesting, and it has to actually go somewhere: a rejection that vanished
    would teach a physician that rejecting is pointless."""
    admin_h = A.headers_for(_admin())
    doc = _labeler()
    task_id = _create_task(admin_h)
    h = A.headers_for(doc)
    client.get("/api/asclepius/tasks/next", headers=h)
    r = client.post("/api/asclepius/submissions", json={
        "submission_id": "s-" + uuid.uuid4().hex[:12], "task_id": task_id,
        "confidence": "medium", "time_spent_sec": 30,
        "prompt_review": {"reviewed": True, "verdict": "flagged",
                          "attest_clinically_valid": False,
                          "note": "A K+ of 6.4 with this ECG is inconsistent."},
    }, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["record_count"] == 0

    # The admin's existing flagged view is where it lands.
    flagged = client.get("/api/asclepius/tasks?status=prompt_flagged", headers=admin_h)
    assert flagged.status_code == 200
    assert task_id in {t["task_id"] for t in flagged.json()["tasks"]}


def test_a_rejected_case_leaves_the_queue_instead_of_being_served_again():
    """Rejecting has to cost the physician nothing, and being handed the same
    broken case back is a cost."""
    admin_h = A.headers_for(_admin())
    doc = _labeler()
    task_id = _create_task(admin_h)
    h = A.headers_for(doc)
    client.get("/api/asclepius/tasks/next", headers=h)
    client.post("/api/asclepius/submissions", json={
        "submission_id": "s-" + uuid.uuid4().hex[:12], "task_id": task_id,
        "confidence": "medium", "time_spent_sec": 30,
        "prompt_review": {"reviewed": True, "verdict": "flagged"},
    }, headers=h)
    nxt = client.get("/api/asclepius/tasks/next", headers=A.headers_for(_labeler()))
    assert (nxt.json().get("task") or {}).get("task_id") != task_id


def test_rejecting_never_accrues_and_never_costs_the_physician_money():
    """Section 3.4: rejecting does not count against standing or pay. A flagged
    submission produces no records, so it must produce no ledger row either, in
    any state -- including a zero."""
    admin_h = A.headers_for(_admin())
    doc = _labeler()
    task_id = _create_task(admin_h)
    h = A.headers_for(doc)
    client.get("/api/asclepius/tasks/next", headers=h)
    client.post("/api/asclepius/submissions", json={
        "submission_id": "s-" + uuid.uuid4().hex[:12], "task_id": task_id,
        "confidence": "medium", "time_spent_sec": 30,
        "prompt_review": {"reviewed": True, "verdict": "flagged"},
    }, headers=h)
    payload = _earnings(doc)
    assert _task_rows(payload) == []
    assert payload["pending_cents"] == 0
    assert payload["approved_cents"] == 0


# ─── The unpaid consequence ──────────────────────────────────────────────────
def test_a_falsely_attested_case_is_voided_rather_than_paid():
    """The consequence the meeting named, made real in the ledger rather than in
    copy. The row is VOID with the reason attached, never silently absent: a
    physician must never see a number move without an explanation."""
    admin_h = A.headers_for(_admin())
    doc = _labeler()
    _sign(doc)
    sid, _ = _submit(_create_task(admin_h), doc,
                     prompt_review={"reviewed": True, "verdict": "valid",
                                    "attest_clinically_valid": True})
    assert _task_rows(_earnings(doc))[0]["status"] == "accrued"

    r = client.post(f"/api/asclepius/admin/submissions/{sid}/validity-finding",
                    json={"finding": "false",
                          "note": "The ECG described cannot arise from this potassium."},
                    headers=admin_h)
    assert r.status_code == 200, r.text

    rows = _task_rows(_earnings(doc))
    assert len(rows) == 1
    assert rows[0]["status"] == "void"
    assert "3.5" in (rows[0].get("note") or "")
    assert _earnings(doc)["pending_cents"] == 0
    assert _earnings(doc)["approved_cents"] == 0


def test_a_finding_recorded_before_the_ledger_row_exists_still_bites():
    """The sweep is derived and runs on read, so a finding can land before the
    submission has ever been swept. Voiding only in the resolving pass would let
    a case slip through whenever an admin was faster than an Earnings page."""
    admin_h = A.headers_for(_admin())
    doc = _labeler()
    _sign(doc)
    sid, _ = _submit(_create_task(admin_h), doc,
                     prompt_review={"reviewed": True, "verdict": "valid",
                                    "attest_clinically_valid": True})
    # Nobody has looked at Earnings yet, so no ledger row exists.
    assert client.post(f"/api/asclepius/admin/submissions/{sid}/validity-finding",
                       json={"finding": "false", "note": "Not a possible presentation."},
                       headers=admin_h).status_code == 200

    rows = _task_rows(_earnings(doc))
    assert len(rows) == 1 and rows[0]["status"] == "void"


def test_an_accepting_verdict_never_restores_a_falsely_attested_case():
    """A later accept restores money in the ordinary case, deliberately. It must
    not here: a reviewer saying the LABEL was good does not overrule a finding
    that the case should never have been labelled at all."""
    admin_h = A.headers_for(_admin())
    doc = _labeler()
    _sign(doc)
    sid, _ = _submit(_create_task(admin_h), doc,
                     prompt_review={"reviewed": True, "verdict": "valid",
                                    "attest_clinically_valid": True})
    assert client.post(f"/api/asclepius/admin/submissions/{sid}/validity-finding",
                       json={"finding": "false", "note": "Inconsistent case."},
                       headers=admin_h).status_code == 200
    assert _task_rows(_earnings(doc))[0]["status"] == "void"

    _review(sid, _reviewer(), "accept")
    rows = _task_rows(_earnings(doc))
    assert rows[0]["status"] == "void"
    assert _earnings(doc)["approved_cents"] == 0


def test_a_payment_already_settled_is_never_restated():
    """Section 3.5 says so, and it says so because clawing settled pay back from
    a doctor is a thing this company is choosing not to do. `only_from=[ACCRUED]`
    in the sweep is the guarantee, and this is the test that holds it."""
    admin_h = A.headers_for(_admin())
    doc = _labeler()
    _sign(doc)
    sid, _ = _submit(_create_task(admin_h), doc,
                     prompt_review={"reviewed": True, "verdict": "valid",
                                    "attest_clinically_valid": True})
    _review(sid, _reviewer(), "accept")
    before = _earnings(doc)
    assert _task_rows(before)[0]["status"] == "approved"
    assert before["approved_cents"] == 7500

    assert client.post(f"/api/asclepius/admin/submissions/{sid}/validity-finding",
                       json={"finding": "false", "note": "Found later to be invalid."},
                       headers=admin_h).status_code == 200

    after = _earnings(doc)
    assert _task_rows(after)[0]["status"] == "approved"
    assert after["approved_cents"] == 7500


def test_an_upheld_attestation_and_a_case_nobody_reviewed_both_pay_normally():
    """Silence is not an accusation. NULL means nobody looked and 'upheld' means
    somebody looked and it was fine, and neither may cost a physician a case."""
    admin_h = A.headers_for(_admin())
    doc = _labeler()
    unlooked, _ = _submit(_create_task(admin_h), doc,
                          prompt_review={"reviewed": True, "verdict": "valid"})
    checked, _ = _submit(_create_task(admin_h), doc,
                         prompt_review={"reviewed": True, "verdict": "valid"})
    assert client.post(f"/api/asclepius/admin/submissions/{checked}/validity-finding",
                       json={"finding": "upheld"}, headers=admin_h).status_code == 200

    rows = {r["ref_id"]: r for r in _task_rows(_earnings(doc))}
    assert rows[unlooked]["status"] == "accrued"
    assert rows[checked]["status"] == "accrued"
    assert _earnings(doc)["pending_cents"] == 15000


def test_a_finding_of_false_is_refused_without_a_reason():
    """Section 4.3 promises the physician is told WHICH case and WHY. A finding
    with no reason cannot keep that promise, so the API will not record one
    rather than leaving a doctor with an unexplained zero."""
    admin_h = A.headers_for(_admin())
    doc = _labeler()
    sid, _ = _submit(_create_task(admin_h), doc,
                     prompt_review={"reviewed": True, "verdict": "valid"})
    r = client.post(f"/api/asclepius/admin/submissions/{sid}/validity-finding",
                    json={"finding": "false"}, headers=admin_h)
    assert r.status_code == 400
    assert _store().get_submission(sid)["validity_finding"] is None


def test_only_an_admin_records_a_finding_and_never_a_sweep():
    """The whole reason the attestation moves responsibility is that a named
    person looked. An automated finding would be an automated pay cut, which
    this codebase already refuses to make anywhere else."""
    admin_h = A.headers_for(_admin())
    doc = _labeler()
    sid, _ = _submit(_create_task(admin_h), doc,
                     prompt_review={"reviewed": True, "verdict": "valid"})
    r = client.post(f"/api/asclepius/admin/submissions/{sid}/validity-finding",
                    json={"finding": "false", "note": "no"}, headers=A.headers_for(doc))
    assert r.status_code in (401, 403)

    # And the sweep alone never writes one, however many times it runs.
    asc_payments.reconcile_task_accruals(_store())
    asc_payments.reconcile_task_accruals(_store())
    assert _store().get_submission(sid)["validity_finding"] is None


def test_the_finding_records_who_decided_and_why():
    """Reconstructible later, by somebody who was not in the room."""
    admin = _admin()
    admin_h = A.headers_for(admin)
    doc = _labeler()
    _sign(doc)
    sid, _ = _submit(_create_task(admin_h), doc,
                     prompt_review={"reviewed": True, "verdict": "valid"})
    r = client.post(f"/api/asclepius/admin/submissions/{sid}/validity-finding",
                    json={"finding": "false", "note": "Creatinine and GFR disagree."},
                    headers=admin_h)
    body = r.json()
    assert body["validity_finding"] == "false"
    assert body["validity_finding_by"]
    assert body["validity_finding_at"]
    assert "Creatinine" in body["validity_finding_note"]


def test_the_sweep_is_idempotent_over_a_falsely_attested_case():
    """It runs on every Earnings page load. A second pass must not write a
    second row or move a row that is already decided."""
    admin_h = A.headers_for(_admin())
    doc = _labeler()
    _sign(doc)
    sid, _ = _submit(_create_task(admin_h), doc,
                     prompt_review={"reviewed": True, "verdict": "valid"})
    client.post(f"/api/asclepius/admin/submissions/{sid}/validity-finding",
                json={"finding": "false", "note": "Invalid."}, headers=admin_h)
    for _ in range(3):
        asc_payments.reconcile_task_accruals(_store())
    rows = _task_rows(_earnings(doc))
    assert len(rows) == 1 and rows[0]["status"] == "void"


# ─── No signed agreement, no agreement-backed consequence ────────────────────
def test_a_false_finding_is_refused_when_the_physician_never_signed():
    """The void cites the contributor agreement. A physician whose
    validity_agreement_version is NULL never signed one, so there are no terms
    to hold the attestation against, and recording the finding anyway would be
    a pay cut citing a document its subject never saw."""
    admin_h = A.headers_for(_admin())
    doc = _labeler()   # deliberately unsigned
    sid, _ = _submit(_create_task(admin_h), doc,
                     prompt_review={"reviewed": True, "verdict": "valid",
                                    "attest_clinically_valid": True})
    assert _store().get_submission(sid)["validity_agreement_version"] is None

    r = client.post(f"/api/asclepius/admin/submissions/{sid}/validity-finding",
                    json={"finding": "false", "note": "The case is impossible."},
                    headers=admin_h)
    assert r.status_code == 409, r.text
    assert _store().get_submission(sid)["validity_finding"] is None
    # And the pay is untouched: the case still accrues normally.
    rows = _task_rows(_earnings(doc))
    assert len(rows) == 1 and rows[0]["status"] == "accrued"


def test_an_explicit_override_still_lets_an_admin_record_the_finding():
    """The refusal is a guard, not a dead end. An admin who has decided the
    in-product attestation copy alone is enough says so explicitly, and the
    finding then lands with its ordinary consequence."""
    admin_h = A.headers_for(_admin())
    doc = _labeler()   # unsigned
    sid, _ = _submit(_create_task(admin_h), doc,
                     prompt_review={"reviewed": True, "verdict": "valid",
                                    "attest_clinically_valid": True})
    r = client.post(f"/api/asclepius/admin/submissions/{sid}/validity-finding",
                    json={"finding": "false", "note": "Impossible presentation.",
                          "override_unsigned": True},
                    headers=admin_h)
    assert r.status_code == 200, r.text
    assert _store().get_submission(sid)["validity_finding"] == "false"
    rows = _task_rows(_earnings(doc))
    assert len(rows) == 1 and rows[0]["status"] == "void"


def test_an_upheld_finding_never_needs_a_signature():
    """'Upheld' carries no consequence: it records that somebody looked and it
    was fine. Refusing it on an unsigned physician would leave their cases
    permanently un-reviewable for no one's protection."""
    admin_h = A.headers_for(_admin())
    doc = _labeler()   # unsigned
    sid, _ = _submit(_create_task(admin_h), doc,
                     prompt_review={"reviewed": True, "verdict": "valid",
                                    "attest_clinically_valid": True})
    r = client.post(f"/api/asclepius/admin/submissions/{sid}/validity-finding",
                    json={"finding": "upheld"}, headers=admin_h)
    assert r.status_code == 200, r.text
    assert _store().get_submission(sid)["validity_finding"] == "upheld"
