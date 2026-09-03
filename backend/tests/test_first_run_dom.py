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


def test_no_stop_ever_shows_two_primaries_or_two_ways_to_do_one_thing():
    """§7: at most one primary action, one quiet skip — and no action offered twice.

    Stop 2 has ZERO primaries on purpose. It asks "where would you like to
    start?" and its two choice cards are the answers, so a black
    "Start the practice case →" underneath them was the right-hand card a second
    time: same words, same destination, and the heaviest element on the screen,
    which is a screen answering its own question. §7's "one primary" is a ceiling
    on emphasis, not a requirement that every stop carry a button.
    """
    out = _run_node(_ctx(user={"first_run": {"version": 1, "stops": {"welcome": "done"}}}) + """
      var seen = [];
      function snapshot(label) {
        seen.push({ stop: label,
                    primaries: find(rootNode, 'asc-btn-primary').map(textOf),
                    choices: find(rootNode, 'asc-fr-choice').map(textOf),
                    skips: find(rootNode, 'asc-fr-skip').length });
      }
      window.FirstRunWalkthrough.start(ctx);
      done(function () { done(function () {
        snapshot('start');
        // The right-hand choice card is the way forward now that this required
        // stop has no skip control.
        find(rootNode, 'asc-fr-choice').slice(-1)[0].dispatch('click');
        snapshot('practice-handoff');
        console.log(JSON.stringify({ seen: seen, handoffs: handoffs }));
      }); });
    """)
    start = out["seen"][0]
    assert len(start["primaries"]) == 0, (
        "stop 2's choice cards ARE the action; a primary here duplicates one of them")
    # Welcome package v2 §4.1: ZERO skips. "Choose your start" is a required
    # stop, and the "Skip for now" that used to sit here wrote a terminal
    # `skipped` — which is how real accounts reached the dashboard having seen
    # neither the demo nor a case.
    assert start["skips"] == 0, "a required stop must render no skip control"
    # The real rule, stated positively: nothing on this screen offers the same
    # action twice. A primary that repeats a card's label is the failure mode.
    labels = [t.strip() for t in start["primaries"]]
    for choice in start["choices"]:
        for label in labels:
            assert label.rstrip(" →") not in choice, (
                f"primary {label!r} repeats the choice card {choice!r}")
    # Choosing "start the practice case" runs the practice case.
    assert "tutorial" in out["handoffs"]


def test_later_stops_still_carry_one_primary_and_one_quiet_skip():
    """The ceiling is one primary; stops 4-6 each spend theirs."""
    out = _run_node(_ctx(user={"first_run": {"version": 1, "stops": {
        "welcome": "done", "start": "done", "practice": "done"}}}) + """
      var seen = [];
      function snapshot(label) {
        seen.push({ stop: label,
                    primaries: find(rootNode, 'asc-btn-primary').length,
                    skips: find(rootNode, 'asc-fr-skip').length });
      }
      window.FirstRunWalkthrough.start(ctx);
      done(function () { done(function () {
        snapshot('community');
        find(rootNode, 'asc-fr-skip')[0].dispatch('click');
        done(function () {
          snapshot('earnings');
          find(rootNode, 'asc-fr-skip')[0].dispatch('click');
          done(function () {
            snapshot('manual');
            console.log(JSON.stringify({ seen: seen }));
          });
        });
      }); });
    """)
    assert [s["stop"] for s in out["seen"]] == ["community", "earnings", "manual"]
    for stop in out["seen"]:
        assert stop["primaries"] == 1, f"{stop['stop']} should have exactly one primary"
        assert stop["skips"] == 1, f"{stop['stop']} should have exactly one quiet skip"


def test_no_stop_prints_its_own_position_beside_the_checklists_count():
    """The checklist counts COMPLETED ("3 of 6"); an eyebrow counted POSITION
    ("Stop 4 of 6"). Two different numbers for the same six things, in the same
    visual register, 400px apart. Only the checklist survives."""
    src = _FIRST_RUN_JS.read_text()
    assert not re.search(r"Stop\s+\d\s+of\s+6'", src), (
        "a stop is printing its own position; the checklist already reports progress")
    # This test used to ban the class `asc-fr-eyebrow` outright, because the only
    # eyebrow that had ever existed was the "Stop 4 of 6" one. Welcome package v2
    # §4.1 gives the name a different job — the tiny OPTIONAL label that splits
    # the checklist's required rows from its optional ones — so the ban narrows
    # to what it was always about: no stop announces its own POSITION anywhere.
    for eyebrow in re.findall(r"asc-fr-eyebrow'[^\n]*", src):
        assert not re.search(r"\d\s+of\s+\d", eyebrow), (
            f"an eyebrow is counting position again: {eyebrow!r}")


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


def test_closing_a_stop_posts_it_and_a_defer_is_recorded_as_a_defer():
    """State is server-side, and putting a stop off is a different fact from
    finishing it.

    The word on the wire is `defer`, not `skip`: Welcome package v2 §1 made the
    outcome non-terminal, and the server refuses either word against a required
    stop. This drives the first OPTIONAL stop, since the required three no
    longer render a control that could send this at all.
    """
    out = _run_node(_ctx(user={"first_run": {"version": 1, "stops": {
        "welcome": "done", "start": "done", "practice": "done"}}}) + """
      window.FirstRunWalkthrough.start(ctx);
      done(function () { done(function () {
        find(rootNode, 'asc-fr-skip')[0].dispatch('click');
        console.log(JSON.stringify({ calls: apiCalls.filter(function (c) {
          return c.path === '/me/first-run';
        }) }));
      }); });
    """)
    assert out["calls"] == [{"path": "/me/first-run", "method": "PATCH",
                            "body": {"action": "defer", "stop": "community"}}]


def test_the_checklist_counts_done_stops_and_marks_deferred_ones_later():
    """The count is COMPLETED work, and a deferred stop is not completed work.

    Under the old model any outcome counted, so a physician who skipped three
    stops read "6 of 6" and the walkthrough never returned. `deferred` is
    progress nobody has made yet: it does not count, and it says `later` rather
    than `skipped`, because it is a thing they have not done — not a thing they
    declined for good.
    """
    out = _run_node(_ctx(user={"first_run": {"version": 1, "stops": {
        "welcome": "done", "start": "done", "practice": "done",
        "community": "deferred"}}}) + """
      window.FirstRunWalkthrough.start(ctx);
      done(function () {
        var list = find(rootNode, 'asc-fr-checklist')[0];
        console.log(JSON.stringify({
          count: textOf(find(list, 'asc-fr-check-count')[0]),
          items: find(list, 'asc-fr-check-item').map(function (li) {
            return { text: textOf(li).trim(),
                     done: li.classList.contains('is-done'),
                     later: li.classList.contains('is-later') };
          }),
          eyebrows: find(list, 'asc-fr-eyebrow').map(textOf),
        }));
      });
    """)
    assert out["count"] == "3 of 6", "a deferred stop must not count as done"
    assert [i["done"] for i in out["items"]] == [True, True, True, False, False, False]
    # The deferred one is visibly a different state from both done and untouched.
    assert out["items"][3]["later"] is True
    assert "later" in out["items"][3]["text"]
    assert "skipped" not in out["items"][3]["text"]
    # §4.1: required rows first, then the optional three under a tiny eyebrow.
    assert out["eyebrows"] == ["OPTIONAL"]


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
          handoffs: handoffs,
          calls: apiCalls.filter(function (c) { return c.path === '/me/first-run'; }),
        }));
      });
    """)
    assert any("calendly.com/tejpatel-berkeley" in (l or "") for l in out["links"])
    assert out["calls"][-1]["body"] == {"action": "defer", "stop": "manual"}
    # Welcome package v2: putting the last stop off does NOT reach "You're all
    # set". That card carries the dismiss — the one control that stops the
    # product ever mentioning onboarding again — and a physician who deferred
    # their way to the end has finished nothing. Congratulating them and then
    # quietly switching off the re-entry cadence they were promised would defeat
    # §2 on the very first login. They leave, and login 2 brings the re-entry
    # page back.
    assert "You’re all set." not in out["finished"]
    assert "exit" in out["handoffs"], "deferring the last stop should leave"
    assert not [c for c in out["calls"] if c["body"].get("action") == "dismiss"]


def test_finishing_the_last_stop_for_real_does_reach_the_all_set_card():
    """The other half of the rule above: `done` on all six earns the finish card
    and the dismiss that goes with it."""
    out = _run_node(_ctx(user={"first_run": {"version": 1, "stops": {
        "welcome": "done", "start": "done", "practice": "done",
        "community": "done", "earnings": "done"}}}) + """
      window.FirstRunWalkthrough.resume(ctx);
      done(function () {
        find(rootNode, 'asc-btn-primary')[0].dispatch('click');
        done(function () {
          console.log(JSON.stringify({ finished: textOf(rootNode), handoffs: handoffs }));
        });
      });
    """)
    # The manual's primary opens the guide panel, which is where "the manual"
    # lives — the stop is done, and the finish card is reached on the next resume.
    assert "guide" in str(out["handoffs"])


def test_the_checklist_card_collapses_to_one_line_when_every_stop_is_closed():
    """§6 stop 6: "the card collapses to a one-line 'You're all set' with
    confetti-free restraint". It is not REMOVED — a checklist that vanishes at
    the moment you finish it takes the sense of having finished with it."""
    probe = """
      window.FirstRunWalkthrough.resume(ctx);
      done(function () {
        var card = find(rootNode, 'asc-fr-checklist')[0];
        console.log(JSON.stringify({
          collapsed: !!card && card.classList.contains('asc-fr-checklist-done'),
          text: card ? textOf(card).trim() : null,
          items: card ? find(card, 'asc-fr-check-item').length : -1,
        }));
      });
    """
    out = _run_node(_ctx(user={"first_run": {"version": 1, "stops": {
        "welcome": "done", "start": "done", "practice": "done", "community": "done",
        "earnings": "done", "manual": "done"}}}) + probe)
    assert out["collapsed"], "the checklist did not collapse"
    assert "You’re all set" in out["text"]
    assert out["items"] == 0
    # No exclamation, no count, no confetti.
    assert "!" not in out["text"]

    # ...and it does NOT collapse on a deferred stop. This case read as finished
    # under the old model — every stop carried an outcome, so the card collapsed,
    # the count said 6 of 6, and the walkthrough never came back. That is the bug
    # Welcome package v2 §1 exists to fix, so it is pinned here as well as in the
    # count test.
    out = _run_node(_ctx(user={"first_run": {"version": 1, "stops": {
        "welcome": "done", "start": "done", "practice": "done", "community": "done",
        "earnings": "done", "manual": "deferred"}}}) + probe)
    assert not out["collapsed"], "a deferred stop is not a finished checklist"


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
      var ALL = { welcome: 'done', start: 'done', practice: 'done',
                  community: 'done', earnings: 'done', manual: 'done' };
      console.log(JSON.stringify({
        fresh: W.shouldRun({ first_run: { version: 1, stops: {} } }),
        partial: W.shouldRun({ first_run: { version: 1, stops: { welcome: 'done' } } }),
        dismissed: W.shouldRun({ first_run: { stops: {}, dismissed_at: '2026-01-01' } }),
        allDone: W.shouldRun({ first_run: { stops: ALL } }),
        noPayload: W.shouldRun({}),
        // 'skipped' is the PREVIOUS bundle's word, and a payload cached from
        // before the deploy can still carry it. A required skip is not progress
        // (they must actually do it); an optional one reads as deferred, which
        // is also not progress. Either way this counts 1.
        progress: W.progress({ first_run: { stops: { welcome: 'done', start: 'skipped' } } }),
        progressLegacyOptional: W.progress({
          first_run: { stops: { welcome: 'done', community: 'skipped' } } }),
      }));
    """)
    assert out["fresh"] is True and out["partial"] is True
    assert out["dismissed"] is False
    # Every stop genuinely done is the only thing that means "nothing to show".
    assert out["allDone"] is False
    # An account whose payload is missing entirely (a very old row) still gets
    # the walkthrough rather than silently never seeing it.
    assert out["noPayload"] is True
    # The old assertion here was {"done": 2}: it counted a skip as progress,
    # which is exactly how a physician who skipped the practice case read "6 of
    # 6" and was never asked again.
    assert out["progress"] == {"done": 1, "total": 6}
    assert out["progressLegacyOptional"] == {"done": 1, "total": 6}


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
    walkthrough = js.index("if (frMode === 'walkthrough') { startFirstRun(); return; }")
    assert rotate < walkthrough


def test_the_walkthrough_is_offered_to_physicians_only():
    """Admins, QA reviewers and advisors do not get a first-login walkthrough.

    The role check is the SHELL's, not the module's — the module only answers
    the checklist question — so it is asserted on the one predicate both the
    entry gate and the dashboard chip go through.
    """
    js = _PORTAL_JS.read_text(encoding="utf-8")
    # Welcome package v2 §2 moved the routing decision from a boolean to
    # `firstRunMode()`; the role question stayed exactly where it was, on the
    # shell side, and this is still the one predicate everything goes through.
    body = js[js.index("function firstRunMode()"):]
    body = body[:body.index("\n  }") + 4]
    assert "state.user.role !== 'evaluator'" in body
    assert "isAdvisor()" in body
    assert "window.FirstRunWalkthrough.mode(state.user)" in body
    # The chip still derives from it rather than re-deriving the rule...
    assert "function firstRunPending()" in js
    assert "return firstRunMode() !== 'none';" in js
    assert "if (!firstRunPending()) return null;" in js
    # ...and so does every entry point, including the two new ones.
    assert "const frMode = firstRunMode();" in js
    assert "if (frMode === 'walkthrough') { startFirstRun(); return; }" in js
    assert "if (frMode === 'reentry') { openFirstRunReentry(); return; }" in js


def test_the_review_deep_link_is_read_before_the_walkthrough_opens():
    """`#review` comes from a link we already emailed. Returning early on the
    walkthrough would both ignore it and leave it in the URL to fire on some
    later reload."""
    js = _PORTAL_JS.read_text(encoding="utf-8")
    assert js.index("readReviewHash()") < js.index("const frMode = firstRunMode();")


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


# ── the demo player reserves its own frame ───────────────────────────────────
def test_the_video_element_reserves_its_frame_before_metadata_loads():
    """A <video> has no intrinsic height until the browser has read the moov atom
    off the file. On a 73 MB demo over a physician's connection that is a real
    window, during which the player renders as a thin strip of controls and then
    jumps to full size — a layout shift on the first thing a new physician sees.
    An aspect-ratio reserves the box immediately."""
    css = _CSS.read_text(encoding="utf-8")
    block = css[css.index(".asc-fr-video {"):]
    block = block[:block.index("}")]
    assert "aspect-ratio" in block


def test_the_demo_close_button_sits_inside_the_frame():
    """At top/right -14px it hung off the card and landed on whatever the overlay
    was centred over — in the six-stop layout, the setup checklist. A close button
    floating on unrelated content reads as belonging to that content."""
    css = _CSS.read_text(encoding="utf-8")
    block = css[css.index(".asc-fr-demo-close {"):]
    block = block[:block.index("}")]
    offsets = re.findall(r"(?:top|right):\s*(-?[\d.]+)px", block)
    assert offsets, "the close button is absolutely positioned; it needs offsets"
    assert all(float(v) >= 0 for v in offsets), (
        f"negative offsets put the button outside the frame: {offsets}")
    # Moving it inside the frame puts it over the <video>, which is replaced
    # content and paints over a positioned sibling carrying no stack order — so
    # the button becomes not merely invisible but UNCLICKABLE (verified in
    # Chromium: elementFromPoint over its centre returned the video). The two
    # changes only work together, so they are asserted together.
    assert "z-index" in block, (
        "a close button inside the frame must out-stack the video it sits on")


def test_the_resume_chip_does_not_stretch_to_the_content_width():
    """`.asc-wrap` is a flex COLUMN whose default align-items:stretch beats the
    chip's inline-flex, turning a quiet pill into a full-bleed 1180px bar with two
    words at the far left — which reads as a broken empty banner."""
    css = _CSS.read_text(encoding="utf-8")
    block = css[css.index(".asc-fr-chip {"):]
    block = block[:block.index("}")]
    assert "align-self: flex-start" in block


def test_the_two_choice_cards_on_stop_two_share_one_accent():
    """Green means physician-verified and lime means needs attention. Neither
    describes "a video" or "a practice case", and colouring two equal options
    differently says one of them carries weight the other does not."""
    js = _FIRST_RUN_JS.read_text(encoding="utf-8")
    thumbs = re.findall(r"'(asc-fr-choice-thumb[^']*)'", js)
    assert len(thumbs) == 2, f"expected two choice thumbs, found {thumbs}"
    assert thumbs[0] == thumbs[1], f"peer cards carry different accents: {thumbs}"

