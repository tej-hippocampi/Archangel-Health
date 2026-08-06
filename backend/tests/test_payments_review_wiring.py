"""PRD-P — the seam between the billable session and the review surface.

This file used to assert P's own wiring of ``review.js``. Agent R has since
rewritten that file, which is R's lane, so these tests now pin the **contract**
rather than either side's implementation: what the review surface must do, and
what P's client must expose for it to be possible.

── The defect this file exists to have caught ────────────────────────────────

R built against P's client without ever seeing it — their handoff says so
plainly: *"the heartbeat contract is inferred, not read... consumed by feature
detection: state() for rendering, start() if present."*

``start()`` was not present. P's client exposed ``open``/``attach``. R's page did:

    if (S && typeof S.start === 'function') { S.start(SESSION); }

which is a silent no-op against a client that has no ``start``. Every guard in
that line is doing its job and the outcome is still: the server opens a billable
session, the page never beats, and a physician reviews for an hour and is paid
nothing. Nothing throws, nothing logs, and the reviewer's clock reads
"not being timed" — which R's page renders honestly, and which nobody would look
at twice on a tree where payments had not merged yet.

Feature detection across an unmerged seam produces exactly this: two correct
halves and no working whole. So the contract is a test now, and the test drives
R's actual call sequence rather than P's preferred one.
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
    """Source with comments stripped, so the greps read what a file DOES rather
    than the prose explaining what it deliberately avoids."""
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


def _run_node(script: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed in this environment")
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\n{proc.stderr}\n{proc.stdout}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ─── The deploy wiring ────────────────────────────────────────────────────────
def test_the_review_page_loads_the_session_client_before_review_js():
    """A missing script tag is how the whole clock went missing once already.
    Order matters: the review page reads ``window.AsclepiusSession`` at draw
    time, so the client must be defined by then. Both are ``defer``, which
    preserves document order."""
    html = _REVIEW_HTML.read_text(encoding="utf-8")
    scripts = re.findall(r'src="/static/asclepius/([\w.-]+\.js)"', html)
    assert "earnings.js" in scripts, "review.html does not load the session client"
    assert "review.js" in scripts
    assert scripts.index("earnings.js") < scripts.index("review.js")


def test_the_review_surface_starts_a_billable_session():
    code = _js_code(_REVIEW_JS)
    assert "AsclepiusSession" in code, "the review surface never starts a billable session"
    assert re.search(r"\.(start|open|attach)\s*\(", code), \
        "the review surface holds a session client and never hands it a session"


def test_the_review_surface_does_not_reimplement_the_payment_clock():
    """PRD-P owns the billable session. If the review surface computes credited
    time itself there are two clocks, they will disagree, and the one the
    reviewer is looking at will be the wrong one — which is precisely the defect
    R's own audit found (a page that read 20:00 while the server had counted
    zero)."""
    code = _js_code(_REVIEW_JS)
    for banned in ("credited_seconds =", "MAX_GAP", "/heartbeat", "/sessions"):
        assert banned not in code, f"the review surface is doing payments' job: {banned}"


# ─── The contract, driven exactly the way R drives it ─────────────────────────
_CTX = """
require(%(shim)s);
globalThis.localStorage = {
  _v: { asclepius_token: 'test-token' },
  getItem: function (k) { return this._v[k] || null; },
  setItem: function (k, v) { this._v[k] = v; },
  removeItem: function (k) { delete this._v[k]; },
};
var fetchCalls = [];
var ROUTES = %(routes)s;
globalThis.fetch = function (url, opts) {
  fetchCalls.push({ url: url, method: (opts && opts.method) || 'GET',
                    body: (opts && opts.body) ? JSON.parse(opts.body) : null });
  var key = String(url).replace('/api/asclepius', '');
  return Promise.resolve({
    ok: true, status: 200, statusText: 'OK',
    json: function () { return Promise.resolve(ROUTES['FETCH ' + key] || {}); },
  });
};
globalThis.setInterval = function (fn, ms) { globalThis.__intervals.push({fn: fn, ms: ms}); return globalThis.__intervals.length; };
globalThis.clearInterval = function (id) { if (id) globalThis.__intervals[id - 1] = null; };
globalThis.__intervals = [];
window.addEventListener = function () {};
window.removeEventListener = function () {};
function done(fn) { setTimeout(fn, 0); }
eval(require('fs').readFileSync(%(module)s, 'utf8'));
"""

# What Agent P's ``open_session`` actually returns for a fresh session. R's router
# forwards this block verbatim and opaquely, which is the correct shape for the
# seam — R never reads inside it.
_SESSION_BLOCK = {
    "session_id": "ws-1", "started_at": "2026-08-03T14:00:00", "min_seconds": 1200,
    "credited_seconds": 0, "continuous_seconds": 0, "nonce": "n0", "qualified": False,
    "rate_cents": 10000, "remaining_seconds": 1200, "resumed": False,
    "params": {"beat_interval_seconds": 15, "min_seconds": 1200, "rate_cents": 10000},
}
_BEAT_OK = {
    "credited_seconds": 15, "continuous_seconds": 15, "min_seconds": 1200,
    "remaining_seconds": 1185, "qualified": False, "next_nonce": "n1", "seq": 1,
    "ended": False,
}


def _script(routes: dict, tail: str) -> str:
    return (_CTX % {"shim": json.dumps(str(_DOM_SHIM)),
                    "module": json.dumps(str(_EARNINGS_JS)),
                    "routes": json.dumps(routes)}) + tail


def test_the_client_exposes_start_which_is_the_name_the_review_surface_calls():
    """The whole defect in one assertion. R's page feature-detects ``start``; a
    client without one silently does nothing, and a reviewer works unpaid."""
    out = _run_node(_script({}, """
console.log(JSON.stringify({
  api: Object.keys(window.AsclepiusSession).sort(),
  hasStart: typeof window.AsclepiusSession.start === 'function',
}));
"""))
    assert out["hasStart"] is True, "R's page calls start(); this client has no start()"
    # The names both sides may rely on. Adding is fine; removing breaks a seam.
    for name in ("start", "attach", "open", "setProgress", "stop", "state", "subscribe"):
        assert name in out["api"], name


def test_r_s_exact_call_sequence_produces_heartbeats():
    """Drive the seam the way the review surface actually drives it: take P's
    session block off the draw response, hand it to ``start``, and nothing else.
    If this does not beat, a physician is not being paid."""
    out = _run_node(_script({
        "FETCH /sessions/ws-1/heartbeat": _BEAT_OK,
    }, """
var S = window.AsclepiusSession;
if (S && typeof S.start === 'function') {
  try { S.start(%s); } catch (e) { /* never block the review */ }
}
done(function () {
  var beats = fetchCalls.filter(function (c) { return c.url.indexOf('heartbeat') !== -1; });
  console.log(JSON.stringify({
    beats: beats.length,
    body: beats.length ? beats[0].body : null,
    intervals: globalThis.__intervals.filter(Boolean).map(function (i) { return i.ms; }),
    state: S.state(),
  }));
});
""" % json.dumps(_SESSION_BLOCK)))
    assert out["beats"] == 1, "start() did not send an opening heartbeat"
    assert out["body"]["nonce"] == "n0"
    assert out["intervals"] == [15000], "start() did not begin beating on the server's cadence"
    assert out["state"]["continuous_seconds"] == 15


def test_a_caller_that_names_no_work_still_sends_a_progress_key():
    """Every beat must name the work it is a beat for, or the server refuses it
    (audit C2). The review surface does not currently pass one.

    Refusing to beat would be defensible and is the wrong call: a physician would
    lose $100 because two agents' merge order left an argument unpassed. So the
    client supplies a session-scoped fallback and makes it OBVIOUS in the record —
    the key literally reads ``session:<id>``, so anyone auditing a payout can see
    at a glance that the client never named the work.

    This does not weaken the server: the server still requires a key on every
    beat, and this is only P's own client declining to violate its own
    contract."""
    out = _run_node(_script({
        "FETCH /sessions/ws-1/heartbeat": _BEAT_OK,
    }, """
window.AsclepiusSession.start(%s);
done(function () {
  var beats = fetchCalls.filter(function (c) { return c.url.indexOf('heartbeat') !== -1; });
  console.log(JSON.stringify({ key: beats[0].body.progress_key }));
});
""" % json.dumps(_SESSION_BLOCK)))
    assert out["key"] == "session:ws-1"


def test_a_caller_that_does_name_the_work_wins():
    """The fallback is a floor, not a ceiling. When the review surface passes the
    pair it is on — which is the two-line change it should make — that key is what
    lands on the beat, and the per-case accounting works as designed."""
    out = _run_node(_script({
        "FETCH /sessions/ws-1/heartbeat": _BEAT_OK,
    }, """
window.AsclepiusSession.start(%s, 'task-abc');
done(function () {
  var beats = fetchCalls.filter(function (c) { return c.url.indexOf('heartbeat') !== -1; });
  console.log(JSON.stringify({ key: beats[0].body.progress_key }));
});
""" % json.dumps(_SESSION_BLOCK)))
    assert out["key"] == "task-abc"


def test_starting_twice_on_the_same_session_does_not_double_the_beat_loop():
    """The review surface calls this on EVERY pair draw, because the server call
    behind it is idempotent. The client has to be idempotent too, or a reviewer
    who adjudicates six pairs ends up with six interval timers beating in
    parallel — six times the write load, and a nonce race against itself."""
    out = _run_node(_script({
        "FETCH /sessions/ws-1/heartbeat": _BEAT_OK,
    }, """
var S = window.AsclepiusSession;
S.start(%s);
done(function () {
  S.start(%s);
  S.start(%s);
  done(function () {
    console.log(JSON.stringify({
      timers: globalThis.__intervals.filter(Boolean).length,
    }));
  });
});
""" % (json.dumps(_SESSION_BLOCK), json.dumps(_SESSION_BLOCK), json.dumps(_SESSION_BLOCK))))
    assert out["timers"] == 1, "each draw started another beat loop"


def test_starting_a_new_session_replaces_the_old_loop():
    """A reviewer who signs out and back in gets a different session. The old
    loop must stop, or it keeps beating against a session this tab no longer
    owns."""
    second = dict(_SESSION_BLOCK, session_id="ws-2", nonce="m0")
    out = _run_node(_script({
        "FETCH /sessions/ws-1/heartbeat": _BEAT_OK,
        "FETCH /sessions/ws-2/heartbeat": dict(_BEAT_OK, next_nonce="m1"),
    }, """
var S = window.AsclepiusSession;
S.start(%s);
done(function () {
  S.start(%s);
  done(function () {
    console.log(JSON.stringify({
      timers: globalThis.__intervals.filter(Boolean).length,
      session: S.state().session_id,
    }));
  });
});
""" % (json.dumps(_SESSION_BLOCK), json.dumps(second))))
    assert out["timers"] == 1
    assert out["session"] == "ws-2"


def test_state_returns_the_three_fields_the_review_surface_renders():
    """R's page reads exactly ``continuous_seconds``, ``min_seconds`` and
    ``qualified``, and renders "not being timed" if any is missing. Those three
    are load-bearing across the seam; renaming one is a silent blank clock."""
    out = _run_node(_script({
        "FETCH /sessions/ws-1/heartbeat": _BEAT_OK,
    }, """
window.AsclepiusSession.start(%s);
done(function () {
  var st = window.AsclepiusSession.state();
  console.log(JSON.stringify({
    continuous: typeof st.continuous_seconds,
    min: typeof st.min_seconds,
    qualified: typeof st.qualified,
  }));
});
""" % json.dumps(_SESSION_BLOCK)))
    assert out["continuous"] == "number"
    assert out["min"] == "number"
    assert out["qualified"] == "boolean"


def test_only_the_server_ever_says_a_session_qualified():
    """R's page refuses to infer qualification from elapsed time reaching the
    target, because a session can run past its minimum and still not count. The
    client must not infer it either — ``qualified`` is copied from the response
    and never computed."""
    out = _run_node(_script({
        "FETCH /sessions/ws-1/heartbeat": dict(
            _BEAT_OK, continuous_seconds=1800, remaining_seconds=0, qualified=False),
    }, """
window.AsclepiusSession.start(%s);
done(function () {
  var st = window.AsclepiusSession.state();
  console.log(JSON.stringify({ continuous: st.continuous_seconds, qualified: st.qualified }));
});
""" % json.dumps(_SESSION_BLOCK)))
    assert out["continuous"] == 1800
    assert out["qualified"] is False, "the client decided a session qualified; only the server may"
