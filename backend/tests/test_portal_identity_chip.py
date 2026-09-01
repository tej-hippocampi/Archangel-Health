"""One identity, one place, and a rail that lost a tab.

The portal carried the physician's identity twice on a wide screen: a header
chip (email + role word + specialty + its own Sign out) and a rail-foot chip
(avatar + name + specialty + its own Sign out). Two names, two specialties, two
sign-out buttons.

The rail foot won. Profile moved onto it and left the rail, which is what took
the rail from six tabs to five, and the specialty word left both.

Source and DOM assertions against the shipped file, same conventions as
test_portal_ux.py.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

_FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
JS = (_FRONTEND / "asclepius.js").read_text(encoding="utf-8")
CSS = (_FRONTEND / "asclepius.css").read_text(encoding="utf-8")
HTML = (_FRONTEND / "index.html").read_text(encoding="utf-8")
_DOM = str((pathlib.Path(__file__).parent / "_asclepius_dom.js").resolve())

_LINE_COMMENT = re.compile(r"(?m)^\s*//.*$")
_CODE = _LINE_COMMENT.sub("", JS)


def _extract_function(src: str, name: str) -> str:
    marker = f"function {name}("
    start = src.index(marker)
    if src[start - 6 : start] == "async ":
        start -= 6
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"unbalanced braces extracting {name}")


def _fn(name: str) -> str:
    return _extract_function(JS, name)


def _array_const(name: str) -> str:
    start = JS.index(f"const {name} = ")
    i = JS.index("[", start)
    depth = 0
    for j in range(i, len(JS)):
        if JS[j] in "([{":
            depth += 1
        elif JS[j] in ")]}":
            depth -= 1
            if depth == 0:
                return JS[start : j + 1] + ";"
    raise AssertionError(f"unbalanced brackets extracting {name}")


def _run_node(script: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed in this environment")
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\n{proc.stderr}\n{proc.stdout}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


_RAIL_PRELUDE = """
require(%(dom)r);
const state = { user: {} };
function sessionCan() { return true; }
function sessionHasSurface() { return true; }
function isAdvisor() { return state.user.account_kind === 'advisor'; }
function isReferralOnly() { return state.user.account_kind === 'referrer'; }
%(payload)s
function out(o) { console.log(JSON.stringify(o)); }
"""


def _rail_harness(body: str) -> dict:
    payload = "\n".join([_array_const("RAIL_ITEMS"), _fn("visibleRailItems")])
    return _run_node(_RAIL_PRELUDE % {"dom": _DOM, "payload": payload} + "\n" + body)


# ─── One identity ────────────────────────────────────────────────────────────
def test_the_shell_carries_the_physicians_identity_exactly_once():
    assert "ascUserBadge" not in HTML, "the header chip is back"
    assert "ascLogoutBtn" not in HTML, "the header's own sign out is back"
    assert "ascUserBadge" not in _CODE
    assert _CODE.count("class: 'asc-rail-foot'") == 1


def test_the_chip_does_not_print_the_specialty_at_the_physician():
    """A physician does not need their own specialty read back to them, and it
    was the second line of a chip that already had their name on it. The avatar
    keeps the specialty HUE, which is a colour rather than a label."""
    foot = _LINE_COMMENT.sub("", _fn("renderSidePanel"))
    assert "asc-rail-spec" not in foot, "the specialty line is back on the chip"
    assert "state.user.specialty" in foot, "the specialty hue should still tint the avatar"
    assert "specialtyDotColor" in foot
    # And the retired header chip's role word does not reappear anywhere.
    assert "asc-user-role" not in _CODE


def test_both_things_you_can_do_with_your_name_are_one_click():
    """Profile and Sign out are buttons on the chip, not entries behind a menu.

    A menu would have cost a click on sign-out, which matters most on a shared
    clinical workstation and to the person walking away from one in a hurry.
    """
    foot = _LINE_COMMENT.sub("", _fn("renderSidePanel"))
    assert "'Profile'" in foot
    assert "'Sign out'" in foot
    assert "setPanel('profile')" in foot
    assert "onClick: logout" in foot


def test_the_specialty_and_signout_styles_left_with_their_emitters():
    """The repo fails a build when a styled class is emitted by nothing."""
    for gone in (".asc-user-badge", ".asc-user-email", ".asc-user-role", ".asc-rail-spec"):
        assert gone not in CSS, gone
    for kept in (".asc-rail-foot", ".asc-rail-footlinks", ".asc-rail-avatar", ".asc-me-signout"):
        assert kept in CSS, kept


def test_the_foot_survives_every_rail_width():
    """It used to be display:none in the compact rail, at 701-1100px and on
    mobile, each time with a comment pointing at the header chip. That chip is
    gone, so hiding the foot would take Profile and Sign out with it."""
    assert ".asc-rail-foot { display: none; }" not in CSS
    assert "body.asc-rail-compact .asc-rail-foot { display: none" not in CSS
    # It collapses to the avatar instead of disappearing.
    assert "body.asc-rail-compact .asc-rail-usertext { display: none; }" in CSS


def test_sign_out_is_reachable_from_a_destination_not_only_from_chrome():
    """What makes collapsing the foot on a narrow screen safe."""
    prof = _LINE_COMMENT.sub("", _fn("renderProfileView"))
    assert "asc-me-signout" in prof
    assert "onClick: logout" in prof


# ─── The rail ────────────────────────────────────────────────────────────────
def test_the_rail_is_five_destinations_and_profile_is_not_one_of_them():
    out = _rail_harness("""
    state.user = { role: 'evaluator', capabilities: ['label'], surfaces: [] };
    out({ dests: visibleRailItems().map((i) => i.dest) });
    """)
    assert out["dests"] == ["tasks", "community", "referral", "earnings", "guide"]


def test_a_referral_only_account_keeps_its_one_destination():
    """Profile left this branch with the rail tab, not with their access: the
    chip is on every screen and opens it for them too."""
    out = _rail_harness("""
    state.user = { role: 'evaluator', account_kind: 'referrer',
                   capabilities: [], surfaces: [] };
    out({ dests: visibleRailItems().map((i) => i.dest) });
    """)
    assert out["dests"] == ["referral"]


def test_profile_is_still_routable_after_losing_its_tab():
    """setPanel validates against its own allowlist and never consulted
    RAIL_ITEMS, so removing the tab cannot break the route. Asserted rather
    than assumed, because that is exactly the kind of coupling that is easy to
    be wrong about."""
    set_panel = _LINE_COMMENT.sub("", _fn("setPanel"))
    assert "'profile'" in set_panel
    assert "RAIL_ITEMS" not in set_panel
    assert "renderProfileView" in set_panel


def test_every_rail_icon_belongs_to_a_rail_item():
    """The invariant whose absence shipped two bugs at once: RAIL_ICONS had no
    `profile` key, so Profile rendered an empty 20x20 icon box, and it had a
    `review` key with no matching item, which was simply dead."""
    icons_start = JS.index("const RAIL_ICONS = {")
    icons = JS[icons_start : JS.index("\n  };", icons_start)]
    icon_keys = set(re.findall(r"(?m)^\s{4}(\w+):", icons))
    item_dests = set(re.findall(r"\{ dest: '(\w+)'", _array_const("RAIL_ITEMS")))
    assert icon_keys == item_dests, (
        f"icons without an item: {icon_keys - item_dests}; "
        f"items without an icon: {item_dests - icon_keys}"
    )


def test_the_locked_hint_is_rendered_rather_than_merely_declared():
    """It was declared, propagated, and rendered nowhere. The only lock
    affordance was a bare middot, and "Opens when your credentials clear" is
    the single sentence a pending physician most needs."""
    panel = _LINE_COMMENT.sub("", _fn("renderSidePanel"))
    assert "lockedHint" in panel, "the hint is still write-only"
