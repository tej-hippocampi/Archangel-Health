"""The applicant's one screen, and the landing screen that sends them to it.

Onboarding Master PRD, Phase 2 — PRD A §1.

What was here before: a six-stage hidden state machine whose first three stages
were full-screen interstitials, read once and then never again. An applicant's
second visit landed on a DIFFERENT screen from their first, decided by state
they could not see. The dashboard behind those three had one button whose label
changed four ways — Start / Continue practice case / Take my examination /
Resume my examination — so the same button meant four things depending on
something invisible. The examination, which is the entire point, sat behind all
of it and behind a practice case. The founders introduced themselves and offered
a Calendly on two of those screens, to someone we had not accepted yet.

The rule that replaced it: pre-approval the portal is ONE screen and it says one
thing — the last step of your application is the examination. Two cards. Card 1
is how to label and everything in it is quiet and optional. Card 2 carries the
only primary button on the page. The only state that changes anything is the
examination: not started, in progress, submitted.

Asserted against the shipped portal source, in the style the rest of the portal
suites use, plus the live server behaviour where a claim depends on it.
"""

from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient

from tests._asclepius import app, fresh_store, headers_for, make_user

_FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
_JS = (_FRONTEND / "asclepius.js").read_text(encoding="utf-8")
_CSS = (_FRONTEND / "asclepius.css").read_text(encoding="utf-8")
_LANDING = pathlib.Path(__file__).resolve().parents[2] / "landing" / "src"
_STEPS = (_LANDING / "app" / "components" / "onboarding" / "steps.tsx").read_text(encoding="utf-8")
_AUTH_API = (_LANDING / "lib" / "auth-api.ts").read_text(encoding="utf-8")


def _strip_js_comments(source: str) -> str:
    """This codebase explains its rules in prose beside the code, and a grep
    that reads the prose as code fails on its own documentation."""
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


def _fn(name: str) -> str:
    """One function's body, bounded at the next declaration.

    Bounded that way rather than by a character count on purpose: a fixed window
    runs past the end of a function into its neighbour, and then an assertion
    here is satisfied by code on a different screen and keeps passing after this
    one loses it.
    """
    start = _CODE.index("function " + name + "(")
    return _CODE[start:_CODE.index("\n  function ", start + 10)]


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


def _applicant(store, **kw):
    user = make_user(store, role="evaluator", tier=None, practice_case=False, **kw)
    store.set_verification_status(user["id"], "pending")
    return store.get_user_by_id(user["id"])


# ── One screen, whatever state they arrive in ───────────────────────────────

def test_there_is_one_pre_approval_screen_and_it_is_this_one():
    view = _fn("renderApplicantHome")
    assert "YOUR APPLICATION" in view
    assert "1 · HOW TO LABEL A CASE" in view
    assert "2 · TAKE THE EXAMINATION" in view


def test_no_stage_can_route_an_applicant_somewhere_else():
    """The interstitials are gone, and so is every branch that reached them.
    An applicant lands on renderApplicantHome or nowhere."""
    stage = _fn("credentialingStage")
    for gone in ("'welcome'", "'choice'", "'info'", "'resources'", "'waiting'",
                 "'practice_in_progress'", "'exam_ready'"):
        assert gone not in stage, f"credentialingStage still returns {gone}"


def test_a_legacy_applicant_and_a_new_one_get_the_same_screen(client):
    """MIGRATION BY IGNORING (PRD A §1.5). Somebody who stopped half way through
    the old journey carries welcome_seen_at, onboarding_choice and info_seen_at.
    Nothing reads them, so they land exactly where a fresh applicant does — with
    no backfill, no schema change and no stored blob rewritten."""
    store = fresh_store()
    legacy = _applicant(store)
    h = headers_for(legacy)
    blob = {}
    for action, extra in (("welcome_seen", {}),
                          ("onboarding_choice", {"choice": "email_me"}),
                          ("info_seen", {}), ("resources_seen", {})):
        res = client.patch("/api/asclepius/me/tutorial",
                           json={"action": action, **extra}, headers=h)
        assert res.status_code == 200, res.text
        blob = res.json()["tutorial"]

    assert blob["welcome_seen_at"], "the marks must still be written"
    assert blob.get("onboarding_choice") == "email_me"
    # And the client's only question of that blob is the examination.
    stage = _fn("credentialingStage")
    assert "exam.state" in stage
    for legacy_key in ("welcome_seen_at", "onboarding_choice",
                       "info_seen_at", "resources_seen_at"):
        assert legacy_key not in stage


# ── Card 2 is the only thing that changes ───────────────────────────────────

def test_card_two_has_a_variant_for_each_of_the_three_exam_states():
    view = _fn("renderApplicantHome")
    assert "exam_submitted" in view and "exam_in_progress" in view
    assert "'Resume'" in view and "'Start'" in view
    assert "Your examination is with us" in view


def test_a_filed_examination_removes_the_button_rather_than_disabling_it():
    """A disabled button still reads as an action somebody failed to take, on
    the one screen whose job is to say there is nothing left to do."""
    view = _fn("renderApplicantHome")
    assert "submitted ? null : h('button'" in view
    assert "disabled" not in view


def test_exactly_one_primary_button_on_the_page():
    """Card 1's items are quiet rows with an icon, a line and an arrow. A second
    button-shaped thing competes with the one that matters."""
    view = _fn("renderApplicantHome")
    assert view.count("asc-btn-primary") == 1


def test_nothing_else_on_the_page_changes_with_state():
    """The heading, the lede and card 1 are the same in all three states. The
    old screen changed its own copy by stage, which is what made a second visit
    unrecognisable."""
    view = _fn("renderApplicantHome")
    # Card 1: everything from its declaration to card 2's.
    card_one = view[view.index("const howToLabel"):view.index("const examBody")]
    for state_read in ("submitted", "resuming", "stage ==="):
        assert state_read not in card_one, f"card 1 branches on {state_read}"
    # The heading and the lede: from the setRoot to the grid.
    heading = view[view.index("setRoot("):view.index("asc-applicant-grid")]
    for state_read in ("submitted", "resuming", "stage ==="):
        assert state_read not in heading, f"the heading branches on {state_read}"


# ── Nothing pre-approval that belongs after approval ────────────────────────

def test_no_founders_no_calendly_no_mission_paragraph():
    view = _fn("renderApplicantHome")
    assert "founderStripEl" not in view
    assert "calendly" not in view.lower()
    assert "Book 20 minutes" not in view


def test_one_line_of_contact_replaces_them():
    view = _fn("renderApplicantHome")
    assert "Any questions: " in view
    assert "mailto:tejpatel@berkeley.edu" in view


def test_the_rail_sections_are_not_explained_in_the_application_copy():
    """Community, Referrals and Earnings stay view-only in the rail exactly as
    they were; the sentence about them moved out of the application copy, which
    is about the application."""
    view = _fn("renderApplicantHome")
    for gone in ("Community, Referral and", "look through, view only"):
        assert gone not in view


# ── The video, the guide, the practice case ─────────────────────────────────

def test_the_video_opens_in_place_using_the_existing_player():
    """The walkthrough's demo overlay already closes on ✕ and on Esc. Building a
    second player would be a second thing to keep working."""
    view = _fn("renderApplicantHome")
    assert "FirstRunWalkthrough.playDemo" in view
    assert "demoAvailable" in view, "a row that opens nothing is worse than no row"


def test_the_guide_opens_as_an_overlay_and_returns_here():
    guide = _fn("openGuideOverlay")
    assert "'Escape'" in guide, "Esc must close it"
    assert "aria-label': 'Close the guide'" in guide
    assert "role: 'dialog'" in guide and "'aria-modal': 'true'" in guide
    assert "opener.focus()" in guide, "focus must return to what opened it"
    # Built from the same manual data and the same section builder the full
    # Guide uses, so this content has one source.
    assert "guideSection(sec)" in guide
    assert "ASC_MANUALS" in guide


def test_the_practice_case_is_offered_and_never_required():
    view = _fn("renderApplicantHome")
    assert "Optional: try a practice case first" in view
    assert "startTutorial({ replay: false })" in view, "never replay: it clears the draft"


def test_pausing_the_examination_returns_to_this_screen():
    fn = _CODE[_CODE.index("function pauseExam"):][:500]
    assert "renderApplicantHome()" in fn
    assert "saveDraft()" in fn
    assert "clearDraft" not in fn, "pausing must not throw the answers away"


def test_an_applicant_mid_examination_is_never_told_to_do_a_practice_case():
    """The server exempts the examination from the practice gate, so this should
    not fire. If it ever does, the two worst things to do are both here: tell
    them to go and do a practice case they were told was optional, and then
    START one, which replaces the examination in the workspace and takes the
    case they are sitting away from them."""
    fn = _CODE[_CODE.index("function goToPracticeCase"):][:800]
    assert "examActive()" in fn
    guard = fn[fn.index("examActive()"):]
    assert "return;" in guard[:guard.index("startTutorial")], (
        "the examination branch must return before the tutorial is started")


# ── The landing screen that sends them here ─────────────────────────────────

_STEPS_CODE = _strip_js_comments(_STEPS)


def _success_screen(code: bool = False) -> str:
    """The success step. `code=True` strips the prose beside it, for the checks
    that assert copy is ABSENT — otherwise the comment explaining what was
    deleted satisfies a grep for the deleted thing."""
    src = _STEPS_CODE if code else _STEPS
    start = src.index("export function StepApplicationSubmitted")
    end = src.find("\nexport function ", start + 10)
    return src[start:end if end != -1 else len(src)]


def test_the_success_screen_names_the_examination_in_the_button():
    """The CTA label carries the instruction; the paragraph carries the why,
    once."""
    screen = _success_screen()
    assert "Open my account and take the examination" in screen
    assert "One step left: open your account and take the examination." in screen
    assert "one real case in your specialty" in screen
    assert "about 15 minutes" in screen


def test_the_success_screen_no_longer_argues_its_case_four_times():
    """Four paragraphs of philosophy, a founders' signature, and then a SECOND
    thank-you below a rule repeating the 24 to 48 hours the first had given."""
    screen = _success_screen(code=True)
    for gone in ("review human on purpose",
                 "only confirming that you are who you say you are",
                 "sign in with the password you just chose",
                 "Tej Patel &amp; Aryaa Bhatia",
                 "Or read our mission",
                 "There is a short onboarding inside it"):
        assert gone not in screen, f"deleted copy is back: {gone!r}"


def test_the_success_screen_still_says_the_work_is_saved():
    screen = _success_screen()
    assert "You can stop part way" in screen
    assert "emailed you a link" in screen


def test_the_cta_deep_links_to_the_examination():
    """So the button lands on the thing it named rather than the top of the
    page. A focus hint and never routing: the card is on the screen either way,
    so a fragment stripped by a proxy costs nothing."""
    screen = _success_screen()
    assert '"examination"' in screen
    assert "#examination" in screen, "the no-token fallback needs it too"
    # The shared redirect takes it as an OPTIONAL argument, so every other
    # caller's URL is byte-identical to what it was.
    assert "fragment?: string" in _AUTH_API
    assert "const hash = fragment ?" in _AUTH_API
    # And the portal reads it.
    assert "'#examination'" in _CODE
    assert "ascExamStart" in _CODE


# ── The server is untouched ─────────────────────────────────────────────────

def test_the_examination_endpoints_are_not_touched_by_this(client):
    """PRD A §1.6. The screen changed; the exam did not."""
    store = fresh_store()
    user = _applicant(store)
    res = client.get("/api/asclepius/exam/task", headers=headers_for(user))
    assert res.status_code == 200, res.text
    assert res.json()["task"]["task_id"].startswith("gold-")


def test_the_tutorial_stamping_endpoints_still_exist(client):
    """Kept as history (PRD A §1.5): the client stopped reading these fields, so
    removing the endpoints too would have broken an applicant sitting on an
    older cached page for no gain at all."""
    store = fresh_store()
    h = headers_for(_applicant(store))
    for action in ("welcome_seen", "info_seen", "resources_seen"):
        assert client.patch("/api/asclepius/me/tutorial",
                            json={"action": action}, headers=h).status_code == 200


# ── Hygiene ─────────────────────────────────────────────────────────────────

def test_the_applicant_css_sits_outside_every_foreign_media_block():
    """§2 invariant 7, and a lesson this codebase has already paid for: a rule
    nested inside somebody else's breakpoint applies only at that width, so the
    screen looks correct on the reviewer's monitor and unstyled on a laptop.

    Computed by brace depth rather than by reading, because that is the thing
    that actually goes wrong."""
    depth = 0
    open_media: list[int] = []
    for line in _CSS.split("\n"):
        if "@media" in line and "{" in line:
            open_media.append(depth)
        if ".asc-applicant-" in line and line.lstrip().startswith("."):
            assert not open_media or depth == 0 or line.strip().startswith(
                ".asc-applicant-grid"), (
                f"an applicant rule is nested inside a foreign block: {line.strip()}")
        before = depth
        depth += line.count("{") - line.count("}")
        if open_media and depth <= open_media[-1] and before > open_media[-1]:
            open_media.pop()
    assert depth == 0, "the stylesheet's braces do not balance"


def test_the_only_media_block_the_applicant_grid_opens_is_its_own():
    """One breakpoint, opened by an applicant selector and closed before the
    next rule."""
    block = _CSS[_CSS.index(".asc-applicant-grid {"):]
    block = block[:block.index(".asc-applicant-card {")]
    assert block.count("@media") == 1
    assert "@media (min-width: 900px) {" in block
    assert block.count("{") == block.count("}")
