"""Two ways a launch morning silently loses a physician.

The first is volumetric. Signup completion sits behind a GLOBAL sliding window,
one bucket for the whole planet, and it was set to 300/hour. That is a number
below a launch: physician 301 in the hour is told we are receiving a lot of
requests, and the comment above that limiter already says what happens next, a
physician who cannot complete signup is gone for good. The tests here pin the
ceiling somewhere only a runaway script reaches, pin that it is movable from
the environment without a deploy, and pin that the two limits that ARE abuse
controls, per onboarding token and per IP, did not move with it.

The second is a dead end. An applicant finishes the form, and the practice case
is the entire justification for the pre-approval wait: it is real clinical
reasoning, the reviewer reads it, and a PROVISIONAL account is allowed to do
exactly that and little else. So the submitted email must carry a door back in,
and the success screen must spend the session the server already minted rather
than throw it away. Both halves are asserted on what the physician receives,
not on which function was called.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("ADMIN_AUTH_TOKEN", "test-admin-token")

import onboarding_emails as oe  # noqa: E402
import routers.onboarding as onboarding_module  # noqa: E402
from main import app  # noqa: E402

_STEPS_TSX = Path(__file__).resolve().parents[2] \
    / "landing/src/app/components/onboarding/steps.tsx"


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def sent(monkeypatch):
    """Record what would have gone out. These tests are ABOUT the email body."""
    outbox: list = []

    async def _send(to, subject, html_body, **kwargs):  # noqa: ANN001
        outbox.append({"to": to, "subject": subject, "html": html_body})
        return True

    monkeypatch.setattr(onboarding_module, "send_html_email", _send)
    monkeypatch.setattr(onboarding_module, "_email_configured", lambda: True)
    return outbox


CREDS_MINIMAL = {"fullLegalName": "Dr. Amara Okafor", "primarySpecialty": "Nephrology"}
ATTS = {
    "consentCredentialShare": True,
    "attestIndependentJudgment": True,
    "ipAssignment": True,
    "noPhi": True,
    "signedInitials": "AO",
}


def _submit_an_application(client: TestClient) -> str:
    """Walk the physician path to /finish. Returns the applicant's email."""
    ts = client.app.state.team_store
    invite = ts.create_health_system_invite(
        invite_base_url="http://localhost:5173", product="asclepius")
    token = invite["onboarding_url"].rsplit("/", 1)[-1]
    hs_id = invite["health_system_id"]
    email = f"dr_{uuid.uuid4().hex[:8]}@hospital.example.org"
    ts.update_health_system_director_identity(
        hs_id, first_name="Amara", last_name="Okafor", email=email)
    with sqlite3.connect(ts.db_path) as conn:
        conn.execute("UPDATE health_systems SET onboarding_step = 2 WHERE id = ?", (hs_id,))
        conn.commit()
    client.post("/api/onboarding/asclepius/credentials",
                json={"token": token, "credentials": CREDS_MINIMAL})
    client.post("/api/onboarding/asclepius/attestations",
                json={"token": token, "attestations": ATTS})
    r = client.post("/api/onboarding/asclepius/finish", json={"token": token})
    assert r.status_code == 200, r.text
    return email


# ═════════════════════════════════════════════════════════════════════════════
# the global signup ceiling
# ═════════════════════════════════════════════════════════════════════════════

def test_the_global_ceiling_is_above_a_real_launch_morning():
    """The backstop must be somewhere a script reaches and a crowd does not.

    A single shared bucket cannot tell thousands of invited physicians from a
    loop, so the only safe size is one that human demand cannot plausibly
    reach. 300/hour was not that, and a volumetric backstop that fires on
    ordinary demand is an outage rather than a backstop.
    """
    ceiling, window = onboarding_module._SIGNUP_GLOBAL
    assert window == 3600
    assert ceiling >= 2000, (
        "a launch morning is measured in hundreds to low thousands of signups; "
        f"a global ceiling of {ceiling}/hour is a wall across it")
    assert onboarding_module._SIGNUP_GLOBAL_DEFAULT >= 2000


def test_the_real_abuse_controls_did_not_move_with_it():
    """Raising the global bucket is only safe because it never was the guard.

    Per token and per IP are what actually stop a replayed link and a scripted
    signup, and they are unchanged. If this test fails alongside the one above,
    the ceiling was raised by loosening the wrong thing.
    """
    assert onboarding_module._SIGNUP_PER_TOKEN == (6, 3600)
    assert onboarding_module._SIGNUP_PER_IP == (20, 3600)
    # And the global one stays far looser than per-IP, so an ordinary crowd
    # arriving from many addresses can never hit the shared bucket first.
    assert onboarding_module._SIGNUP_GLOBAL[0] > onboarding_module._SIGNUP_PER_IP[0]


def test_the_ceiling_is_movable_from_the_environment(monkeypatch):
    """Mid-launch, the fix has to be a variable, not a deploy."""
    monkeypatch.setenv("ASCLEPIUS_SIGNUP_GLOBAL_PER_HOUR", "12000")
    assert onboarding_module._signup_global_per_hour() == 12000


@pytest.mark.parametrize("raw", ["", "   ", "lots", "0", "-1", "5_000_0.5"])
def test_a_bad_value_falls_back_instead_of_uncapping(monkeypatch, raw):
    """A typo in an env var must not leave an account-creating endpoint open.

    Every unusable spelling lands on the default, including a non-positive one:
    "0" reads like "off" to whoever types it, and "off" is not something this
    endpoint may be.
    """
    monkeypatch.setenv("ASCLEPIUS_SIGNUP_GLOBAL_PER_HOUR", raw)
    assert onboarding_module._signup_global_per_hour() \
        == onboarding_module._SIGNUP_GLOBAL_DEFAULT


def test_an_unset_variable_is_the_default(monkeypatch):
    monkeypatch.delenv("ASCLEPIUS_SIGNUP_GLOBAL_PER_HOUR", raising=False)
    assert onboarding_module._signup_global_per_hour() \
        == onboarding_module._SIGNUP_GLOBAL_DEFAULT


# ═════════════════════════════════════════════════════════════════════════════
# the way back to the practice case
# ═════════════════════════════════════════════════════════════════════════════

def test_the_submitted_email_carries_a_door_back_in(monkeypatch, client, sent):
    """It was the one email we send with no link in it at all.

    Asserted end to end from /finish rather than on the builder, because the
    bug was never in the copy: the caller had no URL to pass and the mail went
    out as a dead end.
    """
    monkeypatch.setenv("ASCLEPIUS_PORTAL_URL", "https://portal.example.test")
    email = _submit_an_application(client)
    body = next(m["html"] for m in sent
                if m["to"] == email and "application" in m["subject"].lower())
    assert "https://portal.example.test/asclepius" in body
    assert "practice case" in body.lower()
    # The reassurance is the reason the mail exists; the link is not allowed to
    # push it out.
    assert "24&ndash;48 hours" in body or "24–48 hours" in body
    assert "We keep review human on purpose" in body


def test_the_link_points_at_the_portal_not_the_api(monkeypatch):
    """In production the portal is a different host from the backend.

    Resolving this from BASE_URL alone mails a physician a link to the API. The
    approval welcome already resolves ASCLEPIUS_PORTAL_URL first, and these two
    mails open the same door, so they must agree.
    """
    monkeypatch.setenv("BASE_URL", "https://api.example.test")
    monkeypatch.setenv("ASCLEPIUS_PORTAL_URL", "https://portal.example.test")
    assert onboarding_module._asclepius_portal_url() \
        == "https://portal.example.test/asclepius"
    monkeypatch.delenv("ASCLEPIUS_PORTAL_URL", raising=False)
    assert onboarding_module._asclepius_portal_url() == "https://api.example.test/asclepius"


def test_the_builder_still_renders_without_a_url():
    """The preview and the older callers render copy without a hostname.

    Optional, not silently link-free by default: the caller that mails a real
    physician passes one, which is what the end-to-end test above pins.
    """
    html = oe.build_application_submitted_email(full_name="Amara Okafor")
    assert html.startswith("<!doctype html>")
    assert "We keep review human on purpose" in html


def test_the_success_screen_spends_the_session_instead_of_dropping_it():
    """/finish mints a REAL session for an applicant, and the screen threw it away.

    Read off the source of the component, the same way the wizard's step order
    is pinned, because what matters is that the token reaches the portal at all
    rather than which render produced the button.
    """
    src = _STEPS_TSX.read_text(encoding="utf-8")
    start = src.index("export function StepApplicationSubmitted")
    screen = src[start:src.index("export function", start + 1)]
    assert "data.asclepiusToken" in screen, \
        "the submitted screen ignores the session /finish minted for the applicant"
    assert "redirectToAsclepiusPortal" in screen, \
        "a token cannot cross origins in storage; it has to be traded for a handoff code"
    assert "practice case" in screen.lower()
    # The wait still has to be explained, not replaced by a task list.
    assert "24&ndash;48 hours" in screen
