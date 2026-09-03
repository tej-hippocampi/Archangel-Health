"""team.db must be opened the way asclepius.db is (WAL + a real busy timeout).

asclepius.db has always used WAL + ``busy_timeout=30000`` + ``timeout=30``
(``asclepius/store.py``). team.db did not: six modules opened it with the sqlite
defaults, four of them without even a connect timeout. Rollback-journal mode
makes a reader block the writer, and Python's 5s default turns that into
"database is locked", which the audit path then swallowed, so ePHI access
records disappeared while requests still returned 200.

These tests pin the connection settings and pin the fact that every team.db
opener goes through the one shared helper.
"""

from __future__ import annotations

import os
import re
import sys
import uuid
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

os.environ["TEAM_DB_PATH"] = os.path.join("/tmp", f"teamdb_conn_{uuid.uuid4().hex}.db")

import patient_session  # noqa: E402
import team_store  # noqa: E402
import token_revocation  # noqa: E402
from audit import audit_log  # noqa: E402
from gold import store as gold_store  # noqa: E402


def _settings(conn):
    return (
        conn.execute("PRAGMA journal_mode").fetchone()[0],
        conn.execute("PRAGMA busy_timeout").fetchone()[0],
    )


def test_shared_helper_sets_wal_and_busy_timeout(tmp_path):
    with team_store.connect_team_db(str(tmp_path / "team.db")) as conn:
        mode, busy = _settings(conn)
    assert mode == "wal"
    assert busy >= 30000


def test_wal_persists_to_later_connections(tmp_path):
    """journal_mode lives on the file, so a second connection inherits it, but
    busy_timeout is per-connection and has to be re-set on every open."""
    path = str(tmp_path / "team.db")
    team_store.connect_team_db(path).close()
    with team_store.connect_team_db(path) as second:
        mode, busy = _settings(second)
    assert mode == "wal"
    assert busy >= 30000


def test_every_team_db_opener_is_configured():
    """The five modules that hold a live team.db connection on the request path
    (TeamStore, the ePHI audit log, gold visits, token revocation, patient
    sessions) must all report WAL + a nonzero busy timeout."""
    openers = {
        "team_store": team_store.TeamStore()._conn,
        "audit_log": audit_log._conn,
        "gold_store": gold_store._conn,
        "token_revocation": token_revocation._conn,
        "patient_session": patient_session._conn,
    }
    for name, opener in openers.items():
        conn = opener()
        try:
            mode, busy = _settings(conn)
        finally:
            conn.close()
        assert mode == "wal", f"{name} opened team.db in {mode} mode"
        assert busy >= 30000, f"{name} left busy_timeout at {busy}ms"


def test_no_bare_sqlite_connect_against_team_db():
    """A bare ``sqlite3.connect`` in any of these files is how the drift happened
    the first time; the helper is the only sanctioned door."""
    files = [
        BACKEND / "audit" / "audit_log.py",
        BACKEND / "gold" / "store.py",
        BACKEND / "token_revocation.py",
        BACKEND / "patient_session.py",
        BACKEND / "triage_demo_seed.py",
    ]
    for f in files:
        src = f.read_text()
        assert not re.search(r"\bsqlite3\.connect\(", src), f"{f.name} opens team.db directly"

    # team_store.py holds the single sanctioned connect, inside the helper.
    ts_src = (BACKEND / "team_store.py").read_text()
    assert len(re.findall(r"\bsqlite3\.connect\(", ts_src)) == 1
    helper = ts_src[ts_src.index("def connect_team_db("):ts_src.index("class TeamStore")]
    assert "sqlite3.connect(" in helper


def test_audit_create_table_is_off_the_write_path():
    """The CREATE TABLE used to run on every ePHI audit write, inside the global
    lock and ahead of the chain read. It belongs in _ensure_table_once."""
    src = (BACKEND / "audit" / "audit_log.py").read_text()
    body = src[src.index("def record("):src.index("def _recompute_hash(")]
    assert "_ensure_table_once(" in body
    assert "_ensure_table(conn)" not in body


def test_audit_failure_is_logged_not_printed():
    """A dropped ePHI record must be loud: ERROR with the actor and the route,
    never a print that nothing is watching."""
    src = (BACKEND / "audit" / "audit_log.py").read_text()
    body = src[src.index("def record("):src.index("def _recompute_hash(")]
    assert "print(" not in body
    assert "log.error(" in body


def test_audit_record_survives_and_logs_a_db_failure(monkeypatch, caplog):
    """Recording must never raise into the request path, but the failure has to
    land in the log with enough to identify what was not recorded."""
    def _boom(_path):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(audit_log, "connect_team_db", _boom)
    with caplog.at_level("ERROR", logger="audit"):
        audit_log.record(
            actor_type="staff", actor_id="dr.who@example.org", action="GET",
            outcome="success", resource="/api/patient/p1/discharge", patient_id="p1",
        )
    assert any(
        r.levelname == "ERROR" and "dr.who@example.org" in r.getMessage()
        and "/api/patient/p1/discharge" in r.getMessage()
        for r in caplog.records
    )


def test_audit_chain_still_works_after_the_hoist():
    audit_log.record(
        actor_type="staff", actor_id="a@example.org", action="GET",
        outcome="success", resource="/api/patient/px/discharge", patient_id="px",
    )
    assert audit_log.verify()["ok"] is True
    assert any(e["patient_id"] == "px" for e in audit_log.list_events(limit=50))
