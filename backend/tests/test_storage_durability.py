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
              "ASCLEPIUS_INGEST_DIR", asc_constants.VOLUME_MOUNT_ENV):
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


# ── The platform's declared volume mount outranks the prefix heuristic ───────
# EPHEMERAL_PREFIXES can only recognise storage it has been TOLD about, and it
# holds four well-known temp directories. A container-local /data is on none of
# them, so a store under it reads "durable" whether or not a volume was ever
# attached — a confident wrong answer, which is worse than no answer, on exactly
# the deployment shape this product ships in. When the host declares a mount
# (Railway sets RAILWAY_VOLUME_MOUNT_PATH on every service with a volume), that
# declaration is ground truth and the guess does not get a vote.

def test_declared_mount_absent_changes_nothing(monkeypatch):
    """No declaration, no new behaviour. A host that says nothing must land on
    exactly the verdict it landed on before this check existed."""
    _clear_store_env(monkeypatch)
    monkeypatch.setenv("ASCLEPIUS_DATA_DIR", "/data")
    monkeypatch.setenv("ASCLEPIUS_DB_PATH", "/data/asclepius.db")
    assert asc_constants.declared_volume_mount() == ""
    assert asc_constants.path_under_declared_volume("/data/assets") is None
    assert asc_assets.asset_storage_durable()[0] is True


def test_store_outside_the_declared_mount_is_not_durable(monkeypatch):
    """The whole point: /data LOOKS durable and is not. Only the platform knows."""
    _clear_store_env(monkeypatch)
    monkeypatch.setenv("ASCLEPIUS_DATA_DIR", "/data")
    monkeypatch.setenv("ASCLEPIUS_DB_PATH", "/data/asclepius.db")
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", "/data/ingest")
    monkeypatch.setenv(asc_constants.VOLUME_MOUNT_ENV, "/srv/volume")
    for check in (asc_assets.asset_storage_durable, _db_storage_durable,
                  asc_ingestion.ingest_storage_durable):
        ok, why = check()
        assert ok is False, check.__name__
        assert "/srv/volume" in why and asc_constants.VOLUME_MOUNT_ENV in why


def test_store_inside_the_declared_mount_names_the_volume(monkeypatch):
    """Durable is not enough on its own: "durable" is a claim an operator cannot
    check, and naming the mount the platform declared is one they can."""
    _clear_store_env(monkeypatch)
    monkeypatch.setenv("ASCLEPIUS_DATA_DIR", "/data")
    monkeypatch.setenv("ASCLEPIUS_DB_PATH", "/data/asclepius.db")
    monkeypatch.setenv(asc_constants.VOLUME_MOUNT_ENV, "/data")
    ok, why = asc_assets.asset_storage_durable()
    assert ok is True and "/data" in why


def test_a_declared_mount_is_matched_on_path_segments_not_string_prefix(monkeypatch):
    """/database must not count as "under /data". A substring match here would
    silently bless a directory that shares nothing but its opening letters."""
    _clear_store_env(monkeypatch)
    monkeypatch.setenv(asc_constants.VOLUME_MOUNT_ENV, "/data")
    assert asc_constants.path_under_declared_volume("/data") is True
    assert asc_constants.path_under_declared_volume("/data/assets") is True
    assert asc_constants.path_under_declared_volume("/database/assets") is False
    assert asc_constants.path_under_declared_volume("/datastore") is False


def test_an_s3_backend_ignores_the_declared_mount(monkeypatch):
    """s3 durability has nothing to do with any local volume."""
    _clear_store_env(monkeypatch)
    monkeypatch.setenv("ASCLEPIUS_ASSET_STORE", "s3://bucket/assets")
    monkeypatch.setenv(asc_constants.VOLUME_MOUNT_ENV, "/srv/volume")
    assert asc_assets.asset_storage_durable()[0] is True


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


# ── The onboarding demo video is an asset like any other ─────────────────────
# It lives in the same content-addressed store but is referenced by a
# `platform_media` row, not a case study — so the reconciler, which walked
# ingest cases and tasks only, could not see it. That inverted the truth twice on
# the one asset a human uploads by hand and expects to stay put: the blob was
# inventoried as an unreferenced orphan (first in line for any future sweep), and
# a demo video that had actually vanished off the volume was reported as nothing
# at all.

def _install_demo(store, data=b"fake-mp4-bytes-for-the-reconciler"):
    meta = asc_assets.store_media(iter([data]), "video/mp4")
    store.set_platform_media("onboarding_demo", sha256=meta["sha256"],
                             mime="video/mp4", byte_size=len(data),
                             filename="demo.mp4")
    return meta["sha256"]


def test_the_demo_video_is_a_reference_not_an_orphan(monkeypatch, tmp_path):
    monkeypatch.setenv("ASCLEPIUS_ASSET_STORE", str(tmp_path / "assets"))
    store = A.fresh_store()
    sha = _install_demo(store)

    rep = asc_assets.reconcile_assets(store)
    assert sha not in rep["orphan_blobs"], "the demo video is referenced, not stray"
    assert rep["n_rows"] == 1
    assert rep["missing_blobs"] == []


def test_a_demo_video_that_vanished_off_the_volume_is_reported_missing(monkeypatch, tmp_path):
    """The alarm that matters: the row survives a redeploy on the durable DB
    while the blob does not, and the walkthrough then plays a 404. Silence here
    is the failure — an operator has no other way to learn the video is gone."""
    monkeypatch.setenv("ASCLEPIUS_ASSET_STORE", str(tmp_path / "assets"))
    store = A.fresh_store()
    sha = _install_demo(store)

    os.unlink(asc_assets._blob_path(sha))
    rep = asc_assets.reconcile_assets(store)
    assert [m["sha256"] for m in rep["missing_blobs"]] == [sha]
    entry = rep["missing_blobs"][0]
    assert entry["source"] == "platform_media"
    assert entry["case_id"] == "onboarding_demo", "name the slot, not a case id"


def test_replacing_the_demo_leaves_the_old_blob_as_a_reported_orphan(monkeypatch, tmp_path):
    """`set_platform_media` rewrites the slot in place, so the previous upload
    stops being referenced. That is a real orphan and should read as one —
    reported, never deleted, exactly like every other orphan."""
    monkeypatch.setenv("ASCLEPIUS_ASSET_STORE", str(tmp_path / "assets"))
    store = A.fresh_store()
    first = _install_demo(store, b"first-cut-of-the-demo")
    second = _install_demo(store, b"second-cut-of-the-demo")
    assert first != second

    rep = asc_assets.reconcile_assets(store)
    assert second not in rep["orphan_blobs"]
    assert first in rep["orphan_blobs"]
    assert Path(asc_assets._blob_path(first)).exists()


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
