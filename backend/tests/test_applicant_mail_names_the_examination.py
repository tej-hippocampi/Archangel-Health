"""What the post-submit mail and screen tell an applicant about getting back in.

Onboarding Master PRD §3.2 step 4, against finding F2.

Two sentences were shipped that were not true. The submitted screen said "we've
emailed you a link back in" and the mail said "there is no password to remember
yet: signing in is a single-use link we email you" — but no sign-in link is
minted at finish, and screen 1 of the wizard has taken a password since v2 §2.
So the applicant was told to wait for a link that would never arrive, about an
account whose real credential they had already chosen and were being encouraged
to forget.

The mail also called the outstanding work "your practice case" when the thing we
read is the examination. Two messages naming different cases as the decisive one
is how somebody does neither properly.

Asserted on the RENDERED html rather than on the source, because the failure
being guarded is what a physician reads.
"""

from __future__ import annotations

import pathlib
import re

import onboarding_emails as oe

_STEPS_TSX = (pathlib.Path(__file__).resolve().parents[2] / "landing" / "src" / "app"
              / "components" / "onboarding" / "steps.tsx")

_PORTAL = "https://portal.example.test/asclepius"


def _submitted(portal_url: str = _PORTAL) -> str:
    return oe.build_application_submitted_email(
        full_name="Amara Okafor", portal_url=portal_url)


# ─── The promise that could not be kept ──────────────────────────────────────

def test_the_mail_no_longer_promises_a_link_instead_of_a_password():
    html = _submitted()
    assert "no password to remember" not in html.lower(), (
        "screen 1 takes a password; telling the physician there is none to "
        "remember teaches them to forget the only credential they have"
    )
    assert "single-use" not in html.lower()


def test_the_mail_says_how_to_actually_sign_in():
    html = _submitted()
    lower = html.lower()
    assert "password you chose" in lower
    assert "forgot your password" in lower, (
        "the recovery door has to be named, or a physician who forgets is back "
        "at the dead end this section exists to remove"
    )


def test_the_mail_names_the_examination_and_links_straight_at_it():
    html = _submitted()
    assert "Open my examination" in html
    assert "#examination" in html, "the CTA must focus the card, not the page"
    # It must not still be pointing at the practice case as the outstanding work.
    assert "Open my practice case" not in html


def test_the_practice_case_nudge_stops_calling_itself_the_examination_door():
    """The nudge IS the practice-case mail and still leads with it. What it may
    not do is send the physician to a link labelled as the thing we read."""
    html = oe.build_practice_case_nudge_email(
        first_name="Amara", portal_url=_PORTAL)
    assert "Open my practice case" not in html
    assert "Open my account" in html


# ─── _exam_url ───────────────────────────────────────────────────────────────

def test_exam_url_never_builds_a_url_no_router_resolves():
    assert oe._exam_url(_PORTAL) == _PORTAL + "#examination"
    # Idempotent, and it does not stack fragments.
    assert oe._exam_url(_PORTAL + "#examination") == _PORTAL + "#examination"
    assert oe._exam_url(_PORTAL + "#other") == _PORTAL + "#other"
    assert oe._exam_url(_PORTAL + "#examination").count("#") == 1


def test_exam_url_leaves_the_no_link_case_alone():
    """Callers treat an empty portal_url as 'render the copy without a link'.
    A bare '#examination' is not a destination and would render a dead button."""
    assert oe._exam_url("") == ""
    assert oe._exam_url(None) == ""
    assert oe._exam_url("   ") == ""


def test_the_builder_renders_without_a_url_and_shows_no_dead_cta():
    """The preview and the older callers pass no hostname."""
    html = oe.build_application_submitted_email(full_name="Amara Okafor")
    assert html.startswith("<!doctype html>")
    assert "Open my examination" not in html
    assert "#examination" not in html


# ─── The screen ──────────────────────────────────────────────────────────────

def test_the_success_screen_stops_promising_an_emailed_link():
    src = _STEPS_TSX.read_text(encoding="utf-8")
    start = src.index("export function StepApplicationSubmitted")
    screen = src[start:src.index("export function", start + 1)]
    # Strip comments FIRST. The prose recording why the sentence was removed
    # quotes the sentence, so a grep over raw source would fail on the very
    # change it is meant to confirm. JSX block comments span lines, so this is
    # a real strip rather than a per-line prefix test.
    rendered = re.sub(r"/\*.*?\*/", "", screen, flags=re.DOTALL)
    rendered = re.sub(r"//[^\n]*", "", rendered)
    # Whitespace-insensitive: the sentence was wrapped across two source lines.
    flat = " ".join(rendered.split())
    assert "link back in" not in flat, "the screen still promises an emailed link"
    assert "password you chose" in flat
    # And the comment really was the only place it survived, which is what makes
    # the strip above load-bearing rather than a way to hide a failure.
    assert "link back in" in " ".join(screen.split())


# ─── The comment that would mislead the next agent (F3, step 5) ──────────────

_WIZARD_TSX = (pathlib.Path(__file__).resolve().parents[2] / "landing" / "src" / "app"
               / "components" / "OnboardingWizard.tsx")


def test_the_wizard_no_longer_claims_the_physician_path_takes_no_password():
    """F3. Two comments in this tree described the same fact and disagreed:
    `orderFor` said the physician path takes no password, the sign-in page said
    screen 1 takes one. The second is true.

    A grep, because the failure mode is a HUMAN (or an agent) reading the stale
    note and building on it — which is how the passwordless assumption survived
    long enough to strand real accounts.
    """
    src = _WIZARD_TSX.read_text(encoding="utf-8")
    assert "NO PASSWORD STEP" not in src

    start = src.index("if (product === \"asclepius\") {")
    block = src[start:start + 2000]
    assert "Step1NameEmail" in block, (
        "the corrected comment has to name where the password IS taken, or it "
        "only removes a wrong answer without leaving a right one"
    )
    assert "legacy" in block.lower(), (
        "and it has to mark /auth/signin-link as legacy recovery, so the next "
        "reader does not wire it back into a current signup"
    )
