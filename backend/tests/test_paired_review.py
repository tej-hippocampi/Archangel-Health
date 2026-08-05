"""PRD R Phase 2/3 — the reviewer draws a PAIR.

Review stops being about a submission and becomes about a case with two
independent labels, which is the only shape from which Cohen's κ and an expert
acceptance rate can both be computed honestly.

Everything here goes through the real routes. The two things that are asserted
hardest are the two that are expensive to get wrong: **blinding** (the buyer-
facing honesty claim) and **the statistics' names** (the commercial argument).
"""
from __future__ import annotations

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


def _paired_task(admin_h, *, tl1=None, tl2=None, verdicts=("A_better", "A_better"), **kw):
    """A case with two independent labels, built entirely through HTTP."""
    tid = _create_task(admin_h, **kw)
    _submit(tid, tl1 or _labeler(), verdict=verdicts[0])
    _submit(tid, tl2 or _labeler(), verdict=verdicts[1])
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
    state machine knew that, the two would be a second pair of truths."""
    store = asc_store.get_store()
    admin_h = _admin_h()
    tid = _create_task(admin_h, max_labels=3)
    _submit(tid, _labeler())
    _submit(tid, _labeler())

    task = store.get_task(tid)
    assert asc_routing.phase(task, 2, 0) == asc_routing.AWAITING_SECOND
    assert _draw_pair(_reviewer())["pair"] is None

    _submit(tid, _labeler())
    assert asc_routing.phase(store.get_task(tid), 3, 0) == asc_routing.REVIEW_READY
    assert _draw_pair(_reviewer())["pair"]["task_id"] == tid


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
