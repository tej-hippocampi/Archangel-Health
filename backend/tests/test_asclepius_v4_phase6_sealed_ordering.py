"""Asclepius V4 Build Spec — Phase 6: sealed-key ordering (H1).

The case was inserted as `ingested`, THEN the sealed answer key was stored, then
re-quarantined on failure. A crash between the two left an `ingested` case with no
answer key — and `recover_interrupted_uploads` only revisits non-terminal UPLOAD
statuses, so nothing ever returned to it. This phase stages the key first (keyed on
(upload_id, patient_key), NULL ingest_case_id), inserts the case, then binds — so the
only crash-window failure is a key on disk with no case, the strictly better one.
Spec Phase 6 tests 29-30 / Audit §H1.
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import ingestion as I  # noqa: E402

client = TestClient(A.app)

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "nephrology_pgnmid_bundle.json"
_KEY = base64.urlsafe_b64encode(b"phase6-sealed-ordering-test-032b").decode()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    A.fresh_store()
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))
    monkeypatch.setenv("ASCLEPIUS_ASSET_STORE", str(tmp_path / "assets"))
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", _KEY)
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


def _ingest(store, raw: bytes, *, specialty="nephrology", filename="bundle.json"):
    data = I.wrap_loose_files([{"filename": filename, "content": raw}], specialty=specialty)
    uid = store.new_upload_id()
    rp = I.store_raw(uid, data)
    store.insert_ingest_upload(upload_id=uid, link_id="L1", partner_id="P1", filename=filename,
                               sha256=I.sha256_hex(data), size_bytes=len(data),
                               raw_path=rp, source_ip=None)
    summary = I.process_upload(store, uid)
    summary["upload_id"] = uid
    return summary


def _sealed_rows(store, upload_id):
    with store._conn() as conn:  # noqa: SLF001
        return [dict(r) for r in conn.execute(
            "SELECT sealed_id, ingest_case_id, upload_id, patient_key FROM "
            "sealed_ground_truth WHERE upload_id = ?", (upload_id,)).fetchall()]


# ── happy path — key ends up BOUND ────────────────────────────────────────────
def test_sealed_key_bound_on_clean_ingest():
    store = _store()
    summary = _ingest(store, _FIXTURE.read_bytes())
    assert summary["status"] == "ingested", summary
    cases = store.list_ingest_cases(upload_id=summary["upload_id"])
    assert len(cases) == 1
    rows = _sealed_rows(store, summary["upload_id"])
    assert rows and rows[0]["ingest_case_id"] == cases[0]["ingest_case_id"]
    # And the adjudication surface can still read it back.
    assert store.get_sealed_ground_truth(cases[0]["ingest_case_id"]) is not None


# ── test 29 ───────────────────────────────────────────────────────────────────
def test_sealed_key_staged_before_case_row(monkeypatch):
    """Case insertion raises → NO orphan `ingested` row exists, and the key is present
    on disk, unbound (recoverable), never lost."""
    store = _store()

    def _boom(*a, **k):
        raise RuntimeError("simulated crash between staging and case insert")
    monkeypatch.setattr(store, "insert_ingest_case", _boom)

    summary = _ingest(store, _FIXTURE.read_bytes())
    # process_upload never raised, and no case row landed (ingested OR quarantined).
    assert summary["status"] in ("rejected", "quarantined"), summary
    assert store.list_ingest_cases(upload_id=summary["upload_id"]) == []
    # The staged key is on disk, keyed to (upload_id, patient_key), UNBOUND.
    rows = _sealed_rows(store, summary["upload_id"])
    assert rows, "the sealed key must be staged before the case row"
    assert rows[0]["ingest_case_id"] is None
    assert rows[0]["patient_key"]


# ── test 30 ───────────────────────────────────────────────────────────────────
def test_sealed_staging_failure_quarantines(monkeypatch):
    """Staging the key fails → the case quarantines (never ships ingested with a lost
    adjudication), and no row is ever `ingested`."""
    store = _store()

    def _boom(*a, **k):
        raise RuntimeError("simulated sealed-store failure")
    monkeypatch.setattr(store, "stage_sealed_ground_truth", _boom)

    summary = _ingest(store, _FIXTURE.read_bytes())
    assert summary["status"] == "quarantined", summary
    cases = store.list_ingest_cases(upload_id=summary["upload_id"])
    assert cases and all(c["status"] == "quarantined" for c in cases)
    assert summary["ingested"] == 0
    reason = (cases[0].get("report") or {}).get("quarantine_reason") or ""
    assert "sealed answer key could not be stored" in reason


# ── reconciliation — a key left unbound by a crash is re-bound + held ──────────
def test_reconcile_binds_unbound_key_and_holds_case():
    store = _store()
    # Stage a key with NO case (the crash-window state), then insert a matching case
    # as if the pre-Phase-6 path had landed it before the bind.
    ref = store.stage_sealed_ground_truth(upload_id="u9", patient_key="pk9",
                                          payload={"answer_key": {"diagnosis": "PGNMID"}})
    assert ref
    ic = store.insert_ingest_case(upload_id="u9", patient_key="pk9", specialty="nephrology",
                                  case={"case_source": "real_deid", "specialty": "nephrology"},
                                  status="ingested", report={})
    rep = store.reconcile_sealed_ground_truth(older_than_seconds=0)
    assert rep["bound"] == 1 and not rep["orphans"]
    # The key is now bound, and the recovered case is held for review (blocking).
    refreshed = store.get_ingest_case(ic["ingest_case_id"])
    assert refreshed["status"] == "needs_review"
    assert any(r["reason"] == "sealed_key_unbound" and r["severity"] == "blocking"
               for r in (refreshed.get("review") or []))
    assert store.get_sealed_ground_truth(ic["ingest_case_id"]) is not None


def test_reconcile_reports_orphan_key_without_case():
    store = _store()
    store.stage_sealed_ground_truth(upload_id="u404", patient_key="pkX",
                                    payload={"answer_key": {"diagnosis": "x"}})
    rep = store.reconcile_sealed_ground_truth(older_than_seconds=0)
    assert rep["bound"] == 0
    assert rep["orphans"] and rep["orphans"][0]["reason"] == "sealed_key_unbound"
    assert rep["orphans"][0]["severity"] == "blocking"
