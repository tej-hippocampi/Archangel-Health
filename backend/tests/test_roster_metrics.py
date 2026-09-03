"""Per-physician speed and agreement on the admin roster (Task Pipeline PRD §C).

Both numbers already existed in the backend and neither reached a screen, so
"who is doing good work" was answerable only by opening physicians one at a
time. Putting them on the roster is easy; putting them there honestly is the
part these tests hold: unknown must render as unknown rather than as zero, the
per-physician kappa must be computed over the same pool the reported aggregate
uses, and the roster must not grow a query per row on the way.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from tests import _asclepius as A  # noqa: E402
from asclepius import agreement as AG  # noqa: E402

client = TestClient(A.app)
ROSTER = "/api/asclepius/admin/physicians"


def _store():
    from asclepius.store import get_store
    return get_store()


@pytest.fixture(autouse=True)
def _isolated():
    A.fresh_store()
    yield


def _approved_doc(store, *, specialty="cardiology"):
    user = A.make_user(store, specialty=specialty, tier="labeler")
    with store._conn() as conn:                                  # noqa: SLF001
        conn.execute("UPDATE users SET verification_status = 'approved' WHERE id = ?",
                     (user["id"],))
    return store.get_user_by_id(user["id"])


_SUB_SEQ = {"n": 0}


def _submit(store, *, task_id, user_id, seconds):
    _SUB_SEQ["n"] += 1
    return store.insert_submission(
        submission_id=f"s-{_SUB_SEQ['n']}-{A.uniq()}",
        task_id=task_id, evaluator_id=user_id, verdict="a",
        chosen_id=None, rejected_id=None, confidence="high",
        time_spent_sec=seconds, payload={}, annotator={}, dedupe_hash=None)


def _observation(store, *, task_id, a_id, b_id, va, vb, blinded=True,
                 excluded=None, specialty="cardiology"):
    """One double-labeled agreement row, written through the real path so the
    submission join this feature depends on actually has rows to join."""
    sa = _submit(store, task_id=task_id, user_id=a_id, seconds=100)
    sb = _submit(store, task_id=task_id, user_id=b_id, seconds=100)
    store.upsert_agreement(
        task_id=task_id, specialty=specialty,
        sub_a=sa["submission_id"], sub_b=sb["submission_id"],
        verdict_a=va, verdict_b=vb, tags_a=[], tags_b=[], jaccard_tags=1.0,
        verdict_agree=(va == vb), n_labels=2, flagged=False,
        blinded=blinded, kappa_excluded_reason=excluded or "")


# ═══ D6: unknown renders as unknown ═════════════════════════════════════════
def test_median_seconds_none_without_timed_submissions():
    """WHY: a zero reads as a fast physician, or as a broken one.

    Neither is what "we have not measured them yet" means, and on a roster the
    two are indistinguishable at a glance. Absent from the batch dict, null over
    the API, placeholder on the screen -- one meaning, carried the whole way.
    """
    store = _store()
    doc = _approved_doc(store)
    assert store.evaluator_median_seconds_by_user() == {}

    task = store.insert_task(prompt="c", specialty="cardiology")
    _submit(store, task_id=task["task_id"], user_id=doc["id"], seconds=0)
    assert store.evaluator_median_seconds_by_user() == {}, (
        "an untimed submission is not a measurement of speed")

    _submit(store, task_id=task["task_id"], user_id=doc["id"], seconds=300)
    assert store.evaluator_median_seconds_by_user()[doc["id"]] == 300.0


def test_the_batch_median_matches_the_per_user_one_row_for_row():
    """WHY: two definitions of "how long they take" that disagree by a row is
    the defect this codebase writes single-source helpers to avoid.

    The batch variant exists for cost, not for a different answer. If it ever
    diverges, the dossier and the roster show different numbers for the same
    physician and nobody can tell which is wrong.
    """
    store = _store()
    a, b = _approved_doc(store), _approved_doc(store)
    task = store.insert_task(prompt="c", specialty="cardiology")
    for secs in (30, 90, 120, 400):        # even count: the median is an average
        _submit(store, task_id=task["task_id"], user_id=a["id"], seconds=secs)
    for secs in (60, 61, 62):              # odd count: the median is a member
        _submit(store, task_id=task["task_id"], user_id=b["id"], seconds=secs)

    batch = store.evaluator_median_seconds_by_user()
    for who in (a, b):
        assert batch[who["id"]] == store.evaluator_median_seconds(who["id"])
    assert batch[a["id"]] == 105.0 and batch[b["id"]] == 61.0


# ═══ C1/C2: the same pool, or it is a different metric ══════════════════════
def test_kappa_none_below_min_n_and_matches_pool_gates(monkeypatch):
    """WHY: a per-physician kappa over rows the aggregate excludes would be a
    different statistic wearing the same name.

    The aggregate drops two kinds of row for two independent reasons: an
    observation not explicitly recorded as blinded (it may measure anchoring),
    and a trajectory observation carrying a kappa-pool exclusion (blinded, and
    still temporally dependent). A per-person number computed over either would
    be higher, more flattering, and not comparable to the figure a buyer audits.
    And below the minimum pair count it is None for the same reason the
    aggregate suppresses it: a kappa on three pairs is noise presented as
    measurement.
    """
    monkeypatch.setenv("ASCLEPIUS_KAPPA_MIN_N", "4")
    store = _store()
    a, b = _approved_doc(store), _approved_doc(store)

    def _pair(i, va, vb, **kw):
        t = store.insert_task(prompt=f"c{i}", specialty="cardiology")
        _observation(store, task_id=t["task_id"], a_id=a["id"], b_id=b["id"],
                     va=va, vb=vb, **kw)

    # Three eligible pairs: below the (monkeypatched) floor of four.
    for i, (va, vb) in enumerate([("a", "a"), ("b", "b"), ("a", "b")]):
        _pair(i, va, vb)
    out = store.evaluator_kappa_by_user()
    assert out[a["id"]]["n"] == 3
    assert out[a["id"]]["kappa"] is None, "three pairs is not a measurement"

    # Two INELIGIBLE rows. If either gate leaked, n would cross the floor and a
    # number would appear that the pooled kappa does not stand behind.
    _pair(10, "a", "a", blinded=False)
    _pair(11, "b", "b", excluded=AG.KAPPA_EXCLUSION_SEQUENTIAL)
    still = store.evaluator_kappa_by_user()
    assert still[a["id"]]["n"] == 3 and still[a["id"]]["kappa"] is None

    # A fourth ELIGIBLE pair reaches the floor and the number appears.
    _pair(12, "b", "a")
    now = store.evaluator_kappa_by_user()
    assert now[a["id"]]["n"] == 4
    assert now[a["id"]]["kappa"] is not None
    assert now[b["id"]]["n"] == 4, "both raters of a pair are measured by it"


def test_per_annotator_kappa_uses_the_same_helper_as_the_pooled_number():
    """WHY: reuse, so the two numbers cannot drift.

    On a set where every eligible pair involves the same two physicians, each
    physician's kappa IS the pooled kappa over that set. Anything else means the
    per-person path has grown its own arithmetic.
    """
    obs = [{"annotator_a": "u-a", "annotator_b": "u-b", "blinded": True,
            "verdict_a": v_a, "verdict_b": v_b}
           for v_a, v_b in [("a", "a"), ("a", "b"), ("b", "b"), ("b", "b"),
                            ("a", "a"), ("b", "a")]]
    per = AG.per_annotator_kappa(obs, min_n=1)
    pooled = AG.aggregate_kappa(obs, min_n=1)
    assert per["u-a"]["n"] == len(obs)
    assert per["u-a"]["kappa"] == pooled["overall"]


# ═══ C3/D5: the roster stays one query per metric ═══════════════════════════
def test_roster_endpoint_is_batch_not_per_row():
    """WHY: the roster's own comment already forbids a query per physician.

    ``contributor_score`` is read from a stored row rather than recomputed for
    exactly this reason, and speed and agreement are no different. A metric
    computed per row turns opening the Physicians screen into N round trips, and
    it degrades silently -- it is fast on the four physicians a developer has
    and slow on the two hundred production has.
    """
    store = _store()
    ah = A.headers_for(A.make_user(store, role="admin"))
    task = store.insert_task(prompt="c", specialty="cardiology")

    counts = []
    for target in (2, 12):
        while len([u for u in store.list_users() if u.get("role") == "evaluator"]) < target:
            doc = _approved_doc(store)
            _submit(store, task_id=task["task_id"], user_id=doc["id"], seconds=90)
        real_conn = type(store)._conn                            # noqa: SLF001
        hits = {"n": 0}

        def _counting(self, _real=real_conn, _hits=hits):
            _hits["n"] += 1
            return _real(self)

        type(store)._conn = _counting                            # noqa: SLF001
        try:
            r = client.get(ROSTER, headers=ah)
        finally:
            type(store)._conn = real_conn                        # noqa: SLF001
        assert r.status_code == 200, r.text
        assert len(r.json()["physicians"]) == target
        counts.append(hits["n"])

    assert counts[0] == counts[1], (
        f"roster query count grew with the roster: {counts[0]} then {counts[1]}")


def test_roster_carries_the_three_new_fields_and_never_a_zero_for_unmeasured():
    """WHY: the API is where None has to survive.

    A router that coalesced these to 0 would make the UI's placeholder rule
    unreachable, and every unmeasured physician would read as the worst one on
    the roster.
    """
    store = _store()
    ah = A.headers_for(A.make_user(store, role="admin"))
    measured, unmeasured = _approved_doc(store), _approved_doc(store)
    task = store.insert_task(prompt="c", specialty="cardiology")
    _submit(store, task_id=task["task_id"], user_id=measured["id"], seconds=240)

    rows = {r["id"]: r for r in client.get(ROSTER, headers=ah).json()["physicians"]}
    assert rows[measured["id"]]["median_seconds"] == 240.0
    assert rows[unmeasured["id"]]["median_seconds"] is None
    for row in rows.values():
        assert row["kappa"] is None, "nobody here has a kappa-eligible pair yet"
        assert "kappa_n" in row


def test_no_metric_is_ever_shown_to_a_physician():
    """WHY: C5 -- the internal-score rule extends to speed and agreement.

    A physician told they are slower than their colleagues starts optimising for
    the clock on work whose value is care taken. These are operator numbers, and
    the contributor-facing surfaces must not carry them.
    """
    store = _store()
    doc = _approved_doc(store)
    A.pass_practice_case(store, doc["id"])
    task = store.insert_task(prompt="c", specialty="cardiology")
    _submit(store, task_id=task["task_id"], user_id=doc["id"], seconds=240)

    body = client.get("/api/asclepius/me/stats", headers=A.headers_for(doc)).text
    for leaked in ("median_seconds", "kappa_n", '"kappa"'):
        assert leaked not in body, f"{leaked} reached a physician-facing response"

    frontend = Path(__file__).resolve().parent.parent.parent / "frontend" / "asclepius"
    for js in frontend.glob("*.js"):
        if js.name == "admin_physicians.js":
            continue
        assert "median_seconds" not in js.read_text(encoding="utf-8"), js.name
