"""Where /partner shows up for a physician who already knows a health system.

The Sep 1 meeting asked for the link on the onboarding pages and in the emails,
and the reason is narrower than "more links is better". A physician finishing
signup is the likeliest person in the funnel to think of a hospital they know,
and until this change that thought had nowhere to go: /partner lived only on the
referral tab, which a brand new account has not found yet.

The other half of the requirement is that it must not bloat anything. So these
tests pin the presence of the line AND its restraint: one sentence, never a
second button competing with the one that opens their workspace, and nothing at
all when there is no URL to point at.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import onboarding_emails as OE  # noqa: E402

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent
ONBOARDING_PY = BACKEND / "routers" / "onboarding.py"
STEPS_TSX = REPO / "landing" / "src" / "app" / "components" / "onboarding" / "steps.tsx"

PARTNER = "https://archangelhealth.ai/partner?ref=PRIYA4K"


def _complete_email(**overrides) -> str:
    kwargs = dict(
        email="dr@stmarys.org",
        full_name="Priya Raman",
        role_label="Nephrologist",
        org_name="St Mary's Health",
        specialty="Nephrology",
        workspace_url="https://app.example.com/asclepius",
        is_director=False,
    )
    kwargs.update(overrides)
    return OE.build_asclepius_complete_email(**kwargs)


# ─── The email ───────────────────────────────────────────────────────────────

def test_the_workspace_email_offers_the_partner_link():
    """This email is the one artifact of onboarding a physician keeps, and the
    meeting asked for the door to be in it. A link only on a page they visited
    once is a link they cannot find again."""
    html = _complete_email(partner_url=PARTNER)
    assert f'href="{PARTNER}"' in html
    assert "Know a health system" in html


def test_the_email_says_nothing_when_there_is_no_url_to_point_at():
    """A caller with no landing URL configured should send a complete email, not
    a dead link. Silence is the honest degradation for an optional offer."""
    html = _complete_email()
    assert "Know a health system" not in html
    assert "/partner" not in html
    # And the email is otherwise whole, so the absence costs nothing else.
    assert "Your workspace is ready." in html


def test_the_partner_link_is_a_sentence_and_not_a_second_button():
    """"Without bloating any email" is the requirement, and the failure mode is
    specific: these emails carry exactly one call to action, and a second pill
    beside it costs us the click that matters more, which is the one that opens
    their workspace."""
    html = _complete_email(partner_url=PARTNER)
    pills = re.findall(r"border-radius:999px", html)
    plain = re.findall(r"border-radius:999px", _complete_email())
    assert len(pills) == len(plain), "the partner link grew into a button"
    # One line, not a paragraph. The sentence lives in one <p>.
    assert html.count("Know a health system") == 1


def test_the_offer_survives_a_url_with_characters_that_need_escaping():
    """The attributed link carries query parameters, so an unescaped href here
    would break the markup of the one email a physician keeps."""
    html = _complete_email(partner_url="https://x.test/partner?ref=A&hs=tok")
    assert 'href="https://x.test/partner?ref=A&amp;hs=tok"' in html


# ─── The wiring ──────────────────────────────────────────────────────────────

def test_both_workspace_ready_senders_pass_a_partner_url():
    """A builder that grows an optional argument nobody passes ships looking
    finished and does nothing, which is exactly how the /partner link came to
    exist in only one place. Director and invited member both send this email
    and both have to carry the offer."""
    src = ONBOARDING_PY.read_text(encoding="utf-8")
    calls = src.count("build_asclepius_complete_email(")
    assert calls >= 2, "the workspace-ready senders moved"
    assert src.count("partner_url=_partner_intro_url(") == calls


def test_the_link_is_attributed_when_we_can_mint_a_code():
    """A physician who forwards an unattributed link never finds out it worked,
    and the introduction they made looks to us like inbound traffic."""
    from asclepius import referrals as asc_referrals

    attributed = asc_referrals.partner_url("PRIYA4K", None)
    assert attributed.endswith("/partner?ref=PRIYA4K")


def test_a_failed_code_lookup_still_yields_a_working_link():
    """This runs in the last request of a signup that has already provisioned an
    account. Losing a query parameter is not worth failing that request over, so
    the fallback has to be a usable link rather than an exception."""
    import routers.onboarding as ON

    class _Boom:
        def get_user_by_email(self, email):
            raise RuntimeError("store is down")

    class _State:
        asclepius_store = _Boom()

    class _App:
        state = _State()

    class _Request:
        app = _App()

    url = ON._partner_intro_url(_Request(), "dr@stmarys.org")
    assert url.endswith("/partner")
    assert "ref=" not in url


# ─── The onboarding screen ───────────────────────────────────────────────────

def test_the_success_screen_offers_the_same_door():
    """Source-level, following ``test_hs_signin_split``: there is no browser
    here, and what actually breaks is the line being dropped in a redesign of a
    screen nobody re-reads."""
    tsx = STEPS_TSX.read_text(encoding="utf-8")
    success = tsx.split("export function Step8AsclepiusSuccess", 1)[1] \
                 .split("export function ", 1)[0]
    assert 'href="/partner"' in success
    assert "Know a health system" in success


def test_the_screen_under_review_still_offers_only_one_link():
    """StepApplicationSubmitted deliberately offers a single link, because the
    honest answer to "what do I do now" while a person reviews your credentials
    is "nothing". A second door there would be this requirement bloating a
    screen it was never about."""
    tsx = STEPS_TSX.read_text(encoding="utf-8")
    submitted = tsx.split("export function StepApplicationSubmitted", 1)[1] \
                   .split("export function ", 1)[0]
    assert 'href="/partner"' not in submitted
