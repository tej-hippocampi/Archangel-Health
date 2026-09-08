"""What a physician meets between submitting an application and the examination.

Before any of this the answer was: nothing, then a case. Signing in dropped them
into Calibration Case 1, and the landing screen they had just left told them the
practice case was "the part of your application we read most closely" while the
portal, ninety seconds later, told them it was the examination.

The first fix for that was a journey: welcome, then a choice, then an explainer,
then the learning materials, then the examination. It removed the contradiction
and introduced a worse problem, which is what PRD A §1 is about. The first three
stages were full-screen interstitials read once and never again, so an
applicant's second visit landed on a different screen from their first for
reasons they could not see, and the dashboard's single button changed its own
label four ways depending on hidden state.

So the journey is gone and the portal is ONE screen with two boxes: how to label
a case, and take the examination (PRD A §1.2). What is asserted here now:

  * the three server marks still exist, are still written once, and still grant
    nothing — they are history, and PRD A §1.5 keeps them deliberately;
  * NOTHING READS THEM. That is the whole migration: an applicant carrying a
    full set of legacy marks and one carrying none land on the same screen, with
    no backfill and no blob rewritten;
  * the founders' strip and the Calendly invitation are NOT on a pre-approval
    screen. They belong to a physician we have accepted.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from fastapi.testclient import TestClient

from tests._asclepius import app, fresh_store, headers_for, make_user

from asclepius import auth as asc_auth

_FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
_JS = (_FRONTEND / "asclepius.js").read_text(encoding="utf-8")
_CSS = (_FRONTEND / "asclepius.css").read_text(encoding="utf-8")
_STEPS = (pathlib.Path(__file__).resolve().parents[2] / "landing" / "src" / "app"
          / "components" / "onboarding" / "steps.tsx").read_text(encoding="utf-8")


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _applicant(store):
    user = make_user(store, role="evaluator", practice_case=False)
    store.set_verification_status(user["id"], "pending")
    return user


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


def _stage_fn() -> str:
    start = _CODE.index("function credentialingStage()")
    return _CODE[start:_CODE.index("\n  function ", start + 10)]


def _applicant_home() -> str:
    """The body of renderApplicantHome, comments stripped.

    Bounded at the next declaration rather than by a character count: a fixed
    window runs past the end of the function into its neighbour, and then half
    the assertions here would be satisfied by copy on a different screen."""
    start = _CODE.index("function renderApplicantHome()")
    return _CODE[start:_CODE.index("\n  function ", start + 10)]


# ── The marks ───────────────────────────────────────────────────────────────

def test_each_mark_is_written_once_and_grants_nothing(client):
    store = fresh_store()
    user = _applicant(store)
    h = headers_for(user)

    first = client.patch("/api/asclepius/me/tutorial",
                         json={"action": "welcome_seen"}, headers=h)
    assert first.status_code == 200
    stamped = first.json()["tutorial"]["welcome_seen_at"]
    assert stamped

    again = client.patch("/api/asclepius/me/tutorial",
                         json={"action": "welcome_seen"}, headers=h)
    assert again.json()["tutorial"]["welcome_seen_at"] == stamped, "the mark moved"

    # Nothing about what they may DO changed.
    assert again.json()["access_level"] == "provisional"
    assert again.json()["surfaces"] == first.json()["surfaces"]
    assert again.json()["tier"] == first.json()["tier"], "the mark moved a tier"
    assert again.json()["capabilities"] == first.json()["capabilities"]


def test_the_choice_is_recorded_and_not_re_answerable(client):
    """It is a record of what they picked, not a setting. Letting the client
    rewrite it would bounce somebody between two screens; changing their mind
    is the dashboard's job and its `waiting` stage carries the way in."""
    store = fresh_store()
    h = headers_for(_applicant(store))
    client.patch("/api/asclepius/me/tutorial",
                 json={"action": "onboarding_choice", "choice": "email_me"}, headers=h)
    res = client.patch("/api/asclepius/me/tutorial",
                       json={"action": "onboarding_choice", "choice": "start_now"}, headers=h)
    assert res.json()["tutorial"]["onboarding_choice"] == "email_me"


def test_a_choice_the_server_has_never_heard_of_is_refused(client):
    store = fresh_store()
    h = headers_for(_applicant(store))
    res = client.patch("/api/asclepius/me/tutorial",
                       json={"action": "onboarding_choice", "choice": "call_me"}, headers=h)
    assert res.status_code == 422


def test_the_marks_carry_no_outcome_into_the_session(client):
    """Same rule the examination is held to: this payload decides which screen
    to paint and must never be able to say how somebody did."""
    store = fresh_store()
    h = headers_for(_applicant(store))
    body = client.patch("/api/asclepius/me/tutorial",
                        json={"action": "welcome_seen"}, headers=h).json()
    blob = json.dumps(body["tutorial"])
    for leak in ("score", "passed", "grade", "matched"):
        assert leak not in blob


def test_an_account_that_predates_the_journey_reads_as_not_done():
    """Absent means not-done. No backfill, no migration, and a blob written
    before these keys existed is silently correct."""
    legacy = json.dumps({"status": "completed", "version": 1,
                         "gate": {"state": "passed", "attempts": 1}})
    parsed = asc_auth._parse_tutorial(legacy)
    assert parsed["welcome_seen_at"] is None
    assert parsed["onboarding_choice"] is None
    assert parsed["info_seen_at"] is None
    assert parsed["gate_state"] == "passed", "the existing state must survive"


# ── Where the stages sit ────────────────────────────────────────────────────

def test_the_stage_helper_reads_only_the_examination():
    """THE MIGRATION, and it is a deletion rather than a backfill.

    Every legacy mark is still written and still returned; none is consulted.
    So an applicant who stopped half way through the old journey and one who
    signed up this morning get the same screen, and no stored blob had to be
    touched to make that true."""
    fn = _stage_fn()
    for gone in ("welcome_seen_at", "onboarding_choice", "info_seen_at",
                 "resources_seen_at", "t.status"):
        assert gone not in fn, f"credentialingStage still routes on {gone}"
    for kept in ("exam.state === 'submitted'", "exam.state === 'in_progress'",
                 "exam_not_started"):
        assert kept in fn, f"the examination state {kept} is missing"


def test_the_interstitials_are_gone():
    """Three full-screen screens, each read once and then never again, in front
    of the one thing an applicant is here to do."""
    for gone in ("renderProvisionalWelcome", "renderOnboardingChoice",
                 "renderOnboardingInfo", "renderCredentialingResources",
                 "renderCredentialingDashboard", "startCredentialing",
                 "stampCredentialing"):
        assert gone not in _CODE, f"{gone} is still in the portal"


def test_the_legacy_marks_are_still_written_and_still_grant_nothing(client):
    """Kept as history, per PRD A §1.5. Removing the endpoints would break an
    applicant mid-flight on an older cached page for no gain, and the marks are
    a record of what somebody was shown."""
    store = fresh_store()
    h = headers_for(_applicant(store))
    res = client.patch("/api/asclepius/me/tutorial",
                       json={"action": "welcome_seen"}, headers=h)
    assert res.status_code == 200
    assert res.json()["tutorial"]["welcome_seen_at"]
    assert res.json()["access_level"] == "provisional"


def test_the_examination_is_reachable_without_the_optional_material():
    """Both items in card 1 are optional and the examination is not, so the
    button that skips them has to actually exist or the sentence is a lie."""
    home = _applicant_home()
    assert "startExam" in home
    assert "the examination" in home


def test_the_practice_case_is_a_link_and_never_a_stage():
    """It stays because some applicants want it. It is not a gate, it is not a
    stage, and pre-approval it blocks nothing — the server exempts the
    examination from the practice gate for exactly this reason."""
    home = _applicant_home()
    assert "asc-btn-link" in home, "the practice case is offered as a button"
    assert "startTutorial({ replay: false })" in home


# ── The funnel stops contradicting itself ───────────────────────────────────

def test_the_landing_screen_no_longer_calls_the_practice_case_what_we_read():
    """It is the examination, and the portal says so a minute later."""
    submitted = _STEPS[_STEPS.index("export function StepApplicationSubmitted"):][:4200]
    assert "we read most closely" not in submitted
    assert "Start my practice case" not in submitted


def test_the_founders_do_not_introduce_themselves_before_we_have_accepted_anyone():
    """INVERTED BY PRD A §1.2, deliberately. The strip and "Book 20 minutes with
    us" used to render on two pre-approval screens. Both are said to a physician
    we have ACCEPTED; saying them to somebody still waiting to hear whether we
    will is a different sentence. One line of contact replaces them."""
    home = _applicant_home()
    assert "founderStripEl" not in home
    assert "FOUNDER_CALENDLY" not in home
    assert "calendly" not in home.lower()
    assert "mailto:tejpatel@berkeley.edu" in home


def test_the_founder_strip_is_kept_for_the_post_approval_welcome():
    """Kept rather than deleted, per PRD A §1.4.2: this is the strip the
    post-approval welcome renders, and FOUNDER_CALENDLY is additionally bound to
    a backend constant by test_landing_config."""
    assert "function founderStripEl" in _JS
    assert "FOUNDER_CALENDLY" in _JS


def test_the_founder_strip_survives_a_missing_photo():
    """The photo is deliberately not in git, so the strip has to work without
    it: no broken-image glyph, no gap where a face should be."""
    fn = _JS[_JS.index("function founderStripEl"):][:1600]
    assert "addEventListener('error'" in fn
    assert "removeChild" in fn


def test_every_class_the_journey_emits_has_a_rule():
    """The view-only chip shipped with no rule and rendered as raw text in the
    middle of the rail. These screens are new surface with the same exposure."""
    for cls in ("asc-founders", "asc-founders-photo", "asc-founders-mission",
                "asc-founders-sign", "asc-applicant-grid", "asc-applicant-card",
                "asc-applicant-rows", "asc-applicant-row", "asc-applicant-row-icon",
                "asc-applicant-row-text", "asc-applicant-row-title",
                "asc-applicant-row-body", "asc-applicant-row-go",
                "asc-applicant-practice", "asc-applicant-done",
                "asc-applicant-tick", "asc-applicant-help",
                "asc-applicant-guide-overlay", "asc-applicant-guide-frame",
                "asc-applicant-guide-body", "asc-applicant-guide-close"):
        assert f".{cls}" in _CSS, f"{cls} has no rule"


def test_no_rule_survives_the_screen_that_emitted_it():
    """The other half of the same guard. A rule for a class nothing renders is
    a rule nobody can check, and these four belonged to the deleted screens."""
    for gone in ("asc-res-grid", "asc-res-card", "asc-info-rows", "asc-info-row"):
        assert f".{gone}" not in _CSS, f"{gone} outlived its screen"


# ── The founders, where a physician actually meets them ─────────────────────

def test_one_calendar_for_every_physician_facing_invitation():
    """This pointed at two different founders' calendars: a doctor invited to
    book from the approval email and the same doctor booking from the portal
    landed on different people. One audience having one conversation gets one
    link. The health-system pair stays separate on purpose, because which
    founder takes that call is a routing decision and not a tidiness one."""
    import onboarding_emails as oe

    first_run = (_FRONTEND / "first_run.js").read_text(encoding="utf-8")
    assert "aryaabhatia-berkeley" in oe.FOUNDER_INTRO_CALENDLY
    assert "aryaabhatia-berkeley" in first_run
    assert "tejpatel-berkeley" not in first_run
    assert "aryaabhatia-berkeley" in _JS, "the applicant dashboard books elsewhere"


def test_the_founder_signature_is_spelled_one_way():
    """Six sites said "&", three said "and", one said "Tej Patel & Aryaa
    Bhatia". A signature that changes between two emails on the same morning
    reads as two different senders."""
    import onboarding_emails as oe
    import inspect

    src = inspect.getsource(oe)
    for wrong in ("Tej &amp; Aryaa", "Tej & Aryaa", "Tej Patel &amp; Aryaa Bhatia"):
        assert wrong not in src, wrong
    assert "Tej and Aryaa" in src


def test_no_founder_image_is_committed():
    """Every consumer degrades deliberately when the file is missing (initials,
    or names alone, or no <img> at all), and each of those reads better than a
    grey rectangle where a face should be. So a blank placeholder would be
    worse than nothing, and a real one is a photo of real people on a public
    unauthenticated path, in permanent history."""
    assets = pathlib.Path(__file__).resolve().parents[1] / "assets"
    images = [p.name for p in assets.iterdir()
              if p.suffix.lower() in {".png", ".jpg", ".jpeg"}]
    assert not images, f"committed founder imagery: {images}"
    readme = (assets / "README.md").read_text(encoding="utf-8")
    for named in ("founders.jpg", "community-persona.png", "founders-wide.jpg"):
        assert named in readme, f"{named} is referenced by code but undocumented"
