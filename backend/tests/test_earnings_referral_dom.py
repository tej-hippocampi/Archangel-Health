"""PRD-REF Phase 4 — the referral card actually renders, and renders honestly.

Source-grepping a frontend module proves it was written, not that it works, and
this repo has already paid for that lesson: a section can be complete, correct
and INVISIBLE for a whole build round because nothing mounted it and the failure
was quiet. So these tests execute ``earnings.js`` against the DOM shim and assert
what lands in the document.

The ``h`` here is FAITHFUL to the one ``asclepius.js`` hands its section modules
— including ``value`` and the ``disabled`` special case — because this card is
the first thing on this surface with an input in it. A simplified helper that
silently turned ``value`` into an attribute would let the whole draft-preservation
property pass here and fail in a browser.

Four properties, and the second is the one the whole PRD is written around:

  * zero ``innerHTML``, still, with a form and server strings in the card;
  * a PENDING referral renders its own line with money on it — never as absence.
    A doctor who refers two colleagues and sees nothing for a month concludes the
    feature is broken;
  * no raw status token (``signed_up``, ``verified``) reaches the DOM;
  * the empty state is a SENTENCE that names the next action, not a blank card.
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
_CSS = _FRONTEND / "asclepius.css"


def _run_node(script: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed in this environment")
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\n{proc.stderr}\n{proc.stdout}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ``h`` mirrors asclepius.js exactly (the onClick / value / disabled branches
# included), so a control that works here works in the portal.
_JS_CTX = """
require(%(shim)s);
function h(tag, attrs) {
  var el = document.createElement(tag);
  if (attrs) for (var k in attrs) {
    var v = attrs[k];
    if (v == null || v === false) continue;
    if (k === 'class' || k === 'className') el.className = v;
    else if (k === 'text' || k === 'textContent') el.textContent = v;
    else if (k === 'dataset') { for (var d in v) el.dataset[d] = v[d]; }
    else if (k === 'disabled') { if (v) el.setAttribute('disabled', ''); }
    else if (k === 'hidden') { if (v) el.setAttribute('hidden', ''); }
    else if (k.slice(0, 2) === 'on' && typeof v === 'function') {
      el.addEventListener(k.slice(2).toLowerCase(), v);
    } else if (k === 'value') { el.value = v; }
    else el.setAttribute(k, v);
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
var ROUTES = %(routes)s;
var FAIL = %(fail)s;
var API_BASE = '/api/asclepius';
var ctx = {
  h: h,
  clear: function (el) { while (el.firstChild) el.removeChild(el.firstChild); },
  api: function (path, opts) {
    var url = API_BASE + path;
    apiCalls.push({ path: path, url: url, method: (opts && opts.method) || 'GET',
                    body: (opts && opts.body) || null });
    if (FAIL[url]) return Promise.reject(FAIL[url]);
    if (!Object.prototype.hasOwnProperty.call(ROUTES, url)) {
      return Promise.reject({ status: 404, detail: 'Not Found', url: url });
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
globalThis.fetch = function () {
  return Promise.resolve({ ok: true, status: 200, statusText: 'OK',
                           json: function () { return Promise.resolve({}); } });
};
window.addEventListener = function () {};
window.removeEventListener = function () {};
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
function tagsOf(el, tag) {
  var out = [];
  (el.childNodes || []).forEach(function (c) {
    if (!c.tagName) return;
    if (c.tagName === tag) out.push(c);
    out = out.concat(tagsOf(c, tag));
  });
  return out;
}
function done(fn) { setTimeout(function () { fn(); }, 0); }
eval(require('fs').readFileSync(%(module)s, 'utf8'));
window.EarningsSection.reset();
"""

_FUNNEL = {
    "can_refer": True,
    "earns_bounty": True,
    "bounty_cents": 15000,
    "referral_code": "ABCD2345",
    "invite_url": "https://example.com/physicians?ref=ABCD2345",
    "referrals": [
        {"referral_id": "ref-1", "invitee_display": "Dr A. Whitfield",
         "status": "approved", "status_sentence": "Completed first case",
         "bounty_state": "earned", "invited_at": "2026-08-02T09:00:00",
         "resolved_at": "2026-08-30T09:00:00"},
        {"referral_id": "ref-2", "invitee_display": "Dr M. Osei",
         "status": "verified", "status_sentence": "Verified · awaiting first case",
         "bounty_state": "pending", "invited_at": "2026-08-07T09:00:00",
         "resolved_at": None},
        {"referral_id": "ref-3", "invitee_display": "j••••@mgh.org",
         "status": "invited", "status_sentence": "Invited",
         "bounty_state": "pending", "invited_at": "2026-08-09T09:00:00",
         "resolved_at": None},
    ],
    "total": 3, "earned_count": 1, "earned_cents": 15000,
    "pending_count": 2, "pending_cents": 30000,
}

_LEDGER = {
    "currency": "USD",
    "approved_cents": 262500,
    "paid_cents": 0,
    "unpaid_cents": 262500,
    "pending_cents": 15000,
    "void_cents": 0,
    "lines": [
        {"kind": "task", "label": "Tasks labeled", "count": 33,
         "rate_cents": 7500, "cents": 247500, "pending_count": 0, "pending_cents": 0},
        {"kind": "review_session", "label": "Review sessions", "count": 2,
         "rate_cents": 10000, "cents": 20000, "pending_count": 0, "pending_cents": 0},
        {"kind": "referral", "label": "Referrals", "count": 1,
         "rate_cents": 15000, "cents": 15000,
         "pending_count": 1, "pending_cents": 15000,
         "pending_label": "1 invited, awaiting their first case"},
    ],
    "recent": [
        {"earning_id": "e1", "kind": "referral", "kind_label": "Referral",
         "ref_id": "ref-1", "amount_cents": 15000, "rate_cents": 15000,
         "status": "approved", "status_word": "Approved",
         "accrued_at": "2026-08-30T10:00:00", "resolved_at": "2026-08-30T10:00:00",
         "note": "Referral · Dr A. Whitfield completed their first case",
         "detail": None},
    ],
    "referrals": _FUNNEL,
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


def _code() -> str:
    """The module with comments stripped — the greps are about what it DOES.

    Its docstring explains at length which APIs it refuses to use; a naive grep
    over the raw file would read those explanations as violations, which pushes
    every future maintainer toward deleting the reasoning to keep the test green.
    """
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


# ─── 13 · The rule that has no exceptions ─────────────────────────────────────
def test_the_referral_card_adds_no_innerhtml_to_the_module():
    """Every string in this card is server data — an invitee's display name, a
    status sentence, an error detail — and a form was just added beside them."""
    code = _code()
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert banned not in code, f"{banned} reached earnings.js"
    # And the card is actually there, so this test cannot pass vacuously.
    assert "referralCard" in code
    assert "asc-ref-card" in code


def test_the_card_builds_its_controls_through_h_and_binds_real_listeners():
    """A control assembled as markup is the one place innerHTML creeps back in.
    Asserted by firing the click, not by reading the source."""
    out = _run_node(_script({
        "/api/asclepius/earnings": _LEDGER,
        "/api/asclepius/referrals": _FUNNEL,
    }, """
var body = document.createElement('div');
window.EarningsSection.render(body, ctx);
done(function () {
  var input = find(body, 'asc-ref-input')[0];
  var button = find(body, 'asc-ref-send')[0];
  input.value = 'colleague@hospital.org';
  input.dispatch('input', {});
  button.dispatch('click', {});
  done(function () { done(function () {
    console.log(JSON.stringify({
      inputTag: input.tagName,
      buttonTag: button.tagName,
      posts: apiCalls.filter(function (c) { return c.method === 'POST'; }),
      refetch: apiCalls.filter(function (c) { return c.url === '/api/asclepius/referrals'
                                                     && c.method === 'GET'; }).length,
      message: find(body, 'asc-ref-msg').map(textOf),
    }));
  }); });
});
"""))
    assert out["inputTag"] == "INPUT"
    assert out["buttonTag"] == "BUTTON"
    assert len(out["posts"]) == 1
    assert out["posts"][0]["url"] == "/api/asclepius/referrals"
    assert out["posts"][0]["body"] == {"email": "colleague@hospital.org"}
    # The funnel is refetched rather than patched locally: a card that renders an
    # optimistic row is a card that can disagree with the ledger beside it.
    assert out["refetch"] >= 1
    assert out["message"], "a successful send must say so"


# ─── 14 · Green earned, and the pending line that IS the design ───────────────
def test_the_referral_line_is_green_and_a_pending_referral_gets_its_own_sub_line():
    """Green means physician-authored value in this product, and a referral is
    the one line on the page earned by a physician's judgment about another
    physician rather than by an hour of their labour.

    The muted sub-line under it is the whole feature: without it a doctor who
    referred someone six weeks ago sees nothing and assumes nothing happened."""
    out = _run_node(_script({
        "/api/asclepius/earnings": _LEDGER,
        "/api/asclepius/referrals": _FUNNEL,
    }, """
var body = document.createElement('div');
window.EarningsSection.render(body, ctx);
done(function () {
  console.log(JSON.stringify({
    lines: find(body, 'asc-pay-line').map(function (e) {
      return { cls: e.className, text: textOf(e) };
    }),
    amounts: find(body, 'asc-ref-amount').map(function (e) {
      return { cls: e.className, text: textOf(e) };
    }),
    states: find(body, 'asc-ref-state').map(function (e) {
      return { cls: e.className, text: textOf(e) };
    }),
    all: classesOf(body).join(' '),
  }));
});
"""))
    referral_line = [l for l in out["lines"] if "asc-pay-line-referral" in l["cls"]]
    assert len(referral_line) == 1, "the referral line must carry its own green class"
    assert "1 × $150" in referral_line[0]["text"]
    assert "$150" in referral_line[0]["text"]

    pending = [l for l in out["lines"] if "asc-pay-line-pending" in l["cls"]]
    assert len(pending) == 1
    assert "1 invited, awaiting their first case" in pending[0]["text"], (
        "a pending referral must render as a SENTENCE, never as a bare count")
    assert "+$150" in pending[0]["text"]

    # In the card: earned is green, in flight is lime, and nothing is pink.
    assert "asc-ref-earned" in out["amounts"][0]["cls"]
    assert out["amounts"][0]["text"] == "$150"
    assert "asc-ref-flight" in out["amounts"][1]["cls"]
    assert "+$150 pending" in out["amounts"][1]["text"]
    assert "asc-ref-flight" in out["states"][1]["cls"]
    assert "pink" not in out["all"].lower()
    assert "critical" not in out["all"].lower()
    assert "danger" not in out["all"].lower()


def test_the_accent_classes_the_card_uses_are_defined_and_never_pink():
    """The class names above only carry meaning if the stylesheet gives them
    one. Green earned, lime in flight, muted grey settled — and a referral that
    has not converted is NOT a safety event, so nothing here is pink."""
    css = _CSS.read_text(encoding="utf-8")
    block = css.split("PRD-REF — refer a physician")[1].split("END PRD-P")[0]
    assert ".asc-ref-earned" in block and "--green-deep" in block
    assert ".asc-ref-flight" in block and "--lime-deep" in block
    assert ".asc-ref-quiet" in block
    assert ".asc-pay-line-referral" in block
    for banned in ("--pink", "pink-deep", "pink-wash", "pink-line"):
        assert banned not in block, f"the referral card reached for {banned}"


def test_a_referral_that_can_never_pay_shows_no_pending_money():
    """Two rows that will never convert — an invitee who was refused
    verification, and one already credited to another referrer. Neither may
    render "+$150 pending", because a promise the funnel cannot keep is the same
    failure as showing nothing, reached from the other direction. Grey, not
    pink: the referrer did nothing wrong."""
    funnel = dict(_FUNNEL, referrals=[
        {"referral_id": "ref-a", "invitee_display": "Dr Turned Down",
         "status": "declined", "status_sentence": "Not verified",
         "bounty_state": "closed", "bounty_cents": 15000,
         "invited_at": "2026-08-02T09:00:00", "resolved_at": "2026-08-20T09:00:00"},
        {"referral_id": "ref-b", "invitee_display": "Dr Popular",
         "status": "approved",
         "status_sentence": "Joined · already credited to another referrer",
         "bounty_state": "duplicate", "bounty_cents": 15000,
         "invited_at": "2026-08-03T09:00:00", "resolved_at": None},
    ], total=2, earned_count=0, earned_cents=0, pending_count=0, pending_cents=0)
    out = _run_node(_script({
        "/api/asclepius/earnings": dict(_LEDGER, referrals=funnel),
        "/api/asclepius/referrals": funnel,
    }, """
var body = document.createElement('div');
window.EarningsSection.render(body, ctx);
done(function () {
  console.log(JSON.stringify({
    amounts: find(body, 'asc-ref-amount').map(function (e) {
      return { cls: e.className, text: textOf(e).trim() };
    }),
    states: find(body, 'asc-ref-state').map(textOf),
    all: classesOf(body).join(' '),
  }));
});
"""))
    assert [a["text"] for a in out["amounts"]] == ["", ""], (
        "a referral that cannot pay must show no figure at all")
    for a in out["amounts"]:
        assert "asc-ref-quiet" in a["cls"]
        assert "asc-ref-flight" not in a["cls"]
    assert "Not verified" in out["states"][0]
    assert "already credited to another referrer" in out["states"][1]
    assert "pink" not in out["all"].lower()


def test_an_earned_row_shows_what_the_ledger_paid_not_the_rate_today():
    """Rates are stamped on the ledger at accrual so a change can never restate a
    past earning. The card is where a doctor would read the restatement, so the
    per-row figure comes from the row and only the forward-looking promise moves
    with the current rate."""
    funnel = dict(_FUNNEL, bounty_cents=30000, referrals=[
        {"referral_id": "ref-1", "invitee_display": "Dr A. Whitfield",
         "status": "approved", "status_sentence": "Completed first case",
         "bounty_state": "earned", "bounty_cents": 15000,
         "invited_at": "2026-08-02T09:00:00", "resolved_at": "2026-08-30T09:00:00"},
        {"referral_id": "ref-2", "invitee_display": "Dr M. Osei",
         "status": "verified", "status_sentence": "Verified · awaiting first case",
         "bounty_state": "pending", "bounty_cents": 30000,
         "invited_at": "2026-08-07T09:00:00", "resolved_at": None},
    ], total=2, earned_count=1, earned_cents=15000,
        pending_count=1, pending_cents=30000)
    out = _run_node(_script({
        "/api/asclepius/earnings": dict(_LEDGER, referrals=funnel),
        "/api/asclepius/referrals": funnel,
    }, """
var body = document.createElement('div');
window.EarningsSection.render(body, ctx);
done(function () {
  console.log(JSON.stringify({
    amounts: find(body, 'asc-ref-amount').map(function (e) { return textOf(e).trim(); }),
    pitch: find(body, 'asc-ref-pitch').map(textOf),
  }));
});
"""))
    assert out["amounts"][0] == "$150", "the earned row was restated at the new rate"
    assert out["amounts"][1] == "+$300 pending"
    # The promise for a referral sent TOMORROW does move — saying otherwise
    # would be the opposite error.
    assert "$300 when a colleague" in out["pitch"][0]


# ─── 15 · No raw token reaches a human ────────────────────────────────────────
def test_no_raw_status_token_reaches_the_dom():
    """A physician should never have to learn our state machine to know whether
    their friend is nearly there. The server sends a sentence; the page must not
    fall back to the token beside it."""
    out = _run_node(_script({
        "/api/asclepius/earnings": _LEDGER,
        "/api/asclepius/referrals": _FUNNEL,
    }, """
var body = document.createElement('div');
window.EarningsSection.render(body, ctx);
done(function () { console.log(JSON.stringify({ text: textOf(body) })); });
"""))
    text = out["text"]
    for token in ("signed_up", "paid_out", "review_session", "bounty_state",
                  "invited_at", "ineligible"):
        assert token not in text, f"the raw token {token!r} reached the DOM"
    # The sentences DID render, so the assertion above is not passing vacuously.
    assert "Completed first case" in text
    assert "Verified · awaiting first case" in text
    # And a third party's raw address never comes back to the referrer.
    assert "j••••@mgh.org" in text


# ─── 16 · The empty state names the next action ───────────────────────────────
def test_the_empty_state_is_a_sentence_and_never_a_blank_card():
    """It is true, it is the strongest thing we can say about our own network,
    and it does the persuading without a call to action shouting at a doctor who
    came here to check their pay."""
    empty = dict(_FUNNEL, referrals=[], total=0, earned_count=0, earned_cents=0,
                 pending_count=0, pending_cents=0)
    ledger = dict(_LEDGER, referrals=empty,
                  lines=[l for l in _LEDGER["lines"] if l["kind"] != "referral"])
    out = _run_node(_script({
        "/api/asclepius/earnings": ledger,
        "/api/asclepius/referrals": empty,
    }, """
var body = document.createElement('div');
window.EarningsSection.render(body, ctx);
done(function () {
  console.log(JSON.stringify({
    cards: find(body, 'asc-ref-card').length,
    empty: find(body, 'asc-ref-empty').map(textOf),
    rows: find(body, 'asc-ref-row').length,
    forms: find(body, 'asc-ref-form').length,
    lines: find(body, 'asc-pay-line').map(textOf),
  }));
});
"""))
    assert out["cards"] == 1, "the card renders even with nothing in the funnel"
    assert out["rows"] == 0
    assert out["forms"] == 1, "the ask is still there — that is the point of the card"
    assert len(out["empty"]) == 1
    assert "No referrals yet" in out["empty"][0]
    assert "came through another physician" in out["empty"][0]
    # No "Referrals 0 × $150 · $0" row: the card does the asking, and a permanent
    # zeroed rate line is the growth-loop instinct this feature resists.
    assert not any("Referrals" in l for l in out["lines"])


# ─── The card is absent where it does not belong ──────────────────────────────
def test_no_card_for_a_physician_who_may_not_refer():
    """An inert form for someone the server will refuse is worse than nothing."""
    blocked = dict(_FUNNEL, can_refer=False)
    out = _run_node(_script({"/api/asclepius/earnings": dict(_LEDGER, referrals=blocked)}, """
var body = document.createElement('div');
window.EarningsSection.render(body, ctx);
done(function () { console.log(JSON.stringify({ cards: find(body, 'asc-ref-card').length })); });
"""))
    assert out["cards"] == 0


def test_an_older_server_that_sends_no_referral_block_still_renders_the_page():
    """The card is additive. A deploy where the frontend lands before the backend
    must not blank a physician's ledger — the money is the load-bearing half."""
    ledger = dict(_LEDGER)
    ledger.pop("referrals")
    ledger["lines"] = [l for l in _LEDGER["lines"] if l["kind"] != "referral"]
    out = _run_node(_script({"/api/asclepius/earnings": ledger}, """
var body = document.createElement('div');
window.EarningsSection.render(body, ctx);
done(function () {
  console.log(JSON.stringify({
    cards: find(body, 'asc-ref-card').length,
    hero: find(body, 'asc-pay-hero-value').map(textOf),
    lines: find(body, 'asc-pay-line').length,
  }));
});
"""))
    assert out["cards"] == 0
    assert out["hero"] == ["$2,625"]
    assert out["lines"] == 2


# ─── The draft survives a re-render ───────────────────────────────────────────
def test_a_half_typed_address_survives_the_session_poll_re_render():
    """This section re-renders on every heartbeat response and on every tick of
    the read-only session poll. Without the draft being held in module state, a
    doctor typing a colleague's address while a review session beats in ANOTHER
    tab watches the field empty itself every fifteen seconds — a bug that only
    appears in the exact situation the card exists for.

    Driven through the real trigger (an open session the ledger reports, then
    firing the poll interval) rather than by calling ``render`` twice: a fresh
    MOUNT is a fresh visit and deliberately does clear the draft, so a test that
    re-mounted would be asserting the opposite of the intended behaviour."""
    ledger = dict(_LEDGER, open_session={
        "session_id": "ws-1", "started_at": "2026-08-03T14:00:00",
        "credited_seconds": 300, "continuous_seconds": 300, "min_seconds": 1200,
        "remaining_seconds": 900, "qualified": False, "rate_cents": 10000,
    })
    out = _run_node(_script({
        "/api/asclepius/earnings": ledger,
        "/api/asclepius/referrals": _FUNNEL,
        "/api/asclepius/sessions/ws-1": {"session_id": "ws-1", "ended": False,
                                         "credited_seconds": 315,
                                         "continuous_seconds": 315,
                                         "min_seconds": 1200, "qualified": False},
    }, """
var body = document.createElement('div');
window.EarningsSection.render(body, ctx);
done(function () {
  var input = find(body, 'asc-ref-input')[0];
  input.value = 'half.typed@hospital.org';
  input.dispatch('input', {});
  var ticks = globalThis.__intervals.filter(Boolean);
  ticks.forEach(function (t) { t.fn(); });
  done(function () { done(function () {
    console.log(JSON.stringify({
      armed: ticks.length,
      value: (find(body, 'asc-ref-input')[0] || {}).value,
      elapsed: find(body, 'asc-pay-session-elapsed').map(textOf),
    }));
  }); });
});
"""))
    assert out["armed"] == 1, "the poll must actually be armed, or this passes vacuously"
    assert out["elapsed"] == ["5:15"], "the poll re-rendered the page"
    assert out["value"] == "half.typed@hospital.org"


def test_a_fresh_mount_does_not_inherit_the_previous_visitors_draft():
    """The other half of the rule. The portal shell never calls ``reset``, so
    without clearing on mount a half-typed colleague outlives a logout and sits
    in the field for whoever signs in next on the same machine."""
    out = _run_node(_script({
        "/api/asclepius/earnings": _LEDGER,
        "/api/asclepius/referrals": _FUNNEL,
    }, """
var body = document.createElement('div');
window.EarningsSection.render(body, ctx);
done(function () {
  var input = find(body, 'asc-ref-input')[0];
  input.value = 'someone.elses@hospital.org';
  input.dispatch('input', {});
  var next = document.createElement('div');
  window.EarningsSection.render(next, ctx);
  done(function () {
    console.log(JSON.stringify({ value: find(next, 'asc-ref-input')[0].value }));
  });
});
"""))
    assert out["value"] == ""


def test_a_failed_send_says_so_instead_of_failing_silently():
    """A silent failure on this control is how a physician concludes the feature
    is broken and never refers again."""
    out = _run_node(_script(
        {"/api/asclepius/earnings": _LEDGER, "/api/asclepius/referrals": _FUNNEL},
        """
var body = document.createElement('div');
window.EarningsSection.render(body, ctx);
done(function () {
  var input = find(body, 'asc-ref-input')[0];
  input.value = 'colleague@hospital.org';
  input.dispatch('input', {});
  find(body, 'asc-ref-send')[0].dispatch('click', {});
  done(function () { done(function () {
    console.log(JSON.stringify({
      errors: find(body, 'asc-ref-error').map(textOf),
      messages: find(body, 'asc-ref-msg').length,
    }));
  }); });
});
""",
        fail={"/api/asclepius/referrals": {"status": 429,
                                           "detail": "Too many invitations sent recently."}}))
    assert len(out["errors"]) == 1
    assert "Too many invitations" in out["errors"][0]
    assert out["messages"] == 0
