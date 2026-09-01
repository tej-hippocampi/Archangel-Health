"""The practice-case tour opens at the beginning, and each step means one thing.

The reported bug: a physician opened the practice case and landed on "STEP 3 OF
14", mid-flow, with no welcome screen. Two causes, and they compounded.

Steps 1 and 2 were gated on the IDENTICAL predicate (``d.stage !==
'prompt_review'``), so a single stale draft bit satisfied both in one pass of
tutTick's fast-forward loop and the pointer went 0 to 2. And the welcome screen
was checked AFTER that loop, so it never got the chance to hold the pointer at
the start. The stale bit came from ``asclepius_draft_tutorial-calibration-1``,
which survived every abandonment path.

These tests run the shipped tour source under the node DOM shim, so they fail
on the real functions rather than on a description of them.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

_FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
JS = (_FRONTEND / "asclepius.js").read_text(encoding="utf-8")
_DOM = str((pathlib.Path(__file__).parent / "_asclepius_dom.js").resolve())

_LINE_COMMENT = re.compile(r"(?m)^\s*//.*$")


def _extract_function(src: str, name: str) -> str:
    marker = f"function {name}("
    start = src.index(marker)
    if src[start - 6 : start] == "async ":
        start -= 6
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"unbalanced braces extracting {name}")


def _fn(name: str) -> str:
    return _extract_function(JS, name)


def _const(name: str) -> str:
    """Bracket-balanced, because TUTORIAL_CHAPTERS is a deep literal full of
    semicolons inside arrow functions; a non-greedy match to the first `;`
    truncates it mid-array and the node run dies on a syntax error rather than
    on the thing under test."""
    start = JS.index(f"const {name} = ")
    i = JS.index("=", start) + 1
    while JS[i] in " \n":
        i += 1
    openers, closers = "([{", ")]}"
    depth = 0
    j = i
    while j < len(JS):
        ch = JS[j]
        if ch in openers:
            depth += 1
        elif ch in closers:
            depth -= 1
            if depth == 0:
                return JS[start : j + 1] + ";"
        j += 1
    raise AssertionError(f"unbalanced brackets extracting {name}")


def _run_node(script: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed in this environment")
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\n{proc.stderr}\n{proc.stdout}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


# The tour engine's real content and pointer logic. Everything that paints is
# stubbed, because what is under test is WHICH STEP the pointer lands on.
_TOUR_PRELUDE = """
require(%(dom)r);
const calls = [];
const state = { user: {}, draft: null, tutorial: null, task: {task_id: 'tutorial-calibration-1'} };

const TOUR_TARGETS = new Proxy({}, { get: (_t, k) => '.' + String(k) });
function validatePrompt() {}
function selectVerdict() {}
function hasCriticalNegative() { return false; }
function renderTourWelcome() { calls.push('welcome'); }
function renderTourSpotlight(step) { calls.push('spotlight:' + step.id); }
function hideTourLayer() { calls.push('hide'); }
function tutorialActive() { return !!(state.tutorial && state.tutorial.active); }
function tutPersistStep() {}
function resolveTourIndex() {}
function tutCurrentStep() {
  const t = state.tutorial;
  return t ? TUTORIAL_STEPS[t.idx] : null;
}
%(payload)s
function out(o) { console.log(JSON.stringify(o)); }
"""


def _steps_block() -> str:
    """TUTORIAL_STEPS is declared empty and then filled by a forEach, so the
    declaration alone extracts to `[]` and every assertion below would pass
    vacuously against a zero-length tour. Take the declaration AND the loop."""
    start = JS.index("  const TUTORIAL_STEPS = [];")
    end = JS.index("}));", start) + len("}));")
    block = JS[start:end]
    assert "TUTORIAL_STEPS.push" in block, "the fill loop moved away from the declaration"
    return block


def _tour_harness(body: str) -> dict:
    payload = "\n".join([
        _const("TUTORIAL_CHAPTERS"),
        _steps_block(),
        _fn("tutMarkDone"), _fn("tutDone"),
        _fn("tutStepSatisfied"), _fn("tutTick"),
        _fn("tutVisibleSteps"), _fn("tutStepNumber"),
    ])
    return _run_node(_TOUR_PRELUDE % {"dom": _DOM, "payload": payload} + "\n" + body)


def test_the_harness_actually_loaded_the_tour():
    """Guard against every assertion below passing against an empty step list.

    TUTORIAL_STEPS is declared empty and filled by a loop; extracting only the
    declaration gave a zero-length tour on which "the pointer did not move" is
    trivially true. Found exactly that way.
    """
    out = _tour_harness("out({ n: TUTORIAL_STEPS.length, first: TUTORIAL_STEPS[0].id });")
    assert out["n"] == 14, out
    assert out["first"] == "ch1-tabs"


# ─── The reported bug ────────────────────────────────────────────────────────
def test_a_stale_draft_cannot_open_the_tour_mid_flow():
    """THE REGRESSION. A draft left at a later stage by an abandoned run used to
    satisfy the first two steps at once, landing the physician on step 3."""
    out = _tour_harness("""
    state.draft = { stage: 'compare', verdict: 'B_better',
                    prompt_review: { reviewed: true } };
    state.tutorial = { active: true, idx: 0, welcomed: false, done: {} };
    tutTick();
    out({ calls, idx: state.tutorial.idx });
    """)
    assert out["idx"] == 0, "the pointer moved off step 1 before the physician saw anything"
    assert out["calls"] == ["welcome"], (
        "a fresh run must render the welcome screen, not a mid-flow spotlight"
    )


def test_the_welcome_screen_is_decided_before_the_fast_forward_runs():
    """Ordering IS the guarantee.

    The loop used to run first and move the pointer underneath the screen the
    physician was still looking at, so dismissing the welcome dropped them into
    the middle of the tour. Un-welcomed now renders the welcome and nothing
    else; welcomed renders the FIRST step, not a later one.
    """
    out = _tour_harness("""
    state.draft = { stage: 'submit', verdict: 'B_better', confidence_set: true,
                    prompt_review: { reviewed: true }, refine_saved: true };
    state.tutorial = { active: true, idx: 0, welcomed: false, done: {} };
    tutTick();
    const first = { idx: state.tutorial.idx, calls: calls.slice() };
    state.tutorial.welcomed = true;
    tutTick();
    out({ first, afterIdx: state.tutorial.idx, calls });
    """)
    assert out["first"]["idx"] == 0
    assert out["first"]["calls"] == ["welcome"]
    # Still step 1 after the welcome, because ch1-tabs is ledger-backed and a
    # draft cannot claim it. That is the whole fix.
    assert out["afterIdx"] == 0
    assert out["calls"][-1] == "spotlight:ch1-tabs"


def test_the_fast_forward_still_works_once_a_step_is_genuinely_done():
    """The fix must not freeze the tour: real progress still advances it."""
    out = _tour_harness("""
    state.draft = { stage: 'compare', verdict: 'B_better',
                    prompt_review: { reviewed: true } };
    state.tutorial = { active: true, idx: 0, welcomed: true, done: {} };
    tutMarkDone('ch1-tabs');
    tutTick();
    out({ idx: state.tutorial.idx, at: TUTORIAL_STEPS[state.tutorial.idx].id });
    """)
    assert out["idx"] > 0, "marking a step done should let the pointer move on"
    # ch1-valid is satisfied by prompt_review.reviewed, so it advances past it
    # too, and stops at the first thing genuinely undone.
    assert out["at"] == "ch2-instinct"


# ─── The structural fix ──────────────────────────────────────────────────────
def test_no_two_adjacent_steps_share_a_completion_predicate():
    """The defect class, not just the instance.

    Two steps satisfied by the same fact means one draft bit advances the
    pointer twice. It happened at ch1-tabs/ch1-valid and again at
    ch3-read/ch3-verdict, so it is worth a test that catches the next one.
    """
    out = _tour_harness("""
    const draft = { stage: 'compare', verdict: 'B_better',
                    prompt_review: { reviewed: true }, refine_saved: true,
                    confidence_set: true };
    state.tutorial = { active: true, idx: 0, welcomed: true, done: {} };
    const pairs = [];
    for (let i = 0; i < TUTORIAL_STEPS.length - 1; i++) {
      state.draft = draft;
      const a = TUTORIAL_STEPS[i], b = TUTORIAL_STEPS[i + 1];
      pairs.push({ a: a.id, b: b.id,
                   aSat: tutStepSatisfied(a), bSat: tutStepSatisfied(b) });
    }
    out({ pairs });
    """)
    # ch1-tabs and ch3-read are now ledger-backed, so with an empty ledger they
    # are NOT satisfied by draft state however far along it is.
    by_id = {p["a"]: p for p in out["pairs"]}
    assert by_id["ch1-tabs"]["aSat"] is False, (
        "ch1-tabs is satisfiable from the draft again, which is the step-three bug"
    )
    assert by_id["ch3-read"]["aSat"] is False, (
        "ch3-read is satisfiable from the draft again, the same collision one chapter later"
    )


def test_a_ledger_backed_step_advances_when_the_physician_actually_does_it():
    """The fix must not make those steps unreachable: marking done advances."""
    out = _tour_harness("""
    state.draft = { stage: 'compare', prompt_review: { reviewed: true } };
    state.tutorial = { active: true, idx: 0, welcomed: true, done: {} };
    const before = tutStepSatisfied(TUTORIAL_STEPS[0]);
    tutMarkDone('ch1-tabs');
    out({ before, after: tutStepSatisfied(TUTORIAL_STEPS[0]) });
    """)
    assert out["before"] is False
    assert out["after"] is True


# ─── The counter ─────────────────────────────────────────────────────────────
def test_the_counter_does_not_promise_steps_this_physician_will_never_see():
    """`skipIf` prunes up to four steps. Counting them anyway reported a longer
    tour than the one being taken."""
    out = _tour_harness("""
    state.tutorial = { active: true, idx: 0, welcomed: true, done: {} };
    state.draft = { stage: 'compare', verdict: 'B_better' };
    const full = tutVisibleSteps().length;
    state.draft = { stage: 'compare', verdict: 'both_inadequate' };
    const pruned = tutVisibleSteps().length;
    const last = tutVisibleSteps()[pruned - 1];
    out({ full, pruned, lastN: tutStepNumber(last).n, lastTotal: tutStepNumber(last).total });
    """)
    assert out["pruned"] < out["full"], "both_inadequate should prune steps"
    assert out["lastN"] == out["lastTotal"], (
        "the final visible step must read as the final step, so the bar can reach 100%"
    )


# ─── Source guarantees ───────────────────────────────────────────────────────
def test_mid_tour_resume_is_gone():
    """Resume read a saved server position and suppressed the welcome screen
    whenever it fired, onto a draft that abandonment never cleared."""
    body = _extract_function(JS, "startTutorial")
    body = _LINE_COMMENT.sub("", body)
    assert "opts.resume" not in body
    assert "resumeStep" not in body
    assert "welcomed: false" in body, "a run must always start un-welcomed"
    assert "clearDraft(TUTORIAL_TASK_ID);" in body, "the draft must be cleared unconditionally"


def test_skipping_the_practice_case_is_not_offered_any_more():
    src = _LINE_COMMENT.sub("", JS)
    assert "action: 'skip'" not in src, "the portal still asks the server to skip"
    assert "'Skip tutorial'" not in src, "a skip control was re-introduced"


def test_a_gate_403_never_costs_the_physician_their_draft():
    """The worst thing this change could do is refuse a completed evaluation at
    the last step and delete it on the way out."""
    src = _LINE_COMMENT.sub("", JS)
    open_body = _LINE_COMMENT.sub("", _extract_function(JS, "openTaskById"))
    gate_line = open_body.index("isPracticeGate(e)")
    # The TERMINAL-403 clear specifically: openTaskById has an earlier and
    # unrelated clearDraft(id) on a different path.
    clear_line = open_body.index("=== 410) clearDraft(id)")
    assert gate_line < clear_line, (
        "the practice-case gate must be handled BEFORE the terminal-403 clearDraft"
    )
    assert "isPracticeGate" in src


# ─── The reveal ──────────────────────────────────────────────────────────────
_REVEAL_PRELUDE = """
require(%(dom)r);
let rootNode = null;
function setRoot(n) { rootNode = n; }
function isAdvisor() { return false; }
function openInstructionDrawer() {}
function renderDashboardView() { calls.push('dashboard'); }
function startTutorial(o) { calls.push('replay:' + !!(o && o.replay)); }
const calls = [];
const state = { user: {}, tutorial: null };
%(payload)s
function find(pred, n) {
  if (!n) return null;
  if (pred(n)) return n;
  for (const c of (n.childNodes || [])) { const r = find(pred, c); if (r) return r; }
  return null;
}
function out(o) { console.log(JSON.stringify(o)); }
"""


def _reveal_harness(body: str) -> dict:
    payload = "\n".join([_fn("h"), _fn("appendChildren"), _fn("renderTutorialReveal")])
    return _run_node(_REVEAL_PRELUDE % {"dom": _DOM, "payload": payload} + "\n" + body)


_RESULT = """
  const result = {
    passed: true, headline: 'You got the call right.',
    findings: [
      {id:'sound-answer', label:'Picked the answer', matched:true, reason:'ok',
       your_answer:'You chose B.'},
      {id:'congestion-evidence', label:'Cited congestion', matched:false,
       reason:'you did not name it', your_answer:'You wrote: "Increase the loop dose."',
       planted:true},
    ],
    planted_finding: {id:'congestion-evidence', matched:false, reason:'the JVP is the point'},
    must_acknowledge: ['congestion-evidence'],
    teaching: { key_data: ['JVP 12 cm', 'weight down 1.5 kg'],
                reference_answer: 'the rise is permissive' },
  };
"""


def test_the_reveal_quotes_the_physician_back_to_themselves():
    """"You missed the congestion evidence" teaches nothing next to the sentence
    they actually wrote."""
    out = _reveal_harness(_RESULT + """
    renderTutorialReveal(result, {});
    out({ text: rootNode.textContent });
    """)
    assert 'You wrote: "Increase the loop dose."' in out["text"]
    assert "JVP 12 cm" in out["text"], "the teaching block did not render"


def test_a_miss_has_to_be_opened_before_the_physician_can_move_on():
    out = _reveal_harness(_RESULT + """
    renderTutorialReveal(result, {});
    const btn = find((n) => n.tagName === 'BUTTON'
                            && n.textContent.indexOf('Start real') >= 0, rootNode);
    const det = find((n) => n.tagName === 'DETAILS'
                            && n.className.indexOf('asc-tour-finding-ack') >= 0, rootNode);
    const before = btn.disabled;
    det.open = true;
    det.dispatch('toggle');
    out({ before, after: btn.disabled });
    """)
    assert out["before"] is True, "a pass with an unread miss must not offer the exit yet"
    assert out["after"] is False, "opening the miss must release it: one click, not a quiz"


def test_a_failed_attempt_offers_another_go_rather_than_the_door():
    out = _reveal_harness("""
    renderTutorialReveal({ passed: false, headline: 'Not yet.', findings: [],
                           planted_finding: null, must_acknowledge: [], teaching: {} }, {});
    const btn = find((n) => n.tagName === 'BUTTON', rootNode);
    btn.dispatch('click');
    out({ label: btn.textContent, calls });
    """)
    assert out["label"] == "Take it again"
    assert "replay:true" in out["calls"], "the retry must start a clean run"
