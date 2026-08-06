"""Advisor PRD §7 — the definition of done, as one walk through the product.

The PRD states its own acceptance criterion in a sentence, so it is encoded here
as a single test rather than left implicit across four files:

  an admin appoints Toby by email with an agreement reference; he signs in and
  can draw a case, review someone else's submission blind, invite physicians and
  watch them move through the funnel, open a pending export bundle and leave
  changes_requested with comments that appear in admin — and a record he labeled
  ships carrying his real credential AND ``related_party: true``.

Every step goes through the real HTTP surface. A unit test proving each piece in
isolation would still have let the pieces fail to meet.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import packaging  # noqa: E402
from asclepius import store as asc_store  # noqa: E402

client = TestClient(A.app)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    A.fresh_store()
    from asclepius import pipeline as asc_pipeline

    async def _ok_critic(task, submission):
        return {"consistent": True, "issues": [], "skipped": True}

    async def _ok_grounding(task, submission):
        return {"grounding_ok": True, "issues": [], "skipped": True, "checked_anchors": 0}

    monkeypatch.setattr(asc_pipeline, "run_critic", _ok_critic)
    monkeypatch.setattr(asc_pipeline, "run_grounding_check", _ok_grounding)
    yield


def test_the_whole_advisor_journey(monkeypatch):
    # PRD R / Audit R H2: with double-labeling as the default, a singly-labelled
    # case is awaiting its pair and the SINGLE-submission review flow correctly
    # refuses it. This test exercises that single flow, so it pins the incident
    # switch — the one supported way to run without second labels.
    monkeypatch.setenv("ASCLEPIUS_DOUBLE_LABEL_HALT", "1")
    store = asc_store.get_store()
    admin = A.make_user(store, role="admin")
    admin_h = A.headers_for(admin)

    # ── 1. An admin appoints Toby by email, with an agreement reference ──────
    toby_email = f"toby-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/api/asclepius/admin/advisors",
                    json={"email": toby_email, "name": "Toby Barrack",
                          "specialty": "nephrology",
                          "agreement_ref": "ADV-AGR-2026-001"},
                    headers=admin_h)
    assert r.status_code == 200, r.text
    toby = store.get_user_by_email(toby_email)
    with store._conn() as conn:                       # give him a credential to ship
        conn.execute("UPDATE users SET board_cert = 'ABIM — Nephrology' WHERE id = ?",
                     (toby["id"],))
    toby = store.get_user_by_id(toby["id"])
    toby_h = A.headers_for(toby)

    # ── 2. He signs in and the session tells the portal what he can do ───────
    me = client.get("/api/asclepius/auth/me", headers=toby_h).json()
    assert me["tier_word"] == "Medical Advisor"
    assert {"label", "review", "refer"} <= set(me["capabilities"])

    # ── 3. He can draw a case ───────────────────────────────────────────────
    store.insert_task(prompt="72yo on HD, K+ 6.4 with peaked T-waves.",
                      specialty="nephrology",
                      candidate_answers=[{"id": "A", "text": "Calcium, then dialyze."},
                                         {"id": "B", "text": "Insulin-dextrose only."}])
    r = client.get("/api/asclepius/tasks/next", headers=toby_h)
    assert r.status_code == 200, r.text
    assert (r.json().get("task") or {}).get("task_id")

    # ── 4. He reviews someone else's submission, BLIND ──────────────────────
    labeler = A.make_user(store, role="evaluator", specialty="nephrology",
                          email=f"dr-mcallister-{uuid.uuid4().hex[:6]}@example.com")
    task = store.insert_task(
        prompt="A 62-year-old with rising creatinine…", specialty="nephrology",
        candidate_answers=[{"id": "A", "text": "Option A"}, {"id": "B", "text": "Option B"}])
    store.insert_submission(
        submission_id=f"sub-{uuid.uuid4().hex[:10]}", task_id=task["task_id"],
        evaluator_id=labeler["id"], verdict="A_better", chosen_id="A", rejected_id="B",
        confidence="high", time_spent_sec=90,
        payload={"verdict": "A_better", "chosen_id": "A", "rejected_id": "B",
                 "rationale": "A stabilizes the membrane first."},
        annotator=store.annotator_block(labeler), dedupe_hash=None)

    r = client.get("/api/asclepius/review/next", headers=toby_h)
    assert r.status_code == 200, r.text
    drawn = r.json().get("submission")
    assert drawn, "the advisor drew nothing from the review queue"
    # More senior is not exempt from PRD A's blinding rule.
    raw = json.dumps(drawn).lower()
    assert labeler["email"].lower() not in raw
    assert labeler["id"] not in raw
    assert drawn["blinded"] is True

    r = client.post(f"/api/asclepius/review/{drawn['submission_id']}",
                    json={"verdict": "accept",
                          "dimensions": {"clinical_accuracy": "agree",
                                         "reasoning_quality": "agree",
                                         "completeness": "agree",
                                         "rubric_quality": "cannot_assess"}},
                    headers=toby_h)
    assert r.status_code == 200, r.text

    # ── 5. He invites three physicians and watches the funnel move ──────────
    invitees = [f"ref{i}-{uuid.uuid4().hex[:6]}@example.com" for i in range(3)]
    for i, email in enumerate(invitees):
        r = client.post("/api/asclepius/advisor/referrals",
                        json={"email": email, "name": f"Dr Referral {i}",
                              "note": "Stanford ortho"}, headers=toby_h)
        assert r.status_code == 200, r.text

    funnel = client.get("/api/asclepius/advisor/referrals", headers=toby_h).json()
    assert funnel["total"] == 3
    assert all(x["status_word"] == "Invited" for x in funnel["referrals"])

    # The first one signs up and is approved; the funnel follows him.
    signup = store.provision_user(email=invitees[0], password="pw-12345678",
                                  role="evaluator", full_name="Dr Referral 0")
    store.claim_referral_for_signup(email=invitees[0], user_id=signup["id"])
    r = client.post(f"/api/asclepius/verify/queue/{signup['id']}/approve",
                    json={"tier": "labeler"}, headers=admin_h)
    assert r.status_code == 200, r.text

    funnel = client.get("/api/asclepius/advisor/referrals", headers=toby_h).json()
    assert funnel["active"] == 1
    assert sorted(x["status_word"] for x in funnel["referrals"]) == [
        "Active", "Invited", "Invited"]

    # ── 6. He opens a pending export bundle and requests changes ────────────
    export_id = f"exp-{uuid.uuid4().hex[:8]}"
    store.insert_export(export_id=export_id, created_by=admin["email"], record_count=240,
                        filters={}, dir_path="", manifest={"profile": "default"})

    queue = client.get("/api/asclepius/advisor/queue", headers=toby_h).json()
    assert any(e["export_id"] == export_id for e in queue["queue"]["export_bundle"])

    r = client.get(f"/api/asclepius/advisor/artifacts/export_bundle/{export_id}",
                   headers=toby_h)
    assert r.status_code == 200, r.text
    assert r.json()["record_count"] == 240

    r = client.post("/api/asclepius/advisor/signoffs",
                    json={"artifact_type": "export_bundle", "artifact_id": export_id,
                          "verdict": "changes_requested",
                          "comments": "The data dictionary omits the renal dosing units."},
                    headers=toby_h)
    assert r.status_code == 200, r.text
    assert r.json()["blocking"] is False

    # ── 7. …and the comments appear in admin, WITH the relationship ─────────
    admin_view = client.get("/api/asclepius/admin/signoffs", headers=admin_h).json()
    row = next(s for s in admin_view["signoffs"] if s["artifact_id"] == export_id)
    assert row["verdict"] == "changes_requested"
    assert "renal dosing units" in row["comments"]
    assert row["relationship"] == "advisor_equity"
    assert row["advisor_name"]
    assert store.get_export(export_id)["signoff_status"] == "changes_requested"

    # ── 8. A record he labeled ships with his real credential AND the flag ──
    own = store.insert_submission(
        submission_id=f"sub-{uuid.uuid4().hex[:10]}", task_id=task["task_id"],
        evaluator_id=toby["id"], verdict="A_better", chosen_id="A", rejected_id="B",
        confidence="high", time_spent_sec=120,
        payload={"verdict": "A_better", "chosen_id": "A", "rejected_id": "B",
                 "rationale": "A is correct for this patient.",
                 "prompt_review": {"reviewed": True, "verdict": "valid"}},
        annotator=store.annotator_block(toby), dedupe_hash=None)
    records = packaging.package_submission(task, own, store=store)
    assert records
    for rec in records:
        # Both facts, in the same block. The credential is real; the
        # relationship is disclosed. Neither sentence works without the other.
        assert rec["annotator_credential"] == "ABIM — Nephrology"
        assert rec["related_party"] is True
