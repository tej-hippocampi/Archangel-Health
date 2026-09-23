"""Synthetic ownership checks before legacy clinical processing has side effects."""
import copy
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import main
import patient_session
import realm
from scripts.data_inventory import compare, snapshot
from staff_context import StaffContext, assert_staff_patient_scope
from team_store import TeamStore
from tests._role_auth import landing_token, tenant_token

_persist_patients = main._persist_demo_patient_store

ROUTES = ["/api/process-discharge", "/api/process-discharge/stream",
          "/api/process-preop", "/api/process-preop/stream", "/api/process-patient"]


def body(path, patient_id="synthetic-owned"):
    result = {"patient_id": patient_id, "patient_name": "Synthetic retained patient"}
    if path == "/api/process-patient":
        result.update({key: "Synthetic fixture" for key in (
            "phone_number", "pmh", "procedure_context", "after_visit_summary",
            "clinical_notes", "medication_list", "allergies", "problem_list")})
    else:
        result["preparation_notes" if "preop" in path else "discharge_notes"] = "Synthetic notes"
    return result


def headers(owner="hs-alpha", *, realm_name="live", landing=False):
    with realm.scoped(realm_name):
        token = (landing_token(email="synthetic-landing@example.org") if landing else
                 tenant_token(health_system_id=owner))
    return {"Authorization": "Bearer " + token}


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    patients = type(main._patient_store)()
    team = TeamStore(str(tmp_path / "team.db"))
    monkeypatch.setattr(main, "_patient_store", patients)
    monkeypatch.setattr(main.app.state, "patient_store", patients)
    monkeypatch.setattr(main, "_team_store", team)
    monkeypatch.setattr(main.app.state, "team_store", team)
    monkeypatch.setattr(main, "_persist_demo_patient_store", lambda: None)
    async def no_outreach(*args, **kwargs):
        pass
    monkeypatch.setattr(main, "_maybe_trigger_preop_outreach", no_outreach)
    monkeypatch.setenv(realm.ADMIN_PASSWORD_VAR, "synthetic-sandbox-only")
    monkeypatch.setenv("ENFORCE_PATIENT_AUTH", "1")
    monkeypatch.setenv("TEAM_DB_PATH", team.db_path)
    monkeypatch.setattr(main, "get_doctor_profile", lambda *_: None)
    return TestClient(main.app), patients, team


def seed(patients, pid="synthetic-owned", owner="hs-alpha"):
    patients[pid] = {"name": "Synthetic retained patient", "health_system_id": owner,
                     "structured_data": {"retained": "Complete synthetic original"},
                     "resources": {}, "clinic_code": "TEST", "resource_code": "SYNTHETIC"}


@pytest.mark.parametrize("path", ROUTES)
@pytest.mark.parametrize("caller", ["anonymous", "foreign", "orphan"])
def test_processing_refuses_before_model_or_patient_changes(isolated, monkeypatch, path, caller):
    client, patients, team = isolated
    seed(patients, owner=None if caller == "orphan" else "hs-alpha")
    before_patients = copy.deepcopy(dict(patients))
    before_db = snapshot(team.db_path)
    calls = []
    def unexpected(*args, **kwargs):
        calls.append(1)
        raise AssertionError("Denied processing reached an external-model boundary")
    monkeypatch.setattr(main, "run_postop_stream", unexpected)
    monkeypatch.setattr(main, "run_preop_stream", unexpected)
    monkeypatch.setattr(main, "IngestLayer", unexpected)
    auth = {} if caller == "anonymous" else headers("hs-beta" if caller == "foreign" else "hs-alpha")
    response = client.post(path, json=body(path), headers=auth)
    assert response.status_code == (401 if caller == "anonymous" else 404)
    assert calls == []
    assert dict(patients) == before_patients
    assert compare(before_db, snapshot(team.db_path)) == []


def test_sandbox_staff_cannot_read_live_patient_with_same_tenant_id(isolated):
    client, patients, _ = isolated
    seed(patients)
    url = "/api/patient/synthetic-owned/discharge"
    assert client.get(url, headers=headers()).status_code == 200
    response = client.get(url, headers=headers(realm_name="sandbox"))
    assert response.status_code == 404


def stub_processing(monkeypatch, calls):
    async def stream(input_data, **kw):
        assert kw["ctx"].team_store.get_episode(kw["patient_id"])["health_system_id"] == kw["health_system_id"]
        calls.append(kw["patient_id"])
        kw["ctx"].patient_store[kw["patient_id"]] = {
            "name": input_data.patient_name, "health_system_id": kw["health_system_id"],
            "structured_data": {"synthetic": "completed"}}
        yield {"stage": "complete", "payload": {"patient_id": kw["patient_id"]}}
    monkeypatch.setattr(main, "run_postop_stream", stream)
    monkeypatch.setattr(main, "run_preop_stream", stream)
    def ingest(data):
        assert main._team_store.get_episode(data["patient_id"])["health_system_id"]
        calls.append(data["patient_id"])
        return data
    async def extract(data):
        return {"synthetic": "completed"}
    async def generate(*args):
        return "Synthetic script", "<p>Synthetic card</p>"
    async def synthesize(**kw):
        return SimpleNamespace(script=kw["script"]), None
    async def avatar(**kw):
        return {"conversation_url": None}
    monkeypatch.setattr(main, "IngestLayer", lambda: SimpleNamespace(process=ingest))
    monkeypatch.setattr(main, "ExtractionLayer", lambda: SimpleNamespace(extract=extract))
    monkeypatch.setattr(main, "ClassificationLayer", lambda: SimpleNamespace(classify=lambda *_: "post_op"))
    monkeypatch.setattr(main, "GenerationLayer", lambda: SimpleNamespace(generate=generate))
    monkeypatch.setattr(main, "synthesize_script", synthesize)
    monkeypatch.setattr(main, "apply_grounding_to_patient", lambda *args: None)
    monkeypatch.setattr(main, "TavusClient", lambda: SimpleNamespace(create_conversation=avatar))
    monkeypatch.setattr(main, "_send_sms", lambda **kw: None)


@pytest.mark.parametrize("path", ROUTES)
@pytest.mark.parametrize("landing", [False, True])
@pytest.mark.parametrize("existing", [False, True])
def test_own_and_explicit_demo_processing_keep_owner(isolated, monkeypatch, path, landing, existing):
    client, patients, team = isolated
    owner = main.DEMO_HEALTH_SYSTEM_ID if landing else "hs-alpha"
    if existing:
        seed(patients, owner=owner)
    calls = []
    stub_processing(monkeypatch, calls)
    response = client.post(path, json=body(path), headers=headers(landing=landing))
    assert response.status_code == 200, response.text
    assert calls == ["synthetic-owned"]
    assert patients["synthetic-owned"]["health_system_id"] == owner
    assert team.get_episode("synthetic-owned")["health_system_id"] == owner


def test_unknown_staff_scope_and_orphans_are_never_shared(isolated):
    client, patients, _ = isolated
    seed(patients)
    seed(patients, "synthetic-demo", main.DEMO_HEALTH_SYSTEM_ID)
    seed(patients, "synthetic-orphan", None)
    for owner, landing, allowed in [("hs-alpha", False, "synthetic-owned"),
                                     ("unused", True, "synthetic-demo")]:
        response = client.get("/api/patients", headers=headers(owner, landing=landing))
        assert response.status_code == 200
        assert {row["id"] for row in response.json()["patients"]} == {allowed}
        assert client.get("/api/patient/synthetic-orphan/discharge",
                          headers=headers(owner, landing=landing)).status_code == 404
    for source, tenant_id in [("tenant", None), ("unknown", "hs-alpha")]:
        staff = StaffContext(source, "synthetic@example.org", None, "surgeon", tenant_id, None, None)
        for patient in patients.values():
            with pytest.raises(main.HTTPException) as refused:
                assert_staff_patient_scope(patient=patient, staff=staff)
            assert refused.value.status_code == 404
        assert not main._patient_principal_ok("synthetic-owned", staff)


def test_concurrent_owner_claims_have_one_winner_and_retries_preserve_original(isolated):
    _, _, team = isolated
    owners = ["hs-alpha", "hs-beta"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        won = list(pool.map(lambda owner: team.claim_patient_owner("synthetic-race", owner), owners))
    assert sum(won) == 1
    winner = owners[won.index(True)]
    before = snapshot(team.db_path)
    reopened = TeamStore(team.db_path)
    assert reopened.claim_patient_owner("synthetic-race", winner)
    assert not reopened.claim_patient_owner("synthetic-race", owners[won.index(False)])
    assert compare(before, snapshot(team.db_path)) == []


@pytest.mark.parametrize("path", ROUTES)
@pytest.mark.parametrize("reserved_owner", [None, "hs-beta"])
def test_persisted_unknown_or_foreign_owner_blocks_before_processing(isolated, monkeypatch, path, reserved_owner):
    client, patients, team = isolated
    team.ensure_episode(patient_id="synthetic-owned", health_system_id=reserved_owner)
    before = snapshot(team.db_path)
    calls = []
    stub_processing(monkeypatch, calls)
    response = client.post(path, json=body(path), headers=headers())
    assert response.status_code == 404
    assert calls == [] and not patients
    assert compare(before, snapshot(team.db_path)) == []


def test_failed_pipeline_keeps_owner_reservation_for_only_original_tenant(isolated, monkeypatch):
    client, patients, team = isolated
    def fail(*args, **kwargs):
        raise OSError("synthetic external-service failure")
    monkeypatch.setattr(main, "run_postop_stream", fail)
    response = client.post(ROUTES[0], json=body(ROUTES[0]), headers=headers())
    assert response.status_code == 500
    assert not patients
    assert team.get_episode("synthetic-owned")["health_system_id"] == "hs-alpha"
    before = snapshot(team.db_path)
    response = client.post(ROUTES[0], json=body(ROUTES[0]), headers=headers("hs-beta"))
    assert response.status_code == 404
    assert compare(before, snapshot(team.db_path)) == []
    calls = []
    stub_processing(monkeypatch, calls)
    assert client.post(ROUTES[0], json=body(ROUTES[0]), headers=headers()).status_code == 200
    assert calls == ["synthetic-owned"]


def test_patient_cache_and_snapshot_are_separate_per_realm(isolated, monkeypatch, tmp_path):
    _, patients, _ = isolated
    live_path = tmp_path / "patients.json"
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setenv("DEMO_PERSIST_PATIENT_STORE", "1")
    monkeypatch.setenv("DEMO_PATIENT_STORE_PATH", str(live_path))
    seed(patients)
    patients["synthetic-owned"]["name"] = "Synthetic live original"
    _persist_patients()
    live_bytes = live_path.read_bytes()
    with realm.scoped("sandbox"):
        assert not patients
        seed(patients)
        patients["synthetic-owned"]["name"] = "Synthetic sandbox original"
        _persist_patients()
        assert main._demo_patient_store_snapshot_path() == str(tmp_path / "sandbox" / "patients.json")
        patients.clear()
        main._load_demo_patient_store_snapshot()
        assert patients["synthetic-owned"]["name"] == "Synthetic sandbox original"
    assert live_path.read_bytes() == live_bytes
    assert patients["synthetic-owned"]["name"] == "Synthetic live original"
    patients.clear()
    main._load_demo_patient_store_snapshot()
    assert patients["synthetic-owned"]["name"] == "Synthetic live original"


def test_patient_cookie_cannot_cross_realms_with_colliding_patient_ids(isolated):
    client, patients, _ = isolated
    seed(patients)
    patients["synthetic-owned"]["structured_data"] = {"synthetic_realm": "live"}
    live_cookie = patient_session.create_patient_session("synthetic-owned", "hs-alpha")
    with realm.scoped("sandbox"):
        seed(patients)
        patients["synthetic-owned"]["structured_data"] = {"synthetic_realm": "sandbox"}
        sandbox_cookie = patient_session.create_patient_session("synthetic-owned", "hs-alpha")
    path = "/api/patient/synthetic-owned/discharge"
    client.cookies.set("pt_session", live_cookie)
    assert client.get(path).json()["structured_data"] == {"synthetic_realm": "live"}
    assert client.get(path, headers={realm.HEADER: "sandbox"}).status_code == 404
    client.cookies.set("pt_session_sandbox", sandbox_cookie)
    assert client.get(path, headers={realm.HEADER: "sandbox"}).json()["structured_data"] == {"synthetic_realm": "sandbox"}
    assert client.get(path).json()["structured_data"] == {"synthetic_realm": "live"}
    client.cookies.set("pt_session", sandbox_cookie)
    assert client.get(path).status_code == 404
