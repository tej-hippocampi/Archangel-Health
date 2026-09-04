"""Sandbox PRD §1.1–§1.2 and §6.1 / §6.8 / §6.9 — the realm plumbing.

The sandbox is a BOUNDARY, not a filter (§0): sandbox rows live in different
files. These tests pin the three things that make that true:

  * every file-backed store resolves to a DIFFERENT path per realm (§6.1);
  * sandbox paths are DERIVED from the live ones — there is no ``*_SANDBOX_*``
    path variable to read, so production and sandbox cannot drift (§6.9);
  * no module in ``backend/`` pins a store instance at import (§6.8) — a pinned
    instance is a store that ignores the realm forever.
"""

from __future__ import annotations

import ast
import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import tests._asclepius as A  # noqa: E402,F401  (sets env, imports main)

import realm  # noqa: E402

BACKEND = pathlib.Path(__file__).resolve().parent.parent


# ─── §6.1 No shared table ─────────────────────────────────────────────────────
def test_every_store_path_differs_between_realms():
    live = realm.paths("live")
    sandbox = realm.paths("sandbox")
    assert set(live) == set(sandbox) == {"asclepius", "community", "team", "assets", "exports", "ingest"}
    for key in live:
        assert os.path.abspath(live[key]) != os.path.abspath(sandbox[key]), key


def test_sandbox_stores_open_different_files():
    from asclepius.store import get_store
    from community.store import get_community_store
    from team_store import get_team_store
    from asclepius.assets import _store_root

    live = (get_store().db_path, get_community_store().db_path, get_team_store().db_path, _store_root())
    with realm.scoped("sandbox"):
        sb = (get_store().db_path, get_community_store().db_path, get_team_store().db_path, _store_root())
    for a, b in zip(live, sb):
        assert os.path.abspath(a) != os.path.abspath(b)
    # And the live handles are the same objects again once the scope ends.
    assert get_store().db_path == live[0]
    assert get_team_store().db_path == live[2]


def test_realm_proxy_follows_the_context():
    import main

    from asclepius.store import drop_store_for_realm
    from team_store import drop_team_store_for_realm
    # An earlier test may have rebound the sandbox realm to a temp file; start
    # from the derived binding so the suffix assertion below means something.
    drop_store_for_realm("sandbox")
    drop_team_store_for_realm("sandbox")
    live_path = main._team_store.db_path
    with realm.scoped("sandbox"):
        assert main._team_store.db_path != live_path
        assert main._team_store.db_path.endswith("_sandbox.db")
        assert main.app.state.asclepius_store.db_path.endswith("_sandbox.db")
    assert main._team_store.db_path == live_path


# ─── §6.9 Config parity — derived, never read ────────────────────────────────
def test_sandbox_paths_are_derived_not_configured(monkeypatch):
    monkeypatch.setenv("ASCLEPIUS_DB_PATH", "/data/asclepius.db")
    monkeypatch.setenv("COMMUNITY_DB_PATH", "/data/community.db")
    monkeypatch.setenv("TEAM_DB_PATH", "/data/team.db")
    monkeypatch.setenv("ASCLEPIUS_ASSET_STORE", "/data/assets")
    monkeypatch.setenv("ASCLEPIUS_EXPORT_DIR", "/data/exports")
    monkeypatch.delenv("ASCLEPIUS_INGEST_DIR", raising=False)
    # A stray sandbox variable must be IGNORED — one env, two realms (§1.1).
    monkeypatch.setenv("ASCLEPIUS_SANDBOX_DB_PATH", "/elsewhere/x.db")
    monkeypatch.setenv("ASCLEPIUS_SANDBOX_ASSET_STORE", "/elsewhere/assets")
    p = realm.paths("sandbox")
    assert p == {
        "asclepius": "/data/asclepius_sandbox.db",
        "community": "/data/community_sandbox.db",
        "team": "/data/team_sandbox.db",
        "assets": "/data/assets/sandbox",
        "exports": "/data/exports/sandbox",
        "ingest": "/data/asclepius-ingest/sandbox",
    }


def test_no_sandbox_path_variable_is_read_anywhere():
    """§7: the ONLY ``*_SANDBOX_*`` variables are the two passwords."""
    allowed = {realm.ADMIN_PASSWORD_VAR, realm.DOCTOR_PASSWORD_VAR}
    offenders = []
    for path in BACKEND.rglob("*.py"):
        rel = path.relative_to(BACKEND)
        if rel.parts[0] in ("tests", "_retired") or "_retired" in rel.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for token in set(__import__("re").findall(r"[A-Z0-9_]*SANDBOX[A-Z0-9_]*", text)):
            if token.startswith("ASCLEPIUS_") and token not in allowed and "SANDBOX" in token \
                    and (token.endswith("_PATH") or token.endswith("_DIR") or token.endswith("_STORE")):
                offenders.append((str(rel), token))
    assert not offenders, offenders


def test_live_asset_root_matches_constants():
    """``asclepius.constants.asset_store`` and ``realm.live_asset_root`` must be
    the same answer — the former now delegates to the latter, and this keeps
    anyone from re-inlining the resolution order in one of them."""
    from asclepius.constants import asset_store
    assert asset_store() == realm.live_asset_root()


# ─── §6.8 No import-time store pins ──────────────────────────────────────────
_STORE_CLASSES = {"TeamStore", "AsclepiusStore", "CommunityStore"}
#: Flag-gated legacy the PRD says to leave (§1.2): ``eligibility/pipeline.py``.
_EXEMPT = {pathlib.Path("eligibility") / "pipeline.py"}


def _module_level_store_calls(tree: ast.AST):
    hits = []
    for node in tree.body:  # module level ONLY — not inside functions/classes
        targets = []
        if isinstance(node, ast.Assign):
            targets = [node.value]
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.value]
        elif isinstance(node, ast.Expr):
            targets = [node.value]
        for value in targets:
            for sub in ast.walk(value):
                if isinstance(sub, ast.Call):
                    fn = sub.func
                    name = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else None)
                    if name in _STORE_CLASSES:
                        hits.append((sub.lineno, name))
    return hits


def test_no_module_instantiates_a_store_at_import():
    offenders = []
    for path in BACKEND.rglob("*.py"):
        rel = path.relative_to(BACKEND)
        if rel.parts[0] in ("tests", "scripts") or "_retired" in rel.parts or rel in _EXEMPT:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for lineno, name in _module_level_store_calls(tree):
            offenders.append(f"{rel}:{lineno} {name}()")
    assert not offenders, "import-time store pins (Sandbox PRD §1.2):\n" + "\n".join(offenders)


def test_scan_actually_catches_a_pin(tmp_path):
    """The scan above is only worth having if it fires."""
    tree = ast.parse("from team_store import TeamStore\n_s = TeamStore()\n")
    assert _module_level_store_calls(tree) == [(2, "TeamStore")]
    tree = ast.parse("def f():\n    return TeamStore()\n")
    assert _module_level_store_calls(tree) == []


# ─── read_live is sandbox-only and read-only ─────────────────────────────────
def test_read_live_refuses_outside_sandbox():
    with pytest.raises(realm.RealmError):
        with realm.read_live():
            pass


def test_read_live_opens_live_file_with_mode_ro():
    """§6.5: the copy endpoint's live connection is opened ``?mode=ro``."""
    from asclepius.store import get_store
    live_path = os.path.abspath(get_store().db_path)
    with realm.scoped("sandbox"):
        with realm.read_live() as live:
            assert live.read_only is True
            assert os.path.abspath(live.db_path) == live_path
            assert live._connect_uri().endswith("?mode=ro")
            assert live._connect_uri().startswith("file:")
            # A write through this handle is refused by sqlite itself.
            import sqlite3
            with pytest.raises(sqlite3.OperationalError):
                with live._conn() as conn:
                    conn.execute("CREATE TABLE sandbox_should_not_write (x)")
            # …and a read works.
            with live._conn() as conn:
                conn.execute("SELECT count(*) FROM users").fetchone()
