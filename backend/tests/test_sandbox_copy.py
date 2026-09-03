"""Sandbox PRD §4 — the snapshot copy, and §6.5 (live is never written).

A live health system with an upload (raw blob on disk), an ingest case that
references an image asset, and a portal account with a purpose is copied into
the sandbox: rows land in the sandbox DB stamped ``origin='production'``,
files land in the sandbox directories, the live file is byte-identical
afterwards, re-copy replaces rather than duplicates, and the copied portal
account cannot sign in.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import tests._asclepius as A  # noqa: E402

import realm  # noqa: E402
from asclepius import auth as asc_auth  # noqa: E402
from asclepius import sandbox_copy  # noqa: E402

client = TestClient(A.app)
BACKEND = pathlib.Path(__file__).resolve().parent.parent

SHA = "a" * 64


def _file_digest(path: str) -> str:
    h = hashlib.sha256()
    for suffix in ("", "-wal"):
        p = path + suffix
        if os.path.exists(p):
            h.update(pathlib.Path(p).read_bytes())
    return h.hexdigest()


@pytest.fixture
def world(monkeypatch):
    """Live: one hospital with an upload, a case and a portal account.
    Sandbox: fresh, with an admin. Live dirs are pointed at a scratch tree so
    file copies are observable."""
    monkeypatch.setenv(realm.ADMIN_PASSWORD_VAR, "sandbox-admin-secret")
    scratch = pathlib.Path(A.TMP_DIR) / f"copy_{A.uniq()}"
    (scratch / "assets" / SHA[:2]).mkdir(parents=True)
    (scratch / "ingest" / "quarantine").mkdir(parents=True)
    monkeypatch.setenv("ASCLEPIUS_ASSET_STORE", str(scratch / "assets"))
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(scratch / "ingest"))
    (scratch / "assets" / SHA[:2] / SHA).write_bytes(b"live-image-bytes")
    raw = scratch / "ingest" / "quarantine" / "upl-1.bin"
    raw.write_bytes(b"encrypted-raw-chart")

    live = A.fresh_store()
    hs = live.create_health_system_unclaimed("Mercy General", contact_email="ops@mercy.example")
    up = live.insert_ingest_upload(link_id="link-1", partner_id=hs["hs_id"], filename="chart.zip",
                                   sha256="b" * 64, size_bytes=19, raw_path=str(raw), source_ip="127.0.0.1")
    with live._conn() as conn:
        conn.execute("UPDATE ingest_uploads SET health_system_id = ?, purpose = 'task_creation' WHERE upload_id = ?",
                     (hs["hs_id"], up["upload_id"]))
        conn.execute(
            "INSERT INTO hs_portal_users (username, hs_id, password_hash, must_reset, email, active, created_at, purpose) "
            "VALUES (?, ?, ?, 0, ?, 1, ?, ?)",
            ("mercygeneral", hs["hs_id"], "$pbkdf2-sha256$fake", "ops@mercy.example",
             "2026-01-01T00:00:00", "task_creation"))
    case = live.insert_ingest_case(upload_id=up["upload_id"], patient_key="p1", specialty="nephrology",
                                   case={"studies": [{"asset": {"sha256": SHA}}]}, status="ingested", report=None)
    with realm.scoped("sandbox"):
        sb = A.fresh_store()
        admin = A.make_user(sb, role="admin")
        token = asc_auth.create_token(admin)
    return {"live": live, "sb": sb, "hs": hs, "upload": up, "case": case, "scratch": scratch,
            "headers": {"Authorization": "Bearer " + token}}


def test_copy_sources_lists_live_systems_and_the_fixture(world):
    r = client.get("/api/asclepius/sandbox/copy-sources", headers=world["headers"])
    assert r.status_code == 200, r.text
    by_id = {s["hs_id"]: s for s in r.json()["sources"]}
    src = by_id[world["hs"]["hs_id"]]
    assert src["name"] == "Mercy General" and src["uploads"] == 1 and src["ingest_cases"] == 1
    assert src["copied_at"] is None and src["fixture"] is False
    fx = by_id[sandbox_copy.FIXTURE_SOURCE_ID]
    assert fx["fixture"] is True and fx["name"] == "Archangel (fixture)"


def test_copy_snapshots_rows_and_files_and_never_writes_live(world):
    live, sb, hs = world["live"], world["sb"], world["hs"]
    before = _file_digest(live.db_path)
    r = client.post(f"/api/asclepius/sandbox/copy-health-system/{hs['hs_id']}", headers=world["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["uploads"] == 1 and body["ingest_cases"] == 1 and body["portal_accounts"] == 1
    assert body["files"] == 1 and body["assets"] == 1 and body["replaced"] is False
    assert body["purposes"] == ["task_creation"]
    # Live file byte-identical.
    assert _file_digest(live.db_path) == before
    with live._conn() as conn:
        assert conn.execute("SELECT origin FROM health_systems WHERE hs_id = ?", (hs["hs_id"],)).fetchone()[0] is None

    with realm.scoped("sandbox"):
        copy = sb.get_health_system(hs["hs_id"])
        assert copy["origin"] == "production" and copy["source_hs_id"] == hs["hs_id"] and copy["copied_at"]
        ups = sb.list_uploads_for_health_system(hs["hs_id"])
        assert len(ups) == 1 and ups[0]["upload_id"] == world["upload"]["upload_id"]
        # The raw blob was copied INTO the sandbox ingest dir and the row points there.
        sb_paths = realm.paths("sandbox")
        assert ups[0]["raw_path"].startswith(os.path.abspath(sb_paths["ingest"]))
        assert pathlib.Path(ups[0]["raw_path"]).read_bytes() == b"encrypted-raw-chart"
        case = sb.get_ingest_case(world["case"]["ingest_case_id"])
        assert case and case["case"]["studies"][0]["asset"]["sha256"] == SHA
        assert pathlib.Path(sb_paths["assets"], SHA[:2], SHA).read_bytes() == b"live-image-bytes"
        users = sb.list_hs_portal_users(hs["hs_id"])
        assert len(users) == 1 and users[0]["purpose"] == "task_creation"
        with sb._conn() as conn:
            row = conn.execute("SELECT password_hash, must_reset FROM hs_portal_users WHERE hs_id = ?",
                               (hs["hs_id"],)).fetchone()
        assert row["password_hash"] == sandbox_copy._UNUSABLE_HASH and row["must_reset"] == 1
    # Nothing else came along: no tasks, no physicians.
    with realm.scoped("sandbox"):
        assert [u for u in sb.list_users() if u.get("role") == "evaluator"] == []
    # The Data → Systems list shows the chip's data.
    r = client.get("/api/asclepius/admin/health-systems", headers=world["headers"])
    row = next(x for x in r.json()["health_systems"] if x["hs_id"] == hs["hs_id"])
    assert row["origin"] == "production" and row["copied_at"] and row["source_hs_id"] == hs["hs_id"]


def test_recopy_replaces_instead_of_duplicating(world):
    hs = world["hs"]
    for _ in range(2):
        r = client.post(f"/api/asclepius/sandbox/copy-health-system/{hs['hs_id']}", headers=world["headers"])
        assert r.status_code == 200, r.text
    assert r.json()["replaced"] is True
    with realm.scoped("sandbox"):
        sb = world["sb"]
        with sb._conn() as conn:
            assert conn.execute("SELECT count(*) FROM health_systems WHERE hs_id = ?", (hs["hs_id"],)).fetchone()[0] == 1
            assert conn.execute("SELECT count(*) FROM ingest_uploads WHERE health_system_id = ?", (hs["hs_id"],)).fetchone()[0] == 1
            assert conn.execute("SELECT count(*) FROM hs_portal_users WHERE hs_id = ?", (hs["hs_id"],)).fetchone()[0] == 1
    r = client.get("/api/asclepius/sandbox/copy-sources", headers=world["headers"])
    src = next(s for s in r.json()["sources"] if s["hs_id"] == hs["hs_id"])
    assert src["copied_at"]


def test_unknown_source_is_a_404(world):
    r = client.post("/api/asclepius/sandbox/copy-health-system/hs-nope", headers=world["headers"])
    assert r.status_code == 404


def test_copy_refuses_outside_the_sandbox_realm(world):
    with pytest.raises(sandbox_copy.NotSandbox):
        sandbox_copy.copy_health_system(world["hs"]["hs_id"])
    with pytest.raises(sandbox_copy.NotSandbox):
        sandbox_copy.list_sources()


def test_fixture_bundles_ingest_into_the_sandbox_only(world):
    r = client.post(f"/api/asclepius/sandbox/copy-health-system/{sandbox_copy.FIXTURE_SOURCE_ID}",
                    headers=world["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["origin"] == "fixture" and body["uploads"] >= 1
    with realm.scoped("sandbox"):
        with world["sb"]._conn() as conn:
            n = conn.execute("SELECT count(*) FROM ingest_uploads WHERE partner_id = ?",
                             (sandbox_copy.FIXTURE_SOURCE_ID,)).fetchone()[0]
    assert n >= 1
    with world["live"]._conn() as conn:
        assert conn.execute("SELECT count(*) FROM ingest_uploads WHERE partner_id = ?",
                            (sandbox_copy.FIXTURE_SOURCE_ID,)).fetchone()[0] == 0


# ─── §4 / §6.5 — read_live is called from exactly one module ─────────────────
def test_read_live_is_only_called_from_the_snapshot_copy():
    """A CALL, not a mention: store.py documents read_live in a comment, and a
    grep that cannot tell prose from code would forbid documenting it."""
    import ast
    callers = []
    for path in BACKEND.rglob("*.py"):
        rel = path.relative_to(BACKEND)
        if rel.parts[0] in ("tests",) or "_retired" in rel.parts or rel.name == "realm.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else (fn.id if isinstance(fn, ast.Name) else None)
                if name == "read_live":
                    callers.append(str(rel))
                    break
    assert callers == ["asclepius/sandbox_copy.py"], callers
