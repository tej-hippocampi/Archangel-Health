"""The Intro Calls tab: where a founder marks the call and the product sends.

The backend can be perfect and the feature still not exist, because the person
who takes the call is a founder with a browser, not a curl command. These tests
hold the three things that decide whether the screen is safe to put in front of
them: it offers BOTH outcomes, it takes the transition table from the server
rather than re-deriving it, and every class it emits has a rule.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
_ADMIN_JS = _FRONTEND / "admin_physicians.js"


def _js() -> str:
    return _ADMIN_JS.read_text(encoding="utf-8")


def _intro_source() -> str:
    """Just the intro-call block, so an assertion cannot be satisfied by an
    unrelated part of a 2000-line file."""
    js = _js()
    start = js.index("function renderIntroTab(")
    end = js.index("function renderPendingTab(")
    return js[start:end]


def test_the_module_still_parses():
    """Every other assertion here reads source. A syntax error would satisfy all
    of them and render nothing at all."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed in this environment")
    proc = subprocess.run([node, "--check", str(_ADMIN_JS)],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr


def test_the_tab_lives_in_the_module_that_owns_the_funnel():
    """These people become the rows in Pending. A separate section would need
    the shell's tab list, which is in asclepius.js, and would split one operator
    loop across two screens."""
    js = _js()
    assert "'intro', 'Intro Calls'" in js
    assert "if (activeTab === 'intro')" in js


def test_the_row_offers_the_outcome_that_sends_nothing_too():
    """Offering only "held" makes a call nobody got round to marking
    indistinguishable from one that happened, which is how somebody who never
    joined receives "great speaking with you"."""
    src = _intro_source()
    assert "held: 'Mark held'" in src
    assert "no_show: 'No show'" in src
    assert "cancelled: 'Cancelled'" in src


def test_the_buttons_come_from_the_server_not_from_a_second_copy_of_the_rules():
    """Two surfaces that each decide whether a no-show can still be marked held
    will eventually disagree, and the one that is wrong is the one with a
    button."""
    src = _intro_source()
    assert "r.available_outcomes" in src
    # No client-side transition table: the only status strings here are for
    # colour and copy, never for deciding what may be pressed.
    assert "=== 'scheduled'" not in src
    assert "=== 'no_show'" not in src


def test_logging_a_call_and_marking_it_held_are_different_endpoints():
    """Logging must never send. They are separate calls so that pressing the
    first one cannot mail anybody."""
    src = _intro_source()
    assert "api('/admin/intro-meetings'," in src
    assert "'/admin/intro-meetings/'" in src
    assert "'/outcome'" in src
    assert "Logging a call sends nothing" in src


def test_the_send_button_says_what_it_sends():
    """A button that mails a physician should say so before it is pressed, not
    in the toast afterwards."""
    src = _intro_source()
    assert "Sends their application link and the one-pager." in src
    assert "Records the outcome. Sends nothing." in src


def test_the_booking_link_is_read_from_the_payload():
    """The product owns the booking link now. A console that hardcoded its own
    would show the founders a link the emails no longer use."""
    src = _intro_source()
    assert "introCache || {}).booking_url" in src
    assert "calendly.com" not in src, "the console must not carry its own copy"


def test_every_class_the_tab_emits_is_styled():
    """A class with no rule renders as unstyled text in the middle of an admin
    table, and nothing errors to say so."""
    css = "\n".join(
        (_FRONTEND / name).read_text(encoding="utf-8")
        for name in ("asclepius.css", "admin.css", "_base.css", "_tokens.css"))
    src = _intro_source()
    emitted = set()
    for match in re.findall(r"class: '([^']+)'", src):
        for token in match.replace("+", " ").split():
            if token.startswith("asc-"):
                emitted.add(token)
    # The status-to-badge map is built as values rather than in a class literal.
    emitted.update(re.findall(r"'(asc-badge-[a-z]+)'", src))
    assert emitted, "the tab emitted no classes, so this test proved nothing"
    for cls in sorted(emitted):
        assert f".{cls}" in css, cls
