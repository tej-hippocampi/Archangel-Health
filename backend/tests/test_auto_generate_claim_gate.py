"""The durable worker cannot implicitly arm an upload that was never approved."""
import asyncio

from asclepius import auto_generate, background_jobs
from scripts.data_inventory import compare, snapshot
from tests import _asclepius as A
from tests.test_auto_generate_on_arrival import _upload


def test_direct_worker_does_not_generate_or_claim_an_unarmed_upload(monkeypatch):
    store = A.fresh_store()
    uid = _upload(store, purpose="task_creation", mode="longitudinal", armed=False)
    store.insert_ingest_case(upload_id=uid, patient_key="synthetic-unarmed-case",
                             specialty="nephrology", status="ingested",
                             case={"case_source": "real_deid", "synthetic": True}, report={})
    before = snapshot(store.db_path)

    async def unexpected_generation(*args, **kwargs):
        raise AssertionError("An unarmed upload must not reach generation")

    monkeypatch.setattr(auto_generate, "_generate_case", unexpected_generation)
    assert asyncio.run(auto_generate.run_upload(store, uid, "admin-test")) == {}
    assert background_jobs.get(store, "auto_upload", uid) is None
    upload = store.get_ingest_upload(uid)
    assert not upload["auto_generate"]
    assert upload["auto_generate_started_at"] is None
    assert compare(before, snapshot(store.db_path)) == []
