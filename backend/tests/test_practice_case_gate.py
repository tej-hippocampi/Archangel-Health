"""The practice case gates real work, and the gate holds.

Before this, ``store.get_tutorial_state`` was read in exactly two places, both
inside the tutorial's own endpoints. /tasks/next, /tasks/available and
/submissions never consulted it, ``skip`` wrote a field nothing read, and
grading was lenient enough that a 0-of-4 stamped ``completed``. So the practice
case was a suggestion with a progress bar.

The tests that matter most here are the ones about what the gate must NOT do:
lock out physicians who are already working, hand a revoked gate back on the
next redeploy, or let fourteen clicks of "Skip this step" through.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import tests._asclepius as A
from asclepius import capabilities as asc_caps
from asclepius.tutorial_case import (
    GRADED_STEP_IDS,
    PASS_MIN_MATCHED,
    TUTORIAL_TASK_ID,
    TUTORIAL_VERSION,
)

client = TestClient(A.app)

#: Every endpoint that can reach a real case.
GATED = [
    ("get", "/api/asclepius/tasks/next?specialty=nephrology"),
    ("get", "/api/asclepius/tasks/available"),
    ("get", "/api/asclepius/tasks/t-does-not-matter"),
    ("post", "/api/asclepius/submissions"),
    ("post", "/api/asclepius/tasks/t-does-not-matter/reveal"),
    ("post", "/api/asclepius/rubric/suggest"),
    ("post", "/api/asclepius/assist/prelabel"),
]


def _call(method: str, url: str, headers: dict):
    if method == "get":
        return client.get(url, headers=headers)
    return client.post(url, json={"task_id": TUTORIAL_TASK_ID, "text": "x"}, headers=headers)


def _good_payload() -> dict:
    return {
        "task_id": TUTORIAL_TASK_ID,
        "verdict": "B_better", "chosen_id": "B", "rejected_id": "A",
        "confidence": "high", "time_spent_sec": 240,
        "independent_answer": {"text": "Still congested with JVP twelve; permissive rise."},
        "chosen_revision": {
            "revised_text": "Intensify decongestion.",
            "why_better_tags": ["safer"],
            "why_better_notes": "Reads the volume exam, not the creatinine trend.",
        },
        "rejected_critique": {
            "error_tags": ["unsafe_recommendation"],
            "severities": {"unsafe_recommendation": "high"},
            "why_worse": "A fluid bolus in a volume-overloaded patient re-congests them.",
        },
        "rubric": [
            {"text": "Recommends holding diuresis or giving IV fluids", "points": -9,
             "axis": "safety"},
        ],
    }


def _gate(store, user_id) -> dict:
    return (store.get_tutorial_state(user_id) or {}).get("gate") or {}


# ─── The gate ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("method,url", GATED)
def test_a_tiered_physician_cannot_reach_real_work_before_passing(method, url):
    store = A.fresh_store()
    user = A.make_user(store, practice_case=False)
    r = _call(method, url, A.headers_for(user))
    assert r.status_code == 403, (url, r.text)
    body = r.json()["detail"]
    assert body["error"] == "practice_case_required"
    assert body["reason"] == "not_started"
    # The client picks a screen from the header, never from the prose.
    assert r.headers.get("X-Asclepius-Practice-Gate") == "not_started"
    # And it is told what to do rather than only what it cannot do.
    assert body["action"]["kind"] == "start_practice_case"


def test_passing_opens_every_gated_endpoint():
    store = A.fresh_store()
    user = A.make_user(store, practice_case=False)
    r = client.post("/api/asclepius/tutorial/submit", json=_good_payload(),
                    headers=A.headers_for(user))
    assert r.status_code == 200 and r.json()["result"]["passed"] is True
    for method, url in GATED:
        assert _call(method, url, A.headers_for(user)).status_code != 403, url


def test_failing_does_not_open_it_and_does_not_claim_completion():
    store = A.fresh_store()
    user = A.make_user(store, practice_case=False)
    bad = _good_payload()
    bad["chosen_id"] = "A"          # the answer the reference panel rejected
    r = client.post("/api/asclepius/tutorial/submit", json=bad,
                    headers=A.headers_for(user))
    assert r.status_code == 200
    assert r.json()["result"]["passed"] is False
    state = store.get_tutorial_state(user["id"])
    assert state["status"] == "in_progress", "a failed attempt must not read as completed"
    assert state.get("completed_at") is None
    assert _gate(store, user["id"])["attempts"] == 1
    assert client.get("/api/asclepius/tasks/next?specialty=nephrology",
                      headers=A.headers_for(user)).status_code == 403


def test_retries_are_unlimited_and_the_gate_opens_on_the_pass():
    store = A.fresh_store()
    user = A.make_user(store, practice_case=False)
    hdrs = A.headers_for(user)
    bad = _good_payload(); bad["chosen_id"] = "A"
    for _ in range(3):
        assert client.post("/api/asclepius/tutorial/submit", json=bad,
                           headers=hdrs).status_code == 200
    assert _gate(store, user["id"])["attempts"] == 3
    assert asc_caps.practice_gate_state(store.get_user_by_id(user["id"])) == "locked"

    assert client.post("/api/asclepius/tutorial/submit", json=_good_payload(),
                       headers=hdrs).json()["result"]["passed"] is True
    assert asc_caps.practice_gate_state(store.get_user_by_id(user["id"])) == "passed"


def test_skipping_grants_nothing():
    store = A.fresh_store()
    user = A.make_user(store, practice_case=False)
    hdrs = A.headers_for(user)
    assert client.patch("/api/asclepius/me/tutorial", json={"action": "skip"},
                        headers=hdrs).status_code == 200
    assert client.get("/api/asclepius/tasks/next?specialty=nephrology",
                      headers=hdrs).status_code == 403


@pytest.mark.parametrize("action", ["start", "reset"])
def test_a_physician_cannot_clear_their_own_gate(action):
    """``reset`` is self-service, and start/advance/reset each rebuilt the state
    dict from scratch. A grandfathered physician clicking any of them would have
    dropped their own gate and locked themselves out of their own queue."""
    store = A.fresh_store()
    user = A.make_user(store, practice_case=False)
    store.set_tutorial_state(user["id"], {
        "status": "not_started", "version": None,
        "gate": {"state": "grandfathered", "source": "migration:practice_gate_backfill"}})
    client.patch("/api/asclepius/me/tutorial", json={"action": action},
                 headers=A.headers_for(user))
    assert _gate(store, user["id"])["state"] == "grandfathered"


def test_admins_and_the_demo_account_are_exempt():
    store = A.fresh_store()
    admin = A.make_user(store, role="admin", practice_case=False)
    assert asc_caps.practice_gate_reason(
        store.get_user_by_id(admin["id"]), required_version=TUTORIAL_VERSION) is None
    assert asc_caps.practice_gate_reason(
        {"role": "evaluator", "is_mock": 1}, required_version=TUTORIAL_VERSION) is None


# ─── The autofill hole ───────────────────────────────────────────────────────
def test_skipping_every_step_is_not_a_pass():
    """"Skip this step" fills a clinically reasonable placeholder through the
    app's own handlers, and clinically reasonable is exactly what the answer key
    checks. Fourteen clicks matched all four findings, which under a hard gate
    would have unlocked every paid case."""
    store = A.fresh_store()
    user = A.make_user(store, practice_case=False)
    payload = _good_payload()                       # a full 4-of-4 payload...
    payload["assisted"] = sorted(GRADED_STEP_IDS)   # ...that the client admits it filled
    r = client.post("/api/asclepius/tutorial/submit", json=payload,
                    headers=A.headers_for(user))
    result = r.json()["result"]
    assert result["matched"] == 4
    assert result["passed"] is False, "autofilled graded steps must not pass"
    assert client.get("/api/asclepius/tasks/next?specialty=nephrology",
                      headers=A.headers_for(user)).status_code == 403


def test_skipping_an_ungraded_step_is_still_a_pass():
    """The affordance stays usable: only the steps the key reads disqualify."""
    store = A.fresh_store()
    user = A.make_user(store, practice_case=False)
    payload = _good_payload()
    payload["assisted"] = ["ch1-tabs", "ch3-read"]   # pure reading beats
    r = client.post("/api/asclepius/tutorial/submit", json=payload,
                    headers=A.headers_for(user))
    assert r.json()["result"]["passed"] is True


# ─── Grandfathering ──────────────────────────────────────────────────────────
def _seed_worker(store, *, tutorial_json):
    """A physician with one real submission, in some prior tutorial state."""
    user = A.make_user(store, practice_case=False)
    with store._conn() as conn:
        conn.execute("UPDATE users SET tutorial_json = ? WHERE id = ?",
                     (tutorial_json, user["id"]))
        conn.execute(
            "INSERT INTO submissions (submission_id, task_id, evaluator_id, verdict, "
            "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("s-" + A.uniq(8), "t-" + A.uniq(6), user["id"], "A_better",
             "export_ready", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
    return user


@pytest.mark.parametrize("prior", [
    None,
    json.dumps({"status": "skipped", "skipped_at": "2026-01-01T00:00:00Z"}),
    json.dumps({"status": "not_started", "version": None}),
    json.dumps({"status": "completed", "score": {"matched": 0, "total": 4}}),
])
def test_a_physician_already_doing_real_work_is_never_locked_out(prior):
    """The gate exists to show somebody the standard BEFORE their first real
    case. Someone already submitting has had that case; the gate cannot un-happen
    it, and locking them out is a supply outage dressed as a quality fix."""
    store = A.fresh_store()
    user = _seed_worker(store, tutorial_json=prior)
    with store._conn() as conn:
        assert store._backfill_practice_gate(conn) == 1
    row = store.get_user_by_id(user["id"])
    assert asc_caps.practice_gate_reason(row, required_version=TUTORIAL_VERSION) is None
    assert client.get("/api/asclepius/tasks/available",
                      headers=A.headers_for(user)).status_code != 403


def test_the_backfill_leaves_the_record_of_what_they_did_alone():
    """`status` is reporting truth and `gate` is the access answer. A skipped row
    stays skipped, because that is what the physician did."""
    store = A.fresh_store()
    user = _seed_worker(store, tutorial_json=json.dumps({"status": "skipped"}))
    with store._conn() as conn:
        store._backfill_practice_gate(conn)
    state = store.get_tutorial_state(user["id"])
    assert state["status"] == "skipped"
    assert state["gate"]["state"] == "grandfathered"
    assert state["gate"]["source"] == "migration:practice_gate_backfill"


def test_the_backfill_is_idempotent_across_reboots():
    store = A.fresh_store()
    _seed_worker(store, tutorial_json=None)
    with store._conn() as conn:
        assert store._backfill_practice_gate(conn) == 1
    with store._conn() as conn:
        assert store._backfill_practice_gate(conn) == 0


def test_a_deliberately_locked_gate_is_not_handed_back_on_the_next_deploy():
    """THE anti-regrant test. The migration runs on every boot, so it keys on the
    gate being ABSENT rather than on its state. Without that, an admin who
    revoked somebody would find them re-granted by the next redeploy, which is
    worse than the gap the migration closes."""
    store = A.fresh_store()
    user = _seed_worker(store, tutorial_json=json.dumps(
        {"status": "skipped", "gate": {"state": "locked", "source": "admin:revoked"}}))
    with store._conn() as conn:
        assert store._backfill_practice_gate(conn) == 0
    assert _gate(store, user["id"])["state"] == "locked"


def test_a_physician_with_no_real_work_is_not_grandfathered():
    store = A.fresh_store()
    user = A.make_user(store, practice_case=False)
    with store._conn() as conn:
        store._backfill_practice_gate(conn)
    assert _gate(store, user["id"]) == {}


def test_a_grandfathered_physician_who_replays_and_fails_keeps_their_access():
    """Replaying the practice case for practice must not cost somebody their
    queue because they had a bad afternoon."""
    store = A.fresh_store()
    user = _seed_worker(store, tutorial_json=None)
    with store._conn() as conn:
        store._backfill_practice_gate(conn)
    bad = _good_payload(); bad["chosen_id"] = "A"
    client.post("/api/asclepius/tutorial/submit", json=bad, headers=A.headers_for(user))
    assert _gate(store, user["id"])["state"] == "grandfathered"
    assert client.get("/api/asclepius/tasks/available",
                      headers=A.headers_for(user)).status_code != 403


# ─── Versioning ──────────────────────────────────────────────────────────────
def test_a_pass_under_an_older_answer_key_is_re_gated():
    store = A.fresh_store()
    user = A.make_user(store, practice_case=False)
    store.set_tutorial_state(user["id"], {
        "status": "completed",
        "gate": {"state": "passed", "passed_version": 0, "attempts": 1}})
    row = store.get_user_by_id(user["id"])
    assert asc_caps.practice_gate_reason(row, required_version=1) == "stale_version"


def test_grandfathered_rows_are_exempt_from_the_version_check():
    """They were never asked, so re-gating them on a version bump would undo the
    migration that let them keep working."""
    assert asc_caps.practice_gate_reason(
        {"role": "evaluator", "tutorial_json": {"gate": {"state": "grandfathered"}}},
        required_version=99) is None


# ─── The score stays internal ────────────────────────────────────────────────
def test_the_session_payload_carries_no_grade_for_the_physicians_own_work():
    store = A.fresh_store()
    user = A.make_user(store, practice_case=False)
    hdrs = A.headers_for(user)
    client.post("/api/asclepius/tutorial/submit", json=_good_payload(), headers=hdrs)
    tut = client.get("/api/asclepius/auth/me", headers=hdrs).json()["tutorial"]
    assert "score" not in tut
    assert "step" not in tut
    assert tut["gate_state"] == "passed"
    # Still recorded, for the admin and for events.
    assert store.get_tutorial_state(user["id"])["score"] == {"matched": 4, "total": 4}


def test_the_teaching_material_opens_only_after_a_submission():
    """`hard_hook`, `key_data` and the reference answer are stripped from the
    task by _blind_task and have never been visible. The split stays deliberate:
    the task is still blind, and the submit response is what opens them."""
    store = A.fresh_store()
    user = A.make_user(store, practice_case=False)
    hdrs = A.headers_for(user)
    task = client.get("/api/asclepius/tutorial/task", headers=hdrs).json()["task"]
    assert "ground_truth" not in json.dumps(task)
    assert "hard_hook" not in json.dumps(task)

    teaching = client.post("/api/asclepius/tutorial/submit", json=_good_payload(),
                           headers=hdrs).json()["result"]["teaching"]
    assert teaching["key_data"], "the reveal has nothing to teach with"
    assert teaching["reference_answer"]


def test_every_miss_is_something_the_physician_has_to_open():
    store = A.fresh_store()
    user = A.make_user(store, practice_case=False)
    payload = _good_payload()
    payload["independent_answer"] = {"text": "Increase the loop dose."}
    payload["chosen_revision"]["why_better_notes"] = "It is the better answer."
    payload["chosen_revision"]["revised_text"] = "Increase the loop dose and watch weights."
    payload["rejected_critique"]["why_worse"] = "The recommendation is unsafe."
    result = client.post("/api/asclepius/tutorial/submit", json=payload,
                         headers=A.headers_for(user)).json()["result"]
    assert result["passed"] is True, "3 of 4 with the right answer is a pass"
    assert result["must_acknowledge"] == ["congestion-evidence"]
    planted = result["planted_finding"]
    assert planted["matched"] is False
    # And they are shown what they actually wrote, not just what they missed.
    assert "Increase the loop dose" in planted["your_answer"]


def test_the_pass_mark_needs_the_right_answer_and_enough_of_it():
    store = A.fresh_store()
    user = A.make_user(store, practice_case=False)
    thin = {"task_id": TUTORIAL_TASK_ID, "chosen_id": "B",
            "independent_answer": {"text": "Still congested, so keep going."}}
    result = client.post("/api/asclepius/tutorial/submit", json=thin,
                         headers=A.headers_for(user)).json()["result"]
    assert result["matched"] < PASS_MIN_MATCHED
    assert result["passed"] is False
