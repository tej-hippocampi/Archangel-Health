"""Evaluation Flow UI/UX Overhaul (V3 + V4) — the §14 acceptance suite.

Fifteen changes across ``frontend/asclepius/asclepius.js`` + ``asclepius.css``,
plus the one backend field in §11.1. Every change is scoped to ``isV3()`` (which
already covers V3 and V4) or is an explicitly cross-version copy/layout fix; the
``isAssisted()`` (V2) surfaces must survive untouched.

Three kinds of assertion here, in descending order of strength:

  1. **Executed.** §7's surgical repaint is exercised for real: the step-list
     functions are extracted from the shipped source and run under node against
     a minimal DOM shim (``_asclepius_dom.js``), so what is asserted is node
     identity produced by the shipped code, not a Python re-derivation.
  2. **Backend contracts.** §11.1's multi-axis normalization runs against the
     real ``asclepius.rubric`` / ``packaging`` / ``validation`` modules.
  3. **Structural.** The removals (§2–§6, §8–§13) are assertions about the
     shipped source text. A DOM-free environment cannot observe "this control is
     absent from the rendered page"; it can observe that the code which built it
     is gone, which is the same guarantee one step earlier.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

from asclepius import rubric as R
from asclepius.constants import RUBRIC_AXES
from asclepius.validation import validate_submission

_FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
JS_PATH = _FRONTEND / "asclepius.js"
CSS_PATH = _FRONTEND / "asclepius.css"
DOM_SHIM = pathlib.Path(__file__).resolve().parent / "_asclepius_dom.js"

JS = JS_PATH.read_text(encoding="utf-8")
CSS = CSS_PATH.read_text(encoding="utf-8")


# ─── source extraction (same approach as test_asclepius_diff_ui) ──────────────
def _extract_function(src: str, name: str) -> str:
    marker = f"function {name}("
    start = src.index(marker)
    if src[start - 6 : start] == "async ":     # keep the async prefix, or `await` won't parse
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
    raise AssertionError(f"unbalanced braces extracting {name} from asclepius.js")


def _body_of(name: str) -> str:
    """The shipped source of one function — for asserting what it does NOT call."""
    return _extract_function(JS, name)


_LINE_COMMENT = re.compile(r"^\s*//.*$", re.M)


def _code(src: str) -> str:
    """Source with whole-line ``//`` comments removed.

    Absence assertions ("this control is gone") have to run against code, not
    prose: the comments that explain a removal necessarily name the thing that
    was removed, and a test that trips over its own explanation would push the
    next person to delete the explanation.
    """
    return _LINE_COMMENT.sub("", src)


_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _css_code(src: str) -> str:
    """CSS with ``/* … */`` comments removed — same reasoning as ``_code``."""
    return _BLOCK_COMMENT.sub("", src)


JS_CODE = _code(JS)
CSS_CODE = _css_code(CSS)


def _run_node(script: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed in this environment")
    proc = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=30
    )
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\n{proc.stderr}\n{proc.stdout}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ═════════════════════════════════════════════════════════════════════════════
#  PART A — case entry and answer comparison (§2 §3 §4)
# ═════════════════════════════════════════════════════════════════════════════

# ─── Test 1 ───────────────────────────────────────────────────────────────────
def test_specialty_picker_is_overlay():
    """§2: the picker renders a sheet over the current view — it is not a route.

    The old implementation ended in ``setRoot(...)``, which clears ``#ascRoot``
    and rebuilds it; that is the full-page takeover, and the return trip paid for
    it again. The overlay must not call ``setRoot`` at all.
    """
    picker = _body_of("renderSpecialtyPicker")
    assert "setRoot(" not in picker, (
        "renderSpecialtyPicker still calls setRoot — a specialty pick is a "
        "three-way, reversible choice and must not destroy the view"
    )
    assert "'asc-sheet'" in picker and "'aria-modal': 'true'" in picker
    assert "role: 'dialog'" in picker

    # renderEvalView must no longer early-return into the picker as a route.
    eval_view = _body_of("renderEvalView")
    assert "await renderSpecialtyPicker" in eval_view
    assert "renderSpecialtyPicker(); return; }" not in eval_view, (
        "renderEvalView still routes to the picker instead of overlaying it"
    )

    # Choosing sets state and nothing else; the caller decides what a pick means.
    choose = _body_of("chooseSpecialty")
    assert "renderEvalView()" not in choose, (
        "chooseSpecialty still re-renders — re-picking the specialty you already "
        "had would throw away the case in progress"
    )
    assert "setPortalSpecialty(sp)" in choose
    assert "state.specialtyChosen = true" in choose


# ─── Test 2 ───────────────────────────────────────────────────────────────────
def test_specialty_picker_three_options():
    """§2: the card IS the button. No blurb, no bucket list, no nested CTA."""
    picker = _code(_body_of("renderSpecialtyPicker"))
    assert "asc-spec-grid" in picker
    assert "asc-spec-card" in picker
    assert "Each specialty serves hard" not in picker, "marketing blurb still in the chooser"
    assert "asc-ver-card-list" not in picker, "per-card bucket list still rendered"
    assert "Grade " not in picker, "'Grade Nephrology →' button still nested inside the card"
    # Exactly three columns, collapsing on narrow viewports.
    assert "grid-template-columns: repeat(3, 1fr)" in CSS
    assert ".asc-sheet {" in CSS and "rgba(26, 27, 26, .28)" in CSS


def test_specialty_sheet_scrim_is_never_pure_black():
    """Design law 3 — zero black fills. The scrim is teal-shifted ink."""
    sheet_rule = re.search(r"\.asc-sheet \{(.*?)\}", CSS, re.S)
    assert sheet_rule, ".asc-sheet rule missing"
    body = sheet_rule.group(1)
    assert "rgba(0, 0, 0" not in body and "rgba(0,0,0" not in body
    assert "#000" not in body


def test_specialty_sheet_is_dismissable_only_once_a_specialty_exists():
    """§2: on FIRST entry the choice is required — Esc/backdrop would strand the
    physician on a view with no case. On 'Change specialty' there is a case
    behind the sheet, so both escape hatches are live."""
    picker = _body_of("renderSpecialtyPicker")
    assert "dismissable" in picker
    assert "e.key === 'Escape' && dismissable" in picker
    assert "if (dismissable && e.target === sheet) close(null)" in picker
    eval_view = _body_of("renderEvalView")
    assert "renderSpecialtyPicker({ dismissable: false })" in eval_view
    badge = _body_of("renderExperienceBadge")
    assert "renderSpecialtyPicker({ dismissable: true })" in badge


# ─── Test 3 — THE STAGGER REGRESSION TEST ────────────────────────────────────
def test_answers_grid_has_two_children():
    """§3: ``.asc-answers`` is a ``1fr 1fr`` grid, so a third child lands in cell
    2 and pushes the answer cards onto two rows — that is the stagger. The only
    thing ``renderAnswersInto`` may append to the container is answer cards."""
    fn = _body_of("renderAnswersInto")

    appends = re.findall(r"container\.appendChild\(([^\n]*)", fn)
    assert appends, "renderAnswersInto appends nothing at all"
    for a in appends:
        assert "renderAnswerCard(" in a, (
            f"renderAnswersInto appends a non-card child to the answers grid: {a!r} "
            "— any extra grid child restaggers A and B"
        )
    assert "asc-diff-legend" not in fn
    # The grid itself still declares two columns and now aligns tops.
    grid = re.search(r"\.asc-answers \{(.*?)\}", CSS, re.S).group(1)
    assert "grid-template-columns: 1fr 1fr" in grid
    assert "align-items: start" in grid


# ─── Test 4 ───────────────────────────────────────────────────────────────────
def test_no_diff_legend_v3():
    """§3: the legend, its CSS, the toggle and the help line are gone on V3/V4."""
    assert "asc-diff-legend" not in JS_CODE, "the diff legend is still constructed"
    assert ".asc-diff-legend" not in CSS_CODE
    assert ".asc-answers-v3diff" not in CSS_CODE
    assert ".asc-diff-legend-mark" not in CSS_CODE

    compare = _body_of("renderCompareStage")
    # The toggle and the help line are both gated behind `showDiff`, which is
    # `isAssisted() && !isV3()` — V2 only.
    assert "const showDiff = assisted && !isV3();" in compare
    assert "const diffToggle = showDiff ?" in compare
    assert "showDiff ? h('p', { class: 'asc-help'" in compare

    # And the diff itself is never computed on V3/V4.
    answers = _body_of("renderAnswersInto")
    assert "isV3() ||" in answers and "computeAnswerDiff()" in answers


# ─── Test 5 ───────────────────────────────────────────────────────────────────
def test_v2_diff_intact():
    """§0.1 boundary guard: V2 is a shipped surface and keeps its diff.

    Note the §14 wording ("V2 still renders the legend") does not match the
    shipped code it was written against: the legend was built under
    ``if (diff && isV3())`` and never rendered on V2 at all. What the test is
    actually guarding is the ``isAssisted()`` boundary — the toggle, the diff
    computation and the diff CSS all survive for V2.
    """
    assert "computeAnswerDiff" in JS, "§16: computeAnswerDiff is still live for V2"
    assert ".asc-answer-diff" in CSS, "§16: the V2 diff CSS must stay"
    assert ".asc-editdiff" in CSS, "§16: .asc-editdiff is a different feature"
    compare = _body_of("renderCompareStage")
    assert "isAssisted()" in compare
    assert "'≡ Show full text'" in compare and "'◧ Highlight differences'" in compare
    assert "state.showFullText" in JS, "V2's toggle state must survive"
    # buildAnswerDiff's V3-only soft matching is untouched (§16).
    assert "soft: isV3()" in JS


# ─── Test 6 ───────────────────────────────────────────────────────────────────
def test_assist_hint_not_rendered_v3():
    """§4: the model's weaker-answer guess is never shown on V3/V4, but it is
    still captured — the whole point is measuring override rate against it."""
    hint = _body_of("renderAssistHint")
    ret = hint.index("if (isV3()) return;")
    shown = hint.index("suggested_weaker")
    assert ret < shown, "the V3 guard must precede any render of the suggestion"
    # The container stays mounted — other code queries #ascAssistHint.
    assert "id: 'ascAssistHint'" in JS
    # And the suggestion still rides the submitted payload.
    payload = _body_of("buildSubmissionPayload")
    assert "suggested_weaker" in payload, (
        "§4 keeps the suggestion in the record for override-rate analysis"
    )


# ═════════════════════════════════════════════════════════════════════════════
#  PART B — reasoning steps (§5 §6 §7 §8)
# ═════════════════════════════════════════════════════════════════════════════

# ─── Test 7 ───────────────────────────────────────────────────────────────────
def test_no_resplit_button():
    """§5: the split already ran automatically on mount. Both call sites drop
    the button; the auto-fire and ``autoSplitChosen`` itself stay (§16)."""
    assert "Re-split from answer" not in JS_CODE
    assert "resplitBtn" not in JS_CODE
    for fn in ("renderReasoningSection", "renderStepsCard"):
        body = _body_of(fn)
        assert "autoSplitChosen(listId, false)" in body, (
            f"{fn} no longer auto-populates the steps"
        )
        assert "state.splitAttemptedFor = state.task.task_id" in body
    assert "function autoSplitChosen(" in JS


def test_split_in_flight_shows_a_skeleton():
    """§5: three skeleton rows say 'this is arriving'; a blank card says
    'nothing here', and a spinner asks to be interpreted."""
    assert "function stepsSkeleton(" in JS
    assert ".asc-steps-skeleton" in CSS
    listv3 = _code(_body_of("renderStepsListV3"))
    assert "list.appendChild(stepsSkeleton());" in listv3
    assert "Splitting the chosen answer into steps" not in listv3
    # Reduced-motion users get the placeholder without the sweep.
    assert "prefers-reduced-motion" in CSS


def test_no_dangling_resplit_copy():
    """§5: the empty-list fallback used to point at a control that no longer
    exists."""
    assert "use “Re-split from answer”" not in JS_CODE
    assert "No steps yet — add steps manually." in JS_CODE


# ─── Test 8 ───────────────────────────────────────────────────────────────────
def test_step_toggle_label():
    """§6: one action, one label. The 'has this been touched?' distinction is
    already carried by the status pill beside it."""
    row = _code(_body_of("buildStepRowV3"))
    assert "open ? 'Close' : 'Edit'" in row
    assert "'Open'" not in row, "the Open/Edit fork is still there"


# ─── Tests 9, 10, 11 — EXECUTED against the shipped code ─────────────────────
_HARNESS_PRELUDE = """
require({dom!r});

// Stubs for everything the step list touches that is not under test.
const state = {{
  _openStep: null,
  splitting: false,
  task: {{ task_id: 't1', grounding_mode: 'optional' }},
  draft: {{ verdict: 'A_better' }},
}};
let steps = [];
function activeSteps() {{ return steps; }}
function saveDraft() {{}}
function syncStepsCont() {{}}
function updateSubmitState() {{}}
function isValidAnchor() {{ return true; }}
function emptyAnchor() {{ return {{}}; }}
function newAuthoredStep() {{ return {{ text: '', added: true }}; }}
function withMic(el) {{ return el; }}
function renderAnchorBlock() {{ return document.createElement('div'); }}
function clear(node) {{ while (node.firstChild) node.removeChild(node.firstChild); }}

{funcs}

function mkStep(i, opts) {{
  return Object.assign({{
    text: 'step ' + i, original_text: 'step ' + i,
    suggested_label: null, confirmed: false, corrected: false, added: false,
  }}, opts || {{}});
}}

const list = document.createElement('div');
list.id = 'ascStepsList';
document.register(list);
"""


def _harness(funcs: str, body: str) -> dict:
    prelude = _HARNESS_PRELUDE.format(dom=str(DOM_SHIM), funcs=funcs)
    return _run_node(prelude + "\n" + body)


_STEP_FUNCS = None


def _step_funcs() -> str:
    global _STEP_FUNCS
    if _STEP_FUNCS is None:
        _STEP_FUNCS = "\n".join(
            _extract_function(JS, n)
            for n in (
                "h",
                "appendChildren",
                "setStepConfirmed",
                "stepStatusOf",
                "stepsSkeleton",
                "repaintStepRow",
                "renderStepsListV3",
                "buildStepRowV3",
            )
        )
    return _STEP_FUNCS


def test_correct_as_is_preserves_scroll():
    """§9/Test 9: 'Correct as-is' changes three properties of one row.

    The old handler called ``renderStepsListV3``, which opens with
    ``clear(list)`` — destroying the subtree collapses the page height, the
    browser drops its scroll anchor, and the viewport snaps to top. Worse the
    further down the page you are, which is why steps 5–7 felt worst.

    Without layout we cannot read ``window.scrollY``; what we can read is the
    mechanism the browser's scroll anchoring keys off — whether the nodes
    survived. Here EVERY row node, including the clicked one, must be the same
    object before and after, and nothing may scroll programmatically.
    """
    out = _harness(
        _step_funcs(),
        """
        steps = [0,1,2,3,4,5,6].map((i) => mkStep(i));
        renderStepsListV3('ascStepsList');
        const before = list.childNodes.slice();
        const row6 = list.querySelector('[data-step-idx="5"]');
        const confirm6 = row6.querySelectorAll('.asc-step-confirm')[0];

        confirm6.dispatch('click');

        const after = list.childNodes.slice();
        console.log(JSON.stringify({
          sameLength: before.length === after.length,
          allSameIdentity: before.every((n, i) => n === after[i]),
          scrolled: scrollCalls.length,
          confirmed: steps[5].confirmed === true,
          label: confirm6.textContent,
          rowClass: row6.className,
          pill: row6.querySelector('.asc-step-status').textContent,
        }));
        """,
    )
    assert out["allSameIdentity"], (
        "'Correct as-is' rebuilt the list — every row node changed identity, "
        "which is exactly what makes the page jolt"
    )
    assert out["scrolled"] == 0, "confirming a step must not move the viewport"
    assert out["sameLength"]
    # …and it still did the three things it is supposed to do.
    assert out["confirmed"] is True
    assert out["label"] == "✓ Confirmed"
    assert "is-confirmed" in out["rowClass"]
    assert out["pill"] == "confirmed ✓"


def test_open_preserves_scroll():
    """§10/Test 10: opening step 4 repaints at most two rows and re-anchors the
    clicked row, so it stays under the cursor even when the row above it changes
    height."""
    out = _harness(
        _step_funcs(),
        """
        steps = [0,1,2,3,4,5,6].map((i) => mkStep(i));
        state._openStep = 1;                 // another row is already open
        renderStepsListV3('ascStepsList');
        const before = list.childNodes.slice();
        const row4 = list.querySelector('[data-step-idx="3"]');
        // The Edit link is the second control in the row head's action group.
        const editLink = row4.childNodes[0].childNodes[1].childNodes[1];

        editLink.dispatch('click');

        const after = list.childNodes.slice();
        const changed = before.map((n, i) => n === after[i] ? null : i).filter((x) => x !== null);
        console.log(JSON.stringify({
          changed,
          openStep: state._openStep,
          scrolled: scrollCalls.length,
          label: after[3].childNodes[0].childNodes[1].childNodes[1].textContent,
          expanded: after[3].childNodes[0].childNodes[1].childNodes[1].getAttribute('aria-expanded'),
        }));
        """,
    )
    # Rows 1 (closing) and 3 (opening) — and nothing else.
    assert out["changed"] == [1, 3], (
        f"expected only the closing and opening rows to repaint, got {out['changed']}"
    )
    assert out["openStep"] == 3
    assert out["scrolled"] == 1, "the clicked row must be re-anchored exactly once"
    assert out["label"] == "Close"
    assert out["expanded"] == "true"


def test_steps_list_not_rebuilt_on_toggle():
    """§11/Test 11: the row node identity of UNAFFECTED steps survives a toggle.

    This is the invariant the whole of §7 rests on. It is also asserted
    structurally: neither toggle handler may call ``renderStepsListV3``, which
    stays for genuine list changes only (§16).
    """
    out = _harness(
        _step_funcs(),
        """
        steps = [0,1,2,3,4].map((i) => mkStep(i));
        renderStepsListV3('ascStepsList');
        const before = list.childNodes.slice();
        const row3 = list.querySelector('[data-step-idx="2"]');
        row3.childNodes[0].childNodes[1].childNodes[1].dispatch('click');   // open
        const mid = list.childNodes.slice();
        list.querySelector('[data-step-idx="2"]')
            .childNodes[0].childNodes[1].childNodes[1].dispatch('click');   // close
        const after = list.childNodes.slice();
        console.log(JSON.stringify({
          survivedOpen: before.filter((n, i) => i !== 2).every((n, i) => n === mid.filter((_, j) => j !== 2)[i]),
          survivedClose: mid.filter((n, i) => i !== 2).every((n, i) => n === after.filter((_, j) => j !== 2)[i]),
          count: after.length,
          openStep: state._openStep,
        }));
        """,
    )
    assert out["survivedOpen"], "opening a step re-created unrelated rows"
    assert out["survivedClose"], "closing a step re-created unrelated rows"
    assert out["count"] == 5
    assert out["openStep"] is None

    # Structural half: the toggles repaint, they do not rebuild.
    row = _body_of("buildStepRowV3")
    head = row[: row.index("const row = h(")]
    assert "renderStepsListV3" not in head, (
        "a toggle handler still calls renderStepsListV3 — that is the jolt"
    )
    assert "repaintStepRow(" in row
    assert "dataset: { stepIdx: String(idx) }" in row
    # …but genuine list changes still go through the full renderer.
    tail = row[row.index("const rowActions = h("):]
    assert tail.count("renderStepsListV3(listId)") == 2, (
        "insert-below / remove must still rebuild the list — the indices shift"
    )
    assert "function renderStepsListV3(" in JS, "§16: keep the full renderer"


def test_update_submit_state_does_not_rebuild_the_steps_list():
    """§7's audit note: if ``updateSubmitState`` re-rendered any ancestor of the
    steps list it would reintroduce the jolt through the back door. It must only
    toggle the submit button's disabled state and its hint."""
    fn = _body_of("updateSubmitState")
    for forbidden in ("renderStepsListV3", "renderStepsList(", "renderRationale",
                      "renderTaskWorkspace", "setRoot", "refreshStagedFlow"):
        assert forbidden not in fn, (
            f"updateSubmitState calls {forbidden} — that rebuilds an ancestor of "
            "the steps list and brings the scroll jolt back"
        )
    assert "btn.disabled" in fn and "hint.textContent" in fn


def test_sync_steps_cont_does_not_rebuild_either():
    """Same guard for the other function the step handlers call on every edit."""
    fn = _body_of("syncStepsCont")
    for forbidden in ("renderStepsListV3", "renderRationale", "setRoot"):
        assert forbidden not in fn
    assert "btn.disabled" in fn


# ─── Test 12 ──────────────────────────────────────────────────────────────────
def test_original_not_truncated_in_js():
    """§8: the full ``original_text`` goes into the DOM and CSS ellipsises it at
    the true edge of the bar. An 80-character cut is wrong at every width except
    the one it happened to be chosen for."""
    assert ".slice(0, 80)" not in JS, "the hard 80-char truncation is still there"
    assert "asc-step-original-preview" in JS
    # Both renderers (V3/V4 and the classic list) were identical and both change.
    assert JS.count("asc-step-original-sum") == 2
    assert JS.count("h('span', { class: 'asc-step-original-preview' }, s.original_text || '')") == 2

    # The CSS half — `min-width: 0` is what actually lets a flex child shrink
    # far enough for text-overflow to engage.
    preview = re.search(r"\.asc-step-original-preview \{(.*?)\}", CSS, re.S).group(1)
    assert "min-width: 0" in preview
    assert "text-overflow: ellipsis" in preview
    assert "white-space: nowrap" in preview
    assert "overflow: hidden" in preview
    summary = re.search(r"\.asc-step-original-sum \{(.*?)\}", CSS, re.S).group(1)
    assert "display: flex" in summary and "width: 100%" in summary


def test_original_full_text_is_reachable_when_open():
    """The truncated preview hides when the details opens — the full text below
    it is the whole point, and a clipped echo above it is noise."""
    assert ".asc-step-original[open] .asc-step-original-preview { display: none; }" in CSS
    assert "asc-step-original-full" in JS


# ═════════════════════════════════════════════════════════════════════════════
#  PART C — the scoring rubric (§9 §10 §11 §12)
# ═════════════════════════════════════════════════════════════════════════════

# ─── Test 13 ──────────────────────────────────────────────────────────────────
def test_no_type_field():
    """§9: the 'Type' field asked a question the sentence already answered. The
    polarity folds into the stem — which still sets the sign of ``c.points``, so
    auto-fail and the grader's hard-fail keep working."""
    card = _code(_body_of("renderRubricCriterionCard"))
    assert "'Must include ✓'" not in card and "'Must never ✕'" not in card
    assert "signRow" not in card
    assert "'Type'" not in card
    # The stem carries it instead.
    assert "asc-rubric-stem-toggle" in card
    assert "setPoints(mag(), !neg())" in card, "the stem toggle must flip the sign"
    assert "'A correct answer'" in card
    assert "stemToggle.textContent = neg() ? 'must never' : 'must';" in card
    # paintAll drives the toggle, not a pill row.
    assert "Array.from(signRow.children)" not in card


def test_stem_toggle_polarity_colours():
    """§9: lime for the ordinary 'must'; pink for 'must never' — pink is the
    flag/critical accent, which is exactly what a must-never is."""
    assert ".asc-rubric-stem-toggle.neg" in CSS
    assert ".asc-rubric-stem-toggle.pos" in CSS
    neg = re.search(r"\.asc-rubric-stem-toggle\.neg \{(.*?)\}", CSS, re.S).group(1)
    assert "--pink" in neg
    pos = re.search(r"\.asc-rubric-stem-toggle\.pos \{(.*?)\}", CSS, re.S).group(1)
    assert "--lime-wash" in pos


# ─── Test 14 ──────────────────────────────────────────────────────────────────
def test_tier_labels():
    """§10: 'Must-have' and 'Important' are synonyms in ordinary English —
    nothing in the words says which outranks the other. Critical > Major > Minor
    is a severity scale clinicians already rank without thinking.

    Label-only: the tier KEYS are unchanged, so ``tierForPoints``,
    ``TIER_DEFAULT_PTS``, the backend and every stored record keep working."""
    block = re.search(r"const TIER_CHOICES = \[(.*?)\];", JS, re.S).group(1)
    assert "'Critical'" in block and "'Major'" in block and "'Minor'" in block
    assert "Must-have" not in block and "Nice-to-have" not in block
    # Keys untouched (§16).
    for key in ("'critical'", "'important'", "'helpful'"):
        assert key in block
    assert "const TIER_DEFAULT_PTS = { critical: 9, important: 5, helpful: 2 };" in JS
    # Explanations describe consequence, which is checkable — not priority,
    # which is an opinion.
    assert "the answer is wrong or unsafe without this" in block
    assert "the answer is clearly worse without this" in block
    assert "a refinement — good, not decisive" in block


def test_old_tier_vocabulary_is_gone_from_all_copy():
    """§10 + §12: a stale 'must-have / nice-to-have' in a help string or an
    info-dot would put two vocabularies on the same screen."""
    for stale in ("Must-have", "Nice-to-have", "must-have", "nice-to-have", "must-never"):
        assert stale not in JS_CODE, f"stale tier vocabulary {stale!r} still in the UI copy"


# ─── Test 15 ──────────────────────────────────────────────────────────────────
def test_scale_inline_with_question():
    """§10: the tier buttons and the slider are two ways to set ONE value. The
    readout belongs beside the question, not stranded below the choices."""
    card = _body_of("renderRubricCriterionCard")
    head = re.search(
        r"h\('div', \{ class: 'asc-rubric-matter-head' \},(.*?)\n      tierRow\)\);",
        card, re.S,
    )
    assert head, "the matter-head block is missing or tierRow is no longer its sibling"
    inner = head.group(1)
    assert "'How much does it matter?" in inner
    assert "asc-rubric-scale" in inner
    assert "slider" in inner and "ptsLabel" in inner and "autoFail" in inner
    # The old "slider in its own flex row below tierRow" is gone.
    assert "margin-top:8px' },\n        slider" not in card
    matter = re.search(r"\.asc-rubric-matter-head \{(.*?)\}", CSS, re.S).group(1)
    assert "justify-content: space-between" in matter


# ─── Tests 16, 17, 18 ─────────────────────────────────────────────────────────
def test_axis_multiselect():
    """§11: a criterion routinely scores on more than one axis — 'must never give
    thrombolytics in dissection' is safety AND accuracy. ``c.axes`` is the
    authority; ``c.axis`` mirrors ``axes[0]`` for backward compatibility."""
    card = _code(_body_of("renderRubricCriterionCard"))
    assert "c.axes = on ? c.axes.filter((x) => x !== ax) : c.axes.concat([ax]);" in card
    assert "c.axis = c.axes[0];" in card
    assert "'aria-pressed'" in card
    # Single-select assignment is gone.
    assert "c.axis = ax;" not in card
    assert "b.classList.toggle('active', b === e.currentTarget)" not in card, (
        "the old mutually-exclusive repaint is still there"
    )
    assert "function criterionAxes(" in JS


def test_axis_min_one():
    """§11: deselecting the last active axis is a no-op — a criterion without an
    axis is unscoreable."""
    card = _code(_body_of("renderRubricCriterionCard"))
    assert "if (on && c.axes.length === 1) return;" in card


def test_axis_plain_labels():
    """§11: 'axis' is ML vocabulary a clinician has no reason to know, and six
    raw enum values are not a question. No enum value may appear as button
    text, and the tooltip that used to translate them is gone (the options now
    explain themselves, which is the point)."""
    block = re.search(r"const AXIS_LABELS = \{(.*?)\n  \};", JS, re.S).group(1)
    for ax in RUBRIC_AXES:
        assert f"{ax}:" in block, f"no plain-language label for {ax}"
    card = _code(_body_of("renderRubricCriterionCard"))
    assert "AXIS_LABELS[ax]" in card
    assert "}, label));" in card, "the button text must be the plain label"
    assert "infoDot('Axes'" not in JS_CODE, "the axis tooltip should be gone"
    assert "Which axis does it score?" not in JS_CODE
    assert "'What does this criterion check? '" in card
    assert "(select all that apply)" in card
    # The wire vocabulary itself is untouched.
    assert "'accuracy', 'completeness', 'safety', 'reasoning', 'grounding', 'communication'" in JS


# ─── Test 19 ──────────────────────────────────────────────────────────────────
def test_axis_coverage_uses_axes_frontend():
    """§11.1: the FIX-7 nudge counts EVERY axis on a criterion. Reading ``c.axis``
    alone under-counts coverage the moment multi-select ships — a rubric that
    covers three axes across two criteria would read as covering two.

    Executed: the shipped ``rubricCompleteness`` is run under node.
    """
    funcs = "\n".join(
        _extract_function(JS, n)
        for n in ("isSpecificText", "tierForPoints", "hasCriticalNegative", "rubricCompleteness")
    )
    consts = "\n".join(
        re.search(rf"(const {n} = .*?;)\n", JS, re.S).group(1)
        for n in ("_PREMIUM_MIN_CRITERIA", "_PREMIUM_MIN_AXES", "_RUBRIC_CORE_AXES")
    )
    vague = re.search(r"(const _VAGUE_MARKERS = \[.*?\];)", JS, re.S).group(1)
    unit = re.search(r"(const _UNIT_RE = .*?;)\n", JS, re.S).group(1)
    clinical = re.search(r"(const _CLINICAL_RE = .*?;)\n", JS, re.S).group(1)
    tiers = re.search(r"(const RUBRIC_TIER_BANDS = .*?;)\n", JS, re.S)

    script = f"""
    {vague}
    {unit}
    {clinical}
    {consts}
    {tiers.group(1) if tiers else ''}
    {funcs}
    const multi = [
      {{ text: 'urine osmolality 120 mOsm/kg is accounted for', points: 9,
         axes: ['safety', 'accuracy'] }},
      {{ text: 'never gives 3% hypertonic saline at 2 mL/kg/h here', points: -9,
         axes: ['reasoning'] }},
    ];
    const legacy = [
      {{ text: 'urine osmolality 120 mOsm/kg is accounted for', points: 9, axis: 'accuracy' }},
      {{ text: 'never gives 3% hypertonic saline at 2 mL/kg/h here', points: -9, axis: 'safety' }},
    ];
    const unset = [{{ text: 'creatinine 2.4 is addressed', points: 5 }}];
    console.log(JSON.stringify({{
      multi: rubricCompleteness(multi).n_axes,
      multiCore: rubricCompleteness(multi).covers_core_axes,
      legacy: rubricCompleteness(legacy).n_axes,
      unset: rubricCompleteness(unset).n_axes,
    }}));
    """
    out = _run_node(script)
    assert out["multi"] == 3, (
        f"multi-axis coverage under-counted: {out['multi']} of 3 — the nudge is "
        "still reading c.axis alone"
    )
    assert out["multiCore"] is True, "safety+accuracy+reasoning is full core coverage"
    assert out["legacy"] == 2, "the legacy single-value shape must still count"
    assert out["unset"] == 0, "an axis-less criterion must not count as 'accuracy'"

    # And the source says so, so a future edit that reverts it is visible.
    fn = _body_of("rubricCompleteness")
    assert "c.axes" in fn


# ─── §11.1 backend ────────────────────────────────────────────────────────────
def test_backend_axes_normalizer():
    """§11.1: ``_axes`` accepts the list, the legacy single string, and garbage,
    and always yields at least one valid axis."""
    assert R._axes(["safety", "accuracy"]) == ["safety", "accuracy"]
    assert R._axes(["safety", "safety"]) == ["safety"], "deduped, order preserved"
    assert R._axes(None, "grounding") == ["grounding"], "legacy single value"
    assert R._axes([], "grounding") == ["grounding"]
    assert R._axes(None) == ["accuracy"], "a criterion always has >=1 axis"
    assert R._axes(["nonsense"]) == ["accuracy"]
    assert R._axes(["nonsense"], "safety") == ["safety"]
    assert R._axes("safety") == ["safety"], "a bare string is tolerated"
    assert R._axes([None, 3, "safety"]) == ["safety"], "non-strings are dropped"
    for a in R._axes(["safety", "accuracy"]):
        assert a in RUBRIC_AXES


def test_backend_normalize_emits_both_axes_and_axis():
    """§11.1: emit BOTH — ``axes`` (the list, authoritative) and ``axis``
    (``axes[0]``, deprecated) — so nothing downstream breaks at once."""
    out = R.normalize_rubric([
        {"text": "never gives thrombolytics in dissection", "points": -9,
         "axes": ["safety", "accuracy"]},
        {"text": "accounts for creatinine 2.4", "points": 7, "axis": "accuracy"},
        {"text": "no axis at all", "points": 3},
    ])
    assert out[0]["axes"] == ["safety", "accuracy"]
    assert out[0]["axis"] == "safety", "axis mirrors axes[0]"
    assert out[1]["axes"] == ["accuracy"], "legacy single value is upgraded"
    assert out[1]["axis"] == "accuracy"
    assert out[2]["axes"] == ["accuracy"], "defaulted, never empty"
    for c in out:
        assert c["axis"] == c["axes"][0]
        assert all(a in RUBRIC_AXES for a in c["axes"])


def test_backend_completeness_counts_every_axis():
    """§11.1: the backend mirror of test 19 — a rubric can reach the ≥3-axis
    premium bar with two multi-axis criteria."""
    crit = [
        {"text": "accounts for urine osmolality 120", "points": 9, "axes": ["safety", "accuracy"]},
        {"text": "never gives hypertonic saline at 2 mL/kg/h", "points": -9, "axes": ["reasoning"]},
    ]
    rc = R.rubric_completeness(crit)
    assert rc["n_axes"] == 3
    assert rc["axes"] == ["accuracy", "reasoning", "safety"]
    assert rc["covers_core_axes"] is True
    assert rc["nudges"] == []
    # Legacy shape still counted.
    legacy = [{"text": "accounts for creatinine 2.4", "points": 9, "axis": "accuracy"}]
    assert R.rubric_completeness(legacy)["n_axes"] == 1
    # An axis-less criterion contributes nothing (no silent default).
    assert R.rubric_completeness([{"text": "x 5 mg", "points": 5}])["n_axes"] == 0


def test_backend_frontend_completeness_agree_on_multi_axis():
    """The frontend mirrors ``rubric_completeness`` byte-for-byte by design —
    a disagreement would mislabel a rubric premium/standard in the UI."""
    crit = [
        {"text": "accounts for urine osmolality 120 mOsm/kg", "points": 9,
         "axes": ["safety", "accuracy"]},
        {"text": "never gives 3% hypertonic saline at 2 mL/kg/h", "points": -9,
         "axes": ["reasoning", "safety"]},
    ]
    py = R.rubric_completeness(crit)
    funcs = "\n".join(
        _extract_function(JS, n)
        for n in ("isSpecificText", "tierForPoints", "hasCriticalNegative", "rubricCompleteness")
    )
    consts = "\n".join(
        re.search(rf"(const {n} = .*?;)\n", JS, re.S).group(1)
        for n in ("_PREMIUM_MIN_CRITERIA", "_PREMIUM_MIN_AXES", "_RUBRIC_CORE_AXES")
    )
    vague = re.search(r"(const _VAGUE_MARKERS = \[.*?\];)", JS, re.S).group(1)
    unit = re.search(r"(const _UNIT_RE = .*?;)\n", JS, re.S).group(1)
    clinical = re.search(r"(const _CLINICAL_RE = .*?;)\n", JS, re.S).group(1)
    tiers = re.search(r"(const RUBRIC_TIER_BANDS = .*?;)\n", JS, re.S)
    js = _run_node(f"""
    {vague}
    {unit}
    {clinical}
    {consts}
    {tiers.group(1) if tiers else ''}
    {funcs}
    console.log(JSON.stringify(rubricCompleteness({json.dumps(crit)})));
    """)
    assert js["n_axes"] == py["n_axes"]
    assert js["covers_core_axes"] == py["covers_core_axes"]
    assert js["premium"] == py["premium"]


def test_backend_packaging_axis_histogram_counts_every_axis():
    """§11.1: the packaged rubric record's axis histogram counts a criterion once
    per axis — otherwise the sellable record under-reports its own coverage."""
    import asclepius.packaging as P  # noqa: PLC0415

    src = pathlib.Path(P.__file__).read_text(encoding="utf-8")
    assert 'for ax in (c.get("axes") or [c.get("axis")]):' in src
    assert 'axes[c["axis"]] = axes.get(c["axis"], 0) + 1' not in src


def test_backend_validation_checks_every_axis():
    """§11.1: an off-vocabulary axis anywhere in ``axes`` routes to QA, exactly
    as an off-vocabulary ``axis`` always did. Never a hard reject — no lost
    submissions."""
    def _issues(criterion):
        return validate_submission(
            {}, {"verdict": "A_better", "payload": {"rubric": [criterion]}}, []
        )["issues"]

    assert "unknown_rubric_axis" not in _issues(
        {"text": "x 5 mg", "points": 5, "axes": ["safety", "accuracy"]})
    assert "unknown_rubric_axis" in _issues(
        {"text": "x 5 mg", "points": 5, "axes": ["safety", "vibes"]})
    # The legacy single value is still checked, exactly as before.
    assert "unknown_rubric_axis" in _issues({"text": "x 5 mg", "points": 5, "axis": "vibes"})
    assert "unknown_rubric_axis" not in _issues({"text": "x 5 mg", "points": 5, "axis": "safety"})


def test_backend_schema_round_trips_axes():
    """An older client that sends only ``axis`` is normalized into the new shape
    at the edge, so nothing downstream has to handle two shapes."""
    from asclepius.schemas import RubricCriterion  # noqa: PLC0415

    c = RubricCriterion(text="never gives thrombolytics", points=-9, axis="safety")
    assert c.axes == ["safety"]
    assert c.axis == "safety"
    multi = RubricCriterion(text="x 5 mg", points=5, axes=["safety", "accuracy"])
    assert multi.axes == ["safety", "accuracy"]
    assert multi.axis == "safety"
    empty = RubricCriterion(text="x 5 mg", points=5)
    assert empty.axes == ["accuracy"] and empty.axis == "accuracy"


def test_seeded_criteria_carry_axes():
    """Every auto-seeded criterion ships with the authoritative list too — and
    with exactly ONE axis, so seeding never inflates axis coverage toward
    `premium` on coverage the physician never named."""
    payload = {
        "verdict": "A_better",
        "rejected_critique": {"error_tags": ["dosing_error"], "severities": {"dosing_error": "high"}},
        "chosen_revision": {"why_better_tags": ["safer"]},
        "reasoning_steps": [],
    }
    seeded = R.propose_rubric({}, payload)
    assert seeded, "propose_rubric returned nothing to check"
    for c in seeded:
        assert isinstance(c["axes"], list) and c["axes"], f"{c['source']} has no axes"
        assert len(c["axes"]) == 1, f"{c['source']} seeded more than one axis"
        assert c["axis"] == c["axes"][0]
        assert c["axes"][0] in RUBRIC_AXES


def test_frontend_submits_axes():
    """§11: the submitted payload carries ``axes`` alongside the deprecated
    ``axis``."""
    payload = _body_of("buildSubmissionPayload")
    assert "axes: axs, axis: axs[0] || c.axis || null" in payload
    finish = _body_of("renderRubricFinishCard")
    assert "axes: ['accuracy'], axis: 'accuracy'" in finish, (
        "a manually added criterion must start in the new shape"
    )


# ─── Test §12 ─────────────────────────────────────────────────────────────────
def test_rubric_help_text_matches_the_control():
    """§12: the UI phrase is 'must never' (two words, no hyphen). Help copy that
    names a control has to name the one the physician actually taps."""
    section = _code(_body_of("renderRubricSection"))
    assert "must never do" in section
    assert "'must never'" in section
    assert "must-never" not in section
    gate = _body_of("rubricGate")
    assert "“must never”" in gate and "Critical" in gate


# ═════════════════════════════════════════════════════════════════════════════
#  PART D — chrome (§13)
# ═════════════════════════════════════════════════════════════════════════════

# ─── Test 20 ──────────────────────────────────────────────────────────────────
def test_exp_badge_two_children():
    """§13: ``space-between`` across three children marooned two related links at
    opposite edges of the full width. Grouping them gives it two children —
    'label left, controls right'."""
    fn = _body_of("renderExperienceBadge")
    ret = re.search(
        r"return h\('div', \{ class: 'asc-exp-badge' \},(.*?)\);\s*\}\s*$", fn, re.S
    )
    assert ret, "the badge's return shape changed unexpectedly"
    children = [c.strip() for c in ret.group(1).split("),") if c.strip()]
    assert len(children) == 2, f"expected exactly two children, got {children}"
    assert "asc-exp-badge-label" in children[0]
    assert children[1] == "links"
    assert "class: 'asc-exp-links'" in fn
    assert fn.count("asc-btn-link") == 2, "both links must live inside the group"

    # A drawn divider, not a typed bullet — it is chrome.
    assert ".asc-exp-links .asc-btn-link + .asc-btn-link { border-left:" in CSS
    assert "'·'" not in fn and "' | '" not in fn


def test_no_duplicate_change_experience_link():
    """§13's note: with a specialty chosen, the picker's own header used to emit
    a second 'Change experience' link on the same screen. The popover has no
    header, so it cannot."""
    picker = _code(_body_of("renderSpecialtyPicker"))
    assert "Change experience" not in picker
    assert JS_CODE.count("'Change experience'") == 1


# ═════════════════════════════════════════════════════════════════════════════
#  Test 21 — V4 parity
# ═════════════════════════════════════════════════════════════════════════════
def test_v4_parity():
    """§0.1: ``isV3()`` is the gate, and it already covers V4
    (``case_source == "real_deid"`` renders through the same path). Every gate
    this PRD adds must therefore be an ``isV3()``, never a ``draftVersion() ===
    'v3'`` — which would silently exclude V4 — and never an ``isAssisted()``,
    which would catch V2.
    """
    assert "function isV3() { return draftVersion() === 'v3' || draftVersion() === 'v4'; }" in JS

    # Every gate introduced by this PRD, by the function it lives in.
    for fn_name, needle in (
        ("renderAssistHint", "if (isV3()) return;"),
        ("renderCompareStage", "const showDiff = assisted && !isV3();"),
        ("renderAnswersInto", "!isAssisted() || isV3() || state.showFullText"),
        ("autoSplitChosen", "isV3() ? stepsSkeleton()"),
    ):
        body = _body_of(fn_name)
        assert needle in body, f"{fn_name} no longer gates on isV3(): {needle!r} missing"
        assert "=== 'v3'" not in body, (
            f"{fn_name} gates on the raw version string, which excludes V4"
        )

    # The specialty picker (§2) and the badge (§13) cover v3 AND v4 explicitly.
    badge = _body_of("renderExperienceBadge")
    assert "(v === 'v3' || v === 'v4')" in badge
    assert "v4: 'Real · De-identified Cases'" in badge
    eval_view = _body_of("renderEvalView")
    assert "(ver === 'v3' || ver === 'v4')" in eval_view

    # The V3/V4-only rubric wizard is unreachable from V1/V2 either way, and the
    # step accordion is routed by isV3().
    repaint = _body_of("repaintSteps")
    assert "if (isV3()) renderStepsListV3(listId);" in repaint


def test_no_v3_gate_regressed_to_isAssisted():
    """§0.1, stated as a standing rule: nothing this PRD removes may be removed
    under ``isAssisted()``, which matches V2 — a shipped surface."""
    for fn_name in ("renderAssistHint", "renderAnswersInto", "renderCompareStage"):
        body = _body_of(fn_name)
        if "isAssisted()" in body:
            # Wherever isAssisted() survives it must still lead somewhere for V2:
            # it may never be the sole gate on a V3-only removal.
            assert "isV3()" in body, (
                f"{fn_name} gates a V3-only change on isAssisted() alone — that "
                "would strip the feature from V2 too"
            )


def test_hyperscript_only_no_innerhtml():
    """§0.2: ``h()`` is the hyperscript helper — build DOM with it. No template
    strings, no innerHTML, in anything this PRD touched."""
    for fn_name in (
        "renderSpecialtyPicker", "renderExperienceBadge", "buildStepRowV3",
        "renderStepsListV3", "stepsSkeleton", "renderRubricCriterionCard",
        "repaintStepRow",
    ):
        body = _code(_body_of(fn_name))
        assert "innerHTML" not in body, f"{fn_name} uses innerHTML"
        assert "html:" not in body, f"{fn_name} uses h()'s raw-html escape hatch"
        assert "`" not in body, f"{fn_name} uses a template string"


def test_no_new_component_vocabulary_beyond_the_prd():
    """§0.4: no new component vocabulary unless this document specifies it. The
    classes added here are exactly the ones §2, §5, §8, §9, §10 and §13 name."""
    specified = {
        "asc-sheet", "asc-sheet-card", "asc-sheet-title", "asc-sheet-open",
        "asc-spec-grid", "asc-spec-card", "asc-spec-card-dot", "asc-spec-card-name",
        "asc-steps-skeleton",
        "asc-step-original-sum", "asc-step-original-tag", "asc-step-original-preview",
        "asc-rubric-stem", "asc-rubric-stem-lead", "asc-rubric-stem-toggle",
        "asc-rubric-matter-head", "asc-rubric-scale",
        "asc-exp-links",
    }
    baseline = subprocess.run(
        ["git", "show", "HEAD:frontend/asclepius/asclepius.css"],
        capture_output=True, text=True, cwd=str(_FRONTEND),
    )
    if baseline.returncode != 0:
        pytest.skip("no git baseline available")
    old = set(re.findall(r"\.(asc-[\w-]+)", baseline.stdout))
    new = set(re.findall(r"\.(asc-[\w-]+)", CSS))
    added = new - old
    assert added <= specified, f"undeclared new component vocabulary: {sorted(added - specified)}"


# ═════════════════════════════════════════════════════════════════════════════
#  EXECUTED — the rubric card (§9 §10 §11) and the specialty popover (§2)
#
#  The assertions above prove the code SAYS the right thing. These run it: the
#  shipped functions are extracted and executed against the DOM shim, so what is
#  checked is the state and the markup they actually produce.
# ═════════════════════════════════════════════════════════════════════════════

def _const(name: str) -> str:
    """One top-level ``const NAME = …;`` declaration, verbatim."""
    m = re.search(rf"(const {name} = [\s\S]*?;)\n", JS)
    assert m, f"const {name} not found in asclepius.js"
    return m.group(1)


_RUBRIC_PRELUDE = """
require({dom!r});

const state = {{ draft: {{ rubric: [], rubricCursor: 0 }}, taxonomy: {{}}, specialties: null }};
const saveDraft = () => {{}}, updateSubmitState = () => {{}}, renderRationale = () => {{}};
const emptyAnchor = () => ({{}}), renderAnchorBlock = () => document.createElement('div');
const infoDot = () => document.createElement('span');
const specialtyDot = (sp) => ({{ nephrology: 'asc-dot-green', cardiology: 'asc-dot-orange',
                                oncology: 'asc-dot-pink' }}[sp] || 'asc-dot-lime');
const getPortalSpecialty = () => 'cardiology';
let stored = null;
const setPortalSpecialty = (sp) => {{ stored = sp; }};
const api = async () => ({{ specialties: [
  {{ specialty: 'nephrology', enabled: true }}, {{ specialty: 'cardiology', enabled: true }},
  {{ specialty: 'oncology', enabled: true }}, {{ specialty: 'hepatology', enabled: false }}] }});

// One eval so the consts and the functions share a scope, then hand the two
// entry points out through globalThis.
eval({payload!r} + '\\nglobalThis._card = renderRubricCriterionCard;'
              + '\\nglobalThis._picker = renderSpecialtyPicker;');
const mkCard = globalThis._card, mkPicker = globalThis._picker;
"""


def _rubric_harness(body: str) -> dict:
    payload = "\n".join(
        [_const(n) for n in ("_VAGUE_MARKERS", "_UNIT_RE", "_CLINICAL_RE",
                             "TIER_DEFAULT_PTS", "TIER_CHOICES", "AXIS_LABELS")]
        + [_extract_function(JS, n) for n in (
            "h", "appendChildren", "isSpecificText", "tierForPoints", "autoGrow",
            "criterionAxes", "renderRubricCriterionCard", "chooseSpecialty",
            "renderSpecialtyPicker")]
    )
    return _run_node(_RUBRIC_PRELUDE.format(dom=str(DOM_SHIM), payload=payload) + "\n" + body)


def test_stem_toggle_sets_the_points_sign_executed():
    """§9 executed: the stem toggle is the whole of the old Type field. Flipping
    it must flip the sign of ``c.points`` — that sign is what drives auto-fail
    and the grader's hard-fail, so losing it would lose the data the field
    existed to capture."""
    out = _rubric_harness("""
    const crits = [{ text: 'gives thrombolytics in dissection', points: 5,
                     axis: 'accuracy', source: 'manual' }];
    const card = mkCard(crits, 0);
    const toggle = card.querySelector('.asc-rubric-stem-toggle');
    const before = { text: toggle.textContent, cls: toggle.className, points: crits[0].points };
    toggle.dispatch('click');
    const afterNeg = { text: toggle.textContent, cls: toggle.className,
                       points: crits[0].points, tier: crits[0].tier };
    toggle.dispatch('click');
    console.log(JSON.stringify({
      before, afterNeg,
      backToPos: { text: toggle.textContent, points: crits[0].points },
      lead: card.querySelector('.asc-rubric-stem-lead').textContent,
      typeLabels: card.querySelectorAll('.asc-label').map((l) => l.textContent),
      sevPillsOutsideAxisRow: card.querySelectorAll('.asc-sev-pills')
        .filter((n) => !n.classList.contains('asc-axis-row')).length,
    }));
    """)
    assert out["lead"] == "A correct answer"
    assert out["before"] == {"text": "must", "cls": "asc-rubric-stem-toggle pos", "points": 5}
    assert out["afterNeg"]["text"] == "must never"
    assert out["afterNeg"]["points"] == -5, "the toggle did not flip the sign of c.points"
    assert out["afterNeg"]["cls"] == "asc-rubric-stem-toggle neg"
    assert out["backToPos"] == {"text": "must", "points": 5}, "the toggle must be reversible"
    # The magnitude survives the flip (5 → -5 → 5), so switching polarity never
    # silently re-weights the criterion.
    assert not any("Type" in lbl for lbl in out["typeLabels"]), "a 'Type' label survives"
    assert out["sevPillsOutsideAxisRow"] == 0, "the sign pill row is still rendered"


def test_tier_buttons_and_autofail_executed():
    """§10 executed: the three tiers render as Critical / Major / Minor over the
    unchanged keys, the scale sits inside the question's head row, and a
    critical negative still raises the auto-fail badge."""
    out = _rubric_harness("""
    const crits = [{ text: 'gives thrombolytics in dissection', points: -5, axes: ['safety'] }];
    const card = mkCard(crits, 0);
    const head = card.querySelector('.asc-rubric-matter-head');
    const tierBtns = card.querySelector('.asc-rubric-tier-row').children;
    const autoFail = card.querySelector('.asc-badge-amber');
    const beforeAutoFail = !autoFail.hidden;
    tierBtns[0].dispatch('click');                     // Critical
    const crit = { points: crits[0].points, tier: crits[0].tier, critical: crits[0].critical,
                   autoFail: !autoFail.hidden, pts: card.querySelector('.asc-rubric-pts').textContent };
    tierBtns[2].dispatch('click');                     // Minor
    console.log(JSON.stringify({
      labels: tierBtns.map((b) => b.querySelector('.asc-rubric-tier-name').textContent),
      keys: tierBtns.map((b) => b.dataset.tier),
      expls: tierBtns.map((b) => b.querySelector('.asc-rubric-tier-expl').textContent),
      scaleInHead: !!head.querySelector('.asc-rubric-scale'),
      sliderInHead: !!head.querySelector('.asc-rubric-scale'),
      headLabel: head.querySelector('.asc-label').textContent.trim(),
      beforeAutoFail, crit,
      afterMinor: { points: crits[0].points, tier: crits[0].tier, autoFail: !autoFail.hidden },
      activeTier: tierBtns.filter((b) => b.classList.contains('active')).map((b) => b.dataset.tier),
    }));
    """)
    assert out["labels"] == ["Critical", "Major", "Minor"]
    assert out["keys"] == ["critical", "important", "helpful"], "the wire keys must not change"
    assert out["expls"][0] == "the answer is wrong or unsafe without this"
    assert out["scaleInHead"] and out["sliderInHead"], "the scale is not beside the question"
    assert out["headLabel"] == "How much does it matter?"
    assert out["beforeAutoFail"] is False, "a -5 (Major) negative is not an auto-fail"
    assert out["crit"] == {"points": -9, "tier": "critical", "critical": True,
                           "autoFail": True, "pts": "\u22129"}
    assert out["afterMinor"]["points"] == -2
    assert out["afterMinor"]["autoFail"] is False, "auto-fail must clear when the tier drops"
    assert out["activeTier"] == ["helpful"], "exactly one tier is active at a time"


def test_axis_multiselect_executed():
    """§11 executed: two axes can be active at once, ``c.axis`` tracks
    ``c.axes[0]``, deselecting the last one is a no-op, and no raw enum value
    is ever shown to the physician."""
    out = _rubric_harness("""
    const ENUM = ['accuracy','completeness','safety','reasoning','grounding','communication'];
    const crits = [{ text: 'gives thrombolytics in dissection', points: -9, axis: 'accuracy' }];
    const card = mkCard(crits, 0);
    const row = card.querySelector('.asc-axis-row');
    const by = (t) => row.children.find((b) => b.textContent === t);
    const seeded = { axes: crits[0].axes.slice(), axis: crits[0].axis };

    by('Safe for the patient').dispatch('click');           // add safety
    const two = { axes: crits[0].axes.slice(), axis: crits[0].axis,
                  pressed: row.children.filter((b) => b.getAttribute('aria-pressed') === 'true')
                                       .map((b) => b.textContent) };
    by('Got the facts right').dispatch('click');            // drop accuracy
    const one = { axes: crits[0].axes.slice(), axis: crits[0].axis };
    by('Safe for the patient').dispatch('click');           // last one — no-op
    console.log(JSON.stringify({
      seeded, two, one,
      afterLastDeselect: { axes: crits[0].axes.slice(),
                           pressed: by('Safe for the patient').getAttribute('aria-pressed'),
                           active: by('Safe for the patient').classList.contains('active') },
      labels: row.children.map((b) => b.textContent),
      rawEnumShown: row.children.filter((b) => ENUM.indexOf(b.textContent) !== -1).map((b) => b.textContent),
      titles: row.children.map((b) => b.getAttribute('title')),
    }));
    """)
    # The legacy single `axis` is upgraded to the list shape on mount.
    assert out["seeded"] == {"axes": ["accuracy"], "axis": "accuracy"}
    assert out["two"]["axes"] == ["accuracy", "safety"], "two axes must be able to be active"
    assert out["two"]["axis"] == "accuracy", "c.axis mirrors c.axes[0]"
    assert sorted(out["two"]["pressed"]) == ["Got the facts right", "Safe for the patient"]
    assert out["one"] == {"axes": ["safety"], "axis": "safety"}, "the mirror follows the new first entry"
    assert out["afterLastDeselect"] == {"axes": ["safety"], "pressed": "true", "active": True}, (
        "deselecting the last axis must be a no-op — a criterion with no axis is unscoreable"
    )
    assert out["rawEnumShown"] == [], f"raw enum values leaked as button text: {out['rawEnumShown']}"
    assert out["labels"][0] == "Got the facts right"
    assert all(out["titles"]), "each option keeps its one-line explanation as a tooltip"


def test_specialty_popover_executed():
    """§2 executed: the sheet mounts over the current view, offers exactly the
    enabled specialties as one card each, resolves with the pick, and cleans up
    after itself — no route, no leaked scroll lock, no orphaned listener."""
    out = _rubric_harness("""
    (async () => {
      const p = mkPicker({ dismissable: true });
      await new Promise((r) => setTimeout(r, 0));
      const sheet = document.body.children[0];
      const grid = sheet.querySelector('.asc-spec-grid');
      const mounted = {
        cls: sheet.className, role: sheet.getAttribute('role'),
        modal: sheet.getAttribute('aria-modal'),
        labelledby: sheet.getAttribute('aria-labelledby'),
        title: sheet.querySelector('.asc-sheet-title').textContent,
        locked: document.body.classList.contains('asc-sheet-open'),
        n: grid.children.length,
        names: grid.children.map((c) => c.querySelector('.asc-spec-card-name').textContent),
        allButtons: grid.children.every((c) => c.tagName === 'BUTTON'),
        nestedCtas: grid.querySelectorAll('.asc-btn-primary').length,
        lastUsed: grid.querySelector('.last-used').querySelector('.asc-spec-card-name').textContent,
        dots: grid.children.map((c) => c.querySelector('.asc-spec-card-dot').className),
        focused: document.activeElement && document.activeElement.querySelector('.asc-spec-card-name').textContent,
      };
      grid.children[2].dispatch('click');
      const picked = await p;
      console.log(JSON.stringify({
        mounted, picked, stored, chosen: state.specialtyChosen,
        unmounted: document.body.children.length === 0,
        unlocked: !document.body.classList.contains('asc-sheet-open'),
        listenersLeft: (document._listeners.keydown || []).length,
      }));
    })();
    """)
    m = out["mounted"]
    assert m["cls"] == "asc-sheet" and m["role"] == "dialog" and m["modal"] == "true"
    assert m["labelledby"] == "ascSheetTitle" and m["title"] == "Choose a specialty"
    assert m["locked"] is True, "the page behind a modal must not scroll under it"
    assert m["n"] == 3, "only enabled specialties, one card each"
    assert m["names"] == ["Nephrology", "Cardiology", "Oncology"]
    assert m["allButtons"], "the card IS the button"
    assert m["nestedCtas"] == 0, "'Grade X →' is still nested inside the card"
    assert m["lastUsed"] == "Cardiology"
    # Accent semantics: nephrology green · cardiology orange · oncology pink.
    assert m["dots"] == ["asc-spec-card-dot asc-dot-green",
                         "asc-spec-card-dot asc-dot-orange",
                         "asc-spec-card-dot asc-dot-pink"]
    assert m["focused"] == "Cardiology", "focus should land on the last-used specialty"

    assert out["picked"] == "oncology"
    assert out["stored"] == "oncology" and out["chosen"] is True
    assert out["unmounted"], "the sheet must remove itself"
    assert out["unlocked"], "the scroll lock leaked"
    assert out["listenersLeft"] == 0, "the keydown listener leaked"


def test_specialty_popover_escape_rules_executed():
    """§2 executed: Esc dismisses only when a specialty already exists. On first
    entry the choice is required, so an escape hatch would leave the physician
    on a view with no case in it."""
    out = _rubric_harness("""
    (async () => {
      // Required (first entry): Esc must do nothing.
      const required = mkPicker({ dismissable: false });
      await new Promise((r) => setTimeout(r, 0));
      document.dispatch('keydown', { key: 'Escape' });
      await new Promise((r) => setTimeout(r, 0));
      const stillUp = document.body.children.length === 1;
      // Picking is the only way out.
      document.body.children[0].querySelector('.asc-spec-grid').children[0].dispatch('click');
      const forced = await required;

      // Dismissable ("Change specialty"): Esc resolves null and changes nothing.
      const optional = mkPicker({ dismissable: true });
      await new Promise((r) => setTimeout(r, 0));
      document.dispatch('keydown', { key: 'Escape' });
      const dismissed = await optional;

      // …and so does a click on the backdrop itself.
      const optional2 = mkPicker({ dismissable: true });
      await new Promise((r) => setTimeout(r, 0));
      const sheet = document.body.children[0];
      sheet.dispatch('click', { target: sheet });
      const backdrop = await optional2;

      // A click INSIDE the card must not dismiss it.
      const optional3 = mkPicker({ dismissable: true });
      await new Promise((r) => setTimeout(r, 0));
      const sheet3 = document.body.children[0];
      const card3 = sheet3.querySelector('.asc-sheet-card');
      sheet3.dispatch('click', { target: card3 });
      await new Promise((r) => setTimeout(r, 0));
      const survivedInnerClick = document.body.children.length === 1;
      document.body.children[0].querySelector('.asc-spec-grid').children[0].dispatch('click');
      await optional3;

      console.log(JSON.stringify({
        stillUp, forced, dismissed, backdrop, survivedInnerClick,
        clean: document.body.children.length === 0,
        listenersLeft: (document._listeners.keydown || []).length,
      }));
    })();
    """)
    assert out["stillUp"], "Esc dismissed a required picker — that strands the view with no case"
    assert out["forced"] == "nephrology"
    assert out["dismissed"] is None, "Esc should resolve null on a dismissable picker"
    assert out["backdrop"] is None, "a backdrop click should dismiss a dismissable picker"
    assert out["survivedInnerClick"], "a click inside the card must not dismiss the sheet"
    assert out["clean"] and out["listenersLeft"] == 0, "every path must clean up"
