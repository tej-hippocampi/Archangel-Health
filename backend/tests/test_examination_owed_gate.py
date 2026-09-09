"""The door on the gate card for an applicant who still owes us the examination.

Onboarding Master PRD §3.2 steps 2–3, against finding F1.

The bug this pins down is not a crash and not a 500. A legacy passwordless
applicant — one who finished the wizard during the window when it had no
password step — signs in, is told "your application is in review, we'll email
you", and is handed a "Check again" button that re-runs the same refusal
forever. Meanwhile the thing we are actually waiting on is THEIR examination.
Both halves of that screen are wrong: the sentence, and the only control on it.

So the properties worth pinning are about the SHAPE of the answer, not about a
status code:

  * the two waiting states are distinguishable on the wire, so a client can
    tell "we owe you" from "you owe us";
  * the card for "you owe us" carries a control that CHANGES something, and
    does not carry the one that cannot;
  * the door is the ordinary forgot-password mint — no second recovery path,
    no admin in the loop — and it really does convert a NO_PASSWORD_HASH
    account into one that can sign in;
  * a converted account lands on the applicant home, which is where the
    examination is.
"""

from __future__ import annotations

import pathlib
import uuid

from fastapi.testclient import TestClient

from tests._asclepius import app, fresh_store

from asclepius import auth as asc_auth
from asclepius import exam_case
from asclepius import store as asc_store_mod

_PORTAL_JS = (pathlib.Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
              / "asclepius.js").read_text(encoding="utf-8")


def _extract_fn(source: str, name: str) -> str:
    """The body of one `function name(` declaration, brace-balanced.

    Same approach as the other DOM tests here: assert against the function that
    actually renders the screen rather than against the whole file, so a string
    living in an unrelated renderer cannot satisfy the assertion.
    """
    needle = f"function {name}("
    start = source.find(needle)
    assert start != -1, f"function {name} not found in asclepius.js"
    i = source.find("{", start)
    depth, n = 0, len(source)
    while i < n:
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
        i += 1
    raise AssertionError(f"unbalanced braces extracting {name}")


def _passwordless_applicant(store, exam_state=None):
    applicant = store.provision_user(
        email=f"dr_{uuid.uuid4().hex[:8]}@hospital.example.org",
        password_hash=asc_store_mod.NO_PASSWORD_HASH,
        role="evaluator", full_name="Rosalind Achebe", credentials={}, attestations={},
    )
    store.set_verification_status(applicant["id"], "pending")
    if exam_state:
        store.set_tutorial_state(applicant["id"], {
            "status": "not_started", "version": None,
            "exam": {"state": exam_state, "attempt": 1, "task_id": "gold-x"},
        })
    return applicant


# ─── The wire ────────────────────────────────────────────────────────────────

def test_the_two_waiting_states_are_distinguishable_on_the_wire():
    """One header value cannot carry two meanings. A client that sees only
    'pending' has no way to know which side the ball is on."""
    store = fresh_store()
    owes_us = _passwordless_applicant(store)
    owes_nothing = _passwordless_applicant(store, exam_state="submitted")

    c = TestClient(app)
    a = c.post("/api/asclepius/auth/login",
               json={"email": owes_us["email"], "password": "x"})
    b = c.post("/api/asclepius/auth/login",
               json={"email": owes_nothing["email"], "password": "x"})

    assert a.headers.get(asc_auth.AUTH_GATE_HEADER) == "pending_examination"
    assert b.headers.get(asc_auth.AUTH_GATE_HEADER) == "pending"
    assert a.json()["detail"] != b.json()["detail"]


def test_exam_state_reads_the_blob_shape_the_server_actually_writes():
    """`exam_state` is only useful if it agrees with /exam/task's stamp.

    get_tutorial_state DISCARDS a blob with no top-level `status`, which is a
    real trap: a reader that tested itself against a bare {"exam": ...} fixture
    would pass while answering not_started for every live account.
    """
    store = fresh_store()
    user = _passwordless_applicant(store)
    assert exam_case.exam_state(store, user) == "not_started"

    # Exactly what /exam/task writes: the existing blob, round-tripped, with
    # `exam` set on it.
    blob = store.get_tutorial_state(user["id"])
    blob["exam"] = {"state": "in_progress", "attempt": 1, "task_id": "gold-1"}
    store.set_tutorial_state(user["id"], blob)
    assert exam_case.exam_state(store, user) == "in_progress"

    blob["exam"]["state"] = "submitted"
    store.set_tutorial_state(user["id"], blob)
    assert exam_case.exam_state(store, user) == "submitted"


def test_an_unreadable_blob_offers_the_door_rather_than_withholding_it():
    """Fail open, in the direction that cannot lock somebody out."""
    class Exploding:
        def get_tutorial_state(self, _uid):
            raise RuntimeError("disk went away")

    assert exam_case.exam_state(Exploding(), {"id": "u1"}) == "not_started"


def test_a_junk_exam_value_is_not_treated_as_submitted():
    """Only the two known words close the door; anything else owes us one."""
    store = fresh_store()
    user = _passwordless_applicant(store)
    blob = store.get_tutorial_state(user["id"])
    for junk in ("", "SUBMITTED", "done", None, 7, {"nested": 1}):
        blob["exam"] = {"state": junk, "attempt": 1}
        store.set_tutorial_state(user["id"], blob)
        assert exam_case.exam_state(store, user) == "not_started", junk


# ─── The door actually opens ─────────────────────────────────────────────────

def test_forgot_password_really_converts_a_passwordless_applicant(monkeypatch):
    """The whole premise of step 2, end to end and with nothing stubbed in the
    middle: no new endpoint is needed, because a "reset" SETS a first password
    on a NO_PASSWORD_HASH account and clears must_change_password with it.

    If this ever stops being true, the card built on it becomes a button that
    mails a link that cannot let anybody in — which is worse than the dead end
    it replaced, because it looks like it worked.
    """
    from routers import asclepius as asc_router

    store = fresh_store()
    applicant = _passwordless_applicant(store)
    assert asc_store_mod.password_is_unset(applicant)

    # The raw token only ever exists in the mail. Intercept the send rather than
    # reach into the token table, so the test walks the path the applicant does.
    sent: list = []

    async def _capture(email, raw_token):
        sent.append((email, raw_token))

    monkeypatch.setattr(asc_router, "_mail_password_reset", _capture)

    c = TestClient(app)
    r = c.post("/api/asclepius/auth/password/forgot",
               json={"email": applicant["email"]})
    assert r.status_code == 200, r.text
    assert len(sent) == 1, "a live applicant must actually be mailed a link"
    assert sent[0][0] == applicant["email"]

    r = c.post("/api/asclepius/auth/password/reset",
               json={"token": sent[0][1], "new_password": "Corr3ct-Horse-Battery!"})
    assert r.status_code == 200, r.text

    after = store.get_user_by_email(applicant["email"])
    assert not asc_store_mod.password_is_unset(after)
    assert not after.get("must_change_password"), (
        "a converted applicant must not be bounced into a change-password wall"
    )

    # And the credential works at the front door.
    r = c.post("/api/asclepius/auth/login",
               json={"email": applicant["email"],
                     "password": "Corr3ct-Horse-Battery!"})
    assert r.status_code == 200, r.text


def test_a_converted_account_can_sign_in_and_is_still_provisional():
    """Step 3: setting a password must not approve anybody. They land back in
    the portal as a provisional applicant, which is what puts them on the
    applicant home and therefore on the examination."""
    store = fresh_store()
    applicant = _passwordless_applicant(store)
    store.set_user_password(applicant["id"], "Corr3ct-Horse-Battery!")

    c = TestClient(app)
    r = c.post("/api/asclepius/auth/login",
               json={"email": applicant["email"],
                     "password": "Corr3ct-Horse-Battery!"})
    assert r.status_code == 200, r.text
    assert (r.json()["user"].get("verification_status") or "pending") == "pending"


# ─── The card ────────────────────────────────────────────────────────────────

def test_the_gate_router_sends_the_new_state_to_the_new_card():
    fn = _extract_fn(_PORTAL_JS, "renderGated")
    assert "pending_examination" in fn
    assert "renderExaminationOwed" in fn


def test_verification_gate_accepts_the_new_header_value():
    """A 403 the client does not recognise is rendered as a plain refusal, so
    the header has to be on the allowlist or the card is unreachable."""
    fn = _extract_fn(_PORTAL_JS, "verificationGate")
    assert "pending_examination" in fn


def test_the_login_form_routes_the_new_gate_to_the_card_not_inline():
    """The path that matters. This gate arrives from the LOGIN call itself, so
    if renderLogin's catch does not know the state, the applicant reads 'set a
    password' inline on a form with nothing that sets one."""
    fn = _extract_fn(_PORTAL_JS, "renderLogin")
    assert "pending_examination" in fn, (
        "renderLogin's catch must route pending_examination to the card; "
        "otherwise the message lands inline with no door"
    )


def test_the_card_offers_a_password_door_and_no_check_again():
    fn = _extract_fn(_PORTAL_JS, "renderExaminationOwed")
    assert "Set my password" in fn
    assert "/auth/password/forgot" in fn
    assert "Sign in with a different account" in fn
    # The control that cannot help is the one that must not be here: it would
    # re-run the identical refusal, which is the failure being fixed.
    assert "Check again" not in fn, "the card must not offer Check again"


def test_the_waiting_room_keeps_check_again():
    """The other card is correct as it stands — nothing here may regress it."""
    fn = _extract_fn(_PORTAL_JS, "renderAwaitingVerification")
    assert "Check again" in fn
    assert "Set my password" not in fn


def test_the_card_never_leaves_a_dead_control():
    """A button stuck on 'Sending…' is how a blocked physician concludes the
    product is broken — the same failure mode the waiting room guards."""
    fn = _extract_fn(_PORTAL_JS, "renderExaminationOwed")
    catch = fn[fn.find("catch"):]
    assert "removeAttribute('disabled')" in catch


# ─── Where a converted account lands (step 3) ────────────────────────────────

def test_a_provisional_account_is_routed_home_before_the_tutorial_can_grab_it():
    """Step 3 rests on an ORDERING, so assert the ordering rather than the
    existence of either branch.

    A converted legacy applicant boots with an empty-or-legacy tutorial blob,
    which parses to gate_state 'locked' — and the practice-case launch below
    fires on exactly that. If the provisional guard ever moves after it, the
    applicant we just handed a password to is thrown into Calibration Case 1
    instead of onto the examination, and the whole repair lands them one screen
    short of the thing it was for.
    """
    fn = _extract_fn(_PORTAL_JS, "enterApp")
    guard = fn.find("sessionIsProvisional() && !isAdvisor()")
    launch = fn.find("startTutorial(")
    assert guard != -1, "enterApp must route a provisional account explicitly"
    assert launch != -1, "the practice-case launch is expected to still exist"
    assert guard < launch, (
        "the provisional guard must precede the tutorial launch, or a converted "
        "legacy applicant lands inside a practice case instead of on their "
        "examination"
    )


def test_the_provisional_landing_is_the_applicant_home():
    """And the screen it routes to is the one carrying the examination card."""
    fn = _extract_fn(_PORTAL_JS, "renderDashboardView")
    assert "renderApplicantHome" in fn
