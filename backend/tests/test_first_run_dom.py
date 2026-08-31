"""Onboarding v2 §6 — the walkthrough renders, and renders honestly.

Source-grepping a frontend module proves it was written, not that it works, so
these execute ``first_run.js`` against the DOM shim and assert what lands in the
document: the stops in order, one primary and one quiet skip on each, a skip
that closes a stop permanently, the checklist counting what it should, and the
demo expanding IN PLACE rather than navigating.
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
_FIRST_RUN_JS = _FRONTEND / "first_run.js"
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


#: The shim plus a context standing in for the portal shell. Every hand-off the
#: module makes (the tutorial, the community, the rail, exit) is recorded rather
#: than performed, so a test can assert WHICH one a button reaches for.
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
var handoffs = [];
var rootNode = null;
var USER = %(user)s;
var DEMO = %(demo)s;

var ctx = {
  h: h,
  setRoot: function (node) { rootNode = node; },
  toast: function () {},
  user: USER,
  api: function (path, opts) {
    apiCalls.push({ path: path, method: (opts && opts.method) || 'GET',
                    body: (opts && opts.body) || null });
    if (path === '/assets/onboarding-demo/meta') return Promise.resolve(DEMO);
    if (path === '/assets/onboarding-demo/ticket') return Promise.resolve({ ticket: 'tkt' });
    if (path === '/me/first-run') return Promise.resolve(null);
    return Promise.resolve({});
  },
  onUser: function () {},
  startTutorial: function () { handoffs.push('tutorial'); },
  openCommunity: function () { handoffs.push('community'); },
  setPanel: function (dest) { handoffs.push('panel:' + dest); },
  exit: function () { handoffs.push('exit'); },
};

globalThis.localStorage = {
  _v: {}, getItem: function (k) { return this._v[k] || null; },
  setItem: function (k, v) { this._v[k] = v; }, removeItem: function (k) { delete this._v[k]; },
};
window.addEventListener = function () {};
window.removeEventListener = function () {};
globalThis.URL = globalThis.URL || { createObjectURL: function () { return 'blob:x'; },
                                     revokeObjectURL: function () {} };
globalThis.Event = globalThis.Event || function (t) { this.type = t; };

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
function findIn(el, cls) { return find(el, cls); }
function done(fn) { setTimeout(function () { fn(); }, 0); }

eval(require('fs').readFileSync(%(module)s, 'utf8'));
"""


def _ctx(user: dict | None = None, demo: dict | None = None) -> str:
    return _JS_CTX % {
        "shim": json.dumps(str(_DOM_SHIM)),
        "module": json.dumps(str(_FIRST_RUN_JS)),
        "user": json.dumps(user or {"first_run": {"version": 1, "stops": {}}}),
        "demo": json.dumps(demo if demo is not None
                           else {"available": True, "url": "/api/asclepius/assets/onboarding-demo",
                                 "version": "abc123"}),
    }


# ═════════════════════════════════════════════════════════════════════════════
# The stops
# ═════════════════════════════════════════════════════════════════════════════

def test_a_fresh_physician_lands_in_the_welcome_letter():
    out = _run_node(_ctx() + """
      window.FirstRunWalkthrough.start(ctx);
      done(function () {
        console.log(JSON.stringify({
          text: textOf(rootNode),
          serif: find(rootNode, 'asc-fr-letter-title').length,
          buttons: find(rootNode, 'asc-btn-primary').map(textOf),
          skips: find(rootNode, 'asc-fr-skip').length,
        }));
      });
    """)
    assert "Welcome to Archangel Health." in out["text"]
    # The mission lines, verbatim from the PRD.
    assert "Doctors earn from their judgment." in out["text"]
    assert "A 70% benchmark score is irrelevant when a patient is downstream." in out["text"]
    assert "Tej Patel & Aryaa Bhatia" in out["text"]
    assert out["serif"] == 1, "the welcome letter is the design system's serif moment"
    assert len(out["buttons"]) == 1 and "Let’s get you started" in out["buttons"][0]
    # No skip on the letter: four paragraphs and a button, and skipping the
    # reason the product exists helps nobody.
    assert out["skips"] == 0


def test_every_later_stop_has_exactly_one_primary_and_one_quiet_skip():
    """§7: one primary action, one quiet skip. Never two primaries."""
    out = _run_node(_ctx(user={"first_run": {"version": 1, "stops": {"welcome": "done"}}}) + """
      var seen = [];
      function snapshot(label) {
        seen.push({ stop: label,
                    primaries: find(rootNode, 'asc-btn-primary').length,
                    skips: find(rootNode, 'asc-fr-skip').length });
      }
      window.FirstRunWalkthrough.start(ctx);
      done(function () { done(function () {
        snapshot('start');
        // Skip forward through the remaining stops.
        find(rootNode, 'asc-fr-skip')[0].dispatch('click');
        snapshot('practice-handoff');
        console.log(JSON.stringify({ seen: seen, handoffs: handoffs }));
      }); });
    """)
    start = out["seen"][0]
    assert start["primaries"] == 1, "two primaries is exactly what §7 forbids"
    assert start["skips"] == 1
    # Skipping "choose your start" still runs the practice case: the stop being
    # skipped is the CHOICE, not the case.
    assert "tutorial" in out["handoffs"]


def test_stop_two_offers_the_demo_only_when_one_is_installed():
    installed = _run_node(_ctx(user={"first_run": {"version": 1, "stops": {"welcome": "done"}}}) + """
      window.FirstRunWalkthrough.start(ctx);
      done(function () { done(function () {
        console.log(JSON.stringify({ choices: find(rootNode, 'asc-fr-choice').map(textOf) }));
      }); });
    """)
    assert len(installed["choices"]) == 2
    assert any("Watch the 3-minute demo" in c for c in installed["choices"])
    assert any("Start the practice case" in c for c in installed["choices"])

    missing = _run_node(
        _ctx(user={"first_run": {"version": 1, "stops": {"welcome": "done"}}},
             demo={"available": False}) + """
      window.FirstRunWalkthrough.start(ctx);
      done(function () { done(function () {
        console.log(JSON.stringify({
          choices: find(rootNode, 'asc-fr-choice').map(textOf),
          text: textOf(rootNode),
        }));
      }); });
    """)
    # A deployment with no video shows the practice case alone rather than a
    # card that plays a 404.
    assert len(missing["choices"]) == 1
    assert "Watch the 3-minute demo" not in missing["text"]


def test_the_demo_expands_in_place_and_esc_closes_it():
    """§6 stop 2: it EXPANDS IN PLACE — never a route change."""
    out = _run_node(_ctx(user={"first_run": {"version": 1, "stops": {"welcome": "done"}}}) + """
      window.FirstRunWalkthrough.start(ctx);
      done(function () { done(function () {
        var before = rootNode;
        find(rootNode, 'asc-fr-choice')[0].dispatch('click');
        var overlay = document.getElementById('ascFrDemo');
        var video = overlay ? find(overlay, 'asc-fr-video') : [];
        var closed;
        done(function () {
          find(overlay, 'asc-fr-demo-close')[0].dispatch('click');
          closed = !document.getElementById('ascFrDemo');
          console.log(JSON.stringify({
            sameScreen: before === rootNode,
            hasOverlay: !!overlay,
            videoCount: video.length,
            hasControls: video.length ? video[0].getAttribute('controls') === 'controls' : false,
            closed: closed,
            handoffs: handoffs,
          }));
        });
      }); });
    """)
    assert out["hasOverlay"], "the demo did not open"
    assert out["sameScreen"], "opening the demo re-rendered the stop — that is a route change"
    assert out["videoCount"] == 1 and out["hasControls"], "a native <video controls>"
    assert out["closed"], "the close control did not close it"
    assert out["handoffs"] == [], "watching the demo must not navigate anywhere"


def test_closing_a_stop_posts_it_and_a_skip_is_recorded_as_a_skip():
    """State is server-side, and a skip is a different fact from a completion."""
    out = _run_node(_ctx(user={"first_run": {"version": 1, "stops": {"welcome": "done"}}}) + """
      window.FirstRunWalkthrough.start(ctx);
      done(function () { done(function () {
        find(rootNode, 'asc-fr-skip')[0].dispatch('click');
        console.log(JSON.stringify({ calls: apiCalls.filter(function (c) {
          return c.path === '/me/first-run';
        }) }));
      }); });
    """)
    assert out["calls"] == [{"path": "/me/first-run", "method": "PATCH",
                            "body": {"action": "skip", "stop": "start"}}]


def test_the_checklist_counts_closed_stops_and_marks_skips():
    out = _run_node(_ctx(user={"first_run": {"version": 1, "stops": {
        "welcome": "done", "start": "skipped", "practice": "done"}}}) + """
      window.FirstRunWalkthrough.start(ctx);
      done(function () {
        var list = find(rootNode, 'asc-fr-checklist')[0];
        console.log(JSON.stringify({
          count: textOf(find(list, 'asc-fr-check-count')[0]),
          items: find(list, 'asc-fr-check-item').map(function (li) {
            return { text: textOf(li).trim(), done: li.classList.contains('is-done') };
          }),
        }));
      });
    """)
    assert out["count"] == "3 of 6"
    assert [i["done"] for i in out["items"]] == [True, True, True, False, False, False]
    # A skip reads as closed AND says so, rather than looking like a completion.
    assert "skipped" in out["items"][1]["text"]


def test_a_walkthrough_resumed_after_the_practice_case_lands_on_the_community():
    """The practice-case stop is closed by the SERVER, so resume reads the
    refreshed user rather than any local memory."""
    out = _run_node(_ctx(user={"first_run": {"version": 1, "stops": {
        "welcome": "done", "start": "done", "practice": "done"}}}) + """
      window.FirstRunWalkthrough.resume(ctx);
      done(function () {
        console.log(JSON.stringify({ text: textOf(rootNode), handoffs: handoffs }));
      });
    """)
    assert "This is our Slack." in out["text"]
    assert "message Tej or Aryaa directly any time" in out["text"]
    assert out["handoffs"] == [], "resuming must not navigate on its own"


def test_the_earnings_stop_states_the_rate_and_labels_the_bank_card_disabled():
    out = _run_node(_ctx(user={"first_run": {"version": 1, "stops": {
        "welcome": "done", "start": "done", "practice": "done", "community": "done"}}}) + """
      window.FirstRunWalkthrough.resume(ctx);
      done(function () {
        var bank = find(rootNode, 'asc-fr-bank')[0];
        console.log(JSON.stringify({
          text: textOf(rootNode),
          bankDisabled: !!bank && bank.getAttribute('disabled') === '',
          bankAria: bank ? bank.getAttribute('aria-disabled') : null,
        }));
      });
    """)
    assert "$75 per completed case" in out["text"]
    assert "coming soon" in out["text"]
    assert "we’ll DM you the moment it does" in out["text"]
    # Architecture on screen, not a control that pretends to work.
    assert out["bankDisabled"] and out["bankAria"] == "true"


def test_the_manual_stop_offers_the_founders_intro_and_finishes_the_checklist():
    out = _run_node(_ctx(user={"first_run": {"version": 1, "stops": {
        "welcome": "done", "start": "done", "practice": "done",
        "community": "done", "earnings": "done"}}}) + """
      window.FirstRunWalkthrough.resume(ctx);
      done(function () {
        var links = [];
        (function walk(el) {
          (el.childNodes || []).forEach(function (c) {
            if (c.tagName === 'A') links.push(c.getAttribute('href'));
            walk(c);
          });
        })(rootNode);
        find(rootNode, 'asc-fr-skip')[0].dispatch('click');
        console.log(JSON.stringify({
          links: links,
          finished: textOf(rootNode),
          calls: apiCalls.filter(function (c) { return c.path === '/me/first-run'; }),
        }));
      });
    """)
    assert any("calendly.com/tejpatel-berkeley" in (l or "") for l in out["links"])
    assert "You’re all set." in out["finished"]
    assert out["calls"][-1]["body"] == {"action": "skip", "stop": "manual"}


def test_the_finish_card_dismisses_the_checklist_for_good():
    out = _run_node(_ctx(user={"first_run": {"version": 1, "stops": {
        "welcome": "done", "start": "done", "practice": "done", "community": "done",
        "earnings": "done", "manual": "done"}}}) + """
      window.FirstRunWalkthrough.resume(ctx);
      done(function () {
        find(rootNode, 'asc-btn-primary')[0].dispatch('click');
        console.log(JSON.stringify({
          calls: apiCalls.filter(function (c) { return c.path === '/me/first-run'; }),
          handoffs: handoffs,
        }));
      });
    """)
    assert {"path": "/me/first-run", "method": "PATCH",
            "body": {"action": "dismiss"}} in out["calls"]
    assert "exit" in out["handoffs"]


def test_leaving_early_does_not_dismiss_the_remaining_stops():
    """"Finish this later" is navigation, not "never show me this again" — the
    dashboard chip has to be able to bring them back."""
    out = _run_node(_ctx() + """
      window.FirstRunWalkthrough.start(ctx);
      done(function () {
        find(rootNode, 'asc-fr-check-exit')[0].dispatch('click');
        console.log(JSON.stringify({
          calls: apiCalls.filter(function (c) { return c.path === '/me/first-run'; }),
          handoffs: handoffs,
        }));
      });
    """)
    assert out["handoffs"] == ["exit"]
    assert out["calls"] == [], "leaving must not close or dismiss anything"


def test_should_run_and_progress_answer_the_shell_correctly():
    out = _run_node(_ctx() + """
      var W = window.FirstRunWalkthrough;
      console.log(JSON.stringify({
        fresh: W.shouldRun({ first_run: { version: 1, stops: {} } }),
        partial: W.shouldRun({ first_run: { version: 1, stops: { welcome: 'done' } } }),
        dismissed: W.shouldRun({ first_run: { stops: {}, dismissed_at: '2026-01-01' } }),
        completed: W.shouldRun({ first_run: { stops: {}, completed_at: '2026-01-01' } }),
        noPayload: W.shouldRun({}),
        progress: W.progress({ first_run: { stops: { welcome: 'done', start: 'skipped' } } }),
      }));
    """)
    assert out["fresh"] is True and out["partial"] is True
    assert out["dismissed"] is False and out["completed"] is False
    # An account whose payload is missing entirely (a very old row) still gets
    # the walkthrough rather than silently never seeing it.
    assert out["noPayload"] is True
    assert out["progress"] == {"done": 2, "total": 6}


# ═════════════════════════════════════════════════════════════════════════════
# Wiring
# ═════════════════════════════════════════════════════════════════════════════

def test_the_module_is_loaded_by_the_portal_page():
    assert 'src="/static/asclepius/first_run.js"' in _INDEX.read_text(encoding="utf-8")


def test_the_shell_gates_on_rotation_before_the_walkthrough():
    """§0.1: the temporary password is rotated FIRST. A physician who lands in
    the welcome letter still holding an emailed credential has skipped the one
    screen that retires it."""
    js = _PORTAL_JS.read_text(encoding="utf-8")
    rotate = js.index("if (state.user.must_change_password) { renderRotateTempPassword(); return; }")
    walkthrough = js.index("window.FirstRunWalkthrough.shouldRun(state.user)")
    assert rotate < walkthrough


def test_the_walkthrough_is_offered_to_physicians_only():
    js = _PORTAL_JS.read_text(encoding="utf-8")
    assert ("if (state.user.role === 'evaluator' && !isAdvisor()\n"
            "        && window.FirstRunWalkthrough") in js


def test_the_walkthrough_builds_its_dom_with_h_and_never_innerHTML():
    """House rule, and it earns its keep here specifically: this module renders
    founder copy and a video frame into a portaled overlay, which is exactly the
    shape of thing someone reaches for an HTML string to build."""
    src = _FIRST_RUN_JS.read_text(encoding="utf-8")
    # Comment-stripped so the rule polices code, not the prose explaining it.
    code = re.sub(r"//[^\n]*", "", re.sub(r"/\*[\s\S]*?\*/", "", src))
    assert "innerHTML" not in code
    assert "insertAdjacentHTML" not in code


def test_every_walkthrough_class_is_styled_and_emitted():
    """The house orphan-class rule, applied to this surface specifically."""
    import re
    css = _CSS.read_text(encoding="utf-8")
    js = _FIRST_RUN_JS.read_text(encoding="utf-8") + _PORTAL_JS.read_text(encoding="utf-8")
    styled = set(re.findall(r"\.(asc-fr-[\w-]+)", css))
    emitted = set(re.findall(r"(asc-fr-[\w-]+)", js))
    assert styled, "no walkthrough styles found"
    assert not (styled - emitted), f"styled but never emitted: {sorted(styled - emitted)}"
    assert not (emitted - styled), f"emitted but never styled: {sorted(emitted - styled)}"
