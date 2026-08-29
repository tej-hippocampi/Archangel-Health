"""The merged queue query, proven placeholder by placeholder.

Two features landed on the same SQL from opposite sides. PRD-ASSIGN rewrote the
priority order so an assigned case sorts to the top of its assignee's queue —
adding a ``?`` inside ORDER BY. PRD-2 added the sequence gate so a chart walk is
served in order — adding a ``?`` inside WHERE. Neither author saw the other's
change, and git merged both cleanly.

**A clean merge is exactly the dangerous outcome here.** SQLite numbers ``?`` by
position across the whole statement, so the ORDER BY parameter binds AFTER every
WHERE parameter, including the optional ones that only appear when a caller passes
a specialty or a difficulty floor. Get that order wrong and nothing raises: an
``evaluator_id`` string lands where a float belongs, ``t.empirical_difficulty >=
'u-alice'`` is simply false in SQLite's type ordering, and the queue quietly serves
a wrong — but plausible — ranking. No error, no log line, no failing assertion
anywhere else in the suite. The bug is invisible until a buyer asks why a
physician's assigned cases never came up.

So the merge is not asserted to be correct by inspection. These tests build a
fixture where EVERY optional placeholder is live at once — an assigned task, a
trajectory, a specialty filter and a measured-difficulty floor — and assert the
served result. That is the only arrangement in which a mis-binding cannot hide
behind a placeholder that was not being used.

They are also a regression fence in both directions: the assignment sort and the
sequence gate each have a test here that fails if the other feature's merge
resolution silently drops it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated():
    A.fresh_store()
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


def _task(store, **kw):
    kw.setdefault("prompt", "q?")
    kw.setdefault("specialty", "cardiology")
    kw.setdefault("difficulty", "hard")
    kw.setdefault("max_labels", 1)
    return store.insert_task(**kw)


def _submit(store, task_id, evaluator_id):
    """A stored label, with the store's full keyword contract."""
    return store.insert_submission(
        submission_id=f"s-{evaluator_id}-{task_id[-6:]}", task_id=task_id,
        evaluator_id=evaluator_id, verdict="A_better", chosen_id="a",
        rejected_id="b", confidence="high", time_spent_sec=300,
        payload={}, annotator={}, dedupe_hash=None,
    )


def _measured(store, task_id, value=0.9):
    """Stamp a live-measured difficulty so the empirical floor can bind against it."""
    with store._conn() as conn:  # noqa: SLF001
        conn.execute("UPDATE tasks SET difficulty_measured = 1, empirical_difficulty = ? "
                     "WHERE task_id = ?", (value, task_id))


# ═══════════════════════════════════════════════════════════════════════════════
# Every placeholder live at once
# ═══════════════════════════════════════════════════════════════════════════════
def test_every_placeholder_binds_to_its_own_clause():
    """The all-at-once fixture. Assigned task + trajectory + specialty + measured
    floor, so the statement carries its maximum number of ``?``.

    If any parameter were off by one, the measured-difficulty float would land on
    the specialty comparison (or an evaluator id on the float) and the result set
    would change. It is asserted exactly, not merely as non-empty."""
    store = _store()
    doc = A.make_user(store, specialty="cardiology")

    assigned = _task(store, prompt="the assigned one")
    plain = _task(store, prompt="an ordinary open case")
    _measured(store, assigned["task_id"])
    _measured(store, plain["task_id"])

    # A trajectory whose point 0 is open — it must pass the gate, and its point 1
    # must not, so the gate's own placeholder is provably doing work.
    walk = [
        _task(store, prompt=f"walk point {i}", trajectory_id="traj-1", sequence_index=i)
        for i in range(2)
    ]
    for t in walk:
        _measured(store, t["task_id"])

    store.upsert_assignment(task_id=assigned["task_id"], user_id=doc["id"],
                            role="label", assigned_by="u-admin")

    sql, params = store.labeler_queue_sql(
        evaluator_id=doc["id"], specialty="cardiology", hard_only=True,
        require_measured_difficulty=True, min_empirical_difficulty=0.5,
    )
    # Placeholder count and parameter count must agree — the cheapest possible
    # statement of the invariant, and the one that catches an appended clause
    # whose parameter someone forgot.
    assert sql.count("?") == len(params), (sql.count("?"), len(params))

    with store._conn() as conn:  # noqa: SLF001
        rows = [r["task_id"] for r in conn.execute(sql, params).fetchall()]

    assert rows[0] == assigned["task_id"], "the assigned case must sort first"
    assert walk[0]["task_id"] in rows, "point 0 of an untouched walk is servable"
    assert walk[1]["task_id"] not in rows, "point 1 is sealed behind point 0"
    assert plain["task_id"] in rows


def test_the_float_floor_actually_filters():
    """Proof the measured-difficulty placeholder binds to a NUMBER.

    Were an evaluator id bound here instead, SQLite would compare a float column
    against a string — always false under its type ordering — and the queue would
    come back EMPTY rather than wrong, which is the failure this pins."""
    store = _store()
    doc = A.make_user(store, specialty="cardiology")
    lo = _task(store, prompt="barely measured")
    hi = _task(store, prompt="clearly measured")
    _measured(store, lo["task_id"], 0.20)
    _measured(store, hi["task_id"], 0.95)

    sql, params = store.labeler_queue_sql(
        evaluator_id=doc["id"], specialty="cardiology", hard_only=True,
        require_measured_difficulty=True, min_empirical_difficulty=0.50,
    )
    with store._conn() as conn:  # noqa: SLF001
        rows = [r["task_id"] for r in conn.execute(sql, params).fetchall()]
    assert rows == [hi["task_id"]], "the floor must exclude 0.20 and keep 0.95"


def test_the_specialty_placeholder_actually_filters():
    """Same argument on the other optional placeholder."""
    store = _store()
    doc = A.make_user(store, specialty="cardiology")
    mine = _task(store, prompt="cardiology", specialty="cardiology")
    theirs = _task(store, prompt="nephrology", specialty="nephrology")

    sql, params = store.labeler_queue_sql(
        evaluator_id=doc["id"], specialty="cardiology", hard_only=True)
    with store._conn() as conn:  # noqa: SLF001
        rows = [r["task_id"] for r in conn.execute(sql, params).fetchall()]
    assert mine["task_id"] in rows and theirs["task_id"] not in rows


# ═══════════════════════════════════════════════════════════════════════════════
# Each feature, fenced against the other's resolution
# ═══════════════════════════════════════════════════════════════════════════════
def test_regression_assigned_case_outranks_open_queue_cases():
    """PRD-ASSIGN's whole delivery mechanism, and §3 of the batches PRD depends on
    it. Fails if the merge took this branch's older priority order."""
    store = _store()
    doc = A.make_user(store, specialty="cardiology")
    # Give the open case a label so it would outrank a fresh one on the OLD order
    # (label_count DESC) — the assigned one must still win.
    older = _task(store, prompt="older, once-labeled", max_labels=2)
    _submit(store, older["task_id"], "u-someone-else")
    assigned = _task(store, prompt="assigned, untouched")
    store.upsert_assignment(task_id=assigned["task_id"], user_id=doc["id"],
                            role="label", assigned_by="u-admin")

    sql, params = store.labeler_queue_sql(evaluator_id=doc["id"], specialty="cardiology")
    with store._conn() as conn:  # noqa: SLF001
        rows = [r["task_id"] for r in conn.execute(sql, params).fetchall()]
    assert rows[0] == assigned["task_id"], (
        "an assigned case must outrank a singly-labelled open one; taking the "
        "pre-merge ORDER BY silently reverses this")


def test_regression_the_sequence_gate_survived_the_merge():
    """PRD-2's seal. Fails if the merge took main's store.py wholesale."""
    store = _store()
    doc = A.make_user(store, specialty="cardiology")
    pts = [_task(store, prompt=f"p{i}", trajectory_id="traj-2", sequence_index=i)
           for i in range(3)]

    sql, params = store.labeler_queue_sql(evaluator_id=doc["id"], specialty="cardiology")
    with store._conn() as conn:  # noqa: SLF001
        rows = [r["task_id"] for r in conn.execute(sql, params).fetchall()]
    assert rows == [pts[0]["task_id"]], "only point 0 is servable on an untouched walk"

    _submit(store, pts[0]["task_id"], doc["id"])
    with store._conn() as conn:  # noqa: SLF001
        rows = [r["task_id"] for r in conn.execute(sql, params).fetchall()]
    assert rows == [pts[1]["task_id"]], "answering point 0 unlocks point 1 and only 1"


def test_an_assignment_never_defeats_the_sequence_gate():
    """The two features meet. An assignment is a PRIORITY, not a permission — the
    store's own words — so assigning point 2 of a walk must not serve it early.
    This is the case where a merge that 'works' could still be wrong: both
    placeholders bind, both clauses run, and the wrong one wins."""
    store = _store()
    doc = A.make_user(store, specialty="cardiology")
    pts = [_task(store, prompt=f"p{i}", trajectory_id="traj-3", sequence_index=i)
           for i in range(3)]
    store.upsert_assignment(task_id=pts[2]["task_id"], user_id=doc["id"],
                            role="label", assigned_by="u-admin")

    sql, params = store.labeler_queue_sql(evaluator_id=doc["id"], specialty="cardiology")
    with store._conn() as conn:  # noqa: SLF001
        rows = [r["task_id"] for r in conn.execute(sql, params).fetchall()]
    assert pts[2]["task_id"] not in rows, (
        "assigning a later point must not unseal it — the gate is a WHERE clause "
        "and the assignment only reorders what already passed it")
    assert rows == [pts[0]["task_id"]]
