"""What the health-system introduction email may and may not say.

The rendering gate for HS-REF. ``test_hs_referrals`` owns the flow, who gets
written to and when; this file owns the words, because the two failure modes
here are both silent: an email that quotes a figure we never agreed to pay, and
an email that cites research we were not confident in.
"""

from __future__ import annotations

import html as html_mod
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asclepius import hs_enrich  # noqa: E402
from onboarding_emails import (  # noqa: E402
    build_hs_referral_alert_email, build_hs_referral_intro_email, hs_referral_subject,
)

FACT = "Meridian announced an ambient-scribe rollout across four hospitals in July."


def _render(**over):
    kw = dict(
        contact_first_name="James", contact_role="Chief Operating Officer",
        hs_name="Meridian Health", referrer_name="Priya Patel",
        referrer_specialty="Nephrology", relationship="you were at college together",
        partner_url="https://archangelhealth.ai/partner?ref=PRIYA4K&hs=tok123",
        enrichment_sentence="",
    )
    kw.update(over)
    return build_hs_referral_intro_email(**kw)


def _text(markup: str) -> str:
    """What the reader actually SEES: tags dropped, entities resolved,
    whitespace collapsed.

    Unescaping matters for both jobs this helper does. The copy is written with
    ``&rsquo;`` for its apostrophes, so a raw-substring search for "won't" finds
    nothing in a body that plainly contains it; and a ``%`` hidden as ``&#37;``
    would slip past the no-figure rule below if we only looked at source.
    """
    return re.sub(r"\s+", " ", html_mod.unescape(re.sub(r"<[^>]+>", " ", markup))).strip()


# ═══ The rule that outlived the redesign ══════════════════════════════════════
@pytest.mark.parametrize("kw", [
    {},
    {"enrichment_sentence": FACT},
    {"referrer_name": "", "referrer_specialty": ""},
    {"contact_role": ""},
])
def test_no_figure_for_an_institutional_introduction_ever_reaches_the_body(kw):
    """The card that links to this email prints no dollar figure and no
    percentage, because institutional terms are negotiated one deal at a time
    and a number stated first becomes a promise the negotiation has to keep.

    The email is the other half of that surface and the same rule binds it.
    Asserted on the VISIBLE TEXT rather than the HTML, so a percent-encoded
    character in a URL cannot fail this for the wrong reason.
    """
    text = _text(_render(**kw))
    assert "$" not in text, text
    assert "%" not in text, text
    assert "percent" not in text.lower(), text


def test_the_exec_is_never_quoted_an_hourly_rate():
    """The physician invite sells hours: a rate, async work, your own schedule.
    Sent to a COO those sentences are aimed at the wrong person, and quoting a
    rate to the executive who would authorise their physicians' participation
    reads as having misjudged who we were writing to."""
    text = _text(_render()).lower()
    assert "per hour" not in text
    assert "/hour" not in text
    assert "hourly" not in text


#: The banned glyph, written as an escape so this file can assert on its
#: absence without containing one.
_EM_DASH = "\u2014"


def test_house_style_holds():
    assert _EM_DASH not in _text(_render())


def test_entities_are_rendered_not_printed():
    """``_eyebrow`` and ``_cta`` HTML-escape their arguments, so an "&middot;"
    written into one arrives at the reader as the literal seven characters.
    Caught in review once; asserted here so it cannot come back."""
    html = _render()
    assert "&amp;middot;" not in html
    assert "&amp;rarr;" not in html
    assert "·" in _text(html)
    assert "→" in _text(html)


# ═══ The referrer is the mechanism ════════════════════════════════════════════
def test_the_referrer_carries_the_subject_and_the_opening():
    html = _render()
    text = _text(html)
    assert hs_referral_subject("Priya Patel") == "Priya Patel suggested I reach out"
    assert "Priya Patel" in text
    assert "you were at college together" in text


def test_no_name_on_file_degrades_to_neutral_copy_and_leaks_no_address():
    """``referrer_display_name`` never falls back to the account email, because
    that string would be disclosed to a third party. The template has to hold up
    when it hands over an empty name."""
    html = _render(referrer_name="", referrer_specialty="")
    text = _text(html)
    assert hs_referral_subject("") == "An introduction to Archangel Health"
    assert "@" not in text
    assert "A physician we work with" in text


def test_the_recipient_is_always_given_a_way_out():
    """One spam complaint costs the sending domain that every other
    introduction rides on. Both the named and the neutral body carry the line."""
    for kw in ({}, {"referrer_name": "", "referrer_specialty": ""}):
        text = _text(_render(**kw)).lower()
        assert "ignore this" in text, kw
        assert "won’t follow up" in text or "won't follow up" in text, kw


def test_the_cta_carries_both_attribution_parameters():
    html = _render()
    assert 'href="https://archangelhealth.ai/partner?ref=PRIYA4K&amp;hs=tok123"' in html


# ═══ The personalization gate ═════════════════════════════════════════════════
def test_a_confident_fact_appears_verbatim_and_only_once():
    text = _text(_render(enrichment_sentence=FACT))
    assert "ambient-scribe" in text
    assert text.count("ambient-scribe") == 1


def test_an_empty_sentence_leaves_no_hole_in_the_body():
    """The clean body is a whole email, not the personalized one with a gap."""
    text = _text(_render(enrichment_sentence=""))
    assert "ambient-scribe" not in text
    assert "Two ways health systems work with us." in text
    assert "License de-identified records." in text


@pytest.mark.parametrize("data,why", [
    (None, "no enrichment at all"),
    ({"confidence": "low", "org_confirmed": True, "one_public_fact": FACT,
      "source_url": "https://x", "do_not_contact": False}, "low confidence"),
    ({"confidence": "high", "org_confirmed": False, "one_public_fact": FACT,
      "source_url": "https://x", "do_not_contact": False}, "organization unconfirmed"),
    ({"confidence": "high", "org_confirmed": True, "one_public_fact": "",
      "source_url": "https://x", "do_not_contact": False}, "no fact"),
    ({"confidence": "high", "org_confirmed": True, "one_public_fact": FACT,
      "source_url": "", "do_not_contact": False}, "fact with no source"),
    ({"confidence": "high", "org_confirmed": True, "one_public_fact": FACT,
      "source_url": "https://x", "do_not_contact": True}, "flagged do-not-contact"),
])
def test_the_gate_refuses_every_shaky_finding(data, why):
    """``may_personalize`` is what stands between a stranger and a confidently
    wrong sentence about their own organization. Every condition has to hold."""
    assert hs_enrich.may_personalize(data) is False, why


def test_the_gate_passes_only_a_complete_confident_finding():
    assert hs_enrich.may_personalize({
        "confidence": "high", "org_confirmed": True, "one_public_fact": FACT,
        "source_url": "https://example.org/news", "do_not_contact": False}) is True


# ═══ The founder alert ════════════════════════════════════════════════════════
def test_the_alert_says_who_was_written_to_and_what_they_saw():
    html = build_hs_referral_alert_email(
        referrer_name="Priya Patel", referrer_email="priya@ucsf.edu",
        contact_name="James Okoye", contact_email="j.okoye@meridianhealth.org",
        contact_role="COO", hs_name="Meridian Health",
        relationship="college friends", note="They run four hospitals.",
        enrich_state="ok", enrich_summary="confidence high",
        outcome="personalized introduction (delivered)")
    text = _text(html)
    for expected in ("James Okoye", "j.okoye@meridianhealth.org", "Meridian Health",
                     "Priya Patel", "priya@ucsf.edu", "college friends",
                     "They run four hospitals.", "personalized introduction"):
        assert expected in text, expected


def test_untrusted_input_cannot_inject_markup_into_the_alert():
    html = build_hs_referral_alert_email(
        referrer_name="<script>alert(1)</script>", referrer_email="a@b.com",
        contact_name="<img src=x onerror=alert(1)>", contact_email="c@d.com",
        contact_role="", hs_name="Evil & Co", relationship="x",
        note="<b>bold</b>", enrich_state="skipped", enrich_summary="", outcome="nothing")
    # The threat is a live tag, not the substring. ``onerror=`` survives inside
    # the escaped text ``&lt;img src=x onerror=alert(1)&gt;``, which renders as
    # visible characters and executes nothing, so assert on the tags.
    assert "<script>" not in html
    assert "<img" not in html
    assert "<b>bold</b>" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html
    # The ampersand in a plain organization name still has to survive as one.
    assert "Evil & Co" in _text(html)
