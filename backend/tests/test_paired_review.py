"""PRD R Phase 2/3 — the reviewer draws a PAIR.

Review stops being about a submission and becomes about a case with two
independent labels, which is the only shape from which Cohen's κ and an expert
acceptance rate can both be computed honestly.

Everything here goes through the real routes. The two things that are asserted
hardest are the two that are expensive to get wrong: **blinding** (the buyer-
facing honesty claim) and **the statistics' names** (the commercial argument).
"""
from __future__ import annotations

import signal
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _asclepius as A  # noqa: E402
from asclepius import agreement as asc_agreement  # noqa: E402
from asclepius import review as asc_review  # noqa: E402
from asclepius import routing as asc_routing  # noqa: E402
from asclepius import store as asc_store  # noqa: E402

client = TestClient(A.app)

_REVIEW_JS = Path(__file__).resolve().parents[2] / "frontend" / "asclepius" / "review.js"
_BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _fresh():
    A.fresh_store()
    yield


# ─── helpers ──────────────────────────────────────────────────────────────────
def _admin_h():
    return A.headers_for(A.make_user(asc_store.get_store(), role="admin"))


def _labeler(specialty="nephrology", **kw):
    store = asc_store.get_store()
    user = A.make_user(store, role="evaluator", specialty=specialty,
                       board_cert="board_certified_nephrology", years_experience=12, **kw)
    _grant_tier(store, user["id"], "labeler")
    return store.get_user_by_id(user["id"])


def _grant_tier(store, user_id: str, tier: str) -> None:
    """Simulate the PRD-B tier assignment (same helper as test_review_tier)."""
    with store._conn() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
        if "tier" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN tier TEXT")
        conn.execute("UPDATE users SET tier = ? WHERE id = ?", (tier, user_id))


def _reviewer(specialty="nephrology"):
    store = asc_store.get_store()
    user = A.make_user(store, role="evaluator", specialty=specialty,
                       board_cert="board_certified_nephrology", years_experience=22)
    _grant_tier(store, user["id"], "reviewer")
    return store.get_user_by_id(user["id"])


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


def _submit(task_id, labeler, *, verdict="A_better", scratch=None, critique=None):
    sid = "s-" + uuid.uuid4().hex[:12]
    salt = A.uniq(6)
    body = {
        "submission_id": sid, "task_id": task_id, "verdict": verdict,
        "chosen_id": "A" if verdict == "A_better" else "B",
        "rejected_id": "B" if verdict == "A_better" else "A",
        "time_spent_sec": 140,
        "prompt_review": {"reviewed": True, "verdict": "valid"},
        "independent_answer": {"text": scratch or f"IV calcium, then dialysis ({salt})."},
        "chosen_revision": {"edited": False, "why_better_notes": f"B over-lowers K+ ({salt})"},
        "rejected_critique": {"error_tags": ["dosing_error"],
                              "why_worse": critique or f"too aggressive {salt}"},
    }
    r = client.post("/api/asclepius/submissions", json=body, headers=A.headers_for(labeler))
    assert r.status_code == 200, r.text
    return sid


def _reveal(task_id, labeler, text=None):
    """POST /tasks/{id}/reveal — the blind-commit gate a labeler passes through
    BEFORE the candidate answers are shown to them."""
    r = client.post(f"/api/asclepius/tasks/{task_id}/reveal",
                    json={"text": text or f"Calcium first, then dialysis ({A.uniq(6)})."},
                    headers=A.headers_for(labeler))
    assert r.status_code == 200, r.text
    return r.json()


def _paired_task(admin_h, *, tl1=None, tl2=None, verdicts=("A_better", "A_better"),
                 attested=True, **kw):
    """A case with two independent labels, built entirely through HTTP.

    ``attested`` walks each labeler through the reveal gate first, which is what
    a real labeler does and what licenses κ's blinding flag (Audit R C2). Pass
    ``attested=False`` to build the unverified case deliberately."""
    tid = _create_task(admin_h, **kw)
    for labeler, verdict in ((tl1 or _labeler(), verdicts[0]),
                             (tl2 or _labeler(), verdicts[1])):
        if attested:
            _reveal(tid, labeler)
        _submit(tid, labeler, verdict=verdict)
    return tid


def _draw_pair(reviewer):
    r = client.get("/api/asclepius/review/pair/next", headers=A.headers_for(reviewer))
    assert r.status_code == 200, r.text
    return r.json()


def _adjudication(**kw):
    body = {
        "verdict": "accept", "stronger": "A", "accepted_side": "A",
        "dimensions": {k: "agree" for k in asc_review.DIMENSION_KEYS},
        "time_spent_sec": 90,
    }
    body.update(kw)
    return body


# ═══ the draw ════════════════════════════════════════════════════════════════
def test_a_reviewer_draws_a_pair_containing_both_submissions():
    admin_h = _admin_h()
    tid = _paired_task(admin_h, verdicts=("A_better", "B_better"))

    got = _draw_pair(_reviewer())["pair"]
    assert got is not None and got["task_id"] == tid
    assert [a["label"] for a in got["answers"]] == ["A", "B"]
    # Both labels are present, and they are the two DIFFERENT answers.
    served = sorted(a["answer"]["verdict"] for a in got["answers"])
    assert served == ["A_better", "B_better"]
    assert got["blinded"] is True


def test_a_singly_labelled_case_is_never_served_as_a_pair():
    admin_h = _admin_h()
    tid = _create_task(admin_h)
    _submit(tid, _labeler())
    assert _draw_pair(_reviewer())["pair"] is None


def test_a_case_still_wanting_a_third_label_is_not_review_ready():
    """The SQL and ``routing.phase`` have to agree. An admin-set max_labels of 3
    with two labels in is awaiting_second, not review_ready — and if only the
    state machine knew that, the two would be a second pair of truths.

    This test used to end by asserting that the case IS served once its third
    label lands, which merely PROVED the truncation Audit R H3 names and said
    nothing about its consequence. What happens at three labels is now asserted
    where it belongs, in
    ``test_a_third_label_is_never_marked_reviewed_by_someone_who_did_not_see_it``:
    the case is not served at all, and it is counted so a human sees it."""
    store = asc_store.get_store()
    admin_h = _admin_h()
    tid = _create_task(admin_h, max_labels=3)
    _submit(tid, _labeler())
    _submit(tid, _labeler())

    task = store.get_task(tid)
    assert asc_routing.phase(task, 2, 0) == asc_routing.AWAITING_SECOND
    assert _draw_pair(_reviewer())["pair"] is None
    # ...and it is still labeler work, so the labeler queue keeps offering it.
    r = client.get("/api/asclepius/tasks/next", headers=A.headers_for(_labeler()))
    assert (r.json().get("task") or {}).get("task_id") == tid


def test_a_reviewer_who_authored_one_of_them_never_draws_that_case():
    """Enforced in SQL, and again on the POST — a physician grading their own
    work is not a review, and κ's blinding claim collapses with it."""
    admin_h = _admin_h()
    rv = _reviewer()
    tid = _paired_task(admin_h, tl1=rv)

    assert _draw_pair(rv)["pair"] is None
    r = client.post(f"/api/asclepius/review/pair/{tid}",
                    json=_adjudication(), headers=A.headers_for(rv))
    assert r.status_code == 403


def test_two_reviewers_cannot_draw_the_same_case():
    """The compare-and-swap holds under concurrency: one pair, N reviewers, one
    winner. Moved from the submission to the task — the unit of work changed."""
    admin_h = _admin_h()
    _paired_task(admin_h)
    reviewers = [_reviewer() for _ in range(6)]

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda rv: _draw_pair(rv)["pair"], reviewers))
    assert sum(1 for p in results if p is not None) == 1


def test_a_reviewer_never_draws_the_same_case_twice():
    admin_h = _admin_h()
    tid = _paired_task(admin_h)
    rv = _reviewer()
    assert _draw_pair(rv)["pair"]["task_id"] == tid

    r = client.post(f"/api/asclepius/review/pair/{tid}",
                    json=_adjudication(), headers=A.headers_for(rv))
    assert r.status_code == 200, r.text
    assert _draw_pair(rv)["pair"] is None


def test_an_abandoned_claim_requeues_after_its_lease(monkeypatch):
    monkeypatch.setenv("ASCLEPIUS_REVIEW_LEASE_MIN", "1")
    store = asc_store.get_store()
    admin_h = _admin_h()
    tid = _paired_task(admin_h)
    first = _reviewer()
    assert _draw_pair(first)["pair"]["task_id"] == tid
    assert _draw_pair(_reviewer())["pair"] is None       # still leased

    # Age the lease clock — review_claimed_at, never updated_at.
    with store._conn() as conn:
        conn.execute("UPDATE tasks SET review_claimed_at = '2000-01-01T00:00:00Z' "
                     "WHERE task_id = ?", (tid,))
    assert _draw_pair(_reviewer())["pair"]["task_id"] == tid


# ═══ A/B assignment (PRD R §2.2) ═════════════════════════════════════════════
def test_ab_assignment_is_stable_for_one_reviewer_and_differs_across_reviewers():
    """Stable across reloads (a refresh that swapped the columns would read as a
    bug and cost the reviewer's trust in the pair), and uncorrelated with
    submission order (position must never leak who went first)."""
    admin_h = _admin_h()
    tid = _paired_task(admin_h, verdicts=("A_better", "B_better"))

    rv = _reviewer()
    first = _draw_pair(rv)["pair"]
    second = _draw_pair(rv)["pair"]                 # the claim is theirs; re-served
    assert second is not None
    assert [a["answer"]["verdict"] for a in first["answers"]] == \
           [a["answer"]["verdict"] for a in second["answers"]]

    # Across many reviewers on the same case, both orders occur — the assignment
    # is a real permutation, not a constant that happens to look shuffled.
    orders = {asc_routing.ab_swapped(tid, _reviewer()["id"]) for _ in range(40)}
    assert orders == {True, False}


def test_the_position_the_client_names_is_resolved_server_side():
    """The client sends 'A'; the server maps it back through the same seeded
    permutation. A client that guessed a submission id could otherwise attribute
    an acceptance to a physician the reviewer never chose."""
    store = asc_store.get_store()
    admin_h = _admin_h()
    tid = _paired_task(admin_h)
    rv = _reviewer()
    _draw_pair(rv)

    subs = [s for s in store.submissions_for_task(tid) if s.get("verdict")]
    shown_a, _ = asc_routing.ab_pair(subs, task_id=tid, reviewer_id=rv["id"])
    r = client.post(f"/api/asclepius/review/pair/{tid}",
                    json=_adjudication(stronger="A", accepted_side="A"),
                    headers=A.headers_for(rv))
    assert r.status_code == 200, r.text
    assert r.json()["review"]["accepted_submission_id"] == shown_a["submission_id"]
    # Stored pair columns are CANONICAL, not this reviewer's shuffled positions.
    assert r.json()["review"]["pair_sub_a"] == subs[0]["submission_id"]
    assert r.json()["review"]["pair_sub_b"] == subs[1]["submission_id"]


# ═══ H1 — 'stronger' must not be stored in the reviewer's shuffled positions ══
def test_stronger_is_stored_canonically_next_to_the_canonical_pair():
    """H1. ``pair_sub_a``/``pair_sub_b`` are canonical (oldest-first);
    ``accepted_side`` is resolved through the seeded map. ``stronger`` was
    written RAW, in the reviewer's positions, in the column next door.

    Nothing reads it yet, which is exactly why it would break later: the first
    person to write the pairwise-quality report joins ``stronger`` to
    ``pair_sub_a`` — the only sane reading of two adjacent columns — and gets a
    coin flip. 'Which is stronger?' is the entire reason the pair exists."""
    store = asc_store.get_store()
    admin_h = _admin_h()

    # Cover both permutations, so a reviewer who happens to see the canonical
    # order cannot make this pass by accident.
    seen_swapped = set()
    for _ in range(12):
        tid = _paired_task(admin_h)
        rv = _reviewer()
        _draw_pair(rv)
        swapped = asc_routing.ab_swapped(tid, rv["id"])
        seen_swapped.add(swapped)

        subs = [s for s in store.submissions_for_task(tid) if s.get("verdict")]
        shown_a, shown_b = asc_routing.ab_pair(subs, task_id=tid, reviewer_id=rv["id"])

        r = client.post(f"/api/asclepius/review/pair/{tid}",
                        json=_adjudication(stronger="A", accepted_side="A"),
                        headers=A.headers_for(rv))
        assert r.status_code == 200, r.text
        row = r.json()["review"]

        # The naive read — the one a downstream report will write.
        by_position = {"A": row["pair_sub_a"], "B": row["pair_sub_b"]}[row["stronger"]]
        assert by_position == shown_a["submission_id"], (
            "stronger points at the wrong physician when read against the "
            "canonical pair columns")
        # And the unambiguous form agrees with it.
        assert row["stronger_submission_id"] == shown_a["submission_id"]
        assert row["accepted_submission_id"] == shown_a["submission_id"]
    assert seen_swapped == {True, False}, "only one A/B permutation was exercised"


def test_equivalent_names_no_stronger_submission():
    admin_h = _admin_h()
    tid = _paired_task(admin_h)
    rv = _reviewer()
    _draw_pair(rv)
    r = client.post(f"/api/asclepius/review/pair/{tid}",
                    json=_adjudication(stronger="equivalent", accepted_side="B"),
                    headers=A.headers_for(rv))
    assert r.status_code == 200, r.text
    assert r.json()["review"]["stronger"] == "equivalent"
    assert r.json()["review"]["stronger_submission_id"] is None


# ═══ H2 — one case, one adjudication, one row in the denominator ═════════════
def test_a_case_that_became_a_pair_stops_accepting_a_single_submission_review():
    """H2. The single-review predicates were checked at DRAW time and never
    again, while the labeler queue deliberately kept the same task servable to a
    second labeler. A reviewer holding a single claim, and a second labeler,
    could both proceed — producing two ``case_reviews`` rows for one case and an
    acceptance rate that counts it twice."""
    store = asc_store.get_store()
    admin_h = _admin_h()
    tid = _create_task(admin_h)
    tl1 = _labeler()
    _reveal(tid, tl1); _submit(tid, tl1)

    rv = _reviewer()
    drawn = client.get("/api/asclepius/review/next", headers=A.headers_for(rv)).json()
    sub_id = (drawn.get("submission") or {}).get("submission_id")
    if sub_id is None:                     # already routed to the pair queue
        pytest.skip("the single queue declined it, which is the other correct outcome")

    # ...and while they read it, the case gets its second label.
    tl2 = _labeler()
    _reveal(tid, tl2); _submit(tid, tl2)

    r = client.post(f"/api/asclepius/review/{sub_id}", json={
        "verdict": "accept",
        "dimensions": {k: "agree" for k in asc_review.DIMENSION_KEYS},
        "time_spent_sec": 45,
    }, headers=A.headers_for(rv))
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    # The copy has to say what actually happened. "Your claim expired" is false
    # and offers no way forward.
    assert "pair" in str(detail).lower()
    assert "expired" not in str(detail).lower()
    # Their work is handed back, not thrown away.
    assert detail.get("your_review", {}).get("dimensions")
    assert detail.get("task_id") == tid

    assert store.reviews_for_task(tid) == []


def test_a_case_is_adjudicated_once_from_either_flow():
    """The denominator invariant, stated directly: one case, at most one review
    row, whichever queue produced it."""
    store = asc_store.get_store()
    admin_h = _admin_h()
    tid = _paired_task(admin_h)
    rv1 = _reviewer()
    _draw_pair(rv1)
    assert client.post(f"/api/asclepius/review/pair/{tid}", json=_adjudication(),
                       headers=A.headers_for(rv1)).status_code == 200

    # A second reviewer cannot draw it, and cannot force it by hand either.
    rv2 = _reviewer()
    assert _draw_pair(rv2)["pair"] is None
    assert client.post(f"/api/asclepius/review/pair/{tid}", json=_adjudication(),
                       headers=A.headers_for(rv2)).status_code == 409
    assert len(store.reviews_for_task(tid)) == 1


# ═══ H3 — a third label is neither shown nor silently retired ════════════════
def test_a_third_label_is_never_marked_reviewed_by_someone_who_did_not_see_it():
    """H3. The pair queue gated on ``>= 2`` and ``ab_pair`` truncated to two in
    Python, so a third physician's label was invisible to the reviewer — and
    then marked ``reviewed`` by that reviewer anyway. That is paid work retired
    by someone who never read it, excluded from both queues, adjudicated never."""
    store = asc_store.get_store()
    admin_h = _admin_h()
    tid = _create_task(admin_h, max_labels=3)
    for _ in range(3):
        tl = _labeler()
        _reveal(tid, tl); _submit(tid, tl)

    subs = [s for s in store.submissions_for_task(tid) if s.get("verdict")]
    assert len(subs) == 3

    # The paired review is defined over exactly two labels. It must not serve a
    # three-label case at all rather than serving two thirds of one.
    assert _draw_pair(_reviewer())["pair"] is None
    assert all(s.get("review_status") is None for s in
               store.submissions_for_task(tid)), "a label was retired unseen"

    # And the case is COUNTABLE rather than invisible — someone has to look at it.
    stats = client.get("/api/asclepius/review/stats", headers=A.headers_for(_reviewer()))
    assert stats.status_code == 200, stats.text
    assert stats.json()["over_labelled"] == 1


def test_adjudicating_a_pair_retires_exactly_the_two_labels_it_showed():
    store = asc_store.get_store()
    admin_h = _admin_h()
    tid = _paired_task(admin_h)
    rv = _reviewer()
    _draw_pair(rv)
    assert client.post(f"/api/asclepius/review/pair/{tid}", json=_adjudication(),
                       headers=A.headers_for(rv)).status_code == 200

    reviewed = [s for s in store.submissions_for_task(tid)
                if s.get("review_status") == "reviewed"]
    assert len(reviewed) == 2


# ═══ M4 / M5 / the incident switch ═══════════════════════════════════════════
_STATS_SQL = """
                SELECT
                  SUM(CASE WHEN lc = 1 THEN 1 ELSE 0 END) AS awaiting_second,
                  SUM(CASE WHEN lc = 2 AND rc = 0 THEN 1 ELSE 0 END) AS review_ready,
                  SUM(CASE WHEN rc > 0 THEN 1 ELSE 0 END) AS adjudicated,
                  -- Audit R H3: a case carrying more than two labels cannot be
                  -- adjudicated by a PAIRED review. Counted, not dropped — the
                  -- whole failure was that this work was invisible.
                  SUM(CASE WHEN lc > 2 AND rc = 0 THEN 1 ELSE 0 END) AS over_labelled,
                  -- Audit R M5: terminal, never adjudicated, nobody notified.
                  SUM(CASE WHEN rs = 'reviewed' AND rc = 0 THEN 1 ELSE 0 END) AS parked
                FROM (
                  SELECT COALESCE(c.n_labels, 0) AS lc,
                         COALESCE(r.n_reviews, 0) AS rc,
                         t.review_status AS rs
                  FROM tasks t
                  LEFT JOIN (SELECT task_id, COUNT(*) AS n_all, SUM(CASE WHEN verdict IS NOT NULL THEN 1 ELSE 0 END) AS n_labels FROM submissions GROUP BY task_id) c ON c.task_id = t.task_id
                  LEFT JOIN (SELECT task_id, COUNT(*) AS n_reviews
                               FROM case_reviews GROUP BY task_id) r
                         ON r.task_id = t.task_id
                  WHERE t.status IN ('open', 'done')
                )
                """


def test_the_header_stats_do_not_scan_the_task_table_per_row():
    """M4. A correlated subquery per task, over the whole table, with no limit,
    backing a header refreshed on every draw."""
    store = asc_store.get_store()
    with store._conn() as conn:
        plan = " | ".join(
            dict(r)["detail"] for r in conn.execute(
                "EXPLAIN QUERY PLAN " + _STATS_SQL).fetchall()).upper()
    assert "CORRELATED SCALAR SUBQUERY" not in plan, plan
    assert "MATERIALIZE" in plan, plan


def test_a_parked_case_is_countable_rather_than_only_logged():
    """M5. A case refused as not-independent is set terminal and logged at ERROR.
    Two physicians' paid labels are stranded in it and no surface read that log."""
    store = asc_store.get_store()
    admin_h = _admin_h()
    tid = _paired_task(admin_h)
    store.mark_task_reviewed(tid)          # what the draw does on a refusal

    stats = client.get("/api/asclepius/review/stats",
                       headers=A.headers_for(_reviewer())).json()
    assert stats["parked"] == 1
    assert stats["adjudicated"] == 0, "a parked case must not read as adjudicated"


def test_one_flag_halts_double_labeling_whatever_else_is_set(monkeypatch):
    """The audit's note on open question #3: lowering the rate produces an
    OSCILLATION, not a reduction — the queue passes ``specialty_n=None`` and
    sheds, while the sweep passes it and re-flags what the queue declined, since
    ``specialty_n < 30`` routes unconditionally. The incident switch is one flag
    checked ahead of all three predicates."""
    from asclepius import agreement as AG

    monkeypatch.setenv("ASCLEPIUS_DOUBLE_LABEL_RATE", "0")
    # Without the halt, the per-specialty rule still routes everything.
    assert AG.should_double_label({"specialty": "new"}, current_rate=0.9, specialty_n=5) is True
    assert AG.should_double_label({"case_source": "real_deid"}, current_rate=0.9) is True

    monkeypatch.setenv("ASCLEPIUS_DOUBLE_LABEL_HALT", "1")
    assert AG.double_label_halted() is True
    assert AG.double_label_rate() == 0.0
    assert AG.should_double_label({"specialty": "new"}, current_rate=0.0, specialty_n=5) is False
    assert AG.should_double_label({"case_source": "real_deid"}, current_rate=0.0) is False
    assert asc_routing.second_label_is_default() is False
    # And a singly-labelled case stops asking for a partner.
    assert asc_routing.phase({"max_labels": 1, "difficulty": "medium"}, 1, 0) == \
        asc_routing.REVIEW_READY


# ═══ M2 — an adjudication commits, or it does not ════════════════════════════
def test_an_adjudication_that_fails_partway_leaves_no_half_review():
    """M2. This spanned five independent transactions: insert the review, stamp
    the pair columns, mark the task reviewed, retire each submission. A crash in
    the middle left a ``case_reviews`` row that COUNTS in ``review_acceptance``
    with NULL pair columns, beside a task still ``in_review`` — which, after the
    lease expires, reproduces H2 deterministically.

    Fails the write at the last step and asserts nothing survived."""
    store = asc_store.get_store()
    admin_h = _admin_h()
    tid = _paired_task(admin_h)
    rv = _reviewer()
    _draw_pair(rv)
    subs = [s for s in store.submissions_for_task(tid) if s.get("verdict")]

    class _Boom(Exception):
        pass

    # A connection that dies on the LAST write of the adjudication — the retire
    # step. `sqlite3.Connection` is immutable, so the failure is injected by
    # wrapping the store's connection factory rather than patching the driver.
    class _FlakyConn:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, *a, **kw):
            if "review_status" in str(sql) and "UPDATE submissions" in str(sql):
                raise _Boom("disk gave out mid-adjudication")
            return self._real.execute(sql, *a, **kw)

        def __getattr__(self, name):
            return getattr(self._real, name)

        def __enter__(self):
            self._real.__enter__()
            return self

        def __exit__(self, *exc):
            return self._real.__exit__(*exc)

    orig_conn = store._conn
    store._conn = lambda: _FlakyConn(orig_conn())
    try:
        with pytest.raises(_Boom):
            store.insert_pair_review(
                task_id=tid, reviewer_user_id=rv["id"], reviewer_id_hashed="h",
                verdict="accept", stronger="A",
                stronger_submission_id=subs[0]["submission_id"],
                pair_sub_a=subs[0]["submission_id"], pair_sub_b=subs[1]["submission_id"],
                accepted_submission_id=subs[0]["submission_id"],
                dimensions={k: "agree" for k in asc_review.DIMENSION_KEYS},
                blinded=True,
            )
    finally:
        store._conn = orig_conn

    # Nothing half-applied: no review row that would count in the denominator...
    assert store.reviews_for_task(tid) == []
    # ...and no submission retired by an adjudication that never landed.
    assert all(s.get("review_status") is None
               for s in store.submissions_for_task(tid))


def test_a_committed_adjudication_lands_whole():
    """The other half of M2: one call, and every consequence of it is present."""
    store = asc_store.get_store()
    admin_h = _admin_h()
    tid = _paired_task(admin_h)
    rv = _reviewer()
    _draw_pair(rv)
    assert client.post(f"/api/asclepius/review/pair/{tid}", json=_adjudication(),
                       headers=A.headers_for(rv)).status_code == 200

    row = store.reviews_for_task(tid)[0]
    assert row["pair_sub_a"] and row["pair_sub_b"] and row["stronger"]
    assert store.task_review_claim(tid)["status"] == "reviewed"
    assert all(s.get("review_status") == "reviewed"
               for s in store.submissions_for_task(tid) if s.get("verdict"))


def test_both_review_writers_produce_the_same_row_shape():
    """``insert_pair_review`` writes the case_reviews row itself so the whole
    adjudication is one transaction. That means two writers for one table, so
    the shapes are asserted equal here rather than assumed."""
    store = asc_store.get_store()
    admin_h = _admin_h()

    single_tid = _create_task(admin_h)
    tl = _labeler()
    _submit(single_tid, tl)
    single = store.insert_case_review(
        task_id=single_tid, submission_id="s-x", reviewer_user_id="u-x",
        reviewer_id_hashed="h", verdict="accept",
        dimensions={k: "agree" for k in asc_review.DIMENSION_KEYS})

    paired_tid = _paired_task(admin_h)
    rv = _reviewer()
    _draw_pair(rv)
    subs = [s for s in store.submissions_for_task(paired_tid) if s.get("verdict")]
    paired = store.insert_pair_review(
        task_id=paired_tid, reviewer_user_id=rv["id"], reviewer_id_hashed="h",
        verdict="accept", stronger="A",
        stronger_submission_id=subs[0]["submission_id"],
        pair_sub_a=subs[0]["submission_id"], pair_sub_b=subs[1]["submission_id"],
        accepted_submission_id=subs[0]["submission_id"],
        dimensions={k: "agree" for k in asc_review.DIMENSION_KEYS}, blinded=True)

    assert set(single) == set(paired)
    # And the paired row is countable by the ONE acceptance definition.
    assert asc_agreement.review_acceptance([single, paired])["n"] == 2


# ═══ blinding ════════════════════════════════════════════════════════════════
def test_a_labelers_name_seeded_in_their_free_text_is_not_served():
    """The vector PRD R §2.2 names: the whitelist serves labeler-authored prose,
    and two answers side by side make a signed one far easier to attribute.

    'Not served' is the requirement, so the name is REDACTED rather than merely
    measured. Recording ``blinded = 0`` and serving the name anyway satisfies the
    statistic while the reviewer has already read it — the adjudication is biased
    before κ ever gets to exclude the observation."""
    store = asc_store.get_store()
    admin_h = _admin_h()
    tl1 = _labeler(email="marguerite.okonkwo@hospital.example.com")
    tid = _create_task(admin_h)
    # Seeded two ways: as they'd sign a note, and as their address spells it.
    _submit(tid, tl1, scratch="Per my usual approach — Marguerite Okonkwo, nephrology.",
            critique="marguerite.okonkwo would not dose it that way")
    _submit(tid, _labeler())

    got = _draw_pair(_reviewer())["pair"]
    assert got is not None
    blob = repr(got).lower()
    assert "okonkwo" not in blob
    assert "marguerite.okonkwo" not in blob
    # The clinical content survives; only the signature is gone, and visibly so.
    assert asc_review.REDACTION_MARKER in repr(got)
    assert "usual approach" in blob

    # Derived from the payload actually served — which, post-redaction, IS blind.
    # An honest True here is what keeps the observation inside κ's denominator.
    assert got["blinded"] is True
    assert store.task_review_claim(tid)["blinded"] is True
    # The redaction is logged so someone can look at it — without the strings.
    events = [e for e in store.list_events(entity_type="task", entity_id=tid)
              if e.get("event_type") == "review_pair_redacted"]
    assert len(events) == 1
    assert "okonkwo" not in repr(events).lower()


def _with_deadline(seconds, fn, *args, **kw):
    """Run ``fn`` under a hard wall-clock deadline, raising if it overruns.

    A plain ``assert`` cannot catch a non-terminating loop — the test never gets
    to run it. SIGALRM interrupts the interpreter inside the loop itself, so a
    hang FAILS instead of taking the suite (and, in production, the worker) with
    it. That is exactly the failure mode under test."""
    if not hasattr(signal, "SIGALRM"):        # pragma: no cover - Unix in CI
        pytest.skip("SIGALRM is required to bound a non-terminating call")

    def _boom(signum, frame):
        raise TimeoutError(f"did not return within {seconds}s")

    old = signal.signal(signal.SIGALRM, _boom)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        return fn(*args, **kw)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


# Every 4-, 5- and 6-gram of the marker the redaction writes. If the scan
# restarts from zero after a substitution it re-finds the needle *inside the
# marker it just wrote*, and appends forever.
_MARKER_NGRAMS = sorted({
    asc_review.REDACTION_MARKER[i:i + n]
    for n in (4, 5, 6)
    for i in range(len(asc_review.REDACTION_MARKER) - n + 1)
    if asc_review.REDACTION_MARKER[i:i + n].isalpha()
})


@pytest.mark.parametrize("needle", _MARKER_NGRAMS)
def test_redaction_terminates_on_every_substring_of_its_own_marker(needle):
    """C1. The marker contains ordinary English fragments — 'dent', 'tail',
    'move', 'remo'. A physician signing up as ``remo@mercy.org``, or with a
    surname containing 'dent', is enough to make the substitution re-find itself.

    This runs on the reviewer draw path, synchronously, BEFORE the claim: a hang
    here pins a worker at 100% CPU and allocates without bound until the
    container OOMs, and every later draw on that worker dies with it."""
    text = f"Signed, {needle} of Mercy. Start calcium gluconate."
    # 2s is enormous for a single-pass substitution over one short string, and
    # keeps a regression from costing the suite three minutes to report.
    out, hit = _with_deadline(
        2.0, asc_review.redact_identity,
        {"answers": [{"answer": {"from_scratch": {"ideal_answer": text}}}]},
        [{"organization": needle}],
    )
    served = repr(out)
    assert asc_review.REDACTION_MARKER in served
    assert "calcium gluconate" in served, "the clinical content did not survive"
    # Bounded output: one marker per hit, never a marker per marker.
    assert len(served) < len(text) + 4 * len(asc_review.REDACTION_MARKER)


def test_redaction_output_length_is_bounded_by_the_hits():
    """The growth half of C1: even when it terminated, an output that grows with
    each pass is the same defect with a smaller exponent."""
    needle = "okonkwo"
    text = " ".join([needle] * 20)
    out, hit = _with_deadline(
        5.0, asc_review.redact_identity, {"t": text}, [{"organization": needle}])
    assert out["t"] == " ".join([asc_review.REDACTION_MARKER] * 20)
    assert hit == [needle]


def test_a_labeler_whose_name_collides_with_the_marker_can_still_be_reviewed():
    """The production path, end to end. Before the fix this GET never returns."""
    admin_h = _admin_h()
    tl1 = _labeler(email="remo@mercy.example.com")
    tid = _create_task(admin_h)
    _submit(tid, tl1, scratch="Per remo's usual approach, calcium first.")
    _submit(tid, _labeler())

    got = _with_deadline(10.0, _draw_pair, _reviewer())["pair"]
    assert got is not None and got["task_id"] == tid
    assert "calcium first" in repr(got)


def test_redaction_never_fires_on_clinical_prose_that_merely_looks_like_a_name():
    """The needles are this account's actual strings, never a general name
    detector. A heuristic that shredded 'per Osler's sign' would silently shrink
    κ's n and corrupt the clinical text two physicians were paid for."""
    view = {"answers": [{"answer": {"from_scratch": {
        "ideal_answer": "Check for Osler nodes; Chvostek sign negative."}}}]}
    out, hit = asc_review.redact_identity(
        view, [{"email": "j.smith@hospital.example.com", "full_name": "Jane Smith"}])
    assert hit == []
    assert out == view


def test_the_pair_payload_carries_no_structural_first_or_second_tell():
    admin_h = _admin_h()
    _paired_task(_admin_h() if False else admin_h)
    got = _draw_pair(_reviewer())["pair"]
    blob = repr(got)
    for leak in ("evaluator_id", "submitted_at", "submission_id", "annotator",
                 "id_hashed", "created_at", "portal_version"):
        assert leak not in blob, f"{leak} leaked into the pair payload"


def test_identity_keys_anywhere_in_either_payload_break_blinding():
    view = asc_review.blinded_pair_view(
        {"task_id": "t1", "prompt": "p"},
        {"verdict": "A_better", "payload": {"from_scratch": {"ideal_answer": "x"}}},
        # 'npi' rather than 'evaluator_id': the metadata scrub already strips
        # ordering/identity bookkeeping keys, so this asserts the SECOND line of
        # defence — an identity marker the scrub does not know about.
        {"verdict": "B_better", "payload": {"from_scratch": {"ideal_answer": "y",
                                                            "npi": "1999999999"}}},
    )
    assert asc_review.pair_is_blinded(view, reviewer_role="evaluator") is False
    # An admin is never blind regardless of the payload: require_reviewer admits
    # admins, and an admin can de-blind through GET /submissions/{id}.
    clean = asc_review.blinded_pair_view(
        {"task_id": "t1"}, {"verdict": "A_better"}, {"verdict": "B_better"})
    assert asc_review.pair_is_blinded(clean, reviewer_role="evaluator") is True
    assert asc_review.pair_is_blinded(clean, reviewer_role="admin") is False


# ═══ the verdict ═════════════════════════════════════════════════════════════
def test_accept_with_edits_without_corrections_is_a_400():
    admin_h = _admin_h()
    tid = _paired_task(admin_h)
    rv = _reviewer()
    _draw_pair(rv)

    r = client.post(
        f"/api/asclepius/review/pair/{tid}",
        json=_adjudication(verdict="accept_with_edits", accepted_side=None, corrections={}),
        headers=A.headers_for(rv))
    assert r.status_code == 400, r.text
    assert any("corrections" in e for e in r.json()["detail"]["errors"])


def test_reject_both_without_corrections_is_a_400():
    admin_h = _admin_h()
    tid = _paired_task(admin_h)
    rv = _reviewer()
    _draw_pair(rv)

    r = client.post(f"/api/asclepius/review/pair/{tid}",
                    json=_adjudication(verdict="reject", stronger="equivalent",
                                       accepted_side=None),
                    headers=A.headers_for(rv))
    assert r.status_code == 400, r.text


def test_which_is_stronger_is_required():
    body = _adjudication()
    body.pop("stronger")
    assert any("stronger" in e for e in asc_review.validate_pair_review_payload(body))
    assert asc_review.validate_pair_review_payload(_adjudication(stronger="best")) != []
    # 'equivalent' is a real answer, not a cop-out.
    assert asc_review.validate_pair_review_payload(
        _adjudication(stronger="equivalent", verdict="accept", accepted_side="B")) == []


def test_an_acceptance_must_name_whose_work_is_accepted():
    assert any("accepted side" in e for e in
               asc_review.validate_pair_review_payload(_adjudication(accepted_side=None)))
    assert any("no side" in e for e in asc_review.validate_pair_review_payload(
        _adjudication(verdict="reject", corrections={"notes": "both wrong"},
                      accepted_side="A")))


def test_submitting_without_a_claim_is_a_409():
    admin_h = _admin_h()
    tid = _paired_task(admin_h)
    r = client.post(f"/api/asclepius/review/pair/{tid}",
                    json=_adjudication(), headers=A.headers_for(_reviewer()))
    assert r.status_code == 409


def test_another_reviewers_claim_cannot_be_evicted_by_a_hand_crafted_post():
    admin_h = _admin_h()
    tid = _paired_task(admin_h)
    holder = _reviewer()
    _draw_pair(holder)
    r = client.post(f"/api/asclepius/review/pair/{tid}",
                    json=_adjudication(), headers=A.headers_for(_reviewer()))
    assert r.status_code == 409


# ═══ the statistics (PRD R §2.4) ═════════════════════════════════════════════
def test_kappa_is_computable_over_the_two_labels_and_acceptance_is_separate():
    """The two numbers answer two different buyer questions and are never
    interchangeable. κ compares the two LABELERS; expert acceptance is the
    reviewer's verdict."""
    store = asc_store.get_store()
    admin_h = _admin_h()
    tid = _paired_task(admin_h, verdicts=("A_better", "A_better"))
    rv = _reviewer()
    _draw_pair(rv)
    r = client.post(f"/api/asclepius/review/pair/{tid}",
                    json=_adjudication(), headers=A.headers_for(rv))
    assert r.status_code == 200, r.text

    # κ: one observation per double-labelled task, over the two LABELS.
    obs = store.get_agreement_observation(tid)
    assert obs is not None
    assert obs["verdict_a"] == "A_better" and obs["verdict_b"] == "A_better"
    kappa = asc_agreement.independent_kappa(store.list_agreement_observations(), min_n=1)
    assert kappa["n"] == 1 and kappa["overall"] is not None

    # Expert acceptance: the REVIEWER's verdict, its own name, its own function.
    acceptance = asc_agreement.review_acceptance(store.reviews_for_task(tid))
    assert acceptance["n"] == 1 and acceptance["accept_rate"] == 1.0
    assert "kappa" not in acceptance and "overall" not in acceptance
    assert "accept_rate" not in kappa


# ═══ C2 — κ's blinding flag must be a MEASUREMENT ════════════════════════════
# ``upsert_agreement`` defaulted ``blinded=True`` and its only caller never
# passed it, so every observation was stamped blind and ``_blinded_only`` was a
# permanent no-op. Two buyer-facing artifacts inherited that: quality_report.md
# printed "unblinded observations excluded: 0" unconditionally, and every
# packaged record shipped ``independent_second_label`` on the strength of an
# observation merely EXISTING. Moving the double-label rate 0.15 → 1.0 took that
# from ~15% of the dataset to 100% of it.
def test_blinding_on_a_kappa_observation_is_measured_not_asserted():
    """Both labelers committed a blind independent answer before either saw
    anything else. That is evidence, and it is what licenses ``blinded = 1``."""
    store = asc_store.get_store()
    admin_h = _admin_h()
    tid = _create_task(admin_h)
    tl1, tl2 = _labeler(), _labeler()
    _reveal(tid, tl1); _submit(tid, tl1)
    _reveal(tid, tl2); _submit(tid, tl2)

    obs = store.get_agreement_observation(tid)
    assert obs is not None
    assert obs["blinded"] == 1

    kappa = asc_agreement.independent_kappa(store.list_agreement_observations(), min_n=1)
    assert kappa["n"] == 1 and kappa["excluded_unverified"] == 0


def test_an_unattested_pair_is_excluded_from_kappa_rather_than_asserted_blind():
    """No blind commit on record for either labeler, so nothing attests that the
    second read was independent. NULL — 'not verified' — never 1.

    This is the whole point: below, ``excluded_unverified`` is non-zero, which
    it could not be while the flag was a constant."""
    store = asc_store.get_store()
    admin_h = _admin_h()
    tid = _create_task(admin_h)
    _submit(tid, _labeler())          # submitted without passing the reveal gate
    _submit(tid, _labeler())

    obs = store.get_agreement_observation(tid)
    assert obs is not None
    assert obs["blinded"] is None, "an unverified observation was asserted blind"

    kappa = asc_agreement.independent_kappa(store.list_agreement_observations(), min_n=1)
    assert kappa["n"] == 0
    assert kappa["excluded_unverified"] == 1
    assert kappa["overall"] is None


def test_one_attested_labeler_is_not_enough():
    """κ is a statement about the PAIR. One physician's blind commit says nothing
    about whether the other read independently."""
    store = asc_store.get_store()
    admin_h = _admin_h()
    tid = _create_task(admin_h)
    tl1, tl2 = _labeler(), _labeler()
    _reveal(tid, tl1); _submit(tid, tl1)
    _submit(tid, tl2)                 # no commit for the second read

    assert store.get_agreement_observation(tid)["blinded"] is None


def test_the_packaged_record_does_not_claim_an_independence_it_cannot_show():
    """``packaging.supervision_block`` and the export case bundle already gate on
    ``blinded in (True, 1)`` — correctly. They were dishonest only because the
    value they gate on was a constant. Fixing the source fixes both."""
    from asclepius import packaging as asc_packaging

    store = asc_store.get_store()
    admin_h = _admin_h()
    attested = _create_task(admin_h)
    a1, a2 = _labeler(), _labeler()
    _reveal(attested, a1); _submit(attested, a1)
    _reveal(attested, a2); _submit(attested, a2)

    bare = _create_task(admin_h)
    _submit(bare, _labeler()); _submit(bare, _labeler())

    good = asc_packaging.supervision_block(
        labeler_id_hashed="h", observation=store.get_agreement_observation(attested))
    unattested = asc_packaging.supervision_block(
        labeler_id_hashed="h", observation=store.get_agreement_observation(bare))
    assert good["independent_second_label"] is True
    assert unattested["independent_second_label"] is False


def test_the_paired_verdict_never_falls_out_of_the_acceptance_denominator():
    """The trap this design exists to avoid: an 'accept_a' verdict token would
    be unrecognized by ``review_acceptance``, land in n_unclassified, and read as
    a 0% acceptance rate while appearing nowhere. Accept-A is ONE verdict plus a
    side."""
    store = asc_store.get_store()
    admin_h = _admin_h()
    for side in ("A", "B"):
        tid = _paired_task(admin_h)
        rv = _reviewer()
        _draw_pair(rv)
        client.post(f"/api/asclepius/review/pair/{tid}",
                    json=_adjudication(stronger=side, accepted_side=side),
                    headers=A.headers_for(rv))
    reviews = [r for t in store.list_tasks() for r in store.reviews_for_task(t["task_id"])]
    acceptance = asc_agreement.review_acceptance(reviews)
    assert acceptance["n"] == 2 and acceptance["n_unclassified"] == 0
    assert acceptance["accept_rate"] == 1.0


def test_a_pair_never_reaches_the_single_submission_review_queue():
    """PRD R §1, defect 1: the old queue served one labeler's work out of a pair,
    which both double-serves the case and destroys the comparison."""
    store = asc_store.get_store()
    admin_h = _admin_h()
    tid = _paired_task(admin_h)
    rv = _reviewer()

    r = client.get("/api/asclepius/review/next", headers=A.headers_for(rv))
    assert r.status_code == 200, r.text
    assert r.json()["submission"] is None
    assert store.next_review_for(rv["id"], specialty="nephrology") is None
    # ...and the paired queue does serve it.
    assert _draw_pair(rv)["pair"]["task_id"] == tid


def test_a_case_awaiting_its_second_label_is_not_reviewable_at_all():
    admin_h = _admin_h()
    tid = _create_task(admin_h)
    _submit(tid, _labeler())
    # Serving it to a labeler lifts max_labels to 2 — now it is explicitly a
    # pair-in-progress and neither review queue may touch it.
    client.get("/api/asclepius/tasks/next", headers=A.headers_for(_labeler()))
    assert asc_store.get_store().get_task(tid)["max_labels"] == 2

    rv = _reviewer()
    assert client.get("/api/asclepius/review/next",
                      headers=A.headers_for(rv)).json()["submission"] is None
    assert _draw_pair(rv)["pair"] is None


# ═══ Phase 3 — the session hook, and its boundary ════════════════════════════
def test_drawing_a_pair_returns_a_session_slot():
    """R calls open_session and forwards what P returns. Before Agent P merges
    that is None — the page renders no countdown, which is visibly missing
    rather than silently wrong."""
    admin_h = _admin_h()
    _paired_task(admin_h)
    body = _draw_pair(_reviewer())
    assert "session" in body


def test_the_session_is_forwarded_opaquely_when_payments_is_present(monkeypatch):
    """The whole integration, proven end to end with a stand-in for P: whatever
    open_session returns reaches the client unmodified."""
    from routers import asclepius_review as rr

    calls = {}

    class _FakePayments:
        @staticmethod
        def open_session(store, *, user_id, kind):
            calls["kind"] = kind
            calls["user_id"] = user_id
            return {"session_id": "ws-1", "min_seconds": 1200, "credited_seconds": 0,
                    "nonce": "n1", "qualified": False, "started_at": "2026-01-01T00:00:00Z"}

    monkeypatch.setattr(rr, "_asc_payments", _FakePayments)
    admin_h = _admin_h()
    _paired_task(admin_h)
    rv = _reviewer()
    body = _draw_pair(rv)

    assert calls == {"kind": "review", "user_id": rv["id"]}
    assert body["session"]["session_id"] == "ws-1"
    # Opaque: every key P returned survives, because R never enumerates them.
    assert set(body["session"]) == {"session_id", "min_seconds", "credited_seconds",
                                    "nonce", "qualified", "started_at"}


def test_a_failing_payments_module_never_blocks_a_physician_from_reviewing(monkeypatch):
    from routers import asclepius_review as rr

    class _Broken:
        @staticmethod
        def open_session(store, **kw):
            raise RuntimeError("payments is down")

    monkeypatch.setattr(rr, "_asc_payments", _Broken)
    admin_h = _admin_h()
    tid = _paired_task(admin_h)
    body = _draw_pair(_reviewer())
    assert body["pair"]["task_id"] == tid and body["session"] is None


def test_the_review_surface_never_crosses_into_agent_ps_territory():
    """PRD R §4 / §6.5. If this code contains the number 1200 or the string
    'qualified', it has crossed the boundary."""
    forbidden = ("work_sessions", "session_beats", "credited_seconds", "earnings")
    for rel in ("asclepius/review.py", "asclepius/routing.py",
                "routers/asclepius_review.py"):
        src = (_BACKEND / rel).read_text(encoding="utf-8")
        for needle in forbidden:
            assert needle not in src, f"{rel} references {needle!r}"

    import re
    router_src = (_BACKEND / "routers" / "asclepius_review.py").read_text(encoding="utf-8")
    # Exactly one thing is called on the payments module, and it is open_session.
    assert set(re.findall(r"_asc_payments\.(\w+)", router_src)) == {"open_session"}
    # The policy modules never reach for it at all — no import, no attribute.
    for rel in ("asclepius/review.py", "asclepius/routing.py"):
        src = (_BACKEND / rel).read_text(encoding="utf-8")
        assert not re.search(r"^\s*(from|import)\s.*payments", src, re.M)
        assert not re.search(r"payments\s*\.\s*\w", src)


def test_the_page_renders_the_countdown_from_the_api_and_never_invents_it():
    """§5: the countdown value comes from the API response. A client-side clock
    that disagreed with the server's would be telling a physician they had
    earned time they had not."""
    src = _REVIEW_JS.read_text(encoding="utf-8")
    assert "session" in src
    # No hardcoded session length anywhere in the page.
    assert "1200" not in src
