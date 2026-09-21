"""Accepting a physician grants membership; an admin still has to assign work."""

import pytest
from fastapi.testclient import TestClient

from asclepius import case_access
from community import onboard
from tests import _asclepius as A


@pytest.mark.parametrize("role,tier,account_kind", [
    ("evaluator", "labeler", None),
    ("evaluator", "reviewer", None),
    ("qa_reviewer", "reviewer", None),
    ("evaluator", "reviewer", "advisor"),
])
def test_approval_requires_separate_assignment(monkeypatch, role, tier, account_kind):
    # conftest opts old queue tests into the open pool. Exercise the deployed
    # default here, including the real HTTP approval endpoint and its side effects.
    monkeypatch.delenv("ASCLEPIUS_OPEN_CASE_POOL_ENABLED", raising=False)

    async def no_community_welcome(*args, **kwargs):
        return None

    monkeypatch.setattr(onboard, "welcome_new_member", no_community_welcome)
    store = A.fresh_store()
    admin = A.make_user(store, role="admin")
    physician = A.make_user(
        store, role=role, tier=None, practice_case=False, specialty="nephrology")
    if account_kind:
        with store._conn() as conn:
            conn.execute("UPDATE users SET account_kind=? WHERE id=?",
                         (account_kind, physician["id"]))
    store.set_verification_status(physician["id"], "pending")
    A.pass_practice_case(store, physician["id"])
    store.set_first_run(physician["id"], {
        "stops": {name: "done" for name in (
            "welcome", "start", "practice", "community", "earnings", "manual")},
        "completed_at": "2026-09-21T12:00:00Z",
        "dismissed_at": "2026-09-21T12:00:00Z",
    })

    # Keep real and synthetic production-style work available, so an empty
    # physician queue proves authorization rather than absence of supply.
    chart = {"case_source": "real_deid", "notes": [{"text": "Fictional chart"}]}
    cases = [store.insert_task(
        prompt="Fictional admission fixture", specialty="nephrology", difficulty="hard",
        candidate_answers=[{"id": "A", "text": "Fixture A"},
                           {"id": "B", "text": "Fixture B"}],
        **extra,
    ) for extra in (
        {},
        {"case": chart},
        {"case": chart, "trajectory_id": "admission-fixture", "sequence_index": 0},
    )]
    client = TestClient(A.app)
    admin_headers = A.headers_for(admin)
    physician_headers = A.headers_for(physician)
    response = client.post(
        f"/api/asclepius/verify/queue/{physician['id']}/approve",
        json={"tier": tier, "note": "Synthetic test approval"}, headers=admin_headers)
    assert response.status_code == 200, response.text
    approved = store.get_user_by_id(physician["id"])
    assert approved["verification_status"] == "approved"
    assert approved["tier"] == tier
    assert approved["real_data_approved"]
    assert case_access.assignment_required(approved)

    for version in ("v1", "v2", "v3", "v4", "v5"):
        for endpoint in ("available", "next"):
            response = client.get(
                f"/api/asclepius/tasks/{endpoint}",
                params={"portal_version": version}, headers=physician_headers)
            assert response.status_code == 200, response.text
            if endpoint == "available":
                assert response.json()["tasks"] == []
            else:
                assert response.json()["task"] is None

    for task in cases:
        task_id = task["task_id"]
        for suffix in ("", "/answers"):
            response = client.get(
                f"/api/asclepius/tasks/{task_id}{suffix}", headers=physician_headers)
            assert response.status_code == 403, response.text
        response = client.post(
            f"/api/asclepius/tasks/{task_id}/reveal", headers=physician_headers,
            json={"text": "Fictional independent answer"})
        assert response.status_code == 403, response.text
        response = client.post(
            "/api/asclepius/submissions", headers=physician_headers,
            json={"task_id": task_id, "verdict": "A_better", "chosen_id": "A"})
        assert response.status_code == 403, response.text

    response = client.post(
        "/api/asclepius/sessions", headers=physician_headers, json={"kind": "review"})
    assert response.status_code == 403, response.text
    assignment = {"task_ids": [cases[1]["task_id"]],
                  "user_ids": [physician["id"]], "dry_run": False}
    response = client.post(
        "/api/asclepius/admin/assignments/allocate",
        headers=physician_headers, json=assignment)
    assert response.status_code == 403, response.text
    with store._conn() as conn:
        for table in ("assignments", "submissions", "earnings", "work_sessions"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table

    # Positive control: the same approved physician and chart become usable
    # only after an admin assigns it. Earlier denials cannot be an unrelated
    # incomplete-onboarding gate or a fixture that can never serve real work.
    from routers import asclepius_admin
    monkeypatch.setattr(asclepius_admin.asc_route_notify, "notify_routed", lambda *a, **k: {})
    response = client.post(
        "/api/asclepius/admin/assignments/allocate", headers=admin_headers, json=assignment)
    assert response.status_code == 200, response.text
    assert response.json()["committed"]
    response = client.get("/api/asclepius/tasks/next", headers=physician_headers)
    assert response.status_code == 200, response.text
    assert response.json()["task"]["task_id"] == cases[1]["task_id"]
    response = client.post(
        f"/api/asclepius/tasks/{cases[1]['task_id']}/reveal", headers=physician_headers,
        json={"text": "Fictional independent answer after assignment"})
    assert response.status_code == 200, response.text
