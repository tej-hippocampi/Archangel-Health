"""Synthetic applicant CV -> real TS prefill -> provisioning -> released cases.

No fixture cases or real model calls: these requests read the same immutable
86-case library shipped to production. Applicant records are isolated fixtures.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from asclepius import credentialing, onboarding_library as library, onboarding_cases as bank
from asclepius.onboarding_catalog import CURRICULUM
from asclepius.onboarding_specialties import canonical, resolve
from routers import onboarding
from tests._asclepius import app, fresh_store, make_user, headers_for
from tests.test_onboarding_specialty_cases import set_credentials


@pytest.fixture(scope="module")
def mapped_cvs():
    node = os.getenv("NODE_BINARY") or shutil.which("node")
    if not node or int(subprocess.check_output([node, "-p", 'process.versions.node.split(".")[0]'], text=True)) < 22:
        if os.getenv("CV_REQUIRE_CORPUS"):
            pytest.fail("Node 24 required for the production TypeScript mapper")
        pytest.skip("Node 24 required; enforced in CV extraction workflow")
    parses = []
    for name, _, _ in CURRICULUM:
        text = ("Example Physician, MD\nPrevious training\n"
                "Internal Medicine internship, 2001-2002\n"
                f"Current practice: {name}\nReferences\nReference Physician\n"
                "Board Certified, Nephrology - American Board of Internal Medicine")
        parsed = {**credentialing._parse_cv_text(text), "ok": True}
        assert parsed["specialty"] == canonical(name), (name, parsed)
        parses.append(parsed)
    mapper = Path(__file__).parent / "cv_corpus" / "map.cjs"
    forms = json.loads(subprocess.check_output([node, str(mapper)], input=json.dumps(parses), text=True))
    return {row[0]: (parsed, form) for row, parsed, form in zip(CURRICULUM, parses, forms)}


def released_library(monkeypatch):
    monkeypatch.setattr(library, "ROOT", Path(library.__file__).with_name("onboarding_material") / "cases")


@pytest.mark.parametrize("name", [row[0] for row in CURRICULUM])
def test_cv_to_provisioned_specialty_and_both_released_cases(name, mapped_cvs, monkeypatch):
    parsed, form = mapped_cvs[name]
    specialty = canonical(name)
    assert canonical(form["primarySpecialty"]) == specialty
    released_library(monkeypatch)
    store = fresh_store()
    monkeypatch.setattr(onboarding, "_asclepius_store", lambda request: store)
    credentials = {**form, "cvParsed": parsed}
    onboarding._provision_asclepius_user(
        SimpleNamespace(), email="synthetic@example.test", role="annotator",
        full_name="Example Physician", org_name="Example", specialty="nephrology",
        clinical_role="attending", credentials=credentials, attestations={}, verify=False)
    user = store.get_user_by_email("synthetic@example.test")
    store.set_verification_status(user["id"], "pending")
    assert user["specialty"] == specialty
    headers = headers_for(user)
    with TestClient(app) as client:
        tasks = []
        for kind, endpoint in (("practice", "tutorial"), ("examination", "exam")):
            response = client.get(f"/api/asclepius/{endpoint}/task", headers=headers)
            assert response.status_code == 200, (name, response.text)
            task = response.json()["task"]
            assert task["specialty"] == specialty
            assert task["case"]["specialty"] == specialty
            assert task["task_id"] == bank.task_id(specialty, kind)
            assert "ground_truth" not in task["case"]
            tasks.append(task["task_id"])
        assert tasks[0] != tasks[1]


def test_attributed_cv_recovers_legacy_profile_without_overwriting_confirmed_choice():
    cv = {"ok": True, "specialty_status": "resolved", "specialty": "dermatology", "specialty_display": "Dermatology"}
    user = {"specialty": "nephrology", "cv_parsed_json": json.dumps(cv)}
    assert resolve(user)["specialty"] == "dermatology"
    assert resolve({"specialty": "nephrology", "credentials": {"cvParsed": cv}})["specialty"] == "dermatology"
    user["credentials"] = {"primarySpecialty": "Pathology"}
    assert resolve(user)["specialty"] == "pathology"
    for cv in ({"specialty": None, "specialty_display": "Nephrology"},
               {"ok": False, "specialty": "nephrology"},
               {"specialty_status": "ambiguous", "specialty_display": "Nephrology"}):
        assert resolve({"cv_parsed": cv})["specialty"] == ""
    assert resolve({"cv_parsed": {"specialty": "pediatric nephrology", "specialty_display": "Nephrology"}})["specialty"] == "pediatric nephrology"


def test_unfinished_wrong_specialty_is_replaced_for_both_cases_and_originals_retained(monkeypatch):
    from scripts.data_inventory import snapshot, compare
    released_library(monkeypatch)
    store = fresh_store()
    user = make_user(store, specialty="nephrology")
    store.set_verification_status(user["id"], "pending")
    headers = headers_for(user)
    with TestClient(app) as client:
        old_practice = client.get("/api/asclepius/tutorial/task", headers=headers).json()["task"]["task_id"]
        old_exam = client.get("/api/asclepius/exam/task", headers=headers).json()["task"]["task_id"]
        set_credentials(store, user["id"], {"primarySpecialty": "Dermatology"})
        before = snapshot(store.db_path)
        practice = client.get("/api/asclepius/tutorial/task", headers=headers).json()["task"]
        exam = client.get("/api/asclepius/exam/task", headers=headers).json()["task"]
        assert practice["specialty"] == exam["specialty"] == "dermatology"
        assert practice["task_id"] != old_practice and exam["task_id"] != old_exam
        state = store.get_tutorial_state(user["id"])
        assert old_practice in json.dumps(state["previous_practice_tasks"])
        assert old_exam in json.dumps(state["previous_exam_draws"])
        assert compare(before, snapshot(store.db_path), allowed=["users.tutorial_json"]) == []
        assert bank.get_task(store, old_practice) is not None
        assert bank.get_task(store, old_exam) is not None
