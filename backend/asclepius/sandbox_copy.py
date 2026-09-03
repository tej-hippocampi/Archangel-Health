"""Sandbox PRD §4 — real data in the sandbox: the snapshot copy.

``POST /api/asclepius/sandbox/copy-health-system/{hs_id}`` copies ONE live
health system into the sandbox: the ``health_systems`` row, its uploads (with
the raw blobs and any referenced image assets into the sandbox directories),
its ingest cases, and its purpose resolutions (the portal accounts that carry
them, with their passwords made unusable). It does NOT copy tasks,
submissions or physicians — the point is to re-run task creation and routing
from raw data.

This module is the ONLY place in the codebase that opens both realms' stores
in one request, through ``realm.read_live()`` — a test greps for the call.
The live connection is opened ``?mode=ro`` (asserted by another test), so a
live row cannot be written from here even by mistake. Re-copy is idempotent:
it replaces the sandbox copy wholesale.

The four committed patient bundles are reachable through the same button as
the ``Archangel (fixture)`` provider: copying that pseudo-source runs the real
ingest door inside the sandbox realm, so the longitudinal pipeline is testable
in the sandbox on day one.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

import realm as _realm

log = logging.getLogger("asclepius.sandbox")

#: The pseudo-source id for the committed bundles (== the fixture partner id).
FIXTURE_SOURCE_ID = "archangel-fixture"

#: An impossible password hash: passlib never verifies against it, so a copied
#: portal account keeps its purpose (what the copy is for) but nobody can sign
#: in with a live credential in the sandbox. A sandbox admin resets it if a
#: portal login is wanted.
_UNUSABLE_HASH = "!sandbox-copy-no-login"

_SHA_RE = re.compile(r"\b[0-9a-f]{64}\b")


class SourceNotFound(LookupError):
    """No live health system with that id."""


class NotSandbox(RuntimeError):
    pass


def _utcnow_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


def _require_sandbox() -> None:
    if not _realm.is_sandbox():
        raise NotSandbox("the snapshot copy runs only in the sandbox realm")


# ─── Generic row copy ────────────────────────────────────────────────────────
def _columns(conn: sqlite3.Connection, table: str) -> List[str]:
    return [r["name"] if isinstance(r, sqlite3.Row) else r[1]
            for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _select_rows(live_conn: sqlite3.Connection, table: str, where: str, params: tuple) -> List[Dict[str, Any]]:
    rows = live_conn.execute(f"SELECT * FROM {table} WHERE {where}", params).fetchall()
    return [dict(r) for r in rows]


def _insert_rows(sb_conn: sqlite3.Connection, table: str, rows: List[Dict[str, Any]]) -> int:
    """INSERT OR REPLACE each row into the sandbox table, restricted to the
    columns the sandbox schema actually has (both realms carry the live schema,
    so this is the whole row in practice)."""
    if not rows:
        return 0
    cols = [c for c in _columns(sb_conn, table) if c in rows[0]]
    placeholders = ", ".join("?" for _ in cols)
    sql = f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
    sb_conn.executemany(sql, [tuple(r.get(c) for c in cols) for r in rows])
    return len(rows)


# ─── Files ───────────────────────────────────────────────────────────────────
def _copy_file(src: str, dst: str) -> bool:
    if not src or not os.path.isfile(src):
        return False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    return True


def _relocate_raw(raw_path: Optional[str], upload_id: str, live_root: str, sb_root: str) -> Optional[str]:
    """Where an upload's raw blob lands in the sandbox ingest dir. Same
    relative path when it sits under the live root; otherwise a per-upload
    folder so two blobs can never collide."""
    if not raw_path:
        return None
    src = os.path.abspath(raw_path)
    live_root = os.path.abspath(live_root)
    if src.startswith(live_root + os.sep):
        return os.path.join(sb_root, os.path.relpath(src, live_root))
    return os.path.join(sb_root, "copied", upload_id, os.path.basename(src))


def _asset_shas(*blobs: Optional[str]) -> List[str]:
    out: List[str] = []
    for blob in blobs:
        for sha in _SHA_RE.findall(blob or ""):
            if sha not in out:
                out.append(sha)
    return out


# ─── Sources (§4, the Accounts tab's copy panel) ─────────────────────────────
def list_sources() -> List[Dict[str, Any]]:
    _require_sandbox()
    from asclepius import patient_fixtures as asc_fixtures  # noqa: PLC0415
    from asclepius.store import get_store  # noqa: PLC0415

    sb = get_store()
    out: List[Dict[str, Any]] = []
    with _realm.read_live() as live:
        with live._conn() as conn:
            systems = [dict(r) for r in conn.execute(
                "SELECT hs_id, name, active, created_at FROM health_systems ORDER BY name").fetchall()]
            for hs in systems:
                n_up = conn.execute("SELECT count(*) FROM ingest_uploads WHERE health_system_id = ?",
                                    (hs["hs_id"],)).fetchone()[0]
                n_cases = conn.execute(
                    "SELECT count(*) FROM ingest_cases WHERE upload_id IN "
                    "(SELECT upload_id FROM ingest_uploads WHERE health_system_id = ?)",
                    (hs["hs_id"],)).fetchone()[0]
                copy = sb.get_health_system(hs["hs_id"])
                out.append({
                    "hs_id": hs["hs_id"], "name": hs["name"], "active": bool(hs.get("active", 1)),
                    "uploads": int(n_up), "ingest_cases": int(n_cases), "fixture": False,
                    "copied_at": (copy or {}).get("copied_at") if (copy or {}).get("origin") == "production" else None,
                })
    # The committed bundles, through the same button (§4).
    with sb._conn() as conn:
        fx = conn.execute("SELECT max(created_at) FROM ingest_uploads WHERE partner_id = ?",
                          (FIXTURE_SOURCE_ID,)).fetchone()[0]
    out.append({
        "hs_id": FIXTURE_SOURCE_ID, "name": asc_fixtures.FIXTURE_PARTNER_LABEL, "active": True,
        "uploads": len(asc_fixtures.available_bundles()), "ingest_cases": 0, "fixture": True,
        "copied_at": fx,
    })
    return out


# ─── The copy ────────────────────────────────────────────────────────────────
def copy_health_system(hs_id: str, *, actor_id: Optional[str] = None) -> Dict[str, Any]:
    """Snapshot one live health system into the sandbox (see module doc)."""
    _require_sandbox()
    if hs_id == FIXTURE_SOURCE_ID:
        return copy_fixture_bundles(actor_id=actor_id)
    from asclepius.store import get_store  # noqa: PLC0415

    sb = get_store()
    live_paths = _realm.paths(_realm.LIVE)
    sb_paths = _realm.paths(_realm.SANDBOX)
    now = _utcnow_iso()

    # 1. Read everything from the live store, read-only, in one pass.
    with _realm.read_live() as live:
        with live._conn() as conn:
            hs_rows = _select_rows(conn, "health_systems", "hs_id = ?", (hs_id,))
            if not hs_rows:
                raise SourceNotFound(f"No live health system {hs_id!r}.")
            uploads = _select_rows(conn, "ingest_uploads", "health_system_id = ?", (hs_id,))
            upload_ids = [u["upload_id"] for u in uploads]
            cases: List[Dict[str, Any]] = []
            links: List[Dict[str, Any]] = []
            if upload_ids:
                marks = ", ".join("?" for _ in upload_ids)
                cases = _select_rows(conn, "ingest_cases", f"upload_id IN ({marks})", tuple(upload_ids))
                link_ids = sorted({u["link_id"] for u in uploads if u.get("link_id")})
                if link_ids:
                    marks = ", ".join("?" for _ in link_ids)
                    links = _select_rows(conn, "ingest_upload_links", f"link_id IN ({marks})", tuple(link_ids))
            portal_users = _select_rows(conn, "hs_portal_users", "hs_id = ?", (hs_id,))

    # 2. Files: raw blobs into the sandbox ingest dir, image assets into the
    #    sandbox asset dir. Paths on the copied rows are rewritten to the
    #    sandbox location so nothing in the sandbox ever points at a live file.
    files_copied = 0
    for u in uploads:
        dst = _relocate_raw(u.get("raw_path"), u["upload_id"], live_paths["ingest"], sb_paths["ingest"])
        if dst and _copy_file(u["raw_path"], dst):
            files_copied += 1
            u["raw_path"] = dst
        elif dst:
            u["raw_path"] = None   # the live blob is gone (purged) — say so, do not point at it
    shas: List[str] = []
    for c in cases:
        for sha in _asset_shas(c.get("case_json"), c.get("report_json")):
            if sha not in shas:
                shas.append(sha)
    assets_copied = 0
    for sha in shas:
        src = os.path.join(live_paths["assets"], sha[:2], sha)
        dst = os.path.join(sb_paths["assets"], sha[:2], sha)
        if _copy_file(src, dst):
            assets_copied += 1

    # 3. Stamp and neutralise.
    hs = dict(hs_rows[0])
    hs["origin"] = "production"
    hs["copied_at"] = now
    hs["source_hs_id"] = hs_id
    for pu in portal_users:
        pu["password_hash"] = _UNUSABLE_HASH
        pu["must_reset"] = 1
        pu["last_login"] = None

    # 4. Replace the sandbox copy wholesale (idempotent re-copy).
    with sb._conn() as conn:
        replaced = conn.execute("SELECT count(*) FROM health_systems WHERE hs_id = ?", (hs_id,)).fetchone()[0] > 0
        conn.execute(
            "DELETE FROM ingest_cases WHERE upload_id IN "
            "(SELECT upload_id FROM ingest_uploads WHERE health_system_id = ?)", (hs_id,))
        conn.execute("DELETE FROM ingest_uploads WHERE health_system_id = ?", (hs_id,))
        conn.execute("DELETE FROM hs_portal_users WHERE hs_id = ?", (hs_id,))
        conn.execute("DELETE FROM health_systems WHERE hs_id = ?", (hs_id,))
        _insert_rows(conn, "health_systems", [hs])
        _insert_rows(conn, "ingest_upload_links", links)
        n_up = _insert_rows(conn, "ingest_uploads", uploads)
        n_cases = _insert_rows(conn, "ingest_cases", cases)
        n_users = _insert_rows(conn, "hs_portal_users", portal_users)
    sb.log_event(entity_type="health_system", entity_id=hs_id, event_type="sandbox_copied",
                 actor=actor_id, payload={"uploads": n_up, "ingest_cases": n_cases,
                                          "portal_accounts": n_users, "files": files_copied,
                                          "assets": assets_copied, "replaced": replaced})
    log.info("[sandbox] copied live %s: %d uploads, %d cases, %d files, %d assets",
             hs_id, n_up, n_cases, files_copied, assets_copied)
    return {
        "ok": True, "hs_id": hs_id, "name": hs.get("name"), "origin": "production",
        "copied_at": now, "replaced": bool(replaced),
        "uploads": n_up, "ingest_cases": n_cases, "portal_accounts": n_users,
        "purposes": sorted({(pu.get("purpose") or "") for pu in portal_users} - {""}),
        "files": files_copied, "assets": assets_copied,
    }


def copy_fixture_bundles(*, actor_id: Optional[str] = None) -> Dict[str, Any]:
    """The committed patient bundles, through the real ingest door, in the
    sandbox realm. No live read is needed — the bundles live in the repo."""
    _require_sandbox()
    from asclepius import patient_fixtures as asc_fixtures  # noqa: PLC0415
    from asclepius.store import get_store  # noqa: PLC0415

    store = get_store()
    # Unpack inline (the admin route defers this to BackgroundTasks; here the
    # caller wants the cases to exist when the response comes back).
    result = asc_fixtures.ingest_committed_bundles(
        store, actor=actor_id or "sandbox", on_ingested=lambda fn, *a, **k: fn(*a, **k))
    return {
        "ok": True, "hs_id": FIXTURE_SOURCE_ID, "name": asc_fixtures.FIXTURE_PARTNER_LABEL,
        "origin": "fixture", "copied_at": _utcnow_iso(), "replaced": bool(result.get("skipped")),
        "uploads": int(result.get("ingested", 0)) + int(result.get("skipped", 0)),
        "ingest_cases": None, "portal_accounts": 0, "purposes": [], "files": int(result.get("ingested", 0)),
        "assets": 0, "bundles": result.get("results", []),
    }
