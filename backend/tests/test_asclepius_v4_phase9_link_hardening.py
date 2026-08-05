"""Asclepius V4 Build Spec — Phase 9: import-link hardening.

- 9.1 A structurally-unreadable upload is rejected with ACTIONABLE copy, never a
  "zip magic bytes" message a hospital IT team can't act on.
- 9.3 Terminal-state reconciliation catches cases that reached a terminal state but
  are internally inconsistent (unbound sealed key, missing asset blob).
- 9.4 The raw blob is retained past the window when every entry failed to parse.
Spec Phase 9 tests 35-37 / Audit §9.
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
_KEY = base64.urlsafe_b64encode(b"phase9-link-hardening-test-0032b").decode()


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


def _link():
    admin = A.headers_for(A.make_user(_store(), role="admin"))
    return client.post("/api/asclepius/admin/upload-links",
                       json={"partner_id": "mercy", "specialty": "nephrology",
                             "expires_hours": 24, "one_time": False},
                       headers=admin).json()


# ── test 35 ───────────────────────────────────────────────────────────────────
def test_bare_json_via_magic_link():
    """The real bundle, unzipped, through the magic link, reaches the adapter and
    ingests — a bare .json is wrapped, not rejected."""
    link = _link()
    r = client.post(f"/api/asclepius/partner/uploads?t={link['token']}",
                    files={"file": ("bundle.json", _FIXTURE.read_bytes(), "application/json")})
    assert r.status_code == 200, r.text
    res = r.json()
    st = client.get(f"/api/asclepius/partner/uploads/{res['upload_id']}?t={link['token']}")
    assert st.json()["status"] == "ingested", st.json()


# ── test 36 ───────────────────────────────────────────────────────────────────
def test_partner_error_copy_is_actionable():
    """A structurally-unreadable upload (corrupt PK archive) is a 400 whose body tells
    the partner what to DO — never a 'magic bytes' message."""
    link = _link()
    corrupt = b"PK\x03\x04" + b"\x00" * 8 + b"garbage that is not a real zip central dir"
    r = client.post(f"/api/asclepius/partner/uploads?t={link['token']}",
                    files={"file": ("bundle.zip", corrupt, "application/zip")})
    assert r.status_code == 400, r.text
    detail = r.json()["detail"].lower()
    assert "magic" not in detail and "bytes" not in detail
    # It is the actionable copy: says what formats to send.
    assert ".zip" in detail or "secure transfer" in detail
    assert "could not read this upload" in detail


def test_unreadable_upload_reason_is_actionable_not_magic():
    """Even if a corrupt blob reaches the pipeline (e.g. via retry), the partner-facing
    REASON is the actionable copy, not the raw 'bad magic bytes' text."""
    store = _store()
    uid = store.new_upload_id()
    # A single PK-prefixed blob passes wrap_loose_files untouched, then fails to unpack.
    rp = I.store_raw(uid, b"PK\x03\x04corrupt-not-a-zip")
    store.insert_ingest_upload(upload_id=uid, link_id="L", partner_id="P", filename="b.zip",
                               sha256=I.sha256_hex(b"x"), size_bytes=10, raw_path=rp, source_ip=None)
    summary = I.process_upload(store, uid)
    assert summary["status"] == "rejected"
    reason = (store.get_ingest_upload(uid).get("reason") or "").lower()
    assert "magic" not in reason
    assert "could not read this upload" in reason


# ── 9.4 raw blob retained when every entry fails ──────────────────────────────
def test_raw_retained_when_all_entries_fail():
    """A bundle with no parseable content is rejected AND its raw blob is retained
    past the window (retain_raw=1), so it survives a purge for a later re-run."""
    store = _store()
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mystery.bin", b"\x00\x01\x02 not clinical content")
    uid = store.new_upload_id()
    rp = I.store_raw(uid, buf.getvalue())
    store.insert_ingest_upload(upload_id=uid, link_id="L", partner_id="P", filename="b.zip",
                               sha256=I.sha256_hex(buf.getvalue()), size_bytes=99,
                               raw_path=rp, source_ip=None)
    summary = I.process_upload(store, uid)
    assert summary["status"] == "rejected"
    retained = {u["upload_id"] for u in store.list_uploads_with_retained_raw()}
    assert uid in retained
    # A purge leaves the retained blob in place even if it is old.
    import os
    old = os.stat(rp).st_mtime - 10 ** 9
    os.utime(rp, (old, old))
    I.purge_expired_raw(store)
    assert os.path.exists(rp), "a retained raw blob must survive the retention purge"


# ── test 37 ───────────────────────────────────────────────────────────────────
def test_reconcile_flags_unbound_sealed_key():
    """An unbound sealed row older than an hour is caught by terminal-state
    reconciliation and reported as `sealed_key_unbound`."""
    store = _store()
    ref = store.stage_sealed_ground_truth(upload_id="u-old", patient_key="pk-old",
                                          payload={"answer_key": {"diagnosis": "PGNMID"}})
    # Backdate it >1h so the default (3600s) sweep picks it up.
    with store._conn() as conn:  # noqa: SLF001
        conn.execute("UPDATE sealed_ground_truth SET created_at = ? WHERE sealed_id = ?",
                     ("2020-01-01T00:00:00", ref))
    rep = I.reconcile_ingested_cases(store)
    # No matching case → reported as an orphan sealed key (still blocking).
    assert rep["sealed_orphans"] == 1, rep


def test_reconcile_holds_case_with_missing_asset(monkeypatch):
    """Terminal-state reconciliation holds an ingested case whose asset blob has since
    gone missing (asset_blob_missing, blocking)."""
    store = _store()
    from asclepius import assets as asc_assets
    monkeypatch.setattr(asc_assets, "verify_case_assets", lambda s: {
        "checked": 1, "ok": 0,
        "missing": [{"ingest_case_id": "ic-x", "reason": "asset_blob_missing",
                     "severity": "blocking", "detail": "blob gone"}],
        "corrupt": []})
    store.insert_ingest_case(upload_id="u1", patient_key="pk1", specialty="nephrology",
                             case={"case_source": "real_deid", "specialty": "nephrology"},
                             status="ingested", report={})
    # Point the hold at the real inserted case id.
    ic = store.list_ingest_cases(upload_id="u1")[0]
    monkeypatch.setattr(asc_assets, "verify_case_assets", lambda s: {
        "checked": 1, "ok": 0,
        "missing": [{"ingest_case_id": ic["ingest_case_id"], "reason": "asset_blob_missing",
                     "severity": "blocking", "detail": "blob gone"}],
        "corrupt": []})
    rep = I.reconcile_ingested_cases(store)
    assert rep["assets_missing"] == 1 and rep["cases_held"] >= 1
    refreshed = store.get_ingest_case(ic["ingest_case_id"])
    assert refreshed["status"] == "needs_review"
    assert any(r["reason"] == "asset_blob_missing" for r in (refreshed.get("review") or []))
