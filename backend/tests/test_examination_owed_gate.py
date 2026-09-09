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


def _code(source: str) -> str:
    """JS with comments removed.

    This codebase explains its rules in prose beside the code that follows them,
    which makes a raw grep over a renderer ambiguous in BOTH directions: a
    comment naming the control we removed fails an assertion that the control is
    gone, and a comment quoting a call satisfies an assertion that the call is
    made. Neither is the contract. Strip first, then assert.
    """
    out, i, n = [], 0, len(source)
    while i < n:
        if source.startswith("/*", i):
            end = source.find("*/", i + 2)
            i = n if end == -1 else end + 2
        elif source.startswith("//", i):
            end = source.find("\n", i)
            i = n if end == -1 else end
        elif source[i] in "\"'":
            quote, j = source[i], i + 1
            while j < n and source[j] != quote:
                j += 2 if source[j] == "\\" else 1
            out.append(source[i:j + 1])
            i = j + 1
        else:
            out.append(source[i])
            i += 1
    return "".join(out)


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
    fn = _code(_extract_fn(_PORTAL_JS, "renderGated"))
    assert "pending_examination" in fn
    assert "renderExaminationOwed" in fn


def test_verification_gate_accepts_the_new_header_value():
    """A 403 the client does not recognise is rendered as a plain refusal, so
    the header has to be on the allowlist or the card is unreachable."""
    fn = _code(_extract_fn(_PORTAL_JS, "verificationGate"))
    assert "pending_examination" in fn


def test_the_login_form_routes_the_new_gate_to_the_card_not_inline():
    """The path that matters. This gate arrives from the LOGIN call itself, so
    if renderLogin's catch does not know the state, the applicant reads 'set a
    password' inline on a form with nothing that sets one."""
    fn = _code(_extract_fn(_PORTAL_JS, "renderLogin"))
    assert "pending_examination" in fn, (
        "renderLogin's catch must route pending_examination to the card; "
        "otherwise the message lands inline with no door"
    )


def test_the_card_offers_a_password_door_and_no_check_again():
    fn = _code(_extract_fn(_PORTAL_JS, "renderExaminationOwed"))
    assert "Set my password" in fn
    assert "/auth/password/forgot" in fn
    assert "Sign in with a different account" in fn
    # The control that cannot help is the one that must not be here: it would
    # re-run the identical refusal, which is the failure being fixed.
    assert "Check again" not in fn, "the card must not offer Check again"


def test_the_waiting_room_keeps_check_again():
    """The other card is correct as it stands — nothing here may regress it."""
    fn = _code(_extract_fn(_PORTAL_JS, "renderAwaitingVerification"))
    assert "Check again" in fn
    assert "Set my password" not in fn


def test_the_card_never_leaves_a_dead_control():
    """A button stuck on 'Sending…' is how a blocked physician concludes the
    product is broken — the same failure mode the waiting room guards."""
    fn = _code(_extract_fn(_PORTAL_JS, "renderExaminationOwed"))
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
    fn = _code(_extract_fn(_PORTAL_JS, "enterApp"))
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
    fn = _code(_extract_fn(_PORTAL_JS, "renderDashboardView"))
    assert "renderApplicantHome" in fn


# ─── The door must not lie about having sent anything ────────────────────────

def test_the_card_does_not_report_success_on_an_http_error():
    """`fetch` does not reject on 4xx/5xx, so a hand-rolled `await fetch(...)`
    falls straight through to the success path on every HTTP error.

    Three reachable ones, none exotic: /auth/password/forgot is rate-limited per
    IP, so a hospital behind one NAT gets a 429; the live-reset ceiling answers
    200 but mails nothing; and the server can be unwell. Reporting "sent" for
    any of them sends the applicant to a mailbox that will never receive
    anything AND leaves this button dead on "Sent" — strictly worse than the
    "Check again" the card replaced, which at least stayed clickable.

    So the call goes through `api()`, which throws on !res.ok.
    """
    fn = _code(_extract_fn(_PORTAL_JS, "renderExaminationOwed"))
    assert "api('/auth/password/forgot'" in fn, (
        "the door must go through api(), which raises on a non-2xx"
    )
    assert "fetch(" not in fn, (
        "a bare fetch here cannot tell success from a 429 — that is the bug"
    )


def test_the_card_reports_the_servers_reason_when_it_fails():
    """A generic "could not reach the server" on a 429 is a lie in the other
    direction: the server was reached and said something useful."""
    fn = _code(_extract_fn(_PORTAL_JS, "renderExaminationOwed"))
    # The catch belonging to the MINT, not the last catch in the function (that
    # one is the localStorage guard on the secondary button, and it is correctly
    # silent). Anchored to the call so this cannot drift onto the wrong handler.
    after = fn[fn.index("api('/auth/password/forgot'"):]
    catch = after[after.index("catch"):]
    assert "e.message" in catch, (
        "a generic 'could not reach the server' on a 429 is a lie in the other "
        "direction: the server was reached and said something useful"
    )


def test_the_card_never_promises_delivery_it_cannot_confirm():
    """The endpoint answers identically for an address it will NOT mail — an
    inactive account, an unknown one — because answering differently would be
    an enumeration oracle. So the client cannot claim delivery either; it hedges
    exactly as the server's own sentence does."""
    fn = _code(_extract_fn(_PORTAL_JS, "renderExaminationOwed"))
    assert "has an account with us" in fn, "the fallback copy must hedge"
    assert "We\\u2019ve emailed " not in fn, (
        "an unconditional 'we've emailed you' claims a delivery the server "
        "never promised"
    )


def test_the_sign_in_forms_forgot_button_has_the_same_guard():
    """It is the door the legacy-applicant hint on that screen points at, so a
    silent lie there strands exactly the people §3 exists for."""
    fn = _code(_extract_fn(_PORTAL_JS, "renderLogin"))
    start = fn.index("Forgot your password?") if "Forgot your password?" in fn else 0
    assert "api('/auth/password/forgot'" in fn
    assert "/auth/password/forgot', {\n            method: 'POST',\n            headers:" not in fn


def test_the_sign_in_forms_forgot_notice_is_actually_visible():
    """errBox is created hidden when the screen opens without an error, so a
    handler that only sets textContent writes into an invisible div. The
    physician clicks, nothing appears, and the door reads as broken."""
    fn = _code(_extract_fn(_PORTAL_JS, "renderLogin"))
    i = fn.index("Forgot your password?")
    handler = fn[max(0, i - 2000):i]
    assert "errBox.removeAttribute('hidden')" in handler, (
        "the forgot handler must unhide errBox or its message is never seen"
    )


# ─── The coupling, driven rather than assumed (PRD §3.3, last line) ──────────

def test_the_gate_agrees_with_what_the_exam_endpoints_actually_write():
    """EVERY OTHER TEST HERE HAND-BUILDS THE BLOB, and that is the gap.

    A fixture encodes a belief about what the server writes. It is correct
    today, but the coupling is what matters: rename `exam.state`, or have a
    writer replace the blob instead of round-tripping it, and `exam_state`
    quietly answers `not_started` forever. The gate then tells an applicant who
    HAS filed their examination that they still owe us one — with every test
    above still green, because they all assert against the same belief.

    So this one touches no blob. It drives `/exam/task` and `/exam/submit` and
    reads the answer back through the login gate, which is the whole chain the
    repair depends on.
    """
    from tests._asclepius import make_user

    store = fresh_store()
    # An applicant, exactly as test_exam_task_access builds one: no tier, no
    # practice case, pending. Inheriting an approved account's tier would make
    # the assertions vacuous.
    user = make_user(store, role="evaluator", specialty="nephrology",
                     tier=None, practice_case=False)
    store.set_verification_status(user["id"], "pending")
    user = store.get_user_by_id(user["id"])

    c = TestClient(app)
    hdrs = {"Authorization": "Bearer " + asc_auth.create_token(user)}

    # Nothing drawn yet: the examination is owed.
    assert exam_case.exam_state(store, user) == "not_started"

    res = c.get("/api/asclepius/exam/task", headers=hdrs)
    assert res.status_code == 200, res.text
    task_id = res.json()["task"]["task_id"]

    # The DRAW alone must move the gate, or somebody who closes the tab
    # mid-examination is told to start something they are halfway through.
    assert exam_case.exam_state(store, user) == "in_progress"

    res = c.post("/api/asclepius/exam/submit", headers=hdrs,
                 json={"task_id": task_id, "time_spent_sec": 900})
    assert res.status_code == 200, res.text
    assert exam_case.exam_state(store, user) == "submitted"


def test_a_filed_examination_stops_the_gate_asking_for_it_again():
    """The other half of the same chain, through the front door.

    A passwordless applicant who has actually sat and filed their examination
    must get the waiting room, not "one step left". Driven through the real
    endpoints for the same reason as above.
    """
    store = fresh_store()
    applicant = _passwordless_applicant(store)
    c = TestClient(app)
    hdrs = {"Authorization": "Bearer " + asc_auth.create_token(applicant)}

    # Before: we are waiting on them.
    r = c.post("/api/asclepius/auth/login",
               json={"email": applicant["email"], "password": "x"})
    assert r.headers.get(asc_auth.AUTH_GATE_HEADER) == "pending_examination"

    res = c.get("/api/asclepius/exam/task", headers=hdrs)
    assert res.status_code == 200, res.text
    task_id = res.json()["task"]["task_id"]
    res = c.post("/api/asclepius/exam/submit", headers=hdrs,
                 json={"task_id": task_id, "time_spent_sec": 900})
    assert res.status_code == 200, res.text

    # After: they are waiting on us, and the copy says so.
    r = c.post("/api/asclepius/auth/login",
               json={"email": applicant["email"], "password": "x"})
    assert r.headers.get(asc_auth.AUTH_GATE_HEADER) == "pending", r.text
    assert "24–48 hours" in r.json()["detail"]


# ─── The third sign-in surface (the landing site's own dialog) ───────────────

_AUTH_API_TS = (pathlib.Path(__file__).resolve().parents[2] / "landing" / "src"
                / "lib" / "auth-api.ts")
_SIGNIN_DIALOG_TSX = (pathlib.Path(__file__).resolve().parents[2] / "landing" / "src"
                      / "app" / "components" / "SignInDialog.tsx")


def test_the_landing_dialog_stops_swallowing_an_unambiguous_403():
    """There are THREE sign-in surfaces, and step 1's copy has to reach all of
    them or the repair is partial.

    SignInDialog deliberately swallows a failed Asclepius attempt and falls
    through to the landing plane. That is correct for a 401 — both planes answer
    the same generic 401 whether the account is absent or the password wrong, so
    there is nothing to tell apart. A gated 403 is the opposite: it says this IS
    an Asclepius account and names its state. Swallowing that shows the
    physician the landing plane's error about a different account, and for the
    applicant who owes us an examination it discards the only sentence telling
    them what to do.
    """
    src = _SIGNIN_DIALOG_TSX.read_text(encoding="utf-8")
    assert "isAsclepiusGateError" in src
    assert "catch {" not in src.split("asclepiusLogin")[1][:600], (
        "the Asclepius attempt's catch must bind the error to inspect it"
    )


def test_the_401_is_still_swallowed():
    """The anti-enumeration fall-through is the reason that catch exists, and
    narrowing it must not become removing it."""
    src = _SIGNIN_DIALOG_TSX.read_text(encoding="utf-8")
    after = src.split("asclepiusLogin")[1][:1600]
    assert "login(trimmedEmail, password)" in src
    # Rethrow is CONDITIONAL — an unconditional throw would break every
    # tenant/landing account whose address is simply not an Asclepius one.
    assert "if (isAsclepiusGateError(ascErr)) throw ascErr;" in after


def test_the_login_error_carries_the_gate_not_just_a_sentence():
    """A plain Error() drops the status and the header, which is why the dialog
    could not tell the two cases apart in the first place."""
    src = _AUTH_API_TS.read_text(encoding="utf-8")
    assert "X-Asclepius-Auth-Gate" in src
    assert "AsclepiusLoginError" in src


def test_the_gate_header_is_readable_cross_origin():
    """The landing app is a different origin in production, so a header that is
    not in expose_headers is invisible to it however carefully the client asks.
    Without this the fix above is inert in prod and works only in dev."""
    import main as main_mod

    exposed = []
    for mw in main_mod.app.user_middleware:
        opts = getattr(mw, "kwargs", None) or getattr(mw, "options", {})
        if "expose_headers" in (opts or {}):
            exposed = opts["expose_headers"]
    assert asc_auth.AUTH_GATE_HEADER in exposed, (
        f"CORS must expose {asc_auth.AUTH_GATE_HEADER!r}; exposed={exposed}"
    )
