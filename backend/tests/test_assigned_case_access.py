"""The shipped contributor default: onboarding grants access, routing grants work."""
from fastapi.testclient import TestClient
import pytest

from tests import _asclepius as A
from asclepius import case_access
from scripts.data_inventory import snapshot, compare


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.delenv("ASCLEPIUS_OPEN_CASE_POOL_ENABLED", raising=False)
    A.fresh_store()


def setup():
    from asclepius.store import get_store
    store = get_store()
    doctor = A.make_user(store, specialty="nephrology")
    store.set_real_data_approved(doctor["id"], True)
    store.set_verification_status(doctor["id"], "approved")
    store.set_first_run(doctor["id"], {
        "stops": {s: "done" for s in ("welcome", "start", "practice", "community", "earnings", "manual")},
        "completed_at": "2026-09-14T12:00:00Z", "dismissed_at": "2026-09-14T12:00:00Z"})
    doctor = store.get_user_by_id(doctor["id"])
    return store, doctor, TestClient(A.app), A.headers_for(doctor)


def task(store, **kw):
    return store.insert_task(prompt="Synthetic test case", specialty="nephrology", difficulty="hard",
                             candidate_answers=[{"id": "A", "text": "Answer A"},
                                                {"id": "B", "text": "Answer B"}], **kw)


@pytest.mark.parametrize("version", [None, "v1", "v2", "v3", "v4", "v5"])
def test_welcome_completion_leaves_empty_queue_without_creating_work(version, monkeypatch):
    from routers import asclepius as routes
    store, doctor, client, headers = setup()
    task(store)
    task(store, case={"case_source": "real_deid", "notes": [{"text": "Synthetic chart"}]})
    def unexpected(*args, **kwargs):
        pytest.fail("A contributor's empty queue must not seed/generate tasks")
    for name in ("_ensure_gold_cases", "_ensure_v4_real_cases", "_autofill_queue"):
        monkeypatch.setattr(routes, name, unexpected)
    before = snapshot(store.db_path)
    params = {"portal_version": version} if version else {}
    available = client.get("/api/asclepius/tasks/available", headers=headers, params=params)
    assert available.status_code == 200, available.text
    assert available.json()["tasks"] == []
    assert available.json()["longitudinal_available"] == 0
    draw = client.get("/api/asclepius/tasks/next", headers=headers, params=params)
    assert draw.status_code == 200 and draw.json()["task"] is None, draw.text
    assert compare(before, snapshot(store.db_path)) == []


@pytest.mark.parametrize("access", ["none", "other", "review", "revoked", "expired", "timestamp", "invalid_expiry"])
def test_unassigned_case_cannot_be_opened_or_worked_by_any_route(access):
    store, doctor, client, headers = setup()
    t = task(store)
    if access != "none":
        who = A.make_user(store) if access == "other" else doctor
        row = store.upsert_assignment(task_id=t["task_id"], user_id=who["id"],
            role="review" if access == "review" else "label", assigned_by="admin",
            expires_at=("2000-01-01T00:00:00Z" if access == "timestamp" else
                        "invalid" if access == "invalid_expiry" else None))
        if access in ("revoked", "expired"):
            store.set_assignment_status(row["assignment_id"], access)
    before = snapshot(store.db_path)
    # A review assignment may read a case; it may never label it.
    response = client.get(f"/api/asclepius/tasks/{t['task_id']}", headers=headers)
    assert response.status_code == (200 if access == "review" else 403), response.text
    calls = [
        (f"/api/asclepius/tasks/{t['task_id']}/reveal", {"text": "My independent answer"}),
        ("/api/asclepius/submissions", {"task_id": t["task_id"], "verdict": "A_better", "chosen_id": "A"}),
        ("/api/asclepius/rubric/suggest", {"task_id": t["task_id"], "verdict": "A_better"}),
        ("/api/asclepius/assist/prelabel", {"task_id": t["task_id"]}),
    ]
    for url, payload in calls:
        response = client.post(url, headers=headers, json=payload)
        assert response.status_code == 403, (url, response.text)
    assert client.get(f"/api/asclepius/tasks/{t['task_id']}/answers", headers=headers).status_code == 403
    assert client.get("/api/asclepius/tasks/available", headers=headers).json()["count"] == 0
    assert store.next_double_label_for(doctor["id"]) is None
    assert compare(before, snapshot(store.db_path)) == []


@pytest.mark.parametrize("real", [False, True])
def test_admin_routing_restores_the_normal_case_flow(real, monkeypatch):
    from routers import asclepius_admin as admin_routes
    monkeypatch.setattr(admin_routes.asc_route_notify, "notify_routed", lambda *a, **k: {})
    store, doctor, client, headers = setup()
    t = task(store, **({"case": {"case_source": "real_deid", "notes": [{"text": "Test chart"}]}} if real else {}))
    other = A.make_user(store, specialty="nephrology")
    store.set_real_data_approved(other["id"], True)
    admin = A.make_user(store, role="admin")
    response = client.post("/api/asclepius/admin/assignments/allocate", headers=A.headers_for(admin),
        json={"task_ids": [t["task_id"]], "user_ids": [doctor["id"]], "dry_run": False})
    assert response.status_code == 200, response.text
    assert response.json()["committed"]
    for url in ("available", "next"):
        response = client.get("/api/asclepius/tasks/" + url, params={"portal_version": "v3"}, headers=headers)
        assert response.status_code == 200, response.text
        value = response.json()
        assert value["served_portal_version"] == ("v4" if real else "v3")
        assert (value["tasks"][0] if url == "available" else value["task"])["task_id"] == t["task_id"]
    assert client.get("/api/asclepius/tasks/available", headers=A.headers_for(other)).json()["count"] == 0
    assert client.get(f"/api/asclepius/tasks/{t['task_id']}", headers=headers).status_code == 200
    reveal = client.post(f"/api/asclepius/tasks/{t['task_id']}/reveal", headers=headers,
                         json={"text": "A clinically considered independent answer"})
    assert reveal.status_code == 200, reveal.text


def test_committed_non_exam_work_survives_rerouting():
    store, doctor, client, headers = setup()
    t = task(store)
    row = store.upsert_assignment(task_id=t["task_id"], user_id=doctor["id"], assigned_by="admin", role="label")
    assert client.post(f"/api/asclepius/tasks/{t['task_id']}/reveal", headers=headers,
                       json={"text": "Existing clinical reasoning"}).status_code == 200
    store.set_assignment_status(row["assignment_id"], "revoked")
    assert client.get(f"/api/asclepius/tasks/{t['task_id']}", headers=headers).status_code == 200
    assert client.get("/api/asclepius/tasks/available", headers=headers).json()["count"] == 1


def test_assigned_label_can_be_submitted_and_retried_once(monkeypatch):
    from asclepius import pipeline
    async def ok(*args, **kwargs):
        return {"consistent": True, "grounding_ok": True, "issues": [], "skipped": True}
    monkeypatch.setattr(pipeline, "run_critic", ok)
    monkeypatch.setattr(pipeline, "run_grounding_check", ok)
    store, doctor, client, headers = setup()
    t = task(store)
    store.upsert_assignment(task_id=t["task_id"], user_id=doctor["id"], assigned_by="admin", role="label")
    body = {"submission_id": "s-assigned-verified", "task_id": t["task_id"], "verdict": "A_better",
            "chosen_id": "A", "rejected_id": "B", "confidence": "high", "time_spent_sec": 180,
            "prompt_review": {"reviewed": True, "verdict": "valid"},
            "independent_answer": {"text": "Stabilize the myocardium with calcium before dialysis."},
            "chosen_revision": {"edited": False, "why_better_notes": "Prioritizes stabilization", "why_better_tags": ["safer"]},
            "rejected_critique": {"error_tags": ["dosing_error"], "severities": {}, "why_worse": "Premature intervention"}}
    for _ in range(2):
        response = client.post("/api/asclepius/submissions", headers=headers, json=body)
        assert response.status_code == 200, response.text
    assert len(store.submissions_for_task(t["task_id"])) == 1
    assert client.get("/api/asclepius/tasks/available", headers=headers).json()["count"] == 0


def test_legacy_qa_physician_cannot_adjudicate_unassigned_work():
    store, doctor, client, _ = setup()
    qa = A.make_user(store, role="qa_reviewer", tier="reviewer")
    headers = A.headers_for(qa)
    t = task(store)
    sub = store.insert_submission(submission_id="s-qa-test", task_id=t["task_id"],
        evaluator_id=doctor["id"], verdict="A_better", chosen_id="A", rejected_id="B",
        confidence="high", time_spent_sec=180, payload={}, annotator={}, dedupe_hash=None, status="needs_qa")
    assert client.get("/api/asclepius/qa/queue", headers=headers).json()["submissions"] == []
    assert client.get("/api/asclepius/submissions", headers=headers).json()["submissions"] == []
    assert client.get(f"/api/asclepius/submissions/{sub['submission_id']}", headers=headers).status_code == 403
    assert client.post("/api/asclepius/qa/approve-all", headers=headers).json()["approved"] == 0
    response = client.post(f"/api/asclepius/qa/{sub['submission_id']}/decision", headers=headers, json={"decision": "approve"})
    assert response.status_code == 403, response.text
    assert store.get_submission(sub["submission_id"])["status"] == "needs_qa"


def test_to_all_cannot_publish_an_unassigned_open_pool():
    store, _, client, _ = setup()
    t = task(store)
    admin = A.make_user(store, role="admin")
    before = snapshot(store.db_path)
    response = client.post("/api/asclepius/admin/assignments/allocate", headers=A.headers_for(admin),
        json={"task_ids": [t["task_id"]], "to_all": True, "dry_run": False})
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["error"] == "individual_assignment_required"
    assert compare(before, snapshot(store.db_path)) == []


def test_exam_commit_never_becomes_permission_for_paid_work(monkeypatch):
    from routers import asclepius as routes
    monkeypatch.setattr(routes, "_send_exam_received", lambda *a: None)
    store, _, client, _ = setup()
    applicant = A.make_user(store, specialty="nephrology", tier=None, practice_case=False)
    store.set_verification_status(applicant["id"], "pending")
    headers = A.headers_for(applicant)
    draw = client.get("/api/asclepius/exam/task", headers=headers)
    assert draw.status_code == 200, draw.text
    tid = draw.json()["task"]["task_id"]
    assert client.post(f"/api/asclepius/tasks/{tid}/reveal", headers=headers,
                       json={"text": "Examination reasoning"}).status_code == 200
    response = client.post("/api/asclepius/exam/submit", headers=headers,
                           json={"task_id": tid, "verdict": "A_better", "chosen_id": "A"})
    assert response.status_code == 200, response.text
    store.set_verification_status(applicant["id"], "approved")
    store.set_user_tier(applicant["id"], "labeler")
    A.pass_practice_case(store, applicant["id"])
    assert client.get(f"/api/asclepius/tasks/{tid}", headers=headers).status_code == 403
    assert client.post("/api/asclepius/submissions", headers=headers,
                       json={"task_id": tid, "verdict": "A_better", "chosen_id": "A"}).status_code == 403
    assert client.get("/api/asclepius/tasks/available", headers=headers).json()["tasks"] == []
    assert store.get_independent_commit(tid, applicant["id"])
    assert len(store.list_credentialing_exams(applicant["id"])) == 1


@pytest.mark.parametrize("legacy", [False, True])
def test_resetting_practice_cannot_unlock_an_unsubmitted_exam(legacy):
    import json
    store, _, client, _ = setup()
    applicant = A.make_user(store, specialty="nephrology", tier=None, practice_case=False)
    store.set_verification_status(applicant["id"], "pending")
    headers = A.headers_for(applicant)
    tid = client.get("/api/asclepius/exam/task", headers=headers).json()["task"]["task_id"]
    assert client.post(f"/api/asclepius/tasks/{tid}/reveal", headers=headers,
                       json={"text": "Unsubmitted examination reasoning"}).status_code == 200
    if legacy:
        with store._conn() as conn:
            row = conn.execute("SELECT payload_json FROM independent_commits WHERE task_id=? AND evaluator_id=?",
                               (tid, applicant["id"])).fetchone()
            payload = json.loads(row["payload_json"])
            payload.pop("purpose")
            conn.execute("UPDATE independent_commits SET payload_json=? WHERE task_id=? AND evaluator_id=?",
                         (json.dumps(payload), tid, applicant["id"]))
    store.set_verification_status(applicant["id"], "approved")
    store.set_user_tier(applicant["id"], "labeler")
    A.pass_practice_case(store, applicant["id"])
    for action in ("reset", "start", "reset", "advance"):
        response = client.patch("/api/asclepius/me/tutorial", headers=headers, json={"action": action, "step": "0"})
        assert response.status_code == 200, response.text
        assert store.get_tutorial_state(applicant["id"])["exam"]["task_id"] == tid
        assert client.get(f"/api/asclepius/tasks/{tid}", headers=headers).status_code == 403
    assert store.list_credentialing_exams(applicant["id"]) == []
    assert client.post("/api/asclepius/submissions", headers=headers,
                       json={"task_id": tid, "verdict": "A_better", "chosen_id": "A"}).status_code == 403


def test_untagged_commit_from_before_approval_is_not_paid_work_permission():
    store, doctor, client, headers = setup()
    t = task(store)
    store.commit_independent_answer(task_id=t["task_id"], evaluator_id=doctor["id"], payload={"text": "Legacy answer"})
    with store._conn() as conn:
        conn.execute("UPDATE independent_commits SET created_at='2000-01-01T00:00:00' WHERE evaluator_id=?", (doctor["id"],))
        conn.execute("UPDATE users SET verified_at='2001-01-01T00:00:00' WHERE id=?", (doctor["id"],))
    assert client.get(f"/api/asclepius/tasks/{t['task_id']}", headers=headers).status_code == 403
    assert client.get("/api/asclepius/tasks/available", headers=headers).json()["count"] == 0


def test_earlier_approval_evidence_preserves_work_across_reapproval():
    store, doctor, client, headers = setup()
    t = task(store)
    store.commit_independent_answer(task_id=t["task_id"], evaluator_id=doctor["id"], payload={"text": "Legacy paid answer"})
    store.log_event(entity_type="user", entity_id=doctor["id"], event_type="verification_approved", actor="admin")
    with store._conn() as conn:
        conn.execute("UPDATE events SET occurred_at='2000-01-01T00:00:00' WHERE entity_id=?", (doctor["id"],))
        conn.execute("UPDATE independent_commits SET created_at='2001-01-01T00:00:00' WHERE evaluator_id=?", (doctor["id"],))
        conn.execute("UPDATE users SET verified_at='2002-01-01T00:00:00' WHERE id=?", (doctor["id"],))
    assert client.get(f"/api/asclepius/tasks/{t['task_id']}", headers=headers).status_code == 200
    assert client.get("/api/asclepius/tasks/available", headers=headers).json()["count"] == 1


@pytest.mark.parametrize("difficulty", ["easy", "medium"])
def test_explicitly_routed_difficulty_is_not_hidden_by_default_picker(difficulty):
    store, doctor, client, headers = setup()
    t = store.insert_task(prompt="Routed case", specialty="nephrology", difficulty=difficulty)
    store.upsert_assignment(task_id=t["task_id"], user_id=doctor["id"], assigned_by="admin", role="label")
    available = client.get("/api/asclepius/tasks/available?portal_version=v3", headers=headers).json()
    drawn = client.get("/api/asclepius/tasks/next?portal_version=v3", headers=headers).json()
    assert available["tasks"][0]["task_id"] == drawn["task"]["task_id"] == t["task_id"]
    assert available["served_portal_version"] == drawn["served_portal_version"] == "v3"


@pytest.mark.parametrize("version", ["v1", "v2", "v3"])
def test_named_assignment_overrides_specialty_and_preserves_selected_version(version):
    store, doctor, client, headers = setup()
    t = store.insert_task(prompt="Explicit cross-specialty assignment", specialty="cardiology", difficulty="hard")
    assert client.get("/api/asclepius/tasks/available", headers=headers).json()["count"] == 0
    store.upsert_assignment(task_id=t["task_id"], user_id=doctor["id"], assigned_by="admin", role="label")
    for endpoint in ("available", "next"):
        response = client.get(f"/api/asclepius/tasks/{endpoint}", params={"portal_version": version}, headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        row = body["task"] if endpoint == "next" else body["tasks"][0]
        assert row["task_id"] == t["task_id"]
        assert body["served_portal_version"] == version


def test_assigned_environment_filters_before_window_and_can_be_annotated():
    store, doctor, client, headers = setup()
    t = task(store)
    wanted = store.insert_env_run(task_id=t["task_id"], specialty="nephrology", task_type="diagnosis", mode="rollout")
    with store._conn() as conn:
        conn.execute("UPDATE env_runs SET created_at='2000-01-01T00:00:00' WHERE run_id=?", (wanted["run_id"],))
    for _ in range(201):
        store.insert_env_run(task_id="another-environment", specialty="nephrology", task_type="diagnosis", mode="rollout")
    store.upsert_assignment(task_id=t["task_id"], user_id=doctor["id"], assigned_by="admin", role="label")
    response = client.get("/api/asclepius/environments/annotation-queue", headers=headers)
    assert response.status_code == 200, response.text
    assert [r["run_id"] for r in response.json()["queue"]] == [wanted["run_id"]]
    assert client.get(f"/api/asclepius/environments/runs/{wanted['run_id']}", headers=headers).status_code == 200
    response = client.post(f"/api/asclepius/environments/{t['task_id']}/annotate", headers=headers,
                           json={"run_id": wanted["run_id"], "annotation": {"notes": "Physician reviewed"}})
    assert response.status_code == 200, response.text


def test_expiry_sweep_cannot_return_case_to_new_contributors():
    store, doctor, client, headers = setup()
    t = task(store, distribution="assigned_only")
    store.upsert_assignment(task_id=t["task_id"], user_id=doctor["id"], assigned_by="admin", role="label",
                            exclusive=True, expires_at="2000-01-01T00:00:00Z")
    assert store.expire_stale_assignments() == 1
    assert store.get_task(t["task_id"])["distribution"] == "open"
    assert client.get("/api/asclepius/tasks/available", headers=headers).json()["count"] == 0
    assert client.get(f"/api/asclepius/tasks/{t['task_id']}", headers=headers).status_code == 403


def test_shared_image_uses_any_authorized_case_not_just_last_index_owner(monkeypatch):
    from asclepius import assets
    store, doctor, client, headers = setup()
    asset = {"asset_id": "shared-image", "sha256": "a" * 64, "mime": "image/png"}
    chart = {"case_source": "real_deid", "notes": [{"text": "Synthetic chart"}],
             "studies": [{"kind": "image", "asset": asset}]}
    mine, other = task(store, case=chart), task(store, case=chart)
    store.insert_asset_ref(**asset, task_id=other["task_id"], case_source="real_deid")
    assignment = store.upsert_assignment(task_id=mine["task_id"], user_id=doctor["id"], assigned_by="admin", role="label")
    reads = []
    def load(ref):
        reads.append(ref["asset_id"])
        return b"fixture-image", "image/png"
    monkeypatch.setattr(assets, "load_asset", load)
    response = client.get("/api/asclepius/assets/shared-image", headers=headers)
    assert response.status_code == 200 and response.content == b"fixture-image", response.text
    store.set_assignment_status(assignment["assignment_id"], "revoked")
    assert client.get("/api/asclepius/assets/shared-image", headers=headers).status_code == 403
    assert reads == ["shared-image"]


def test_unassigned_environment_annotation_and_asset_paths_are_closed(monkeypatch):
    from asclepius import assets
    store, doctor, client, headers = setup()
    t = task(store)
    run = store.insert_env_run(task_id=t["task_id"], specialty="nephrology", task_type="diagnosis", mode="rollout")
    assert client.get("/api/asclepius/environments/annotation-queue", headers=headers).json()["queue"] == []
    assert client.get(f"/api/asclepius/environments/runs/{run['run_id']}", headers=headers).status_code == 403
    response = client.post(f"/api/asclepius/environments/{t['task_id']}/annotate", headers=headers,
                           json={"run_id": run["run_id"], "annotation": {}})
    assert response.status_code == 403, response.text
    monkeypatch.setattr(assets, "find_asset_by_id", lambda *a: {"task_id": t["task_id"], "sha256": "synthetic"})
    monkeypatch.setattr(assets, "load_asset", lambda *a: pytest.fail("Unassigned asset was loaded"))
    assert client.get("/api/asclepius/assets/test-asset", headers=headers).status_code == 403
    assert store.get_env_run(run["run_id"])["physician_annotation"] is None


def test_default_cannot_be_opened_by_a_misspelled_configuration(monkeypatch):
    for value in ("", "true", "yes", "invalid", "0"):
        monkeypatch.setenv("ASCLEPIUS_OPEN_CASE_POOL_ENABLED", value)
        assert not case_access.open_pool_enabled()
