"""PRD R Phase 4 — the TR page actually renders, and renders the right thing.

Source-grepping a frontend module proves it was written, not that it works. This
repo has already paid for that lesson: a surface can be complete, correct and
INVISIBLE for a whole build round because nothing mounted it and the failure was
quiet. So these tests execute ``review.js`` against the DOM shim and assert what
lands in the document — including the failure case, which is part of the contract
rather than a nicety.

The properties under test are the ones §5 says are load-bearing:

  * both cards are GREEN and neither is orange — the accent carries meaning;
  * ``.asc-answers`` contains EXACTLY the two cards — a third child lands in
    cell 2 and pushes B to row 2, which has already shipped as a bug once;
  * the countdown's value comes from the API response, never from the client;
  * a failed draw renders a VISIBLE error, never a silent placeholder.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
_DOM_SHIM = Path(__file__).resolve().parent / "_asclepius_dom.js"
_REVIEW_JS = _FRONTEND / "review.js"
_REVIEW_HTML = _FRONTEND / "review.html"
_CSS = _FRONTEND / "asclepius.css"


def _run_node(script: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed in this environment")
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\n{proc.stderr}\n{proc.stdout}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


# The browser globals review.js touches that the shared shim does not provide.
# Installed here rather than in the shim so the shim stays exactly what the other
# DOM suites already depend on.
_HARNESS = """
require(%(shim)s);
const store = { asclepius_token: 'tok-1' };
globalThis.localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
};
const timers = [];
globalThis.setInterval = (fn) => { timers.push(fn); return timers.length; };
globalThis.clearInterval = () => {};
globalThis.__timers = timers;

const ROUTES = %(routes)s;
const calls = [];
globalThis.fetch = function (url, opts) {
  calls.push({ url: url, method: (opts && opts.method) || 'GET',
               body: opts && opts.body ? JSON.parse(opts.body) : null });
  const path = String(url).replace('/api/asclepius', '');
  const hit = ROUTES[path];
  if (!hit) return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
  return Promise.resolve({
    ok: hit.status < 400, status: hit.status,
    json: () => Promise.resolve(hit.body),
  });
};
globalThis.__calls = calls;

const root = document.createElement('div');
root.id = 'reviewRoot';
document.register(root);
// The shell's load-failure state, which review.js must clear on boot.
const boot = document.createElement('div');
boot.className = 'rv-error';
boot.appendChild(document.createTextNode('The review console did not load.'));
root.appendChild(boot);
document.body.appendChild(root);

require(%(module)s);

function classesOf(el, out) {
  out = out || [];
  if (el.className) out.push(el.className);
  (el.children || []).forEach((c) => classesOf(c, out));
  return out;
}
function findByClass(el, cls, out) {
  out = out || [];
  if (el.className && el.className.split(/\\s+/).indexOf(cls) !== -1) out.push(el);
  (el.children || []).forEach((c) => findByClass(c, cls, out));
  return out;
}
// Click a button by its dataset key/value, anywhere under the root.
globalThis.__click = function (key, value) {
  const hits = [];
  (function walk(el) {
    if (el.dataset && el.dataset[key] === value) hits.push(el);
    (el.children || []).forEach(walk);
  })(root);
  if (!hits.length) throw new Error('no element with data-' + key + '=' + value);
  hits[0].dispatch('click', { currentTarget: hits[0], target: hits[0] });
  return hits[0];
};
globalThis.__type = function (index, text) {
  const areas = [];
  (function walk(el) {
    if (el.tagName === 'TEXTAREA') areas.push(el);
    (el.children || []).forEach(walk);
  })(root);
  areas[index].value = text;
  areas[index].dispatch('input', { currentTarget: areas[index], target: areas[index] });
};
function submitButton() {
  const btns = [];
  (function walk(el) {
    if (el.className === 'rv-submit') btns.push(el);
    (el.children || []).forEach(walk);
  })(root);
  return btns[0] || null;
}
globalThis.__submitState = function () {
  const b = submitButton();
  return b ? b.disabled : null;
};
globalThis.__submit = function () {
  const b = submitButton();
  if (!b) throw new Error('no submit button');
  b.dispatch('click', { currentTarget: b, target: b });
};
globalThis.__report = function () {
  const grids = findByClass(root, 'asc-answers');
  return {
    text: root.textContent,
    classes: classesOf(root),
    grids: grids.map((g) => ({
      childCount: g.children.length,
      childClasses: g.children.map((c) => c.className),
    })),
    greenCards: findByClass(root, 'asc-answer-physician').length,
    eyebrows: findByClass(root, 'asc-answer-eyebrow').map((e) => e.textContent),
    clock: findByClass(root, 'asc-session-clock').map((e) => e.textContent),
    note: findByClass(root, 'asc-session-note').map((e) => e.textContent),
    errors: findByClass(root, 'rv-error').map((e) => e.textContent),
    calls: globalThis.__calls.map((c) => c.url),
  };
};
// The module's boot chain is promise-based; drain the microtask queue first.
setTimeout(() => {
  const extra = %(drive)s;
  if (extra) { new Function(extra)(); }
  setTimeout(() => {
    const rep = globalThis.__report();
    rep.posts = globalThis.__calls.filter((c) => c.method === 'POST');
    console.log(JSON.stringify(rep));
  }, 0);
}, 0);
"""


def _me():
    return {
        "user": {"specialty": "nephrology"},
        "tier": "reviewer",
        "can_review": True,
        "dimensions": [
            ["clinical_accuracy", "Clinically correct", "the answer is right for this patient"],
            ["reasoning_quality", "Reasoning holds", "the steps actually support the answer"],
            ["completeness", "Nothing decisive missing", "no omission that changes management"],
            ["rubric_quality", "Grader is usable", "the rubric would score a new answer correctly"],
        ],
        "dimension_states": ["agree", "disagree", "cannot_assess"],
        "verdicts": ["accept", "accept_with_edits", "reject"],
        "strength_choices": ["A", "B", "equivalent"],
        "paired": True,
    }


def _pair_body(*, session=None):
    return {
        "pair": {
            "task_id": "t-1",
            "task": {"task_id": "t-1", "specialty": "nephrology",
                     "prompt": "K+ 7.1 with peaked T waves. Next step?",
                     "candidate_answers": [{"id": "A", "text": "IV calcium"},
                                           {"id": "B", "text": "Dialyze"}]},
            "answers": [
                {"label": "A", "confidence": "high",
                 "answer": {"verdict": "A_better", "chosen_id": "A",
                            "from_scratch": {"ideal_answer": "Calcium gluconate first."}}},
                {"label": "B", "confidence": "medium",
                 "answer": {"verdict": "B_better", "chosen_id": "B",
                            "from_scratch": {"ideal_answer": "Emergent dialysis."}}},
            ],
            "blinded": True,
        },
        "session": session,
    }


def _routes(**over):
    routes = {
        "/review/me": {"status": 200, "body": _me()},
        "/review/pair/next": {"status": 200, "body": _pair_body()},
        "/review/stats": {"status": 200,
                          "body": {"review_ready": 4, "awaiting_second": 2, "adjudicated": 9}},
        "/review/double-label/next": {"status": 200, "body": {"task": None}},
    }
    routes.update(over)
    return routes


def _render(routes, drive: str = "") -> dict:
    return _run_node(_HARNESS % {
        "shim": json.dumps(str(_DOM_SHIM)),
        "module": json.dumps(str(_REVIEW_JS)),
        "routes": json.dumps(routes),
        "drive": json.dumps(drive) if drive else "null",
    })


# ═══ the module runs at all ══════════════════════════════════════════════════
def test_the_module_boots_and_clears_the_shells_load_failure_state():
    out = _render(_routes())
    assert "did not load" not in out["text"]
    assert "/api/asclepius/review/pair/next" in out["calls"]


def test_the_page_draws_a_PAIR_not_a_single_submission():
    out = _render(_routes())
    assert "/api/asclepius/review/pair/next" in out["calls"]
    assert "/api/asclepius/review/next" not in out["calls"]


# ═══ the accent carries meaning (§5) ═════════════════════════════════════════
def test_both_cards_are_green_and_neither_is_orange():
    out = _render(_routes())
    assert out["greenCards"] == 2
    joined = " ".join(out["classes"])
    assert "orange" not in joined
    # `.asc-answer` is the MODEL-output card and carries an orange left rule in
    # the base stylesheet. The physician card is its own class, not an override.
    assert "asc-answer " not in (joined + " ")


def test_a_and_b_are_told_apart_only_by_the_mono_eyebrow_and_position():
    out = _render(_routes())
    assert out["eyebrows"] == ["Physician A", "Physician B"]
    # Identical class on both cards — no per-column hue.
    grid = out["grids"][0]
    assert grid["childClasses"] == ["asc-answer-physician", "asc-answer-physician"]


def test_the_answers_grid_contains_exactly_the_two_cards():
    """`.asc-answers` is 1fr 1fr. A legend, toolbar or badge dropped inside it
    lands in cell 2 and pushes B to row 2 — this has already shipped once."""
    out = _render(_routes())
    assert len(out["grids"]) == 1
    assert out["grids"][0]["childCount"] == 2


# ═══ the countdown ═══════════════════════════════════════════════════════════
def test_the_countdown_renders_from_the_api_response():
    session = {"session_id": "ws-1", "min_seconds": 1200, "credited_seconds": 751,
               "nonce": "n", "qualified": False}
    out = _render(_routes(**{"/review/pair/next":
                             {"status": 200, "body": _pair_body(session=session)}}))
    assert out["clock"] == ["Session · 12:31 of 20:00"]


def test_under_two_minutes_the_copy_changes_and_the_colour_does_not():
    """Lime means 'needs attention'. Pink means critical and blocking, and a
    running clock is not an emergency."""
    session = {"session_id": "ws-1", "min_seconds": 1200, "credited_seconds": 1120}
    out = _render(_routes(**{"/review/pair/next":
                             {"status": 200, "body": _pair_body(session=session)}}))
    assert out["clock"] == ["Session · 18:40 of 20:00"]
    assert out["note"] and "before this session qualifies" in out["note"][0]
    # The clock element's class is the same one in both states.
    assert out["classes"].count("asc-session-clock") == 1
    css = _CSS.read_text(encoding="utf-8")
    block = css.split("PRD-R — the paired review surface")[1]
    clock_rule = block.split(".asc-session-clock")[1].split("}")[0]
    assert "--lime" in clock_rule and "--pink" not in clock_rule


def test_no_countdown_is_rendered_when_the_server_sends_no_session():
    """Before Agent P merges, ``session`` is null. Missing is honest; a
    client-invented clock would tell a physician they had earned time they
    had not."""
    out = _render(_routes())
    assert out["clock"] == []


# ═══ failure is visible ══════════════════════════════════════════════════════
def test_a_500_on_the_draw_renders_a_visible_error_not_a_silent_placeholder():
    out = _render(_routes(**{"/review/pair/next":
                             {"status": 500, "body": {"detail": "Queue exploded"}}}))
    assert any("Queue exploded" in e for e in out["errors"])
    assert "Retry" in out["text"]


def test_a_non_reviewer_gets_an_honest_state_rather_than_a_bare_403():
    me = _me()
    me["can_review"] = False
    out = _render(_routes(**{"/review/me": {"status": 200, "body": me}}))
    assert "does not have the reviewer tier" in out["text"]
    assert "/api/asclepius/review/pair/next" not in out["calls"]


# ═══ the judgment actually produces the right payload ════════════════════════
def _answer_all_dimensions():
    # Four dimension rows and the stronger row all share data-state, so click by
    # position: pick 'agree' in each dimension segment, then the stronger choice.
    return """
var segs = [];
(function walk(el){ if (el.className === 'rv-seg') segs.push(el);
                    (el.children||[]).forEach(walk); })(document.getElementById('reviewRoot'));
// segs[0] is "Which is stronger?"; the rest are the four dimensions.
segs.slice(1).forEach(function (s) {
  s.children[0].dispatch('click', { currentTarget: s.children[0] });
});
"""


def test_accept_a_submits_one_verdict_plus_a_side():
    """Four buttons, three stored verdicts. 'Accept A' must post
    ``verdict=accept`` with ``accepted_side=A`` — an 'accept_a' token would fall
    straight out of the server's acceptance denominator."""
    drive = _answer_all_dimensions() + """
globalThis.__click('state', 'A');
globalThis.__click('verdict', 'accept:A');
globalThis.__submit();
"""
    out = _render(_routes(), drive)
    assert out["posts"], "no adjudication was posted"
    post = out["posts"][0]
    assert post["url"] == "/api/asclepius/review/pair/t-1"
    assert post["body"]["verdict"] == "accept"
    assert post["body"]["accepted_side"] == "A"
    assert post["body"]["stronger"] == "A"
    assert set(post["body"]["dimensions"]) == {
        "clinical_accuracy", "reasoning_quality", "completeness", "rubric_quality"}
    assert set(post["body"]["dimensions"].values()) == {"agree"}


def test_reject_both_is_blocked_until_a_reason_is_given():
    """Same rule the server enforces with a 400, so the button state and the
    error can never disagree about what a complete review is."""
    drive = _answer_all_dimensions() + """
globalThis.__click('state', 'equivalent');
globalThis.__click('verdict', 'reject');
// __submitState() reports the button's `disabled` flag.
globalThis.__disabledBefore = globalThis.__submitState();
globalThis.__type(0, 'Both miss the calcium step entirely.');
globalThis.__disabledAfter = globalThis.__submitState();
"""
    out = _render(_routes(), drive + """
globalThis.__report = (function (orig) { return function () {
  var r = orig(); r.disabledBefore = globalThis.__disabledBefore;
  r.disabledAfter = globalThis.__disabledAfter; return r; }; })(globalThis.__report);
""")
    assert out["disabledBefore"] is True, "reject-both was submittable with no reason"
    assert out["disabledAfter"] is False, "a reason was given and submit stayed blocked"


def test_corrections_are_revealed_not_always_present():
    """An empty textarea under every review invites the reviewer to feel they owe
    prose on an accept. They don't."""
    src = _REVIEW_JS.read_text(encoding="utf-8")
    assert "display:none" in src
    assert "correctionsBox.style.display" in src


# ═══ the rules (§4.3) ════════════════════════════════════════════════════════
def test_the_module_never_uses_innerHTML():
    src = _REVIEW_JS.read_text(encoding="utf-8")
    assert "innerHTML" not in src
    assert "outerHTML" not in src
    assert "insertAdjacentHTML" not in src


def test_the_load_failure_state_lives_in_the_shell():
    """A module that fails to parse cannot render its own error. The visible
    error is therefore the DEFAULT, and booting is what clears it."""
    html = _REVIEW_HTML.read_text(encoding="utf-8")
    assert "reviewBootError" in html
    assert "did not load" in html


def test_mobile_collapses_through_the_existing_breakpoint():
    """§4.3: follow the established pattern, do not invent a second breakpoint."""
    css = _CSS.read_text(encoding="utf-8")
    assert "@media (max-width: 880px)" in css
    breakpoint = css.split("@media (max-width: 880px)")[1].split("}")[0] + "}"
    assert ".asc-answers { grid-template-columns: 1fr; }" in breakpoint
    # And the PRD-R block reuses that grid rather than defining a second one.
    assert "PRD-R — the paired review surface" in css
    prd_r = css.split("PRD-R — the paired review surface")[1]
    assert "grid-template-columns" not in prd_r


def test_no_raw_hex_is_introduced_by_the_prd_r_block():
    """Design system: do not introduce a hex value outside _tokens.css."""
    import re
    css = _CSS.read_text(encoding="utf-8")
    prd_r = css.split("PRD-R — the paired review surface")[1]
    assert re.search(r"#[0-9a-fA-F]{3,8}\b", prd_r) is None
