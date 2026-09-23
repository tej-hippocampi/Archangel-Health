"""Synthetic interrupted-process/lease tests for acknowledged background work."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest
from fastapi import BackgroundTasks

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from asclepius import auto_generate, background_jobs as jobs, pipeline, real_case_jobs
from asclepius.schemas import GenerateRealCasesRequest, SubmissionIn
from asclepius.store import AsclepiusStore, reset_store_for_tests
from routers import asclepius as routes
from tests import _asclepius as A
from tests.test_auto_generate_on_arrival import _upload
from scripts.data_inventory import compare, snapshot


@pytest.fixture
def store():
    return A.fresh_store()


def seed(store):
    user = A.make_user(store)
    task = store.insert_task(prompt="Synthetic retained question", specialty="nephrology",
                             candidate_answers=[{"id": "a", "text": "First answer"},
                                                {"id": "b", "text": "Second answer"}])
    sid = "s-" + task["task_id"]
    store.insert_submission(submission_id=sid, task_id=task["task_id"], evaluator_id=user["id"],
                            verdict="A_better", chosen_id="a", rejected_id="b", confidence="high",
                            time_spent_sec=120, payload={"accepted_original": "Retain my complete annotation"},
                            annotator={}, dedupe_hash=None)
    return sid, task, user


PACKAGED = [{"type": "preference", "prompt": "Synthetic", "chosen": "Answer A", "rejected": "Answer B"},
            {"type": "sft", "prompt": "Synthetic", "completion": "Answer A"}]


def stub_pipeline(monkeypatch):
    monkeypatch.setattr(pipeline, "package_submission", lambda *a: PACKAGED)
    monkeypatch.setattr(pipeline, "validate_submission", lambda *a, **k: {"valid": True, "issues": []})
    monkeypatch.setattr(pipeline, "compute_and_store_agreement", lambda *a: None)
    monkeypatch.setattr(pipeline, "estimate_and_store_value", lambda *a: {})
    monkeypatch.setattr(pipeline, "_should_sample", lambda: False)
    async def critic(*a):
        return {"consistent": True, "grounding_ok": True}
    monkeypatch.setattr(pipeline, "run_critic", critic)
    monkeypatch.setattr(pipeline, "run_grounding_check", critic)


def ready(store, kind, ref_id):
    with store._conn() as conn:
        conn.execute("UPDATE background_work SET lease_until=0, next_attempt=0 WHERE kind=? AND ref_id=?",
                     (kind, ref_id))


def test_202_has_durable_receipt_before_memory_scheduler_runs(store, monkeypatch):
    stub_pipeline(monkeypatch)
    user = A.make_user(store)
    task = store.insert_task(prompt="Synthetic receipt fixture", specialty="nephrology")
    bg = BackgroundTasks()
    body = SubmissionIn(task_id=task["task_id"], verdict="A_better", confidence="high",
                        chosen_id="a", rejected_id="b", time_spent_sec=120)
    response = asyncio.run(routes.submit(body, bg, async_pipeline=True, user=user))
    assert response.status_code == 202
    sid = json.loads(response.body)["submission_id"]
    assert jobs.get(store, "submission", sid)["status"] == "queued"
    original = store.get_submission(sid)["payload"]
    # Discard BackgroundTasks and reopen only committed storage, as after restart.
    store = reset_store_for_tests(str(store.db_path))
    asyncio.run(jobs.recover_once(store, kind="submission"))
    assert store.get_submission(sid)["status"] == "export_ready"
    assert store.get_submission(sid)["payload"] == original
    assert len(store.records_for_submission(sid)) == 2
    asyncio.run(jobs.recover_once(store, kind="submission"))
    assert len(store.records_for_submission(sid)) == 2


def test_parallel_replays_only_one_worker_processes(store, monkeypatch):
    sid, task, user = seed(store)
    jobs.enqueue_submission(store, sid)
    calls = []
    async def process(fenced, *args):
        calls.append(1)
        await asyncio.sleep(.02)
        fenced.set_submission_pipeline_state(sid, "needs_qa")
        return {"submission_id": sid, "status": "needs_qa"}
    monkeypatch.setattr(routes, "_finalize_submission", process)
    async def race():
        return await asyncio.gather(*(jobs.run(store, "submission", sid) for _ in range(4)))
    results = asyncio.run(race())
    assert len(calls) == sum(r is not None for r in results) == 1
    assert jobs.get(store, "submission", sid)["attempts"] == 1


def test_expired_worker_cannot_mutate_after_replacement(store, monkeypatch):
    sid, _, _ = seed(store)
    jobs.enqueue_submission(store, sid)
    async def scenario():
        entered, resume = asyncio.Event(), asyncio.Event()
        calls = []
        async def process(fenced, *args):
            calls.append(1)
            if len(calls) == 1:
                entered.set()
                await resume.wait()
                fenced.update_submission(sid, status="rejected")
            else:
                fenced.set_submission_pipeline_state(sid, "export_ready")
            return {"submission_id": sid}
        monkeypatch.setattr(routes, "_finalize_submission", process)
        stale = asyncio.create_task(jobs.run(store, "submission", sid))
        await entered.wait()
        ready(store, "submission", sid)
        assert await jobs.run(store, "submission", sid)
        resume.set()
        await stale
    asyncio.run(scenario())
    assert store.get_submission(sid)["status"] == "export_ready"
    assert jobs.get(store, "submission", sid)["status"] == "completed"


@pytest.mark.parametrize("boundary", ["during_package", "after_package", "after_terminal"])
def test_process_death_preserves_ids_and_recovers_at_commit_boundaries(store, monkeypatch, boundary):
    stub_pipeline(monkeypatch)
    sid, _, _ = seed(store)
    jobs.enqueue_submission(store, sid)
    source_payload = store.get_submission(sid)["payload"]
    code = '''
import asyncio, json, os, sys
from asclepius.store import reset_store_for_tests, AsclepiusStore
from asclepius import background_jobs as jobs, pipeline as p
store=reset_store_for_tests(sys.argv[1]); sid=sys.argv[2]; boundary=sys.argv[3]
p.package_submission=lambda *a: json.loads(sys.argv[4])
p.validate_submission=lambda *a,**k: {"valid":True,"issues":[]}
p.compute_and_store_agreement=lambda *a: None
p.estimate_and_store_value=lambda *a: {}
p._should_sample=lambda: False
async def critic(*a): return {"consistent":True,"grounding_ok":True}
p.run_critic=critic; p.run_grounding_check=critic
if boundary=="during_package":
    original=AsclepiusStore.insert_record
    def crash(self,*a,**kw):
        original(self,*a,**kw); os._exit(68)
    AsclepiusStore.insert_record=crash
elif boundary=="after_package":
    original=AsclepiusStore.save_submission_package
    def crash(self,*a,**kw):
        original(self,*a,**kw); os._exit(68)
    AsclepiusStore.save_submission_package=crash
else:
    original=AsclepiusStore.set_submission_pipeline_state
    def crash(self,sid,status,**kw):
        result=original(self,sid,status,**kw)
        if status=="export_ready": os._exit(68)
        return result
    AsclepiusStore.set_submission_pipeline_state=crash
asyncio.run(jobs.run(store,"submission",sid))
'''
    run = subprocess.run([sys.executable, "-c", code, str(store.db_path), sid, boundary, json.dumps(PACKAGED)],
                         cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30)
    assert run.returncode == 68, run.stdout + run.stderr
    store = reset_store_for_tests(str(store.db_path))
    before = store.records_for_submission(sid)
    assert len(before) == (0 if boundary == "during_package" else 2)
    ready(store, "submission", sid)
    asyncio.run(jobs.recover_once(store, kind="submission"))
    after = store.records_for_submission(sid)
    assert len(after) == 2
    assert {r["record_id"] for r in before} <= {r["record_id"] for r in after}
    assert store.get_submission(sid)["payload"] == source_payload
    assert store.get_submission(sid)["status"] == "export_ready"
    assert {r["status"] for r in after} == {"export_ready"}
    assert jobs.get(store, "submission", sid)["status"] == "completed"


def test_legacy_partial_package_keeps_original_and_only_adds_missing_types(store):
    sid, task, _ = seed(store)
    original = {"type": "preference", "complete_legacy_evidence": "Never replace me"}
    rid = store.insert_record(submission_id=sid, task_id=task["task_id"], rtype="preference",
                              specialty="nephrology", payload=original)
    existing = store.records_for_submission(sid)[0]
    rows = store.save_submission_package(task, store.get_submission(sid), PACKAGED)
    assert len(rows) == 2
    assert next(r for r in rows if r["record_id"] == rid) == existing
    assert store.save_submission_package(task, store.get_submission(sid), PACKAGED) == rows


def test_storage_failure_during_phase_rolls_back_and_retries(store, monkeypatch):
    stub_pipeline(monkeypatch)
    sid, _, _ = seed(store)
    jobs.enqueue_submission(store, sid)
    with store._conn() as conn:
        conn.execute("CREATE TRIGGER stop_record_phase BEFORE UPDATE OF status ON records "
                     "BEGIN SELECT RAISE(ABORT, 'simulated disk failure'); END")
    asyncio.run(jobs.run(store, "submission", sid))
    assert store.get_submission(sid)["status"] == "submitted"
    assert {r["status"] for r in store.records_for_submission(sid)} == {"submitted"}
    assert jobs.get(store, "submission", sid)["status"] == "queued"
    with store._conn() as conn:
        conn.execute("DROP TRIGGER stop_record_phase")
    ready(store, "submission", sid)
    asyncio.run(jobs.recover_once(store, kind="submission"))
    assert store.get_submission(sid)["status"] == "export_ready"
    assert len(store.records_for_submission(sid)) == 2


def test_human_rejection_during_critic_is_preserved(store, monkeypatch):
    stub_pipeline(monkeypatch)
    sid, _, _ = seed(store)
    jobs.enqueue_submission(store, sid)
    async def reject(*args):
        pipeline.apply_qa_decision(store, store.get_submission(sid), decision="reject", reviewer_id="qa", notes="Retained")
        return {"consistent": True}
    monkeypatch.setattr(pipeline, "run_critic", reject)
    asyncio.run(jobs.run(store, "submission", sid))
    assert store.get_submission(sid)["status"] == "rejected"
    assert {r["status"] for r in store.records_for_submission(sid)} == {"rejected"}


def test_upload_claim_and_durable_receipt_are_atomic(store):
    uid = _upload(store, purpose="task_creation", mode="static", armed=True)
    before = snapshot(store.db_path)
    with store._conn() as conn:
        conn.execute("CREATE TRIGGER fail_job BEFORE INSERT ON background_work "
                     "BEGIN SELECT RAISE(ABORT, 'disk full'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.claim_auto_generate(uid, actor="admin")
    assert compare(before, snapshot(store.db_path)) == []
    assert store.get_ingest_upload(uid)["auto_generate_started_at"] is None


def test_dropped_upload_scheduler_is_recovered_with_frozen_mode(store, monkeypatch):
    uid = _upload(store, purpose="task_creation", mode="longitudinal", armed=True)
    cid = store.insert_ingest_case(upload_id=uid, patient_key="synthetic", specialty="hepatology",
                                  case={"case_source": "real_deid"}, status="ingested", report={})["ingest_case_id"]
    calls = []
    async def generate(store, uid, cid, body, actor):
        calls.append((body.trajectory, actor))
        return {"generated": 1, "details": {}}
    monkeypatch.setattr(auto_generate, "_generate_case", generate)
    auto_generate.maybe_start(store, uid, actor="original-admin", schedule=lambda *a: None)
    store.set_upload_task_mode(uid, "static")
    store = reset_store_for_tests(str(store.db_path))
    asyncio.run(jobs.recover_once(store, kind="auto_upload"))
    asyncio.run(jobs.recover_once(store, kind="auto_upload"))
    assert calls == [(True, "original-admin")]
    assert store.get_ingest_upload(uid)["auto_generate_report"]["generated"] == 1


def test_child_identity_survives_changed_specialty_and_parent_interruption(store, monkeypatch):
    uid = _upload(store, purpose="task_creation", mode="static", armed=True)
    cid = store.insert_ingest_case(upload_id=uid, patient_key="synthetic", specialty="hepatology",
                                  case={"case_source": "real_deid"}, status="ingested", report={})["ingest_case_id"]
    calls = []
    async def execute(cid, body, bg, admin, *, job):
        tid = job.task_id(0)
        calls.append(tid)
        if not store.get_task(tid):
            store.insert_task(task_id=tid, prompt="Synthetic generated", specialty="hepatology",
                              generation={"ingest_case_id": cid})
        return {"generated": 1, "details": {}}
    monkeypatch.setattr(routes, "_execute_real_case_generation", execute)
    original = jobs.LeaseStore.checkpoint
    def interrupted(self, **values):
        if values.get("report", {}).get("generated"):
            raise asyncio.CancelledError()
        return original(self, **values)
    with monkeypatch.context() as patch:
        patch.setattr(jobs.LeaseStore, "checkpoint", interrupted)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(auto_generate.run_upload(store, uid, "admin"))
    assert len(calls) == 1
    store.update_ingest_case(cid, specialty="nephrology")
    asyncio.run(jobs.recover_once(store, kind="auto_upload"))
    assert len(calls) == 1  # completed child result reused; no second model/task
    assert store.get_ingest_upload(uid)["auto_generate_report"]["generated"] == 1
    with store._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM real_case_generation_jobs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_legacy_work_is_adopted_and_partial_generation_is_preserved(store, monkeypatch):
    stub_pipeline(monkeypatch)
    sid, _, _ = seed(store)
    uid = _upload(store, purpose="task_creation", mode="static", armed=True)
    cid = store.insert_ingest_case(upload_id=uid, patient_key="synthetic", specialty="hepatology",
                                  case={"case_source": "real_deid"}, status="promoted", report={})["ingest_case_id"]
    task = store.insert_task(prompt="Existing accepted generation", generation={"ingest_case_id": cid})
    with store._conn() as conn:
        conn.execute("UPDATE submissions SET updated_at='2020-01-01T00:00:00' WHERE submission_id=?", (sid,))
        conn.execute("UPDATE ingest_uploads SET auto_generate_started_at='2020-01-01T00:00:00', "
                     "updated_at='2020-01-01T00:00:00' WHERE upload_id=?", (uid,))
    asyncio.run(jobs.recover_once(store))
    assert store.get_submission(sid)["status"] == "export_ready"
    assert store.get_task(task["task_id"]) == task
    report = store.get_ingest_upload(uid)["auto_generate_report"]
    assert report["cases_failed"] == 1 and "Preserved 1 existing task" in report["cases"][0]["error"]
    assert jobs.get(store, "auto_upload", uid)["status"] == "completed"


def test_model_failure_retry_keeps_first_evidence(store):
    sid, task, _ = seed(store)
    args = dict(task_id=task["task_id"], submission_id=sid, model="synthetic-model", verdict="A_better",
                error_tags=["first"], corrected_steps=[], expert_correction="accepted original", prompt="Synthetic")
    fid = store.insert_model_failure(**args)
    assert store.insert_model_failure(**{**args, "expert_correction": "regenerated"}) == fid
    assert len(store.list_model_failures()) == 1
    assert store.list_model_failures()[0]["expert_correction"] == "accepted original"


@pytest.mark.parametrize("boundary", ["after_task_insert", "child_checkpoint"])
def test_auto_generation_storage_failure_resumes_same_real_child(store, monkeypatch, boundary):
    import base64
    from tests.test_asclepius_longitudinal_e2e import build_chart, _ingest_chart, _stub_model_legs
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", base64.urlsafe_b64encode(b"synthetic-recovery-key-0123456789").decode())
    _stub_model_legs(monkeypatch)
    cid = _ingest_chart(store, build_chart())
    uid = store.get_ingest_case(cid)["upload_id"]
    store.set_upload_task_mode(uid, "longitudinal")
    store.set_upload_auto_generate(uid, True)
    original_generate = routes._generate_one_real_case
    original_checkpoint = real_case_jobs.Run.checkpoint
    calls, failed = [], []
    async def interrupted_generate(*args, **kwargs):
        result = await original_generate(*args, **kwargs)
        calls.append(args[2]["encounter_index"])
        if boundary == "after_task_insert" and result.get("task_id") and not failed:
            failed.append(1)
            raise sqlite3.OperationalError("transient database write failure")
        return result
    def interrupted_checkpoint(self, **values):
        if boundary == "child_checkpoint" and values.get("generated") and not failed:
            failed.append(1)
            raise OSError("transient storage unavailable")
        return original_checkpoint(self, **values)
    monkeypatch.setattr(routes, "_generate_one_real_case", interrupted_generate)
    monkeypatch.setattr(real_case_jobs.Run, "checkpoint", interrupted_checkpoint)
    asyncio.run(auto_generate.run_upload(store, uid, "admin"))
    assert failed, "fault must occur after a durable child task exists"
    assert jobs.get(store, "auto_upload", uid)["status"] == "queued"
    assert store.get_ingest_upload(uid)["auto_generate_report"] is None
    with store._conn() as conn:
        child = dict(conn.execute("SELECT * FROM real_case_generation_jobs").fetchone())
        ids = {r[0] for r in conn.execute("SELECT task_id FROM tasks")}
    assert ids and child["status"] == "queued"
    ready(store, "auto_upload", uid)
    asyncio.run(jobs.recover_once(store, kind="auto_upload"))
    report = store.get_ingest_upload(uid)["auto_generate_report"]
    assert report["generated"] == 5 and report["cases_failed"] == 0, report
    assert len(calls) == len(set(calls)) == 5  # inserted encounter never regenerated
    with store._conn() as conn:
        after_ids = {r[0] for r in conn.execute("SELECT task_id FROM tasks")}
        assert conn.execute("SELECT COUNT(*) FROM real_case_generation_jobs").fetchone()[0] == 1
    assert ids <= after_ids and len(after_ids) == 5
    assert jobs.get(store, "auto_upload", uid)["status"] == "completed"
