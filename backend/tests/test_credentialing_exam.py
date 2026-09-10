"""The one case a physician is judged on.

An applicant used to be assessed on the PRACTICE case, which is a guided tour
with a "Skip this step" button on every screen. That is a poor basis for a
decision about somebody, and it made an exercise meant to teach behave like a
test they had to keep resitting.

So the practice case teaches and the EXAMINATION is what a person reads: one
synthetic case in the applicant's own specialty, in the same workspace and with
the same validation a paid case uses.

The properties worth pinning are not "the endpoint returns 200":

  * a nephrologist sits a nephrology case, and when we cannot serve their
    specialty the response SAYS so rather than quietly handing them another;
  * the answers cannot reach the pay or export pipeline, structurally;
  * nothing anywhere tells the applicant how they did, because that decision
    belongs to the person reading it;
  * a rejection reopens the case work instead of closing the account, and does
    NOT reopen it for anybody rejected before that was true.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from tests._asclepius import app, fresh_store, headers_for, make_user

from asclepius import capabilities as asc_caps
from asclepius import exam_case


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _applicant(store, specialty="nephrology"):
    user = make_user(store, role="evaluator", specialty=specialty)
    store.set_verification_status(user["id"], "pending")
    return user


# ── Which case they sit ─────────────────────────────────────────────────────

def test_a_physician_sits_a_case_in_their_own_specialty(client):
    store = fresh_store()
    for specialty in ("nephrology", "cardiology", "oncology"):
        user = _applicant(store, specialty)
        res = client.get("/api/asclepius/exam/task", headers=headers_for(user))
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["specialty"] == specialty
        assert body["is_own_specialty"] is True
        assert body["task"]["task_id"].startswith("gold-")


def test_a_fallback_is_said_out_loud(client):
    """Handing a dermatologist a kidney case without a word would read as a
    broken product, and they would reasonably answer it as though we had
    made a mistake."""
    store = fresh_store()
    user = _applicant(store, "dermatology")
    body = client.get("/api/asclepius/exam/task", headers=headers_for(user)).json()
    assert body["specialty"] == exam_case.FALLBACK_SPECIALTY
    assert body["is_own_specialty"] is False


def test_the_examination_case_is_blinded_like_a_real_one(client):
    """It is worth reading only because it is the real thing. A case that
    leaked its answer key would be measuring nothing."""
    store = fresh_store()
    task = client.get("/api/asclepius/exam/task",
                      headers=headers_for(_applicant(store))).json()["task"]
    assert "ground_truth" not in task
    for answer in task.get("candidate_answers", []):
        assert "text" not in answer or not answer.get("text")


def test_two_applicants_in_one_specialty_sit_the_same_first_case(client):
    """Which is what makes them comparable to the person reading both."""
    store = fresh_store()
    a = client.get("/api/asclepius/exam/task",
                   headers=headers_for(_applicant(store))).json()
    b = client.get("/api/asclepius/exam/task",
                   headers=headers_for(_applicant(store))).json()
    assert a["task"]["task_id"] == b["task"]["task_id"]


def test_drawing_the_case_does_not_consume_it(client):
    """An examination must not take a lease, count against max_labels, or
    reorder what a paid physician sees next."""
    store = fresh_store()
    user = _applicant(store)
    task_id = client.get("/api/asclepius/exam/task",
                         headers=headers_for(user)).json()["task"]["task_id"]
    before = dict(store.get_task(task_id))
    client.get("/api/asclepius/exam/task", headers=headers_for(user))
    assert dict(store.get_task(task_id)) == before


# ── Where the answers go ────────────────────────────────────────────────────

def _submit(client, user, task_id):
    return client.post("/api/asclepius/exam/submit",
                       json={"task_id": task_id, "verdict": "A_better",
                             "chosen_id": "A", "time_spent_sec": 640},
                       headers=headers_for(user))


def test_an_examination_never_enters_the_paid_pipeline(client):
    """THE structural property.

    The examination is drawn from the gold set, which is also served to paid
    physicians. Writing these answers into `submissions` against a live task_id
    would put an unverified applicant's work within reach of the pay and export
    paths, and AGENTS.md documents an "exactly three code sites may write
    export_ready" invariant that nobody should be testing on a hunch.
    """
    store = fresh_store()
    user = _applicant(store)
    task_id = client.get("/api/asclepius/exam/task",
                         headers=headers_for(user)).json()["task"]["task_id"]
    assert _submit(client, user, task_id).status_code == 200

    with store._conn() as conn:
        subs = conn.execute(
            "SELECT COUNT(*) n FROM submissions WHERE evaluator_id = ?",
            (user["id"],)).fetchone()["n"]
        recs = conn.execute("SELECT COUNT(*) n FROM records").fetchone()["n"]
    assert subs == 0, "an examination wrote a submissions row"
    assert recs == 0, "an examination wrote a records row"

    filed = store.list_credentialing_exams(user["id"])
    assert len(filed) == 1
    assert filed[0]["task_id"] == task_id
    assert filed[0]["payload"]["verdict"] == "A_better"


def test_the_applicant_is_never_told_how_they_did(client):
    """The founders were explicit: no applicant is told they are not ready, and
    the admin console has the only say."""
    store = fresh_store()
    user = _applicant(store)
    task_id = client.get("/api/asclepius/exam/task",
                         headers=headers_for(user)).json()["task"]["task_id"]
    body = _submit(client, user, task_id).json()

    # The submit response says it landed and nothing else.
    assert set(body) == {"ok", "user"}
    exam = body["user"]["tutorial"]["exam"]
    assert exam["state"] == "submitted"
    # State, attempt and a timestamp. No outcome, and no exam_id: nothing good
    # comes of the client holding one, and an id is a handle to fetch with.
    assert set(exam) == {"state", "attempt", "submitted_at"}

    # No grade anywhere in the tutorial block either. `gate_state` is in there
    # and is legitimately allowed to say "passed": that is the PRACTICE gate,
    # not a verdict on the examination, so this checks the values that would
    # constitute a grade rather than grepping for a word.
    tutorial = body["user"]["tutorial"]
    assert "score" not in tutorial
    assert "matched" not in tutorial

    # And the stored blob DOES still hold what the admin needs, so this is a
    # projection rather than the product forgetting.
    stored = store.get_tutorial_state(user["id"])
    assert stored["exam"]["exam_id"]


def test_the_admin_sees_the_examination(client):
    """It is the thing they are deciding on, so it has to reach them."""
    from routers.asclepius_verify import _examination_block

    store = fresh_store()
    user = _applicant(store)
    task_id = client.get("/api/asclepius/exam/task",
                         headers=headers_for(user)).json()["task"]["task_id"]
    _submit(client, user, task_id)

    block = _examination_block(store, store.get_user_by_id(user["id"]))
    assert block["state"] == "submitted"
    assert block["attempts"] == 1
    assert block["submissions"][0]["task_id"] == task_id
    assert block["submissions"][0]["payload"]["verdict"] == "A_better"


def test_a_queue_row_is_ready_only_once_the_examination_is_in(client):
    """It used to wait on the PRACTICE case, which is optional and skippable by
    design, so the filter hid applicants on something they were told they need
    not do."""
    from routers.asclepius_verify import _is_ready_for_review

    store = fresh_store()
    user = _applicant(store)
    store.set_npi(user["id"], "1234567893") if hasattr(store, "set_npi") else None
    with store._conn() as conn:
        conn.execute("UPDATE users SET npi = ? WHERE id = ?", ("1234567893", user["id"]))

    assert not _is_ready_for_review(store.get_user_by_id(user["id"]))
    task_id = client.get("/api/asclepius/exam/task",
                         headers=headers_for(user)).json()["task"]["task_id"]
    _submit(client, user, task_id)
    assert _is_ready_for_review(store.get_user_by_id(user["id"]))


# ── Rejection is a request to try again ─────────────────────────────────────

def test_a_rejection_reopens_the_case_work(client):
    store = fresh_store()
    admin = make_user(store, role="admin")
    user = _applicant(store)
    task_id = client.get("/api/asclepius/exam/task",
                         headers=headers_for(user)).json()["task"]["task_id"]
    _submit(client, user, task_id)

    res = client.post(f"/api/asclepius/verify/queue/{user['id']}/reject",
                      json={"note": "the read missed the contraindication"},
                      headers=headers_for(admin))
    assert res.status_code == 200, res.text

    me = client.get("/api/asclepius/auth/me", headers=headers_for(user))
    assert me.status_code == 200, "a rejected physician can no longer sign in"
    body = me.json()
    assert body["access_level"] == "provisional"
    assert "real_work" not in body["surfaces"]
    # The demo and the practice case are offered again before the fresh
    # examination: somebody asked to re-sit should be shown the help first.
    assert body["tutorial"]["resources_seen_at"] is None
    assert body["tutorial"]["exam"]["state"] == "retake"


def test_a_retake_draws_a_different_case(client):
    store = fresh_store()
    admin = make_user(store, role="admin")
    user = _applicant(store)
    first = client.get("/api/asclepius/exam/task",
                       headers=headers_for(user)).json()["task"]["task_id"]
    _submit(client, user, first)
    client.post(f"/api/asclepius/verify/queue/{user['id']}/reject",
                json={"note": "another look please"}, headers=headers_for(admin))

    second = client.get("/api/asclepius/exam/task",
                        headers=headers_for(user)).json()["task"]["task_id"]
    assert second != first, "a retake re-served the case that went badly"


def test_a_rejection_from_before_the_retake_existed_keeps_its_meaning():
    """The production-safety property.

    Flipping every `rejected` row to PROVISIONAL would silently hand sign-in
    back to everybody rejected before this shipped, including whoever was
    rejected for not being a clinician at all. Rows written before the retake
    existed carry no stamp.
    """
    legacy = {"active": 1, "verification_status": "rejected"}
    assert asc_caps.access_level(legacy) == asc_caps.NONE

    offered = {"active": 1, "verification_status": "rejected",
               "tutorial_json": '{"retake_offered_at": "2026-09-04T00:00:00Z"}'}
    assert asc_caps.access_level(offered) == asc_caps.PROVISIONAL

    # And deactivation still closes it, stamp or no stamp.
    gone = {**offered, "active": 0}
    assert asc_caps.access_level(gone) == asc_caps.NONE


# ── The portal side, asserted on the shipped source ─────────────────────────

import pathlib  # noqa: E402

_FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
_JS = (_FRONTEND / "asclepius.js").read_text(encoding="utf-8")
_FIRST_RUN = (_FRONTEND / "first_run.js").read_text(encoding="utf-8")


def _strip_js_comments(source: str) -> str:
    out, i, n = [], 0, len(source)
    while i < n:
        if source.startswith("/*", i):
            end = source.find("*/", i + 2)
            i = n if end == -1 else end + 2
        elif source.startswith("//", i):
            end = source.find("\n", i)
            i = n if end == -1 else end
        else:
            out.append(source[i])
            i += 1
    return "".join(out)


_CODE = _strip_js_comments(_JS)


def test_the_applicant_screen_says_which_parts_are_optional():
    """A physician used to be dropped into a case that decides about them with
    no warning that it did.

    The pre-examination resources screen that first answered this is gone with
    the rest of the staged journey (PRD A §1.1). Its job moved onto the
    applicant's one screen, where the same distinction is drawn structurally
    rather than in a sentence: card 1's items are quiet rows and the practice
    case is a link, and the only primary button on the page is the
    examination's."""
    start = _CODE.index("function renderApplicantHome()")
    screen = _CODE[start:_CODE.index("\n  function ", start + 10)]
    assert "playDemo" in screen and "startTutorial" in screen
    assert "Optional: try a practice case first" in screen
    assert screen.count("asc-btn-primary") == 1


def test_the_examination_is_a_separate_flag_from_the_practice_case():
    """They share the workspace and mean opposite things: one is practice
    nobody reads, the other is the case a person decides on. One flag for both
    is how a practice run gets filed as somebody's examination."""
    assert "function examActive" in _CODE
    assert "exam: null," in _CODE
    submit = _CODE[_CODE.index("async function submitEvaluation"):][:3000]
    assert "if (tutorialActive()) { await submitTutorialEvaluation(); return; }" in submit
    assert "if (examActive()) { await submitExamEvaluation(); return; }" in submit


def test_the_examination_keeps_every_gate_the_practice_case_drops():
    """It is the work sample. An examination somebody could submit half
    finished would measure nothing, which is the opposite of the practice
    case's problem."""
    submit = _CODE[_CODE.index("async function submitEvaluation"):][:3000]
    tutorial_at = submit.index("submitTutorialEvaluation")
    exam_at = submit.index("submitExamEvaluation")
    grounding_at = submit.index("groundingSatisfied")
    assert tutorial_at < grounding_at, "the practice bypass must come first"
    assert grounding_at < exam_at, "the examination must sit behind the gates"


def test_the_examination_can_be_paused_without_losing_the_answers():
    """A physician part way through, realising they are not ready, had two
    options: guess, or abandon the tab."""
    fn = _CODE[_CODE.index("function pauseExam"):][:400]
    assert "saveDraft()" in fn
    assert "stopTimer()" in fn
    assert "renderDashboardView()" in fn
    assert "clearDraft" not in fn, "pausing must not throw the answers away"


def test_nothing_after_the_examination_congratulates_anybody():
    # Bounded at the next declaration rather than by a character count. The
    # fixed 1200-char window used to run past the end of this function into its
    # neighbour, so the positive half of this test was being satisfied by copy
    # on a different screen and would have kept passing if this one lost it.
    start = _CODE.index("function renderExamSubmitted")
    # _CODE has its comments stripped, so the bound has to be a declaration.
    ends = [_CODE.find(tok, start + 1) for tok in ("\n  function ", "\n  var ", "\n  const ")]
    screen = _CODE[start:min(e for e in ends if e != -1)]
    for verdict in ("passed", "Well done", "score", "Congratulations", "failed"):
        assert verdict not in screen, verdict
    # Whitespace-normalised: the sentence is wrapped across two string literals
    # in the source, and an assertion that a line break defeats is an assertion
    # about formatting rather than about copy.
    flat = re.sub(r"'\s*\+\s*'", "", screen)
    assert "read" in flat and "email you either way" in flat


def test_the_standalone_demo_cannot_close_a_walkthrough_stop():
    """The resources screen reuses the walkthrough's player, and an applicant
    has no walkthrough: theirs is suppressed until approval. Without the guard,
    watching the demo would write a stop on an account that has none and then
    hand control to a tour that is not running."""
    code = _strip_js_comments(_FIRST_RUN)
    assert "playDemo: function" in code
    assert "demoAvailable: function" in code
    # Anchored on openDemo rather than on the button label: "Start the practice
    # case" also appears on the walkthrough's choice card, and indexing the
    # first occurrence found the wrong one.
    open_demo = code[code.index("function openDemo"):]
    open_demo = open_demo[:open_demo.index("function attachDemoSource")]
    assert "if (standaloneDemo) { closeDemo(); return; }" in open_demo, (
        "the panel after the video can still close a stop that is not open")
    # And the flag is cleared on close, or a later walkthrough run inherits it.
    close_fn = code[code.index("function closeDemo"):][:200]
    assert "standaloneDemo = false" in close_fn


def test_exam_snapshot_is_persistent_admin_only_and_export_is_user_scoped(client):
    from routers.asclepius_verify import _examination_block
    store = fresh_store()
    user = _applicant(store)
    task_id = client.get('/api/asclepius/exam/task', headers=headers_for(user)).json()['task']['task_id']
    response = _submit(client, user, task_id)
    assert response.status_code == 200
    assert 'score_version' not in response.text and 'key_data_matched' not in response.text
    saved = store.list_credentialing_exams(user['id'])[0]
    assert saved['metadata']['reconstructed'] is False
    original = store.get_task
    store.get_task = lambda task_id: {}
    try:
        block = _examination_block(store, store.get_user_by_id(user['id']))
        assert block['submissions'][0]['metadata'] == saved['metadata']
    finally:
        store.get_task = original
    admin = make_user(store, role='admin')
    path = f"/api/asclepius/verify/queue/{user['id']}/examination/{saved['exam_id']}/export"
    assert client.get(path, headers=headers_for(user)).status_code == 403
    result = client.get(path, headers=headers_for(admin))
    assert result.status_code == 200
    assert result.json()['physician_answers']['task_id'] == task_id
    assert result.json()['answer_key']
    assert result.json()['case']['candidate_answers'] == store.get_task(task_id)['candidate_answers']
    assert result.json()['case']['prompt'] == store.get_task(task_id)['prompt']
    assert result.headers['cache-control'] == 'private, no-store'
    other = _applicant(store)
    assert client.get(path.replace(user['id'], other['id']), headers=headers_for(admin)).status_code == 404
    with store._conn() as conn:
        conn.execute('UPDATE credentialing_exams SET metadata_json = NULL WHERE exam_id = ?', (saved['exam_id'],))
    assert client.get(path, headers=headers_for(admin)).json()['reconstructed'] is True
