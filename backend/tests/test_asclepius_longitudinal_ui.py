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
# The admin half of this feature (the plan modal, the density line) moved to
# the console's own bundle with PRD-F. Same renderers, different file, so the
# extractors below look in both rather than pinning either.
ADMIN_JS = (_FRONTEND / "admin_shell.js").read_text(encoding="utf-8")
_SOURCES = (JS, ADMIN_JS)
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
    for src in _SOURCES:
        if f"function {name}(" in src:
            return _extract_function(src, name)
    raise AssertionError(f"no bundle defines {name}")


JS_CODE = _code(JS + "\n" + ADMIN_JS)


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
require({case_panel!r});

// Everything the trajectory surfaces touch that is not under test.
const REALM = 'production';
const memory = new Map();
const localStorage = {{ getItem: k => memory.get(k) || null, setItem: (k,v) => memory.set(k,v), removeItem: k => memory.delete(k) }};
const state = {{
  user: {{ id: 'physician-a' }}, token: 'token-a', view: 'eval', panel: 'tasks',
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
function renderEvalView() {{}}
function openTaskById(id) {{ globalThis.__opened = id; }}
function stopTimer() {{}}
function renderHeader() {{}}
function setRoot() {{ state.screenGeneration = (state.screenGeneration || 0) + 1; }}

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
    recovery_helpers = ["draftContentFingerprint", "isDuplicateTrajectorySubmission", "trajectoryRecoveryKey", "trajectoryRecoveries", "rememberTrajectoryOutcome", "clearTrajectoryOutcome", "readCaseTransition", "renderTrajectoryLoadError"]
    funcs = "\n".join(_body_of(n) for n in dict.fromkeys(recovery_helpers + names))
    return _run_node(
        _PRELUDE.format(dom=str(DOM_SHIM), case_panel=str(_FRONTEND / "case_panel.js"), funcs=funcs, consts=_const("SELF_SCORE_CHOICES"))
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


@pytest.mark.parametrize("version,seamless", [("v1", False), ("v2", False), ("v3", True), ("v4", True), ("v5", True)])
def test_the_real_version_predicate_keeps_v5_predictions_visible(version, seamless):
    out = _harness([
        "h", "appendChildren", "draftVersion", "isV3",
        "renderExpectedTrajectoryCard", "renderExperienceBadge",
    ], """
    state.draft.portal_version = %s;
    state.task.trajectory_id = state.draft.portal_version === 'v5' ? 'walk-1' : null;
    const card = renderExpectedTrajectoryCard();
    console.log(JSON.stringify({seamless: isV3(), visible: !!card,
      badge: renderExperienceBadge().textContent}));
    """ % json.dumps(version))
    assert out["seamless"] is seamless
    assert out["visible"] is seamless
    if version == "v5":
        assert "Longitudinal" in out["badge"]
        assert "Classic" not in out["badge"]


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


def test_every_submitted_longitudinal_point_keeps_the_walk_continuation():
    """Predictions are optional; skipping one must not drop the physician into a
    different queue selected by an old V4 browser preference."""
    submit = _code(_body_of("submitEvaluation"))
    assert "task.trajectory_id ? task : null" in submit
    assert "&& payload.expected_trajectory" not in submit


def test_a_failed_outcome_fetch_stays_retryable_and_preserves_recovery():
    out = _harness(["h", "appendChildren", "renderTrajectoryOutcomeView"], """
    let rendered;
    setRoot = node => { rendered=node; state.screenGeneration=(state.screenGeneration||0)+1; };
    function renderDashboardView() {}
    api = async () => { throw {status:503,message:'temporary outage'}; };
    (async () => {
      await renderTrajectoryOutcomeView({task_id:'t1',trajectory_id:'walk'});
      console.log(JSON.stringify({text:rendered.textContent,
        pending:Object.keys(trajectoryRecoveries())}));
    })();
    """)
    assert 'Your answer is saved' in out['text']
    assert 'Try again' in out['text']
    assert out['pending'] == ['t1']


# ═══════════════════════════════════════════════════════════════════════════════
# §4 Phase 4 — the reveal and the self-score
# ═══════════════════════════════════════════════════════════════════════════════
def test_the_outcome_panel_dates_everything_from_the_decision_executed():
    """"Day +12", counted from the moment the physician committed. A relative day
    with no stated origin is the one number a longitudinal case cannot afford to
    leave ambiguous."""
    out = _harness(["h", "appendChildren", "renderOutcomePanel"], """
    const panel = renderOutcomePanel({
      lab_panels: [{ panel: 'Chemistry', collected_offset_days: 20,
        results: [{analyte:'Potassium', value:5.8, unit:'mmol/L', ref_low:3.5, ref_high:5, flag:'H'}] }],
      notes: [{ note_type: 'Progress', author_role: 'gi', collected_offset_days: 12, text: 'GGT 983.' }],
      studies: [], medications: [{ drug: 'ceftriaxone', collected_offset_days: 14 }],
      problem_list: [{ condition: 'Stent occlusion', collected_offset_days: 15 }],
      study_findings_policy: 'visible', days_after_decision: 30,
    });
    console.log(JSON.stringify({ text: panel.textContent }));
    """)
    assert "Potassium (mmol/L)" in out["text"]
    assert "5.8 H" in out["text"]
    assert "day +20" in out["text"]
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
    assert "openTaskById(next, progress, finish)" in body
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
    out = _harness(["h", "appendChildren", "trajectoryStepLabel", "renderTrajectoryBanner"], """
    const none = renderTrajectoryBanner();
    state.task.trajectory_id = 'traj-abc';
    state.task.sequence_index = 2;
    state.trajectoryProgress = { n_points: 13, n_answered: 2 };
    const withWalk = renderTrajectoryBanner();
    console.log(JSON.stringify({ none: none, text: withWalk.textContent }));
    """)
    assert out["none"] is None, "an ordinary case must render no banner at all"
    assert "Step 3 of 13" in out["text"]
    assert "sealed" in out["text"]


def test_the_banner_degrades_rather_than_lying_executed():
    """The walk metadata is fetched best-effort. A stale or missing count must
    never produce "Step 4 of 13" on a chart with a different length."""
    out = _harness(["h", "appendChildren", "trajectoryStepLabel", "renderTrajectoryBanner"], """
    state.task.trajectory_id = 'traj-abc';
    state.task.sequence_index = 0;
    state.trajectoryProgress = null;
    console.log(JSON.stringify({ text: renderTrajectoryBanner().textContent }));
    """)
    assert "Step 1" in out["text"]
    assert " of " not in out["text"].split("One patient")[0]


def test_progress_is_reset_before_it_is_rehydrated():
    """A count carried over from the previous case would put the wrong step number
    on an unrelated chart, which is worse than no banner."""
    for fn in ("renderEvalView", "openTaskById"):
        body = _code(_body_of(fn))
        reset_statement = "state.trajectoryProgress = progress || null;" if fn == "openTaskById" else "state.trajectoryProgress = null;"
        assert reset_statement in body, fn
        reset = body.index(reset_statement)
        assert "trajectories/" in body[reset:], f"{fn} resets but never rehydrates"


@pytest.mark.parametrize('point_class,label', [('interval', 'interval visit'), ('decision', 'decision point')])
def test_both_physician_headers_show_the_point_class(point_class, label):
    out = _harness(["h", "appendChildren", "trajectoryStepLabel", "renderTrajectoryBanner",
                    "paintTrajectoryOutcome"], """
    state.task = {task_id:'t3',trajectory_id:'walk',sequence_index:2,generation:{point_class:CLASS}};
    state.trajectoryProgress = {n_points:7};
    const banner = renderTrajectoryBanner().textContent;
    let outcomeText;
    setRoot = n => {outcomeText=n.textContent;};
    function trajectoryContinueButton() {return h('button',{},'Continue');}
    paintTrajectoryOutcome(state.task, {sequence_index:2,outcome:null});
    console.log(JSON.stringify({banner,outcomeText}));
    """.replace('CLASS', json.dumps(point_class)))
    assert f'Step 3 of 7 · {label}' in out['banner']
    assert f'Step 3 of 7 · {label}' in out['outcomeText']


def test_terminal_reveal_with_a_prediction_has_continue_but_no_score_card_executed():
    out = _harness(["h", "appendChildren", "trajectoryStepLabel", "paintTrajectoryOutcome",
                    "trajectoryContinueButton", "continueTrajectory"], """
    let root;
    setRoot = n => {root=n;};
    function renderSelfScoreCard() {throw new Error('terminal point offered a score');}
    paintTrajectoryOutcome(state.task, {outcome:null,
      expected_trajectory:{expectations:[{expectation:'Symptoms improve.'}]},
      progress:{next_task_id:'next-live-point'}});
    root.querySelector('.asc-btn').dispatch('click');
    console.log(JSON.stringify({text:root.textContent,opened:globalThis.__opened}));
    """)
    assert 'no later outcome to score' in out['text']
    assert 'Continue' in out['text']
    assert out['opened'] == 'next-live-point'


def test_interval_rows_include_downgrades_and_honest_terminal_state():
    out = _harness(["h", "appendChildren", "renderDensityLine"], """
    const p = {point_class:'interval',qualifies_as_point:true,outcome_verifiable:true};
    console.log(JSON.stringify({
      interval:renderDensityLine(p).textContent,
      downgraded:renderDensityLine({...p,downgraded:'no presenting narrative'}).textContent,
      terminal:renderDensityLine({...p,outcome_verifiable:false,downgraded:'no presenting narrative'}).textContent,
    }));
    """)
    assert out['interval'] == 'Interval visit · graded by the next point'
    assert 'Interval visit (downgraded: no presenting narrative from this encounter)' in out['downgraded']
    assert 'terminal point' in out['terminal'] and 'graded by the next point' not in out['terminal']
    assert 'held' not in json.dumps(out).lower()


# ═══════════════════════════════════════════════════════════════════════════════
# §2 / §9.3 — the admin console tells the truth about count and cost
# ═══════════════════════════════════════════════════════════════════════════════
def test_the_plan_states_both_the_gate_count_and_the_verifiable_count():
    """They are never the same number: a walk of N points yields N−1 verifiable
    ones, and physician pay is per submission for both classes."""
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


# Recovery and races run the shipped client functions with delayed network replies.
def test_outcome_response_cannot_replace_a_newer_screen():
    out = _harness(["h", "appendChildren", "renderTrajectoryOutcomeView"], """
    let finish, painted=false;
    api = () => new Promise(resolve => { finish=resolve; });
    function paintTrajectoryOutcome() { painted=true; }
    (async () => {
      const pending=renderTrajectoryOutcomeView({task_id:'t1',trajectory_id:'walk'});
      state.view='home'; setRoot();
      finish({outcome:null}); await pending;
      console.log(JSON.stringify({painted,pending:Object.keys(trajectoryRecoveries())}));
    })();
    """)
    assert out == {'painted': False, 'pending': ['t1']}


def test_score_marks_survive_remount_and_are_isolated_by_account():
    out = _harness(["h", "appendChildren", "renderSelfScoreCard", "renderPendingTrajectoryOutcome"], """
    const task={task_id:'t1',trajectory_id:'walk'};
    const expected=[{expectation:'improves'}];
    const card=renderSelfScoreCard(task,{},expected,['worsens']);
    card.querySelectorAll('.asc-conf-pill')[1].dispatch('click');
    const note=card.querySelector('.asc-input'); note.value='Observed in the record'; note.dispatch('input');
    const recovered=renderSelfScoreCard(task,{},expected,['worsens']);
    const ours=renderPendingTrajectoryOutcome().textContent;
    state.user={id:'physician-b'};
    const other=renderPendingTrajectoryOutcome();
    console.log(JSON.stringify({ours,other,mark:recovered.querySelectorAll('.asc-conf-pill')[1].classList.contains('active'),
      note:recovered.querySelector('.asc-input').value}));
    """)
    assert out['mark'] is True
    assert out['note'] == 'Observed in the record'
    assert 'Continue outcome review' in out['ours']
    assert out['other'] is None


def test_score_save_is_single_flight_and_retry_keeps_marks():
    out = _harness(["h", "appendChildren", "renderSelfScoreCard"], """
    let reject, requests=[];
    api = (path,opts) => { requests.push(JSON.parse(JSON.stringify(opts.body))); return new Promise((_,r)=>{reject=r;}); };
    function isAgreementGate() { return false; }
    const card=renderSelfScoreCard({task_id:'t1'},{},[{expectation:'improves'}],[]);
    const pills=card.querySelectorAll('.asc-conf-pill'); const save=card.querySelector('.asc-btn');
    pills[0].dispatch('click'); save.dispatch('click'); pills[1].dispatch('click'); save.dispatch('click');
    const disabled=save.disabled;
    (async () => {
      reject({status:503,message:'temporary'}); await new Promise(r=>setTimeout(r,0));
      console.log(JSON.stringify({requests,disabled,retryEnabled:!save.disabled,
        pending:trajectoryRecoveries().t1.score.marks}));
    })();
    """)
    assert out['disabled'] is True
    assert out['retryEnabled'] is True
    assert len(out['requests']) == 1
    assert out['requests'][0]['marks'][0]['state'] == 'held'
    assert out['pending'][0]['state'] == 'held'


def test_saved_score_does_not_navigate_over_a_newer_screen():
    out = _harness(["h", "appendChildren", "renderSelfScoreCard"], """
    let finish, continued=false;
    api = () => new Promise(r=>{finish=r;});
    function continueTrajectory() { continued=true; }
    const card=renderSelfScoreCard({task_id:'t1'},{},[{expectation:'improves'}],[]);
    card.querySelectorAll('.asc-conf-pill')[0].dispatch('click'); card.querySelector('.asc-btn').dispatch('click');
    (async () => { setRoot(); state.view='home'; finish({}); await new Promise(r=>setTimeout(r,0));
      console.log(JSON.stringify({continued,pending:Object.keys(trajectoryRecoveries())})); })();
    """)
    assert out == {'continued': False, 'pending': []}


_SUBMIT_STUBS = """
state.task={task_id:'t1',trajectory_id:'walk'};
state.draft={task_id:'t1',verdict:'both_inadequate',confidence_set:true,storage_key:'original-key'};
function tutorialActive(){return false;} function examActive(){return false;}
function groundingSatisfied(){return {ok:true};} function stepsReview(){return {ok:true};}
function rubricGate(){return {ok:true};} function failureTagGate(){return {ok:true};}
function updateSubmitState(){} function updateHeaderProgress(){}
function buildSubmissionPayload(){return {task_id:'t1'};}
const cleared=[], opened=[];
function clearDraft(id,key){cleared.push([id,key]);if(state.draft&&state.draft.task_id===id)state.draft=null;}
function renderTrajectoryOutcomeView(task){opened.push(task.task_id);}
function continueFlaggedTrajectory(task){opened.push(task.task_id);}
"""


def test_submission_without_prediction_opens_outcome_and_saves_recovery():
    out = _harness(["submitEvaluation", "workspaceRequestIsCurrent"], _SUBMIT_STUBS + """
    api=async()=>({status:'needs_qa',record_count:1});
    (async()=>{await submitEvaluation();console.log(JSON.stringify({opened,cleared,pending:Object.keys(trajectoryRecoveries())}));})();
    """)
    assert out == {'opened': ['t1'], 'cleared': [['t1', 'original-key']], 'pending': ['t1']}


def test_late_submission_cannot_reveal_a_different_task():
    out = _harness(["submitEvaluation", "workspaceRequestIsCurrent"], _SUBMIT_STUBS + """
    let finish; api=()=>new Promise(r=>{finish=r;});
    (async()=>{const pending=submitEvaluation();state.task={task_id:'new',trajectory_id:'other'};
      state.draft={task_id:'new',text:'Keep this'};finish({status:'needs_qa'});await pending;
      console.log(JSON.stringify({opened,draft:state.draft,pending:Object.keys(trajectoryRecoveries())}));})();
    """)
    assert out == {'opened': [], 'draft': {'task_id': 'new', 'text': 'Keep this'}, 'pending': ['t1']}


def test_recovery_is_saved_before_pipeline_poll_finishes():
    out = _harness(["submitEvaluation", "workspaceRequestIsCurrent"], _SUBMIT_STUBS + """
    let finish; api=async()=>({accepted:true,submission_id:'s1'});
    function pollSubmissionStatus(){return new Promise(r=>{finish=r;});}
    (async()=>{const pending=submitEvaluation();await new Promise(r=>setTimeout(r,0));
      const before=Object.keys(trajectoryRecoveries());finish({done:true,status:'needs_qa'});await pending;
      console.log(JSON.stringify({before,opened}));})();
    """)
    assert out == {'before': ['t1'], 'opened': ['t1']}



def test_assignment_boundary_waits_instead_of_opening_an_unassigned_point():
    out = _harness(["h", "appendChildren", "continueTrajectory"], """
    let rendered,draws=0;
    setRoot=n=>{rendered=n;}; renderEvalView=()=>{draws++;};
    function renderDashboardView() {}
    continueTrajectory({task_id:'t1',progress:{next_task_id:null,complete:false,waiting_for_assignment:true}});
    console.log(JSON.stringify({text:rendered.textContent,draws,opened:globalThis.__opened||null}));
    """)
    assert 'finished your assigned points' in out['text']
    assert out['draws'] == 0
    assert out['opened'] is None


@pytest.mark.parametrize('name', ['flagPrompt', 'flagCaseIncoherent'])
def test_flag_response_preserves_the_walk_and_ignores_newer_navigation(name):
    out = _harness([name, "workspaceRequestIsCurrent"], _SUBMIT_STUBS + """
    let finish; api=()=>new Promise(r=>{finish=r;});
    state.draft.prompt_review={};
    (async()=>{const pending=CALL();state.view='home';finish({});await pending;
      console.log(JSON.stringify({opened,pending:Object.keys(trajectoryRecoveries()),cleared}));})();
    """.replace('CALL', name))
    assert out == {'opened': [], 'pending': ['t1'], 'cleared': [['t1', 'original-key']]}



def test_duplicate_submission_resumes_original_without_discarding_attempted_draft():
    out = _harness(["submitEvaluation", "workspaceRequestIsCurrent"], _SUBMIT_STUBS + """
    api=async()=>{throw {status:409,detail:{error:'trajectory_already_submitted',submission_id:'original-sid'}};};
    state.draft.note='Preserve the attempted work';
    (async()=>{await submitEvaluation();console.log(JSON.stringify({opened,cleared,note:state.draft.note,
      pending:Object.keys(trajectoryRecoveries())}));})();
    """)
    assert out == {'opened': ['t1'], 'cleared': [], 'note': 'Preserve the attempted work', 'pending': ['t1']}



def test_old_score_success_retains_newer_marks_from_reopened_outcome():
    out = _harness(["h", "appendChildren", "renderSelfScoreCard"], """
    let finish;api=()=>new Promise(resolve=>{finish=resolve;});
    const task={task_id:'t1'},expected=[{expectation:'improves'}];
    const first=renderSelfScoreCard(task,{},expected,[]);
    first.querySelectorAll('.asc-conf-pill')[0].dispatch('click');
    first.querySelector('.asc-btn').dispatch('click');
    setRoot();
    const reopened=renderSelfScoreCard(task,{},expected,[]);
    reopened.querySelectorAll('.asc-conf-pill')[1].dispatch('click');
    const note=reopened.querySelector('.asc-input');note.value='Newer clinical observation';note.dispatch('input');
    (async()=>{finish({});await new Promise(r=>setTimeout(r,0));
      console.log(JSON.stringify({score:trajectoryRecoveries().t1.score,
        selected:reopened.querySelectorAll('.asc-conf-pill')[1].classList.contains('active')}));})();
    """)
    assert out['selected'] is True
    assert out['score']['marks'][0] == {'index': 0, 'state': 'did_not_hold', 'note': 'Newer clinical observation'}


def test_score_success_does_not_clear_newer_other_tab_marks_during_continuation():
    out = _harness(["h", "appendChildren", "renderSelfScoreCard", "continueTrajectory"], """
    let finish;api=()=>new Promise(resolve=>{finish=resolve;});
    const task={task_id:'t1'},expected=[{expectation:'improves'}];
    const card=renderSelfScoreCard(task,{task_id:'t1',progress:{next_task_id:'next'}},expected,[]);
    card.querySelectorAll('.asc-conf-pill')[0].dispatch('click');card.querySelector('.asc-btn').dispatch('click');
    rememberTrajectoryOutcome(task,{marks:[{index:0,state:'not_assessable',note:'Other tab edit'}],falsifier_fired:false});
    (async()=>{finish({});await new Promise(r=>setTimeout(r,0));
      console.log(JSON.stringify({mark:trajectoryRecoveries().t1.score.marks[0],opened:globalThis.__opened}));})();
    """)
    assert out['mark'] == {'index': 0, 'state': 'not_assessable', 'note': 'Other tab edit'}
    assert out['opened'] == 'next'


@pytest.mark.parametrize('action', ['submitEvaluation', 'flagPrompt', 'flagCaseIncoherent'])
def test_pending_submission_keeps_in_place_edits_with_real_cleanup(action):
    stubs = _SUBMIT_STUBS.replace("function clearDraft(id,key){cleared.push([id,key]);if(state.draft&&state.draft.task_id===id)state.draft=null;}", "")
    out = _harness([action, "workspaceRequestIsCurrent", "clearDraft"], stubs + """
    function draftKey(id){return 'draft:'+id;}
    let finish;api=()=>new Promise(resolve=>{finish=resolve;});
    state.draft.prompt_review={};state.draft.clinical_note='original';state.draft.savedAt=100;
    localStorage.setItem('original-key',JSON.stringify(state.draft));
    (async()=>{const pending=ACTION();
      state.draft.clinical_note='Typed while submitting';state.draft.savedAt=200;
      localStorage.setItem('original-key',JSON.stringify(state.draft));
      finish({status:'needs_qa'});await pending;
      console.log(JSON.stringify({memory:state.draft.clinical_note,
        stored:JSON.parse(localStorage.getItem('original-key')).clinical_note}));})();
    """.replace('ACTION', action))
    assert out == {'memory': 'Typed while submitting', 'stored': 'Typed while submitting'}


@pytest.mark.parametrize("action", ["renderTrajectoryOutcomeView", "continueFlaggedTrajectory"])
def test_hung_navigation_read_times_out_without_resubmitting(action):
    out = _harness(["h", "appendChildren", action], """
    let rendered, calls=[], signal, delay;
    setRoot=n=>{rendered=n;state.screenGeneration=(state.screenGeneration||0)+1;};
    function renderDashboardView() {}
    api=(path,options)=>{calls.push(path);signal=options.signal;return new Promise(()=>{});};
    const realTimeout=globalThis.setTimeout;
    globalThis.setTimeout=(fn,ms)=>{delay=ms;return realTimeout(fn,1);};
    (async()=>{await ACTION({task_id:'t1',trajectory_id:'walk'});
      console.log(JSON.stringify({text:rendered.textContent,delay,aborted:signal.aborted,calls,
        pending:Object.keys(trajectoryRecoveries())}));})();
    """.replace('ACTION', action))
    assert out['delay'] == 15000
    assert out['aborted'] is True
    assert 'Try again' in out['text'] and 'taking longer than expected' in out['text']
    assert out['pending'] == ['t1']
    assert len(out['calls']) == 1 and '/submissions' not in out['calls']


def test_outcome_rendering_failure_is_retryable_with_saved_recovery():
    out = _harness(["h", "appendChildren", "renderTrajectoryOutcomeView"], """
    let rendered;
    setRoot=n=>{rendered=n;state.screenGeneration=(state.screenGeneration||0)+1;};
    function renderDashboardView() {}
    function paintTrajectoryOutcome(){throw new Error('render failed');}
    api=async()=>({progress:{next_task_id:'next'}});
    (async()=>{await renderTrajectoryOutcomeView({task_id:'t1',trajectory_id:'walk'});
      console.log(JSON.stringify({text:rendered.textContent,pending:Object.keys(trajectoryRecoveries())}));})();
    """)
    assert 'render failed' in out['text'] and 'Try again' in out['text']
    assert out['pending'] == ['t1']


def test_late_flag_continuation_cannot_override_navigation():
    out = _harness(["h", "appendChildren", "continueFlaggedTrajectory"], """
    let finish,continued=false;
    api=()=>new Promise(resolve=>{finish=resolve;});
    function continueTrajectory(){continued=true;}
    (async()=>{const pending=continueFlaggedTrajectory({task_id:'t1',trajectory_id:'walk'});
      setRoot();state.view='home';finish({progress:{next_task_id:'next'}});await pending;
      console.log(JSON.stringify({continued,recovery:trajectoryRecoveries().t1.continuation}));})();
    """)
    assert out == {'continued': False, 'recovery': True}


@pytest.mark.parametrize('action', ['flagPrompt', 'flagCaseIncoherent'])
@pytest.mark.parametrize('duplicate', [False, True])
def test_flags_advance_directly_but_duplicates_recover_the_original_outcome(action, duplicate):
    out = _harness([action, "workspaceRequestIsCurrent"], _SUBMIT_STUBS + """
    const continued=[];
    function continueFlaggedTrajectory(task){continued.push(task.task_id);}
    state.draft.prompt_review={};
    api=async()=>{if(DUPLICATE)throw {status:409,detail:{error:'trajectory_already_submitted',submission_id:'original'}};return {};};
    (async()=>{await ACTION();console.log(JSON.stringify({opened,continued,cleared,
      continuation:trajectoryRecoveries().t1.continuation}));})();
    """.replace('ACTION', action).replace('DUPLICATE', json.dumps(duplicate)))
    assert out['opened'] == (['t1'] if duplicate else [])
    assert out['continued'] == ([] if duplicate else ['t1'])
    assert out['continuation'] is (not duplicate)
    assert out['cleared'] == ([] if duplicate else [['t1', 'original-key']])
