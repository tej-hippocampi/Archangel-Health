"""Tenant staff may inspect only audit rows owned by their persisted episodes."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.tenant_portal import router
from team_store import TeamStore
from tenant_jwt import create_tenant_staff_token


@pytest.fixture
def tenant_audit(monkeypatch, tmp_path):
    monkeypatch.setenv("TEAM_DB_PATH", str(tmp_path / "team.db"))
    store = TeamStore(str(tmp_path / "team.db"))
    app = FastAPI()
    app.state.team_store = store
    app.state.patient_store = {}
    app.include_router(router)
    reports = {}
    for patient, tenant, verdict in (
        ("alice", "hospital-a", "PASS"),
        ("bob", "hospital-b", "BLOCK"),
        ("unowned", None, "REVIEW"),
        ("orphan", None, "REVIEW"),
    ):
        if patient != "orphan":
            store.ensure_episode(patient_id=patient, health_system_id=tenant)
        app.state.patient_store[patient] = {"health_system_id": tenant, "name": f"Synthetic {patient}"}
        reports[patient] = store.save_grounding_report(
            patient_id=patient, track="preop", report={"verdict": verdict, "summary": f"Private {patient}"},
            accuracy={}, script=f"Clinical content for {patient}",
        )
        store.log_event(patient_id=patient, event_type="llm_call", payload={
            "role": "writer", "model": f"model-{patient}", "usage": {"input": 10, "output": 5},
        })
    store.log_event(event_type="llm_call", payload={"role": "writer", "model": "global-event"})

    def headers(tenant="hospital-a", slug=None):
        token = create_tenant_staff_token(email=f"nurse@{tenant or 'missing'}.example", name="Nurse",
            role="rn_coordinator", health_system_id=tenant, tenant_slug=slug or tenant,
            health_system_code="AUDIT")
        return {"Authorization": f"Bearer {token}"}

    with TestClient(app) as client:
        yield client, store, app, reports, headers


@pytest.mark.parametrize("tenant,patient", [("hospital-a", "alice"), ("hospital-b", "bob")])
def test_audit_lists_hide_foreign_and_unowned_patients(tenant_audit, tenant, patient):
    client, _store, _app, _reports, headers = tenant_audit
    for endpoint, key in (("grounding/reports", "reports"), ("ai-calls", "calls")):
        response = client.get(f"/api/tenant/{tenant}/{endpoint}", headers=headers(tenant))
        assert response.status_code == 200
        rows = response.json()[key]
        assert [r["patient_id"] for r in rows] == [patient]
        assert rows[0]["patient_name"] == f"Synthetic {patient}"


def test_report_ids_cannot_bypass_tenant_ownership(tenant_audit):
    client, _store, _app, reports, headers = tenant_audit
    own = client.get(f"/api/tenant/hospital-a/grounding/reports/{reports['alice']}", headers=headers())
    assert own.status_code == 200
    assert own.json()["script_excerpt"] == "Clinical content for alice"
    for report_id in (reports["bob"], reports["orphan"], reports["unowned"], 999_999):
        denied = client.get(f"/api/tenant/hospital-a/grounding/reports/{report_id}", headers=headers())
        assert denied.status_code == 404
        assert denied.json() == {"detail": "Report not found"}


def test_tenant_aggregates_exclude_other_tenants_and_global_events(tenant_audit):
    client, store, _app, _reports, headers = tenant_audit
    stats = client.get("/api/tenant/hospital-a/grounding/stats", headers=headers()).json()
    assert (stats["total"], stats["pass"], stats["block"], stats["review"]) == (1, 1, 0, 0)
    calls = client.get("/api/tenant/hospital-a/ai-calls/stats", headers=headers()).json()
    assert calls["total_calls"] == 1
    assert calls["total_input_tokens"] == 10
    assert calls["models_in_use"] == ["model-alice"]
    # Internal administrator queries retain their explicit global view.
    assert store.grounding_summary_stats()["total"] == 4
    assert store.llm_call_stats()["total_calls"] == 5


def test_tenant_filter_applies_before_limits_and_survives_empty_cache(tenant_audit):
    client, store, app, _reports, headers = tenant_audit
    app.state.patient_store = {}
    # Newer foreign rows must not consume the tenant's limit.
    for _ in range(6):
        store.log_event(patient_id="bob", event_type="llm_call", occurred_at="2099-01-01T00:00:00",
                        payload={"role": "writer"})
    with store._conn() as conn:
        conn.execute("UPDATE grounding_check_reports SET created_at='2099-01-01T00:00:00' WHERE patient_id='bob'")
    for endpoint, key in (("grounding/reports", "reports"), ("ai-calls", "calls")):
        result = client.get(f"/api/tenant/hospital-a/{endpoint}?limit=1", headers=headers()).json()
        assert [row["patient_id"] for row in result[key]] == ["alice"]


def test_tenant_identity_must_be_present_and_match_the_route(tenant_audit):
    client, _store, _app, _reports, headers = tenant_audit
    assert client.get("/api/tenant/hospital-a/grounding/reports", headers=headers("hospital-b")).status_code == 401
    for tenant_id in (None, ""):
        assert client.get("/api/tenant/hospital-a/grounding/reports",
                          headers=headers(tenant_id, "hospital-a")).status_code == 401
