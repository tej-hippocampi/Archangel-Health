"""The landing's booking links, asserted at the source.

Source-level, following the pattern ``test_hs_signin_split.py`` sets: there is
no landing test harness in this repo and standing one up to read two constants
would cost more than it proves. What actually breaks here is a component growing
its own hardcoded Calendly URL again, or the two DIFFERENT accounts we ship
being quietly collapsed into one, and both are visible in the source.

The two-account fact is the reason this file exists. ``/partner`` books a call on
one founder's calendar and the team calculator books a demo on another's.
Merging them is a routing decision for the founders, and it must not happen as a
side effect of somebody tidying up an env var.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LANDING = ROOT / "landing" / "src" / "app"
CONFIG = LANDING / "config.ts"
PARTNER = LANDING / "components" / "PartnerInterest.tsx"
TEAM = LANDING / "components" / "TeamCalculator.tsx"

_CALENDLY = re.compile(r"https://calendly\.com/[^\s\"'`]+")


def _strip_comments(src: str) -> str:
    """Block and line comments out. Both files now EXPLAIN the two-account
    situation in a comment naming the URLs, and a test that greps for a Calendly
    link without this would read the explanation as the thing it forbids."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", src)


@pytest.fixture(scope="module")
def config() -> str:
    return CONFIG.read_text(encoding="utf-8")


def test_only_the_shared_config_holds_a_calendly_url(config):
    """One module owns the links. A component that keeps its own copy is the
    drift this change exists to end: the link changes, somebody greps, finds one
    copy, and the other keeps booking meetings nobody attends."""
    for name, path in (("PartnerInterest", PARTNER), ("TeamCalculator", TEAM)):
        src = _strip_comments(path.read_text(encoding="utf-8"))
        found = _CALENDLY.findall(src)
        assert not found, f"{name} hardcodes a Calendly URL again: {found}"
    assert len(_CALENDLY.findall(config)) == 2, (
        "config.ts should hold exactly the two fallback URLs")


def test_each_call_site_reads_its_own_config_value():
    """Two exports, not one. A single shared value would have silently retargeted
    one audience's meetings to the other founder's calendar on the deploy that
    introduced the env var."""
    partner = PARTNER.read_text(encoding="utf-8")
    team = TEAM.read_text(encoding="utf-8")
    assert "PARTNER_BOOKING_URL" in partner and "TEAM_INTRO_URL" not in partner
    assert "TEAM_INTRO_URL" in team and "PARTNER_BOOKING_URL" not in team


def test_the_fallbacks_are_the_urls_the_two_pages_shipped_with(config):
    """A build with neither env var set must behave exactly as the build before
    config.ts existed. These are the constants lifted out of the two components,
    verbatim; changing one here changes where real meetings land."""
    assert "https://calendly.com/aryaabhatia-berkeley/new-meeting?month=2026-03" in config
    assert "https://calendly.com/tejxpatel23/archangel-health-intro" in config


def test_both_env_vars_are_read_and_neither_shadows_the_other(config):
    """Two variables, each feeding one destination. One variable driving both is
    the consolidation the founders have not made yet."""
    assert "VITE_CALENDLY_URL" in config
    assert "VITE_CALENDLY_TEAM_URL" in config
    # Read straight off import.meta.env, per lib/auth-api.ts: Vite substitutes it
    # statically and indirection leaves the value undefined at runtime.
    assert "import.meta.env" in config


def test_the_deploy_documentation_names_both_variables():
    """An env var nobody knows to set is a constant with extra steps, and the
    person setting it is reading the deploy doc rather than this file."""
    for path in (ROOT / "landing" / "README.md",
                 ROOT / "docs" / "DEPLOYMENT_archangelhealth.ai.md"):
        text = path.read_text(encoding="utf-8")
        assert "VITE_CALENDLY_URL" in text, f"{path.name} does not name it"
        assert "VITE_CALENDLY_TEAM_URL" in text, f"{path.name} does not name it"
