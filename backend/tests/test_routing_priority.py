"""PRD R Phase 1 — the task lifecycle, and the priority that makes κ fill.

Every test here walks the REAL routes. The lesson this repo has already paid for
three times (context pack §6): a suite that builds state with
``store.insert_submission`` never runs ``refresh_task_status``, and
``refresh_task_status`` is precisely the behaviour the routing has to work WITH.
A green suite over an unreachable feature is worse than a red one.

The property under test throughout: **a singly-labelled task outranks a fresh
one, as a SORT and never as a FILTER.**
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _asclepius as A  # noqa: E402
from asclepius import routing as asc_routing  # noqa: E402
from asclepius import store as asc_store  # noqa: E402

client = TestClient(A.app)


@pytest.fixture(autouse=True)
def _fresh():
    A.fresh_store()
    yield


# ─── helpers: everything through HTTP ─────────────────────────────────────────
def _admin_h():
    return A.headers_for(A.make_user(asc_store.get_store(), role="admin"))


def _labeler(specialty="nephrology"):
    return A.make_user(asc_store.get_store(), role="evaluator", specialty=specialty,
                       board_cert="board_certified_nephrology", years_experience=12)


def _create_task(admin_h, *, specialty="nephrology", **kw):
    body = {
        "specialty": specialty, "difficulty": "hard", "max_labels": 1,
        "prompt": f"Hyperkalemia case {A.uniq(8)}?",
        "candidate_answers": [{"id": "A", "text": "Calcium then dialyze."},
                              {"id": "B", "text": "Dialysate K+ 1.0."}],
    }
    body.update(kw)
    r = client.post("/api/asclepius/tasks", json={"tasks": [body]}, headers=admin_h)
    assert r.status_code == 200, r.text
    return r.json()["created"][0]


def _submit(task_id, labeler, *, verdict="A_better"):
    """POST /submissions — the REAL path, including refresh_task_status."""
    sid = "s-" + uuid.uuid4().hex[:12]
    salt = A.uniq(6)
    body = {
        "submission_id": sid, "task_id": task_id, "verdict": verdict,
        "chosen_id": "A" if verdict == "A_better" else "B",
        "rejected_id": "B" if verdict == "A_better" else "A",
        "time_spent_sec": 130,
        "prompt_review": {"reviewed": True, "verdict": "valid"},
        "independent_answer": {"text": f"Stabilize with IV calcium then dialyze ({salt})."},
        "chosen_revision": {"edited": False, "why_better_notes": f"B over-lowers K+ ({salt})"},
        "rejected_critique": {"error_tags": ["dosing_error"], "why_worse": f"aggressive {salt}"},
    }
    r = client.post("/api/asclepius/submissions", json=body, headers=A.headers_for(labeler))
    assert r.status_code == 200, r.text
    return sid


def _draw(labeler):
    """GET /tasks/next — the labeler queue a real TL sees."""
    r = client.get("/api/asclepius/tasks/next", headers=A.headers_for(labeler))
    assert r.status_code == 200, r.text
    return (r.json() or {}).get("task")


def _phase(task_id):
    store = asc_store.get_store()
    task = store.get_task(task_id)
    n_subs = len([s for s in store.submissions_for_task(task_id) if s.get("verdict")])
    n_reviews = len(store.reviews_for_task(task_id))
    return asc_routing.phase(task, n_subs, n_reviews)


# ═══ the state machine is DERIVED ════════════════════════════════════════════
def test_phase_is_a_pure_function_of_counts_and_never_a_column():
    """PRD R §2: derive the phase, do not store it. Two representations of one
    fact is how double-labeling became dead code the first time."""
    fresh = {"max_labels": 1}
    assert asc_routing.phase(fresh, 0, 0) == asc_routing.AWAITING_FIRST
    assert asc_routing.phase(fresh, 1, 0) == asc_routing.AWAITING_SECOND   # rate 1.0
    assert asc_routing.phase({"max_labels": 2}, 1, 0) == asc_routing.AWAITING_SECOND
    assert asc_routing.phase({"max_labels": 2}, 2, 0) == asc_routing.REVIEW_READY
    assert asc_routing.phase({"max_labels": 2}, 2, 1) == asc_routing.ADJUDICATED

    # No table anywhere carries the phase as a column.
    store = asc_store.get_store()
    with store._conn() as conn:
        for table in ("tasks", "submissions", "case_reviews"):
            names = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            assert "phase" not in names, f"{table} grew a phase column"


def test_phase_follows_the_rate_knob_rather_than_a_second_copy_of_it(monkeypatch):
    """Shedding load has to reach the state machine, or the queue and the sweep
    disagree about what 'awaiting_second' means."""
    monkeypatch.setenv("ASCLEPIUS_DOUBLE_LABEL_RATE", "0.0")
    plain = {"max_labels": 1, "difficulty": "medium"}
    assert asc_routing.phase(plain, 1, 0) == asc_routing.REVIEW_READY
    # ...but the unconditionally-double-labelled cases still route.
    assert asc_routing.phase({"max_labels": 1, "case_source": "real_deid"}, 1, 0) == \
        asc_routing.AWAITING_SECOND


# ═══ §2 Phase 1 tests, exactly as the PRD names them ═════════════════════════
def test_first_submission_leaves_the_task_awaiting_its_second_label():
    """TL#1 submits; task is awaiting_second and max_labels == 2 and open."""
    store = asc_store.get_store()
    admin_h = _admin_h()
    tid = _create_task(admin_h)
    tl1 = _labeler()
    _submit(tid, tl1)

    assert _phase(tid) == asc_routing.AWAITING_SECOND

    # refresh_task_status closed it at max_labels=1 — untouched, by design. The
    # queue does not depend on the flag having landed yet...
    tl2 = _labeler()
    drawn = _draw(tl2)
    assert drawn is not None and drawn["task_id"] == tid

    # ...and serving it is what lifts the stored capacity and reopens the row.
    task = store.get_task(tid)
    assert task["max_labels"] == 2
    assert task["status"] == "open"


def test_a_second_labeler_drawing_next_gets_that_task_not_a_fresh_one():
    """The priority rule, which is the throughput rule that makes κ fill."""
    admin_h = _admin_h()
    # Three fresh cases created BEFORE the one that will carry a label, so
    # oldest-first ordering alone would serve a fresh one.
    for _ in range(3):
        _create_task(admin_h)
    partial = _create_task(admin_h)
    _submit(partial, _labeler())

    assert _draw(_labeler())["task_id"] == partial


def test_the_first_labeler_never_gets_their_own_task_back():
    """Independence is the whole point — enforced in SQL, not by the caller."""
    admin_h = _admin_h()
    tid = _create_task(admin_h)
    tl1 = _labeler()
    _submit(tid, tl1)

    again = _draw(tl1)
    assert again is None or again["task_id"] != tid


def test_priority_is_a_sort_not_a_filter_so_nobody_sees_an_empty_queue():
    """PRD R §7 — the starvation guard.

    One awaiting-second case, excluded for this labeler because they wrote its
    first label, and three fresh ones. A DESC sort falls through to the fresh
    work; a WHERE clause would hand this physician an empty screen and lose them.
    """
    admin_h = _admin_h()
    tl1 = _labeler()
    partial = _create_task(admin_h)
    _submit(partial, tl1)
    fresh = [_create_task(admin_h) for _ in range(3)]

    drawn = _draw(tl1)
    assert drawn is not None, "a labeler with no eligible second-label work saw an empty queue"
    assert drawn["task_id"] in fresh


def test_starvation_guard_holds_when_every_case_is_awaiting_second():
    """The degenerate version: EVERY case carries this labeler's first label
    except the newest. Nothing eligible is high-priority, so the scan must fall
    all the way through rather than stopping at the head of the queue."""
    admin_h = _admin_h()
    tl1 = _labeler()
    for _ in range(4):
        _submit(_create_task(admin_h), tl1)
    escape = _create_task(admin_h)

    assert _draw(tl1)["task_id"] == escape


def test_second_submission_moves_the_task_out_of_every_labeler_queue():
    """After TL#2 submits, phase is review_ready and no TL queue serves it."""
    store = asc_store.get_store()
    admin_h = _admin_h()
    tid = _create_task(admin_h)
    tl1, tl2 = _labeler(), _labeler()
    _submit(tid, tl1)
    assert _draw(tl2)["task_id"] == tid
    _submit(tid, tl2)

    assert _phase(tid) == asc_routing.REVIEW_READY
    task = store.get_task(tid)
    assert task["status"] == "done" and task["max_labels"] == 2

    # No third labeler, and neither of the two, can draw it again.
    for who in (_labeler(), tl1, tl2):
        drawn = _draw(who)
        assert drawn is None or drawn["task_id"] != tid
    assert store.next_double_label_for(_labeler()["id"], specialty="nephrology") is None


def test_the_pair_is_two_different_physicians():
    """κ over two labels by the same person measures consistency, not agreement."""
    store = asc_store.get_store()
    admin_h = _admin_h()
    tid = _create_task(admin_h)
    _submit(tid, _labeler())
    _submit(tid, _labeler())

    subs = store.submissions_for_task(tid)
    assert asc_routing.pair_is_independent(subs) is True
    same = [{"evaluator_id": "u1", "verdict": "A_better"},
            {"evaluator_id": "u1", "verdict": "B_better"}]
    assert asc_routing.pair_is_independent(same) is False


def test_value_aware_routing_sees_the_same_priority():
    """The assisted queue (v2/v3) ranks by expected value-per-minute, not by the
    SQL order. Lifting an awaiting-second case to max_labels=2 turns on the
    double-labeled-credentialed multiplier, so the case the queue wants finished
    also scores higher — the priority survives the re-rank."""
    from asclepius import value as asc_value

    admin_h = _admin_h()
    for _ in range(3):
        _create_task(admin_h)
    partial = _create_task(admin_h)
    _submit(partial, _labeler())

    tl2 = _labeler()
    r = client.get("/api/asclepius/tasks/next?portal_version=v2", headers=A.headers_for(tl2))
    assert r.status_code == 200, r.text
    assert r.json()["task"]["task_id"] == partial

    store = asc_store.get_store()
    lifted = store.get_task(partial)
    fresh_task = store.get_task(_create_task(admin_h))
    assert (asc_value.routing_score(lifted, 420.0)
            > asc_value.routing_score(fresh_task, 420.0))


def test_terminal_prompt_flags_are_never_dragged_back_for_a_second_opinion():
    """A clinician who ruled the prompt out has ruled it out. 'done' is the only
    closed status the widened eligibility admits."""
    store = asc_store.get_store()
    admin_h = _admin_h()
    tid = _create_task(admin_h)
    _submit(tid, _labeler())
    store.mark_task_status(tid, "prompt_flagged")

    for who in (_labeler(), _labeler()):
        drawn = _draw(who)
        assert drawn is None or drawn["task_id"] != tid


# ═══ H4 — the draw must not scan the fleet five times per row ════════════════
def _plan(store, sql, params):
    with store._conn() as conn:
        return [dict(r)["detail"] for r in
                conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()]


def test_the_labeler_draw_computes_its_counts_once_not_per_row():
    """H4. The label count was textually inlined three times per query and joined
    by two more correlated subqueries — five correlated scalar scans per row, on
    an unbounded fetch, on the single writer that labeler SUBMISSIONS also need.

    The property: counts are materialized once (a grouped join), and the plan
    carries no correlated scalar subquery for them."""
    store = asc_store.get_store()
    admin_h = _admin_h()
    for _ in range(3):
        _create_task(admin_h)
    who = _labeler()

    sql, params = store.labeler_queue_sql(
        evaluator_id=who["id"], specialty="nephrology", hard_only=True)
    steps = _plan(store, sql, params)
    plan = " | ".join(steps).upper()

    # The counts are computed once, over an index, not per candidate row.
    assert "MATERIALIZE" in plan, plan
    assert "COVERING INDEX IDX_SUB_TASK_VERDICT" in plan, plan

    # Correlated probes that REMAIN are the two independence/PHI guards, and a
    # NOT EXISTS is exactly what should compile to an indexed point lookup. What
    # must not survive is a correlated subquery that SCANS — that was the H4
    # shape, five of them per row.
    correlated = False
    for step in steps:
        up = step.upper()
        if "CORRELATED" in up:
            correlated = True
            continue
        if correlated:
            assert "USING INDEX" in up or "USING COVERING INDEX" in up, \
                f"a correlated subquery falls back to a scan: {step}"
            correlated = False

    # And the scan is windowed, so one draw cannot materialize the whole table.
    assert "LIMIT" in sql.upper()


def test_the_draw_stays_bounded_as_the_queue_grows():
    """A behavioural ceiling rather than a timing assertion: the draw must not
    build a Python dict for every open task in the fleet."""
    import tracemalloc

    store = asc_store.get_store()
    admin_h = _admin_h()
    who = _labeler()
    # Enough rows that an unbounded fetch is visibly different from a windowed one.
    with store._conn() as conn:
        now = "2026-01-01T00:00:00"
        conn.executemany(
            "INSERT INTO tasks (task_id, prompt, specialty, difficulty, "
            "candidate_answers_json, max_labels, status, created_at) "
            "VALUES (?, ?, 'nephrology', 'hard', '[]', 1, 'open', ?)",
            [(f"t-bulk-{i:06d}", f"case {i}", now) for i in range(4000)])

    tracemalloc.start()
    got = store.next_task_for_evaluator(
        evaluator_id=who["id"], specialty="nephrology", hard_only=True)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert got is not None
    # Measured after the fix: ~0.09 MB and ~7 ms for this queue. The ceiling is
    # ~30× that, which is generous for a lean windowed fetch and nowhere near
    # what a full `SELECT t.*` of every open task costs (the audit measured
    # 98.5 MB at 20,000 tasks — this fixture is a fifth of that).
    assert peak < 3_000_000, f"one draw peaked at {peak / 1e6:.1f} MB"


def test_the_priority_sort_survives_the_scan_window():
    """The window must never cost the throughput rule. An awaiting-second case
    created LAST still comes first, because the sort is in SQL and the window is
    applied after it."""
    admin_h = _admin_h()
    for _ in range(40):
        _create_task(admin_h)
    partial = _create_task(admin_h)
    _submit(partial, _labeler())

    assert _draw(_labeler())["task_id"] == partial


def test_the_starvation_guard_survives_the_scan_window():
    """And the window must never hand a labeler an empty queue: the exclusions
    that make a task ineligible for THIS labeler are applied in SQL, so the
    window counts only work they could actually take."""
    admin_h = _admin_h()
    mine = _labeler()
    for _ in range(40):
        _submit(_create_task(admin_h), mine)
    escape = _create_task(admin_h)

    assert _draw(mine)["task_id"] == escape


# ═══ M1 — one definition of "a label" ════════════════════════════════════════
def test_a_verdictless_row_cannot_wedge_a_case_at_awaiting_second():
    """M1. Eligibility and priority counted verdict-bearing submissions; capacity
    counted ALL of them — three lines after a comment saying a verdict-less row
    'must not count toward the pair'. One new non-terminal verdict-less write and
    a case sits at Awaiting 2nd forever, servable to nobody."""
    store = asc_store.get_store()
    admin_h = _admin_h()
    tid = _create_task(admin_h)
    _submit(tid, _labeler())

    # A verdict-less row lands on the task (a draft, a flag — not a label).
    store.insert_submission(
        submission_id="s-noverdict", task_id=tid, evaluator_id=_labeler()["id"],
        verdict=None, chosen_id=None, rejected_id=None, confidence=None,
        time_spent_sec=0, payload={}, annotator={}, dedupe_hash=None)

    assert _phase(tid) == asc_routing.AWAITING_SECOND
    drawn = _draw(_labeler())
    assert drawn is not None and drawn["task_id"] == tid, \
        "the case is stuck: it wants a second label and no labeler can take it"


def test_capacity_catch_up_is_idempotent_and_writes_once():
    """The lift carries ``AND max_labels < 2``: drawing the same case repeatedly
    must not re-write the row or re-log the flag event."""
    store = asc_store.get_store()
    admin_h = _admin_h()
    tid = _create_task(admin_h)
    _submit(tid, _labeler())

    for _ in range(3):
        _draw(_labeler())

    events = [e for e in store.list_events(entity_type="task", entity_id=tid)
              if e.get("event_type") == "double_label_flagged"]
    assert len(events) == 1
    assert store.get_task(tid)["max_labels"] == 2
