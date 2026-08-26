"""The four-panel first-run intro is gone: first entry lands on the case.

Everything the intro said (what the work is, that it pays, that there is a
practice case) already lives on the landing page, in onboarding, and in the
Guide. The panels were extra clicks in front of the one thing that actually
teaches the product, the practice case, so they were removed. Source
assertions, same convention as the test they replace: there is no DOM harness
here, and the failure being guarded is "somebody wired it back up".
"""

from __future__ import annotations

from pathlib import Path

_PORTAL_JS = Path(__file__).resolve().parents[2] / "frontend" / "asclepius" / "asclepius.js"
_PORTAL_CSS = Path(__file__).resolve().parents[2] / "frontend" / "asclepius" / "asclepius.css"


def _src() -> str:
    return _PORTAL_JS.read_text(encoding="utf-8")


def test_the_intro_panels_are_gone():
    src = _src()
    assert "FIRST_RUN_PANELS" not in src
    assert "renderFirstRunIntro" not in src
    assert "FIRST_RUN_SEEN_KEY" not in src
    assert "asclepius_first_run_seen" not in src


def test_the_intro_css_is_gone():
    assert "asc-firstrun" not in _PORTAL_CSS.read_text(encoding="utf-8")


def test_first_entry_still_reaches_the_practice_case():
    """Removing the intro must not orphan the tutorial: the welcome interstitial
    still exists and the tutorial entry path still calls it."""
    src = _src()
    assert "function renderTourWelcome" in src
    assert "CALIBRATION CASE 1" in src
    assert "function startTutorial" in src


def test_the_welcome_no_longer_gates_on_a_seen_flag():
    src = _src()
    assert "firstRunAlreadySeen" not in src
