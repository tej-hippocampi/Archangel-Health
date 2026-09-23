"""Extraction results and progress obey the source patient's scope and realm."""
import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
import pytest

import auth
import realm
from realm_patient_store import RealmPatientStore
from routers import intraop
from scripts.data_inventory import compare, snapshot
from team_store import TeamStore
from tests._role_auth import landing_token, tenant_token


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAM_DB_PATH", str(tmp_path / "team.db"))
    monkeypatch.setenv(realm.ADMIN_PASSWORD_VAR, "synthetic-sandbox-password")
    monkeypatch.setattr(auth, "_users", {})
    monkeypatch.setattr(intraop, "_INTRAOP_UPLOAD_DIR", tmp_path / "originals")
    patients = RealmPatientStore()
    stores = {name: TeamStore(realm.paths(name)["team"]) for name in realm.REALMS}
    app = FastAPI()
    app.add_middleware(realm.RealmMiddleware)
    app.include_router(intraop.router)
    app.state.patient_store = patients
    app.state.team_store = realm.RealmProxy(lambda: stores[realm.current()])
    return TestClient(app), patients, stores


def headers(owner="hospital-a", *, landing=False):
    token = landing_token() if landing else tenant_token(health_system_id=owner)
    return {"Authorization": "Bearer " + token}


def seed(world, *, owner="hospital-a", name="live", status="COMPLETE"):
    _, patients, stores = world
    with realm.scoped(name):
        patients["same-patient"] = {"name": "Synthetic", "health_system_id": owner, "structured_data": {}}
        stores[name].save_intraop_extraction(extraction_id="same-extraction", patient_id="same-patient",
                                            pdf_blob_url="synthetic-original", status=status)
        stores[name].update_intraop_extraction(extraction_id="same-extraction", fields={"synthetic": name})


@pytest.mark.parametrize("suffix", ["", "/stream"])
@pytest.mark.parametrize("actor", ["anonymous", "foreign", "landing", "missing-tenant", "orphan"])
def test_extraction_denied_without_source_patient_ownership(world, suffix, actor):
    client, patients, stores = world
    seed(world, owner=None if actor == "orphan" else "hospital-a")
    head = {} if actor == "anonymous" else headers(
        "hospital-b" if actor == "foreign" else "" if actor == "missing-tenant" else "hospital-a",
        landing=actor == "landing")
    original = copy.deepcopy(dict(patients))
    before = snapshot(stores["live"].db_path)
    response = client.get("/api/intraop-extractions/same-extraction" + suffix, headers=head)
    assert response.status_code == (401 if actor == "anonymous" else 404)
    assert "synthetic" not in response.text
    assert dict(patients) == original
    assert compare(before, snapshot(stores["live"].db_path)) == []


def test_same_extraction_id_is_separate_in_each_realms_team_store(world):
    client, _, _ = world
    for name in realm.REALMS:
        seed(world, name=name)
    for name in realm.REALMS:
        with realm.scoped(name):
            head = headers()
        response = client.get("/api/intraop-extractions/same-extraction", headers=head)
        assert response.status_code == 200 and response.json()["fields"] == {"synthetic": name}
        stream = client.get("/api/intraop-extractions/same-extraction/stream", headers=head)
        assert stream.status_code == 200
        assert '"synthetic": "' + name + '"' in stream.text
        assert '"synthetic": "' + ("live" if name == "sandbox" else "sandbox") + '"' not in stream.text


def test_stream_rechecks_source_scope_before_later_delivery(world, monkeypatch):
    client, patients, stores = world
    seed(world, status="PENDING")
    async def no_wait(*args):
        pass
    monkeypatch.setattr(intraop, "asyncio", SimpleNamespace(sleep=no_wait))
    head = headers()
    request = Request({"type": "http", "app": client.app,
                       "headers": [(b"authorization", head["Authorization"].encode())]})

    async def scenario():
        response = await intraop.stream_intraop_extraction("same-extraction", request)
        iterator = response.body_iterator
        assert '"synthetic": "live"' in await anext(iterator)
        patients["same-patient"]["health_system_id"] = "hospital-b"
        stores["live"].update_intraop_extraction(extraction_id="same-extraction", status="COMPLETE",
                                                fields={"synthetic": "foreign-later-value"})
        refused = await anext(iterator)
        assert "event: error" in refused and "foreign-later-value" not in refused
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)
    asyncio.run(scenario())


def test_pdf_upload_stops_at_documented_limit_before_writing_or_enqueueing(world):
    client, patients, stores = world
    patients["same-patient"] = {"name": "Synthetic", "health_system_id": "hospital-a", "structured_data": {}}
    head = headers()
    request = Request({"type": "http", "app": client.app,
                       "headers": [(b"authorization", head["Authorization"].encode())]})
    before = snapshot(stores["live"].db_path)
    class Oversized:
        consumed = 0
        async def read(self, size=-1):
            assert 0 < size <= 64 * 1024
            self.consumed += size
            return b"x" * size
    upload = Oversized()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(intraop.upload_intraop_pdf("same-patient", request, file=upload))
    limit_mb = intraop.EXTRACTION["max_pdf_size_mb"]
    assert exc.value.status_code == 413 and exc.value.detail == f"PDF exceeds {limit_mb} MB limit"
    assert upload.consumed == limit_mb * 1024 * 1024 + 1
    assert not intraop._INTRAOP_UPLOAD_DIR.exists()
    assert compare(before, snapshot(stores["live"].db_path)) == []
