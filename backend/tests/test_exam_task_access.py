"""The examination has to be finishable by the people it is for.

Onboarding Master PRD, Phase 0 (PRD A §2).

The bug this file pins shut: the examination entered through a provisional-safe
door (``require_surface(TUTORIAL)`` on ``/exam/task``) and was then sat in the
ordinary workspace, whose every call is gated on ``require_practice_case`` ->
``require_label`` -> full access. So the case loaded and then nothing in it
worked — "Reveal AI answers" answered *"We are still verifying your
credentials"* to an applicant whose examination is the thing being verified.
Everyone who needed the exam was refused it; only people who did not need it
could finish one.

The fix is a carve-out, and the whole value of a carve-out is its edges. So the
tests that matter here are the refusals, not the 200s:

  * their own examination task opens;
  * ANY other task id — a real V4 case, a synthetic one, another applicant's
    examination — is still 403 ``pending``, and ``_BY_ACCESS[PROVISIONAL]`` is
    still four surfaces wide;
  * a fully-approved physician meets the identical gates in the identical order
    (the carve-out must be invisible to them);
  * the examination PERSISTS — it writes ``independent_commits`` like a real
    task, which ``/tutorial/reveal`` deliberately does not, because the
    committed independent answer is what an admin reads before approving.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests._asclepius import app, fresh_store, headers_for, make_user

from asclepius import capabilities as asc_caps
from asclepius import exam_case
from asclepius.tutorial_case import TUTORIAL_PASS_MIN_VERSION


@pytest.fixture()
def client():
    # fresh_store() BEFORE the app boots, not after.
    #
    # Entering TestClient runs the startup hooks, and two of them WRITE: it seeds
    # the three v4 real de-identified cases, and `sync_real_data_approval` sweeps
    # the users table. Booting first and calling fresh_store() inside the test
    # body — which is what this fixture used to do — pointed both of those at
    # whichever store the PREVIOUS test happened to leave bound, so this file
    # wrote real_deid tasks and approval changes into a neighbour's database.
    #
    # Harmless in isolation, which is why it survived review; it is not harmless
    # in a shard, and it is not this file's business either way.
    fresh_store()
    with TestClient(app) as c:
        yield c


def _applicant(store, specialty="nephrology"):
    """A signed-up physician whose credentials are still being read.

    ``tier=None`` and ``practice_case=False`` on purpose: this is what an
    applicant actually looks like, and inheriting an approved account's tier
    would make every assertion below vacuous.
    """
    user = make_user(store, role="evaluator", specialty=specialty,
                     tier=None, practice_case=False)
    store.set_verification_status(user["id"], "pending")
    return store.get_user_by_id(user["id"])


def _synthetic_task(store) -> str:
    """An ordinary queue case: not anybody's examination."""
    return store.insert_task(prompt="An ordinary queue case.",
                             specialty="nephrology")["task_id"]


def _real_v4_task(store) -> str:
    """A real de-identified chart — what the V4 wall exists for."""
    return store.insert_task(
        prompt="A real de-identified chart.", specialty="nephrology",
        case={"case_source": "real_deid", "notes": [{"text": "n"}]},
    )["task_id"]


def _draw_exam(client, user):
    res = client.get("/api/asclepius/exam/task", headers=headers_for(user))
    assert res.status_code == 200, res.text
    return res.json()["task"]["task_id"]


def _reveal(client, user, task_id, text="Independent read of this case, written blind."):
    return client.post(
        f"/api/asclepius/tasks/{task_id}/reveal",
        headers=headers_for(user), json={"text": text},
    )


# ── The applicant is provisional, and stays provisional ──────────────────────

def test_the_applicant_taking_the_exam_is_a_provisional_account():
    """If this fixture ever became FULL the rest of the file would prove nothing."""
    store = fresh_store()
    user = _applicant(store)
    assert asc_caps.access_level(user) == asc_caps.PROVISIONAL


def test_the_provisional_surface_table_is_not_widened():
    """The carve-out is task-scoped. The alternative fix — giving provisional
    accounts the LABEL surface — would let an unverified account draw real
    de-identified patient cases from the queue, which is the exact thing the
    provisional tier exists to prevent."""
    assert asc_caps._BY_ACCESS[asc_caps.PROVISIONAL] == frozenset(
        {asc_caps.TUTORIAL, asc_caps.BROWSE, asc_caps.REFERRAL, asc_caps.EARNINGS}
    )
    assert asc_caps.REAL_WORK not in asc_caps._BY_ACCESS[asc_caps.PROVISIONAL]
    assert asc_caps.LABEL not in asc_caps._BY_ACCESS[asc_caps.PROVISIONAL]


# ── Their own examination opens ──────────────────────────────────────────────

def test_the_draw_stamps_which_case_this_applicant_was_given(client):
    """The stamp is the whole identity check. Without it the carve-out would
    have to trust a task id from the client, which is not a carve-out."""
    store = fresh_store()
    user = _applicant(store)
    task_id = _draw_exam(client, user)
    exam = store.get_tutorial_state(user["id"])["exam"]
    assert exam["task_id"] == task_id
    assert exam["state"] == "in_progress"


def test_a_provisional_applicant_can_open_their_own_exam_task(client):
    store = fresh_store()
    user = _applicant(store)
    task_id = _draw_exam(client, user)
    res = client.get(f"/api/asclepius/tasks/{task_id}", headers=headers_for(user))
    assert res.status_code == 200, res.text
    assert res.json()["task"]["task_id"] == task_id


def test_a_provisional_applicant_can_reveal_on_their_own_exam_task(client):
    """The screenshot bug, directly: this call used to be 403 for every single
    person who was ever asked to make it."""
    store = fresh_store()
    user = _applicant(store)
    task_id = _draw_exam(client, user)
    res = _reveal(client, user, task_id)
    assert res.status_code == 200, res.text


def test_the_examination_persists_unlike_the_practice_case(client):
    """``/tutorial/reveal`` writes no ``independent_commits`` row by design —
    "the practice case leaves no data behind". The examination must do the
    opposite: the committed independent answer is what an admin reads before
    approving, so routing the exam through the tutorial door would have thrown
    away the only artefact the decision rests on."""
    store = fresh_store()
    user = _applicant(store)
    task_id = _draw_exam(client, user)
    assert _reveal(client, user, task_id, "Blind answer, committed before reveal.").status_code == 200

    committed = store.get_independent_commit(task_id, user["id"])
    assert committed, "the examination must commit an independent answer"
    assert committed["evaluator_id"] == user["id"]
    assert "Blind answer" in (committed["payload"].get("text") or "")


def test_reveal_still_refuses_an_empty_independent_answer(client):
    """The anti-peeking rule is not part of the carve-out and does not move."""
    store = fresh_store()
    user = _applicant(store)
    task_id = _draw_exam(client, user)
    res = _reveal(client, user, task_id, "   ")
    assert res.status_code == 400
    assert res.json()["detail"]["error"] == "independent_answer_required"


def test_a_provisional_applicant_can_prelabel_on_their_own_exam_task(client):
    """``/assist/prelabel`` resolves its gate from the request body rather than
    a path parameter, so it is a separate branch and gets a separate test."""
    store = fresh_store()
    user = _applicant(store)
    task_id = _draw_exam(client, user)
    assert _reveal(client, user, task_id).status_code == 200
    res = client.post("/api/asclepius/assist/prelabel",
                      headers=headers_for(user), json={"task_id": task_id})
    # 200 with a suggestion or a `skipped` degrade — either is the gate opening.
    # What must NOT happen is 403.
    assert res.status_code == 200, res.text


def test_the_exam_is_exempt_from_the_practice_case_gate(client):
    """Pre-approval the practice case is optional (PRD A §1). Requiring it
    before the examination would rebuild the blocker one gate lower down."""
    store = fresh_store()
    user = _applicant(store)
    assert asc_caps.practice_gate_reason(
        user, required_version=TUTORIAL_PASS_MIN_VERSION) is not None, "gate must be shut"
    task_id = _draw_exam(client, user)
    assert client.get(f"/api/asclepius/tasks/{task_id}",
                      headers=headers_for(user)).status_code == 200


def test_the_applicant_keeps_their_task_after_submitting(client):
    """The submit path rebuilds the exam blob from scratch. If it dropped the
    stamp it would close the applicant's own case to them at the exact moment
    they filed it — and the workspace re-reads the task after a submit."""
    store = fresh_store()
    user = _applicant(store)
    task_id = _draw_exam(client, user)
    res = client.post("/api/asclepius/exam/submit", headers=headers_for(user),
                      json={"task_id": task_id, "time_spent_sec": 900})
    assert res.status_code == 200, res.text
    exam = store.get_tutorial_state(user["id"])["exam"]
    assert exam["state"] == "submitted"
    assert exam["task_id"] == task_id
    assert client.get(f"/api/asclepius/tasks/{task_id}",
                      headers=headers_for(user)).status_code == 200


# ── The submit path cannot be used to claim a task ───────────────────────────

def test_submitting_someone_elses_task_id_grants_nothing(client):
    """THE HOLE THIS CLOSES, and it was mine.

    ``/exam/submit`` took ``task_id`` from the request body and stamped it into
    ``tutorial.exam``, which is the ONLY input to the carve-out's identity
    check. So one POST — with no draw, on a fresh account — rewrote the stamp to
    any task the caller could name, and the next ``GET /tasks/{id}`` returned
    it. The draw's own comment said the stamp is "written server-side, never
    accepted from a client"; the submit path did exactly that.

    A whole queue of synthetic cases was readable this way, and reveal wrote an
    ``independent_commits`` row under an unverified account. Each submit
    rewrote the stamp, so it walked.
    """
    store = fresh_store()
    user = _applicant(store)
    target = _synthetic_task(store)

    res = client.post("/api/asclepius/exam/submit", headers=headers_for(user),
                      json={"task_id": target, "time_spent_sec": 60})
    assert res.status_code in (400, 403), res.text

    # And nothing was opened by trying.
    assert client.get(f"/api/asclepius/tasks/{target}",
                      headers=headers_for(user)).status_code == 403
    assert _reveal(client, user, target).status_code == 403
    assert exam_case.is_users_exam_task(store, user, target) is False


def test_submitting_a_foreign_task_id_after_drawing_does_not_move_the_stamp(client):
    """The walking variant: draw honestly, then submit a different id. The stamp
    must still name the case they were actually served."""
    store = fresh_store()
    user = _applicant(store)
    mine = _draw_exam(client, user)
    target = _synthetic_task(store)

    res = client.post("/api/asclepius/exam/submit", headers=headers_for(user),
                      json={"task_id": target, "time_spent_sec": 60})
    assert res.status_code in (400, 403), res.text

    exam = store.get_tutorial_state(user["id"])["exam"]
    assert exam["task_id"] == mine, "a client-named task id reached the stamp"
    assert client.get(f"/api/asclepius/tasks/{target}",
                      headers=headers_for(user)).status_code == 403


def test_a_forged_submit_files_no_examination(client):
    """It must not reach the record either. A `credentialing_exams` row naming
    an arbitrary task is read by the admin dossier, which loads that task and
    grades against its held-out answer key."""
    store = fresh_store()
    user = _applicant(store)
    target = _real_v4_task(store)
    client.post("/api/asclepius/exam/submit", headers=headers_for(user),
                json={"task_id": target, "time_spent_sec": 60})
    filed = store.list_credentialing_exams(user_id=user["id"]) \
        if hasattr(store, "list_credentialing_exams") else []
    assert not [e for e in filed if e.get("task_id") == target], \
        "a forged submit filed an examination against a task nobody served"


def test_an_honest_submit_still_works(client):
    """The positive control: the check must not close the door it guards."""
    store = fresh_store()
    user = _applicant(store)
    task_id = _draw_exam(client, user)
    res = client.post("/api/asclepius/exam/submit", headers=headers_for(user),
                      json={"task_id": task_id, "time_spent_sec": 900})
    assert res.status_code == 200, res.text
    assert store.get_tutorial_state(user["id"])["exam"]["task_id"] == task_id


# ── Every other task stays shut ──────────────────────────────────────────────

def test_another_applicants_examination_is_closed(client):
    """Two applicants in one specialty deliberately sit the SAME first case, so
    this cannot be tested with a second exam id — it is the same id. What is
    tested instead is the state: an applicant who has never drawn an exam has no
    claim on the case, even though somebody else's exam is sitting on it."""
    store = fresh_store()
    sitting = _applicant(store)
    task_id = _draw_exam(client, sitting)

    bystander = _applicant(store)
    assert store.get_tutorial_state(bystander["id"]).get("exam") in (None, {})
    res = client.get(f"/api/asclepius/tasks/{task_id}", headers=headers_for(bystander))
    assert res.status_code == 403
    assert res.headers.get("X-Asclepius-Auth-Gate") == "pending"


def test_a_different_specialtys_exam_case_is_closed(client):
    """A nephrology applicant may not open the cardiology examination case."""
    store = fresh_store()
    cardiologist = _applicant(store, "cardiology")
    cardiology_task = _draw_exam(client, cardiologist)

    nephrologist = _applicant(store, "nephrology")
    nephrology_task = _draw_exam(client, nephrologist)
    assert cardiology_task != nephrology_task

    res = client.get(f"/api/asclepius/tasks/{cardiology_task}",
                     headers=headers_for(nephrologist))
    assert res.status_code == 403
    assert res.headers.get("X-Asclepius-Auth-Gate") == "pending"
    assert _reveal(client, nephrologist, cardiology_task).status_code == 403


def test_an_ordinary_synthetic_task_is_closed(client):
    """The applicant has an examination. That is not a licence to open the queue."""
    store = fresh_store()
    user = _applicant(store)
    _draw_exam(client, user)

    task_id = _synthetic_task(store)
    res = client.get(f"/api/asclepius/tasks/{task_id}", headers=headers_for(user))
    assert res.status_code == 403
    assert _reveal(client, user, task_id).status_code == 403


def test_a_real_v4_case_is_closed(client):
    """The V4 real-data wall stands in front of the carve-out, not behind it."""
    store = fresh_store()
    user = _applicant(store)
    _draw_exam(client, user)

    task_id = _real_v4_task(store)
    assert client.get(f"/api/asclepius/tasks/{task_id}",
                      headers=headers_for(user)).status_code == 403
    assert _reveal(client, user, task_id).status_code == 403


def test_prelabel_on_someone_elses_task_is_closed(client):
    store = fresh_store()
    user = _applicant(store)
    _draw_exam(client, user)
    task_id = _synthetic_task(store)
    res = client.post("/api/asclepius/assist/prelabel",
                      headers=headers_for(user), json={"task_id": task_id})
    assert res.status_code == 403


def test_an_applicant_who_never_drew_an_exam_opens_nothing(client):
    """An empty ``exam`` blob is not a claim on a task. Neither is a blob that
    only records an attempt number."""
    store = fresh_store()
    user = _applicant(store)
    assert exam_case.is_users_exam_task(store, user, "gold-anything") is False
    store.set_tutorial_state(user["id"], {"exam": {"attempt": 1}})
    user = store.get_user_by_id(user["id"])
    assert exam_case.is_users_exam_task(store, user, "gold-anything") is False


def test_a_blank_task_id_is_never_somebodys_exam(client):
    store = fresh_store()
    user = _applicant(store)
    _draw_exam(client, user)
    assert exam_case.is_users_exam_task(store, user, "") is False
    assert exam_case.is_users_exam_task(store, user, "   ") is False


def test_a_legacy_in_flight_exam_is_recognised_by_recompute(client):
    """Applicants who were mid-examination when this shipped have a stamp with
    no ``task_id`` in it. They are re-derived through the same deterministic
    rotation that served them — so the migration is a recompute, not a rewrite,
    and no stored blob is edited to make it work."""
    store = fresh_store()
    user = _applicant(store)
    task_id = _draw_exam(client, user)

    legacy = store.get_tutorial_state(user["id"])
    legacy["exam"] = {"state": "in_progress", "attempt": 1}   # the pre-stamp shape
    store.set_tutorial_state(user["id"], legacy)
    user = store.get_user_by_id(user["id"])

    assert exam_case.is_users_exam_task(store, user, task_id) is True
    assert exam_case.is_users_exam_task(store, user, "gold-not-theirs") is False


# ── The library and the microphone ───────────────────────────────────────────

def test_an_applicant_can_search_the_citation_library(client):
    """"Search the library" is in the examination workspace and an applicant
    needs it to cite. It reveals nothing patient-specific — the library is
    published guidance."""
    store = fresh_store()
    user = _applicant(store)
    res = client.post("/api/asclepius/citations/search",
                      headers=headers_for(user),
                      json={"text": "dialysis initiation", "specialty": "nephrology"})
    assert res.status_code == 200, res.text


def test_an_applicant_can_reach_dictation(client):
    """Dictation is a UI convenience, not data access. 503 (no STT provider
    configured in the sandbox) is a pass here; 403 is the bug."""
    store = fresh_store()
    user = _applicant(store)
    res = client.post("/api/asclepius/transcribe", headers=headers_for(user),
                      files={"file": ("clip.webm", b"not-audio", "audio/webm")})
    assert res.status_code != 403, res.text


def test_an_applicant_gets_automatic_citation_suggestions(client):
    """``/assist/cite`` is the one-click chip beside the citation box, and it
    was MISSED when the library and the microphone were opened: PRD A §2.0
    listed the endpoints the exam workspace calls and this was not on the list.
    The client swallows the error, so nothing visibly broke — the chips were
    simply dead for the only population the examination exists for, which is
    what §2.3 means by "cite a guideline ... zero 403s"."""
    store = fresh_store()
    user = _applicant(store)
    res = client.post("/api/asclepius/assist/cite", headers=headers_for(user),
                      json={"text": "start dialysis when uraemic symptoms appear",
                            "specialty": "nephrology"})
    assert res.status_code == 200, res.text


def test_dictation_is_bounded(client):
    """The audience for this widened from approved physicians to any pending
    signup, in front of a metered provider. An unbounded ``await file.read()``
    behind a wider door is a bill, not a feature."""
    from routers.asclepius import TRANSCRIBE_MAX_BYTES

    store = fresh_store()
    user = _applicant(store)
    oversize = b"\0" * (TRANSCRIBE_MAX_BYTES + 1024)
    res = client.post("/api/asclepius/transcribe", headers=headers_for(user),
                      files={"file": ("clip.webm", oversize, "audio/webm")})
    assert res.status_code == 413, res.status_code


def test_a_refused_account_reaches_neither(client):
    """The carve-out is for people we are still reading, never for people we
    have already answered."""
    store = fresh_store()
    user = make_user(store, role="evaluator", tier=None, practice_case=False)
    store.set_verification_status(user["id"], "rejected")
    user = store.get_user_by_id(user["id"])
    assert asc_caps.access_level(user) == asc_caps.NONE
    assert client.post("/api/asclepius/citations/search", headers=headers_for(user),
                       json={"text": "x", "specialty": "nephrology"}).status_code == 403


# ── The approved physician notices nothing ───────────────────────────────────

def test_an_approved_physician_still_meets_the_practice_gate(client):
    """The regression that matters most. ``require_task_access`` reproduces the
    ``require_practice_case`` chain link for link for everybody who is not a
    provisional applicant on their own examination — so a physician who has not
    done the practice case is refused exactly as before, with the same
    structured error the client routes on."""
    store = fresh_store()
    user = make_user(store, role="evaluator", tier="labeler", practice_case=False)
    task_id = _synthetic_task(store)

    res = client.get(f"/api/asclepius/tasks/{task_id}", headers=headers_for(user))
    assert res.status_code == 403
    assert res.headers.get("X-Asclepius-Practice-Gate")
    assert res.json()["detail"]["error"] == "practice_case_required"


def test_an_approved_physician_opens_an_ordinary_task(client):
    store = fresh_store()
    user = make_user(store, role="evaluator", tier="labeler")
    task_id = _synthetic_task(store)
    assert client.get(f"/api/asclepius/tasks/{task_id}",
                      headers=headers_for(user)).status_code == 200


def test_an_untiered_physician_is_still_refused(client):
    """``require_label`` is the middle link of the chain and must not be lost."""
    store = fresh_store()
    user = make_user(store, role="evaluator", tier=None, practice_case=True)
    store.set_verification_status(user["id"], "verified")
    user = store.get_user_by_id(user["id"])
    task_id = _synthetic_task(store)
    assert client.get(f"/api/asclepius/tasks/{task_id}",
                      headers=headers_for(user)).status_code == 403
