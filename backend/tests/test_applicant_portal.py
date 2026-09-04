"""What an applicant sees in the portal while their credentials are checked.

The founders walked the product as a new physician and the portal answered
badly at every turn. Tasks and Dashboard were the same screen under two names.
The rail was four padlocks and one green tab, and the green one was Referral, so
the only thing they could act on was inviting other people. The dashboard ran a
queue fetch that 403s for them and painted the error. "Meet the community"
opened a tab that 403'd. And a six-stop welcome package walked them through
earnings and community rooms they cannot reach.

The rule that replaced all of it: an applicant SEES the whole product and ACTS
on one thing. Asserted here against the shipped portal source, in the style the
rest of the portal suites use.
"""

from __future__ import annotations

import pathlib
import re

import pytest

_FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
_JS = (_FRONTEND / "asclepius.js").read_text(encoding="utf-8")
_REFERRAL = (_FRONTEND / "referral.js").read_text(encoding="utf-8")
_CSS = (_FRONTEND / "asclepius.css").read_text(encoding="utf-8")


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


# ── One home, not two ───────────────────────────────────────────────────────

def test_the_header_no_longer_carries_a_second_dashboard():
    header = _CODE[_CODE.index("function renderHeader"):][:2000]
    assert "'Dashboard'" not in header, "the duplicate home tab is back"
    # Admin entries are untouched.
    assert "'Evaluate'" in header and "'Admin console'" in header


def test_tasks_works_from_inside_a_case():
    """The bug removing the duplicate exposed.

    A physician inside a case already has state.panel === 'tasks', so the
    "already here, do not refetch" early return made the rail's Tasks button do
    nothing. The header button had been masking it, so deleting that button
    would have left the browser's Back as the only way out of a case.
    """
    panel = _CODE[_CODE.index("function setPanel"):][:4000]
    guard = panel.index("if (dest === state.panel) return;")
    reset = panel.index("dest === 'tasks' && state.panel === 'tasks'")
    assert reset < guard, "the Tasks reset is behind the early return again"


# ── The rail ────────────────────────────────────────────────────────────────

def test_tasks_is_gated_on_the_practice_case_not_on_real_work():
    """It is the way to the only work an applicant is asked to do."""
    assert "{ dest: 'tasks',     label: 'Tasks', surface: 'tutorial' }" in _CODE


def test_referral_carries_no_resting_fill():
    """It was the one green tab beside four padlocks, so the rail pointed a
    waiting physician at inviting colleagues rather than at their application."""
    # Every rule naming referral, checked for a resting fill. Written this way
    # rather than against one selector because the fill lived in three rules
    # (base, :hover and the icon-only rail) and removing only the obvious one
    # would leave the tab green on hover.
    for rule in re.finditer(r"([^{}]*\.asc-rail-item-referral[^{}]*)\{([^}]*)\}", _CSS):
        selector, body = rule.group(1).strip(), rule.group(2)
        if ".active" in selector:
            continue          # the selected indicator, not a claim about the tab
        assert "background" not in body, f"{selector} still fills referral: {body}"
    # The ACTIVE green stays, and the icon-only rules that guard it stay too.
    assert ".asc-rail-item-referral.active" in _CSS


def test_the_look_only_tabs_are_chipped_and_tasks_is_not():
    assert "const VIEW_ONLY_DESTS = ['community', 'referral', 'earnings'];" in _CODE
    assert "function viewOnlyBadgeEl" in _CODE
    assert "'tasks'" not in _CODE[_CODE.index("VIEW_ONLY_DESTS"):][:120]


def test_the_chip_says_what_it_means_where_the_label_is_hidden():
    """The rail collapses to icons under 1100px and .asc-rail-label goes with
    it, so the full sentence has to be on the element itself."""
    badge = _CODE[_CODE.index("function viewOnlyBadgeEl"):][:700]
    assert "View only until your application is approved" in badge
    assert "aria-label" in badge and "title" in badge


# ── The dashboard ───────────────────────────────────────────────────────────

def test_an_applicant_never_reaches_the_queue_fetch():
    """It 403s for them, and the dashboard painted the error onto the one
    screen that is supposed to say their application is fine."""
    view = _CODE[_CODE.index("async function renderDashboardView"):][:1200]
    early = view.index("renderCredentialingDashboard()")
    fetch = view.index("/tasks/next") if "/tasks/next" in view else len(view)
    assert early < fetch


def test_the_welcome_package_waits_for_approval():
    mode = _CODE[_CODE.index("function firstRunMode"):][:600]
    assert "if (sessionIsProvisional()) return 'none';" in mode


def test_the_credentialing_dashboard_states_what_is_being_asked():
    dash = _CODE[_CODE.index("function renderCredentialingDashboard"):][:2500]
    assert "We are checking your credentials." in dash
    assert "practice case" in dash and "examination" in dash
    # Exactly one action.
    assert dash.count("asc-btn-primary") == 1


def test_the_stage_helper_can_never_reveal_a_grade():
    """It picks a button label. Whether a physician passed is the admin's call,
    and the session payload strips the score for the same reason."""
    stage = _CODE[_CODE.index("function credentialingStage"):][:900]
    for forbidden in ("score", "passed", "first_attempt_pass", "matched"):
        assert forbidden not in stage, f"credentialingStage reads {forbidden}"


# ── Community ───────────────────────────────────────────────────────────────

def test_an_applicant_is_sent_to_the_preview_not_to_a_403():
    """The dashboard used to offer "Meet the community" to somebody with no
    community_read, which opened a tab and failed there."""
    fn = _CODE[_CODE.index("function openCommunity"):][:900]
    assert "sessionHasSurface('community_read')" in fn
    assert "preview=1" in fn


def test_the_community_rail_item_no_longer_locks():
    assert "{ dest: 'community', label: 'Community', external: true }" in _CODE


# ── Referral ────────────────────────────────────────────────────────────────

def test_both_referral_halves_wait_for_approval():
    """Reverses an earlier decision, on the founders' instruction: an account
    nobody has checked sees the product and acts on none of it. An invitation
    carries our name and their claim to be one of our physicians, and that
    claim is the thing still being checked."""
    code = _strip_js_comments(_REFERRAL)
    assert "function physicianColLocked" in code
    assert code.count("hsUnlocked === false") == 2


def test_an_unknown_standing_does_not_lock_anybody_out():
    """`null` means the /auth/me read has not landed. Locking on that would
    take the page away from an approved physician over a slow request."""
    code = _strip_js_comments(_REFERRAL)
    assert "hsUnlocked = null" in code
    assert "hsUnlocked === false" in code and "hsUnlocked !== true" not in code
