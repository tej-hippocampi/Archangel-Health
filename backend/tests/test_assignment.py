"""Assigning cases to doctors, and the one way it could break everything.

There was no assignment concept at any layer. A hundred promoted nephrology
cases reached physicians purely by pull from a specialty-filtered, oldest-first
queue announced by one email: no load balancing, no per-doctor cap, no
reservation, no matching on domain fit or on the contributor score. One fast
labeler could take all hundred.

The most important test in this file is
``test_an_assignment_is_a_sort_and_never_a_filter``. store.py already states the
law for the second-label priority term, and test_routing_priority pins it: the
moment priority becomes a WHERE clause, a labeler with no eligible work sees an
empty queue and stops working. The argument is stronger here, because an
assignment names ONE person, so as a filter it would empty the queue for
everyone who has not been allocated anything yet, which on the day this ships is
everyone.
"""

from __future__ import annotations

import pytest

from tests._asclepius import fresh_store, headers_for, make_user

from asclepius.allocation import Case, Physician, allocate


@pytest.fixture()
def store():
    return fresh_store()


def _docs(n=10, reviewers=5, **kw):
    return [
        Physician(user_id=f"u{i}", can_label=True, can_review=(i < reviewers),
                  domain_match=1.0, contributor_score=90 - i * 3,
                  real_data_approved=True, **kw)
        for i in range(n)
    ]


def _cases(n=100, **kw):
    return [Case(task_id=f"t{i}", specialty="nephrology", difficulty=0.7, **kw)
            for i in range(n)]


# ─── The worked example ──────────────────────────────────────────────────────

def test_a_hundred_cases_spread_across_the_pool_instead_of_landing_on_one_person():
    """The behaviour that did not exist. One fast labeler could take all 100."""
    p = allocate(_cases(100), _docs(10, reviewers=5))
    labelers = {a["user_id"] for a in p.assignments if a["role"] == "label"}
    assert len(labelers) >= 8, f"only {len(labelers)} physicians got labeling work"
    assert not p.unassigned


def test_reviewers_come_from_the_reviewer_capable_pool_only():
    p = allocate(_cases(100), _docs(10, reviewers=5))
    reviewers = {a["user_id"] for a in p.assignments if a["role"] == "review"}
    assert reviewers <= {"u0", "u1", "u2", "u3", "u4"}


def test_nobody_takes_more_than_their_share_of_a_batch():
    p = allocate(_cases(100), _docs(10, reviewers=5), max_share=0.2)
    slots = 100 * 3
    cap = int(slots * 0.2)
    assert all(c["total"] <= cap for c in p.per_physician.values())


def test_every_case_gets_two_independent_labels():
    p = allocate(_cases(20), _docs(10, reviewers=5))
    by_case = {}
    for a in p.assignments:
        by_case.setdefault(a["task_id"], []).append(a)
    for tid, rows in by_case.items():
        labels = [r for r in rows if r["role"] == "label"]
        assert len(labels) == 2, tid
        assert len({r["user_id"] for r in labels}) == 2, tid


def test_the_reviewer_of_a_case_never_also_labels_it():
    """It would defeat the independence the second blind label exists to
    provide, and the review draw excludes an author in SQL anyway, so an
    allocation that proposed it would simply never be servable."""
    p = allocate(_cases(30), _docs(10, reviewers=5))
    by_case = {}
    for a in p.assignments:
        by_case.setdefault(a["task_id"], []).append(a)
    for tid, rows in by_case.items():
        labelers = {r["user_id"] for r in rows if r["role"] == "label"}
        reviewers = {r["user_id"] for r in rows if r["role"] == "review"}
        assert not (labelers & reviewers), tid


# ─── Eligibility is read, never invented ─────────────────────────────────────

def test_a_physician_with_no_domain_match_is_never_allocated():
    """A distinguished cardiologist scores 0.0 on a nephrology case, and that is
    the correct answer rather than a bug in the encoding."""
    docs = [Physician(user_id="cardio", can_label=True, domain_match=0.0,
                      real_data_approved=True)]
    p = allocate(_cases(3), docs)
    assert p.assignments == []
    assert len(p.unassigned) == 3


def test_real_deid_cases_go_only_to_cleared_physicians():
    docs = [Physician(user_id=f"u{i}", can_label=True, domain_match=1.0,
                      real_data_approved=False) for i in range(5)]
    p = allocate(_cases(2, real_deid=True), docs)
    assert p.assignments == []
    assert all("real de-identified" in u["reason"] for u in p.unassigned)


def test_a_case_nobody_can_take_says_why_rather_than_vanishing():
    """"0 assigned" sends an operator to the database. A named reason sends them
    to the screen that fixes it."""
    p = allocate(_cases(1, real_deid=True),
                 [Physician(user_id="u1", can_label=True, domain_match=1.0)])
    assert p.unassigned and p.unassigned[0]["reason"]


def test_a_case_with_only_one_eligible_labeler_says_it_will_not_pair():
    docs = [Physician(user_id="only", can_label=True, domain_match=1.0,
                      real_data_approved=True)]
    p = allocate(_cases(1), docs, reviewers_per_case=0)
    assert any("agreement pair" in n for n in p.notes)


def test_a_review_is_never_assigned_on_a_case_nobody_is_labelling():
    """A reviewer waiting on work that will never arrive."""
    docs = [Physician(user_id="rev", can_label=False, can_review=True,
                      domain_match=1.0, real_data_approved=True)]
    p = allocate(_cases(2), docs)
    assert p.assignments == []


# ─── It cannot weigh what it cannot see ──────────────────────────────────────

def test_the_allocator_sees_no_protected_attribute():
    """Same argument as payout.py's signature test and tiering's encoder-level
    frozenset: a field it cannot receive is one it cannot weigh.

    This frozenset is a posture, not an inventory. Adding a name to it is a
    decision that the new field is one allocation may look at, and the two tests
    below are what that decision has to survive."""
    fields = set(Physician.__dataclass_fields__)
    assert fields == {
        "user_id", "can_label", "can_review", "domain_match",
        "contributor_score", "real_data_approved", "open_assignments",
        "profile_depth",
    }


def test_profile_depth_is_made_only_of_clinical_self_description():
    """The one field added to the frozen shape has to answer for itself.

    Every member of DEPTH_FIELDS is something a physician says about their own
    clinical practice, and none of it is a credential key the tiering encoder is
    forbidden to read. If that ever stops being true, the allocator has become a
    second, unaudited route to a protected attribute."""
    from asclepius.allocation import DEPTH_FIELDS
    from asclepius.tiering import FORBIDDEN_CREDENTIAL_KEYS, PINNED_ZERO

    assert set(DEPTH_FIELDS) & set(FORBIDDEN_CREDENTIAL_KEYS) == set()
    assert set(DEPTH_FIELDS) & set(PINNED_ZERO) == set()


def test_where_a_physician_practises_never_raises_their_standing():
    """The completeness meter counts practice_city; routing depth must not.

    tiering pins practice_region at exactly zero forever and forbids
    practiceZip/zipCode/practiceRegion from becoming features. A city is the
    same quantity at a finer grain, so counting it toward routing standing would
    walk around a fairness guardrail through a proxy while looking like a
    completeness bonus."""
    from asclepius.allocation import DEPTH_FIELDS, profile_depth

    assert not [f for f in DEPTH_FIELDS
                if "city" in f or "region" in f or "zip" in f]
    # Two physicians identical but for a city answer score identically.
    answered = ["subspecialties", "languages"]
    assert profile_depth(answered) == profile_depth(answered + ["practice_city"])


def test_a_fuller_profile_is_offered_the_case_before_an_emptier_one():
    """The physician profile PRD's actual promise, made true.

    Two unrated physicians, equally matched and equally idle, differ only in how
    much of their profile they filled in. The fuller one gets the case. Without
    this the completeness meter asks a busy clinician for information in
    exchange for nothing, which is what shipped."""
    docs = [
        Physician(user_id="sparse", can_label=True, domain_match=1.0,
                  contributor_score=None, real_data_approved=True,
                  profile_depth=0.0),
        Physician(user_id="full", can_label=True, domain_match=1.0,
                  contributor_score=None, real_data_approved=True,
                  profile_depth=1.0),
    ]
    p = allocate(_cases(1), docs, labels_per_case=1, reviewers_per_case=0)
    assert [a["user_id"] for a in p.assignments] == ["full"]


def test_profile_depth_never_outranks_evidence_of_how_well_someone_works():
    """Self-description is cheap and a contributor score has to be earned.

    A physician with an empty profile and a real track record must still be
    preferred over a fully-filled-in profile with a worse one, or a doctor could
    talk their way past a better labeler."""
    docs = [
        Physician(user_id="better_worker", can_label=True, domain_match=1.0,
                  contributor_score=90.0, real_data_approved=True,
                  profile_depth=0.0),
        Physician(user_id="fuller_profile", can_label=True, domain_match=1.0,
                  contributor_score=60.0, real_data_approved=True,
                  profile_depth=1.0),
    ]
    p = allocate(_cases(1), docs, labels_per_case=1, reviewers_per_case=0)
    assert [a["user_id"] for a in p.assignments] == ["better_worker"]


def test_profile_depth_never_concentrates_a_batch_on_one_person():
    """The spread guarantee outranks profile depth, deliberately.

    One physician with a perfect profile and nine with none must not collect the
    batch: load sorts above depth in the rank key, so the fuller profile wins
    ties and never wins a pile-up."""
    docs = [Physician(user_id="full", can_label=True, domain_match=1.0,
                      contributor_score=None, real_data_approved=True,
                      profile_depth=1.0)]
    docs += [Physician(user_id=f"u{i}", can_label=True, domain_match=1.0,
                       contributor_score=None, real_data_approved=True,
                       profile_depth=0.0) for i in range(9)]
    p = allocate(_cases(50), docs, reviewers_per_case=0)
    counts = {u: c["total"] for u, c in p.per_physician.items()}
    assert counts.get("full", 0) <= int(50 * 2 * 0.35)
    assert len([u for u in counts if counts[u]]) >= 8


def test_the_pay_half_of_the_promise_is_off_and_says_so():
    """The profile PRD promises richer profiles route better AND pay more.

    Routing is shipped; pay is a founder decision about what the company pays
    for, because it needs a number nobody has chosen. It is inert by a named
    constant rather than by omission, so the status is readable in the code
    instead of guessable from a PRD."""
    from asclepius.payout import (
        PROFILE_DEPTH_PAY_BONUS_MAX, profile_depth_multiplier,
    )

    assert PROFILE_DEPTH_PAY_BONUS_MAX == 0.0
    for depth in (0.0, 0.5, 1.0):
        assert profile_depth_multiplier(depth) == 1.0


def test_an_unrated_physician_is_not_sorted_to_the_bottom_forever():
    """Sorting the unrated last means nobody new is ever allocated work, which
    is the loop that stops them ever being rated."""
    docs = [
        Physician(user_id="rated", can_label=True, domain_match=1.0,
                  contributor_score=52.0, real_data_approved=True),
        Physician(user_id="new", can_label=True, domain_match=1.0,
                  contributor_score=None, real_data_approved=True),
    ]
    p = allocate(_cases(4), docs, reviewers_per_case=0)
    assert "new" in {a["user_id"] for a in p.assignments}


def test_the_same_inputs_always_give_the_same_proposal():
    """So an operator can diff a proposal against the last one."""
    a = allocate(_cases(20), _docs(6, reviewers=3))
    b = allocate(_cases(20), _docs(6, reviewers=3))
    assert a.assignments == b.assignments


def test_no_cases_and_no_physicians_are_answered_rather_than_crashed():
    assert allocate([], _docs()).assignments == []
    assert allocate(_cases(2), []).unassigned


# ─── The queue: a sort, never a filter ───────────────────────────────────────

def _task(store, admin_id, **kw):
    return store.insert_task(
        prompt=kw.pop("prompt", "p"), specialty="nephrology", difficulty="hard",
        candidate_answers=[{"id": "A", "text": "a"}, {"id": "B", "text": "b"}],
        created_by=admin_id, **kw)


def test_an_assignment_is_a_sort_and_never_a_filter(store):
    """THE test. As a filter, every labeler with no assignment sees an empty
    queue, and on the day this ships that is everyone."""
    admin = make_user(store, role="admin")
    mine = _task(store, admin["id"], prompt="assigned to somebody else")
    theirs = _task(store, admin["id"], prompt="assigned to nobody")

    assigned_to = make_user(store, role="evaluator", specialty="nephrology")
    unassigned_doc = make_user(store, role="evaluator", specialty="nephrology")
    store.upsert_assignment(task_id=mine["task_id"], user_id=assigned_to["id"],
                            role="label", assigned_by="admin@example.org")

    # The physician holding no assignment still sees work.
    sql, params = store.labeler_queue_sql(
        evaluator_id=unassigned_doc["id"], specialty="nephrology")
    with store._conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    seen = {r["task_id"] for r in rows}
    assert mine["task_id"] in seen, "an assigned case vanished from everyone else's queue"
    assert theirs["task_id"] in seen


def test_an_assigned_case_sorts_to_the_top_of_its_assignees_queue(store):
    admin = make_user(store, role="admin")
    # Created FIRST, so oldest-first would put it top without the new term.
    older = _task(store, admin["id"], prompt="older, unassigned")
    newer = _task(store, admin["id"], prompt="newer, assigned to me")

    doc = make_user(store, role="evaluator", specialty="nephrology")
    store.upsert_assignment(task_id=newer["task_id"], user_id=doc["id"],
                            role="label", assigned_by="admin@example.org")

    sql, params = store.labeler_queue_sql(evaluator_id=doc["id"], specialty="nephrology")
    with store._conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    assert rows[0]["task_id"] == newer["task_id"]
    assert older["task_id"] in {r["task_id"] for r in rows}


def test_a_revoked_assignment_stops_prioritising(store):
    admin = make_user(store, role="admin")
    older = _task(store, admin["id"], prompt="older")
    newer = _task(store, admin["id"], prompt="newer")
    doc = make_user(store, role="evaluator", specialty="nephrology")
    row = store.upsert_assignment(task_id=newer["task_id"], user_id=doc["id"],
                                  role="label", assigned_by="admin@example.org")
    store.set_assignment_status(row["assignment_id"], "revoked")

    sql, params = store.labeler_queue_sql(evaluator_id=doc["id"], specialty="nephrology")
    with store._conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    assert rows[0]["task_id"] == older["task_id"]


def test_reassigning_the_same_case_does_not_duplicate_the_queue(store):
    admin = make_user(store, role="admin")
    t = _task(store, admin["id"])
    doc = make_user(store, role="evaluator", specialty="nephrology")
    store.upsert_assignment(task_id=t["task_id"], user_id=doc["id"], role="label",
                            assigned_by="a@x.org")
    store.upsert_assignment(task_id=t["task_id"], user_id=doc["id"], role="label",
                            assigned_by="a@x.org")
    assert len(store.assignments_for_task(t["task_id"])) == 1


def test_an_exclusive_assignment_expires_back_into_the_pool(store):
    """An exclusive assignment with no timeout is a queue that wedges the moment
    somebody goes on holiday."""
    admin = make_user(store, role="admin")
    t = _task(store, admin["id"])
    doc = make_user(store, role="evaluator", specialty="nephrology")
    store.upsert_assignment(task_id=t["task_id"], user_id=doc["id"], role="label",
                            assigned_by="a@x.org", exclusive=True,
                            expires_at="2020-01-01T00:00:00Z")
    assert store.expire_stale_assignments() == 1
    assert store.assignments_for_user(doc["id"]) == []


# ─── The admin surface ───────────────────────────────────────────────────────

def _admin_client(store):
    from fastapi.testclient import TestClient

    from tests._asclepius import app

    admin = make_user(store, role="admin")
    return TestClient(app), headers_for(admin), admin


def test_a_dry_run_proposes_and_writes_nothing(store):
    """Same shape as ingest promotion: an admin iterates on an allocation before
    a physician is told to do anything."""
    client, h, admin = _admin_client(store)
    tasks = [_task(store, admin["id"]) for _ in range(4)]
    for i in range(4):
        make_user(store, role="evaluator", specialty="nephrology")

    resp = client.post("/api/asclepius/admin/assignments/allocate",
                       json={"task_ids": [t["task_id"] for t in tasks]}, headers=h)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is True
    assert body["committed"] == []
    assert store.assignments_for_task(tasks[0]["task_id"]) == []


def test_committing_writes_the_rows(store):
    client, h, admin = _admin_client(store)
    tasks = [_task(store, admin["id"]) for _ in range(4)]
    for _ in range(4):
        make_user(store, role="evaluator", specialty="nephrology")

    resp = client.post("/api/asclepius/admin/assignments/allocate",
                       json={"task_ids": [t["task_id"] for t in tasks],
                             "dry_run": False}, headers=h)
    assert resp.status_code == 200, resp.text
    assert resp.json()["committed"]
    assert store.assignments_for_task(tasks[0]["task_id"])


def test_the_proposal_names_who_could_not_be_placed_and_why(store):
    client, h, admin = _admin_client(store)
    t = _task(store, admin["id"])
    # case_source is set by the promotion path, not by insert_task's signature.
    with store._conn() as conn:
        conn.execute("UPDATE tasks SET case_source = 'real_deid' WHERE task_id = ?",
                     (t["task_id"],))
    make_user(store, role="evaluator", specialty="nephrology")  # not cleared

    resp = client.post("/api/asclepius/admin/assignments/allocate",
                       json={"task_ids": [t["task_id"]]}, headers=h)
    body = resp.json()
    assert body["unassigned"]
    assert body["unassigned"][0]["reason"]


def test_revoking_returns_the_case_to_the_ordinary_queue(store):
    client, h, admin = _admin_client(store)
    t = _task(store, admin["id"])
    doc = make_user(store, role="evaluator", specialty="nephrology")
    row = store.upsert_assignment(task_id=t["task_id"], user_id=doc["id"],
                                  role="label", assigned_by="a@x.org")

    resp = client.post(
        f"/api/asclepius/admin/assignments/{row['assignment_id']}/revoke", headers=h)
    assert resp.status_code == 200
    assert store.assignments_for_user(doc["id"]) == []


def test_allocating_nothing_is_a_400_not_an_empty_success(store):
    client, h, _admin = _admin_client(store)
    resp = client.post("/api/asclepius/admin/assignments/allocate",
                       json={"task_ids": []}, headers=h)
    assert resp.status_code == 400


def test_only_an_admin_may_allocate(store):
    from fastapi.testclient import TestClient

    from tests._asclepius import app

    doc = make_user(store, role="evaluator", specialty="nephrology")
    with TestClient(app) as client:
        resp = client.post("/api/asclepius/admin/assignments/allocate",
                           json={"task_ids": ["t-1"]}, headers=headers_for(doc))
    assert resp.status_code in (401, 403)
