"""PRD-1 — the reviewer experience, server side.

Two additions, both of which can only be got right on the server:

  * **§3 step_divergence.** The reviewer's version of "mark the exact step the
    reasoning breaks". Today the labeler produces step-level signal and the
    reviewer produces one verdict, so a senior physician's judgment lands as ONE
    BIT on a record carrying dozens. The sides arrive as POSITIONS in what that
    reviewer was shown and are canonicalized before storage, exactly like
    ``stronger`` — a position stored next to canonical columns is the H1 defect
    in miniature.

  * **§4.1 the preview guard.** An operator clicking through the reviewer
    surface must not adjudicate a real pair. A preview draw claims nothing,
    opens no session, and its token is refused at submit with a 409 rather than
    a silent no-op — a silent one leaves the operator believing they recorded a
    judgment, and a physician's work graded by someone sightseeing with
    ``blinded`` false on a row nobody meant to create.

Everything goes through the real routes.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _asclepius as A  # noqa: E402
from asclepius import packaging as asc_packaging  # noqa: E402
from asclepius import review as asc_review  # noqa: E402
from asclepius import routing as asc_routing  # noqa: E402
from asclepius import store as asc_store  # noqa: E402

client = TestClient(A.app)


@pytest.fixture(autouse=True)
def _fresh():
    A.fresh_store()
    yield


# ─── helpers (same shapes as test_paired_review, kept local so neither file
#     becomes the other's fixture library) ──────────────────────────────────────
def _admin_h():
    return A.headers_for(A.make_user(asc_store.get_store(), role="admin"))


def _grant_tier(store, user_id: str, tier: str) -> None:
    with store._conn() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
        if "tier" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN tier TEXT")
        conn.execute("UPDATE users SET tier = ? WHERE id = ?", (tier, user_id))


def _labeler(specialty="nephrology"):
    store = asc_store.get_store()
    user = A.make_user(store, role="evaluator", specialty=specialty,
                       board_cert="board_certified_nephrology", years_experience=12)
    _grant_tier(store, user["id"], "labeler")
    return store.get_user_by_id(user["id"])


def _reviewer(specialty="nephrology"):
    store = asc_store.get_store()
    user = A.make_user(store, role="evaluator", specialty=specialty,
                       board_cert="board_certified_nephrology", years_experience=22)
    _grant_tier(store, user["id"], "reviewer")
    return store.get_user_by_id(user["id"])


def _admin_reviewer():
    """An admin who ALSO holds the reviewer tier: a real reviewer who happens to
    be staff. They must not be forced into preview — that is the difference the
    tier makes, and collapsing it would lock a working reviewer out."""
    store = asc_store.get_store()
    user = A.make_user(store, role="admin")
    _grant_tier(store, user["id"], "reviewer")
    return store.get_user_by_id(user["id"])


def _create_task(admin_h, **kw):
    body = {
        "specialty": "nephrology", "difficulty": "hard", "max_labels": 1,
        "prompt": f"Hyperkalemia case {A.uniq(8)}?",
        "candidate_answers": [{"id": "A", "text": "Calcium then dialyze."},
                              {"id": "B", "text": "Dialysate K+ 1.0."}],
    }
    body.update(kw)
    r = client.post("/api/asclepius/tasks", json={"tasks": [body]}, headers=admin_h)
    assert r.status_code == 200, r.text
    return r.json()["created"][0]


_STEPS = [
    "Confirm the stent is patent on the last study.",
    "Read the GGT trajectory against the bilirubin.",
    "Repeat cross-sectional imaging at 72 hours.",
]


def _steps(texts):
    return [{"step": i + 1, "text": t, "confirmed": True} for i, t in enumerate(texts)]


def _submit(task_id, labeler, *, verdict="A_better", steps=None):
    sid = "s-" + uuid.uuid4().hex[:12]
    salt = A.uniq(6)
    body = {
        "submission_id": sid, "task_id": task_id, "verdict": verdict,
        "chosen_id": "A" if verdict == "A_better" else "B",
        "rejected_id": "B" if verdict == "A_better" else "A",
        "time_spent_sec": 140,
        "prompt_review": {"reviewed": True, "verdict": "valid"},
        "independent_answer": {"text": f"IV calcium, then dialysis ({salt})."},
        "chosen_revision": {"edited": False, "why_better_notes": f"B over-lowers K+ ({salt})"},
        "rejected_critique": {"error_tags": ["dosing_error"], "why_worse": f"too aggressive {salt}"},
    }
    if steps is not None:
        body["reasoning_steps"] = _steps(steps)
    r = client.post("/api/asclepius/submissions", json=body, headers=A.headers_for(labeler))
    assert r.status_code == 200, r.text
    return sid


def _reveal(task_id, labeler):
    r = client.post(f"/api/asclepius/tasks/{task_id}/reveal",
                    json={"text": f"Calcium first, then dialysis ({A.uniq(6)})."},
                    headers=A.headers_for(labeler))
    assert r.status_code == 200, r.text


def _paired_task(admin_h, *, steps_a=None, steps_b=None, **kw):
    tid = _create_task(admin_h, **kw)
    for labeler, steps in ((_labeler(), steps_a), (_labeler(), steps_b)):
        _reveal(tid, labeler)
        _submit(tid, labeler, steps=steps)
    return tid


def _draw_pair(reviewer, *, preview=False):
    url = "/api/asclepius/review/pair/next" + ("?preview=true" if preview else "")
    r = client.get(url, headers=A.headers_for(reviewer))
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


# ═══ §3 — step divergence ════════════════════════════════════════════════════
def test_step_divergence_is_stored_when_both_sides_carried_steps():
    admin_h = _admin_h()
    tid = _paired_task(admin_h, steps_a=_STEPS, steps_b=_STEPS)
    rv = _reviewer()
    _draw_pair(rv)
    r = client.post(f"/api/asclepius/review/pair/{tid}",
                    json=_adjudication(step_divergence=[{"index": 1, "judged": "A"}]),
                    headers=A.headers_for(rv))
    assert r.status_code == 200, r.text
    row = asc_store.get_store().reviews_for_task(tid)[0]
    stored = row["step_divergence"]
    if isinstance(stored, str):
        import json
        stored = json.loads(stored)
    assert len(stored) == 1
    assert stored[0]["index"] == 1
    assert stored[0]["judged"] in ("A", "B")
    assert stored[0]["judged_submission_id"]


def test_the_judged_side_is_canonicalized_like_stronger_is():
    """Audit R H1, applied to §3. ``judged`` arrives as a position in what THIS
    reviewer was shown; the A/B order is seeded per reviewer, so half of the
    stored rows would name the wrong physician if the raw position were kept.

    Six reviewers exercise both permutations of the seed, and in every one the
    canonical letter and the submission id agree with each other."""
    admin_h = _admin_h()
    store = asc_store.get_store()
    seen_swapped = set()
    for _ in range(8):
        tid = _paired_task(admin_h, steps_a=_STEPS, steps_b=_STEPS)
        rv = _reviewer()
        _draw_pair(rv)
        subs = [s for s in store.submissions_for_task(tid) if s.get("verdict")]
        shown_a, _shown_b = asc_routing.ab_pair(subs, task_id=tid, reviewer_id=rv["id"])
        seen_swapped.add(asc_routing.ab_swapped(tid, rv["id"]))
        r = client.post(f"/api/asclepius/review/pair/{tid}",
                        json=_adjudication(step_divergence=[{"index": 0, "judged": "A"}]),
                        headers=A.headers_for(rv))
        assert r.status_code == 200, r.text
        row = store.reviews_for_task(tid)[0]
        stored = row["step_divergence"]
        if isinstance(stored, str):
            import json
            stored = json.loads(stored)
        # The reviewer said "the physician in my column A". That is this
        # submission, whichever canonical position it holds.
        assert stored[0]["judged_submission_id"] == shown_a["submission_id"]
        assert stored[0]["judged"] == asc_routing.canonical_side(
            shown_a["submission_id"], subs)
    assert seen_swapped == {True, False}, "only one A/B permutation was exercised"


def test_neither_names_no_submission():
    admin_h = _admin_h()
    tid = _paired_task(admin_h, steps_a=_STEPS, steps_b=_STEPS)
    rv = _reviewer()
    _draw_pair(rv)
    r = client.post(f"/api/asclepius/review/pair/{tid}",
                    json=_adjudication(step_divergence=[{"index": 2, "judged": "neither"}]),
                    headers=A.headers_for(rv))
    assert r.status_code == 200, r.text
    row = asc_store.get_store().reviews_for_task(tid)[0]
    stored = row["step_divergence"]
    if isinstance(stored, str):
        import json
        stored = json.loads(stored)
    assert stored[0]["judged"] == "neither"
    assert stored[0]["judged_submission_id"] is None


def test_step_divergence_is_refused_when_one_side_carried_no_steps():
    """§3: 'Emit it only when both submissions carried reasoning steps.'

    REFUSED, not dropped. A client that believed it recorded process-level
    supervision must not be told nothing happened."""
    admin_h = _admin_h()
    tid = _paired_task(admin_h, steps_a=_STEPS, steps_b=None)
    rv = _reviewer()
    _draw_pair(rv)
    r = client.post(f"/api/asclepius/review/pair/{tid}",
                    json=_adjudication(step_divergence=[{"index": 0, "judged": "A"}]),
                    headers=A.headers_for(rv))
    assert r.status_code == 400, r.text
    assert "BOTH" in str(r.json()["detail"])


def test_an_adjudication_without_divergence_stores_null_not_an_empty_array():
    """NULL means 'not comparable'; [] means 'compared, and they agreed at every
    step'. Two different facts, and a buyer filtering on the second must not get
    the first."""
    admin_h = _admin_h()
    tid = _paired_task(admin_h, steps_a=None, steps_b=None)
    rv = _reviewer()
    _draw_pair(rv)
    r = client.post(f"/api/asclepius/review/pair/{tid}", json=_adjudication(),
                    headers=A.headers_for(rv))
    assert r.status_code == 200, r.text
    row = asc_store.get_store().reviews_for_task(tid)[0]
    assert row["step_divergence"] is None


def test_an_empty_array_is_a_real_finding_when_both_sides_had_steps():
    admin_h = _admin_h()
    tid = _paired_task(admin_h, steps_a=_STEPS, steps_b=_STEPS)
    rv = _reviewer()
    _draw_pair(rv)
    r = client.post(f"/api/asclepius/review/pair/{tid}",
                    json=_adjudication(step_divergence=[]),
                    headers=A.headers_for(rv))
    assert r.status_code == 200, r.text
    row = asc_store.get_store().reviews_for_task(tid)[0]
    stored = row["step_divergence"]
    if isinstance(stored, str):
        import json
        stored = json.loads(stored)
    assert stored == []


@pytest.mark.parametrize("bad", [
    [{"index": -1, "judged": "A"}],
    [{"index": 99, "judged": "A"}],
    [{"index": 0, "judged": "C"}],
    [{"index": 0, "judged": "A"}, {"index": 0, "judged": "B"}],
    [{"index": 0, "judged": "A", "sneaky": 1}],
    ["not an object"],
    "not a list",
])
def test_a_malformed_divergence_is_a_400_not_a_silent_drop(bad):
    admin_h = _admin_h()
    tid = _paired_task(admin_h, steps_a=_STEPS, steps_b=_STEPS)
    rv = _reviewer()
    _draw_pair(rv)
    r = client.post(f"/api/asclepius/review/pair/{tid}",
                    json=_adjudication(step_divergence=bad),
                    headers=A.headers_for(rv))
    assert r.status_code in (400, 422), r.text


def test_the_divergence_array_is_bounded():
    """A hand-crafted POST must not store an unbounded array. No pair of clinical
    reasoning traces has 200 steps; a payload that claims to is not a review."""
    errors = asc_review.validate_step_divergence(
        [{"index": i, "judged": None} for i in range(asc_review.MAX_DIVERGENCE_STEPS + 1)],
        both_sides_have_steps=True, n_steps=10_000)
    assert errors and "at most" in errors[0]


def test_from_scratch_reasoning_steps_count_as_reasoning_steps():
    """A from-scratch answer carries its steps one level down. Both shapes are
    'this physician produced step-level reasoning', and §3's rule has to see them
    the same way or an honest divergence is refused as a fabrication."""
    assert asc_review.submission_reasoning_steps(
        {"payload": {"reasoning_steps": [{"text": "x"}]}})
    assert asc_review.submission_reasoning_steps(
        {"payload": {"from_scratch": {"reasoning_steps": [{"text": "x"}]}}})
    assert asc_review.submission_reasoning_steps({"payload": {}}) == []
    assert asc_review.submission_reasoning_steps({"payload": {"reasoning_steps": []}}) == []


# ═══ §3 — it reaches the buyer bundle ════════════════════════════════════════
def test_the_export_annex_carries_step_divergence_only_when_it_was_measured():
    measured = asc_packaging.review_block([{
        "verdict": "accept", "dimension_json": "{}", "blinded": 1,
        "step_divergence": '[{"index": 1, "judged": "A", "judged_submission_id": "s-1"}]',
    }])
    entry = measured["reviews"][0]
    assert entry["step_divergence"] == [{"index": 1, "judged": "A"}]
    # The internal submission id is NOT in a buyer annex.
    assert "judged_submission_id" not in entry["step_divergence"][0]

    # Compared, agreed everywhere: a real finding, and it ships as one.
    agreed = asc_packaging.review_block([{
        "verdict": "accept", "dimension_json": "{}", "blinded": 1,
        "step_divergence": "[]",
    }])
    assert agreed["reviews"][0]["step_divergence"] == []

    # Never measured: ABSENT, not [].
    unmeasured = asc_packaging.review_block([{
        "verdict": "accept", "dimension_json": "{}", "blinded": 1,
        "step_divergence": None,
    }])
    assert "step_divergence" not in unmeasured["reviews"][0]


def test_the_review_annex_still_carries_everything_it_did_before():
    """§0: nothing in the existing annex may be lost to this change."""
    block = asc_packaging.review_block([{
        "reviewer_id_hashed": "h-1", "verdict": "accept_with_edits",
        "dimension_json": '{"clinical_accuracy": "cannot_assess"}',
        "corrections_json": '{"notes": "raise the dose"}',
        "identifier_flags": "[]", "blinded": 1, "created_at": "2026-01-01T00:00:00",
        "step_divergence": None,
    }])
    entry = block["reviews"][0]
    for key in ("verdict", "dimensions", "corrections", "corrections_withheld",
                "reviewer_credential", "blinded", "reviewed_at", "reviewer_id_hashed"):
        assert key in entry, key
    # cannot_assess survives as its own state and is never folded into disagree.
    assert entry["dimensions"]["clinical_accuracy"] == "cannot_assess"
    assert block["reviewed"] is True and block["n_reviews"] == 1


def test_review_acceptance_and_kappa_remain_separately_named():
    """§0 / §7. The commercial argument depends on these being two figures with
    two names. Collapsing them is the one documentation change that would cost
    money."""
    doc = (Path(__file__).resolve().parents[1] / "asclepius" / "export.py").read_text(
        encoding="utf-8")
    assert "Expert review is NOT inter-rater agreement" in doc
    assert "two separately named" in doc
    from asclepius import agreement as asc_agreement
    assert hasattr(asc_agreement, "review_acceptance")


# ═══ §4.1 — the preview guard ════════════════════════════════════════════════
def test_a_preview_draw_serves_a_pair_claims_nothing_and_opens_no_session():
    admin_h = _admin_h()
    store = asc_store.get_store()
    tid = _paired_task(admin_h)
    admin = A.make_user(store, role="admin")

    body = _draw_pair(admin, preview=True)
    assert body["preview"] is True
    assert body["pair"]["preview"] is True
    assert body["pair"]["task_id"] == tid
    assert body["session"] is None, "a preview opened a billable session"
    # Nothing was claimed: the task is untouched and a real reviewer still gets it.
    task = store.get_task(tid)
    assert task["review_status"] in (None, "")
    assert task["review_claimed_by"] is None
    assert _draw_pair(_reviewer())["pair"]["task_id"] == tid


def test_submitting_a_preview_draw_is_a_409_not_a_silent_no_op():
    admin_h = _admin_h()
    tid = _paired_task(admin_h)
    admin = A.make_user(asc_store.get_store(), role="admin")
    drawn = _draw_pair(admin, preview=True)["pair"]

    r = client.post(f"/api/asclepius/review/pair/{tid}",
                    json=_adjudication(draw_token=drawn["draw_token"]),
                    headers=A.headers_for(admin))
    assert r.status_code == 409, r.text
    assert "preview" in r.text.lower()
    assert not asc_store.get_store().reviews_for_task(tid)


def test_an_operator_is_forced_into_preview_even_without_asking():
    """The §4 failure, exactly: 'the first time you click through the reviewer
    preview you adjudicate a real pair'. An admin with no reviewer tier reaches
    this queue through the capability override, so the plain draw previews."""
    admin_h = _admin_h()
    tid = _paired_task(admin_h)
    admin = A.make_user(asc_store.get_store(), role="admin")

    body = _draw_pair(admin)                       # NO ?preview=true
    assert body["preview"] is True
    assert body["session"] is None
    assert asc_store.get_store().get_task(tid)["review_claimed_by"] is None

    # ...and their submit is refused even with no token at all.
    r = client.post(f"/api/asclepius/review/pair/{tid}", json=_adjudication(),
                    headers=A.headers_for(admin))
    assert r.status_code == 409, r.text
    assert not asc_store.get_store().reviews_for_task(tid)


def test_an_admin_who_actually_holds_the_reviewer_tier_is_not_forced_into_preview():
    """The tier is what says 'reviewer'; the admin override only says 'may look'.
    Collapsing the two would lock a working reviewer out of their own queue."""
    admin_h = _admin_h()
    tid = _paired_task(admin_h)
    staff = _admin_reviewer()

    body = _draw_pair(staff)
    assert not body.get("preview")
    assert asc_store.get_store().get_task(tid)["review_claimed_by"] == staff["id"]
    r = client.post(f"/api/asclepius/review/pair/{tid}", json=_adjudication(),
                    headers=A.headers_for(staff))
    assert r.status_code == 200, r.text


def test_a_real_reviewer_cannot_ask_for_a_preview():
    """A reviewer asking for a preview is asking to work for free, and a labeler
    asking for one is asking to read a pair they were excluded from."""
    admin_h = _admin_h()
    _paired_task(admin_h)
    r = client.get("/api/asclepius/review/pair/next?preview=true",
                   headers=A.headers_for(_reviewer()))
    assert r.status_code == 403, r.text


def test_review_me_tells_the_client_whether_it_is_previewing():
    """The client never re-derives 'is this a real reviewer' from a tier string —
    that is the two-state check this codebase removed on purpose."""
    store = asc_store.get_store()
    admin = A.make_user(store, role="admin")
    r = client.get("/api/asclepius/review/me", headers=A.headers_for(admin))
    assert r.status_code == 200, r.text
    assert r.json()["preview_only"] is True

    r = client.get("/api/asclepius/review/me", headers=A.headers_for(_reviewer()))
    assert r.json()["preview_only"] is False
    assert r.json()["can_review"] is True


def test_a_preview_never_parks_a_case_two_physicians_were_paid_for():
    """A preview WRITES NOTHING — not even the not-independent park. An operator
    looking around must not retire a case; the next real draw does that, which is
    where the decision belongs."""
    admin_h = _admin_h()
    store = asc_store.get_store()
    tid = _create_task(admin_h, max_labels=2)
    one = _labeler()
    _reveal(tid, one)
    _submit(tid, one)
    # A second label from the SAME physician: not a pair. Written directly, since
    # every queue refuses to produce this shape.
    _submit(tid, one, verdict="B_better")

    admin = A.make_user(store, role="admin")
    assert _draw_pair(admin, preview=True)["pair"] is None
    assert store.get_task(tid)["review_status"] in (None, "")


# ═══ §2.1 — the old URL still lands somewhere ════════════════════════════════
def test_the_retired_review_page_redirects_into_the_shell():
    """The standalone page is gone (§2.1), but its URL is in bookmarks and in
    email we have already sent. A dead link is a reviewer who cannot find their
    work."""
    r = client.get("/asclepius/review", follow_redirects=False)
    assert r.status_code in (307, 308), r.text
    assert r.headers["location"] == "/asclepius#review"


# ═══ the operator roles: admin is not the only one ═══════════════════════════
def _qa_reviewer_with_tier():
    """A `qa_reviewer` who also holds the reviewer tier.

    This account is the seam between two different definitions of "staff".
    `capabilities.granted` overrides for role 'admin' ALONE, so a qa_reviewer's
    review access comes from their TIER — while the portal's header treats
    qa_reviewer as an admin and shows them the Evaluate chooser. Get the two
    definitions out of step and the product draws a button that 403s.
    """
    store = asc_store.get_store()
    user = A.make_user(store, role="qa_reviewer", specialty="nephrology",
                       board_cert="board_certified_nephrology", years_experience=15)
    _grant_tier(store, user["id"], "reviewer")
    return store.get_user_by_id(user["id"])


def test_a_qa_reviewer_may_ask_for_the_preview_the_portal_offers_them():
    """The portal shows the Evaluate chooser to admin AND qa_reviewer. Both must
    therefore be able to draw a preview, or one of them clicks a control we drew
    ourselves and gets a 403."""
    admin_h = _admin_h()
    tid = _paired_task(admin_h)
    qa = _qa_reviewer_with_tier()

    body = _draw_pair(qa, preview=True)
    assert body["preview"] is True
    assert body["pair"]["task_id"] == tid
    assert body["session"] is None
    assert asc_store.get_store().get_task(tid)["review_claimed_by"] is None


def test_a_qa_reviewer_with_the_tier_still_does_real_work_by_default():
    """The preview is something they ASK for, not a cage. Their plain draw is a
    real one, because their tier — not an override — is what admits them."""
    admin_h = _admin_h()
    tid = _paired_task(admin_h)
    qa = _qa_reviewer_with_tier()

    body = _draw_pair(qa)
    assert not body.get("preview")
    assert asc_store.get_store().get_task(tid)["review_claimed_by"] == qa["id"]
    r = client.post(f"/api/asclepius/review/pair/{tid}", json=_adjudication(),
                    headers=A.headers_for(qa))
    assert r.status_code == 200, r.text


def test_every_draw_response_says_whether_it_was_a_preview():
    """Including the ones carrying no pair. The client keeps the banner up across
    an empty queue, and a response that describes itself is one less thing for it
    to remember."""
    store = asc_store.get_store()
    admin = A.make_user(store, role="admin")
    empty = _draw_pair(admin, preview=True)          # nothing in the queue at all
    assert empty["pair"] is None
    assert empty["preview"] is True

    rv = _reviewer()
    assert _draw_pair(rv)["preview"] is False
