"""PRD-P Phase 4 — the Earnings section actually renders, and renders honestly.

Source-grepping a frontend module proves it was written, not that it works. This
repo has already paid for that lesson once: a section can be complete, correct and
INVISIBLE for a whole build round because nothing mounted it and the failure was
quiet. So these tests execute ``earnings.js`` against the DOM shim and assert what
lands in the document.

Two properties matter more than the layout, and both are asserted here:

  * the countdown value comes from a RESPONSE BODY, never from a local timer — a
    client-side clock would drift away from the number that decides the payout;
  * a failed load renders a VISIBLE error, never a reassuring ``$0``. "We could
    not load your ledger" and "you have earned nothing" must never look the same
    to a physician.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
_DOM_SHIM = Path(__file__).resolve().parent / "_asclepius_dom.js"
_EARNINGS_JS = _FRONTEND / "earnings.js"


def _run_node(script: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed in this environment")
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\n{proc.stderr}\n{proc.stdout}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


# The same ctx shape asclepius.js hands its section modules, plus the browser
# globals the session client touches. ``fetch`` and ``localStorage`` are stubbed
# rather than mocked away, so the client's real transport code runs.
_JS_CTX = """
require(%(shim)s);
function h(tag, attrs) {
  var el = document.createElement(tag);
  if (attrs) for (var k in attrs) {
    var v = attrs[k];
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v; else el.setAttribute(k, v);
  }
  for (var i = 2; i < arguments.length; i++) appendChild_(el, arguments[i]);
  return el;
}
function isText_(c) { return typeof c === 'string' || typeof c === 'number'; }
function appendChild_(el, c) {
  if (c == null || c === '' || c === false) return;
  if (Array.isArray(c)) { c.forEach(function (x) { appendChild_(el, x); }); return; }
  el.appendChild(isText_(c) ? document.createTextNode(String(c)) : c);
}
var apiCalls = [];
var fetchCalls = [];
var ROUTES = %(routes)s;
var FAIL = %(fail)s;
var API_BASE = '/api/asclepius';
var ctx = {
  h: h,
  clear: function (el) { while (el.firstChild) el.removeChild(el.firstChild); },
  // Faithful to asclepius.js: the real ctx.api PREFIXES API_BASE and the caller
  // passes a path relative to it. Keying ROUTES on the raw argument made the
  // stub answer any string the caller invented, which is how
  // `ctx.api('/asclepius/earnings')` — resolving to
  // /api/asclepius/asclepius/earnings, a route that does not exist — passed
  // every test in this file while 404ing for every physician in the browser.
  // ROUTES is keyed on the URL THE SERVER ACTUALLY SERVES, so a doubled (or
  // missing) prefix misses here exactly as it does in production.
  api: function (path, opts) {
    var url = API_BASE + path;
    apiCalls.push({ path: path, url: url, method: (opts && opts.method) || 'GET' });
    if (FAIL[url]) return Promise.reject(FAIL[url]);
    if (!Object.prototype.hasOwnProperty.call(ROUTES, url)) {
      return Promise.reject({ status: 404, detail: 'Not Found', message: 'Not Found',
                              url: url });
    }
    return Promise.resolve(ROUTES[url]);
  },
  toast: function () {},
  loadingCard: function (t) { return h('div', {}, t); },
  downloadBlob: function () {},
  fmtDate: function (d) { return String(d); },
  openPipeline: function () {},
};
globalThis.localStorage = {
  _v: { asclepius_token: 'test-token' },
  getItem: function (k) { return this._v[k] || null; },
  setItem: function (k, v) { this._v[k] = v; },
  removeItem: function (k) { delete this._v[k]; },
};
globalThis.fetch = function (url, opts) {
  fetchCalls.push({ url: url, method: (opts && opts.method) || 'GET',
                    keepalive: !!(opts && opts.keepalive),
                    headers: (opts && opts.headers) || {},
                    body: (opts && opts.body) ? JSON.parse(opts.body) : null });
  var key = String(url).replace('/api/asclepius', '');
  var payload = ROUTES['FETCH ' + key] || {};
  return Promise.resolve({
    ok: true, status: 200, statusText: 'OK',
    json: function () { return Promise.resolve(payload); },
  });
};
// The shim's `window` is a scroll stub with no event target. The session client
// binds pagehide to it, so give it one — and record what was bound, so a test can
// assert on the lifecycle events themselves rather than only on the source text.
window.addEventListener = function (type, fn) {
  (globalThis.__windowListeners[type] = globalThis.__windowListeners[type] || []).push(fn);
};
window.removeEventListener = function (type, fn) {
  var l = globalThis.__windowListeners[type] || [];
  var i = l.indexOf(fn);
  if (i !== -1) l.splice(i, 1);
};
window.dispatch = function (type, ev) {
  (globalThis.__windowListeners[type] || []).slice().forEach(function (fn) {
    fn(Object.assign({ type: type }, ev || {}));
  });
};
globalThis.__windowListeners = {};
globalThis.setInterval = function (fn, ms) { globalThis.__intervals.push({ fn: fn, ms: ms }); return globalThis.__intervals.length; };
globalThis.clearInterval = function (id) { if (id) globalThis.__intervals[id - 1] = null; };
globalThis.__intervals = [];
function textOf(el) {
  if (el.nodeValue != null) return el.nodeValue;
  return (el.childNodes || []).map(textOf).join(' ');
}
function classesOf(el) {
  var out = el.className ? [el.className] : [];
  (el.childNodes || []).forEach(function (c) { if (c.tagName) out = out.concat(classesOf(c)); });
  return out;
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
function done(fn) { setTimeout(function () { fn(); }, 0); }
eval(require('fs').readFileSync(%(module)s, 'utf8'));
"""

_LEDGER = {
    "currency": "USD",
    "approved_cents": 247500,
    "pending_cents": 15000,
    "void_cents": 7500,
    "lines": [
        {"kind": "task", "label": "Tasks labeled", "count": 33,
         "rate_cents": 7500, "cents": 247500},
        {"kind": "review_session", "label": "Review sessions", "count": 0,
         "rate_cents": 10000, "cents": 0},
    ],
    "recent": [
        {"earning_id": "e1", "kind": "task", "kind_label": "Task",
         "ref_id": "s-1", "amount_cents": 7500, "rate_cents": 7500,
         "status": "approved", "status_word": "Approved",
         "accrued_at": "2026-08-03T10:00:00", "resolved_at": "2026-08-04T10:00:00",
         "note": None, "detail": "nephrology case"},
        {"earning_id": "e2", "kind": "task", "kind_label": "Task",
         "ref_id": "s-2", "amount_cents": 7500, "rate_cents": 7500,
         "status": "accrued", "status_word": "Pending review",
         "accrued_at": "2026-08-02T10:00:00", "resolved_at": None,
         "note": None, "detail": "cardiology case"},
        {"earning_id": "e3", "kind": "task", "kind_label": "Task",
         "ref_id": "s-3", "amount_cents": 7500, "rate_cents": 7500,
         "status": "void", "status_word": "Not approved",
         "accrued_at": "2026-08-01T10:00:00", "resolved_at": "2026-08-02T10:00:00",
         "note": "Not approved on review: dialysate target is unsafe.",
         "detail": "nephrology case"},
    ],
    "accrues_payment": True,
    "open_session": None,
    "params": {"beat_interval_seconds": 15, "max_gap_seconds": 45,
               "pause_tolerance_seconds": 90, "min_seconds": 1200,
               "rate_cents": 10000},
}


def _script(routes: dict, tail: str, fail: dict | None = None) -> str:
    return (_JS_CTX % {"shim": json.dumps(str(_DOM_SHIM)),
                       "module": json.dumps(str(_EARNINGS_JS)),
                       "routes": json.dumps(routes),
                       "fail": json.dumps(fail or {})}) + tail


# ─── The rule that has no exceptions ──────────────────────────────────────────
def _code() -> str:
    """The module with comments and block comments stripped.

    The greps below are about what the module DOES. Its docstring explains, at
    length, which APIs it refuses to use and why — and a naive grep over the raw
    file would read those explanations as violations, which would push every
    future maintainer toward deleting the reasoning to keep the test green."""
    source = _EARNINGS_JS.read_text(encoding="utf-8")
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


def test_the_module_contains_no_innerhtml():
    """Zero innerHTML. Every server string goes through textContent or h()
    children — a ledger row is server data and a status word is server data."""
    code = _code()
    assert "innerHTML" not in code
    assert "outerHTML" not in code
    assert "insertAdjacentHTML" not in code
    assert "document.write" not in code


def test_the_close_path_uses_visibility_and_pagehide_never_unload():
    """beforeunload/unload are unreliable on mobile and unload disables the
    bfcache. This is asserted on the source because the events themselves cannot
    be fired meaningfully in a shim."""
    code = _code()
    assert "visibilitychange" in code
    assert "pagehide" in code
    assert "beforeunload" not in code
    assert "addEventListener('unload'" not in code
    # And no Web Worker to keep beats flowing in a hidden tab — that would be
    # paying for a tab nobody is looking at.
    assert "new Worker" not in code
    assert "SharedWorker" not in code


def test_no_client_side_countdown_timer_exists():
    """The one property that protects the payout: there is no local stopwatch
    decrementing toward the threshold. Every rendered number is read off a
    response body. A setInterval that recomputed elapsed time locally would drift
    away from the number the server will actually pay on."""
    code = _code()
    # Date arithmetic is how a local clock would be built. The only Date use in
    # the file is formatting a ledger row's date for display.
    assert "Date.now()" not in code
    assert "performance.now" not in code
    assert code.count("new Date(") == 1, "the only Date construction is date formatting"


# ─── Mounting ─────────────────────────────────────────────────────────────────
def test_the_module_defines_both_globals_asclepius_js_mounts():
    out = _run_node(_script({}, """
console.log(JSON.stringify({
  section: !!(window.EarningsSection && typeof window.EarningsSection.render === 'function'),
  client: !!(window.AsclepiusSession && typeof window.AsclepiusSession.open === 'function'),
  clientApi: window.AsclepiusSession ? Object.keys(window.AsclepiusSession).sort() : [],
}));
"""))
    assert out["section"] is True
    assert out["client"] is True
    # The frozen shape PRD-R calls into.
    for name in ("open", "attach", "setProgress", "stop", "state", "subscribe"):
        assert name in out["clientApi"]


# ─── The ledger ───────────────────────────────────────────────────────────────
def test_the_headline_and_the_ledger_render():
    out = _run_node(_script({"/api/asclepius/earnings": _LEDGER}, """
var body = document.createElement('div');
window.EarningsSection.render(body, ctx);
done(function () {
  console.log(JSON.stringify({
    hero: find(body, 'asc-pay-hero-value').map(textOf),
    sub: find(body, 'asc-pay-sub').map(textOf),
    lines: find(body, 'asc-pay-line').map(textOf),
    rows: find(body, 'asc-pay-row').length,
    statuses: find(body, 'asc-pay-status').map(function (e) {
      return { word: textOf(e), cls: e.className };
    }),
    note: find(body, 'asc-pay-note').map(textOf),
    text: textOf(body),
  }));
});
"""))
    assert out["hero"] == ["$2,475"]
    assert "$150 pending review" in " ".join(out["sub"])
    assert "$75 not approved" in " ".join(out["sub"])
    assert any("Tasks labeled" in l and "33 × $75" in l and "$2,475" in l for l in out["lines"])
    assert any("Review sessions" in l and "0 × $100" in l for l in out["lines"])
    assert out["rows"] == 3
    # Never a raw token — the doctor reads words.
    assert [s["word"] for s in out["statuses"]] == ["Approved", "Pending review", "Not approved"]


def test_status_colour_carries_meaning_and_a_rejection_is_never_pink():
    """green approved · lime pending · muted grey not approved. Pink means flag /
    PHI / critical / blocking; a task that did not pass review is not a safety
    event, and colouring it like one would make a doctor think they did something
    dangerous."""
    out = _run_node(_script({"/api/asclepius/earnings": _LEDGER}, """
var body = document.createElement('div');
window.EarningsSection.render(body, ctx);
done(function () {
  console.log(JSON.stringify({
    classes: find(body, 'asc-pay-status').map(function (e) { return e.className; }),
    all: classesOf(body).join(' '),
  }));
});
"""))
    assert "asc-pay-approved" in out["classes"][0]
    assert "asc-pay-pending" in out["classes"][1]
    assert "asc-pay-void" in out["classes"][2]
    assert "pink" not in out["all"].lower()
    assert "critical" not in out["all"].lower()


def test_the_twenty_minute_rule_is_visible_without_a_session_open():
    """Audit U1, and the most consequential UX finding in the release.

    The one sentence that states the $0 rule lived inside the session widget,
    which renders nothing when there is no session. So a physician who had never
    started a review could not find the rule on the Earnings page at all — and
    once they DID have a session, the sentence was on a tab they were not looking
    at. The rule has to be discoverable before it can cost anybody anything."""
    out = _run_node(_script({"/api/asclepius/earnings": _LEDGER}, """
var body = document.createElement('div');
window.EarningsSection.render(body, ctx);
done(function () {
  console.log(JSON.stringify({
    rule: find(body, 'asc-pay-rule').map(textOf),
    widgets: find(body, 'asc-pay-session').length,
  }));
});
"""))
    assert out["widgets"] == 0, "no session is open — this is the case that was blank"
    assert len(out["rule"]) == 1
    rule = out["rule"][0]
    assert "20:00" in rule
    assert "unpaid" in rule
    assert "$100" in rule


def test_the_rule_card_is_not_shown_to_someone_it_does_not_apply_to():
    """An advisor is not paid per session, so telling them what a session pays
    would be noise at best and misleading at worst."""
    ledger = dict(_LEDGER, accrues_payment=False, recent=[])
    out = _run_node(_script({"/api/asclepius/earnings": ledger}, """
var body = document.createElement('div');
window.EarningsSection.render(body, ctx);
done(function () { console.log(JSON.stringify({ rule: find(body, 'asc-pay-rule').length })); });
"""))
    assert out["rule"] == 0


def test_the_page_heading_uses_the_section_scale_not_a_second_h1():
    """Audit U3. The portal shell and each view already own the document's
    heading structure; a section module adding its own ``h1`` puts two at the top
    level of one page."""
    out = _run_node(_script({"/api/asclepius/earnings": _LEDGER}, """
var body = document.createElement('div');
window.EarningsSection.render(body, ctx);
done(function () {
  var tags = [];
  (function walk(el) {
    (el.childNodes || []).forEach(function (c) {
      if (!c.tagName) return;
      tags.push(c.tagName);
      walk(c);
    });
  })(body);
  console.log(JSON.stringify({ h1: tags.filter(function (t) { return t === 'H1'; }).length,
                               h2: tags.filter(function (t) { return t === 'H2'; }).length }));
});
"""))
    assert out["h1"] == 0
    assert out["h2"] >= 1


def test_paid_money_is_distinguishable_from_money_still_owed():
    """Audit H2's UI half. "You have been paid $75" and "we owe you $75" are
    different sentences, and the headline used to render them identically."""
    ledger = dict(_LEDGER, approved_cents=247500, paid_cents=200000, unpaid_cents=47500)
    out = _run_node(_script({"/api/asclepius/earnings": ledger}, """
var body = document.createElement('div');
window.EarningsSection.render(body, ctx);
done(function () { console.log(JSON.stringify({ sub: find(body, 'asc-pay-sub').map(textOf) })); });
"""))
    joined = " ".join(out["sub"])
    assert "$2,000 paid" in joined
    assert "$475 to come" in joined


def test_a_number_that_went_down_carries_its_explanation():
    """§1.2: never show a doctor a number that might go down without an
    explanation next to it."""
    out = _run_node(_script({"/api/asclepius/earnings": _LEDGER}, """
var body = document.createElement('div');
window.EarningsSection.render(body, ctx);
done(function () { console.log(JSON.stringify({ notes: find(body, 'asc-pay-note').map(textOf) })); });
"""))
    assert len(out["notes"]) == 1
    assert "dialysate target is unsafe" in out["notes"][0]


def test_a_non_accruing_account_is_told_why_rather_than_shown_an_unexplained_zero():
    ledger = dict(_LEDGER, accrues_payment=False, approved_cents=0,
                  pending_cents=0, void_cents=0, recent=[])
    out = _run_node(_script({"/api/asclepius/earnings": ledger}, """
var body = document.createElement('div');
window.EarningsSection.render(body, ctx);
done(function () {
  console.log(JSON.stringify({
    advisor: find(body, 'asc-pay-noaccrual').map(textOf),
    hero: find(body, 'asc-pay-hero-value').length,
    lines: find(body, 'asc-pay-line').length,
    recent: find(body, 'asc-pay-recent').length,
    text: textOf(body),
  }));
});
"""))
    assert out["hero"] == 0, "a non-accruing account is not shown a headline figure"
    assert "equity" in out["advisor"][0]
    assert "does not accrue a payment" in out["advisor"][0]
    # "Tasks labeled 0 × $75 · $0" under "you hold equity rather than a per-task
    # rate" reads as a contradiction of the sentence above it.
    assert out["lines"] == 0
    assert out["recent"] == 0
    assert "$75" not in out["text"] and "$0" not in out["text"]


# ─── Failure is visible ───────────────────────────────────────────────────────
def test_a_failed_load_renders_a_visible_error_and_never_a_reassuring_zero():
    out = _run_node(_script(
        {}, """
var body = document.createElement('div');
window.EarningsSection.render(body, ctx);
done(function () {
  console.log(JSON.stringify({
    errors: find(body, 'asc-inline-error').map(textOf),
    heroes: find(body, 'asc-pay-hero-value').map(textOf),
    text: textOf(body),
  }));
});
""",
        fail={"/api/asclepius/earnings": {"status": 500, "detail": "Internal Server Error"}}))
    assert len(out["errors"]) == 1
    assert "could not be loaded" in out["errors"][0]
    assert "Internal Server Error" in out["errors"][0]
    # The critical negative: no figure at all, so nothing can be misread as $0.
    assert out["heroes"] == []
    assert "$0" not in out["text"]


# ─── The countdown ────────────────────────────────────────────────────────────
def test_the_countdown_value_comes_from_the_response_body():
    """751 seconds is 12:31. The number on screen is arithmetic on a field the
    SERVER sent, and nothing else."""
    ledger = dict(_LEDGER, open_session={
        "session_id": "ws-1", "started_at": "2026-08-03T14:00:00",
        "last_beat_at": "2026-08-03T14:12:31", "credited_seconds": 751,
        "continuous_seconds": 751, "min_seconds": 1200, "remaining_seconds": 449,
        "qualified": False, "rate_cents": 10000,
    })
    out = _run_node(_script({"/api/asclepius/earnings": ledger}, """
var body = document.createElement('div');
window.EarningsSection.render(body, ctx);
done(function () {
  console.log(JSON.stringify({
    clock: find(body, 'asc-pay-session-clock').map(textOf),
    elapsed: find(body, 'asc-pay-session-elapsed').map(textOf),
    warning: find(body, 'asc-pay-session-warning').map(textOf),
  }));
});
"""))
    assert out["elapsed"] == ["12:31"]
    assert "Session ·" in out["clock"][0]
    assert "of 20:00" in out["clock"][0]
    # Say it in exactly these words. It is the single sentence that stops a
    # doctor discovering the rule by losing $100.
    assert out["warning"] == ["Leaving before 20:00 ends the session unpaid."]


def test_a_different_response_body_moves_the_countdown_and_nothing_else_does():
    """The same module, the same elapsed wall time, a different server number ->
    a different countdown. That is what "server-authoritative" means, and it is
    only true because nothing local is counting."""
    ledger = dict(_LEDGER, open_session={
        "session_id": "ws-1", "started_at": "2026-08-03T14:00:00",
        "last_beat_at": "2026-08-03T14:03:05", "credited_seconds": 185,
        "continuous_seconds": 185, "min_seconds": 1200, "remaining_seconds": 1015,
        "qualified": False, "rate_cents": 10000,
    })
    out = _run_node(_script({"/api/asclepius/earnings": ledger}, """
var body = document.createElement('div');
window.EarningsSection.render(body, ctx);
done(function () {
  console.log(JSON.stringify({ elapsed: find(body, 'asc-pay-session-elapsed').map(textOf) }));
});
"""))
    assert out["elapsed"] == ["3:05"]


def test_a_qualified_session_says_so_instead_of_the_warning():
    ledger = dict(_LEDGER, open_session={
        "session_id": "ws-1", "started_at": "2026-08-03T14:00:00",
        "last_beat_at": "2026-08-03T14:24:00", "credited_seconds": 1440,
        "continuous_seconds": 1440, "min_seconds": 1200, "remaining_seconds": 0,
        "qualified": True, "rate_cents": 10000,
    })
    out = _run_node(_script({"/api/asclepius/earnings": ledger}, """
var body = document.createElement('div');
window.EarningsSection.render(body, ctx);
done(function () {
  console.log(JSON.stringify({
    warning: find(body, 'asc-pay-session-warning').map(textOf),
    qualified: find(body, 'asc-pay-session').map(function (e) { return e.className; }),
  }));
});
"""))
    assert "has passed 20:00 and is worth $100" in out["warning"][0]
    assert "is-qualified" in out["qualified"][0]


def test_no_session_widget_when_no_session_is_open():
    out = _run_node(_script({"/api/asclepius/earnings": _LEDGER}, """
var body = document.createElement('div');
window.EarningsSection.render(body, ctx);
done(function () {
  console.log(JSON.stringify({ widgets: find(body, 'asc-pay-session').length }));
});
"""))
    assert out["widgets"] == 0


# ─── The session client ───────────────────────────────────────────────────────
def test_opening_a_session_starts_beating_and_carries_the_bearer_token():
    out = _run_node(_script({
        "FETCH /sessions": {
            "session_id": "ws-9", "nonce": "n0", "min_seconds": 1200,
            "credited_seconds": 0, "continuous_seconds": 0, "remaining_seconds": 1200,
            "qualified": False, "rate_cents": 10000, "resumed": False,
            "params": {"beat_interval_seconds": 15, "min_seconds": 1200, "rate_cents": 10000},
        },
        "FETCH /sessions/ws-9/heartbeat": {
            "credited_seconds": 15, "continuous_seconds": 15, "min_seconds": 1200,
            "remaining_seconds": 1185, "qualified": False, "next_nonce": "n1", "ended": False,
        },
    }, """
window.AsclepiusSession.open('review', 'pair-1').then(function () {
  done(function () {
    console.log(JSON.stringify({
      calls: fetchCalls.map(function (c) {
        return { url: c.url, method: c.method, auth: c.headers.Authorization || null,
                 keepalive: c.keepalive, body: c.body };
      }),
      state: window.AsclepiusSession.state(),
      intervals: globalThis.__intervals.filter(Boolean).map(function (i) { return i.ms; }),
    }));
  });
});
"""))
    calls = out["calls"]
    assert calls[0]["url"] == "/api/asclepius/sessions"
    assert calls[0]["method"] == "POST"
    assert calls[0]["auth"] == "Bearer test-token"
    # The opening beat goes out immediately, not one interval later.
    assert calls[1]["url"] == "/api/asclepius/sessions/ws-9/heartbeat"
    assert calls[1]["body"]["nonce"] == "n0"
    assert calls[1]["body"]["active"] is True
    # Every beat names the work it is a beat for (audit C2).
    assert calls[1]["body"]["progress_key"] == "pair-1"
    # The cadence is the SERVER's number, taken from the open response.
    assert out["intervals"] == [15000]
    # And the countdown state is entirely the server's answer.
    assert out["state"]["continuous_seconds"] == 15
    assert out["state"]["remaining_seconds"] == 1185
    assert out["state"]["qualified"] is False


def test_attaching_without_naming_the_work_beats_with_a_visible_fallback_key():
    """This test previously asserted the opposite, and the earlier policy was
    wrong.

    The original reasoning: a caller that forgot the progress key would beat into
    a wall of 422s while looking like it was tracking time, so refuse to start and
    say so. That is sound when the caller is a bug you can fix.

    Then Agent R's real code arrived and passed no key — not from carelessness,
    but because they built against this client without ever being able to read it.
    Under the old policy a physician reviews for an hour and is paid nothing
    because an argument went unpassed across a seam that merges in two pieces.
    That is the worst outcome available, and it lands on the person with the least
    ability to do anything about it.

    So the client falls back to a session-scoped key and makes the fallback
    obvious in the record: ``session:<id>`` reads, to anyone auditing a payout, as
    "the client never named the work". The physician is paid, and the gap is
    visible to us rather than to them.
    """
    out = _run_node(_script({
        "FETCH /sessions": {"session_id": "ws-9", "nonce": "n0", "min_seconds": 1200,
                            "resumed": False, "params": {"beat_interval_seconds": 15}},
        "FETCH /sessions/ws-9/heartbeat": {"credited_seconds": 15, "continuous_seconds": 15,
                                           "next_nonce": "n1", "ended": False},
    }, """
window.AsclepiusSession.open('review').then(function () {
  done(function () {
    var beats = fetchCalls.filter(function (c) { return c.url.indexOf('heartbeat') !== -1; });
    console.log(JSON.stringify({
      beats: beats.length,
      key: beats.length ? beats[0].body.progress_key : null,
      timers: globalThis.__intervals.filter(Boolean).length,
      error: window.AsclepiusSession.state() && window.AsclepiusSession.state().error,
    }));
  });
});
"""))
    assert out["beats"] == 1, "the physician's time must be counted"
    assert out["key"] == "session:ws-9", "the unnamed work must be visible in the record"
    assert out["timers"] == 1
    assert out["error"] is None


def test_moving_to_the_next_case_renames_the_work_and_beats_immediately():
    """PRD-R calls setProgress when the reviewer draws their next pair. The beat
    goes out at once rather than at the next tick, so the key change is recorded
    against the moment it happened."""
    out = _run_node(_script({
        "FETCH /sessions": {"session_id": "ws-9", "nonce": "n0", "min_seconds": 1200,
                            "resumed": False, "params": {"beat_interval_seconds": 15}},
        "FETCH /sessions/ws-9/heartbeat": {"credited_seconds": 15, "continuous_seconds": 15,
                                           "next_nonce": "n1", "ended": False},
    }, """
window.AsclepiusSession.open('review', 'pair-1').then(function () {
  done(function () {
    window.AsclepiusSession.setProgress('pair-2');
    done(function () {
      console.log(JSON.stringify({
        keys: fetchCalls.filter(function (c) { return c.url.indexOf('heartbeat') !== -1; })
                        .map(function (c) { return c.body.progress_key; }),
      }));
    });
  });
});
"""))
    assert out["keys"] == ["pair-1", "pair-2"]


def test_the_nonce_rotates_so_a_naive_replay_cannot_beat():
    out = _run_node(_script({
        "FETCH /sessions": {"session_id": "ws-9", "nonce": "n0", "min_seconds": 1200,
                            "credited_seconds": 0, "resumed": False,
                            "params": {"beat_interval_seconds": 15}},
        "FETCH /sessions/ws-9/heartbeat": {
            "credited_seconds": 15, "continuous_seconds": 15, "min_seconds": 1200,
            "remaining_seconds": 1185, "qualified": False, "next_nonce": "ROTATED", "ended": False},
    }, """
window.AsclepiusSession.open('review', 'pair-1').then(function () {
  done(function () {
    window.AsclepiusSession.beat(true);
    done(function () {
      console.log(JSON.stringify({
        nonces: fetchCalls.filter(function (c) { return c.url.indexOf('heartbeat') !== -1; })
                          .map(function (c) { return c.body.nonce; }),
        seqs: fetchCalls.filter(function (c) { return c.url.indexOf('heartbeat') !== -1; })
                        .map(function (c) { return c.body.seq; }),
      }));
    });
  });
});
"""))
    # The second beat presents the nonce the FIRST response handed back.
    assert out["nonces"] == ["n0", "ROTATED"]
    # And the sequence strictly increases, so a replay is refused server-side.
    assert out["seqs"] == [1, 2]


def test_closing_uses_keepalive_so_the_last_seconds_survive_the_page():
    out = _run_node(_script({
        "FETCH /sessions": {"session_id": "ws-9", "nonce": "n0", "min_seconds": 1200,
                            "resumed": False, "params": {"beat_interval_seconds": 15}},
        "FETCH /sessions/ws-9/heartbeat": {"credited_seconds": 0, "continuous_seconds": 0,
                                           "next_nonce": "n1", "ended": False},
        "FETCH /sessions/ws-9/close": {"credited_seconds": 1230, "continuous_seconds": 1230,
                                       "qualified": True, "payout_cents": 10000,
                                       "min_seconds": 1200, "ended": True},
    }, """
window.AsclepiusSession.open('review', 'pair-1').then(function () {
  window.AsclepiusSession.stop('closed', true).then(function () {
    done(function () {
      var close = fetchCalls.filter(function (c) { return c.url.indexOf('/close') !== -1; })[0];
      console.log(JSON.stringify({
        keepalive: close.keepalive, method: close.method,
        auth: close.headers.Authorization, reason: close.body.reason,
        state: window.AsclepiusSession.state(),
      }));
    });
  });
});
"""))
    assert out["keepalive"] is True
    assert out["method"] == "POST"
    assert out["auth"] == "Bearer test-token"
    assert out["reason"] == "closed"
    assert out["state"]["ended"] is True
    assert out["state"]["qualified"] is True


def test_hiding_the_tab_sends_one_pause_beat_and_then_stops():
    """A hidden tab is not working, by policy. One beat marks the pause and the
    timer stops — after which Chrome's setInterval throttling is irrelevant,
    because the beats were meant to stop."""
    out = _run_node(_script({
        "FETCH /sessions": {"session_id": "ws-9", "nonce": "n0", "min_seconds": 1200,
                            "resumed": False, "params": {"beat_interval_seconds": 15}},
        "FETCH /sessions/ws-9/heartbeat": {"credited_seconds": 0, "continuous_seconds": 0,
                                           "next_nonce": "n1", "ended": False},
    }, """
window.AsclepiusSession.open('review', 'pair-1').then(function () {
  done(function () {
    var before = globalThis.__intervals.filter(Boolean).length;
    document.visibilityState = 'hidden';
    document.dispatch('visibilitychange', {});
    done(function () {
      var beats = fetchCalls.filter(function (c) { return c.url.indexOf('heartbeat') !== -1; });
      console.log(JSON.stringify({
        timersBefore: before,
        timersAfter: globalThis.__intervals.filter(Boolean).length,
        lastActive: beats[beats.length - 1].body.active,
        lastKeepalive: beats[beats.length - 1].keepalive,
        beatCount: beats.length,
      }));
    });
  });
});
"""))
    assert out["timersBefore"] == 1
    assert out["timersAfter"] == 0, "the beat timer must stop when the tab hides"
    assert out["lastActive"] is False, "the pause is DECLARED, not inferred from silence"
    assert out["lastKeepalive"] is True
    assert out["beatCount"] == 2


def test_pagehide_closes_the_session_and_unload_is_never_bound():
    """The lifecycle events actually bound, asserted by firing them — not only by
    grepping the source for the ones that must not appear."""
    out = _run_node(_script({
        "FETCH /sessions": {"session_id": "ws-9", "nonce": "n0", "min_seconds": 1200,
                            "resumed": False, "params": {"beat_interval_seconds": 15}},
        "FETCH /sessions/ws-9/heartbeat": {"credited_seconds": 0, "continuous_seconds": 0,
                                           "next_nonce": "n1", "ended": False},
        "FETCH /sessions/ws-9/close": {"credited_seconds": 1230, "continuous_seconds": 1230,
                                       "qualified": True, "payout_cents": 10000,
                                       "min_seconds": 1200, "ended": True},
    }, """
window.AsclepiusSession.open('review', 'pair-1').then(function () {
  done(function () {
    window.dispatch('pagehide', {});
    done(function () {
      var closes = fetchCalls.filter(function (c) { return c.url.indexOf('/close') !== -1; });
      console.log(JSON.stringify({
        bound: Object.keys(globalThis.__windowListeners).sort(),
        closes: closes.length,
        keepalive: closes.length ? closes[0].keepalive : null,
        timers: globalThis.__intervals.filter(Boolean).length,
      }));
    });
  });
});
"""))
    assert out["bound"] == ["pagehide"], "only pagehide is bound to window"
    assert "beforeunload" not in out["bound"]
    assert "unload" not in out["bound"]
    assert out["closes"] == 1
    assert out["keepalive"] is True
    assert out["timers"] == 0


# ─── Every URL this module resolves must be a route the server serves ─────────
#
# The whole class of bug this section shipped with: `ctx.api` prefixes
# API_BASE = '/api/asclepius', and two calls were converted to it without
# dropping their own '/asclepius' segment. The result resolved to
# /api/asclepius/asclepius/earnings — no route, hard 404, on a top-level rail
# item visible to every signed-in physician. Nothing caught it because the DOM
# stub answered whatever string it was handed and no test ever compared the
# resolved URL to the app's route table.
#
# These two tests close that gap from both ends: the URL earnings.js builds is
# asserted literally, and then checked against the LIVE FastAPI route table, so
# renaming the route on either side fails here rather than in front of a doctor.
def _app_route_paths() -> set[str]:
    import tests._asclepius as A  # noqa: F401  (sets env before importing main)
    from main import app

    return {getattr(r, "path", "") for r in app.routes}


def test_the_earnings_call_resolves_to_the_url_the_server_serves():
    out = _run_node(_script({"/api/asclepius/earnings": _LEDGER}, """
var body = document.createElement('div');
window.EarningsSection.render(body, ctx);
done(function () {
  console.log(JSON.stringify({ urls: apiCalls.map(function (c) { return c.url; }) }));
});
"""))
    assert out["urls"] == ["/api/asclepius/earnings"], (
        "earnings.js must call ctx.api('/earnings'); ctx.api already prefixes "
        "/api/asclepius, so any '/asclepius' of its own doubles the segment"
    )
    assert "/api/asclepius/earnings" in _app_route_paths()


def test_the_read_only_session_poll_resolves_to_the_url_the_server_serves():
    """The poll only arms when the ledger reports an open session this tab does
    not hold, so the fixture has to say so — otherwise the call never fires and
    the assertion passes vacuously."""
    ledger = dict(_LEDGER, open_session={"session_id": "ws-9", "kind": "review",
                                         "credited_seconds": 300, "continuous_seconds": 300,
                                         "min_seconds": 1200, "qualified": False,
                                         "ended": False, "rate_cents": 10000})
    out = _run_node(_script({"/api/asclepius/earnings": ledger,
                             "/api/asclepius/sessions/ws-9": {"session_id": "ws-9",
                                                              "ended": False}}, """
var body = document.createElement('div');
window.EarningsSection.render(body, ctx);
done(function () {
  // Fire the poll tick the widget armed, then read what it asked for.
  globalThis.__intervals.filter(Boolean).forEach(function (t) { t.fn(); });
  done(function () {
    console.log(JSON.stringify({ urls: apiCalls.map(function (c) { return c.url; }) }));
  });
});
"""))
    assert "/api/asclepius/sessions/ws-9" in out["urls"], (
        "the read-only session poll must call ctx.api('/sessions/' + id)"
    )
    assert "/api/asclepius/sessions/{session_id}" in _app_route_paths()


# ─── Submitted work is counted where the money for it is reported ─────────────
def test_a_submitted_task_is_counted_beside_the_money_it_is_pending_for():
    """A labeler's first ever visit read "$75 pending" beside "Tasks labeled: 0".
    The money came from ACCRUED, the count came from APPROVED+PAID, and both sat
    under one label — the page contradicting itself about the only thing it
    exists to report."""
    ledger = dict(_LEDGER, approved_cents=0, paid_cents=0, unpaid_cents=0,
                  pending_cents=7500,
                  lines=[{"kind": "task", "label": "Tasks labeled", "count": 0,
                          "rate_cents": 7500, "cents": 0,
                          "pending_count": 1, "pending_cents": 7500}])
    out = _run_node(_script({"/api/asclepius/earnings": ledger}, """
var body = document.createElement('div');
window.EarningsSection.render(body, ctx);
done(function () {
  console.log(JSON.stringify({ lines: find(body, 'asc-pay-line').map(textOf) }));
});
"""))
    joined = " ".join(out["lines"])
    assert "1 awaiting review" in joined, (
        "pending money with no pending count reads as a bug in the thing that pays them"
    )
    assert "$75" in joined


def test_a_line_with_nothing_pending_grows_no_extra_row():
    """The sub-row is information, not decoration: a settled line must not carry
    an empty '0 awaiting review' under it."""
    ledger = dict(_LEDGER,
                  lines=[{"kind": "task", "label": "Tasks labeled", "count": 3,
                          "rate_cents": 7500, "cents": 22500,
                          "pending_count": 0, "pending_cents": 0}])
    out = _run_node(_script({"/api/asclepius/earnings": ledger}, """
var body = document.createElement('div');
window.EarningsSection.render(body, ctx);
done(function () {
  console.log(JSON.stringify({ lines: find(body, 'asc-pay-line').map(textOf) }));
});
"""))
    assert len(out["lines"]) == 1
    assert "awaiting review" not in " ".join(out["lines"])
