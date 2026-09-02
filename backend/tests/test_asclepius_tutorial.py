"""Tutorial ("Calibration Case 1") — fixture validity, serving, grading, state
machine, and the isolation proofs.

The tutorial task is fully virtual: these tests PROVE a complete tutorial run
leaves nothing behind but ``events`` rows — no ``tasks`` row, no ``submissions``,
no ``records``, an unchanged real queue, and an unchanged ``public_user`` shape
except the single ``tutorial`` key.
"""

from __future__ import annotations

import tests._asclepius as A
from fastapi.testclient import TestClient

from asclepius import auth as asc_auth
from asclepius.cases import assert_multimodal_content
from asclepius.tutorial_case import (
    REFERENCE_FINDINGS,
    TUTORIAL_CASE_ENTRY,
    TUTORIAL_TASK_ID,
    grade_tutorial_submission,
    tutorial_raw_task,
)

client = TestClient(A.app)


# ─── Helpers ─────────────────────────────────────────────────────────────────
def _full_good_payload() -> dict:
    """A submission that should match the reference panel on all 4 findings."""
    return {
        "task_id": TUTORIAL_TASK_ID,
        "verdict": "B_better",
        "chosen_id": "B",
        "rejected_id": "A",
        "confidence": "high",
        "time_spent_sec": 240,
        "independent_answer": {"text": "Still congested with JVP twelve; the creatinine rise is permissive."},
        "chosen_revision": {
            "revised_text": "Intensify decongestion.",
            "why_better_tags": ["safer", "better_reasoning"],
            "why_better_notes": "Reads the volume exam (orthopnea, edema) rather than the creatinine trend.",
        },
        "rejected_critique": {
            "error_tags": ["unsafe_recommendation"],
            "severities": {"unsafe_recommendation": "high"},
            "why_worse": "A fluid bolus in a volume-overloaded patient re-congests them.",
        },
        "rubric": [
            {"text": "Recommends holding diuresis or giving IV fluids", "points": -9, "axis": "safety"},
            {"text": "Names the persistent congestion evidence", "points": 6, "axis": "reasoning"},
        ],
    }


def _seed_real_task(store):
    """One ordinary open task so /tasks/next has something real to serve."""
    store.insert_task(
        task_id=f"real-{A.uniq()}",
        prompt="A real queue task prompt for isolation checks.",
        specialty="nephrology",
        difficulty="hard",
        capture_reasoning=False,
        source="lab_supplied",
        candidate_answers=[{"id": "A", "text": "answer a"}, {"id": "B", "text": "answer b"}],
        created_by="test",
    )


def _table_count(store, table: str) -> int:
    with store._conn() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ─── 1. Fixture validity ─────────────────────────────────────────────────────
def test_fixture_clears_real_content_gate():
    assert_multimodal_content(TUTORIAL_CASE_ENTRY["case"])
    assert len(TUTORIAL_CASE_ENTRY["candidate_answers"]) == 2
    assert TUTORIAL_CASE_ENTRY["intended_flawed_id"] in ("A", "B")


def test_answer_key_shape():
    assert len(REFERENCE_FINDINGS) == 4
    assert sum(1 for f in REFERENCE_FINDINGS if f.get("planted")) == 1
    for f in REFERENCE_FINDINGS:
        assert f["grader"]["kind"] in (
            "chosen_id", "error_tag", "rubric_critical_negative", "keyword")
        assert f["reason_match"] and f["reason_miss"]


def test_raw_task_shape_matches_serving_contract():
    t = tutorial_raw_task()
    assert t["task_id"] == TUTORIAL_TASK_ID
    assert t["specialty"] == "nephrology"
    assert t["modality"] == "multimodal"
    assert t["source"] == "tutorial"
    assert t["capture_reasoning"] is True
    assert len(t["candidate_answers"]) == 2
    assert t["prompt"]  # rendered case prompt


# ─── 2. Task serving ─────────────────────────────────────────────────────────
def test_tutorial_task_requires_auth():
    assert client.get("/api/asclepius/tutorial/task").status_code in (401, 403)


def _no_answer_key(obj) -> bool:
    """Recursively assert the blinded payload leaks no answer-key material."""
    banned = {"ground_truth", "hard_hook", "reasoning_divergence", "intended_flawed_id"}
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if banned & set(cur.keys()):
                return False
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return True


def test_tutorial_task_blinded_like_real_task():
    store = A.fresh_store()
    user = A.make_user(store, practice_case=False)
    r = client.get("/api/asclepius/tutorial/task", headers=A.headers_for(user))
    assert r.status_code == 200
    task = r.json()["task"]
    assert task["task_id"] == TUTORIAL_TASK_ID
    assert task["answers_withheld"] is True
    for c in task["candidate_answers"]:
        assert "text" not in c
    assert _no_answer_key(task)
    # And the task is NOT in the store.
    assert store.get_task(TUTORIAL_TASK_ID) is None


# ─── 3. Reveal ───────────────────────────────────────────────────────────────
def test_tutorial_reveal_requires_instinct_text():
    store = A.fresh_store()
    user = A.make_user(store, practice_case=False)
    r = client.post("/api/asclepius/tutorial/reveal", json={"text": "  "},
                    headers=A.headers_for(user))
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "independent_answer_required"


def test_tutorial_reveal_returns_texts_writes_no_commit():
    store = A.fresh_store()
    user = A.make_user(store, practice_case=False)
    r = client.post("/api/asclepius/tutorial/reveal",
                    json={"text": "still congested; permissive rise"},
                    headers=A.headers_for(user))
    assert r.status_code == 200
    answers = r.json()["answers"]
    assert {a["id"] for a in answers} == {"A", "B"}
    assert all(a["text"] for a in answers)
    assert store.get_independent_commit(TUTORIAL_TASK_ID, user["id"]) is None
    assert _table_count(store, "independent_commits") == 0


# ─── 4. Grading ──────────────────────────────────────────────────────────────
def test_grading_full_match():
    r = grade_tutorial_submission(_full_good_payload())
    assert (r["matched"], r["total"]) == (4, 4)
    assert r["passed"] is True
    assert r["planted_finding"]["matched"] is True


def test_the_physician_is_never_shown_a_number_for_their_own_work():
    """The headline carries a verdict, never a score.

    A physician sees no contributor score anywhere in this product, and "4 of
    4" on the practice case is the same number wearing a different hat: it
    invites reading a grade off a four-item keyword rubric built to teach
    rather than to rank. ``matched``/``total`` stay in the payload for the
    admin and for events; they are not what the physician reads.
    """
    for payload in (_full_good_payload(), {"chosen_id": "A"}, {}):
        headline = grade_tutorial_submission(payload)["headline"]
        assert not any(ch.isdigit() for ch in headline), headline


def test_grading_each_single_miss():
    misses = {
        "sound-answer": {"chosen_id": "A"},
        "trap-error-tagged": {"rejected_critique": {
            "error_tags": ["minor_wording"], "severities": {"minor_wording": "low"},
            "why_worse": "fluid bolus while congested (JVP up)"}},
        "critical-negative": {"rubric": [
            {"text": "Names the persistent congestion evidence", "points": 6}]},
        "congestion-evidence": {
            "independent_answer": {"text": "give the stronger treatment"},
            "chosen_revision": {"revised_text": "Intensify treatment.",
                                "why_better_tags": ["safer"],
                                "why_better_notes": "the safer plan overall"},
            "rejected_critique": {"error_tags": ["unsafe_recommendation"],
                                  "severities": {"unsafe_recommendation": "high"},
                                  "why_worse": "the wrong move for this patient"}},
    }
    for finding_id, override in misses.items():
        payload = _full_good_payload()
        payload.update(override)
        r = grade_tutorial_submission(payload)
        by_id = {f["id"]: f for f in r["findings"]}
        assert by_id[finding_id]["matched"] is False, finding_id
        assert r["matched"] == 3, finding_id
        assert by_id[finding_id]["reason"]  # a miss always explains itself


def test_submit_rejects_non_tutorial_task_id():
    store = A.fresh_store()
    user = A.make_user(store, practice_case=False)
    payload = _full_good_payload()
    payload["task_id"] = "some-real-task"
    r = client.post("/api/asclepius/tutorial/submit", json=payload,
                    headers=A.headers_for(user))
    assert r.status_code == 400


# ─── 5. State machine ────────────────────────────────────────────────────────
def _patch(user, action, **kw):
    return client.patch("/api/asclepius/me/tutorial",
                        json={"action": action, **kw}, headers=A.headers_for(user))


def test_state_machine_lifecycle():
    store = A.fresh_store()
    user = A.make_user(store, practice_case=False)

    me = client.get("/api/asclepius/auth/me", headers=A.headers_for(user)).json()
    assert me["tutorial"]["status"] == "not_started"

    r = _patch(user, "start")
    assert r.json()["tutorial"]["status"] == "in_progress"

    # `step` is stall analytics and is deliberately NOT in the session payload
    # any more (auth._parse_tutorial projects it out along with the score), so
    # read it where it actually lives.
    _patch(user, "advance", step="ch2-instinct")
    assert store.get_tutorial_state(user["id"])["step"] == "ch2-instinct"

    r = _patch(user, "complete")
    t = r.json()["tutorial"]
    assert t["status"] == "completed" and t["completed_at"]

    # start after completed is a no-op — replay must never clear completion.
    r = _patch(user, "start")
    assert r.json()["tutorial"]["status"] == "completed"
    # advance after completed is also a no-op.
    r = _patch(user, "advance", step="ch1-welcome")
    assert r.json()["tutorial"]["status"] == "completed"

    r = _patch(user, "reset")
    assert r.json()["tutorial"]["status"] == "not_started"


def test_skip_grants_nothing_and_does_not_strand_the_physician():
    """Skip is retired, and the retirement has to be a no-op rather than a
    silent success.

    The practice case now gates all real work, so a skip that still wrote
    ``status='skipped'`` would be a control that lies: it granted nothing, and
    because `skipped` used to be terminal and suppress relaunch, honouring it
    would leave the physician on a dashboard where every button 403s and no
    route back to the one thing that unlocks them.
    """
    store = A.fresh_store()
    user = A.make_user(store, practice_case=False)
    r = _patch(user, "skip")
    assert r.status_code == 200                      # a cached client gets a no-op, not a 422
    assert r.json()["tutorial"]["status"] == "not_started"
    assert store.get_tutorial_state(user["id"]).get("skipped_at") is None

    # And a LEGACY skipped row can still start the tour: `skipped` is no longer
    # terminal, or those accounts could never open the gate.
    store.set_tutorial_state(user["id"], {"status": "skipped", "version": 1})
    assert _patch(user, "start").json()["tutorial"]["status"] == "in_progress"


def test_submit_stamps_completed_and_keeps_the_score_off_the_wire():
    store = A.fresh_store()
    user = A.make_user(store, practice_case=False)
    r = client.post("/api/asclepius/tutorial/submit", json=_full_good_payload(),
                    headers=A.headers_for(user))
    assert r.status_code == 200
    body = r.json()
    assert body["result"]["matched"] == 4
    assert body["result"]["passed"] is True
    t = body["user"]["tutorial"]
    assert t["status"] == "completed"
    assert t["gate_state"] == "passed"
    # The score is stored for the admin and for events, and never shipped to
    # the physician: they see no grade for their own work anywhere in this
    # product, and the practice case is not an exception.
    assert "score" not in t
    assert store.get_tutorial_state(user["id"])["score"] == {"matched": 4, "total": 4}
    # A replay submit re-grades but does not restamp completed_at.
    first_completed = t["completed_at"]
    r2 = client.post("/api/asclepius/tutorial/submit", json=_full_good_payload(),
                     headers=A.headers_for(user))
    assert r2.json()["user"]["tutorial"]["completed_at"] == first_completed


# ─── 6. Isolation proofs ─────────────────────────────────────────────────────
def test_full_tutorial_run_leaves_no_data_and_queue_unchanged():
    store = A.fresh_store()
    # Gate open, because this test reads the REAL queue either side of a
    # practice run. Then wound back to not_started so the run is a real one.
    user = A.make_user(store)
    store.set_tutorial_state(user["id"], {
        "status": "not_started", "version": None,
        "gate": store.get_tutorial_state(user["id"]).get("gate")})
    _seed_real_task(store)
    hdrs = A.headers_for(user)

    next_before = client.get("/api/asclepius/tasks/next", headers=hdrs).json()

    # Full tutorial run: task -> reveal -> submit.
    assert client.get("/api/asclepius/tutorial/task", headers=hdrs).status_code == 200
    assert client.post("/api/asclepius/tutorial/reveal",
                       json={"text": "permissive rise, still congested"},
                       headers=hdrs).status_code == 200
    assert client.post("/api/asclepius/tutorial/submit", json=_full_good_payload(),
                       headers=hdrs).status_code == 200

    # No rows anywhere but events; the tutorial task never existed in the store.
    assert store.get_task(TUTORIAL_TASK_ID) is None
    assert _table_count(store, "submissions") == 0
    assert _table_count(store, "records") == 0
    assert _table_count(store, "independent_commits") == 0

    # The real queue serves the SAME task as before the run.
    next_after = client.get("/api/asclepius/tasks/next", headers=hdrs).json()
    assert next_after["task"]["task_id"] == next_before["task"]["task_id"]

    # Submitting to the REAL pipeline with the tutorial task id 404s.
    payload = _full_good_payload()
    r = client.post("/api/asclepius/submissions", json=payload, headers=hdrs)
    assert r.status_code == 404


def test_stats_unchanged_by_tutorial_run():
    store = A.fresh_store()
    admin = A.make_user(store, role="admin")
    user = A.make_user(store, practice_case=False)
    hdrs = A.headers_for(user)

    before = client.get("/api/asclepius/stats", headers=A.headers_for(admin)).json()
    client.get("/api/asclepius/tutorial/task", headers=hdrs)
    client.post("/api/asclepius/tutorial/reveal",
                json={"text": "congested, permissive"}, headers=hdrs)
    client.post("/api/asclepius/tutorial/submit", json=_full_good_payload(), headers=hdrs)
    after = client.get("/api/asclepius/stats", headers=A.headers_for(admin)).json()
    assert before == after


# ─── 7. public_user shape guard ──────────────────────────────────────────────
def test_public_user_diff_is_exactly_the_tutorial_key():
    # The exact baseline this repo shipped when the tutorial branch diverged
    # (pre-Advisor-tier). "tier"/"tier_word"/"capabilities" are a separate,
    # later addition (Advisor PRD) merged in from main — asserted present but
    # not exhaustively, so this test stays about the tutorial key, not theirs.
    store = A.fresh_store()
    user = A.make_user(store, practice_case=False)
    pub = asc_auth.public_user(store.get_user_by_id(user["id"]))
    baseline = {
        "id", "email", "role", "specialty", "board_cert", "years_experience",
        "real_data_approved", "tutorial",
    }
    assert baseline <= set(pub.keys())
    assert {"tier", "tier_word", "capabilities"} <= set(pub.keys())
    assert pub["tutorial"] == {"status": "not_started", "version": None,
                               "gate_state": "locked", "attempts": 0}
