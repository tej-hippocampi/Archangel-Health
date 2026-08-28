"""Two-tier review product tests (PRD A Phases 1–2).

Phase 1: schema + routing — reviewer cannot draw their own work, the second
labeler can never be the first labeler, NULL tier denies review access, and the
migration is guarded + re-runnable.

Phase 2: the review surface contract — the payload served to a reviewer carries
no labeler identity, validation rejects unusable reviews, and ``cannot_assess``
persists as its own value.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import review as asc_review  # noqa: E402
from asclepius import store as asc_store  # noqa: E402

client = TestClient(A.app)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    A.fresh_store()
    # Stub the two LLM legs of the submit pipeline so tests can walk the REAL
    # POST /submissions route (FIX A §1 rule 2: test the path, not the unit).
    from asclepius import pipeline as asc_pipeline

    async def _ok_critic(task, submission):
        return {"consistent": True, "issues": [], "skipped": True}

    async def _ok_grounding(task, submission):
        return {"grounding_ok": True, "issues": [], "skipped": True, "checked_anchors": 0}

    monkeypatch.setattr(asc_pipeline, "run_critic", _ok_critic)
    monkeypatch.setattr(asc_pipeline, "run_grounding_check", _ok_grounding)
    # ─── Why the halt flag is pinned for this whole file ─────────────────────
    # Most of this module tests the SINGLE-review flow: draw one submission,
    # claim it, POST a verdict onto it. PRD-R made double-labeling the default,
    # so a singly-labelled case now routes to the PAIR queue instead and the
    # single-submission POST correctly 409s with ``became_a_pair`` — before it
    # ever reaches the guard the test is about.
    #
    # Whether that happens was a COIN FLIP, and the file already says so at
    # ``test_reviewer_cannot_draw_own_submission``: the route triggers the
    # double-label sweep as a THROTTLED background task, so whether a case had
    # been flagged depended on how much wall-clock other tests had burned in the
    # same process. Twelve of the tests here could not pass alone at all, and one
    # of them surfaced as a red CI shard the first time an unrelated test file was
    # added and the shard packer reshuffled the suite.
    #
    # Pinning the flag is the same fix, for the same stated reason, that
    # ``test_payments_accrual`` already applies: it is the one supported way to
    # run without second labels. It does not weaken anything — these tests are
    # about who may review what, not about how a case reaches a reviewer. The
    # paired flow has its own module (``test_paired_review``), and the handful of
    # tests HERE that drive the sweep deliberately call it themselves and clear
    # the flag first.
    monkeypatch.setenv("ASCLEPIUS_DOUBLE_LABEL_HALT", "1")
    yield


@pytest.fixture
def paired_flow(monkeypatch):
    """Opt back INTO the PRD-R default for a test that is about the paired flow.

    The file-wide pin above is what makes the single-flow tests deterministic;
    a test that wants a case to become a pair asks for it explicitly here, so
    the two intentions are never confused for each other."""
    monkeypatch.delenv("ASCLEPIUS_DOUBLE_LABEL_HALT", raising=False)
    monkeypatch.delenv("ASCLEPIUS_DOUBLE_LABEL_RATE", raising=False)
    yield


def _grant_tier(store, user_id: str, tier: str) -> None:
    """Simulate the PRD-B tier assignment. The users table (and its ``tier``
    column) is owned by PRD B; until that migration ships, this applies the same
    guarded ALTER so the review gate can be exercised."""
    with store._conn() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "tier" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN tier TEXT")
        conn.execute("UPDATE users SET tier = ? WHERE id = ?", (tier, user_id))


def _mk_task(store, *, specialty="nephrology", case=None, max_labels=1, difficulty="medium"):
    return store.insert_task(
        prompt=f"Case prompt {A.uniq()}",
        specialty=specialty,
        difficulty=difficulty,
        candidate_answers=[{"id": "a", "text": "Answer A"}, {"id": "b", "text": "Answer B"}],
        max_labels=max_labels,
        case=case,
    )


def _mk_submission(store, task, user, *, verdict="A_better", confidence="high", payload=None):
    sid = f"sub-{A.uniq()}"
    return store.insert_submission(
        submission_id=sid,
        task_id=task["task_id"],
        evaluator_id=user["id"],
        verdict=verdict,
        chosen_id="a",
        rejected_id="b",
        confidence=confidence,
        time_spent_sec=180,
        payload=payload or {"verdict": verdict, "chosen_id": "a", "rejected_id": "b"},
        annotator={
            "id_hashed": user.get("id_hashed"),
            "specialty": user.get("specialty"),
            "years_experience": user.get("years_experience"),
            "credential": "MD, board-certified",
        },
        dedupe_hash=None,
        portal_version="v3",
    )


def _reviewer(store, *, specialty="nephrology"):
    user = A.make_user(store, role="evaluator", specialty=specialty)
    _grant_tier(store, user["id"], "reviewer")
    return store.get_user_by_id(user["id"])


def _review_body(verdict="accept", **overrides):
    body = {
        "verdict": verdict,
        "dimensions": {k: "agree" for k in asc_review.DIMENSION_KEYS},
        "time_spent_sec": 45,
    }
    body.update(overrides)
    return body


# ─── Phase 1: migration ───────────────────────────────────────────────────────
def test_migration_guarded_and_rerunnable(tmp_path):
    db = str(tmp_path / "review_migration.db")
    s1 = asc_store.AsclepiusStore(db_path=db)
    # Re-running the full init + migration against the same file must be a no-op.
    s2 = asc_store.AsclepiusStore(db_path=db)
    with s2._conn() as conn:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(submissions)").fetchall()]
        assert cols.count("review_status") == 1
        tables = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "case_reviews" in tables
        # No DEFAULT on the status column: NULL (undecided) must stay
        # distinguishable from any decided value (START_HERE §4).
        ddl_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='submissions'"
        ).fetchone()
        assert "review_status TEXT DEFAULT" not in (ddl_row["sql"] or "")
    # New submissions start with review_status NULL (not yet routed).
    labeler = A.make_user(s2)
    task = _mk_task(s2)
    sub = _mk_submission(s2, task, labeler)
    assert sub["review_status"] is None


# ─── Phase 1: access control ──────────────────────────────────────────────────
def test_null_tier_denies_review_access():
    store = asc_store.get_store()
    plain = A.make_user(store)  # evaluator, no tier column value at all
    r = client.get("/api/asclepius/review/next", headers=A.headers_for(plain))
    assert r.status_code == 403
    r = client.get("/api/asclepius/review/stats", headers=A.headers_for(plain))
    assert r.status_code == 403
    # can_review is deny-by-default for both NULL and non-reviewer tiers.
    assert asc_review.can_review({"tier": None}) is False
    assert asc_review.can_review({"tier": "labeler"}) is False
    assert asc_review.can_review({"tier": "reviewer"}) is True
    assert asc_review.can_review(None) is False


def test_reviewer_tier_grants_access():
    store = asc_store.get_store()
    reviewer = _reviewer(store)
    r = client.get("/api/asclepius/review/stats", headers=A.headers_for(reviewer))
    assert r.status_code == 200
    assert set(r.json()) >= {"unreviewed", "in_review", "reviewed", "n_reviews"}


# ─── Phase 1: the reviewer can never draw their own work ─────────────────────
def test_reviewer_cannot_draw_own_submission(paired_flow):
    store = asc_store.get_store()
    reviewer = _reviewer(store)
    task = _mk_task(store)
    _mk_submission(store, task, reviewer)  # the reviewer's OWN labeling work

    # A different reviewer draws it fine...
    other = _reviewer(store)
    drawn = store.next_review_for(other["id"], specialty="nephrology")
    assert drawn is not None and drawn["evaluator_id"] == reviewer["id"]

    # ...and its author never does, in SQL and through the route.
    assert store.next_review_for(reviewer["id"], specialty="nephrology") is None
    r = client.get("/api/asclepius/review/next", headers=A.headers_for(reviewer))
    assert r.status_code == 200
    assert r.json()["submission"] is None

    # PRD R §1: the other-reviewer draw is asserted BEFORE the route call on
    # purpose. A draw triggers the double-label routing sweep, which under the
    # new flow flags this task for a second label — at which point the case has
    # correctly left the single-submission queue for the paired one
    # (test_paired_review.py). The self-review wall is what this test is about,
    # and it holds either way. The sweep is invoked EXPLICITLY below rather than
    # asserted on the route's background task, whose throttle makes it a coin
    # flip and would put a race straight back into this suite.
    asc_review.sweep_double_label_routing(store)
    assert store.get_task(task["task_id"])["max_labels"] == 2
    assert store.next_review_for(other["id"], specialty="nephrology") is None


def test_own_submission_post_is_403_even_by_hand():
    store = asc_store.get_store()
    reviewer = _reviewer(store)
    task = _mk_task(store)
    sub = _mk_submission(store, task, reviewer)
    r = client.post(
        f"/api/asclepius/review/{sub['submission_id']}",
        json=_review_body(),
        headers=A.headers_for(reviewer),
    )
    assert r.status_code == 403


# ─── Phase 1: claims + lease ──────────────────────────────────────────────────
def test_draw_claims_submission_and_stale_lease_requeues():
    store = asc_store.get_store()
    labeler = A.make_user(store)
    task = _mk_task(store)
    sub = _mk_submission(store, task, labeler)

    r1 = _reviewer(store)
    drawn = client.get("/api/asclepius/review/next", headers=A.headers_for(r1)).json()
    assert drawn["submission"]["submission_id"] == sub["submission_id"]
    claimed = store.get_submission(sub["submission_id"])
    assert claimed["review_status"] == "in_review"

    # While the claim lease holds, another reviewer draws nothing.
    r2 = _reviewer(store)
    assert store.next_review_for(r2["id"], specialty="nephrology") is None

    # An abandoned claim re-queues after the lease expires — the submission must
    # not vanish from the worklist forever (PRD A §4 trap).
    # The lease clock is review_claimed_at, NOT updated_at: an unrelated write
    # must NOT extend or expire a reviewer's claim (FIX A A-3.7).
    store.update_submission(sub["submission_id"], qa_reason="unrelated pipeline write")
    assert store.next_review_for(r2["id"], specialty="nephrology") is None
    with store._conn() as conn:
        conn.execute(
            "UPDATE submissions SET review_claimed_at = '2000-01-01T00:00:00' "
            "WHERE submission_id = ?",
            (sub["submission_id"],),
        )
    stale_drawn = store.next_review_for(r2["id"], specialty="nephrology")
    assert stale_drawn is not None and stale_drawn["submission_id"] == sub["submission_id"]


def test_reviewed_submission_leaves_the_queue():
    store = asc_store.get_store()
    labeler = A.make_user(store)
    task = _mk_task(store)
    sub = _mk_submission(store, task, labeler)
    reviewer = _reviewer(store)

    client.get("/api/asclepius/review/next", headers=A.headers_for(reviewer))  # claim it
    r = client.post(
        f"/api/asclepius/review/{sub['submission_id']}",
        json=_review_body(),
        headers=A.headers_for(reviewer),
    )
    assert r.status_code == 200
    assert store.get_submission(sub["submission_id"])["review_status"] == "reviewed"
    # Neither the same reviewer nor a new one draws it again.
    assert store.next_review_for(reviewer["id"], specialty="nephrology") is None
    other = _reviewer(store)
    assert store.next_review_for(other["id"], specialty="nephrology") is None


def test_duplicate_review_by_same_reviewer_is_409():
    store = asc_store.get_store()
    labeler = A.make_user(store)
    task = _mk_task(store)
    sub = _mk_submission(store, task, labeler)
    reviewer = _reviewer(store)
    client.get("/api/asclepius/review/next", headers=A.headers_for(reviewer))  # claim it
    first = client.post(
        f"/api/asclepius/review/{sub['submission_id']}",
        json=_review_body(),
        headers=A.headers_for(reviewer),
    )
    assert first.status_code == 200
    dup = client.post(
        f"/api/asclepius/review/{sub['submission_id']}",
        json=_review_body(),
        headers=A.headers_for(reviewer),
    )
    assert dup.status_code == 409


# ─── Phase 1: second labeler independence ────────────────────────────────────
def test_second_labeler_cannot_be_first_labeler():
    store = asc_store.get_store()
    first = A.make_user(store, specialty="nephrology")
    task = _mk_task(store)
    _mk_submission(store, task, first)
    assert store.flag_task_for_double_label(task["task_id"]) is True

    # The first labeler can NEVER be served their own task for the second label.
    assert store.next_double_label_for(first["id"], specialty="nephrology") is None
    # An independent evaluator is.
    second = A.make_user(store, specialty="nephrology")
    served = store.next_double_label_for(second["id"], specialty="nephrology")
    assert served is not None and served["task_id"] == task["task_id"]


def test_double_label_excludes_anyone_who_already_submitted():
    store = asc_store.get_store()
    first = A.make_user(store, specialty="nephrology")
    second = A.make_user(store, specialty="nephrology")
    task = _mk_task(store, max_labels=2)
    _mk_submission(store, task, first)
    _mk_submission(store, task, second)
    # Task is at capacity now — nobody gets served, including a fresh user.
    third = A.make_user(store, specialty="nephrology")
    assert store.next_double_label_for(third["id"], specialty="nephrology") is None
    # And the second labeler is excluded by their own submission regardless.
    assert store.next_double_label_for(second["id"], specialty="nephrology") is None


def test_double_label_respects_v4_wall():
    store = asc_store.get_store()
    first = A.make_user(store, specialty="nephrology")
    real_case = {
        "case_source": "real_deid",
        "notes": [{"note_type": "hpi", "text": "De-identified note body."}],
    }
    task = _mk_task(store, case=real_case)
    assert task["case_source"] == "real_deid"
    _mk_submission(store, task, first)
    store.flag_task_for_double_label(task["task_id"])

    second = A.make_user(store, specialty="nephrology")
    # Not real-data-approved: the real case never reaches them.
    assert store.next_double_label_for(second["id"], specialty="nephrology", allow_real=False) is None
    assert (
        store.next_double_label_for(second["id"], specialty="nephrology", allow_real=True)["task_id"]
        == task["task_id"]
    )


# ─── Phase 1: routing policy ──────────────────────────────────────────────────
def test_needs_review_stratification(monkeypatch):
    real_task = {"case_source": "real_deid"}
    hard_task = {"case": {"declared_difficulty": "frontier-hard"}}
    plain_task = {"specialty": "nephrology"}
    sub = {"submission_id": "sub-fixed"}

    monkeypatch.setenv("ASCLEPIUS_REVIEW_RATE", "0.0")
    # Real and frontier-hard cases are ALWAYS reviewed, even at rate 0.
    assert asc_review.needs_review(real_task, sub) is True
    assert asc_review.needs_review(hard_task, sub) is True
    assert asc_review.needs_review(plain_task, sub) is False

    monkeypatch.setenv("ASCLEPIUS_REVIEW_RATE", "1.0")
    assert asc_review.needs_review(plain_task, sub) is True

    # Deterministic: the same submission always gets the same answer.
    monkeypatch.setenv("ASCLEPIUS_REVIEW_RATE", "0.5")
    first = asc_review.needs_review(plain_task, sub)
    assert all(asc_review.needs_review(plain_task, sub) == first for _ in range(5))


def test_sweep_double_label_routing_flags_hard_task(paired_flow):
    store = asc_store.get_store()
    labeler = A.make_user(store, specialty="nephrology")
    hard_case = {
        "declared_difficulty": "frontier-hard",
        "notes": [{"note_type": "hpi", "text": "Synthetic frontier-hard case."}],
    }
    task = _mk_task(store, case=hard_case)
    _mk_submission(store, task, labeler)
    assert store.get_task(task["task_id"])["max_labels"] == 1

    flagged = asc_review.sweep_double_label_routing(store)
    assert flagged >= 1
    assert store.get_task(task["task_id"])["max_labels"] == 2
    # Idempotent: a second sweep does not re-flag.
    assert store.get_task(task["task_id"])["max_labels"] == 2
    events = store.list_events(entity_type="task", entity_id=task["task_id"])
    assert any(e["event_type"] == "double_label_flagged" for e in events)


# ─── Phase 2: blinding — the payload, not the flag ────────────────────────────
_IDENTITY_MARKERS = (
    "evaluator_id", "annotator", "id_hashed", "email", "full_name",
    "years_experience", "board_cert", "credential", "npi", "organization",
)


def _assert_no_identity(obj, path="$", *, needles=None):
    """No labeler identity anywhere in the served JSON — by KEY or by VALUE.

    The value scan matters because ``_SUBMISSION_PAYLOAD_VIEW_KEYS`` deliberately
    serves ``from_scratch``, ``rejected_critique`` and ``reasoning_steps``: all
    labeler-authored prose, all capable of naming their author (FIX A Phase 2)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert k not in _IDENTITY_MARKERS, f"labeler identity key {k!r} at {path}"
            _assert_no_identity(v, f"{path}.{k}", needles=needles)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _assert_no_identity(v, f"{path}[{i}]", needles=needles)
    elif isinstance(obj, str):
        low = obj.lower()
        for needle in (needles or []):
            assert needle not in low, f"labeler identity value {needle!r} at {path}"


def test_review_payload_contains_no_labeler_identity():
    store = asc_store.get_store()
    labeler = A.make_user(store, specialty="nephrology", years_experience=17)
    task = _mk_task(store)
    _mk_submission(
        store,
        task,
        labeler,
        payload={
            "verdict": "A_better",
            "chosen_id": "a",
            "rejected_id": "b",
            "chosen_revision": {"edited": True, "revised_text": "Revised answer."},
            "reasoning_steps": [{"text": "Step 1", "confirmed": True}],
            "rubric": [{"text": "Names the decisive lab", "points": 5}],
        },
    )
    reviewer = _reviewer(store)
    r = client.get("/api/asclepius/review/next", headers=A.headers_for(reviewer))
    view = r.json()["submission"]
    assert view is not None
    _assert_no_identity(view, needles=asc_review.labeler_identity_needles(labeler))
    assert view["blinded"] is True
    # The labeler's actual work IS served.
    assert view["labeler_answer"]["verdict"] == "A_better"
    assert view["labeler_answer"]["chosen_revision"]["revised_text"] == "Revised answer."
    assert view["task"]["prompt"].startswith("Case prompt")


def test_accept_with_edits_requires_corrections():
    store = asc_store.get_store()
    labeler = A.make_user(store)
    task = _mk_task(store)
    sub = _mk_submission(store, task, labeler)
    reviewer = _reviewer(store)
    client.get("/api/asclepius/review/next", headers=A.headers_for(reviewer))  # claim it
    r = client.post(
        f"/api/asclepius/review/{sub['submission_id']}",
        json=_review_body("accept_with_edits"),
        headers=A.headers_for(reviewer),
    )
    assert r.status_code == 422
    r = client.post(
        f"/api/asclepius/review/{sub['submission_id']}",
        json=_review_body(
            "accept_with_edits",
            corrections={"notes": "Dose should be renally adjusted.", "edited_answer": ""},
        ),
        headers=A.headers_for(reviewer),
    )
    assert r.status_code == 200
    stored = store.reviews_for_submission(sub["submission_id"])[0]
    assert stored["corrections"]["notes"] == "Dose should be renally adjusted."


def test_reject_requires_a_reason():
    store = asc_store.get_store()
    labeler = A.make_user(store)
    task = _mk_task(store)
    sub = _mk_submission(store, task, labeler)
    reviewer = _reviewer(store)
    client.get("/api/asclepius/review/next", headers=A.headers_for(reviewer))  # claim it
    r = client.post(
        f"/api/asclepius/review/{sub['submission_id']}",
        json=_review_body("reject"),
        headers=A.headers_for(reviewer),
    )
    assert r.status_code == 422
    r = client.post(
        f"/api/asclepius/review/{sub['submission_id']}",
        json=_review_body("reject", reviewer_notes="Contraindicated in stage 4 CKD."),
        headers=A.headers_for(reviewer),
    )
    assert r.status_code == 200
    stored = store.reviews_for_submission(sub["submission_id"])[0]
    assert stored["reviewer_notes"] == "Contraindicated in stage 4 CKD."


def test_cannot_assess_persists_as_its_own_value():
    store = asc_store.get_store()
    labeler = A.make_user(store)
    task = _mk_task(store)
    sub = _mk_submission(store, task, labeler)
    reviewer = _reviewer(store)
    client.get("/api/asclepius/review/next", headers=A.headers_for(reviewer))  # claim it
    dims = {k: "agree" for k in asc_review.DIMENSION_KEYS}
    dims["rubric_quality"] = "cannot_assess"
    r = client.post(
        f"/api/asclepius/review/{sub['submission_id']}",
        json=_review_body(dimensions=dims),
        headers=A.headers_for(reviewer),
    )
    assert r.status_code == 200
    stored = store.reviews_for_submission(sub["submission_id"])[0]
    # Its own value — never folded into disagreement (START_HERE §5 rule 4).
    assert stored["dimensions"]["rubric_quality"] == "cannot_assess"
    with store._conn() as conn:
        raw = conn.execute(
            "SELECT dimension_json FROM case_reviews WHERE review_id = ?",
            (stored["review_id"],),
        ).fetchone()[0]
    assert json.loads(raw)["rubric_quality"] == "cannot_assess"


def test_review_portal_page_served():
    r = client.get("/asclepius/review")
    assert r.status_code == 200
    assert "/static/asclepius/review.js" in r.text
    # The shell itself is identity-free (it is served unauthenticated).
    assert "annotator" not in r.text


def test_review_js_builds_dom_with_h_and_no_innerhtml():
    """START_HERE §5 rule 5: DOM via h(), no innerHTML, no HTML string templates."""
    src = (
        Path(__file__).resolve().parents[2] / "frontend" / "asclepius" / "review.js"
    ).read_text(encoding="utf-8")
    assert ".innerHTML" not in src  # property access, not the word in comments
    assert ".outerHTML" not in src
    assert "insertAdjacentHTML" not in src
    assert "function h(" in src


# ─── Phase 4: two statistics, named correctly ─────────────────────────────────
def test_review_acceptance_rates_and_tri_state_dimensions():
    from asclepius.agreement import review_acceptance

    empty = review_acceptance([])
    # No reviews is not 0% accepted — rates are honestly None. Asserted as a
    # SUBSET so additive keys (n_unclassified, n_total) do not break the seam.
    assert empty["n"] == 0 and empty["by_dimension"] == {} and empty["n_cannot_assess"] == 0
    assert empty["accept_rate"] is None and empty["edit_rate"] is None
    assert empty["reject_rate"] is None

    reviews = [
        {"verdict": "accept", "dimensions": {"clinical_accuracy": "agree"}},
        {"verdict": "accept", "dimensions": {"clinical_accuracy": "agree"}},
        {"verdict": "accept_with_edits", "dimensions": {"clinical_accuracy": "cannot_assess"}},
        {"verdict": "reject", "dimensions": {"clinical_accuracy": "disagree"}},
    ]
    out = review_acceptance(reviews)
    assert out["n"] == 4
    assert out["accept_rate"] == 0.5
    assert out["edit_rate"] == 0.25
    assert out["reject_rate"] == 0.25
    dim = out["by_dimension"]["clinical_accuracy"]
    # cannot_assess is its own bucket — never folded into disagreement.
    assert dim == {"agree": 2, "disagree": 1, "cannot_assess": 1}
    assert out["n_cannot_assess"] == 1


def test_independent_kappa_gates_and_blinding():
    from asclepius.agreement import independent_kappa

    def obs(a, b, blinded=True):
        return {"verdict_a": a, "verdict_b": b, "blinded": blinded, "specialty": "neph"}

    # Below the min-n gate: None WITH a stated reason, never a bare number.
    small = independent_kappa([obs("x", "x")] * 5, min_n=30)
    assert small["overall"] is None and "not reportable" in (small["reason"] or "")

    # Unblinded observations are excluded from the computation entirely.
    mixed = [obs("x", "x")] * 20 + [obs("x", "y")] * 10 + [obs("x", "x", blinded=False)] * 50
    out = independent_kappa(mixed, min_n=30)
    assert out["n"] == 30                      # only the blinded 30
    assert out["excluded_unblinded"] == 50
    assert out["overall"] is not None


def test_incomplete_dimensions_rejected():
    body = _review_body()
    del body["dimensions"]["completeness"]
    errors = asc_review.validate_review_payload(body)
    assert any("completeness" in e for e in errors)
    errors = asc_review.validate_review_payload(
        _review_body(dimensions={**{k: "agree" for k in asc_review.DIMENSION_KEYS}, "vibes": "agree"})
    )
    assert any("vibes" in e for e in errors)


# ═══════════════════════════════════════════════════════════════════════════════
# FIX ROUND — Phase 1 (F1): independent double-labeling must be REACHABLE.
#
# The hackathon tests built state with store.insert_submission(), which bypasses
# POST /submissions and therefore refresh_task_status. On the real route a
# max_labels=1 task is closed ('done') the instant the first label lands, so the
# old candidate query (status='open' AND has-a-submission) could never match.
# Every test below walks the ROUTE. None of them may call insert_submission.
# ═══════════════════════════════════════════════════════════════════════════════
def _admin_h():
    return A.headers_for(A.make_user(asc_store.get_store(), role="admin"))


def _labeler(specialty="nephrology"):
    return A.make_user(asc_store.get_store(), role="evaluator", specialty=specialty,
                       board_cert="board_certified_nephrology", years_experience=12)


def _create_task_via_route(admin_h, *, specialty="nephrology", max_labels=1, **kw):
    body = {
        "specialty": specialty, "difficulty": "hard", "max_labels": max_labels,
        "prompt": f"Hyperkalemia case {A.uniq(8)}?",
        "candidate_answers": [{"id": "A", "text": "Calcium then dialyze."},
                              {"id": "B", "text": "Dialysate K+ 1.0."}],
    }
    body.update(kw)
    r = client.post("/api/asclepius/tasks", json={"tasks": [body]}, headers=admin_h)
    assert r.status_code == 200, r.text
    return r.json()["created"][0]


def _reveal_via_route(task_id, labeler, text=None):
    """POST /tasks/{id}/reveal — the blind-commit gate.

    In production ``ASCLEPIUS_WITHHOLD_ANSWERS`` is on, so a labeler CANNOT see
    the candidate answers without passing through here first. That commit is the
    evidence κ's blinding flag is derived from (Audit R C2), so a route test that
    skips it is not walking the labeler's real path."""
    r = client.post(f"/api/asclepius/tasks/{task_id}/reveal",
                    json={"text": text or f"Blind read {A.uniq(8)}: calcium, then dialyze."},
                    headers=A.headers_for(labeler))
    assert r.status_code == 200, r.text
    return r.json()


def _submit_via_route(task_id, labeler, *, verdict="A_better", extra=None):
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
    if extra:
        body.update(extra)
    r = client.post("/api/asclepius/submissions", json=body, headers=A.headers_for(labeler))
    assert r.status_code == 200, r.text
    return sid


def test_double_label_is_reachable_through_the_real_submit_route(paired_flow):
    """THE TEST THAT WOULD HAVE CAUGHT F1.

    Builds every bit of state through HTTP. Before the fix this fails at the
    first candidate assertion: refresh_task_status has already closed the task,
    so the candidate query (which required status='open') returns nothing and
    the entire κ deliverable is unreachable."""
    store = asc_store.get_store()
    admin_h = _admin_h()
    first = _labeler()
    tid = _create_task_via_route(admin_h)

    _submit_via_route(tid, first)

    # The real route closes a max_labels=1 task on the first label. This is the
    # pre-existing behaviour the routing has to work WITH (never against).
    task = store.get_task(tid)
    assert task["status"] == "done" and task["max_labels"] == 1

    # A closed, singly-labeled task is exactly what the double-label router must
    # consider — it is the only state such a task is ever in.
    candidates = store.tasks_awaiting_double_label_decision()
    assert tid in [c["task_id"] for c in candidates]

    flagged = asc_review.sweep_double_label_routing(store)
    assert flagged >= 1

    # Flagging a 'done' task without reopening it leaves a task nobody can draw.
    reopened = store.get_task(tid)
    assert reopened["max_labels"] == 2
    assert reopened["status"] == "open"

    # A second INDEPENDENT labeler can now be served the task...
    second = _labeler()
    served = store.next_double_label_for(second["id"], specialty="nephrology")
    assert served is not None and served["task_id"] == tid
    # ...and it also reaches them through the ordinary labeler queue, which is
    # where the second label is actually produced.
    assert store.next_task_for_evaluator(
        evaluator_id=second["id"], specialty="nephrology", hard_only=True) is not None


def test_reopened_task_is_never_served_back_to_the_first_labeler(paired_flow):
    store = asc_store.get_store()
    admin_h = _admin_h()
    first = _labeler()
    tid = _create_task_via_route(admin_h)
    _submit_via_route(tid, first)
    asc_review.sweep_double_label_routing(store)
    assert store.get_task(tid)["status"] == "open"

    # Independence is the whole point: the first labeler must not see it again,
    # in either queue.
    assert store.next_double_label_for(first["id"], specialty="nephrology") is None
    assert store.next_task_for_evaluator(
        evaluator_id=first["id"], specialty="nephrology", hard_only=True) is None


def test_second_label_closes_the_task_again(paired_flow):
    store = asc_store.get_store()
    admin_h = _admin_h()
    tid = _create_task_via_route(admin_h)
    _submit_via_route(tid, _labeler())
    asc_review.sweep_double_label_routing(store)

    second = _labeler()
    _submit_via_route(tid, second)
    task = store.get_task(tid)
    # count(2) >= max_labels(2) -> closed again, and no longer a candidate.
    assert task["status"] == "done" and task["max_labels"] == 2
    assert tid not in [c["task_id"] for c in store.tasks_awaiting_double_label_decision()]
    assert store.next_double_label_for(_labeler()["id"], specialty="nephrology") is None


def test_sweep_is_idempotent_across_two_actual_calls(paired_flow):
    """The hackathon test claimed idempotence in a comment and never made the
    second call — a vacuous assertion (FIX A Phase 1)."""
    store = asc_store.get_store()
    admin_h = _admin_h()
    tid = _create_task_via_route(admin_h)
    _submit_via_route(tid, _labeler())

    first_sweep = asc_review.sweep_double_label_routing(store)
    second_sweep = asc_review.sweep_double_label_routing(store)   # the call that was missing
    assert first_sweep >= 1
    assert second_sweep == 0
    assert store.get_task(tid)["max_labels"] == 2
    events = [e for e in store.list_events(entity_type="task", entity_id=tid)
              if e["event_type"] == "double_label_flagged"]
    assert len(events) == 1


def test_reopen_never_resurrects_a_terminally_flagged_task():
    """prompt_flagged / not_hard / case_incoherent are terminal. The reopen must
    not drag a clinically-rejected prompt back into the labeler queue."""
    store = asc_store.get_store()
    admin_h = _admin_h()
    for terminal in ("prompt_flagged", "not_hard", "case_incoherent"):
        tid = _create_task_via_route(admin_h)
        _submit_via_route(tid, _labeler())
        store.mark_task_status(tid, terminal)
        assert store.flag_task_for_double_label(tid) is False
        assert store.get_task(tid)["status"] == terminal
        assert tid not in [c["task_id"] for c in store.tasks_awaiting_double_label_decision()]


def test_full_kappa_loop_through_the_route_produces_a_real_number(paired_flow):
    """FIX A definition-of-done #1, end to end and route-only.

    30 cases, each labeled by two INDEPENDENT physicians via POST /submissions,
    with the double-label routing decided by the product (not the test). The
    payoff is the number this whole tier exists to produce: a real Cohen's κ
    over blinded, independently double-labeled observations. Before the F1 fix
    this could not reach n>0, so quality_report.md said 'not reportable' forever.
    """
    store = asc_store.get_store()
    admin_h = _admin_h()
    n_cases = 30

    for i in range(n_cases):
        tid = _create_task_via_route(admin_h)
        first = _labeler()
        _reveal_via_route(tid, first)
        _submit_via_route(tid, first, verdict="A_better")
        # The PRODUCT decides to double-label, not the test.
        assert asc_review.sweep_double_label_routing(store) >= 1
        # Two of the thirty disagree, so κ is a real chance-corrected number
        # rather than the degenerate single-category 1.0.
        second_verdict = "B_better" if i < 2 else "A_better"
        second = _labeler()
        _reveal_via_route(tid, second)
        _submit_via_route(tid, second, verdict=second_verdict)

    observations = store.list_agreement_observations()
    assert len(observations) == n_cases
    # Audit R C2: `blinded` is now a MEASUREMENT, so it is only 1 because each
    # labeler passed the reveal gate — committing a blind independent answer
    # before being shown anything else. That gate is on by default in production
    # (ASCLEPIUS_WITHHOLD_ANSWERS), which is why walking it here is the faithful
    # route test rather than a concession.
    assert all(o["blinded"] in (1, True) for o in observations)

    from asclepius.agreement import independent_kappa

    out = independent_kappa(observations)
    assert out["n"] == n_cases                      # meets the min-n gate
    assert out["reason"] is None                    # nothing suppressed
    assert isinstance(out["overall"], float)        # a number, at last
    assert out["observed_agreement"] == round((n_cases - 2) / n_cases, 4)


# ═══════════════════════════════════════════════════════════════════════════════
# FIX ROUND — Phase 2 (F2): `blinded` must be DERIVED, not asserted.
#
# In the hackathon build both the router and the view wrote the literal True.
# No code path in the product could produce a 0; the only way to see one was to
# call store.insert_case_review(blinded=False) by hand. A flag nothing can
# falsify is not a measurement, and this one gates independent_kappa.
# ═══════════════════════════════════════════════════════════════════════════════
def _draw(reviewer):
    return client.get("/api/asclepius/review/next", headers=A.headers_for(reviewer)).json()


def _post_review(reviewer, sid, body=None):
    return client.post(f"/api/asclepius/review/{sid}", json=body or _review_body(),
                       headers=A.headers_for(reviewer))


def test_identity_key_in_the_served_payload_records_blinded_zero():
    """THE TEST THAT WOULD HAVE CAUGHT F2.

    Inject a labeler-identity key into the labeler's own answer payload, draw as
    a reviewer, and the recorded review must say blinded=0. Against the old
    hardcoded literal this is impossible."""
    store = asc_store.get_store()
    labeler = A.make_user(store, specialty="nephrology")
    task = _mk_task(store)
    # `from_scratch` is deliberately served to reviewers, so a nested identity
    # key inside it reaches the reviewer's screen.
    sub = _mk_submission(store, task, labeler, payload={
        "verdict": "both_inadequate",
        "from_scratch": {"ideal_answer": "Dialyze.", "annotator": {"email": "who@x.com"}},
    })
    reviewer = _reviewer(store)

    drawn = _draw(reviewer)
    assert drawn["submission"]["blinded"] is False          # served honestly
    assert _post_review(reviewer, sub["submission_id"]).status_code == 200

    stored = store.reviews_for_submission(sub["submission_id"])[0]
    assert stored["blinded"] == 0                            # recorded honestly


def test_labeler_named_in_free_text_records_blinded_zero():
    """The PRD's own listed vector, previously untested: labeler-authored prose
    that names the labeler. Scanned by VALUE, against that account's real
    identifiers — not by a heuristic name-detector that would false-positive on
    clinical text and silently shrink κ's n."""
    store = asc_store.get_store()
    labeler = A.make_user(store, specialty="nephrology",
                          email=f"gregory.house-{A.uniq(6)}@hospital.example.com")
    task = _mk_task(store)
    sub = _mk_submission(store, task, labeler, payload={
        "verdict": "A_better", "chosen_id": "a", "rejected_id": "b",
        "rejected_critique": {
            "why_worse": f"As I noted in my earlier review — {labeler['email']}",
        },
    })
    reviewer = _reviewer(store)
    assert _draw(reviewer)["submission"]["blinded"] is False
    _post_review(reviewer, sub["submission_id"])
    assert store.reviews_for_submission(sub["submission_id"])[0]["blinded"] == 0


def test_admin_draw_is_never_blinded_and_is_excluded_from_kappa():
    """require_reviewer admits admins, and an admin can de-blind through
    GET /submissions/{id}. Stamping blinded=1 for a reviewer who can look the
    author up in another tab is the same lie one hop out."""
    store = asc_store.get_store()
    labeler = A.make_user(store, specialty="nephrology")
    task = _mk_task(store)
    sub = _mk_submission(store, task, labeler)
    admin = A.make_user(store, role="admin", specialty="nephrology")

    drawn = _draw(admin)
    assert drawn["submission"] is not None
    assert drawn["submission"]["blinded"] is False
    assert _post_review(admin, sub["submission_id"]).status_code == 200
    assert store.reviews_for_submission(sub["submission_id"])[0]["blinded"] == 0

    # And such an observation cannot enter the independent-κ computation.
    from asclepius.agreement import _blinded_only
    assert _blinded_only([{"blinded": 0}]) == []


def test_clean_payload_still_records_blinded_one():
    """The derivation must not be so eager that honest reviews fall out of κ."""
    store = asc_store.get_store()
    labeler = A.make_user(store, specialty="nephrology", years_experience=20)
    task = _mk_task(store)
    sub = _mk_submission(store, task, labeler)
    reviewer = _reviewer(store)
    assert _draw(reviewer)["submission"]["blinded"] is True
    _post_review(reviewer, sub["submission_id"])
    assert store.reviews_for_submission(sub["submission_id"])[0]["blinded"] == 1


def test_view_no_longer_carries_an_asserted_blinded_literal():
    store = asc_store.get_store()
    labeler = A.make_user(store)
    task = _mk_task(store)
    sub = _mk_submission(store, task, labeler)
    view = asc_review.blinded_review_view(task, sub)
    assert "blinded" not in view       # derived by the caller, never asserted here


# ═══════════════════════════════════════════════════════════════════════════════
# FIX ROUND — Phase 3: correctness + cost bugs in the review flow.
# ═══════════════════════════════════════════════════════════════════════════════
def test_submit_requires_holding_the_claim():
    """A-3.1: submit used to check only 'exists / not mine / not already
    reviewed', so any reviewer could POST onto a guessed submission id —
    including one another reviewer was holding, evicting their in-flight work.

    A single-flow test: it relies on the file-wide halt pin (see ``_isolated``),
    without which the POST 409s with ``became_a_pair`` before reaching the claim
    guard at all, and the assertion below reads a dict ``detail`` and dies on
    ``.lower()``."""
    store = asc_store.get_store()
    labeler = A.make_user(store, specialty="nephrology")
    task = _mk_task(store)
    sub = _mk_submission(store, task, labeler)
    holder, interloper = _reviewer(store), _reviewer(store)

    # Cold POST with no draw at all.
    assert _post_review(interloper, sub["submission_id"]).status_code == 409

    # holder draws (and therefore claims) it.
    assert _draw(holder)["submission"]["submission_id"] == sub["submission_id"]
    # The interloper cannot evict that claim by guessing the id.
    r = _post_review(interloper, sub["submission_id"])
    assert r.status_code == 409
    assert "another reviewer" in r.json()["detail"].lower()
    # The holder's own in-flight work is untouched.
    assert _post_review(holder, sub["submission_id"]).status_code == 200


def test_expired_claim_cannot_submit():
    store = asc_store.get_store()
    labeler = A.make_user(store, specialty="nephrology")
    task = _mk_task(store)
    sub = _mk_submission(store, task, labeler)
    reviewer = _reviewer(store)
    _draw(reviewer)
    with store._conn() as conn:
        conn.execute("UPDATE submissions SET review_claimed_at = '2000-01-01T00:00:00' "
                     "WHERE submission_id = ?", (sub["submission_id"],))
    r = _post_review(reviewer, sub["submission_id"])
    assert r.status_code == 409 and "expired" in r.json()["detail"].lower()


def test_orphaned_submission_does_not_jam_the_queue(monkeypatch):
    """A-3.2: releasing an orphan to NULL made it the OLDEST eligible row again,
    so the retry loop re-drew the same orphan five times and every reviewer got
    'queue is contended' — permanently.

    The reachable path is a race, not a deleted row: next_review_for INNER JOINs
    tasks, so a submission whose task is gone is invisible to the query. It
    becomes an orphan only when the task disappears BETWEEN that query and the
    router's get_task. That race is what is simulated here."""
    store = asc_store.get_store()
    labeler = A.make_user(store, specialty="nephrology")
    orphan_task = _mk_task(store)
    orphan = _mk_submission(store, orphan_task, labeler)
    good_task = _mk_task(store)
    good = _mk_submission(store, good_task, labeler)

    real_get_task = store.get_task

    def _racy_get_task(task_id):
        if task_id == orphan_task["task_id"]:
            return None          # vanished after the candidate query selected it
        return real_get_task(task_id)

    monkeypatch.setattr(store, "get_task", _racy_get_task)

    reviewer = _reviewer(store)
    drawn = _draw(reviewer)
    # The draw steps over the orphan and serves the real work behind it, in the
    # SAME request — no "queue is contended", no spin.
    assert drawn["submission"] is not None
    assert drawn["submission"]["submission_id"] == good["submission_id"]
    assert store.get_submission(orphan["submission_id"])["review_status"] == "orphaned"
    assert store.review_queue_stats()["orphaned"] == 1

    # The orphan is terminal: it never re-enters any reviewer's queue.
    monkeypatch.undo()
    assert store.next_review_for(_reviewer(store)["id"], specialty="nephrology") is None


def test_declined_routing_decision_is_persisted_and_requeueable(monkeypatch):
    """A-3.3: a submission the policy declines used to stay NULL forever and
    re-occupy a slot in the LIMIT-200 scan window on every draw. With enough of
    them at the head the portal reports an empty queue while real work waits."""
    store = asc_store.get_store()
    labeler = A.make_user(store, specialty="nephrology")
    task = _mk_task(store)
    sub = _mk_submission(store, task, labeler)

    monkeypatch.setenv("ASCLEPIUS_REVIEW_RATE", "0.0")   # decline everything
    reviewer = _reviewer(store)
    assert _draw(reviewer)["submission"] is None
    assert store.get_submission(sub["submission_id"])["review_status"] == "not_routed"

    stats = store.review_queue_stats()
    # The header must not claim work exists that the draw cannot serve.
    assert stats["unreviewed"] == 0 and stats["not_routed"] == 1

    # Raising the rate later is operationally recoverable.
    monkeypatch.setenv("ASCLEPIUS_REVIEW_RATE", "1.0")
    assert store.requeue_not_routed() == 1
    assert _draw(reviewer)["submission"]["submission_id"] == sub["submission_id"]


def test_routing_sweep_is_off_the_draw_path_and_cheap(paired_flow):
    """A-3.4: the sweep issued ~9 statements per candidate — each on a FRESH
    sqlite3 connection — on the request path, against the single writer that
    labeler submissions also need. Measured at 452 statements for 50 candidates.

    The invariant that matters is cost that does not scale with the scan window:
    a fixed number of connections and a fixed number of read queries, with the
    only per-task work being the writes that actually flag a task."""
    store = asc_store.get_store()
    admin_h = _admin_h()
    n = 20
    for _ in range(n):
        tid = _create_task_via_route(admin_h)
        _submit_via_route(tid, _labeler())

    statements = []
    real_conn = store._conn

    def _traced():
        c = real_conn()
        c.set_trace_callback(statements.append)
        return c

    store._conn = _traced
    try:
        flagged = asc_review.sweep_double_label_routing(store, limit=100)
    finally:
        store._conn = real_conn

    assert flagged == n
    # Each _conn() pays two PRAGMAs; counting them counts connections opened.
    connections = sum(1 for st in statements if "busy_timeout" in st)
    assert connections <= 4, f"sweep opened {connections} connections for {n} candidates"

    reads = [st for st in statements
             if st.strip().upper().startswith("SELECT") and "PRAGMA" not in st]
    # Reads are fixed: the candidate page, the fleet counts, one observation
    # COUNT per specialty. Previously this grew by ~3 per candidate.
    assert len(reads) <= 4, f"sweep issued {len(reads)} read queries for {n} candidates"

    # Writes are the flags themselves — inherent, and all on one connection.
    assert len(statements) <= 12 + 2 * flagged

    # A draw does not run the sweep inline: the throttle slot is claimed before
    # running, so concurrent draws cannot each trigger one.
    from routers import asclepius_review as rv
    rv._SWEEP_STATE["last"] = 0.0
    assert rv._sweep_due() is True
    assert rv._sweep_due() is False


def test_review_status_is_indexed():
    """A-3.5: review_queue_stats and next_review_for both filter on it."""
    store = asc_store.get_store()
    with store._conn() as conn:
        idx = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='submissions'")}
    assert "idx_sub_review_status" in idx


def test_next_double_label_for_is_bounded():
    """A-3.6: it fetchall()'d the entire open-task table; next_review_for beside
    it correctly used a LIMIT."""
    import inspect
    src = inspect.getsource(asc_store.AsclepiusStore.next_double_label_for)
    assert "LIMIT ?" in src


# ═══════════════════════════════════════════════════════════════════════════════
# FIX ROUND — Phase 4: the statistics (Seam 3).
# ═══════════════════════════════════════════════════════════════════════════════
def test_acceptance_rates_sum_to_one_with_an_unclassified_verdict():
    """A-4.1: n was len(reviews) but only the three known verdicts incremented
    the tally, so any other verdict shrank all three rates while appearing
    nowhere — and they silently failed to sum to 1."""
    from asclepius.agreement import review_acceptance

    reviews = [
        {"verdict": "accept", "dimensions": {}},
        {"verdict": "accept_with_edits", "dimensions": {}},
        {"verdict": "reject", "dimensions": {}},
        {"verdict": "escalated_to_committee", "dimensions": {}},   # not a known verdict
    ]
    out = review_acceptance(reviews)
    assert out["n"] == 3 and out["n_total"] == 4 and out["n_unclassified"] == 1
    # Rates are rounded to 4dp, so allow that much slack; the point is that
    # they now sum to the whole, which the old denominator made impossible.
    assert abs(out["accept_rate"] + out["edit_rate"] + out["reject_rate"] - 1.0) < 1e-3
    # The unrecognized row is visible, not absorbed.
    assert out["accept_rate"] == round(1 / 3, 4)


def test_acceptance_shape_is_frozen_for_seam_3():
    """Seam 3: C deletes its inline SQL and calls this. The contract keys must
    all be present, including at n=0."""
    from asclepius.agreement import review_acceptance

    contract = {"n", "accept_rate", "edit_rate", "reject_rate",
                "by_dimension", "n_cannot_assess"}
    assert contract <= set(review_acceptance([]))
    assert contract <= set(review_acceptance([{"verdict": "accept", "dimensions": {}}]))
    empty = review_acceptance([])
    assert empty["n"] == 0 and empty["accept_rate"] is None   # not 0% accepted


def test_only_one_definition_of_expert_acceptance_exists():
    """Seam 3, enforced: the combined 'accept OR accept_with_edits' figure is a
    DIFFERENT number and may not be computed anywhere as 'acceptance'."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for path in root.rglob("*.py"):
        if "/tests/" in str(path) or path.name == "agreement.py":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "accept_with_edits" in text and "IN ('accept'" in text.replace('"', "'"):
            offenders.append(str(path.relative_to(root)))
    assert not offenders, f"second definition of acceptance in {offenders}"


def test_double_label_rate_has_one_source_of_truth(paired_flow):
    """A-4.3: review.py delegated to agreement.double_label_rate(), which
    defaulted to 0.20 while the PRD review.py implements specified 0.15.

    PRD R §1.1 moved the default to 1.0 — two labels is the normal path, not a
    sample. The property under test is unchanged and is the point of the test:
    ONE constant, and every caller reads it rather than carrying its own copy."""
    from asclepius import agreement as asc_agreement
    from asclepius import routing as asc_routing

    assert asc_agreement.DEFAULT_DOUBLE_LABEL_RATE == 1.0
    assert asc_agreement.double_label_rate() == asc_agreement.DEFAULT_DOUBLE_LABEL_RATE
    assert asc_review.double_label_rate() == asc_agreement.double_label_rate()
    assert asc_routing.second_label_is_default() is True


def test_double_label_rate_env_override_still_wins(paired_flow, monkeypatch):
    from asclepius import agreement as asc_agreement

    monkeypatch.setenv("ASCLEPIUS_DOUBLE_LABEL_RATE", "0.42")
    assert asc_agreement.double_label_rate() == 0.42
    assert asc_review.double_label_rate() == 0.42
    monkeypatch.setenv("ASCLEPIUS_DOUBLE_LABEL_RATE", "not-a-number")
    # Falls back to the ONE constant, never crashes.
    assert asc_agreement.double_label_rate() == asc_agreement.DEFAULT_DOUBLE_LABEL_RATE
