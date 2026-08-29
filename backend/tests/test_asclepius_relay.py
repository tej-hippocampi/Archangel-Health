"""Relay mode — one chart, N physicians, one decision point each (§8).

A different product from the solo walk, not a variant of it. The solo walk
captures one physician's judgment evolving over a patient; the relay captures how
clinicians build on each other's reasoning, because doctor k reads doctor k−1's
committed assessment before writing their own — a care-team handoff.

Three things here are load-bearing, and each of them can fail silently.

**The gate inverts, and both halves are required.** Solo asks "have YOU done the
earlier points". Relay asks "has the CHART got past them" AND "is it your turn",
because the previous point was somebody else's by design. Drop the second half and
every doctor on the relay can open every unlocked point; drop the first and the
order means nothing. It is tempting to lean on ``distribution='assigned_only'`` for
the turn-taking — it does happen to enforce it today — but a seal that depends on
another switch's current value is not a seal, so the gate carries both itself and
a test proves it by flipping distribution to 'open' underneath.

**The handoff must carry the commitment and nothing else.** The predecessor's
reveal outcome and self-score are precisely what this physician is being asked to
predict. Shipping them turns the relay into reading comprehension and destroys the
verifiable claim for the point — the same unrecoverable loss the sequence gate
exists to prevent, arriving through a different door.

**Solo must be untouched.** Every assertion about relay here is worth nothing if
the mode branch quietly changed what a solo walk does, so the solo rules are
re-asserted rather than assumed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import trajectory as TJ  # noqa: E402

client = TestClient(A.app)


@pytest.fixture(autouse=True)
def _isolated():
    A.fresh_store()
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


def _admin(store):
    return A.headers_for(A.make_user(store, role="admin"))


def _doc(store, *, specialty="cardiology"):
    u = A.make_user(store, specialty=specialty, tier="labeler")
    store.set_real_data_approved(u["id"], True)
    return store.get_user_by_id(u["id"])


def _walk(store, n=4, *, tid="traj-relay", specialty="cardiology"):
    return [store.insert_task(prompt=f"point {i}", specialty=specialty,
                              trajectory_id=tid, sequence_index=i,
                              distribution="assigned_only")
            for i in range(n)]


def _submit(store, task_id, evaluator_id, *, answer="my read of it"):
    store.commit_independent_answer(
        task_id=task_id, evaluator_id=evaluator_id,
        payload={"text": answer, "kind": "full", "portal_version": "v4"})
    return store.insert_submission(
        submission_id=f"s-{evaluator_id}-{task_id[-6:]}", task_id=task_id,
        evaluator_id=evaluator_id, verdict="A_better", chosen_id="a",
        rejected_id="b", confidence="high", time_spent_sec=300,
        payload={}, annotator={}, dedupe_hash=None)


def _queue(store, user_id, *, specialty="cardiology"):
    return [t["task_id"] for t in store.eligible_tasks_for_evaluator(
        evaluator_id=user_id, specialty=specialty)]


def _relay(store, points, doctors):
    """Send a walk as a relay, by hand, in the shape the endpoint commits."""
    rotation = TJ.relay_rotation(len(points), [d["id"] for d in doctors], seed=1)
    for pt, uid in zip(points, rotation):
        store.upsert_assignment(task_id=pt["task_id"], user_id=uid, role="label",
                                assigned_by="u-admin")
    store.set_walk_mode([p["task_id"] for p in points], TJ.WALK_MODE_RELAY)
    return rotation


# ═══════════════════════════════════════════════════════════════════════════════
# The rotation
# ═══════════════════════════════════════════════════════════════════════════════
def test_adjacent_points_go_to_different_physicians():
    """Otherwise a "handoff" is a physician reading their own note back."""
    r = TJ.relay_rotation(13, list("abcde"), seed=7)
    assert len(r) == 13
    assert all(r[i] != r[i + 1] for i in range(12))


def test_the_load_is_even_and_the_first_point_is_not_always_the_same_person():
    """Point 0 is the only point with no handoff to read, so it is systematically
    the easiest — always giving it to whoever sorts first is a quiet bias."""
    from collections import Counter
    assert sorted(Counter(TJ.relay_rotation(13, list("abcde"), seed=7)).values()) == [2, 2, 3, 3, 3]
    firsts = {TJ.relay_rotation(5, list("abcde"), seed=s)[0] for s in range(30)}
    assert len(firsts) > 1, "the shuffle must actually vary who starts"


def test_the_rotation_is_reproducible_so_preview_equals_commit():
    """The admin is shown a mapping and commits it. If preview and commit were two
    draws from the same distribution, the screen would be a lie they cannot see."""
    assert TJ.relay_rotation(9, list("abc"), seed=42) == TJ.relay_rotation(9, list("abc"), seed=42)


# ═══════════════════════════════════════════════════════════════════════════════
# The gate
# ═══════════════════════════════════════════════════════════════════════════════
def test_only_the_first_point_is_serveable_on_send():
    store = _store()
    a, b = _doc(store), _doc(store)
    pts = _walk(store, 4)
    rotation = _relay(store, pts, [a, b])

    first_holder = rotation[0]
    other = b["id"] if first_holder == a["id"] else a["id"]
    assert _queue(store, first_holder) == [pts[0]["task_id"]]
    assert _queue(store, other) == [], "everyone else's assignment is held closed"


def test_a_relay_point_serves_only_to_its_assignee_even_once_unlocked():
    """The half that would be silently missing if the gate leaned on distribution."""
    store = _store()
    a, b = _doc(store), _doc(store)
    pts = _walk(store, 4)
    rotation = _relay(store, pts, [a, b])
    _submit(store, pts[0]["task_id"], rotation[0])

    owner = rotation[1]
    intruder = a["id"] if owner == b["id"] else b["id"]
    assert pts[1]["task_id"] in _queue(store, owner)
    assert pts[1]["task_id"] not in _queue(store, intruder), (
        "point 1 is unlocked but it is not this physician's turn")


def test_the_turn_rule_does_not_depend_on_the_distribution_switch():
    """A seal that holds only because another switch happens to be set is not a
    seal. Flip the walk to the open queue and the relay order must still hold."""
    store = _store()
    a, b = _doc(store), _doc(store)
    pts = _walk(store, 4)
    rotation = _relay(store, pts, [a, b])
    _submit(store, pts[0]["task_id"], rotation[0])
    store.set_task_distribution([p["task_id"] for p in pts], "open")

    intruder = a["id"] if rotation[1] == b["id"] else b["id"]
    assert pts[1]["task_id"] not in _queue(store, intruder)
    assert pts[1]["task_id"] in _queue(store, rotation[1])


def test_predecessors_may_be_completed_by_anyone_which_is_the_whole_point():
    """In solo this would still be blocked — the physician has not done point 0
    themselves. In relay that is exactly the intended handoff."""
    store = _store()
    a, b = _doc(store), _doc(store)
    pts = _walk(store, 3)
    rotation = _relay(store, pts, [a, b])
    _submit(store, pts[0]["task_id"], rotation[0])

    assert rotation[1] != rotation[0]
    assert pts[1]["task_id"] in _queue(store, rotation[1]), (
        "the chart advanced; the next physician has never touched this walk")


def test_the_by_id_path_agrees_with_the_queue():
    """If they disagreed, a point the queue offers would 409 when opened — or a
    point it withholds would open by id, which is the direction that leaks."""
    store = _store()
    a, b = _doc(store), _doc(store)
    pts = _walk(store, 4)
    rotation = _relay(store, pts, [a, b])
    intruder = a["id"] if rotation[1] == b["id"] else b["id"]
    ih = A.headers_for(store.get_user_by_id(intruder))

    r = client.get(f"/api/asclepius/tasks/{pts[1]['task_id']}", headers=ih)
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "trajectory_out_of_order"


def test_the_refusal_says_it_is_somebody_elses_turn_not_that_you_are_behind():
    """A relay doctor who has done nothing wrong must not be told to go finish
    earlier points that were never theirs."""
    store = _store()
    a, b = _doc(store), _doc(store)
    pts = _walk(store, 4)
    rotation = _relay(store, pts, [a, b])
    intruder = a["id"] if rotation[1] == b["id"] else b["id"]
    msg = client.get(f"/api/asclepius/tasks/{pts[1]['task_id']}",
                     headers=A.headers_for(store.get_user_by_id(intruder))
                     ).json()["detail"]["message"]
    assert "another physician" in msg or "not reached this point" in msg


# ═══════════════════════════════════════════════════════════════════════════════
# Solo, re-asserted — the mode branch must not have moved it
# ═══════════════════════════════════════════════════════════════════════════════
def test_solo_still_requires_the_same_evaluator_to_have_done_the_predecessors():
    store = _store()
    a, b = _doc(store), _doc(store)
    pts = _walk(store, 3)                       # never sent: walk_mode stays NULL
    # Routed to BOTH, so distribution is not what is doing the work here — this
    # test is about the sequence rule and nothing else. (That the unrouted version
    # showed an empty queue for everyone is §1 behaving correctly, and is asserted
    # in test_asclepius_distribution.)
    for pt in pts:
        for who in (a, b):
            store.upsert_assignment(task_id=pt["task_id"], user_id=who["id"],
                                    role="label", assigned_by="u-admin")
    _submit(store, pts[0]["task_id"], a["id"])

    assert _queue(store, a["id"]) == [pts[1]["task_id"]]
    assert pts[1]["task_id"] not in _queue(store, b["id"]), (
        "B did not answer point 0; in SOLO that still seals point 1 to them")


def test_an_unstamped_walk_reads_as_solo_the_stricter_rule():
    """NULL must get the stricter rule. The looser one would serve a legacy walk's
    later points to whoever happened to hold an assignment."""
    store = _store()
    a, b = _doc(store), _doc(store)
    pts = _walk(store, 3)
    assert TJ.walk_mode(store.get_task(pts[0]["task_id"])) == "solo"
    for pt in pts:
        store.upsert_assignment(task_id=pt["task_id"], user_id=b["id"],
                                role="label", assigned_by="u-admin")
    _submit(store, pts[0]["task_id"], a["id"])
    assert pts[1]["task_id"] not in _queue(store, b["id"])


# ═══════════════════════════════════════════════════════════════════════════════
# The handoff
# ═══════════════════════════════════════════════════════════════════════════════
def test_the_handoff_carries_the_commitment():
    store = _store()
    a, b = _doc(store), _doc(store)
    pts = _walk(store, 3)
    rotation = _relay(store, pts, [a, b])
    store.set_submission_expected_trajectory(
        _submit(store, pts[0]["task_id"], rotation[0],
                answer="biliary obstruction, stent it")["submission_id"],
        {"expectations": [{"expectation": "bilirubin falls", "horizon_days": 14}],
         "falsifiers": ["if GGT climbs the stent has occluded"]})

    served = client.get(f"/api/asclepius/tasks/{pts[1]['task_id']}",
                        headers=A.headers_for(store.get_user_by_id(rotation[1]))).json()
    ho = served["relay_handoff"]
    assert ho["from_sequence_index"] == 0
    assert "stent it" in ho["assessment"]
    assert "bilirubin falls" in ho["expectations"]
    assert any("GGT" in f for f in ho["falsifiers"])


def test_the_handoff_never_carries_the_reveal_or_the_self_score():
    """What the next physician is being asked to predict must not be handed to
    them. This is the leak that would make the whole relay worthless while every
    other test still passed."""
    store = _store()
    a, b = _doc(store), _doc(store)
    pts = _walk(store, 3)
    rotation = _relay(store, pts, [a, b])
    sub = _submit(store, pts[0]["task_id"], rotation[0])
    store.set_submission_trajectory_self_score(
        sub["submission_id"], {"marks": [{"index": 0, "state": "did_not_hold"}],
                               "falsifier_fired": True})

    served = client.get(f"/api/asclepius/tasks/{pts[1]['task_id']}",
                        headers=A.headers_for(store.get_user_by_id(rotation[1]))).json()
    blob = str(served)
    for leak in ("did_not_hold", "falsifier_fired", "self_score", "outcome"):
        assert leak not in blob, f"the served payload leaked {leak!r}"
    assert set(served["relay_handoff"]) == {
        "from_sequence_index", "from_label", "assessment", "expectations", "falsifiers"}


def test_point_zero_has_no_handoff_and_neither_does_a_solo_walk():
    store = _store()
    a, b = _doc(store), _doc(store)
    pts = _walk(store, 3)
    rotation = _relay(store, pts, [a, b])
    first = client.get(f"/api/asclepius/tasks/{pts[0]['task_id']}",
                       headers=A.headers_for(store.get_user_by_id(rotation[0]))).json()
    assert "relay_handoff" not in first

    solo = _walk(store, 2, tid="traj-solo")
    served = client.get(f"/api/asclepius/tasks/{solo[0]['task_id']}",
                        headers=A.headers_for(a)).json()
    assert "relay_handoff" not in served


def test_the_handoff_names_a_position_not_a_person():
    """Labelers are blinded to each other everywhere else; "the physician before
    you on this chart" is the whole clinically relevant fact."""
    store = _store()
    a, b = _doc(store), _doc(store)
    pts = _walk(store, 3)
    rotation = _relay(store, pts, [a, b])
    _submit(store, pts[0]["task_id"], rotation[0])
    served = client.get(f"/api/asclepius/tasks/{pts[1]['task_id']}",
                        headers=A.headers_for(store.get_user_by_id(rotation[1]))).json()
    ho = served["relay_handoff"]
    assert "decision 1" in ho["from_label"]
    prev_email = (store.get_user_by_id(rotation[0]) or {}).get("email")
    assert prev_email not in str(ho)


def test_a_hole_does_not_blank_the_handoff():
    """The predecessor is the nearest EARLIER point that was answered, not
    index-1, so a retired point does not silently strip the next physician's
    context."""
    store = _store()
    a, b = _doc(store), _doc(store)
    pts = _walk(store, 4)
    _relay(store, pts, [a, b])
    _submit(store, pts[0]["task_id"], a["id"], answer="the first read")
    store.mark_task_status(pts[1]["task_id"], "void")

    ho = store.relay_handoff(trajectory_id="traj-relay", sequence_index=2)
    assert ho and ho["from_sequence_index"] == 0 and "first read" in ho["assessment"]


# ═══════════════════════════════════════════════════════════════════════════════
# κ — the same outcome, a different and correctly-stated reason
# ═══════════════════════════════════════════════════════════════════════════════
def test_relay_points_are_excluded_for_the_single_label_reason_not_the_solo_one():
    """A methodologist must be able to tell "we judged this dependent" from "we
    only have one rater" — the second is fixed by buying a second walk."""
    solo = {"trajectory_id": "t", "sequence_index": 1}
    relay = {"trajectory_id": "t", "sequence_index": 1, "walk_mode": "relay"}
    assert TJ.kappa_exclusion_reason(solo) == TJ.KAPPA_EXCLUSION_SEQUENTIAL
    assert TJ.kappa_exclusion_reason(relay) == TJ.KAPPA_EXCLUSION_RELAY_SINGLE
    assert TJ.kappa_exclusion_reason({}) is None

    assert "sequential" in TJ.kappa_exclusion_rationale(solo).lower()
    r = TJ.kappa_exclusion_rationale(relay)
    assert "DIFFERENT physician" in r and "double-label floor" in r


def test_both_exclusions_are_reported_separately():
    from asclepius import agreement as AG
    out = AG.aggregate_kappa([
        {"kappa_excluded_reason": TJ.KAPPA_EXCLUSION_SEQUENTIAL},
        {"kappa_excluded_reason": TJ.KAPPA_EXCLUSION_RELAY_SINGLE},
        {"kappa_excluded_reason": TJ.KAPPA_EXCLUSION_RELAY_SINGLE},
    ])
    assert out["excluded_trajectory"] == 3
    assert out["excluded_trajectory_sequential"] == 1
    assert out["excluded_trajectory_relay_single"] == 2
    assert out["exclusion_rationale"] and out["exclusion_rationale_relay"]
    assert out["excluded_other"] == 0


# ═══════════════════════════════════════════════════════════════════════════════
# The send endpoint — the shapes it refuses
# ═══════════════════════════════════════════════════════════════════════════════
def test_a_dry_run_shows_the_mapping_and_writes_nothing():
    store = _store()
    ah = _admin(store)
    a, b = _doc(store), _doc(store)
    pts = _walk(store, 4)
    r = client.post("/api/asclepius/admin/batches/relay", headers=ah, json={
        "trajectory_id": "traj-relay", "user_ids": [a["id"], b["id"]],
        "dry_run": True, "seed": 3})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [m["sequence_index"] for m in body["mapping"]] == [0, 1, 2, 3]
    assert all(m["email"] for m in body["mapping"]), "the admin sees WHO, not ids"
    assert store.assignments_for_task(pts[0]["task_id"]) == []
    assert store.get_task(pts[0]["task_id"])["walk_mode"] is None


def test_the_committed_mapping_is_the_one_that_was_previewed():
    store = _store()
    ah = _admin(store)
    a, b = _doc(store), _doc(store)
    _walk(store, 5)
    payload = {"trajectory_id": "traj-relay", "user_ids": [a["id"], b["id"]], "seed": 11}
    shown = client.post("/api/asclepius/admin/batches/relay", headers=ah,
                        json={**payload, "dry_run": True}).json()["mapping"]
    done = client.post("/api/asclepius/admin/batches/relay", headers=ah,
                       json={**payload, "dry_run": False}).json()["mapping"]
    assert [m["user_id"] for m in shown] == [m["user_id"] for m in done], (
        "preview and commit must be one permutation, not two draws")


def test_more_doctors_than_points_is_refused():
    """Somebody would be told they are on a relay and never get a turn."""
    store = _store()
    ah = _admin(store)
    docs = [_doc(store) for _ in range(4)]
    _walk(store, 2)
    r = client.post("/api/asclepius/admin/batches/relay", headers=ah, json={
        "trajectory_id": "traj-relay", "user_ids": [d["id"] for d in docs],
        "dry_run": False})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "too_many_doctors"


def test_a_one_doctor_relay_is_refused_as_a_solo_walk_in_disguise():
    """Every handoff would be that physician reading their own note back, and the
    κ annex would claim independent raters that do not exist."""
    store = _store()
    ah, a = _admin(store), _doc(store)
    _walk(store, 4)
    r = client.post("/api/asclepius/admin/batches/relay", headers=ah, json={
        "trajectory_id": "traj-relay", "user_ids": [a["id"]], "dry_run": False})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "relay_needs_two_doctors"


def test_re_sending_a_sent_walk_is_a_409():
    """A second rotation over the first would silently take point 4 away from a
    doctor already told it was theirs."""
    store = _store()
    ah = _admin(store)
    a, b = _doc(store), _doc(store)
    _walk(store, 4)
    payload = {"trajectory_id": "traj-relay", "user_ids": [a["id"], b["id"]],
               "dry_run": False, "seed": 5}
    assert client.post("/api/asclepius/admin/batches/relay", headers=ah,
                       json=payload).status_code == 200
    again = client.post("/api/asclepius/admin/batches/relay", headers=ah, json=payload)
    assert again.status_code == 409
    assert again.json()["detail"]["error"] == "trajectory_already_sent"


def test_a_relay_send_stamps_the_mode_and_keeps_the_walk_sealed():
    store = _store()
    ah = _admin(store)
    a, b = _doc(store), _doc(store)
    pts = _walk(store, 4)
    r = client.post("/api/asclepius/admin/batches/relay", headers=ah, json={
        "trajectory_id": "traj-relay", "user_ids": [a["id"], b["id"]],
        "dry_run": False, "seed": 2})
    assert r.status_code == 200, r.text
    assert len(r.json()["committed"]) == 4
    for pt in pts:
        row = store.get_task(pt["task_id"])
        assert row["walk_mode"] == "relay"
        assert row["distribution"] == "assigned_only", (
            "a relay is the opposite of an open queue")
    assert r.json()["notified"]["dms"] == 2, "one DM per doctor, not per point"


def test_an_unknown_trajectory_is_a_404():
    store = _store()
    a, b = _doc(store), _doc(store)
    r = client.post("/api/asclepius/admin/batches/relay", headers=_admin(store), json={
        "trajectory_id": "traj-nope", "user_ids": [a["id"], b["id"]]})
    assert r.status_code == 404


def test_the_export_annex_says_which_product_produced_the_record():
    """§8.1 — solo and relay rows look identical, and they are priced and analysed
    differently. A buyer who cannot tell them apart has been sold one as the
    other."""
    from asclepius import packaging as PK

    solo = PK.trajectory_block({"trajectory_id": "t", "sequence_index": 2}, {})
    relay = PK.trajectory_block(
        {"trajectory_id": "t", "sequence_index": 2, "walk_mode": "relay"}, {})
    assert solo["walk_mode"] == "solo"
    assert relay["walk_mode"] == "relay"
    assert solo["kappa_exclusion"] == TJ.KAPPA_EXCLUSION_SEQUENTIAL
    assert relay["kappa_exclusion"] == TJ.KAPPA_EXCLUSION_RELAY_SINGLE
    # An ordinary case carries no walk_mode at all rather than a misleading 'solo'.
    ordinary = PK.trajectory_block({"task_id": "t1"},
                                   {"expected_trajectory": None}) or {}
    assert ordinary.get("walk_mode") is None
