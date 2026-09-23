"""Eligibility ownership/realm refusals preserve accepted document originals."""
from collections import defaultdict, deque
import copy
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

import auth
import realm
from eligibility import store
from realm_patient_store import RealmPatientStore
from routers import eligibility
from staff_context import StaffContext
from tenant_constants import DEMO_HEALTH_SYSTEM_ID
from tests._role_auth import landing_token, tenant_token


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAM_DB_PATH", str(tmp_path / "team.db"))
    monkeypatch.setenv(realm.ADMIN_PASSWORD_VAR, "synthetic-sandbox-password")
    monkeypatch.setattr(auth, "_users", {})
    monkeypatch.setattr(eligibility, "UPLOAD_DIR", tmp_path / "uploads")
    for name in ("ELIGIBILITY_DOCS", "ELIGIBILITY_CHECKS", "BATCHES"):
        monkeypatch.setattr(store, name, store._RealmMap())
    monkeypatch.setattr(store, "AUDIT_LOG", store._RealmList())
    monkeypatch.setattr(store, "_RATE_BUCKETS", store._RealmMap(lambda: defaultdict(deque)))
    async def no_pipeline(*args):
        pass
    monkeypatch.setattr(eligibility.pipeline, "run_batch", no_pipeline)
    app = FastAPI()
    app.add_middleware(realm.RealmMiddleware)
    app.include_router(eligibility.router)
    app.state.patient_store = RealmPatientStore()
    return TestClient(app), app.state.patient_store


def headers(token):
    return {"Authorization": "Bearer " + token}


def create(world, head):
    client, patients = world
    response = client.post("/api/eligibility-draft-patient", headers=head, json={"name": "Synthetic patient"})
    assert response.status_code == 200, response.text
    pid = response.json()["id"]
    upload = client.post("/api/eligibility-documents", headers=head, data={"patientId": pid},
                         files={"file": ("fixture.txt", b"Synthetic accepted original", "text/plain")})
    assert upload.status_code == 200, upload.text
    return pid, upload.json()["id"]


def test_unauthorized_draft_cancel_cannot_destroy_original_or_metadata(world):
    client, patients = world
    owner = headers(tenant_token(health_system_id="hospital-a"))
    pid, did = create(world, owner)
    patient_before, doc_before = copy.deepcopy(patients[pid]), copy.deepcopy(store.get_doc(did))
    path = Path(doc_before["path"])
    for head, status in (({}, 401), (headers(tenant_token(health_system_id="hospital-b")), 404),
                         (headers(landing_token()), 404), (headers(tenant_token(health_system_id="")), 403)):
        assert client.delete(f"/api/eligibility-draft-patients/{pid}", headers=head).status_code == status
        assert client.delete(f"/api/eligibility-documents/{did}", headers=head).status_code in (401, 404)
    assert patients[pid] == patient_before and store.get_doc(did) == doc_before
    assert path.read_bytes() == b"Synthetic accepted original"


def test_cancellation_and_document_detach_archive_without_losing_source(world):
    client, patients = world
    owner = headers(tenant_token(health_system_id="hospital-a"))
    pid, did = create(world, owner)
    original = copy.deepcopy(store.get_doc(did))
    assert client.delete(f"/api/eligibility-documents/{did}", headers=owner).json() == {"ok": True}
    assert client.delete(f"/api/eligibility-documents/{did}", headers=owner).json() == {"ok": True}
    assert client.get(f"/api/patient/{pid}/eligibility-documents", headers=owner).json() == {"documents": []}
    archived = store.get_doc(did, include_archived=True)
    assert archived["archived_at"] and all(archived[k] == v for k, v in original.items())
    assert Path(original["path"]).read_bytes() == b"Synthetic accepted original"
    pid2, did2 = create(world, owner)
    before = copy.deepcopy(patients[pid2])
    original2 = copy.deepcopy(store.get_doc(did2))
    assert client.delete(f"/api/eligibility-draft-patients/{pid2}", headers=owner).json() == {"ok": True}
    assert client.delete(f"/api/eligibility-draft-patients/{pid2}", headers=owner).json() == {"ok": True, "already_gone": True}
    assert patients[pid2]["archived_at"] and all(patients[pid2][k] == v for k, v in before.items())
    assert client.get(f"/api/patient/{pid2}/eligibility-documents", headers=owner).status_code == 404
    assert store.get_doc(did2) is None
    assert all(store.get_doc(did2, include_archived=True)[k] == v for k, v in original2.items())
    assert Path(original2["path"]).read_bytes() == b"Synthetic accepted original"


def test_landing_creation_uses_explicit_demo_owner_and_orphans_remain_inaccessible(world):
    client, patients = world
    landing = headers(landing_token())
    pid, _ = create(world, landing)
    assert patients[pid]["health_system_id"] == DEMO_HEALTH_SYSTEM_ID
    batch = client.post("/api/eligibility-batches", headers=landing,
                        files=[("files", ("fixture.txt", b"Synthetic", "text/plain"))])
    assert batch.status_code == 202
    bid = batch.json()["id"]
    assert store.get_batch(bid)["health_system_id"] == DEMO_HEALTH_SYSTEM_ID
    assert client.get(f"/api/eligibility-batches/{bid}", headers=landing).status_code == 200
    for owner in (None, ""):
        patients["orphan"] = {"name": "Preserved orphan", "health_system_id": owner, "relevant_files": []}
        store.save_batch("orphan", {"id": "orphan", "health_system_id": owner})
        assert client.get("/api/patient/orphan/eligibility-documents", headers=landing).status_code == 404
        assert client.get("/api/eligibility-batches/orphan", headers=landing).status_code == 404
    unknown = StaffContext("unrecognized", "fixture@example.org", "Synthetic", "surgeon", DEMO_HEALTH_SYSTEM_ID, None, None)
    with pytest.raises(HTTPException) as exc:
        eligibility._assert_patient_access(pid, unknown, patients)
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException):
        eligibility._assert_batch_access(store.get_batch(bid), unknown)


def test_audit_scopes_every_actor_and_filters_before_limit(world):
    client, patients = world
    patients.update({"own": {"health_system_id": "hospital-a"},
                     "foreign": {"health_system_id": "hospital-b"},
                     "demo": {"health_system_id": DEMO_HEALTH_SYSTEM_ID},
                     "orphan": {"health_system_id": None}})
    store.append_audit(action="own", actor="synthetic", patient_id="own")
    store.append_audit(action="foreign", actor="synthetic", patient_id="foreign")
    store.append_audit(action="demo", actor="synthetic", patient_id="demo")
    store.append_audit(action="orphan", actor="synthetic", patient_id="orphan")
    store.save_batch("own-batch", {"health_system_id": "hospital-a"})
    store.append_audit(action="batch", actor="synthetic", meta={"batch_id": "own-batch"})
    own = headers(tenant_token(health_system_id="hospital-a"))
    assert [e["action"] for e in client.get("/admin/audit/eligibility", headers=own).json()["events"]] == ["batch", "own"]
    assert client.get("/admin/audit/eligibility?limit=1", headers=own).json()["events"][0]["action"] == "batch"
    assert [e["action"] for e in client.get("/admin/audit/eligibility", headers=headers(landing_token())).json()["events"]] == ["demo"]
    assert client.get("/admin/audit/eligibility", headers=headers(tenant_token(health_system_id=""))).status_code == 403
    assert client.get("/admin/audit/eligibility").status_code == 401


def test_same_ids_docs_checks_batches_audit_and_rate_budget_are_realm_isolated(world):
    client, patients = world
    sessions, paths = {}, {}
    for name in ("live", "sandbox"):
        with realm.scoped(name):
            sessions[name] = headers(tenant_token(health_system_id="hospital-a"))
            patients["same-patient"] = {"health_system_id": "hospital-a", "relevant_files": ["same-doc"]}
            store.save_doc("same-doc", {"id": "same-doc", "patient_id": "same-patient", "filename": name})
            store.save_check("same-check", {"id": "same-check", "patient_id": "same-patient", "fixture": name})
            store.save_batch("same-batch", {"id": "same-batch", "health_system_id": "hospital-a", "fixture": name})
            store.append_audit(action=name, actor="synthetic", patient_id="same-patient")
    for name in ("live", "sandbox"):
        head = sessions[name]
        assert client.get("/api/patient/same-patient/eligibility-documents", headers=head).json()["documents"][0]["filename"] == name
        assert client.get("/api/eligibility-checks/same-check", headers=head).json()["fixture"] == name
        assert client.get("/api/eligibility-batches/same-batch", headers=head).json()["fixture"] == name
        assert client.get("/admin/audit/eligibility", headers=head).json()["events"][0]["action"] == name
        upload = client.post("/api/eligibility-documents", headers=head, data={"patientId": "same-patient"},
                             files={"file": ("fixture.txt", name.encode(), "text/plain")})
        assert upload.status_code == 200
        with realm.scoped(name):
            paths[name] = store.get_doc(upload.json()["id"])["path"]
            assert store.rate_limit_check("same-actor")
    assert Path(paths["sandbox"]).is_relative_to(eligibility.UPLOAD_DIR / "sandbox")
    assert not Path(paths["live"]).is_relative_to(eligibility.UPLOAD_DIR / "sandbox")
    with realm.scoped("sandbox"):
        store.delete_doc("same-doc")
        store.update_check("same-check", {"fixture": "sandbox-updated"})
        store.update_batch("same-batch", {"fixture": "sandbox-updated"})
        for _ in range(store._RATE_LIMIT_MAX - 1):
            assert store.rate_limit_check("same-actor")
        assert not store.rate_limit_check("same-actor")
    with realm.scoped("live"):
        assert store.get_doc("same-doc")["filename"] == "live"
        assert store.get_check("same-check")["fixture"] == store.get_batch("same-batch")["fixture"] == "live"
        assert store.rate_limit_check("same-actor")
