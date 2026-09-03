"""PRD demo-day P1-3 a & c — two screens that told a user something false.

  a  A failed queue load fell through to the empty state, so a 403, a 500 and a
     dead backend all rendered "You are all set. No cases to grade right now."
     — a reassuring sentence, with a checkmark, that is not true.
  c  Brokering uploads still carried a live Promote button. The server refuses
     them correctly, but a design that relies only on the server refusing is a
     design where an operator keeps clicking, and the reason they eventually get
     is the wrong one.

Both are frontend-only, and both are the kind of defect that source-grepping is
genuinely the right tool for: the failure is not "the logic is wrong" but "the
branch does not exist". The earnings surface has a real DOM harness
(``test_payments_earnings_dom.py``) and its assertions live there.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
_PORTAL = _FRONTEND / "asclepius.js"
# The two screens below live in different bundles now that the admin console has
# its own page: the queue a physician sees is still the portal, and the promote
# list an operator works is the console.
_CONSOLE = _FRONTEND / "admin_shell.js"


def _code(path: Path) -> str:
    """The module with comments stripped, so a comment EXPLAINING a rule is never
    mistaken for the rule itself (same approach as the earnings DOM suite)."""
    source = path.read_text(encoding="utf-8")
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


# ─── a · a failed queue and an empty queue are different facts ────────────────
def test_a_failed_queue_load_renders_an_error_not_a_reassuring_zero():
    code = _code(_PORTAL)
    assert "renderDashboardError" in code, (
        "every non-401 failure fell through to renderDashboardEmpty — "
        "'You are all set. No cases to grade right now.' with a checkmark"
    )
    # The error branch must be reached BEFORE the empty branch, or the empty
    # state wins whenever the queue came back undefined.
    err = code.index("if (queueError)")
    empty = code.index("renderDashboardEmpty(specLabel)")
    assert err < empty, "the error state must take precedence over the empty state"


def test_the_empty_state_still_exists_for_an_actually_empty_queue():
    """The fix must not turn 'no cases today' into an error. Both states are real
    and they must stay distinguishable in the other direction too."""
    code = _code(_PORTAL)
    assert "function renderDashboardEmpty(" in code
    assert "You are all set" in code


def test_the_dashboard_error_shows_the_servers_own_reason():
    code = _code(_PORTAL)
    fn = code[code.index("function renderDashboardError("):]
    fn = fn[:fn.index("function renderDashboardEmpty(")]
    assert "err.detail" in fn and "err.message" in fn, (
        "a 403 here is a real answer about this account (no tier, wrong role); "
        "swallowing the server's sentence repeats the original defect"
    )
    assert "403" in fn, "a gated account is told something different from a broken one"


# ─── c · brokering never sits next to a Promote button ────────────────────────
def test_the_promote_list_has_no_live_button_for_brokering():
    code = _code(_CONSOLE)
    block = code[code.index("function renderPromoteList("):]
    block = block[:block.index("async function openPromoteReview(")]
    assert "promote_block" in block, (
        "the surface must read the server's verdict rather than re-deriving it"
    )
    assert "'brokering'" in block, (
        "a brokering upload showed a live Promote button; the server refused it "
        "correctly and the admin was given the wrong reason"
    )
    # The click handler is attached in exactly one branch — the unblocked one.
    assert block.count("openPromoteReview(u, st)") == 1


def test_a_blocked_promote_button_states_its_reason_in_the_page():
    """A disabled button with only a tooltip is a dead end for anyone who does
    not hover it — which is how the specialty 409 stayed unresolvable."""
    code = _code(_CONSOLE)
    block = code[code.index("function renderPromoteList("):]
    block = block[:block.index("async function openPromoteReview(")]
    assert "asc-promote-block" in block
    assert "specialtyResolver(u.upload_id" in block, (
        "where the reason is fixable, the control that fixes it belongs in the row"
    )


def test_the_promote_block_class_is_actually_styled():
    """A class with no rule renders as unstyled text in the middle of a table —
    the reason is present but reads as a rendering bug."""
    css = (_FRONTEND / "asclepius.css").read_text(encoding="utf-8")
    for cls in (".asc-promote-block", ".asc-promote-block-why", ".asc-spec-resolver",
                ".asc-spec-select", ".asc-login-notice", ".asc-wait-rows"):
        assert re.search(re.escape(cls) + r"[\s,{:.]", css), f"{cls} has no CSS rule"
