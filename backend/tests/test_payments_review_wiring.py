"""PRD-P — the billable-session client is actually wired to the review surface
(audit U2).

The finding: ``earnings.js`` built ``window.AsclepiusSession`` — a complete,
correct, server-authoritative heartbeat client — and documented that the review
surface would call it. ``review.js`` never referenced it and ``review.html`` never
loaded it. A reviewer could work for an hour and be credited nothing, because
nothing was beating.

That is the same failure shape this repo has already paid for once: a feature
complete, correct and INVISIBLE for a whole build round because nothing mounted
it, and the failure was silent. Here the silence costs $100 a session.

**Ownership note, deliberately loud.** ``review.html`` and ``review.js`` belong to
Agent R under the release's write allowlist, not to Agent P. These tests and the
change they pin are the payments half of a seam that spans both — the client is
P's, the surface is R's — and they exist so that if R rewrites ``review.js`` (the
PRD says R rewrites it), the reviewer's clock cannot silently disappear again. If
one of these fails after an R merge, the fix is to re-wire, never to delete the
test.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
_DOM_SHIM = Path(__file__).resolve().parent / "_asclepius_dom.js"
_REVIEW_HTML = _FRONTEND / "review.html"
_REVIEW_JS = _FRONTEND / "review.js"
_EARNINGS_JS = _FRONTEND / "earnings.js"


def _js_code(path: Path) -> str:
    """Source with comments stripped, so the greps below read what the file DOES
    rather than what its header explains it deliberately avoids."""
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


# ─── The deploy wiring ────────────────────────────────────────────────────────
def test_the_review_page_loads_the_session_client_before_review_js():
    """A missing script tag is how the whole clock went missing. Order matters:
    ``review.js`` reads ``window.AsclepiusSession`` at draw time, so the client has
    to be defined by then — both are ``defer``, which preserves document order."""
    html = _REVIEW_HTML.read_text(encoding="utf-8")
    scripts = re.findall(r'src="/static/asclepius/([\w.-]+\.js)"', html)
    assert "earnings.js" in scripts, "review.html does not load the session client"
    assert "review.js" in scripts
    assert scripts.index("earnings.js") < scripts.index("review.js")


def test_the_review_surface_drives_the_session_client():
    code = _js_code(_REVIEW_JS)
    assert "AsclepiusSession" in code, "review.js never starts a billable session"
    assert ".open(" in code or ".attach(" in code
    assert "setProgress" in code, "review.js never tells payments which pair it moved to"


def test_the_review_surface_does_not_reimplement_the_payment_clock():
    """PRD-P owns the billable session. If the review surface ever computes
    credited time itself, there are two clocks and they will disagree — and the
    one the reviewer is looking at will be the one that is wrong."""
    code = _js_code(_REVIEW_JS)
    for banned in ("credited_seconds =", "MAX_GAP", "min_seconds =", "/heartbeat"):
        assert banned not in code, f"review.js is computing payment time itself: {banned}"


# ─── The countdown ────────────────────────────────────────────────────────────
def _run_node(script: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed in this environment")
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\n{proc.stderr}\n{proc.stdout}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


# review.js is a whole page controller: it reads #reviewRoot at load and calls
# boot() at the bottom. The shim gives it a root and a fetch that never resolves,
# so it renders its loading state and then sits there — which is all we need,
# because the property under test is what the header does with a session state
# handed to it.
_REVIEW_CTX = """
require(%(shim)s);
globalThis.localStorage = {
  _v: { asclepius_token: 'test-token' },
  getItem: function (k) { return this._v[k] || null; },
  setItem: function (k, v) { this._v[k] = v; },
  removeItem: function (k) { delete this._v[k]; },
};
globalThis.fetch = function () { return new Promise(function () {}); };
globalThis.setInterval = function () { return 1; };
globalThis.clearInterval = function () {};
var root = document.createElement('div');
root.id = 'reviewRoot';
document.register(root);
document.body.appendChild(root);

var SESSION_STATE = %(state)s;
var calls = [];
window.AsclepiusSession = {
  open: function (kind, key) { calls.push(['open', kind, key]); return Promise.resolve({}); },
  attach: function () { calls.push(['attach']); },
  setProgress: function (key) { calls.push(['setProgress', key]); },
  stop: function (reason) { calls.push(['stop', reason]); return Promise.resolve({}); },
  subscribe: function (fn) { globalThis.__notify = fn; return function () {}; },
  state: function () { return SESSION_STATE; },
};
function textOf(el) {
  if (el.nodeValue != null) return el.nodeValue;
  return (el.childNodes || []).map(textOf).join(' ');
}
function find(el, cls) {
  var out = [];
  (el.childNodes || []).forEach(function (c) {
    if (!c.tagName) return;
    if (c.classList && c.classList.contains(cls)) out.push(c);
    out = out.concat(find(c, cls));
  });
  return out;
}
eval(require('fs').readFileSync(%(module)s, 'utf8'));
"""


def _script(state, tail: str) -> str:
    return (_REVIEW_CTX % {"shim": json.dumps(str(_DOM_SHIM)),
                           "module": json.dumps(str(_REVIEW_JS)),
                           "state": json.dumps(state)}) + tail


def test_the_header_counter_reads_the_server_number_not_a_local_clock():
    """751 seconds is 12:31. The number a reviewer trusts is arithmetic on a field
    the SERVER sent in a heartbeat response, and on nothing else.

    This is the assertion the instruction manual depends on: section 01 tells a
    physician to trust this counter, and it may only say that once the counter and
    the payout are the same number."""
    out = _run_node(_script({
        "session_id": "ws-1", "credited_seconds": 751, "continuous_seconds": 751,
        "min_seconds": 1200, "remaining_seconds": 449, "qualified": False,
        "ended": False, "paused": False, "rate_cents": 10000, "error": None,
    }, """
var el = window.__reviewSessionChip();
console.log(JSON.stringify({ text: textOf(el), classes: el.className }));
"""))
    assert "12:31" in out["text"]
    assert "20:00" in out["text"]


def test_a_different_server_number_moves_the_counter():
    out = _run_node(_script({
        "session_id": "ws-1", "credited_seconds": 185, "continuous_seconds": 185,
        "min_seconds": 1200, "remaining_seconds": 1015, "qualified": False,
        "ended": False, "paused": False, "rate_cents": 10000, "error": None,
    }, """
console.log(JSON.stringify({ text: textOf(window.__reviewSessionChip()) }));
"""))
    assert "3:05" in out["text"]


def test_the_counter_says_qualified_only_when_the_server_says_so():
    """The failure the instruction manual would otherwise document: a counter that
    reads 'you have qualified' while the server has credited zero seconds."""
    unqualified = _run_node(_script({
        "session_id": "ws-1", "continuous_seconds": 1199, "min_seconds": 1200,
        "remaining_seconds": 1, "qualified": False, "ended": False, "paused": False,
        "rate_cents": 10000, "error": None,
    }, "console.log(JSON.stringify({ text: textOf(window.__reviewSessionChip()) }));"))
    assert "unpaid" in unqualified["text"].lower()

    qualified = _run_node(_script({
        "session_id": "ws-1", "continuous_seconds": 1200, "min_seconds": 1200,
        "remaining_seconds": 0, "qualified": True, "ended": False, "paused": False,
        "rate_cents": 10000, "error": None,
    }, "console.log(JSON.stringify({ text: textOf(window.__reviewSessionChip()) }));"))
    assert "unpaid" not in qualified["text"].lower()


def test_no_session_no_chip():
    out = _run_node(_script(None, """
console.log(JSON.stringify({ chip: window.__reviewSessionChip() === null }));
"""))
    assert out["chip"] is True


def test_a_tracking_failure_is_shown_rather_than_a_confident_number():
    """If the clock is not running, the reviewer must find out while they can
    still do something about it — not at the end of an unpaid hour."""
    out = _run_node(_script({
        "session_id": "ws-1", "continuous_seconds": 300, "min_seconds": 1200,
        "remaining_seconds": 900, "qualified": False, "ended": False, "paused": False,
        "rate_cents": 10000, "error": "Session time is not syncing.",
    }, "console.log(JSON.stringify({ text: textOf(window.__reviewSessionChip()) }));"))
    assert "not syncing" in out["text"]
