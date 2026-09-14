"""Advisor origin persists; reviewer access requires a human decision."""
import asyncio
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from asclepius import capabilities as caps
from asclepius import verification_agent
from scripts.data_inventory import snapshot, compare
from tests import _asclepius as A
from tests.test_signup_links import signup_client, _open_invite, _PW


@pytest.fixture
def setup():
    store = A.fresh_store()
    admin = A.make_user(store, role="admin")
    advisor = store.provision_user(email="advisor-test@stanford.edu", password=_PW,
                                   role="evaluator", account_kind="advisor",
                                   full_name="Test Advisor", attestations={"signedInitials": "TA"})
    store._init_schema()  # Normalize pre-existing startup defaults before the frozen baseline.
    return store, TestClient(A.app), A.headers_for(admin), advisor


def test_advisor_signup_is_pending_with_reviewer_proposed(signup_client):
    store = A.fresh_store()
    admin = A.make_user(store, role="admin")
    token, email = _open_invite(signup_client, flavor="advisor")
    assert signup_client.post("/api/onboarding/asclepius/password",
                              json={"token": token, "password": _PW}).status_code == 200
    result = signup_client.post("/api/onboarding/asclepius/finish", json={"token": token})
    assert result.status_code == 200, result.text
    assert result.json()["awaiting_review"] is True
    user = store.get_user_by_email(email)
    assert user["verification_status"] == "pending"
    assert user["tier"] is None
    assert user["application_completed_at"]
    queue = signup_client.get("/api/asclepius/verify/queue?ready=true", headers=A.headers_for(admin)).json()
    row = next(q for q in queue["queue"] if q["user_id"] == user["id"])
    assert row["proposed_tier_word"] == "Reviewer"
    assert row["allowed_tiers"] == ["reviewer"]
    assert row["account_kind"] == "advisor"
    assert row["ready_for_review"]


def test_legacy_advisor_migration_preserves_all_other_data_and_restores(setup, tmp_path):
    store, _, _, advisor = setup
    physician = A.make_user(store, specialty="nephrology")
    task = store.insert_task(prompt="Original synthetic clinical source", specialty="nephrology")
    store.record_credentialing_exam(user_id=physician["id"], task_id=task["task_id"],
        specialty="nephrology", attempt=1, payload={"original_answer": "Preserve exactly."})
    store.insert_submission(submission_id="original-submission", task_id=task["task_id"],
        evaluator_id=physician["id"], verdict="accept", chosen_id=None, rejected_id=None,
        confidence="high", time_spent_sec=180, payload={"original": "physician annotation"},
        annotator={"id": physician["id"]}, dedupe_hash="original-evidence")
    store._init_schema()
    # Match the old production state, including accidental startup assignment.
    with store._conn() as conn:
        conn.execute("UPDATE users SET verification_status=NULL, tier='labeler', "
                     "tier_assigned_by='migration:tier_backfill', tier_assigned_at='2026-09-13' WHERE id=?",
                     (advisor["id"],))
    source = tmp_path / "sources"
    source.mkdir()
    (source / "original.txt").write_text("Immutable original evidence")
    before = snapshot(store.db_path, {"sources": source})
    (tmp_path / "before.json").write_text(json.dumps(before, indent=2))
    backup = tmp_path / "backup.db"
    with sqlite3.connect(store.db_path) as src, sqlite3.connect(backup) as dst:
        src.backup(dst)
    store._init_schema()
    after = snapshot(store.db_path, {"sources": source})
    (tmp_path / "after.json").write_text(json.dumps(after, indent=2))
    assert compare(before, after, allowed={"users.verification_status", "users.tier"}) == []
    row = store.get_user_by_id(advisor["id"])
    assert row["verification_status"] == "pending" and row["tier"] is None
    assert row["verified_by"] is None and row["verified_at"] is None
    assert row["tier_assigned_by"] == "migration:tier_backfill"
    store._init_schema()
    assert compare(after, snapshot(store.db_path, {"sources": source})) == []
    restored = tmp_path / "restored.db"
    with sqlite3.connect(backup) as src, sqlite3.connect(restored) as dst:
        src.backup(dst)
    restored_snapshot = snapshot(restored, {"sources": source})
    (tmp_path / "restored.json").write_text(json.dumps(restored_snapshot, indent=2))
    assert compare(before, restored_snapshot) == []


@pytest.mark.parametrize("decision", ["approved", "rejected"])
def test_startup_preserves_prior_human_decisions(setup, decision):
    store, _, _, user = setup
    store.record_verification_decision(user["id"], status=decision, tier="reviewer",
                                      decided_by="owner@example.org", note="Original decision")
    before = store.get_user_by_id(user["id"])
    store._init_schema()
    assert store.get_user_by_id(user["id"]) == before


@pytest.mark.parametrize("path", ["/tasks/available", "/review/stats", "/exam/task"])
def test_pending_advisor_cannot_access_work_or_exam(setup, path):
    _, client, _, user = setup
    assert client.get("/api/asclepius" + path, headers=A.headers_for(user)).status_code == 403


def test_reviewer_approval_opens_work_and_does_not_train_physician_model(setup):
    store, client, headers, user = setup
    # Direct /approve clients and existing browser sessions use the same gate.
    token_headers = A.headers_for(user)
    result = client.post(f"/api/asclepius/verify/queue/{user['id']}/approve",
                         headers=headers, json={"tier": "reviewer"})
    assert result.status_code == 200, result.text
    current = store.get_user_by_id(user["id"])
    assert current["account_kind"] == "advisor"
    assert caps.can_surface(current, caps.REAL_WORK)
    assert caps.can_surface(current, caps.COMMUNITY_WRITE)
    assert current["verified_by"] and current["verified_at"]
    assert client.get("/api/asclepius/review/stats", headers=token_headers).status_code == 200
    with store._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tiering_decisions WHERE user_id=?", (user["id"],)).fetchone()[0] == 0


@pytest.mark.parametrize("path,status", [("verify/queue/{id}/approve", 400),
                                        ("verify/tiering/{id}/decide", 400),
                                        ("admin/physicians/restore?email=advisor-test@stanford.edu", 422)])
def test_labeler_approval_is_refused_by_every_admin_entry_point(setup, path, status):
    store, client, headers, user = setup
    path = path.format(id=user["id"])
    body = {"tier": "labeler"}
    if "restore" in path:
        body["approve_verification"] = True
    result = client.post("/api/asclepius/" + path, headers=headers, json=body)
    assert result.status_code == status, result.text
    assert store.get_user_by_id(user["id"])["verification_status"] == "pending"


def test_reject_closes_existing_sessions_without_retakes(setup):
    store, client, headers, user = setup
    # Even a pre-existing retake stamp cannot reopen a rejected advisor.
    store.set_tutorial_state(user["id"], {"status": "not_started", "retake_offered_at": "old", "exam": {"state": "submitted"}})
    old_tutorial = store.get_tutorial_state(user["id"])
    token_headers = A.headers_for(user)
    result = client.post(f"/api/asclepius/verify/queue/{user['id']}/reject",
                         headers=headers, json={"note": "Not accepted"})
    assert result.status_code == 200, result.text
    row = store.get_user_by_id(user["id"])
    assert caps.access_level(row) == caps.NONE
    assert caps.surfaces(row) == frozenset()
    assert store.get_tutorial_state(user["id"]) == old_tutorial
    for path in ("/auth/me", "/review/stats", "/exam/task"):
        assert client.get("/api/asclepius" + path, headers=token_headers).status_code == 403
    roster = client.get("/api/asclepius/admin/physicians", headers=headers).json()
    assert not any(u["id"] == user["id"] for u in roster["unfiled_physicians"])
    with store._conn() as conn:
        mail = conn.execute("SELECT body_html FROM admin_notify_outbox WHERE kind='physician_rejected'").fetchone()
    assert mail and "Platform access is closed" in mail[0]
    assert "Sign in and try again" not in mail[0]


def test_agent_cannot_approve_advisor_even_when_enabled(setup, monkeypatch):
    store, _, _, user = setup
    monkeypatch.setenv("ASCLEPIUS_VERIFY_AGENT_AUTO_APPROVE", "1")
    def forbidden(*args, **kwargs):
        pytest.fail("Advisor appointment reached automated credential decision")
    monkeypatch.setattr(verification_agent, "build_dossier", forbidden)
    result = asyncio.run(verification_agent.run_one(store, {"user_id": user["id"]}))
    assert result["outcome"] == "skipped"
    assert store.get_user_by_id(user["id"])["verification_status"] == "pending"


def test_pending_advisor_email_does_not_request_an_exam():
    from onboarding_emails import build_application_submitted_email
    mail = build_application_submitted_email(full_name="Test Advisor", advisor=True,
                                             portal_url="https://example.org/asclepius")
    assert "Reviewer access opens only after we approve" in mail
    assert "examination" not in mail.lower()


def test_pending_state_and_account_creation_rollback_together(setup):
    store, _, _, _ = setup
    with store._conn() as conn:
        conn.execute("CREATE TRIGGER fail_advisor BEFORE INSERT ON users WHEN NEW.account_kind='advisor' "
                     "BEGIN SELECT RAISE(ABORT, 'disk write failed'); END")
    with pytest.raises(sqlite3.DatabaseError):
        store.provision_user(email="failed@example.org", role="evaluator", account_kind="advisor")
    assert store.get_user_by_email("failed@example.org") is None


def test_approved_advisor_cannot_be_retiered_as_labeler(setup):
    store, client, headers, user = setup
    store.record_verification_decision(user["id"], status="approved", tier="reviewer", decided_by="owner")
    result = client.post(f"/api/asclepius/verify/retier/{user['id']}", headers=headers,
                         json={"tier": "labeler", "note": "Try alternate entry point"})
    assert result.status_code == 400, result.text
    assert store.set_user_tier(user["id"], "labeler") is False
    with pytest.raises(ValueError):
        store.record_verification_decision(user["id"], status="approved", tier="labeler", decided_by="owner")
    assert store.get_user_by_id(user["id"])["tier"] == "reviewer"
    roster = client.get("/api/asclepius/admin/physicians", headers=headers).json()
    row = next(u for u in roster["physicians"] if u["id"] == user["id"])
    assert row["account_kind"] == "advisor"


def test_advisors_are_not_chased_for_physician_credentials_or_exams(setup):
    from asclepius import onboarding_nudge
    store, _, _, user = setup
    for kind in ("credentials", "practice", "exam"):
        assert onboarding_nudge._still_owes(kind, user) is False
        assert store.list_applicants_needing_nudge(kind, older_than_hours=0) == []


def test_passwordless_advisor_is_waiting_on_a_decision_not_an_exam(setup):
    store, client, _, _ = setup
    user = store.provision_user(email="legacy-advisor@example.org", role="evaluator", account_kind="advisor")
    result = client.post("/api/asclepius/auth/login", json={"email": user["email"], "password": "unused"})
    assert result.status_code == 403
    assert result.headers["X-Asclepius-Auth-Gate"] == "pending_advisor"
    assert "reviewer application is under review" in result.json()["detail"]
    assert "exam" not in result.json()["detail"]


def test_advisor_qa_role_does_not_bypass_community_decision(setup):
    from community.router import _passes_gate
    store, _, _, user = setup
    with store._conn() as conn:
        conn.execute("UPDATE users SET role='qa_reviewer' WHERE id=?", (user["id"],))
    assert _passes_gate(store.get_user_by_id(user["id"])) is False


@pytest.mark.parametrize("action", ["broadcast", "ping"])
def test_rejected_advisor_open_websocket_loses_access(action):
    from tests.test_community import setup_world, client, BASE, token_for, post_msg
    store, _, _, admin = setup_world()
    user = store.provision_user(email="socket-advisor@example.org", password=_PW,
                               role="evaluator", account_kind="advisor")
    store.record_verification_decision(user["id"], status="approved", tier="reviewer", decided_by="owner")
    with client.websocket_connect(f"{BASE}/ws?token={token_for(user)}") as ws:
        assert ws.receive_json()["type"] == "hello"
        result = client.post(f"/api/asclepius/verify/queue/{user['id']}/reject",
                             headers=A.headers_for(admin), json={"note": "Not accepted"})
        assert result.status_code == 200, result.text
        if action == "broadcast":
            assert post_msg(admin, "introductions", "A private member update").status_code == 200
        else:
            ws.send_json({"type": "ping"})
        assert ws.receive()["type"] == "websocket.close"
