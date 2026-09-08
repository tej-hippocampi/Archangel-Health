"""The view-only marker in the side rail, and the guard that would have caught it.

An applicant's rail marks Community, Referral and Earnings as view-only. That
marker shipped as `.asc-rail-badge-viewonly` with NO RULE ANYWHERE, so it fell
back to `.asc-rail-badge`: the community unread-count pill, sized for "99+",
`flex: none` so it cannot shrink, and lime-washed against its own orange dot.
The expanded rail has about 140px for label plus marker; the pill took roughly
78 and "COMMUNITY" needs about 76, so the widest label in the rail painted
underneath it. On the mobile tab bar the count-hiding rule was never applied to
it either, so the words "View only" floated loose over three of five tabs.

No test could see any of it. Source assertions checked that the element was
built and that it carried the sentence; nothing checked that the class it named
had a rule. So the last test here is the general one: every `asc-` class the
portal shell emits must exist in the stylesheet. It is the check that turns this
whole family of defect from invisible into a red build.
"""

from __future__ import annotations

import pathlib
import re

_FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
_JS = (_FRONTEND / "asclepius.js").read_text(encoding="utf-8")
_CSS = (_FRONTEND / "asclepius.css").read_text(encoding="utf-8")
_BASE = (_FRONTEND / "_base.css").read_text(encoding="utf-8")
_TOKENS = (_FRONTEND / "_tokens.css").read_text(encoding="utf-8")
_ALL_CSS = _CSS + _BASE + _TOKENS

#: Classes that ride a styled base class as a hook or a modifier, and correctly
#: have no rule of their own. Each is listed with the base it rides, because an
#: allowlist without a reason is a place to hide the next bug.
_RIDES_A_STYLED_BASE = {
    # querySelector hooks + modifiers on `call-team-popup` / `call-team-overlay`
    "asc-case-popup",
    "asc-tour-interstitial",
    # inline-styled one-offs inside the evaluation workspace
    "asc-extra-anchor",
    "asc-extra-anchors",
    "asc-staged",
}


def _view_only_fn() -> str:
    """Just `viewOnlyBadgeEl`, bounded at the next function.

    A fixed-width slice runs straight into `communityBadgeEl`, which builds the
    unread-count pill and legitimately names every class this suite is checking
    the view-only marker has stopped using.
    """
    start = _JS.index("function viewOnlyBadgeEl")
    nxt = _JS.index("\n  function ", start + 1)
    return _JS[start:nxt]


def _rule_exists(cls: str) -> bool:
    """A class has a rule if the stylesheet names it as a selector token.

    Matched on a word boundary so `.asc-rail-badge` is not credited for
    `.asc-rail-badge-viewonly`, which is the precise substitution that let the
    original defect through a looser check.
    """
    return re.search(r"\." + re.escape(cls) + r"(?![\w-])", _ALL_CSS) is not None


# ── The marker itself ───────────────────────────────────────────────────────

def test_the_view_only_marker_does_not_ride_the_unread_count_pill():
    """.asc-rail-badge is sized for a two-digit count and cannot shrink, so
    anything inheriting it crowds the label out of the rail."""
    fn = _view_only_fn()
    assert "asc-rail-viewonly" in fn
    assert "asc-rail-badge" not in fn


def test_the_view_only_marker_has_a_rule_of_its_own():
    assert _rule_exists("asc-rail-viewonly")


def test_the_view_only_marker_carries_no_chip_background():
    """It sits beside a label competing for the same 140px. A background and a
    border are what made it read as a button rather than as a state."""
    block = re.search(r"\.asc-rail-viewonly\s*\{([^}]*)\}", _CSS)
    assert block, "the base rule is missing"
    body = block.group(1)
    assert "background" not in body
    assert "border" not in body


def test_the_marker_keeps_its_sentence_for_the_collapsed_rail():
    """Three rails hide the label. The full sentence has to be on the element,
    because the visible glyph says 'locked' and not 'until when'."""
    fn = _view_only_fn()
    assert "View only until your application is approved" in fn
    assert "aria-label" in fn and "title" in fn


def test_the_marker_is_placed_in_every_narrow_rail():
    """There are three, not one: the compact rail, the width-only collapse
    between 701 and 1100px, and the mobile tab bar. A fix applied to one of
    them regresses on the other two, which has happened here before."""
    placements = re.findall(r"\.asc-rail-viewonly\s*\{[^}]*position:\s*absolute", _CSS)
    assert len(placements) >= 3, (
        f"only {len(placements)} narrow-rail placements; the compact rail, the "
        "701-1100px collapse and the mobile tab bar each need one"
    )


def test_the_mobile_tab_bar_does_not_leave_words_floating():
    """The old chip put the literal string 'View only' over the tab bar because
    the count-hiding rule was never applied to it. The marker carries no text
    at all now, which is what makes that unrepeatable."""
    fn = _view_only_fn()
    assert "asc-rail-badge-n" not in fn


# ── The label the marker was crowding ───────────────────────────────────────

def test_the_rail_label_actually_clips():
    """min-width:0 lets the box shrink; without overflow nothing clips the
    text, so a long label paints over whatever sits to its right. Both halves
    are needed and only one was there."""
    block = re.search(r"\.asc-rail-label\s*\{([^}]*)\}", _CSS)
    assert block, "the label rule is missing"
    body = block.group(1)
    for prop in ("overflow", "text-overflow", "white-space"):
        assert prop in body, f".asc-rail-label is missing {prop}"


# ── The general guard ───────────────────────────────────────────────────────

def test_every_class_the_portal_shell_emits_exists_in_the_stylesheet():
    """The check that was missing. A class with no rule does not fail loudly,
    it renders as unstyled text in the middle of a working screen, and every
    source assertion still passes because the element is built exactly as the
    code says.
    """
    used = set()
    for match in re.finditer(r"class:\s*'([^']+)'", _JS):
        for cls in match.group(1).split():
            if cls.startswith("asc-"):
                used.add(cls)
    assert used, "extraction found no classes, so the harness is broken and not the code"

    missing = sorted(c for c in used - _RIDES_A_STYLED_BASE if not _rule_exists(c))
    assert not missing, f"classes with no rule in the portal stylesheets: {missing}"


def test_the_allowlist_does_not_outlive_its_entries():
    """An allowlist that keeps naming classes the code stopped using is an
    allowlist nobody rereads, and it will eventually excuse a real orphan."""
    stale = sorted(c for c in _RIDES_A_STYLED_BASE if f"'{c}'" not in _JS and f" {c}'" not in _JS)
    assert not stale, f"allowlisted classes no longer emitted by asclepius.js: {stale}"
