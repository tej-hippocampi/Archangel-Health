"""Longitudinal cases — the physician-facing surfaces (Longitudinal Cases PRD).

Two kinds of assertion, in descending order of strength (same discipline as
``test_asclepius_eval_ui_overhaul``):

  1. **Executed.** The commitment card, the self-score card and the outcome
     panel are extracted from the shipped source and run under node against the
     minimal DOM shim, so what is asserted is behaviour produced by the shipped
     code — not a Python re-derivation of it.
  2. **Structural.** The rules that are about what the client must NOT do — never
     render the future before the commit, never invent a CSS class with no style
     behind it — are assertions about the shipped source text. A DOM-free
     environment cannot observe "this data never reached the browser"; it can
     observe that the code which would have fetched it is gated, which is the
     same guarantee one step earlier.

The load-bearing one is the seal. The client is NOT where it is enforced — the
server refuses the reveal without a stored submission — but a client that fetched
the outcome early would still put the future on the physician's screen, so the
order of operations in ``submitEvaluation`` is asserted here directly.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
JS_PATH = _FRONTEND / "asclepius.js"
CSS_PATH = _FRONTEND / "asclepius.css"
DOM_SHIM = pathlib.Path(__file__).resolve().parent / "_asclepius_dom.js"

JS = JS_PATH.read_text(encoding="utf-8")
CSS = CSS_PATH.read_text(encoding="utf-8")

_LINE_COMMENT = re.compile(r"^\s*//.*$", re.M)


def _code(src: str) -> str:
    """Source with whole-line ``//`` comments stripped.

    Absence assertions have to run against code, not prose: the comments
    explaining a rule necessarily name the thing the rule forbids, and a test
    that trips over its own explanation pushes the next person to delete the
    explanation."""
    return _LINE_COMMENT.sub("", src)


def _extract_function(src: str, name: str) -> str:
    marker = f"function {name}("
    start = src.index(marker)
    if src[start - 6: start] == "async ":
        start -= 6
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start: i + 1]
    raise AssertionError(f"unbalanced braces extracting {name} from asclepius.js")


def _body_of(name: str) -> str:
    return _extract_function(JS, name)


JS_CODE = _code(JS)


def _run_node(script: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed in this environment")
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\n{proc.stderr}\n{proc.stdout}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


_PRELUDE = """
require({dom!r});

// Everything the trajectory surfaces touch that is not under test.
const state = {{
  task: {{ task_id: 't1', trajectory_id: null, sequence_index: null, grounding_mode: 'optional' }},
  trajectoryProgress: null,
  specialties: [],
  draft: {{
    portal_version: 'v4',
    expected_trajectory: {{ expectations: [{{ expectation: '', horizon_days: '' }}],
                            falsifiers: [''], note: '' }},
  }},
}};
function isV3() {{ return true; }}
function saveDraft() {{}}
function toast() {{}}
function api() {{ return Promise.resolve({{}}); }}
function clear(node) {{ while (node.firstChild) node.removeChild(node.firstChild); }}
function autoGrow(ta) {{ return ta; }}
function infoDot() {{ return document.createElement('span'); }}
function renderLabsTrend(panels) {{
  const t = document.createElement('table');
  t.dataset.panels = String((panels || []).length);
  return t;
}}
function renderEvalView() {{}}
function openTaskById(id) {{ globalThis.__opened = id; }}
function stopTimer() {{}}
function renderHeader() {{}}
function setRoot() {{}}

// The self-score vocabulary is a const, not a function, so it cannot be pulled in
// by ``_extract_function``. Sliced verbatim from the shipped source instead of
// re-declared, so a test can never assert against a vocabulary the product does
// not actually use.
{consts}

{funcs}
"""


def _const(name: str) -> str:
    """The shipped source of one top-level ``const NAME = [...]``."""
    start = JS.index(f"const {name} =")
    end = JS.index("];", start) + 2
    return JS[start:end]


def _harness(names, body: str) -> dict:
    funcs = "\n".join(_extract_function(JS, n) for n in names)
    return _run_node(
        _PRELUDE.format(dom=str(DOM_SHIM), funcs=funcs, consts=_const("SELF_SCORE_CHOICES"))
        + "\n" + body)


def _text(node_dump) -> str:
    return node_dump if isinstance(node_dump, str) else json.dumps(node_dump)


# ═══════════════════════════════════════════════════════════════════════════════
# §3.3 field 3 — the commitment surface
# ═══════════════════════════════════════════════════════════════════════════════
def test_the_commitment_card_asks_both_questions_executed():
    """Assessment and plan are opinions. This card is what makes the submission a
    PREDICTION, and it only does that if it asks for the falsifier too."""
    out = _harness(["h", "appendChildren", "renderExpectedTrajectoryCard"], """
    const card = renderExpectedTrajectoryCard();
    const labels = [];
    (function walk(n) {
      if (n.className && String(n.className).indexOf('asc-label') >= 0) labels.push(n.textContent);
      (n.childNodes || []).forEach(walk);
    })(card);
    console.log(JSON.stringify({ labels: labels, optional: card.textContent.indexOf('Optional') >= 0 }));
    """)
    joined = " ".join(out["labels"]).lower()
    assert "what should happen next" in joined
    assert "wrong" in joined, "the falsifier question is missing — this is field 3"
    # OPTIONAL, and it must stay optional: a fabricated falsifier is worse than
    # none, because it gets scored against a real chart.
    assert out["optional"] is True


def test_the_commitment_card_is_not_rendered_on_v1_v2():
    """V1/V2 must stay byte-for-byte unchanged; the card is an isV3() surface."""
    body = _code(_body_of("renderExpectedTrajectoryCard"))
    assert "if (!isV3()) return null;" in body


def test_expectations_and_falsifiers_are_independently_repeatable_executed():
    out = _harness(["h", "appendChildren", "renderExpectedTrajectoryCard"], """
    const card = renderExpectedTrajectoryCard();
    const buttons = [];
    (function walk(n) {
      if (n.tagName === 'BUTTON') buttons.push(n.textContent);
      (n.childNodes || []).forEach(walk);
    })(card);
    const before = state.draft.expected_trajectory.expectations.length;
    buttons.forEach(() => {});
    console.log(JSON.stringify({ buttons: buttons, before: before }));
    """)
    assert any("Add another expectation" in b for b in out["buttons"])
    assert any(b.strip() == "+ Add another" for b in out["buttons"])


def test_a_horizon_input_is_offered_on_every_expectation_executed():
    """A prediction with no horizon is not falsifiable — "bilirubin will fall" is
    true eventually. Optional to fill, asked for every time."""
    out = _harness(["h", "appendChildren", "renderExpectedTrajectoryCard"], """
    const card = renderExpectedTrajectoryCard();
    const inputs = [];
    (function walk(n) {
      if (n.tagName === 'INPUT') inputs.push({ type: n.attributes.type, ph: n.attributes.placeholder });
      (n.childNodes || []).forEach(walk);
    })(card);
    console.log(JSON.stringify({ inputs: inputs }));
    """)
    assert any(i["type"] == "number" for i in out["inputs"])


# ═══════════════════════════════════════════════════════════════════════════════
# §3.2 — the seal, as an order of operations in the client
# ═══════════════════════════════════════════════════════════════════════════════
def test_the_outcome_is_never_fetched_before_the_submission_lands():
    """The server refuses the reveal without a stored submission, so this is the
    second line of defence — but a client that fetched early would still put the
    future on screen, and the submit path is where that would happen."""
    submit = _code(_body_of("submitEvaluation"))
    assert "renderTrajectoryOutcomeView" in submit
    # The reveal call must sit AFTER the POST that commits the answer.
    post = submit.index("'/submissions?async_pipeline=1'")
    reveal = submit.index("renderTrajectoryOutcomeView(")
    assert reveal > post, (
        "the outcome reveal is reachable before the submission POST — the seal is "
        "what converts an opinion into a prediction")


def test_the_reveal_only_fires_for_a_point_that_carried_a_prediction():
    """No prediction, nothing to check: the reveal would be a spoiler with no
    purpose, on a chart the physician may still have points left to walk."""
    submit = _code(_body_of("submitEvaluation"))
    assert "payload.expected_trajectory" in submit
    assert "state.task.trajectory_id" in submit


def test_a_failed_reveal_never_loses_the_submitted_work():
    """The submission is committed server-side before this runs. A reveal failure
    is a display problem, and it must read as one."""
    body = _code(_body_of("renderTrajectoryOutcomeView"))
    assert "Your answer is saved" in body
    assert "renderEvalView()" in body


# ═══════════════════════════════════════════════════════════════════════════════
# §4 Phase 4 — the reveal and the self-score
# ═══════════════════════════════════════════════════════════════════════════════
def test_the_outcome_panel_dates_everything_from_the_decision_executed():
    """"Day +12", counted from the moment the physician committed. A relative day
    with no stated origin is the one number a longitudinal case cannot afford to
    leave ambiguous."""
    out = _harness(["h", "appendChildren", "renderOutcomePanel"], """
    const panel = renderOutcomePanel({
      lab_panels: [{ panel: 'LFT', collected_offset_days: 20 }],
      notes: [{ note_type: 'Progress', author_role: 'gi', collected_offset_days: 12, text: 'GGT 983.' }],
      studies: [], medications: [{ drug: 'ceftriaxone', collected_offset_days: 14 }],
      problem_list: [{ condition: 'Stent occlusion', collected_offset_days: 15 }],
      study_findings_policy: 'visible', days_after_decision: 30,
    });
    console.log(JSON.stringify({ text: panel.textContent }));
    """)
    assert "day +12" in out["text"]
    assert "day +14" in out["text"]
    assert "day +15" in out["text"]


def test_the_outcome_panel_honours_a_hidden_findings_policy_executed():
    """§9.5 — the policy is computed per truncation and legitimately varies across
    one walk. The reveal must honour the window's own policy, not assume the
    walk's first one."""
    out = _harness(["h", "appendChildren", "renderOutcomePanel"], """
    const shown = renderOutcomePanel({
      lab_panels: [], notes: [], medications: [], problem_list: [],
      studies: [{ label: 'CT abdomen', collected_offset_days: 9, findings: 'Duct dilated.' }],
      study_findings_policy: 'visible', days_after_decision: 10 });
    const hidden = renderOutcomePanel({
      lab_panels: [], notes: [], medications: [], problem_list: [],
      studies: [{ label: 'CT abdomen', collected_offset_days: 9, findings: 'Duct dilated.' }],
      study_findings_policy: 'hidden', days_after_decision: 10 });
    console.log(JSON.stringify({ shown: shown.textContent, hidden: hidden.textContent }));
    """)
    assert "Duct dilated." in out["shown"]
    assert "Duct dilated." not in out["hidden"]
    assert "withheld" in out["hidden"].lower()


def test_an_empty_outcome_window_is_a_real_answer_not_a_broken_panel_executed():
    out = _harness(["h", "appendChildren", "renderOutcomePanel"], """
    const panel = renderOutcomePanel({ lab_panels: [], notes: [], studies: [],
      medications: [], problem_list: [], days_after_decision: 4 });
    console.log(JSON.stringify({ text: panel.textContent }));
    """)
    assert "adds nothing" in out["text"]


def test_not_assessable_is_a_first_class_self_score_state():
    """The next encounter frequently does not contain the observation the
    prediction was about. Forcing a binary there manufactures a verification
    nobody made — the same rule as the reviewer's ``cannot_assess``."""
    src = _code(JS)
    block = src[src.index("const SELF_SCORE_CHOICES"):]
    block = block[: block.index("];") + 2]
    assert "'held'" in block and "'did_not_hold'" in block and "'not_assessable'" in block


def test_the_self_score_gate_requires_at_least_one_mark_executed():
    out = _harness(["h", "appendChildren", "renderSelfScoreCard"], """
    const card = renderSelfScoreCard(
      { task_id: 't1' },
      { progress: {} },
      [{ expectation: 'enzymes stay down', horizon_days: 21 }],
      ['GGT climbs again']);
    let save = null; const pills = [];
    (function walk(n) {
      if (n.tagName === 'BUTTON') {
        if (n.textContent === 'Save and continue') save = n;
        else pills.push(n);
      }
      (n.childNodes || []).forEach(walk);
    })(card);
    const before = save.disabled;
    pills[0].dispatch('click');
    console.log(JSON.stringify({ before: before, after: save.disabled,
                                 active: pills[0].className.indexOf('active') >= 0,
                                 falsifierShown: card.textContent.indexOf('GGT climbs again') >= 0 }));
    """)
    assert out["before"] is True, "the save button was live with nothing marked"
    assert out["after"] is False
    assert out["active"] is True
    # The physician's OWN falsifier is the rubric, so it has to be on screen.
    assert out["falsifierShown"] is True


def test_the_self_score_card_states_what_the_check_cannot_show():
    """§6 — what happened next reflects the treatment actually given, not the
    physician's plan. Said at the moment they grade, not only in a data
    dictionary a buyer reads."""
    body = _body_of("renderSelfScoreCard")
    assert "actually given" in body
    assert "does not test your plan" in body


def test_the_walk_continues_on_the_same_patient_not_a_fresh_queue_draw():
    """§5 — reading a new chart is the expensive part of a task, and the whole
    per-decision time saving comes from paying it once."""
    body = _code(_body_of("continueTrajectory"))
    assert "openTaskById(next)" in body
    assert "next_task_id" in body


# ═══════════════════════════════════════════════════════════════════════════════
# §9.1 — what the client does with the 409
# ═══════════════════════════════════════════════════════════════════════════════
def test_an_out_of_order_open_explains_itself_and_offers_the_next_point():
    """The gate is server-side; this is the client not turning a correctly-working
    rule into a generic failure message."""
    body = _code(_body_of("openTaskById"))
    assert "trajectory_out_of_order" in body
    assert "e.detail.next_task_id" in body


def test_the_client_never_enforces_the_sequence_itself():
    """Sequence is a correctness property of the task and belongs in the query
    that decides servability. A client-side gate would be defeated by a hand-typed
    task id or a second tab — and would invite deleting the server one."""
    src = JS_CODE
    for forbidden in ("sequence_index <", "sequence_index >", "sequence_index !=="):
        assert forbidden not in src, (
            f"the client is comparing {forbidden!r} — the sequence gate must live "
            "in the candidate query and the by-ID path, never here")


# ═══════════════════════════════════════════════════════════════════════════════
# §3.5 / §5 — the walk is visible to the physician
# ═══════════════════════════════════════════════════════════════════════════════
def test_the_banner_names_the_step_and_the_seal_executed():
    out = _harness(["h", "appendChildren", "renderTrajectoryBanner"], """
    const none = renderTrajectoryBanner();
    state.task.trajectory_id = 'traj-abc';
    state.task.sequence_index = 2;
    state.trajectoryProgress = { n_points: 13, n_answered: 2 };
    const withWalk = renderTrajectoryBanner();
    console.log(JSON.stringify({ none: none, text: withWalk.textContent }));
    """)
    assert out["none"] is None, "an ordinary case must render no banner at all"
    assert "Decision 3 of 13" in out["text"]
    assert "sealed" in out["text"]


def test_the_banner_degrades_rather_than_lying_executed():
    """The walk metadata is fetched best-effort. A stale or missing count must
    never produce "Decision 4 of 13" on a chart with a different length."""
    out = _harness(["h", "appendChildren", "renderTrajectoryBanner"], """
    state.task.trajectory_id = 'traj-abc';
    state.task.sequence_index = 0;
    state.trajectoryProgress = null;
    console.log(JSON.stringify({ text: renderTrajectoryBanner().textContent }));
    """)
    assert "Decision 1" in out["text"]
    assert " of " not in out["text"].split("One patient")[0]


def test_progress_is_reset_before_it_is_rehydrated():
    """A count carried over from the previous case would put the wrong step number
    on an unrelated chart, which is worse than no banner."""
    for fn in ("renderEvalView", "openTaskById"):
        body = _code(_body_of(fn))
        assert "state.trajectoryProgress = null;" in body, fn
        reset = body.index("state.trajectoryProgress = null;")
        assert "trajectories/" in body[reset:], f"{fn} resets but never rehydrates"


# ═══════════════════════════════════════════════════════════════════════════════
# §2 / §9.3 — the admin console tells the truth about count and cost
# ═══════════════════════════════════════════════════════════════════════════════
def test_the_plan_states_both_the_gate_count_and_the_verifiable_count():
    """They are never the same number: a walk of N points yields N−1 verifiable
    ones, and pricing is per decision point."""
    src = JS_CODE
    assert "plan.decision_points" in src
    assert "plan.verifiable_decision_points" in src


def test_a_skipped_encounter_says_which_threshold_it_missed_executed():
    out = _harness(["h", "appendChildren", "renderDensityLine"], """
    const pass = renderDensityLine({ qualifies_as_decision_point: true, outcome_verifiable: true,
      density: { n_distinct_dates: 3, n_events: 21, n_resource_types: 3, reasons: [] } });
    const terminal = renderDensityLine({ qualifies_as_decision_point: true, outcome_verifiable: false,
      density: { n_distinct_dates: 3, n_events: 21, n_resource_types: 3, reasons: [] } });
    const fail = renderDensityLine({ qualifies_as_decision_point: false,
      density: { n_distinct_dates: 1, n_events: 2, n_resource_types: 1,
                 reasons: ['1 distinct date(s); the gate is 2', '2 recorded event(s); the gate is 8'] } });
    console.log(JSON.stringify({ pass: pass.textContent, terminal: terminal.textContent,
                                 fail: fail.textContent, none: renderDensityLine({}) }));
    """)
    assert "3 date(s), 21 event(s), 3 resource type(s)" in out["pass"]
    assert "a later encounter can check it" in out["pass"]
    assert "nothing later in the record" in out["terminal"]
    assert "the gate is 8" in out["fail"]
    assert out["none"] is None


def test_the_trajectory_button_states_the_cost_before_it_writes_anything():
    """§9.3 — a trajectory is not a discount on physician time; it is N tasks that
    happen to share a chart. Say the number before the click, not after."""
    src = JS_CODE
    modal = src[src.index("function openCasePlanModal"):]
    modal = modal[: modal.index("document.body.appendChild(overlay)")]
    assert "trajectory: true" in modal
    assert "nPoints * 75" in modal, "the physician cost is not stated before generating"
    assert "window.confirm(" in modal
    assert "single-labelled" in modal


# ═══════════════════════════════════════════════════════════════════════════════
# Rendered appearance — no class without a style behind it
# ═══════════════════════════════════════════════════════════════════════════════
def test_every_class_the_new_surfaces_use_exists_in_the_stylesheet():
    """A class with no CSS renders as an unstyled element and is invisible to every
    source assertion — the exact defect class the rendered-appearance CI job was
    added for. Cheaper to catch here."""
    used = set()
    for fn in ("renderExpectedTrajectoryCard", "renderOutcomePanel",
               "renderSelfScoreCard", "renderTrajectoryBanner", "renderDensityLine",
               "paintTrajectoryOutcome"):
        for match in re.finditer(r"class:\s*'([^']+)'", _body_of(fn)):
            for cls in match.group(1).split():
                if cls.startswith("asc-"):
                    used.add(cls)
    assert used, "extraction found no classes — the harness is broken, not the code"
    missing = [c for c in sorted(used) if f".{c}" not in CSS]
    assert not missing, f"classes used with no style behind them: {missing}"
