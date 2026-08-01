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
def test_reviewer_cannot_draw_own_submission():
    store = asc_store.get_store()
    reviewer = _reviewer(store)
    task = _mk_task(store)
    _mk_submission(store, task, reviewer)  # the reviewer's OWN labeling work

    assert store.next_review_for(reviewer["id"], specialty="nephrology") is None
    r = client.get("/api/asclepius/review/next", headers=A.headers_for(reviewer))
    assert r.status_code == 200
    assert r.json()["submission"] is None

    # A different reviewer draws it fine.
    other = _reviewer(store)
    drawn = store.next_review_for(other["id"], specialty="nephrology")
    assert drawn is not None and drawn["evaluator_id"] == reviewer["id"]


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
    with store._conn() as conn:
        conn.execute(
            "UPDATE submissions SET updated_at = '2000-01-01T00:00:00' WHERE submission_id = ?",
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


def test_sweep_double_label_routing_flags_hard_task():
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


def _assert_no_identity(obj, path="$"):
    """No labeler-identity key anywhere in the served JSON."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert k not in _IDENTITY_MARKERS, f"labeler identity key {k!r} at {path}"
            _assert_no_identity(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _assert_no_identity(v, f"{path}[{i}]")


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
    _assert_no_identity(view)
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
    # No reviews is not 0% accepted — rates are honestly None.
    assert empty == {"n": 0, "accept_rate": None, "edit_rate": None,
                     "reject_rate": None, "by_dimension": {}, "n_cannot_assess": 0}

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


def test_double_label_is_reachable_through_the_real_submit_route():
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


def test_reopened_task_is_never_served_back_to_the_first_labeler():
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


def test_second_label_closes_the_task_again():
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


def test_sweep_is_idempotent_across_two_actual_calls():
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


def test_full_kappa_loop_through_the_route_produces_a_real_number():
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
        _submit_via_route(tid, _labeler(), verdict="A_better")
        # The PRODUCT decides to double-label, not the test.
        assert asc_review.sweep_double_label_routing(store) >= 1
        # Two of the thirty disagree, so κ is a real chance-corrected number
        # rather than the degenerate single-category 1.0.
        second_verdict = "B_better" if i < 2 else "A_better"
        _submit_via_route(tid, _labeler(), verdict=second_verdict)

    observations = store.list_agreement_observations()
    assert len(observations) == n_cases
    assert all(o["blinded"] in (1, True) for o in observations)

    from asclepius.agreement import independent_kappa

    out = independent_kappa(observations)
    assert out["n"] == n_cases                      # meets the min-n gate
    assert out["reason"] is None                    # nothing suppressed
    assert isinstance(out["overall"], float)        # a number, at last
    assert out["observed_agreement"] == round((n_cases - 2) / n_cases, 4)
