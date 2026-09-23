"""Synthetic consent/intake authorization; rejected calls preserve complete stores."""
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

import main
import patient_session
from scripts.data_inventory import compare, snapshot
from team_store import TeamStore
from tests._role_auth import tenant_token


@pytest.fixture
def intake(monkeypatch, tmp_path):
    store = TeamStore(str(tmp_path / "team.db"))
    monkeypatch.setenv("TEAM_DB_PATH", store.db_path)
    monkeypatch.setenv("ENFORCE_PATIENT_AUTH", "1")
    patients = {pid: {"id": pid, "name": "Synthetic patient", "health_system_id": hs}
                for pid, hs in (("own-patient", "own-hs"), ("foreign-patient", "foreign-hs"))}
    monkeypatch.setattr(main, "_team_store", store)
    monkeypatch.setattr(main, "_patient_store", patients)
    monkeypatch.setattr(main.app.state, "team_store", store)
    monkeypatch.setattr(main.app.state, "patient_store", patients)
    for pid in (*patients, "orphan-patient"):
        store.ensure_episode(patient_id=pid)
        store.create_intake_form(
            intake_form_id=f"form-{pid}", patient_id=pid, surgery_id=None,
            status="INTERVIEW_COMPLETE", form_data={
                "section2_surgicalInfo": {"surgicalSite": {"value": "original", "source": "patient"}},
                "section1_demographics": {"name": {"value": "Synthetic patient", "source": "patient"}},
            })
        store.create_intake_form_edit(
            edit_id=f"edit-{pid}", intake_form_id=f"form-{pid}", edited_by="PATIENT",
            section_name="section2_surgicalInfo", field_key="surgicalSite",
            previous_value="", new_value="original")
        store.create_intake_notification(
            notification_id=f"notification-{pid}", doctor_id="tenant:own@example.invalid",
            intake_form_id=f"form-{pid}", notification_type="FORM_COMPLETED",
            message="Synthetic notification")
        store.create_care_team_message(
            patient_id=pid, sender_type="PATIENT", body="Synthetic reply",
            health_system_id=(patients.get(pid) or {}).get("health_system_id"))
    escalation_id = store.create_escalation(
        patient_id="own-patient", tier=2, trigger_type="synthetic",
        message="Synthetic escalation", conversation_snapshot=[])
    # Session resolution lazily creates this table. Include it in the frozen
    # before inventory so comparison detects only unauthorized application writes.
    with store._conn() as conn:
        patient_session._ensure_table(conn)
    client = TestClient(main.app)
    yield store, client, escalation_id
    client.close()


def _authenticate(client, actor):
    if actor in ("patient", "other-patient"):
        pid = "own-patient" if actor == "patient" else "foreign-patient"
        hs = "own-hs" if actor == "patient" else "foreign-hs"
        client.cookies.set("pt_session", patient_session.create_patient_session(pid, hs))
    elif actor in ("staff", "foreign-staff"):
        hs = "own-hs" if actor == "staff" else "foreign-hs"
        email = "own@example.invalid" if actor == "staff" else "foreign@example.invalid"
        client.headers["Authorization"] = "Bearer " + tenant_token(
            email=email, health_system_id=hs)


def _mutation(client, escalation_id, action):
    if action == "consent":
        return client.post("/api/escalations/consent", json={"escalation_id": escalation_id, "consent": "yes"})
    if action == "patch":
        return client.patch("/api/intake-forms/form-own-patient", json={
            "section": "section2_surgicalInfo", "field": "surgicalSite", "value": "updated"})
    return client.post("/api/intake-forms/form-own-patient/submit", json={})


@pytest.mark.parametrize("actor", ["anonymous", "other-patient", "foreign-staff"])
@pytest.mark.parametrize("action", ["consent", "patch", "submit"])
def test_wrong_principal_cannot_mutate_intake_or_consent(intake, actor, action):
    store, client, escalation_id = intake
    _authenticate(client, actor)
    before = snapshot(store.db_path)
    patients = deepcopy(main._patient_store)
    response = _mutation(client, escalation_id, action)
    assert response.status_code in (403, 404)
    assert compare(before, snapshot(store.db_path)) == []
    assert main._patient_store == patients


@pytest.mark.parametrize("actor", ["anonymous", "other-patient", "foreign-staff", "patient", "staff"])
@pytest.mark.parametrize("suffix", ["", "/edit-history"])
def test_orphan_form_does_not_bypass_read_authorization(intake, actor, suffix):
    store, client, _ = intake
    _authenticate(client, actor)
    before = snapshot(store.db_path)
    assert client.get(f"/api/intake-forms/form-orphan-patient{suffix}").status_code == 404
    assert compare(before, snapshot(store.db_path)) == []


@pytest.mark.parametrize("actor", ["patient", "staff"])
def test_bound_patient_and_scoped_staff_keep_consent_and_edit_flow(intake, actor):
    store, client, escalation_id = intake
    _authenticate(client, actor)
    assert client.get("/api/intake-forms/form-own-patient").status_code == 200
    assert client.get("/api/intake-forms/form-own-patient/edit-history").status_code == 200
    assert _mutation(client, escalation_id, "consent").status_code == 200
    assert _mutation(client, escalation_id, "patch").status_code == 200
    assert store.get_escalation(escalation_id)["consent"] == "yes"
    edits = store.list_intake_form_edits("form-own-patient")
    assert len(edits) == 2
    assert any(row["id"] == "edit-own-patient" and row["new_value"] == "original" for row in edits)
    assert any(row["previous_value"] == "original" and row["new_value"] == "updated" for row in edits)
    assert _mutation(client, escalation_id, "submit").status_code == (200 if actor == "patient" else 403)


def test_staff_field_restriction_remains(intake):
    store, client, _ = intake
    _authenticate(client, "staff")
    before = snapshot(store.db_path)
    response = client.patch("/api/intake-forms/form-own-patient", json={
        "section": "section1_demographics", "field": "name", "value": "changed"})
    assert response.status_code == 403
    assert compare(before, snapshot(store.db_path)) == []


@pytest.mark.parametrize("actor", ["anonymous", "patient", "foreign-staff"])
def test_notification_identity_and_patient_scope_are_required(intake, actor):
    store, client, _ = intake
    _authenticate(client, actor)
    before = snapshot(store.db_path)
    for doctor_id in ("me", "tenant:own@example.invalid", "doctor:default"):
        response = client.get(f"/api/doctors/{doctor_id}/notifications")
        if response.status_code == 200:
            assert {row["id"] for row in response.json()["notifications"]}.isdisjoint(
                {"notification-own-patient", "ctm-own-patient"})
        else:
            assert response.status_code in (401, 404)
        for notification_id in ("notification-own-patient", "ctm-own-patient"):
            response = client.patch(f"/api/doctors/{doctor_id}/notifications/{notification_id}/read")
            assert response.status_code in (401, 404)
    assert compare(before, snapshot(store.db_path)) == []


def test_notifications_scope_even_same_email_and_preserve_valid_flow(intake):
    store, client, _ = intake
    _authenticate(client, "staff")
    response = client.get("/api/doctors/me/notifications")
    assert response.status_code == 200
    ids = {row["id"] for row in response.json()["notifications"]}
    assert "notification-own-patient" in ids
    assert "notification-foreign-patient" not in ids
    assert "notification-orphan-patient" not in ids
    before = snapshot(store.db_path)
    for notification_id in ("notification-foreign-patient", "notification-orphan-patient",
                            "ctm-foreign-patient", "ctm-orphan-patient"):
        assert client.patch(f"/api/doctors/me/notifications/{notification_id}/read").status_code == 404
    assert compare(before, snapshot(store.db_path)) == []
    for notification_id in ("notification-own-patient", "ctm-own-patient"):
        assert client.patch(f"/api/doctors/me/notifications/{notification_id}/read").status_code == 200
    assert store.list_care_team_messages("own-patient")[0]["read_by_care_team"]
