"""PRD-REF — the Referral tab renders, and renders honestly.

Source-grepping a frontend module proves it was written, not that it works, so
these tests execute ``referral.js`` against the DOM shim and assert what lands
in the document. Ported from the earnings-card suite when the surface moved to
its own tab, plus the tab's additions: the hero quotes the payout structure
from the WIRE (never a hardcoded dollar), the link row carries a copy control,
the funnel is sentences rather than tokens, and the enterprise note posts to
its endpoint.
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
_REFERRAL_JS = _FRONTEND / "referral.js"
_EARNINGS_JS = _FRONTEND / "earnings.js"
_PORTAL_JS = _FRONTEND / "asclepius.js"
_INDEX = _FRONTEND / "index.html"
_CSS = _FRONTEND / "asclepius.css"


def _run_node(script: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed in this environment")
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\n{proc.stderr}\n{proc.stdout}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


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
globalThis.navigator = globalThis.navigator || {};
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
window.ReferralSection.reset();
"""

_FUNNEL = {
    "can_refer": True,
    "earns_bounty": True,
    "bounty_cents": 5000,
    "referral_code": "ABCD2345",
    "invite_url": "https://example.com/join?ref=ABCD2345",
    "referrals": [
        {"referral_id": "ref-1", "invitee_display": "Dr A. Whitfield",
         "status": "approved", "status_sentence": "Completed first case",
         "bounty_state": "earned", "bounty_cents": 5000,
         "invited_at": "2026-08-02T09:00:00", "resolved_at": "2026-08-30T09:00:00",
         "first_case_at": "2026-08-30T09:00:00"},
        {"referral_id": "ref-2", "invitee_display": "Dr M. Osei",
         "status": "verified", "status_sentence": "Verified · awaiting first case",
         "bounty_state": "pending", "bounty_cents": 5000,
         "invited_at": "2026-08-07T09:00:00", "resolved_at": None,
         "first_case_at": None},
        {"referral_id": "ref-3", "invitee_display": "j••••@mgh.org",
         "status": "invited", "status_sentence": "Invited",
         "bounty_state": "pending", "bounty_cents": 5000,
         "invited_at": "2026-08-09T09:00:00", "resolved_at": None,
         "first_case_at": None},
    ],
    "total": 3, "earned_count": 1, "earned_cents": 5000,
    "pending_count": 2, "pending_cents": 10000,
    "payout_structure": {"referrer_bounty_cents": 5000,
                         "referee_bonus_cents": 2500,
                         "cap_cents": 520000},
    "cap_cents": 520000,
    "capped": False,
}


def _script(routes: dict, tail: str, fail: dict | None = None) -> str:
    return (_JS_CTX % {"shim": json.dumps(str(_DOM_SHIM)),
                       "module": json.dumps(str(_REFERRAL_JS)),
                       "routes": json.dumps(routes),
                       "fail": json.dumps(fail or {})}) + tail


_RENDER = """
var body = document.createElement('div');
window.ReferralSection.render(body, ctx);
done(function () {
"""


def _render_and(collect_js: str, funnel: dict | None = None, fail: dict | None = None) -> dict:
    routes = {"/api/asclepius/referrals": funnel if funnel is not None else _FUNNEL}
    return _run_node(_script(routes, _RENDER + collect_js + "\n});", fail))


def _code(path: Path) -> str:
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


# ─── Registration ─────────────────────────────────────────────────────────────
def test_the_module_is_registered_and_routed():
    index = _INDEX.read_text(encoding="utf-8")
    assert "referral.js" in index
    portal = _PORTAL_JS.read_text(encoding="utf-8")
    assert "dest: 'referral'" in portal
    assert "window.ReferralSection" in portal
    assert "capability: 'refer'" in portal


def test_no_innerhtml_and_no_long_dashes_in_the_copy():
    """Comment-stripped, so the check covers every string a physician can see
    (house style bans long dashes in copy) without policing prose comments."""
    code = _code(_REFERRAL_JS)
    assert "innerHTML" not in code
    assert "—" not in code and "–" not in code


# ─── The hero ─────────────────────────────────────────────────────────────────
def test_the_hero_quotes_the_wire_structure_not_a_hardcoded_dollar():
    out = _render_and("""
  console.log(JSON.stringify({
    text: textOf(body),
    heroes: find(body, 'asc-ref-hero-value').map(textOf),
  }));
""")
    assert "Earn up to $5,200" in out["heroes"][0]
    assert "$50 to you" in out["text"]
    assert "$25 to them" in out["text"]


def test_a_changed_env_rate_changes_the_page_with_no_frontend_edit():
    funnel = dict(_FUNNEL, payout_structure={"referrer_bounty_cents": 7500,
                                             "referee_bonus_cents": 1000,
                                             "cap_cents": 780000})
    out = _render_and("console.log(JSON.stringify({text: textOf(body)}));", funnel)
    assert "$7,800" in out["text"] and "$75 to you" in out["text"]


# ─── The link ─────────────────────────────────────────────────────────────────
def test_the_link_and_code_render_with_a_copy_control():
    out = _render_and("""
  var links = find(body, 'asc-ref-linktext').map(textOf);
  var buttons = tagsOf(body, 'BUTTON').map(textOf);
  console.log(JSON.stringify({links: links, buttons: buttons, text: textOf(body)}));
""")
    assert any("join?ref=ABCD2345" in t for t in out["links"])
    assert any("Copy link" in b for b in out["buttons"])
    assert "ABCD2345" in out["text"]


# ─── The funnel ───────────────────────────────────────────────────────────────
def test_pending_referrals_render_their_own_money_line_never_absence():
    out = _render_and("""
  console.log(JSON.stringify({
    amounts: find(body, 'asc-ref-amount').map(textOf),
    states: find(body, 'asc-ref-state').map(textOf),
  }));
""")
    assert sum("pending" in a for a in out["amounts"]) == 2
    assert "Verified · awaiting first case" in " ".join(out["states"])


def test_no_raw_status_token_reaches_the_dom():
    out = _render_and("console.log(JSON.stringify({text: textOf(body)}));")
    assert "signed_up" not in out["text"]
    assert "bounty_state" not in out["text"]


def test_the_empty_state_is_a_sentence():
    funnel = dict(_FUNNEL, referrals=[], total=0, pending_count=0, earned_count=0)
    out = _render_and("console.log(JSON.stringify({empty: find(body, 'asc-ref-empty').map(textOf)}));", funnel)
    assert "another" in out["empty"][0]


def test_a_load_failure_is_a_visible_error_never_a_blank():
    out = _run_node(_script(
        {}, _RENDER + "console.log(JSON.stringify({errs: find(body, 'asc-inline-error').map(textOf)}));\n});",
        fail={"/api/asclepius/referrals": {"status": 500, "detail": "boom"}}))
    assert out["errs"] and "could not be loaded" in out["errs"][0]


# ─── The composer ─────────────────────────────────────────────────────────────
def test_sending_an_invite_posts_and_refetches_the_funnel():
    out = _render_and("""
  var input = tagsOf(body, 'INPUT')[0];
  input.value = 'colleague@hospital.org';
  var buttons = tagsOf(body, 'BUTTON').filter(function (b) {
    return textOf(b).indexOf('Send invitation') !== -1; });
  buttons[0].dispatch('click');
  done(function () { done(function () {
    console.log(JSON.stringify({calls: apiCalls}));
  }); });
""")
    posts = [c for c in out["calls"] if c["method"] == "POST"]
    assert posts and posts[0]["path"] == "/referrals"
    assert posts[0]["body"]["email"] == "colleague@hospital.org"
    gets = [c for c in out["calls"] if c["method"] == "GET"]
    assert len(gets) >= 2  # initial load + post-send refetch


# ─── The enterprise note ──────────────────────────────────────────────────────
def test_the_enterprise_note_posts_to_its_endpoint():
    routes = {"/api/asclepius/referrals": _FUNNEL,
              "/api/asclepius/referrals/enterprise-note": {"ok": True, "message": "Sent."}}
    out = _run_node(_script(routes, _RENDER + """
  var ta = tagsOf(body, 'TEXTAREA')[0];
  ta.value = 'Our CMIO wants to talk about a data partnership.';
  var buttons = tagsOf(body, 'BUTTON').filter(function (b) {
    return textOf(b).indexOf('Send the note') !== -1; });
  buttons[0].dispatch('click');
  done(function () {
    console.log(JSON.stringify({calls: apiCalls, msgs: find(body, 'asc-ref-msg').map(textOf)}));
  });
});
"""))
    posts = [c for c in out["calls"] if c["method"] == "POST"]
    assert posts and posts[0]["path"] == "/referrals/enterprise-note"
    assert "CMIO" in posts[0]["body"]["note"]


def test_the_health_system_pitch_names_large_payouts():
    out = _render_and("console.log(JSON.stringify({text: textOf(body)}));")
    assert "health system" in out["text"].lower()
    assert "large payouts" in out["text"]


# ─── The accent rule ──────────────────────────────────────────────────────────
def test_the_accent_classes_are_defined_and_never_pink():
    css = _CSS.read_text(encoding="utf-8")
    for cls in ("asc-ref-hero-value", "asc-ref-structure-row", "asc-ref-linktext",
                "asc-ref-earned", "asc-ref-flight", "asc-ref-quiet"):
        assert f".{cls}" in css, cls
    import re
    for m in re.finditer(r"\.asc-ref-[a-z-]+\s*\{([^}]*)\}", css):
        assert "--pink" not in m.group(1), m.group(0)


# ─── The earnings pointer ─────────────────────────────────────────────────────
def test_earnings_now_points_at_the_referral_tab():
    code = _code(_EARNINGS_JS)
    assert "Referral tab" in _EARNINGS_JS.read_text(encoding="utf-8")
    # The composer left with the surface: no referral POST remains in earnings.
    assert "'/referrals', { method: 'POST'" not in code
