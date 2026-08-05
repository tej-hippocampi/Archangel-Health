"""Storage durability — PRD I-0.

Every assertion here runs against a real path on a real filesystem. Mocking the
predicate would test that the mock returns what we told it to; the whole finding
in §F1 was that the predicate answered the *wrong question* while returning
perfectly correct values for the question it was actually asking.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests._asclepius as A  # noqa: E402
from asclepius import assets as asc_assets  # noqa: E402
from asclepius import constants as asc_constants  # noqa: E402
from asclepius import ingestion as asc_ingestion  # noqa: E402
from asclepius.store import _db_storage_durable  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(A.app)


def _clear_store_env(monkeypatch):
    for k in ("ASCLEPIUS_ASSET_STORE", "ASCLEPIUS_DATA_DIR", "ASCLEPIUS_DB_PATH",
              "ASCLEPIUS_INGEST_DIR"):
        monkeypatch.delenv(k, raising=False)


# ── F1: the predicate answers durability, not configuredness ─────────────────
def test_explicit_tmp_asset_store_is_ephemeral(monkeypatch):
    """The regression this PRD exists for: an operator sees the 'not set, will be
    lost' warning, sets the variable to /tmp, and the warning goes quiet while the
    data loss continues. Setting it must now TRIGGER the warning, not silence it."""
    _clear_store_env(monkeypatch)
    monkeypatch.setenv("ASCLEPIUS_ASSET_STORE", "/tmp/asc_ephemeral_assets")
    assert asc_constants.asset_store_is_ephemeral() is True
    ok, why = asc_assets.asset_storage_durable()
    assert ok is False and "ephemeral" in why


def test_tmp_db_path_makes_derived_asset_store_ephemeral(monkeypatch):
    """The asset store is DERIVED from the DB path when unset, so an ephemeral DB
    path silently drags the asset store onto ephemeral storage with it."""
    _clear_store_env(monkeypatch)
    monkeypatch.setenv("ASCLEPIUS_DB_PATH", "/tmp/asc_db_probe/asclepius.db")
    assert asc_constants.asset_store().startswith("/tmp/")
    assert asc_constants.asset_store_is_ephemeral() is True


def test_durable_dir_is_not_ephemeral(monkeypatch, tmp_path_factory):
    """A real non-/tmp directory reports durable. pytest's tmp_path lives under
    /tmp, so a genuinely durable-looking path has to be built outside it."""
    _clear_store_env(monkeypatch)
    durable = Path(__file__).resolve().parent.parent / ".durable-test-store"
    durable.mkdir(exist_ok=True)
    try:
        monkeypatch.setenv("ASCLEPIUS_ASSET_STORE", str(durable))
        assert asc_constants.asset_store_is_ephemeral() is False
        ok, _why = asc_assets.asset_storage_durable()
        assert ok is True
    finally:
        try:
            durable.rmdir()
        except OSError:
            pass


@pytest.mark.parametrize("prefix", ["/tmp", "/var/tmp", "/dev/shm", "/run"])
def test_every_ephemeral_prefix_fails_all_three_checks(monkeypatch, prefix):
    _clear_store_env(monkeypatch)
    monkeypatch.setenv("ASCLEPIUS_ASSET_STORE", f"{prefix}/asc_probe/assets")
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", f"{prefix}/asc_probe/ingest")
    monkeypatch.setenv("ASCLEPIUS_DB_PATH", f"{prefix}/asc_probe/asclepius.db")
    assert asc_constants.path_is_ephemeral(f"{prefix}/asc_probe") is True
    assert asc_assets.asset_storage_durable()[0] is False
    assert asc_ingestion.ingest_storage_durable()[0] is False
    assert _db_storage_durable()[0] is False


def test_ephemeral_prefixes_have_one_definition():
    """Three copies of a security-relevant list is how they drift (§F1)."""
    assert asc_ingestion._EPHEMERAL_PREFIXES is asc_constants.EPHEMERAL_PREFIXES


# ── F3: the database check ───────────────────────────────────────────────────
def test_unset_db_path_is_not_durable(monkeypatch):
    """Unset means the DB sits beside the code and is replaced every redeploy —
    the single most destructive loss in the system, and the one with no check."""
    _clear_store_env(monkeypatch)
    ok, why = _db_storage_durable()
    assert ok is False and "ASCLEPIUS_DB_PATH is not set" in why


def test_readonly_db_dir_reports_not_writable(monkeypatch):
    """A mount that attached read-only presents as a perfectly healthy directory
    and fails on first write. Probe it rather than trusting the stat."""
    base = Path(__file__).resolve().parent.parent / ".durable-ro-test"
    base.mkdir(exist_ok=True)
    monkeypatch.setenv("ASCLEPIUS_DB_PATH", str(base / "asclepius.db"))
    os.chmod(base, 0o500)
    try:
        ok, why = _db_storage_durable()
        if os.geteuid() == 0:
            # root ignores the permission bits, so this container cannot exercise
            # the read-only case — assert the writable path instead of asserting
            # something untrue about what was measured.
            assert ok is True
        else:
            assert ok is False and "not writable" in why
    finally:
        os.chmod(base, 0o700)
        base.rmdir()


def test_writable_durable_db_dir_passes(monkeypatch):
    base = Path(__file__).resolve().parent.parent / ".durable-rw-test"
    base.mkdir(exist_ok=True)
    monkeypatch.setenv("ASCLEPIUS_DB_PATH", str(base / "asclepius.db"))
    try:
        ok, why = _db_storage_durable()
        assert ok is True and "durable" in why
        # The probe cleans up after itself — a stray probe file every boot is a
        # slow leak on a volume the operator is watching for capacity.
        assert not [p for p in base.iterdir() if p.name.startswith(".durability-probe")]
    finally:
        for p in base.iterdir():
            p.unlink()
        base.rmdir()


# ── F2: the boot gate ────────────────────────────────────────────────────────
def _boot_gate(*, production: bool, durable: bool, monkeypatch):
    """Run the SAME decision the startup block runs. Importing main's startup
    coroutine would drag in demo seeding and schedulers; the branch under test is
    the failure aggregation, and it is reproduced here verbatim."""
    from http_security import is_production

    monkeypatch.setenv("ENV", "production" if production else "development")
    checks = (("database", lambda: (durable, "probe")),
              ("raw ingest", lambda: (True, "ok")),
              ("asset store", lambda: (True, "ok")))
    failures = [(n, w) for n, fn in checks for ok, w in [fn()] if not ok]
    if failures:
        detail = " · ".join(f"{n}: {w}" for n, w in failures)
        if is_production():
            raise RuntimeError(f"NON-DURABLE STORAGE, refusing to start — {detail}")
        return f"warned: {detail}"
    return "ok"


def test_production_refuses_to_boot_on_non_durable_storage(monkeypatch):
    with pytest.raises(RuntimeError) as exc:
        _boot_gate(production=True, durable=False, monkeypatch=monkeypatch)
    assert "refusing to start" in str(exc.value)
    assert "database" in str(exc.value)  # the log must name WHICH store


def test_development_warns_and_starts(monkeypatch):
    assert _boot_gate(production=False, durable=False,
                      monkeypatch=monkeypatch).startswith("warned:")


def test_startup_block_is_present_and_fails_closed():
    """The gate is a startup block in main.py, not a helper this test can call.
    Assert the shape that matters: it raises in production and warns otherwise."""
    src = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
    block = src[src.index("─── storage durability"):src.index("─── end storage durability")]
    assert "_db_storage_durable" in block
    assert "ingest_storage_durable" in block
    assert "asset_storage_durable" in block
    assert "raise RuntimeError" in block and "is_production()" in block


# ── F4: reconciliation reports, never deletes ────────────────────────────────
def _case_with_asset(store, sha: str):
    upload = store.insert_ingest_upload(
        link_id="hs-portal", partner_id="hs-x", filename="b.zip", sha256="d",
        size_bytes=1, raw_path=None, source_ip=None)
    return store.insert_ingest_case(
        upload_id=upload["upload_id"], patient_key="pk-1", specialty="nephrology",
        case={"case_source": "real_deid",
              "studies": [{"study_id": "s1", "label": "CT",
                           "asset": {"asset_id": "asset-" + sha[:24], "sha256": sha,
                                     "mime": "image/png"}}]},
        status="ingested", report=None)


def test_reconcile_reports_a_deleted_blob_with_its_case(monkeypatch, tmp_path):
    monkeypatch.setenv("ASCLEPIUS_ASSET_STORE", str(tmp_path / "assets"))
    store = A.fresh_store()
    asset = asc_assets.process_upload(_png_bytes(), "image/png")
    ic = _case_with_asset(store, asset["sha256"])

    assert asc_assets.reconcile_assets(store)["missing_blobs"] == []

    os.unlink(asc_assets._blob_path(asset["sha256"]))
    rep = asc_assets.reconcile_assets(store)
    missing = rep["missing_blobs"]
    assert [m["sha256"] for m in missing] == [asset["sha256"]]
    assert missing[0]["case_id"] == ic["ingest_case_id"]
    assert missing[0]["study_id"] == "s1"


def test_reconcile_reports_orphans_and_deletes_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("ASCLEPIUS_ASSET_STORE", str(tmp_path / "assets"))
    store = A.fresh_store()
    stray = "0" * 64
    path = Path(asc_assets._blob_path(stray))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not referenced by anything")

    rep = asc_assets.reconcile_assets(store)
    assert stray in rep["orphan_blobs"]
    # Reporting must never be destructive: an orphan costs disk, a wrongly-deleted
    # blob costs a case whose partner bundle has already been purged.
    assert path.exists() and path.read_bytes() == b"not referenced by anything"


def test_reconcile_endpoint_requires_admin(monkeypatch, tmp_path):
    monkeypatch.setenv("ASCLEPIUS_ASSET_STORE", str(tmp_path / "assets"))
    store = A.fresh_store()
    assert client.get("/api/asclepius/admin/storage/reconcile").status_code in (401, 403)
    evaluator = A.make_user(store, role="evaluator")
    assert client.get("/api/asclepius/admin/storage/reconcile",
                      headers=A.headers_for(evaluator)).status_code == 403
    admin = A.make_user(store, role="admin")
    r = client.get("/api/asclepius/admin/storage/reconcile", headers=A.headers_for(admin))
    assert r.status_code == 200
    body = r.json()
    assert body["missing_count"] == 0 and body["orphan_count"] == 0
    assert {s["store"] for s in body["storage"]} == {"database", "raw ingest", "asset store"}


def _png_bytes() -> bytes:
    from PIL import Image
    import io as _io
    buf = _io.BytesIO()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()
