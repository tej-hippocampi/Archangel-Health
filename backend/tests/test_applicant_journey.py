"""What a physician meets between submitting an application and the examination.

Before this the answer was: nothing, then a case. Signing in dropped them into
Calibration Case 1, and the landing screen they had just left told them the
practice case was "the part of your application we read most closely" while the
portal, ninety seconds later, told them it was the examination. A funnel that
contradicts itself about the one thing it is asking for is worse than a silent
one.

The journey is welcome, then a choice, then the explainer, then the learning
materials, then the examination. Its state is three marks in ``tutorial_json``,
and the property that matters most is not what they do but WHERE THEY SIT in
``credentialingStage``: every new stage is tested below the existing ones, so an
applicant part way through today answers exactly as they answered yesterday and
never meets a screen that did not exist when they started. That ordering is the
whole migration, which is why it is asserted rather than assumed.
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


def _stage_fn() -> str:
    start = _JS.index("function credentialingStage()")
    return _JS[start:_JS.index("\n  /**", start)]


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

def test_the_new_stages_are_all_tested_after_the_old_ones():
    """THE MIGRATION. An applicant mid-application carries resources_seen_at or
    an exam state; if either new check ran first they would be thrown back to a
    welcome screen they never saw and had already passed."""
    fn = _stage_fn()
    last_old = max(fn.index("exam.state === 'submitted'"),
                   fn.index("t.resources_seen_at"),
                   fn.index("t.status === 'in_progress'"))
    for new in ("t.welcome_seen_at", "t.onboarding_choice", "t.info_seen_at"):
        assert fn.index(new) > last_old, f"{new} is consulted before an existing mark"


def test_choosing_to_wait_is_not_a_dead_end():
    """The commonest reason to pick it is not yet knowing the rest is fifteen
    minutes, so the screen that follows still has to offer the way in."""
    assert "waiting" in _stage_fn()
    dash = _JS[_JS.index("function renderCredentialingDashboard"):][:2200]
    assert "waiting: ['Start onboarding now'" in dash
    start = _JS[_JS.index("function startCredentialing"):][:900]
    assert "stage === 'waiting'" in start


def test_the_examination_is_reachable_without_the_optional_material():
    """Both resources say they are optional, and the button that skips them has
    to actually exist or the sentence is a lie."""
    res = _JS[_JS.index("function renderCredentialingResources"):][:2600]
    assert "Both are optional" in res
    assert "startExam" in res


def test_a_physician_can_get_back_to_the_explainer_from_the_materials():
    """They may reach the examination and find they wanted the explainer after
    all. Signing out is not a navigation model."""
    res = _JS[_JS.index("function renderCredentialingResources"):][:2600]
    assert "renderOnboardingInfo" in res


# ── The funnel stops contradicting itself ───────────────────────────────────

def test_the_landing_screen_no_longer_calls_the_practice_case_what_we_read():
    """It is the examination, and the portal says so a minute later."""
    submitted = _STEPS[_STEPS.index("export function StepApplicationSubmitted"):][:4200]
    assert "we read most closely" not in submitted
    assert "Start my practice case" not in submitted


def test_the_founders_appear_where_an_applicant_actually_waits():
    """The provisional dashboard is seen on every sign-in for one to two days
    and was entirely institutional voice."""
    assert "function founderStripEl" in _JS
    dash = _JS[_JS.index("function renderCredentialingDashboard"):]
    dash = dash[:dash.index("\n  async function renderDashboardView")]
    assert "founderStripEl" in dash


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
                "asc-founders-sign", "asc-info-rows", "asc-info-row"):
        assert f".{cls}" in _CSS, f"{cls} has no rule"
