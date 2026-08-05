"""PRD-P Phase 1 — the payments schema migrates, re-runs, and defaults nothing.

The rule this file exists to hold: **no DEFAULT on any status column**. NULL means
"not yet decided" and must stay distinguishable from a decided value. ``qualified``
NULL is a session nobody has closed; ``qualified = 0`` is a session that closed
short of the threshold and is owed nothing. Collapsing those two with a DEFAULT
would make the second indistinguishable from the first, and the first is the one
you still owe an answer on.

The migration is run three times against a database already carrying users and
submissions, because a guarded migration that only works on an empty database is
not a migration.
"""

from __future__ import annotations

import sqlite3
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius.store import AsclepiusStore  # noqa: E402

_TABLES = ("work_sessions", "session_beats", "earnings")


def _cols(db_path: str, table: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return {r["name"]: dict(r) for r in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _fingerprint(db_path: str) -> dict:
    """Everything a re-run must not change: row counts across the pre-existing
    tables plus the exact column layout of the three new ones."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        counts = {}
        for t in ("users", "tasks", "submissions", *_TABLES):
            counts[t] = conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
        shapes = {t: sorted(_cols(db_path, t)) for t in _TABLES}
        return {"counts": counts, "shapes": shapes}
    finally:
        conn.close()


def _seeded_store():
    """A store with real pre-existing rows, so the migration is exercised against
    data rather than against an empty file."""
    store = A.fresh_store()
    user = A.make_user(store, role="evaluator", specialty="nephrology")
    task = store.insert_task(
        specialty="nephrology", difficulty="hard", prompt="pre-existing task",
        candidate_answers=[{"id": "A", "text": "a"}, {"id": "B", "text": "b"}],
    )
    store.insert_submission(
        submission_id="s-" + uuid.uuid4().hex[:12], task_id=task["task_id"],
        evaluator_id=user["id"], verdict="A_better", chosen_id="A", rejected_id="B",
        confidence="high", time_spent_sec=120, payload={}, annotator={},
        dedupe_hash=None,
    )
    return store, user


def test_migration_is_rerunnable_three_times_over_existing_data():
    store, _user = _seeded_store()
    before = _fingerprint(store.db_path)

    for _ in range(3):
        AsclepiusStore(db_path=store.db_path)   # __init__ runs _init_schema()

    assert _fingerprint(store.db_path) == before


def test_no_default_on_any_status_column():
    store, _user = _seeded_store()

    # These are the three columns PRD-P §2 names. A DEFAULT on any of them
    # destroys the tri-state the rest of the feature is built on.
    assert _cols(store.db_path, "work_sessions")["qualified"]["dflt_value"] is None
    assert _cols(store.db_path, "work_sessions")["end_reason"]["dflt_value"] is None
    assert _cols(store.db_path, "earnings")["status"]["dflt_value"] is None

    # ...and none of them is NOT NULL either, which would be the same assertion
    # by another route: a NOT NULL column with no default simply cannot hold
    # "not yet decided".
    assert _cols(store.db_path, "work_sessions")["qualified"]["notnull"] == 0
    assert _cols(store.db_path, "work_sessions")["end_reason"]["notnull"] == 0


def test_counter_columns_do_carry_defaults():
    """The inverse rule, asserted so nobody "fixes" it later: zero credited
    seconds is a DECIDED fact about a session, not an absence of one."""
    ws = _cols(_seeded_store()[0].db_path, "work_sessions")
    assert ws["credited_seconds"]["dflt_value"] == "0"
    assert ws["continuous_seconds"]["dflt_value"] == "0"


def test_unique_kind_ref_id_is_the_double_pay_guard():
    """The database constraint, not the application check, is the guarantee."""
    store, user = _seeded_store()
    ref = "s-" + uuid.uuid4().hex[:12]
    first = store.insert_earning(
        earning_id="e-1", user_id=user["id"], kind="task", ref_id=ref,
        amount_cents=7500, rate_cents=7500, status="accrued", accrued_at="2026-08-01T00:00:00Z")
    assert first is not None

    # A second insert with a DIFFERENT earning_id — i.e. exactly what an
    # application-level "we checked first" race produces — must not land.
    second = store.insert_earning(
        earning_id="e-2", user_id=user["id"], kind="task", ref_id=ref,
        amount_cents=7500, rate_cents=7500, status="accrued", accrued_at="2026-08-01T00:00:00Z")
    assert second is None

    conn = sqlite3.connect(store.db_path)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM earnings WHERE kind = 'task' AND ref_id = ?", (ref,)).fetchone()[0]
    finally:
        conn.close()
    assert n == 1

    # Same ref_id under a different kind is a different thing and IS allowed —
    # the constraint is on the pair, not on the id.
    assert store.insert_earning(
        earning_id="e-3", user_id=user["id"], kind="review_session", ref_id=ref,
        amount_cents=10000, rate_cents=10000, status="approved",
        accrued_at="2026-08-01T00:00:00Z") is not None


def test_seq_replay_is_blocked_at_the_database_level():
    """UNIQUE(session_id, seq): the application also checks that seq increases,
    but a replay must be impossible even if that check is ever refactored away."""
    store, user = _seeded_store()
    store.insert_work_session(
        session_id="ws-1", user_id=user["id"], kind="review",
        started_at="2026-08-01T00:00:00Z", nonce="n0", min_seconds=1200, rate_cents=10000)
    conn = sqlite3.connect(store.db_path)
    try:
        conn.execute("INSERT INTO session_beats (beat_id, session_id, server_ts, seq, active) "
                     "VALUES ('b1', 'ws-1', '2026-08-01T00:00:00Z', 1, 1)")
        conn.commit()
        try:
            conn.execute("INSERT INTO session_beats (beat_id, session_id, server_ts, seq, active) "
                         "VALUES ('b2', 'ws-1', '2026-08-01T00:00:15Z', 1, 1)")
            conn.commit()
            raise AssertionError("a replayed seq was accepted by the database")
        except sqlite3.IntegrityError:
            pass
    finally:
        conn.close()


def test_indexes_the_hot_reads_depend_on_exist():
    store, _user = _seeded_store()
    conn = sqlite3.connect(store.db_path)
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    finally:
        conn.close()
    for idx in ("idx_ws_user_open", "idx_beat_session", "idx_earnings_user", "idx_earnings_sweep"):
        assert idx in names, f"missing index {idx}"
