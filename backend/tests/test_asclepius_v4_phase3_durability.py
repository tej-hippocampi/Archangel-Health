"""Asclepius V4 Build Spec — Phase 3: asset durability (CRITICAL, data loss).

Raw uploads fail closed (503) on non-durable storage; image assets only emitted a
once-per-process log warning and proceeded. Because study_findings_policy='hidden'
whenever a study has an image, losing the blob leaves a case with neither the image
nor the caption. Spec Phase 3 tests 14-17 / Audit §P2.
"""
from __future__ import annotations

import base64
import os
import shutil
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import assets as asc_assets  # noqa: E402
from asclepius import ingestion as asc_ingestion  # noqa: E402

client = TestClient(A.app)
_KEY = base64.urlsafe_b64encode(b"0" * 32).decode()


@pytest.fixture(autouse=True)
def _iso(monkeypatch, tmp_path):
    A.fresh_store()
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))
    monkeypatch.setenv("ASCLEPIUS_ASSET_STORE", str(tmp_path / "assets"))
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", _KEY)
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


# ── test 14 ───────────────────────────────────────────────────────────────────
def test_ephemeral_asset_store_refuses_upload(monkeypatch):
    """No durable ASCLEPIUS_ASSET_STORE → the upload returns 503, not a warning."""
    monkeypatch.setenv("ENV", "production")
    # Isolate the ASSET gate: the raw-storage gate passes so only the asset store
    # can 503. Point the asset store at ephemeral /dev/shm.
    monkeypatch.setattr(asc_ingestion, "ingest_storage_durable", lambda: (True, "ok"))
    monkeypatch.setenv("ASCLEPIUS_ASSET_STORE", "/dev/shm/asc_ephemeral_assets")
    admin = A.headers_for(A.make_user(_store(), role="admin"))
    link = client.post("/api/asclepius/admin/upload-links",
                       json={"partner_id": "mercy", "purpose": "task_creation", "specialty": "nephrology",
                             "expires_hours": 24, "one_time": True}, headers=admin).json()
    r = client.post(f"/api/asclepius/partner/uploads?t={link['token']}",
                    files={"file": ("b.json", b'{"resourceType":"Bundle"}', "application/json")})
    assert r.status_code == 503, r.text
    assert "asset store" in r.json()["detail"].lower()


# ── test 15 ───────────────────────────────────────────────────────────────────
def test_blob_survives_simulated_redeploy(monkeypatch, tmp_path):
    """Write assets to the configured (durable) store, delete the CODE TREE, blobs
    still resolve — they never lived under the code tree."""
    png = _png()
    stored = asc_assets.process_upload(png, "image/png")
    # Simulate a redeploy that wipes the package's fallback store dir.
    code_tree_store = Path(__file__).resolve().parents[1] / "asclepius" / "_assetstore"
    if code_tree_store.exists():
        shutil.rmtree(code_tree_store)
    data, mime = asc_assets.load_asset(stored, verify=True)
    assert asc_assets._sha256(data) == stored["sha256"]


# ── test 16 ───────────────────────────────────────────────────────────────────
def test_blob_readback_verification():
    """A write whose on-disk content does not hash to the expected sha256 raises
    AssetError (the read-back catches a corrupt/bad write)."""
    bogus_sha = "0" * 64
    with pytest.raises(asc_assets.AssetError):
        asc_assets._write_blob(bogus_sha, b"content that does not hash to all-zeros")
    # and the bad blob is not left addressable under the claimed sha
    assert not os.path.exists(asc_assets._blob_path(bogus_sha))


def test_durable_store_reports_durable(monkeypatch, tmp_path):
    monkeypatch.setenv("ASCLEPIUS_ASSET_STORE", str(tmp_path / "durable_assets"))
    (tmp_path / "durable_assets").mkdir()
    ok, _ = asc_assets.asset_storage_durable()
    # tmp_path is under /tmp → flagged ephemeral, which is correct.
    assert ok is False
    monkeypatch.setenv("ASCLEPIUS_ASSET_STORE", "s3://bucket/prefix")
    ok, why = asc_assets.asset_storage_durable()
    assert ok is True and "s3" in why


# ── test 17 ───────────────────────────────────────────────────────────────────
def test_missing_blob_raises_review_reason(monkeypatch, tmp_path):
    """Delete a stored blob → reconciliation reports asset_blob_missing (blocking)."""
    store = _store()
    # Insert a minimal V4 ingest case whose study references a real, then-deleted blob.
    png = _png()
    stored = asc_assets.process_upload(png, "image/png")
    case = {"case_source": "real_deid", "specialty": "nephrology",
            "studies": [{"modality": "pathology", "label": "img", "findings": "",
                         "asset": {"asset_id": stored["asset_id"], "sha256": stored["sha256"],
                                   "mime": "image/png"}}]}
    store.insert_ingest_case(upload_id="u1", patient_key="pk1", specialty="nephrology",
                             case=case, status="ingested", report={})
    # healthy first
    rep = asc_assets.verify_case_assets(store)
    assert rep["checked"] == 1 and rep["ok"] == 1 and not rep["missing"]
    # delete the blob → reconciliation flags it
    os.remove(asc_assets._blob_path(stored["sha256"]))
    rep2 = asc_assets.verify_case_assets(store)
    assert rep2["missing"] and rep2["missing"][0]["reason"] == "asset_blob_missing"
    assert rep2["missing"][0]["severity"] == "blocking"


def _png() -> bytes:
    import io
    from PIL import Image
    im = Image.new("RGB", (4, 4), (123, 200, 50))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()
