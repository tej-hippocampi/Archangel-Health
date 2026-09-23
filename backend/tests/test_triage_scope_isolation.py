"""Synthetic exact-ownership checks for the clinical triage routers."""
import copy

import pytest

import main
from scripts.data_inventory import compare, snapshot
from tests.test_legacy_processing_scope import isolated, headers, seed
from tests.test_initial_tier_router import _tier_input


def request_for(kind):
    path = "/api/episodes/synthetic-owned/"
    return {
        "initial": (path + "initial-tier", {"input": _tier_input()}),
        "intraop": (path + "intraop-form", {}),
        "preop": (path + "preop-retier/run", {}),
    }[kind]


@pytest.mark.parametrize("kind", ["initial", "intraop", "preop"])
@pytest.mark.parametrize("scope", ["foreign", "landing", "missing-tenant", "orphan"])
def test_other_or_unknown_ownership_never_reads_or_mutates(isolated, kind, scope):
    client, patients, team = isolated
    seed(patients, owner=None if scope == "orphan" else "hs-alpha")
    before = copy.deepcopy(dict(patients))
    before_db = snapshot(team.db_path)
    auth = headers("" if scope == "missing-tenant" else
                   "hs-beta" if scope == "foreign" else "hs-alpha",
                   landing=scope == "landing")
    path, body = request_for(kind)
    response = client.post(path, json=body, headers=auth)
    assert response.status_code == 404
    assert dict(patients) == before
    assert compare(before_db, snapshot(team.db_path)) == []


@pytest.mark.parametrize("kind", ["initial", "intraop", "preop"])
@pytest.mark.parametrize("landing", [False, True])
def test_owned_tenant_and_demo_workflows_remain_available(isolated, kind, landing):
    client, patients, _ = isolated
    seed(patients, owner=main.DEMO_HEALTH_SYSTEM_ID if landing else "hs-alpha")
    patients["synthetic-owned"].update({"phase": "pre_op", "initial_tier": "TIER_1"})
    path, body = request_for(kind)
    response = client.post(path, json=body, headers=headers(landing=landing))
    assert response.status_code == 200, response.text


def test_intraop_list_filters_orphans_and_other_owners(isolated):
    client, patients, team = isolated
    for pid, owner in [("own", "hs-alpha"), ("other", "hs-beta"),
                        ("demo", main.DEMO_HEALTH_SYSTEM_ID), ("unknown", None)]:
        seed(patients, pid, owner)
        team.get_or_create_intraop_form(patient_id=pid)
    team.get_or_create_intraop_form(patient_id="missing-patient")
    path = "/api/intraop-forms?status=NEW"
    own = client.get(path, headers=headers()).json()["items"]
    demo = client.get(path, headers=headers(landing=True)).json()["items"]
    assert [row["patientId"] for row in own] == ["own"]
    assert [row["patientId"] for row in demo] == ["demo"]
    assert client.get(path, headers=headers("")).json()["items"] == []


def test_only_verified_admin_can_bypass_patient_scope_for_reopen(isolated, monkeypatch):
    client, patients, team = isolated
    seed(patients)
    team.get_or_create_intraop_form(patient_id="synthetic-owned")
    assert team.mark_intraop_form_ready_for_review(patient_id="synthetic-owned", rn_user_id="synthetic-rn")
    assert team.lock_intraop_form(patient_id="synthetic-owned", surgeon_user_id="prior@example.org")
    monkeypatch.setenv("ADMIN_AUTH_TOKEN", "synthetic-admin-only")
    path = "/api/episodes/synthetic-owned/intraop-form/reopen"
    before = snapshot(team.db_path)
    assert client.post(path, headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert compare(before, snapshot(team.db_path)) == []
    response = client.post(path, headers={"X-Admin-Token": "synthetic-admin-only"})
    assert response.status_code == 200, response.text
    assert team.get_intraop_form("synthetic-owned")["status"] == "REOPENED"
