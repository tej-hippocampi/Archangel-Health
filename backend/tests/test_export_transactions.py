"""Synthetic concurrency and interrupted-commit coverage for buyer/ledger state."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import os
import sqlite3
import subprocess
import sys
import threading

import pytest

from tests import _asclepius as A
from asclepius import export, payments, profiles
from scripts.data_inventory import compare, snapshot


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("ASCLEPIUS_EXPORT_DIR", str(tmp_path / "exports"))
    profiles.clear_cache()
    return A.fresh_store()


def seed(store, status="export_ready", earning_status=None):
    user = A.make_user(store)
    task = store.insert_task(prompt="Synthetic audit fixture", specialty="nephrology")
    sid = "s-" + task["task_id"]
    store.insert_submission(submission_id=sid, task_id=task["task_id"], evaluator_id=user["id"],
        verdict="A_better", chosen_id="a", rejected_id="b", confidence="high", time_spent_sec=60,
        payload={}, annotator={}, dedupe_hash=None, status=status)
    rid = store.insert_record(submission_id=sid, task_id=task["task_id"], rtype="preference",
        specialty="nephrology", status=status, payload={"type": "preference",
        "prompt": "Synthetic audit fixture", "chosen": "Fixture answer A", "rejected": "Fixture answer B",
        "annotator_credential": "board_certified_nephrology", "license": "archangel-commercial",
        "ip_cleared": True, "contains_phi": False, "submission_id": sid, "task_id": task["task_id"]})
    eid = "e-" + sid
    if earning_status:
        store.insert_earning(earning_id=eid, user_id=user["id"], kind="task", ref_id=sid,
            amount_cents=7500, rate_cents=7500, status=earning_status, accrued_at="2026-08-01T00:00:00")
    return sid, rid, eid


def build(store, buyer="lab-a.example"):
    return export.build_export(store, created_by="audit-fixture", include_exported=True,
        licensed_to=buyer, license_exclusivity="exclusive")


def test_concurrent_exclusive_builds_publish_exactly_one_buyer(store, monkeypatch):
    seed(store)
    barrier = threading.Barrier(2)
    commit = store.commit_export
    def ready_to_commit(**kw):
        barrier.wait(timeout=10)
        return commit(**kw)
    monkeypatch.setattr(store, "commit_export", ready_to_commit)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(build, store, buyer) for buyer in ("lab-a.example", "lab-b.example")]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result(timeout=30))
            except export.ExclusiveLicenseConflict:
                outcomes.append("conflict")
    assert outcomes.count("conflict") == 1
    assert len(store.list_exports()) == len(store.list_export_licenses(active_only=True)) == 1
    assert len(export.zip_export(store.list_exports()[0])) > 0


@pytest.mark.parametrize("boundary", ["insert_export", "mark_records_exported", "create_export_license"])
def test_export_commit_failure_preserves_all_prior_rows_and_files(store, monkeypatch, boundary):
    sid, _, _ = seed(store)
    root = export.export_root()
    original = root / "accepted-original.bin"
    original.write_bytes(b"retained synthetic source")
    before = snapshot(store.db_path, {"exports": root})
    operation = getattr(store, boundary)
    def interrupted(*args, **kwargs):
        operation(*args, **kwargs)
        raise OSError("injected failure after write")
    with monkeypatch.context() as patch:
        patch.setattr(store, boundary, interrupted)
        with pytest.raises(OSError):
            build(store)
    assert compare(before, snapshot(store.db_path, {"exports": root})) == []
    assert store.list_exports() == store.list_export_licenses() == []
    assert store.get_submission(sid)["status"] == "export_ready"
    assert len(store.list_records(status="export_ready")) == 1
    # A retry succeeds without manually restoring records or clearing a license.
    assert build(store)["record_count"] == 1
    assert original.read_bytes() == b"retained synthetic source"


def test_export_audit_write_failure_rolls_back_export_and_license(store):
    seed(store)
    before = snapshot(store.db_path)
    with store._conn() as conn:
        conn.execute("CREATE TRIGGER fail_export_audit BEFORE INSERT ON events "
                     "WHEN NEW.event_type = 'export_built' BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END")
    with pytest.raises(sqlite3.IntegrityError):
        build(store)
    assert compare(before, snapshot(store.db_path)) == []
    assert not store.list_exports()
    assert not store.list_export_licenses()


def test_export_file_flush_failure_never_publishes_receipt(store, monkeypatch):
    seed(store)
    before = snapshot(store.db_path)
    monkeypatch.setattr(export.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        build(store)
    assert compare(before, snapshot(store.db_path)) == []
    assert not store.list_exports()


@pytest.mark.parametrize("record_rejected", [False, True])
def test_rejection_during_packaging_cannot_be_overwritten_by_export(store, monkeypatch, record_rejected):
    sid, _, _ = seed(store)
    sync = export._sync_bundle
    def reject_before_commit(path):
        sync(path)
        store.update_submission(sid, status="rejected")
        if record_rejected:
            store.update_records_status_for_submission(sid, "rejected")
    monkeypatch.setattr(export, "_sync_bundle", reject_before_commit)
    with pytest.raises(ValueError, match="changed during packaging"):
        build(store)
    assert store.get_submission(sid)["status"] == "rejected"
    assert store.records_for_submission(sid)[0]["status"] == ("rejected" if record_rejected else "export_ready")
    assert not store.list_exports()


@pytest.mark.parametrize("boundary", ["update_submission", "update_records_status_for_submission"])
def test_manual_approval_failure_rolls_back_ledger_and_gate_then_retries(store, monkeypatch, boundary):
    sid, _, eid = seed(store, "needs_qa", "accrued")
    before = snapshot(store.db_path)
    operation = getattr(store, boundary)
    def interrupted(*args, **kwargs):
        operation(*args, **kwargs)
        raise OSError("injected gate interruption")
    with monkeypatch.context() as patch:
        patch.setattr(store, boundary, interrupted)
        with pytest.raises(OSError):
            payments.approve_earning(store, earning_id=eid, actor="audit")
    assert compare(before, snapshot(store.db_path)) == []
    assert store.get_earning_by_id(eid)["status"] == "accrued"
    assert payments.approve_earning(store, earning_id=eid, actor="audit")["gate"]["moved"]
    assert store.get_submission(sid)["status"] == store.records_for_submission(sid)[0]["status"] == "export_ready"


def test_auto_approval_failure_keeps_accrual_retryable(store, monkeypatch):
    sid, _, eid = seed(store, "needs_qa", "accrued")
    with monkeypatch.context() as patch:
        patch.setattr(store, "update_submission", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(OSError):
            payments._auto_approve(store, now=datetime(2026, 9, 22, tzinfo=timezone.utc))
    assert store.get_earning_by_id(eid)["status"] == "accrued"
    assert store.records_for_submission(sid)[0]["status"] == "needs_qa"
    assert payments._auto_approve(store, now=datetime(2026, 9, 22, tzinfo=timezone.utc)) == 1


def test_void_failure_preserves_approved_earning_and_export_gate(store, monkeypatch):
    sid, _, eid = seed(store, "export_ready", "approved")
    before = snapshot(store.db_path)
    with monkeypatch.context() as patch:
        patch.setattr(store, "update_submission", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(OSError):
            store.void_earning(eid, reason="fixture rejection", voided_by="audit", apply_record_gate=True)
    assert compare(before, snapshot(store.db_path)) == []
    assert store.void_earning(eid, reason="fixture rejection", voided_by="audit", apply_record_gate=True)["gate"]["moved"]
    assert store.get_submission(sid)["status"] == store.records_for_submission(sid)[0]["status"] == "rejected"


def test_gate_repairs_historical_split_without_downgrading_shipped_records(store):
    sid, _, _ = seed(store, "export_ready", "approved")
    store.update_records_status_for_submission(sid, "needs_qa")
    assert payments.apply_ledger_decision_to_records(store, submission_id=sid, decision="approve", reason="repair")["moved"]
    store.update_submission(sid, status="needs_qa")
    store.update_records_status_for_submission(sid, "exported")
    payments.apply_ledger_decision_to_records(store, submission_id=sid, decision="approve", reason="repair")
    assert store.records_for_submission(sid)[0]["status"] == "exported"


@pytest.mark.parametrize("existing_earning", [False, True])
def test_reviewer_approval_failure_rolls_back_new_and_existing_earnings(store, monkeypatch, existing_earning):
    sid, _, eid = seed(store, "needs_qa", "accrued" if existing_earning else None)
    sub = store.get_submission(sid)
    row = {"submission_id": sid, "task_id": sub["task_id"], "evaluator_id": sub["evaluator_id"],
           "user_id": sub["evaluator_id"], "created_at": sub["created_at"], "review_verdicts": "accept",
           "status": "accrued", "earning_id": eid, "rate_cents": 7500}
    monkeypatch.setattr(store, "unaccrued_submissions", lambda **kw: [] if existing_earning else [row])
    monkeypatch.setattr(store, "unresolved_task_earnings", lambda **kw: [row] if existing_earning else [])
    monkeypatch.setattr(payments, "_quality_terms", lambda *args: {
        "multiplier": 1, "proposed": False, "reasons": [], "version": "audit-fixture"})
    with monkeypatch.context() as patch:
        patch.setattr(store, "update_submission", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(OSError):
            payments.reconcile_task_accruals(store)
    earning = store.get_earning(kind="task", ref_id=sid)
    assert (earning["status"] == "accrued") if existing_earning else earning is None
    assert store.records_for_submission(sid)[0]["status"] == "needs_qa"


@pytest.mark.parametrize("after_commit", [False, True])
def test_process_death_at_export_commit_boundary(store, after_commit):
    sid, _, _ = seed(store)
    root = export.export_root()
    child = """
import os, sys
from asclepius import export
from asclepius.store import get_store
store = get_store()
name = 'commit_export' if sys.argv[1] == 'after' else 'create_export_license'
original = getattr(store, name)
def interrupted(*args, **kwargs):
    original(*args, **kwargs)
    os._exit(68)
setattr(store, name, interrupted)
export.build_export(store, created_by='audit', licensed_to='lab-a.example', license_exclusivity='exclusive')
"""
    env = {**os.environ, "ASCLEPIUS_DB_PATH": store.db_path, "ASCLEPIUS_EXPORT_DIR": str(root)}
    result = subprocess.run([sys.executable, "-c", child, "after" if after_commit else "before"],
        env=env, cwd=str(Path(__file__).resolve().parents[1]), capture_output=True, timeout=30)
    assert result.returncode == 68, result.stderr.decode()
    assert len(store.list_exports()) == len(store.list_export_licenses()) == int(after_commit)
    assert store.get_submission(sid)["status"] == ("exported" if after_commit else "export_ready")
    if after_commit:
        assert export.zip_export(store.list_exports()[0])
        with pytest.raises(export.ExclusiveLicenseConflict):
            build(store, "lab-b.example")
    else:
        assert build(store)["record_count"] == 1


@pytest.mark.parametrize('legacy_status', ['submitted', 'auto_validated', 'qa_checked'])
def test_approved_records_with_unchanged_legacy_submission_phase_can_export(store, legacy_status):
    sid, _, _ = seed(store)
    store.update_submission(sid, status=legacy_status)
    manifest = build(store)
    assert manifest['record_count'] == 1
    assert store.get_submission(sid)['status'] == 'exported'


@pytest.mark.parametrize('new_status', ['auto_validated', 'needs_qa', 'rejected'])
def test_legacy_export_rejects_submission_state_changes_during_packaging(store, monkeypatch, new_status):
    sid, _, _ = seed(store)
    store.update_submission(sid, status='submitted')
    sync = export._sync_bundle
    def change_phase(path):
        sync(path)
        store.update_submission(sid, status=new_status)
    monkeypatch.setattr(export, '_sync_bundle', change_phase)
    with pytest.raises(ValueError, match='changed during packaging'):
        build(store)
    assert store.get_submission(sid)['status'] == new_status
    assert not store.list_exports() and not store.list_export_licenses()
