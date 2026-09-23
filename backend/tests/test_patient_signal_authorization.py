"""Patient signals require the correct session before any model or storage work."""
from copy import deepcopy
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import main
import patient_session
from scripts.data_inventory import snapshot
from team_store import TeamStore
from tests._role_auth import tenant_token
from tests.test_postop_router import _checkin_payload, _survey_answers
from triage.postop.patient_state import ensure_postop_patient_state


def _clinical_tables(store):
    # Access denials deliberately append audit_events; no clinical table may
    # gain, lose, or alter a row as a result of the rejected request.
    return {name: table for name, table in snapshot(store.db_path)["tables"].items()
            if name != "audit_events"}


@pytest.fixture
def signals(tmp_path, monkeypatch):
    store = TeamStore(str(tmp_path / "signals.db"))
    monkeypatch.setenv("TEAM_DB_PATH", store.db_path)
    patients = {}
    sessions = {}
    flags = {}
    for pid in ("own", "foreign"):
        patients[pid] = {
            "id": pid, "health_system_id": pid + "-hs", "phase": "post_op",
            "current_tier": "TIER_1", "initial_tier": "TIER_1", "post_intraop_tier": "TIER_1",
            "discharge_at": (datetime.utcnow() - timedelta(days=1)).isoformat(),
            "structured_data": {"procedure_date": "2099-12-15", "procedure_name": "Synthetic procedure"},
            "resources": {"preop": {"voice_script": "Synthetic instruction", "battlecard_html": "<p>Synthetic</p>"}},
        }
        ensure_postop_patient_state(patients[pid])
        store.ensure_episode(patient_id=pid, health_system_id=pid + "-hs")
        sessions[pid] = store.save_teachback_session(
            patient_id=pid, track="pre_op", questions=[], results={"synthetic_original": pid},
            completed=True, prompt_version="synthetic", model="fake")
        flags[pid] = store.create_self_flag(patient_id=pid, free_text="Synthetic original")
    with store._conn() as conn:
        patient_session._ensure_table(conn)
    from token_revocation import is_revoked
    is_revoked("synthetic-initialization")
    monkeypatch.setattr(main, "_patient_store", patients)
    monkeypatch.setattr(main, "_team_store", store)
    monkeypatch.setattr(main.app.state, "patient_store", patients)
    monkeypatch.setattr(main.app.state, "team_store", store)
    client = TestClient(main.app)
    yield store, client, patients, sessions, flags
    client.close()


CASES = [
    ("POST", "/api/episodes/own/pam", {"responses": [{"item_index": i, "value": 4} for i in range(1, 14)]}),
    ("POST", "/api/events/preop-video", {"episode_id": "own", "session_id": "synthetic", "duration_sec": 30, "completed_session": False}),
    ("POST", "/api/events/battlecard", {"episode_id": "own", "dwell_ms": 2500, "scroll_depth_pct": 80}),
    ("POST", "/api/episodes/own/teachback/pre_op/start", {}),
    ("POST", "/api/episodes/own/teachback/pre_op/answer", {"question_id": "synthetic", "answer": "Synthetic answer"}),
    ("GET", "/api/episodes/own/teachback/pre_op", None),
    ("POST", "/api/episodes/own/postop/checkin", _checkin_payload()),
    ("POST", "/api/episodes/own/postop/survey/7", _survey_answers()),
    ("POST", "/api/episodes/own/postop/med-adherence", {"response": "YES"}),
    ("POST", "/api/episodes/own/postop/video-event", {"video_kind": "RED_FLAG", "event_type": "PLAYED", "session_id": "synthetic"}),
    ("POST", "/api/episodes/own/postop/self-flag", {"free_text": "Synthetic signal"}),
]


@pytest.mark.parametrize("actor", ["anonymous", "wrong-patient", "staff"])
@pytest.mark.parametrize("method,path,body", CASES)
def test_all_patient_signal_routes_reject_wrong_principal_without_changes(signals, monkeypatch, actor, method, path, body):
    store, client, patients, _, _ = signals
    if actor == "wrong-patient":
        client.cookies.set("pt_session", patient_session.create_patient_session("foreign", "foreign-hs"))
    elif actor == "staff":
        client.headers["Authorization"] = "Bearer " + tenant_token(health_system_id="own-hs")

    async def unexpected_model(*args, **kwargs):
        raise AssertionError("Unauthorized request reached model work")

    monkeypatch.setattr("routers.teachback.generate_teachback_questions", unexpected_model)
    monkeypatch.setattr("routers.teachback.grade_answer", unexpected_model)
    before_db = _clinical_tables(store)
    before_patients = deepcopy(patients)
    response = client.request(method, path, **({"json": body} if body is not None else {}))
    assert response.status_code == (403 if actor == "staff" else 404)
    assert _clinical_tables(store) == before_db
    assert patients == before_patients


@pytest.mark.parametrize("method,path,body", [CASES[0], CASES[5], CASES[8]])
def test_bound_patient_keeps_each_family_flow(signals, method, path, body):
    _, client, _, _, _ = signals
    client.cookies.set("pt_session", patient_session.create_patient_session("own", "own-hs"))
    response = client.request(method, path, **({"json": body} if body is not None else {}))
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("completed", [True, False])
def test_teachback_answer_cannot_select_another_patients_session(signals, monkeypatch, completed):
    store, client, patients, sessions, _ = signals
    client.cookies.set("pt_session", patient_session.create_patient_session("own", "own-hs"))
    results = {"synthetic_original": "foreign"} if completed else {
        "current_index": 0, "items": [{"question": {"id": "synthetic"}, "attempts": []}]}
    store.update_teachback_session(session_id=sessions["foreign"], completed=completed, results=results)

    async def unexpected_grading(*args, **kwargs):
        raise AssertionError("A foreign session reached grading")

    monkeypatch.setattr("routers.teachback.grade_answer", unexpected_grading)
    before_db = _clinical_tables(store)
    before_patients = deepcopy(patients)
    response = client.post("/api/episodes/own/teachback/pre_op/answer", json={
        "session_id": sessions["foreign"], "question_id": "synthetic", "answer": "Synthetic answer"})
    assert response.status_code == 404
    assert _clinical_tables(store) == before_db
    assert patients == before_patients


def test_scoped_rn_cannot_resolve_foreign_patient_flag(signals):
    store, client, patients, _, flags = signals
    client.headers["Authorization"] = "Bearer " + tenant_token("rn_coordinator", health_system_id="own-hs")
    before_db = _clinical_tables(store)
    before_patients = deepcopy(patients)
    response = client.post("/api/episodes/own/postop/self-flag/resolve", json={
        "flag_id": flags["foreign"], "resolved_by": "synthetic-rn"})
    assert response.status_code == 404
    assert _clinical_tables(store) == before_db
    assert patients == before_patients
    accepted = client.post("/api/episodes/own/postop/self-flag/resolve", json={
        "flag_id": flags["own"], "resolved_by": "synthetic-rn"})
    assert accepted.status_code == 200
    assert store.has_active_self_flag("foreign")
    assert not store.has_active_self_flag("own")
