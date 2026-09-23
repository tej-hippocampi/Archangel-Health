"""Synthetic batch MBI matches cannot merge another owner or cancelled source."""
import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

import realm
from eligibility import pipeline, store
from routers import eligibility
from tests.test_eligibility_scope_isolation import world


IDENTITY = {"mbi": "SYNTHETIC-MBI", "firstName": "Synthetic", "lastName": "Fixture",
            "confidence": "HIGH"}


@pytest.mark.parametrize("realm_name", ["live", "sandbox"])
@pytest.mark.parametrize("original_owner,archived", [("hospital-b", False), (None, False),
                                                    ("hospital-a", True)])
def test_batch_keeps_foreign_unknown_and_archived_sources_unchanged(world, tmp_path, realm_name,
                                                                  original_owner, archived):
    _, patients = world
    app = SimpleNamespace(state=SimpleNamespace(patient_store=patients))
    with realm.scoped(realm_name):
        patients["retained-patient"] = {
            "name": "Synthetic accepted original", "health_system_id": original_owner,
            "structured_data": {"mbi": IDENTITY["mbi"], "pre_op_instructions": "Retain original instructions"},
            "relevant_files": ["retained-doc"],
        }
        if archived:
            patients["retained-patient"]["archived_at"] = "2026-01-01T00:00:00"
        original_path = tmp_path / "accepted-original.txt"
        original_path.write_bytes(b"Synthetic accepted source bytes")
        store.save_doc("retained-doc", {"id": "retained-doc", "patient_id": "retained-patient",
                                        "path": str(original_path), "filename": "accepted-original.txt"})
        patient_before = copy.deepcopy(patients["retained-patient"])
        doc_before = copy.deepcopy(store.get_doc("retained-doc", include_archived=True))
        batch = {"id": "synthetic-batch", "created": [], "needs_review": [], "errors": [],
                 "queue": store.new_check_queue()}
        asyncio.run(pipeline._register_one_segment_and_enqueue(
            filename="new-synthetic.txt", fmt="OTHER", content=b"New synthetic source", slice_text="Synthetic",
            identity=IDENTITY, pre_op_instructions="New synthetic instructions", hs_id="hospital-a",
            actor="tenant:synthetic@example.org", app=app, batch_rec=batch))
        assert len(batch["created"]) == 1
        created = batch["created"][0]
        assert not created["merged"] and created["patientId"] != "retained-patient"
        patient = patients[created["patientId"]]
        assert patient["health_system_id"] == "hospital-a"
        assert patient["structured_data"]["pre_op_instructions"] == "New synthetic instructions"
        doc = store.get_doc(patient["relevant_files"][0])
        assert doc["patient_id"] == created["patientId"]
        assert Path(doc["path"]).is_relative_to(eligibility.upload_root())
        assert Path(doc["path"]).read_bytes() == b"New synthetic source"
        assert patients["retained-patient"] == patient_before
        assert store.get_doc("retained-doc", include_archived=True) == doc_before
        assert original_path.read_bytes() == b"Synthetic accepted source bytes"
        if realm_name == "sandbox":
            assert Path(doc["path"]).is_relative_to(eligibility.UPLOAD_DIR / "sandbox")
        else:
            assert not Path(doc["path"]).is_relative_to(eligibility.UPLOAD_DIR / "sandbox")


def test_same_owner_active_match_still_merges_after_foreign_and_cancelled_matches():
    patients = {
        "foreign": {"health_system_id": "hospital-b", "mbi": IDENTITY["mbi"]},
        "cancelled": {"health_system_id": "hospital-a", "mbi": IDENTITY["mbi"], "archived_at": "retained"},
        "own": {"health_system_id": "hospital-a", "mbi": IDENTITY["mbi"]},
    }
    before = copy.deepcopy(patients)
    assert pipeline._create_or_merge_patient(patients, IDENTITY, "hospital-a") == ("own", True)
    assert patients == before


@pytest.mark.parametrize("owner", [None, "", " "])
def test_batch_without_owner_cannot_create_or_merge(owner):
    patients = {"unknown": {"health_system_id": None, "mbi": IDENTITY["mbi"]}}
    before = copy.deepcopy(patients)
    with pytest.raises(ValueError, match="ownership"):
        pipeline._create_or_merge_patient(patients, IDENTITY, owner)
    assert patients == before
