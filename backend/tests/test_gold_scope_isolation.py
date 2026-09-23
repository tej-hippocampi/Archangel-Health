"""Synthetic Gold ownership, realm, and bounded-upload regressions."""
import asyncio
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
import pytest

import auth
import realm
from gold import schema, store
from routers import gold
from staff_context import StaffContext
from tests._role_auth import landing_token, tenant_token


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAM_DB_PATH", str(tmp_path / "team.db"))
    monkeypatch.setenv(realm.ADMIN_PASSWORD_VAR, "synthetic-sandbox-password")
    monkeypatch.setattr(auth, "_users", {})
    monkeypatch.setattr(store, "_QUEUES", {})
    monkeypatch.setattr(store, "_RINGS", {})
    monkeypatch.setattr(gold, "GOLD_DIR", tmp_path / "audio")
    monkeypatch.setattr(gold, "_spawn", lambda coro: coro.close())
    app = FastAPI()
    app.add_middleware(realm.RealmMiddleware)
    app.include_router(gold.router)
    return TestClient(app)


def headers(token):
    return {"Authorization": "Bearer " + token}


def visit(vid, *, owner="landing:owner@example.org", tenant=None, status=store.ST_NEEDS_REVIEW):
    row = store.create_visit(visit_id=vid, tenant_id=tenant, tenant_slug="synthetic",
                            specialty="orthopedics", encounter_type="follow-up", created_by=owner)
    store.update_visit(vid, status=status, consent_given=1, transcript="Synthetic discussion",
                       clinician_id_hashed=schema.hash_clinician(owner))
    return row


def test_personal_visits_are_scoped_on_detail_lists_stats_and_all_mutations(world):
    owner = headers(landing_token(email="owner@example.org"))
    other = headers(landing_token(email="other@example.org"))
    operator = headers(landing_token("system_admin", email="operator@example.org"))
    visit("own")
    visit("foreign", owner="landing:other@example.org")
    visit("legacy-unowned", owner="")
    assert world.get("/api/gold/visits/own", headers=owner).status_code == 200
    assert [v["id"] for v in world.get("/api/gold/visits", headers=owner).json()["visits"]] == ["own"]
    stats = world.get("/api/gold/stats", headers=owner).json()
    assert stats["totals"]["captured"] == stats["surgeon"]["visits_contributed"] == 1
    assert world.get("/api/gold/visits/own", headers=other).status_code == 404
    assert world.get("/api/gold/visits/legacy-unowned", headers=owner).status_code == 404
    assert world.post("/api/gold/visits/own/stream-ticket", headers=other).status_code == 404
    assert world.get("/api/gold/visits/own/stream", headers=other).status_code == 404
    assert world.post("/api/gold/visits/own/consent", headers=other,
                      json={"consent_given": False}).status_code == 404
    assert world.post("/api/gold/visits/own/audio", headers=other,
                      files={"file": ("fixture.webm", b"fixture", "audio/webm")}).status_code == 404
    assert world.post("/api/gold/visits/own/submit", headers=other,
                      json={"gold_note": "Synthetic note"}).status_code == 404
    assert world.post("/api/gold/visits/own/approve", headers=operator, json={}).status_code == 404
    assert store.get_visit("own")["status"] == store.ST_NEEDS_REVIEW


def test_same_tenant_staff_keep_shared_access_and_missing_tenant_is_denied(world):
    owner = headers(tenant_token(email="owner@example.org", health_system_id="hospital-a"))
    peer = headers(tenant_token(email="peer@example.org", health_system_id="hospital-a"))
    foreign = headers(tenant_token(health_system_id="hospital-b"))
    missing = headers(tenant_token(health_system_id="", is_team_director=True))
    created = world.post("/api/gold/visits", headers=owner, json={})
    assert created.status_code == 200
    vid = created.json()["id"]
    assert world.get(f"/api/gold/visits/{vid}", headers=peer).status_code == 200
    assert world.get("/api/gold/visits", headers=peer).json()["visits"][0]["id"] == vid
    assert world.get("/api/gold/stats", headers=peer).json()["totals"]["captured"] == 1
    assert world.get(f"/api/gold/visits/{vid}", headers=foreign).status_code == 404
    for path in ("/api/gold/visits", "/api/gold/stats", f"/api/gold/visits/{vid}"):
        assert world.get(path, headers=missing).status_code == 403
    assert world.post("/api/gold/visits", headers=missing, json={}).status_code == 403
    assert world.post("/api/gold/export", headers=missing, json={}).status_code == 403


def test_personal_export_never_selects_or_marks_another_creators_records(world, monkeypatch):
    operator = headers(landing_token("system_admin", email="owner@example.org"))
    visit("own-ready", status=store.ST_EXPORT_READY)
    visit("foreign-ready", owner="landing:other@example.org", status=store.ST_EXPORT_READY)
    foreign_before = store.get_raw_row("foreign-ready")
    selected = []

    def build(rows, **kwargs):
        selected.extend(row["id"] for row in rows)
        return "fixture\n", [{"record_id": "fixture"}], []

    monkeypatch.setenv("GOLD_BAA_ON_FILE", "1")
    monkeypatch.setattr(gold.gold_export, "build_export", build)
    monkeypatch.setattr(gold.gold_export, "dataset_card_md", lambda _: "fixture")
    monkeypatch.setattr(gold.gold_export, "croissant_json", lambda _: {})
    assert world.post("/api/gold/export", headers=operator,
                      json={"visit_ids": ["foreign-ready"]}).status_code == 400
    response = world.post("/api/gold/export", headers=operator, json={})
    assert response.status_code == 200, response.text
    assert selected == ["own-ready"]
    assert store.get_raw_row("foreign-ready") == foreign_before


def test_declined_counts_stay_personal_without_assigning_legacy_ownership(world):
    owner = headers(landing_token(email="owner@example.org"))
    other = headers(landing_token(email="other@example.org"))
    store.record_declined(None)  # legacy unknown owner is preserved, not shared
    for head in (owner, other):
        vid = world.post("/api/gold/visits", headers=head, json={}).json()["id"]
        assert world.post(f"/api/gold/visits/{vid}/consent", headers=head,
                          json={"consent_given": False}).status_code == 200
        assert world.get("/api/gold/stats", headers=head).json()["totals"]["declined"] == 1
    assert store.declined_count(None, True) == 3


def test_same_visit_id_has_separate_database_streams_and_audio_in_each_realm(world):
    sessions, audio_paths = {}, {}
    for name in ("live", "sandbox"):
        with realm.scoped(name):
            sessions[name] = headers(tenant_token(health_system_id="hospital-a"))
            visit("same-id", owner="tenant:surgeon@hs.com", tenant="hospital-a", status=store.ST_CAPTURING)
            store.update_visit("same-id", transcript=f"Synthetic {name} discussion")
            store.new_queue("same-id")
            store.emit("same-id", "status", {"fixture": name})
    for name in ("live", "sandbox"):
        detail = world.get("/api/gold/visits/same-id", headers=sessions[name])
        assert detail.status_code == 200, detail.text
        assert detail.json()["transcript"] == f"Synthetic {name} discussion"
        uploaded = world.post("/api/gold/visits/same-id/audio", headers=sessions[name],
                              files={"file": ("fixture.webm", name.encode(), "audio/webm")})
        assert uploaded.status_code == 202, uploaded.text
        with realm.scoped(name):
            audio_paths[name] = store.get_visit("same-id")["audio_path"]
            store.emit("same-id", "status", {"fixture": name})
            assert list(store.get_ring("same-id"))[0]["data"] == {"fixture": name}
            assert store.get_queue("same-id").get_nowait()["data"] == {"fixture": name}
    assert audio_paths["live"] != audio_paths["sandbox"]
    assert all(Path(path).is_file() for path in audio_paths.values())
    with realm.scoped("sandbox"):
        store.finalize_stream("same-id")
        store.drop_streams("same-id")
        assert store.get_queue("same-id") is None
    with realm.scoped("live"):
        assert store.get_queue("same-id") is not None
        assert list(store.get_ring("same-id"))[0]["data"] == {"fixture": "live"}


def test_oversized_audio_stops_before_storage_and_pipeline(world):
    visit("oversized", owner="tenant:surgeon@hs.com", tenant="hospital-a", status=store.ST_CAPTURING)
    before = store.get_raw_row("oversized")

    class Oversized:
        content_type = "audio/webm"
        consumed = 0

        async def read(self, size=-1):
            assert 0 < size <= 64 * 1024
            self.consumed += size
            return b"x" * size

    upload = Oversized()
    staff = StaffContext("tenant", "surgeon@hs.com", "Synthetic", "surgeon", "hospital-a", "synthetic", "")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(gold.upload_audio("oversized", Request({"type": "http"}), file=upload,
                                     difficulty_tags=None, languages=None, staff=staff))
    assert exc.value.status_code == 413 and exc.value.detail == "Audio exceeds 200 MB limit"
    assert upload.consumed == gold.MAX_AUDIO_BYTES + 1
    assert store.get_raw_row("oversized") == before
    assert not gold.GOLD_DIR.exists()
