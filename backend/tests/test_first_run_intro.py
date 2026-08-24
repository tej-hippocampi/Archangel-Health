"""The first-run intro, and the one claim in it we must not make.

A physician arriving at the portal has signed up for something they read about
on a landing page and has never seen the product. The tour that existed opened
with "One practice case, about 4 minutes", which explains the exercise but not
the job.

Source assertions, the same convention as test_pending_verification_surface:
there is no DOM harness here, and the failure being guarded is "nobody wired it
up".
"""

from __future__ import annotations

import re
from pathlib import Path

_PORTAL_JS = Path(__file__).resolve().parents[2] / "frontend" / "asclepius" / "asclepius.js"
_PORTAL_CSS = Path(__file__).resolve().parents[2] / "frontend" / "asclepius" / "asclepius.css"


def _src() -> str:
    return _PORTAL_JS.read_text(encoding="utf-8")


def test_the_intro_exists_and_runs_before_the_case():
    src = _src()
    assert "function renderFirstRunIntro" in src
    assert "renderFirstRunIntro(renderTourWelcome);" in src, (
        "the intro must hand control to the case welcome, or skipping it lands nowhere"
    )


def test_it_is_shown_once_and_never_on_a_replay():
    """Somebody replaying the tutorial is not seeing the product for the first
    time, and being re-explained it is irritating rather than helpful."""
    src = _src()
    assert "FIRST_RUN_SEEN_KEY" in src
    assert "!firstRunAlreadySeen() && !(state.tutorial && state.tutorial.replay)" in src


def test_seen_state_denies_on_a_storage_failure():
    """Safari in private mode throws on localStorage. Failing closed there shows
    the intro again, which is mildly annoying; failing open would swallow it for
    a physician who has never seen it."""
    src = _src()
    i = src.index("function firstRunAlreadySeen")
    body = src[i:i + 240]
    assert "catch" in body and "return false" in body


def test_it_quotes_no_pay_rate():
    """Rates depend on tier, specialty and language, they live on the Earnings
    page, and a number invented for an onboarding slide is a number we would
    have to honour. The panels may say pay goes UP; they may not say by how
    much."""
    src = _src()
    start = src.index("const FIRST_RUN_PANELS")
    panels = src[start:src.index("const FIRST_RUN_SEEN_KEY")]
    assert not re.search(r"\$\s?\d", panels), "a dollar figure reached the intro copy"
    assert not re.search(r"\b\d+\s*(?:per|/)\s*(?:hour|hr|case)\b", panels, re.I)
    assert not re.search(r"\b\d{2,}\s*(?:dollars|usd)\b", panels, re.I)


def test_every_panel_is_complete():
    src = _src()
    start = src.index("const FIRST_RUN_PANELS")
    panels = src[start:src.index("const FIRST_RUN_SEEN_KEY")]
    # Four panels, each with all three fields. A panel missing a body renders an
    # empty paragraph rather than failing, which is the kind of thing that ships.
    assert panels.count("chrome:") == 4
    assert panels.count("title:") == 4
    assert panels.count("body:") == 4


def test_it_is_skippable():
    src = _src()
    i = src.index("function renderFirstRunIntro")
    body = src[i:i + 4000]
    assert "Skip the intro" in body


def test_the_styles_use_tokens_only():
    """The visual suite enforces this globally; assert it here too so a
    regression names this block rather than a line number."""
    css = _PORTAL_CSS.read_text(encoding="utf-8")
    start = css.index("/* ─── First-run intro")
    block = css[start:]
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", block), "raw hex in the first-run styles"
