"""End-to-end tests for the admin refinement pass:

  * QA queue carries an admin-only contributor identity block (name/org/email).
  * Contributor detail exposes full_name + email; /submissions lists tasks.
  * Scoped export supports a single submission (submission_id) + time window.
  * Buyer delivery: admin sends selected-org data → buyer account provisioned +
    delivery recorded + buyer can sign in and download from the workspace.
  * The buyer role is denied the admin/QA surface and admitted only at /buyer/*.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

import tests._asclepius as A
from asclepius import pipeline as asc_pipeline
from asclepius import profiles as asc_profiles

client = TestClient(A.app)
B = "/api/asclepius"


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


def _evaluator(org="Riverside Nephrology Associates"):
    return A.make_user(
        _store(), role="evaluator", specialty="nephrology",
        board_cert="board_certified_nephrology", years_experience=12, organization=org,
    )


def _submit_export_ready(admin_h, ev_h, **task_kw):
    base = {
        "specialty": "nephrology", "difficulty": "hard", "max_labels": 1,
        "prompt": f"Hyperkalemia case {A.uniq(8)}?",
        "candidate_answers": [{"id": "A", "text": "Calcium then dialyze."},
                              {"id": "B", "text": "Dialysate K+ 1.0."}],
    }
    base.update(task_kw)
    tid = client.post(f"{B}/tasks", json={"tasks": [base]}, headers=admin_h).json()["created"][0]
    sid = "s-" + uuid.uuid4().hex[:12]
    r = client.post(f"{B}/submissions", json={
        "submission_id": sid, "task_id": tid, "verdict": "A_better",
        "chosen_id": "A", "rejected_id": "B", "time_spent_sec": 130,
        "prompt_review": {"reviewed": True, "verdict": "valid"},
        "independent_answer": {"text": "Stabilize with IV calcium, shift potassium, then dialyze."},
        "chosen_revision": {"edited": False, "why_better_notes": "B over-lowers K+"},
        "rejected_critique": {"error_tags": ["dosing_error"], "why_worse": "too aggressive"},
    }, headers=ev_h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "export_ready"
    return tid, sid


def test_contributor_detail_and_submissions_have_identity():
    store = _store()
    admin_h = _admin_h()
    ev = _evaluator()
    # Give the evaluator a real name so the admin can see it (never exported).
    store.provision_user(email=ev["email"], password="pw-12345678", role="evaluator",
                         full_name="Dr. Casey Jones", org_name="Riverside Nephrology Associates")
    _submit_export_ready(admin_h, A.headers_for(ev))
    idh = ev["id_hashed"]

    r = client.get(f"{B}/contributors/{idh}", headers=admin_h)
    assert r.status_code == 200, r.text
    c = r.json()["contributor"]
    assert c["full_name"] == "Dr. Casey Jones"
    assert c["email"] == ev["email"]

    r = client.get(f"{B}/contributors/{idh}/submissions", headers=admin_h)
    assert r.status_code == 200, r.text
    subs = r.json()["submissions"]
    assert len(subs) == 1
    assert subs[0]["portal_version"]
    assert subs[0]["created_at"]


def test_single_task_scoped_export():
    admin_h = _admin_h()
    ev = _evaluator()
    _submit_export_ready(admin_h, A.headers_for(ev))
    _, sid2 = _submit_export_ready(admin_h, A.headers_for(ev))
    idh = ev["id_hashed"]

    # Export exactly one submission → one submission's records only.
    r = client.post(f"{B}/contributors/{idh}/export", headers=admin_h,
                    json={"profile": "default", "submission_id": sid2})
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["submission_count"] == 1


def test_qa_queue_has_contributor_identity(monkeypatch):
    store = _store()
    admin_h = _admin_h()
    ev = _evaluator()
    store.provision_user(email=ev["email"], password="pw-12345678", role="evaluator",
                         full_name="Dr. Pat Rivera", org_name="Riverside Nephrology Associates")
    # Force this submission into QA so it appears in /qa/queue.
    monkeypatch.setattr(asc_pipeline, "_should_sample", lambda: True)
    tid = client.post(f"{B}/tasks", json={"tasks": [{
        "specialty": "nephrology", "difficulty": "hard", "max_labels": 1,
        "prompt": f"AKI case {A.uniq(8)}?",
        "candidate_answers": [{"id": "A", "text": "Hold the ACEi."},
                              {"id": "B", "text": "Add an ACEi."}],
    }]}, headers=admin_h).json()["created"][0]
    client.post(f"{B}/submissions", json={
        "submission_id": "s-" + uuid.uuid4().hex[:12], "task_id": tid, "verdict": "A_better",
        "chosen_id": "A", "rejected_id": "B", "time_spent_sec": 130,
        "prompt_review": {"reviewed": True, "verdict": "valid"},
        "independent_answer": {"text": "Hold nephrotoxins and correct volume."},
        "chosen_revision": {"edited": False}, "rejected_critique": {"why_worse": "adds nephrotoxin"},
    }, headers=A.headers_for(ev))

    r = client.get(f"{B}/qa/queue", headers=admin_h)
    assert r.status_code == 200, r.text
    subs = r.json()["submissions"]
    assert subs, "expected a queued submission"
    ident = subs[0]["contributor"]
    assert ident["name"] == "Dr. Pat Rivera"
    assert ident["email"] == ev["email"]
    assert ident["organization"] == "Riverside Nephrology Associates"


def test_buyer_delivery_and_workspace_download(monkeypatch):
    # Email transport → dev mode so send-to-buyer succeeds without SendGrid.
    monkeypatch.setenv("EMAIL_DEV_MODE", "1")
    store = _store()
    admin_h = _admin_h()
    org = "Riverside Nephrology Associates"
    ev = _evaluator(org=org)
    _submit_export_ready(admin_h, A.headers_for(ev))

    # Admin sends the org's data to a buyer.
    buyer_email = f"buyer-{A.uniq(6)}@acme.example.com"
    r = client.post(f"{B}/admin/buyer-deliveries", headers=admin_h, json={
        "buyer_name": "Acme Frontier Labs", "buyer_email": buyer_email,
        "organizations": [org], "profile": "default",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["record_count"] >= 1
    assert body["first_delivery"] is True
    export_id = body["export_id"]

    # A buyer account now exists; it can sign in (the temp password was emailed —
    # in dev mode we can't read it, so drive the buyer via a minted token instead).
    buyer_user = store.get_user_by_email(buyer_email)
    assert buyer_user and buyer_user["role"] == "buyer"
    buyer_h = A.headers_for(buyer_user)

    # Buyer is DENIED the admin/QA surface (deny-by-default).
    assert client.get(f"{B}/qa/queue", headers=buyer_h).status_code == 403
    assert client.get(f"{B}/organizations", headers=buyer_h).status_code == 403

    # Buyer sees the delivery in their workspace and can download it.
    me = client.get(f"{B}/buyer/me", headers=buyer_h)
    assert me.status_code == 200, me.text
    assert me.json()["delivery_count"] == 1
    dl = client.get(f"{B}/buyer/deliveries", headers=buyer_h).json()["deliveries"]
    assert len(dl) == 1 and dl[0]["export_id"] == export_id
    z = client.get(f"{B}/buyer/deliveries/{export_id}/download", headers=buyer_h)
    assert z.status_code == 200
    assert z.headers["content-type"] == "application/zip"
    assert z.content[:2] == b"PK"

    # A DIFFERENT buyer cannot download this export.
    other = A.make_user(store, role="buyer", email=f"other-{A.uniq(6)}@x.example.com")
    assert client.get(f"{B}/buyer/deliveries/{export_id}/download",
                      headers=A.headers_for(other)).status_code == 404


def test_admin_is_denied_buyer_portal():
    admin_h = _admin_h()
    # An admin token is not a buyer → the buyer portal rejects it.
    assert client.get(f"{B}/buyer/me", headers=admin_h).status_code == 403


def test_send_to_existing_nonbuyer_email_is_refused(monkeypatch):
    """Delivering to an email that already belongs to an evaluator/admin must NOT
    convert that account into a buyer — it is refused and the account is intact."""
    monkeypatch.setenv("EMAIL_DEV_MODE", "1")
    store = _store()
    admin_h = _admin_h()
    org = "Riverside Nephrology Associates"
    ev = _evaluator(org=org)
    _submit_export_ready(admin_h, A.headers_for(ev))

    r = client.post(f"{B}/admin/buyer-deliveries", headers=admin_h, json={
        "buyer_name": "Acme", "buyer_email": ev["email"], "organizations": [org],
    })
    assert r.status_code == 409, r.text
    # The evaluator's account is untouched (still an evaluator, still active).
    still = store.get_user_by_email(ev["email"])
    assert still["role"] == "evaluator" and still["active"]
    # And no records were consumed / marked exported for the rejected send.
    assert not store.list_buyer_deliveries()
