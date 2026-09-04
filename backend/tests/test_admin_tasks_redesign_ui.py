"""The two new admin pages — asserted by RUNNING them, not by grepping them.

This repo has paid twice for source-only frontend tests: a section can be
complete, correct and invisible because nothing mounted it, and the failure is
quiet. So the load-bearing tests here execute ``renderAdminTasks`` and
``renderAdminBatches`` against the DOM shim with a stubbed ``api`` and assert
what lands in the document.

Three properties are worth more than the rest.

  1. **The one-way door is stated before it is walked through.** Brokering is
     irreversible — the server refuses brokering → task_creation with a 409, so
     the data can never become tasks. A UI that renders that as a plain toggle is
     lying about what the click costs.

  2. **The right panel never shows a control the selection cannot use.** That is
     the entire re-cut: relay offered for a whole walk and nothing else, flat
     targeting for standalone cases, and a hint when nothing is selected.

  3. **Removing a card did not remove a capability.** Every endpoint behind a
     deleted card is still live, and "Grade real" — which lived only in the old
     Tasks table — moved to the preview rather than being dropped.
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

_FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
_SHIM = pathlib.Path(__file__).resolve().parent / "_asclepius_dom.js"
# PRD-F moved the console out of the physician bundle. These pages are the same
# code they were, in a new file; every assertion below is unchanged.
JS = (_FRONTEND / "admin_shell.js").read_text()
PORTAL_JS = (_FRONTEND / "asclepius.js").read_text()
CSS = "\n".join((_FRONTEND / f).read_text()
                for f in ("asclepius.css", "admin.css", "_base.css", "_tokens.css"))


def _fn(src: str, name: str) -> str:
    """The body of one function, by brace matching."""
    start = src.index(f"function {name}(")
    depth, i = 0, src.index("{", start)
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError(f"unterminated {name}")


CREATION = _fn(JS, "renderAdminTasks")
ROUTING = _fn(JS, "renderAdminBatches")

_HARNESS = """
function h(tag, attrs) {
  var el = document.createElement(tag);
  if (attrs) for (var k in attrs) {
    var v = attrs[k];
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k === 'disabled') { if (v) el.setAttribute('disabled', ''); }
    else if (k === 'hidden') { if (v) el.setAttribute('hidden', ''); }
    else if (k === 'checked') { el.checked = !!v; }
    else if (k.slice(0, 2) === 'on' && typeof v === 'function') {
      el.addEventListener(k.slice(2).toLowerCase(), v);
    } else if (k === 'value') { el.value = v; }
    else el.setAttribute(k, v);
  }
  for (var i = 2; i < arguments.length; i++) app_(el, arguments[i]);
  return el;
}
function app_(el, c) {
  if (c == null || c === false) return;
  if (Array.isArray(c)) { c.forEach(function (x) { app_(el, x); }); return; }
  el.appendChild((c && c.tagName) || (c && c.nodeValue != null)
    ? c : document.createTextNode(String(c)));
}
function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }
function toast(m, k) { CALLS.push('toast:' + (k || 'info')); }
function loadingCard(t) { return h('div', { class: 'asc-card' }, t); }
function fmtDate(d) { return String(d); }
function selectFrom(o, s) { var e = document.createElement('select'); e.value = s; return e; }
function renderAdminView() { CALLS.push('renderAdminView'); }
function renderCasePanelReadOnly() { return h('div', {}); }
function baselineCell(t) { return h('div', { class: 'asc-baselines' }, 'Grade real'); }
function openSampleReviewModal() { CALLS.push('openSampleReviewModal'); }
function openCasePlanModal(u, ic, plan, box, opts) {
  CALLS.push('openCasePlanModal:trajectory=' + !!(opts && opts.trajectory));
}
function textOf(el) {
  if (el.nodeValue != null) return el.nodeValue;
  return (el.childNodes || []).map(textOf).join(' ');
}
function findAll(el, cls, out) {
  out = out || [];
  if (el.className && (' ' + el.className + ' ').indexOf(' ' + cls + ' ') !== -1) out.push(el);
  (el.childNodes || []).forEach(function (c) { if (c.tagName) findAll(c, cls, out); });
  return out;
}
function inputsOf(el, out) {
  out = out || [];
  if (el.tagName === 'INPUT') out.push(el);
  (el.childNodes || []).forEach(function (c) { if (c.tagName) inputsOf(c, out); });
  return out;
}
function checkboxes(el) {
  return inputsOf(el).filter(function (i) { return i.getAttribute('type') === 'checkbox'; });
}
function tidy(el) { return textOf(el).replace(/\\s+/g, ' ').trim(); }
"""

# The REAL ``toUtcDate``, spliced from the shipped file rather than stubbed:
# ``isFresh`` depends on it reading a bare server timestamp as UTC, and a stub
# built on Date.parse would hide the exact defect that dependency exists to fix.
_HARNESS += "\n" + _fn(JS, "toUtcDate") + "\n"

# The REAL ``copyableId`` and its clipboard helpers (Export & Approval PRD §1.3).
# Task Routing renders every case id through it. Spliced rather than stubbed for
# the same reason as ``toUtcDate``: the property under test in this file is what
# a routing row CONTAINS, and a stub that returned a plain span would let a
# regression in the id cell pass unnoticed here.
for _name in ("copyableId", "copyTextToClipboard", "legacyCopy"):
    _HARNESS += "\n" + _fn(JS, _name) + "\n"


def _run(script: str, tz: str | None = None) -> dict:
    """Execute a render against the DOM shim.

    ``tz`` is applied to the CHILD PROCESS ENVIRONMENT, not with
    ``process.env.TZ`` inside the script: V8 caches the zone at startup, so an
    in-script assignment is ignored and any test relying on it passes against
    the very bug it was written for. (It did. That is why this parameter
    exists.)"""
    import os

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed in this environment")
    full = f"require({json.dumps(str(_SHIM))});\nvar CALLS = [];\n{_HARNESS}\n{script}"
    env = dict(os.environ)
    if tz:
        env["TZ"] = tz
    proc = subprocess.run([node, "-e", full], capture_output=True, text=True,
                          timeout=60, env=env)
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\n{proc.stderr}\n{proc.stdout}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ═══════════════════════════════════════════════════════════════════════════════
# Page 1 — Data & Task Creation
# ═══════════════════════════════════════════════════════════════════════════════
_UPLOADS = """
var NOW = new Date().toISOString();
var UPLOADS = { uploads: [
  { upload_id:'u-undecided', partner_label:"St Mary's Health", filename:'b.zip',
    size_bytes: 412*1048576, created_at:'2026-08-28T00:00:00', verified_at:'x',
    staging:'undecided', purpose:null, specialties:['nephrology'],
    description:'2019-2023 CKD cohort',
    case_counts:{total:27,ingested:25,promoted:0,needs_review:2,quarantined:0},
    tasks_created:0, task_creation_complete:false },
  { upload_id:'u-making', partner_label:'Alpha', filename:'a.zip', size_bytes:1048576,
    created_at:'2026-08-29T00:00:00', staging:'task_creation', purpose:'task_creation',
    specialties:['nephrology'], description:null, task_mode:'longitudinal',
    case_counts:{total:10,ingested:8,promoted:0,needs_review:0,quarantined:0},
    tasks_created:0, task_creation_complete:false },
  { upload_id:'u-done', partner_label:'Beta', filename:'d.zip', size_bytes:1048576,
    created_at:'2026-08-30T00:00:00', staging:'task_creation', purpose:'task_creation',
    specialties:['cardiology'], task_mode:'static',
    case_counts:{total:5,ingested:0,promoted:5,needs_review:0,quarantined:0},
    tasks_created:5, task_creation_complete:true },
  { upload_id:'u-broker', partner_label:'Gamma', filename:'g.zip', size_bytes:1,
    created_at:'2026-08-30T00:00:00', staging:'brokering', purpose:'brokering',
    specialties:[], case_counts:{total:1,ingested:1,promoted:0},
    task_creation_complete:false },
]};
function api(path, opts) {
  CALLS.push((opts && opts.method || 'GET') + ' ' + path);
  if (path.indexOf('/ingestion/uploads/') === 0 || path.indexOf('/ingestion/uploads/') > 0) {
    if (path.indexOf('?') === -1 && path.split('/').length > 3) {
      return Promise.resolve({ cases: [
        { ingest_case_id:'ic1', patient_key:'p1', specialty:'nephrology',
          status:'ingested', review: [] }] });
    }
  }
  return Promise.resolve(UPLOADS);
}
var state = { dataCreation: null, adminSub: { work: 'tasks' } };
"""


def _creation(after: str) -> dict:
    return _run(_UPLOADS + CREATION + f"""
var body = document.createElement('div');
renderAdminTasks(body);
setTimeout(function () {{ {after} }}, 30);
""")


def test_page_one_renders_both_boxes_and_the_done_fold():
    out = _creation("""
      console.log(JSON.stringify({ text: tidy(body), calls: CALLS }));
    """)
    t = out["text"]
    assert "Incoming data" in t and "Task creation" in t
    assert "St Mary's Health" in t, "the undecided upload must be in Box 1"
    assert "Alpha" in t, "the task_creation upload must be in Box 2"
    assert "Done (1)" in t, "a finished bundle folds rather than vanishing"


def test_a_brokering_upload_appears_in_neither_box():
    """It left the page when the decision was made. Rendering it under a
    Task-creation heading would offer a promotion that every endpoint refuses."""
    out = _creation("console.log(JSON.stringify({ text: tidy(body) }));")
    assert "Gamma" not in out["text"]


def test_box_one_says_the_brokering_choice_cannot_be_undone():
    """The server refuses brokering → task_creation with a 409, permanently. A
    screen that presents the pair as symmetrical buttons is lying about the
    cost of a mis-click."""
    assert "cannot be undone" in CREATION
    assert "askBrokering" in CREATION
    # The confirm is wired to the destructive button only.
    idx = CREATION.index("askBrokering(u)")
    window = CREATION[idx - 300:idx]
    assert "Brokering" in window


def test_choosing_task_creation_does_not_ask_for_confirmation():
    """It is reversible — task_creation → brokering removes a promotion path and
    never adds one — so a dialog here would be ceremony that teaches an operator
    to click through the one that matters."""
    idx = CREATION.index("'Task creation');")
    window = CREATION[idx:CREATION.index("const toBroker", idx)]
    assert "resolvePurpose(u, 'task_creation')" in window, (
        "the reversible choice should call through directly")
    assert "askBrokering" not in window, (
        "a confirm on the reversible half teaches an operator to click through "
        "the one that matters")


def test_the_longitudinal_commit_carries_the_trajectory_flag():
    """The plan an admin approves was computed with trajectory:true. A commit
    that dropped the flag would generate independent cases from the same chart —
    the right count, silently the wrong product, with no sequence gate."""
    out = _creation("""
      var btns = [];
      (function walk(el) {
        if (el.tagName === 'BUTTON') btns.push(el);
        (el.childNodes || []).forEach(function (c) { if (c.tagName) walk(c); });
      })(body);
      var build = btns.filter(function (b) { return tidy(b).indexOf('Build the chart walk') !== -1; });
      if (build.length) build[0].dispatch('click');
      setTimeout(function () { console.log(JSON.stringify({ calls: CALLS })); }, 30);
    """)
    assert "openCasePlanModal:trajectory=true" in out["calls"], out["calls"]


def test_the_mode_radio_is_locked_once_tasks_exist_in_that_mode():
    """Mirrors the server's 409. A control that posts a request the backend
    always refuses reads as the product being broken."""
    assert "disabled: !!counts.promoted && mode !== m" in CREATION


def test_the_upload_modal_requires_an_answer_to_what_is_this():
    """The four answers go to four different places, and guessing wrong is
    expensive in both directions."""
    assert "What is this?" in CREATION
    assert "go = h('button', { class: 'asc-btn asc-btn-primary', disabled: true }" in CREATION
    assert "if (mode) go.removeAttribute('disabled'); else go.setAttribute('disabled', '');" in CREATION


def test_real_records_go_through_the_partner_door_not_a_second_ingest_endpoint():
    """That door fails closed on unconfigured encryption and on non-durable
    storage. A second admin-only door would have to reproduce both exactly or
    quietly become the unsafe way in."""
    assert "'/admin/upload-links'" in CREATION
    assert "'/partner/uploads?t='" in CREATION


def test_gold_is_a_button_not_a_file_drop():
    """``load-gold`` loads committed fixtures and takes no file, so a file input
    for it would be a control with nothing to do."""
    assert "'/load-gold'" in CREATION
    assert "needsFile = mode === 'real_static' || mode === 'real_longitudinal' || mode === 'task_file'" in CREATION


# ═══════════════════════════════════════════════════════════════════════════════
# Page 2 — Task Routing
# ═══════════════════════════════════════════════════════════════════════════════
_BATCHES = """
var NOW = new Date().toISOString();
var ROUTES = {
  '/admin/batches': { longitudinal:{n_trajectories:1,n_points:3,n_unrouted:3},
                      real_static:{n_cases:18,n_open:6}, synthetic:{n_cases:34,n_open:34} },
  '/admin/batches/longitudinal': { cases: [
     {task_id:'t0',trajectory_id:'tr1',sequence_index:0,specialty:'nephrology',difficulty:'hard',
      distribution:'assigned_only',label_count:0,max_labels:1,created_at:NOW,
      display_bucket:'longitudinal_real'},
     {task_id:'t1',trajectory_id:'tr1',sequence_index:1,specialty:'nephrology',difficulty:'hard',
      distribution:'assigned_only',label_count:0,max_labels:1,created_at:NOW,
      display_bucket:'longitudinal_real'},
     {task_id:'t2',trajectory_id:'tr1',sequence_index:2,specialty:'nephrology',difficulty:'hard',
      distribution:'assigned_only',label_count:0,max_labels:1,created_at:NOW,
      display_bucket:'longitudinal_real'}]},
  '/admin/batches/synthetic': { cases: [
     {task_id:'s1',specialty:'nephrology',difficulty:'hard',distribution:'open',label_count:0,
      max_labels:2,created_at:'2020-01-01T00:00:00',display_bucket:'physician_authored'},
     {task_id:'s2',specialty:'cardiology',difficulty:'medium',distribution:'open',label_count:1,
      max_labels:2,created_at:NOW,display_bucket:'synthetic'}]},
  '/admin/physicians': { physicians: [
     {id:'d1',name:'Dr Faheem',email:'f@x.com',specialty:'nephrology',tier:'labeler',
      real_data_approved:true},
     {id:'d2',name:'Dr Vadgama',email:'v@x.com',specialty:'nephrology',tier:'reviewer',
      real_data_approved:true}]},
};
var SENT = [];
function api(path, opts) {
  CALLS.push((opts && opts.method || 'GET') + ' ' + path);
  if (path.indexOf('resolve-selection') !== -1) {
    return Promise.resolve({ task_ids: (opts.body || {}).task_ids, n_added: 0 });
  }
  if (path.indexOf('allocate') !== -1) { SENT.push(opts.body); return Promise.resolve({ dry_run: true, cases: 1, per_physician: {} }); }
  return Promise.resolve(ROUTES[path] || {});
}
var state = { batches: null };
"""
_META = JS[JS.index("  const BATCH_META = {"):JS.index("};", JS.index("  const BATCH_META = {")) + 2]


def _routing(after: str) -> dict:
    return _run(_BATCHES + _META + ROUTING + f"""
var body = document.createElement('div');
renderAdminBatches(body);
setTimeout(function () {{ {after} }}, 30);
""")


def test_all_three_columns_render_at_once():
    """The old shape made you navigate between the parts of one decision."""
    out = _routing("console.log(JSON.stringify({ text: tidy(body) }));")
    t = out["text"]
    assert "LONGITUDINAL V4" in t and "REAL · STATIC V4" in t and "SYNTHETIC V3" in t
    assert "Select cases to route them" in t


def test_the_panel_offers_relay_only_for_a_whole_walk():
    out = _routing("""
      findAll(body, 'asc-route-rail-btn')[0].dispatch('click');
      setTimeout(function () {
        checkboxes(body).forEach(function (cb) { cb.checked = true; cb.dispatch('change'); });
        setTimeout(function () { console.log(JSON.stringify({ text: tidy(body) })); }, 30);
      }, 30);
    """)
    assert "Send as relay" in out["text"]
    assert "Solo walk" in out["text"]


def test_a_standalone_selection_is_never_offered_relay():
    """A relay is defined over a walk. Half a chart split between five doctors is
    neither a solo walk nor a handoff chain, and the server refuses it anyway."""
    out = _routing("""
      findAll(body, 'asc-route-rail-btn')[2].dispatch('click');
      setTimeout(function () {
        var cb = checkboxes(body)[0]; cb.checked = true; cb.dispatch('change');
        setTimeout(function () { console.log(JSON.stringify({ text: tidy(body) })); }, 30);
      }, 30);
    """)
    t = out["text"]
    assert "Send as relay" not in t
    assert "All approved doctors" in t, "flat targeting is what a standalone case takes"


def test_a_gold_case_carries_its_chip_in_the_synthetic_rail():
    """batch_overview counts gold as synthetic (its case_source is not
    real_deid). The chip keeps it visible as physician-authored without a fourth
    rail item that would then disagree with the backend's three."""
    out = _routing("""
      findAll(body, 'asc-route-rail-btn')[2].dispatch('click');
      setTimeout(function () { console.log(JSON.stringify({ text: tidy(body) })); }, 30);
    """)
    assert "physician-authored" in out["text"]


def test_a_task_made_today_carries_a_new_chip():
    """So what you just built on the other page is findable without searching."""
    out = _routing("""
      findAll(body, 'asc-route-rail-btn')[2].dispatch('click');
      setTimeout(function () {
        var rows = findAll(body, 'asc-chip-new');
        console.log(JSON.stringify({ n: rows.length }));
      }, 30);
    """)
    assert out["n"] == 1, "exactly the recent task, not the 2020 one"


def test_the_new_chip_reads_the_timestamp_as_utc_not_local():
    """The server writes a bare 'YYYY-MM-DDTHH:MM:SS' with no zone, which
    Date.parse reads as LOCAL time. East of UTC that shifts an old task inside
    the 24-hour window and chips it "new" — a lie on the one screen an operator
    uses to find what they just made.

    The zone has to be WEST of UTC to expose it. Parsing a bare stamp as local
    in UTC+O places the instant at C-O, so the apparent age is (age + O): east
    makes a task look OLDER and hides the bug, west makes it look younger. At
    UTC-11 a 30-hour-old task reads as 19 hours and gets chipped.

    Guarded here rather than left to the implementation because the naive
    spelling is the one a later reader reaches for first."""
    import datetime as _dt

    old = (_dt.datetime.utcnow() - _dt.timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%S")
    out = _run(f"""
var ROUTES = {{
  '/admin/batches': {{ longitudinal: {{}}, real_static: {{}}, synthetic: {{n_cases:1}} }},
  '/admin/batches/synthetic': {{ cases: [
     {{task_id:'old', specialty:'x', difficulty:'hard', distribution:'open',
      label_count:0, max_labels:1, created_at:'{old}', display_bucket:'synthetic'}}]}},
}};
function api(path, opts) {{ return Promise.resolve(ROUTES[path] || {{}}); }}
var state = {{ batches: null }};
{_META}
{ROUTING}
var body = document.createElement('div');
renderAdminBatches(body);
setTimeout(function () {{
  findAll(body, 'asc-route-rail-btn')[2].dispatch('click');
  setTimeout(function () {{
    console.log(JSON.stringify({{ fresh: findAll(body, 'asc-chip-new').length }}));
  }}, 30);
}}, 30);
""", tz="Pacific/Midway")   # UTC-11, applied before node starts
    assert out["fresh"] == 0, "a 30-hour-old task was chipped 'new' in UTC-11"


def test_the_relay_doctor_picker_repaints_once_the_roster_arrives():
    """A bare loadDoctors() resolves into a screen already drawn, leaving "No
    approved doctors to name." permanently — and on the relay path that empty
    list IS the control."""
    assert "if (!view.doctors) loadDoctors().then(paint);" in ROUTING


def test_the_per_doctor_role_reaches_the_allocate_payload():
    """The gap §4.3 closes: the role existed in assignments and never in this
    screen, so choosing "Reviewer" silently assigned labeling."""
    out = _routing("""
      findAll(body, 'asc-route-rail-btn')[2].dispatch('click');
      setTimeout(function () {
        var cb = checkboxes(body)[0]; cb.checked = true; cb.dispatch('change');
        setTimeout(function () {
          var sel = findAll(body, 'asc-input')[0];
          // switch targeting to explicit
          var sels = [];
          (function walk(el) {
            if (el.tagName === 'SELECT') sels.push(el);
            (el.childNodes || []).forEach(function (c) { if (c.tagName) walk(c); });
          })(body);
          sels[0].value = 'explicit'; sels[0].dispatch('change');
          setTimeout(function () {
            var boxes = checkboxes(body);
            var doc = boxes[boxes.length - 1];
            doc.checked = true; doc.dispatch('change');
            setTimeout(function () {
              // The doctor we actually checked was the LAST row; its two radios
              // are name="role-<that id>". Picking any other doctor's radio
              // proves nothing — send() drops roles for unselected names, which
              // is itself the behaviour under test.
              var mine = inputsOf(body).filter(function (i) {
                return i.getAttribute('name') === 'role-d2';
              });
              var reviewer = mine[1];              // [labeler, reviewer]
              reviewer.checked = true; reviewer.dispatch('change');
              setTimeout(function () {
                var btns = [];
                (function walk(el) {
                  if (el.tagName === 'BUTTON') btns.push(el);
                  (el.childNodes || []).forEach(function (c) { if (c.tagName) walk(c); });
                })(body);
                btns.filter(function (b) { return tidy(b) === 'Preview send'; })[0].dispatch('click');
                setTimeout(function () { console.log(JSON.stringify({ sent: SENT })); }, 30);
              }, 30);
            }, 30);
          }, 30);
        }, 30);
      }, 30);
    """)
    assert out["sent"], "nothing was sent"
    payload = out["sent"][-1]
    assert payload.get("roles"), f"no roles in payload: {payload}"
    assert "review" in payload["roles"].values()


def test_a_solo_walk_sends_to_the_named_doctors_and_never_to_everyone():
    """The worst bug this branch produced, and the reason the panel sets its own
    targeting.

    The walk panel rendered a doctor picker but left ``view.mode`` at its default
    of 'all'. Naming a doctor for a solo walk and pressing Send therefore posted
    ``to_all``: no assignments written, the whole trajectory flipped to the open
    queue, and the un-sealing warning not even shown — it lives on the flat
    control. The operator asked for one doctor and silently got everybody, on the
    one case class the product deliberately seals."""
    out = _routing("""
      findAll(body, 'asc-route-rail-btn')[0].dispatch('click');
      setTimeout(function () {
        checkboxes(body).forEach(function (cb) { cb.checked = true; cb.dispatch('change'); });
        setTimeout(function () {
          // name a doctor in the walk panel
          var docs = findAll(body, 'asc-route-doc');
          var cb = checkboxes(docs[0])[0];
          cb.checked = true; cb.dispatch('change');
          // Longitudinal E2E §3 — Send is disabled until a human has opened
          // one of these cases this session. Auto-generation removed the click
          // that used to force somebody past a preview on the way to creating
          // tasks, so this is now the only point at which a person is guaranteed
          // to have read the case they are about to route. Clicking it here is
          // not test scaffolding: it IS the required flow.
          findAll(body, 'asc-btn')
            .filter(function (b) { return tidy(b) === 'Preview'; })[0].dispatch('click');
          setTimeout(function () {
            var btns = [];
            (function walk(el) {
              if (el.tagName === 'BUTTON') btns.push(el);
              (el.childNodes || []).forEach(function (c) { if (c.tagName) walk(c); });
            })(body);
            btns.filter(function (b) { return tidy(b) === 'Send'; })[0].dispatch('click');
            setTimeout(function () { console.log(JSON.stringify({ sent: SENT })); }, 30);
          }, 30);
        }, 30);
      }, 30);
    """)
    assert out["sent"], "nothing was sent"
    payload = out["sent"][-1]
    assert payload.get("user_ids"), f"solo walk did not target the named doctor: {payload}"
    assert not payload.get("to_all"), (
        "a solo walk posted to_all — this un-seals the trajectory to every "
        "eligible doctor and writes no assignments")


def test_an_explicit_send_with_nobody_named_is_refused_not_posted():
    """An empty ``user_ids`` reads to the allocator as "no targeting", so it
    picks doctors itself: the screen says "these people" and the server hears
    "anyone". Refused client-side, where the operator can still fix it."""
    out = _routing("""
      findAll(body, 'asc-route-rail-btn')[0].dispatch('click');
      setTimeout(function () {
        checkboxes(body).forEach(function (cb) { cb.checked = true; cb.dispatch('change'); });
          // Longitudinal E2E §3 — Send is disabled until a human has opened
          // one of these cases this session. Auto-generation removed the click
          // that used to force somebody past a preview on the way to creating
          // tasks, so this is now the only point at which a person is guaranteed
          // to have read the case they are about to route. Clicking it here is
          // not test scaffolding: it IS the required flow.
          findAll(body, 'asc-btn')
            .filter(function (b) { return tidy(b) === 'Preview'; })[0].dispatch('click');
        setTimeout(function () {
          var btns = [];
          (function walk(el) {
            if (el.tagName === 'BUTTON') btns.push(el);
            (el.childNodes || []).forEach(function (c) { if (c.tagName) walk(c); });
          })(body);
          btns.filter(function (b) { return tidy(b) === 'Send'; })[0].dispatch('click');
          setTimeout(function () {
            console.log(JSON.stringify({ sent: SENT, text: tidy(body) }));
          }, 30);
        }, 30);
      }, 30);
    """)
    assert not out["sent"], "posted an allocate with nobody named"
    assert "Pick at least one doctor" in out["text"]


def test_un_sealing_a_walk_is_its_own_named_mode_and_carries_the_warning():
    """Sending a walk to the open queue is a legitimate, deliberate act — it just
    must not be what happens when nobody chose it."""
    assert "'open', 'Open queue — any eligible doctor, in sequence'" in ROUTING
    assert "enter the open queue" in ROUTING


def test_role_radios_are_not_offered_for_a_doctor_nobody_selected():
    """Greyed radios beside an unchecked name are noise, and a pre-selected
    "Labeler" on somebody nobody chose reads as a decision never made."""
    assert "(withRoles && on) ?" in ROUTING


def test_the_relay_mode_offers_no_roles_because_the_endpoint_takes_none():
    """/batches/relay commits a rotation and has no roles field. Radios there
    would be a control that silently does nothing."""
    assert "doctorPicker({ roles: current === 'solo' })" in ROUTING


def test_the_reviewer_refusal_is_surfaced_by_name():
    """The server refuses a named non-reviewer at send. Rendering a bare 400
    would make the admin guess which of five names was wrong."""
    assert "not_a_reviewer" in ROUTING
    assert "reviewer tier" in ROUTING


def test_grade_real_survived_the_removal_of_the_tasks_table():
    """It lived only in the old Tasks table. Dropping the card was specified;
    dropping the capability was not, and it is the only way to see a HELD
    "needs baseline" task."""
    assert "baselineCell(t)" in ROUTING
    assert "function gradeRealModelsBtn(" in JS
    assert "function baselineCell(" in JS


# ═══════════════════════════════════════════════════════════════════════════════
# The removals
# ═══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("gone", [
    "Paste tasks (JSON)",
    "Generate candidates",
    "Seed corpus",
    "Generation jobs",
    "Load gold cases (RATIFIED, no LLM needed)",
    "Load REAL de-identified cases (V4)",
])
def test_the_removed_cards_are_gone_from_the_client(gone):
    """Checked against BOTH bundles: the console moved to its own file in
    PRD-F, and a card that came back on either surface is the same defect."""
    assert gone not in JS + PORTAL_JS, f"{gone!r} is still rendered"


@pytest.mark.parametrize("dead", ["loadTasksTable", "loadSeedCorpus", "loadGenerationJobs"])
def test_the_helpers_that_only_served_removed_cards_are_deleted(dead):
    """Dead render code outlives its card and is the thing a later reader
    restores by accident."""
    assert dead not in JS + PORTAL_JS


def test_frontier_model_failures_lives_on_metrics():
    """§2 moves it: it is a measurement, not a creation step."""
    assert "loadModelFailures" in JS
    metrics = _fn(JS, "renderAdminMetrics")
    assert "ascModelFailures" in metrics


def test_every_class_the_new_pages_emit_has_a_style():
    """A class with no rule is invisible in review and invisible on screen."""
    emitted = set(re.findall(r"class: '([^']+)'", CREATION + ROUTING))
    names = {c for blob in emitted for c in blob.split() if c.startswith("asc-")}
    missing = [c for c in sorted(names) if f".{c}" not in CSS]
    assert not missing, f"classes with no CSS rule: {missing}"


def test_no_raw_hex_colour_entered_the_stylesheet_outside_tokens():
    """§6.6 — the palette lives in _tokens.css and nowhere else."""
    sheet = (_FRONTEND / "asclepius.css").read_text()
    block = sheet[sheet.index("PRD ADMIN-TASKS — Data & Task Creation"):]
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", block)


def test_the_send_panel_survives_a_partial_view_built_elsewhere():
    """``state.batches`` is not always built by ``renderAdminBatches``.

    ``openBatchesFor(physician)`` — "route cases to this doctor", entered from
    their row in Physicians — constructs a PARTIAL object and then hands control
    here, which finds ``state.batches`` already truthy and keeps it as-is. Every
    key that caller omits is ``undefined`` at read time, and the two read as maps
    (``view.roles[id]``, ``view.previewed[id]``) throw a TypeError that takes the
    whole send panel down.

    Found by auditing rather than by a failure: no test entered Batches by that
    route, so both the pre-existing ``roles`` hole and the ``previewed`` one this
    branch added were invisible. The defaults are now backfilled onto whatever
    arrives, so a key added later covers both entry points by construction.
    """
    out = _run(_BATCHES + _META + ROUTING + """
var body = document.createElement('div');
// EXACTLY the object openBatchesFor builds — no roles, no previewed, no relay*.
state.batches = {
  overview: null, batch: null, rows: null, selected: {}, busy: false,
  err: null, mode: 'explicit', userIds: ['u-doc-1'], specialty: '',
  doctors: [{ id: 'u-doc-1', email: 'a@b.c' }], proposal: null,
};
renderAdminBatches(body);
setTimeout(function () {
  findAll(body, 'asc-route-rail-btn')[0].dispatch('click');
  setTimeout(function () {
    checkboxes(body).forEach(function (cb) { cb.checked = true; cb.dispatch('change'); });
    setTimeout(function () {
      console.log(JSON.stringify({ text: tidy(body), errs: CALLS.filter(function (c) {
        return String(c).indexOf('error') !== -1; }) }));
    }, 30);
  }, 30);
}, 30);
""")
    # It renders at all — a TypeError here produced an empty panel and a dead screen.
    assert "Send" in out["text"], out["text"][:200]
    # And the preview gate is present rather than skipped by the crash.
    assert "Open one of these cases first" in out["text"]


# ═══════════════════════════════════════════════════════════════════════════════
# Case Generation Fix PRD §B1 / §A5 — the row, and the specialty gate on Build
# ═══════════════════════════════════════════════════════════════════════════════
_CONTENT_UPLOADS = """
var UPLOADS = { uploads: [
  { upload_id:'u-gs', partner_label:'Gray Scrubs Hospitals', filename:'patient-4-abc.zip',
    size_bytes: 266240, created_at:'2026-08-09T23:45:00', verified_at:'x',
    staging:'undecided', purpose:null, specialties:[], description:null,
    content:{ charts:1, encounters:12, notes:79, lab_panels:45, studies:0,
              specialty_inferred:'hepatology', specialty_confidence:0.71,
              specialty_clears_floor:true, specialty_floor:0.6 },
    case_counts:{total:1,ingested:1,promoted:0,needs_review:0,quarantined:0},
    tasks_created:0, task_creation_complete:false },
  { upload_id:'u-low', partner_label:'St Mary', filename:'p3.zip', size_bytes: 3145728,
    created_at:'2026-08-10T00:00:00', staging:'task_creation', purpose:'task_creation',
    specialties:[], description:null, task_mode:'longitudinal',
    content:{ charts:1, encounters:5, notes:155, lab_panels:113, studies:0,
              specialty_inferred:'nephrology', specialty_confidence:0.38,
              specialty_clears_floor:false, specialty_floor:0.6 },
    case_counts:{total:1,ingested:1,promoted:0,needs_review:0,quarantined:0},
    tasks_created:0, task_creation_complete:false },
  { upload_id:'u-set', partner_label:'Alpha', filename:'a.zip', size_bytes:1048576,
    created_at:'2026-08-29T00:00:00', staging:'task_creation', purpose:'task_creation',
    specialties:['hepatology'], description:null, task_mode:'longitudinal',
    content:{ charts:1, encounters:22, notes:300, lab_panels:100, studies:0,
              specialty_inferred:'hepatology', specialty_confidence:0.9,
              specialty_clears_floor:true, specialty_floor:0.6 },
    case_counts:{total:1,ingested:1,promoted:0,needs_review:0,quarantined:0},
    tasks_created:0, task_creation_complete:false },
]};
function api(path, opts) {
  CALLS.push((opts && opts.method || 'GET') + ' ' + path);
  if (path === '/specialties') return Promise.resolve({ specialties: ['hepatology', 'nephrology'] });
  return Promise.resolve(UPLOADS);
}
var state = { dataCreation: null, adminSub: { work: 'tasks' } };
// The real picker loads /specialties and posts the choice; here the property
// under test is that the ROW offers it and gates Build on it, so a marker span
// carrying the picker's class stands in for the control.
function specialtyResolver(uploadId, onDone) {
  CALLS.push('specialtyResolver:' + uploadId);
  return h('span', { class: 'asc-spec-resolver' });
}
"""


def _content_page(after: str) -> dict:
    return _run(_CONTENT_UPLOADS + CREATION + f"""
var body = document.createElement('div');
renderAdminTasks(body);
setTimeout(function () {{ {after} }}, 30);
""")


def test_a_box_one_row_reads_one_chart_with_its_counts_and_the_inferred_specialty():
    """§B1: "Gray Scrubs Hospitals · patient-4-abc.zip · 260 KB · SHA ✓" then
    "Hepatology (inferred 0.71) · 1 chart · 12 encounters · 79 notes · 45
    panels". Not "3 case(s)", not "0 MB", not "Unknown sender"."""
    out = _content_page("console.log(JSON.stringify({ text: tidy(body) }));")
    t = out["text"]
    assert "Gray Scrubs Hospitals" in t and "patient-4-abc.zip" in t
    assert "260 KB" in t
    assert not re.search(r"(?<![\d.])0 MB", t), "a 260 KB bundle must not round to 0 MB"
    assert "Hepatology (inferred 0.71)" in t
    assert "1 chart · 12 encounters · 79 notes · 45 panels" in t
    assert "No description was sent with this bundle." in t
    assert "case(s)" not in t.split("Task creation")[0], "Box 1 counts charts, not cases"
    assert "cannot be undone" not in t, "irreversibility belongs to the confirm dialog, not the heading"


def test_build_is_disabled_until_the_specialty_is_set_when_inference_is_below_floor():
    """§A5: the picker is a required step before Build when the chart's own signal
    does not clear the floor. The row says so, and the button is dead until then."""
    out = _content_page("""
      var rows = findAll(body, 'asc-stage-row');
      var info = rows.map(function (r) {
        var btns = [];
        (function walk(el) {
          if (el.tagName === 'BUTTON') btns.push(el);
          (el.childNodes || []).forEach(function (c) { if (c.tagName) walk(c); });
        })(r);
        var build = btns.filter(function (b) { return tidy(b).indexOf('Build the chart walk') !== -1; })[0];
        return { text: tidy(r), buildDisabled: build ? build.getAttribute('disabled') !== null : null,
                 hasPicker: findAll(r, 'asc-spec-resolver').length > 0 };
      });
      console.log(JSON.stringify({ rows: info }));
    """)
    rows = {r["text"][:12]: r for r in out["rows"]}
    low = [r for r in out["rows"] if "St Mary" in r["text"]][0]
    ok = [r for r in out["rows"] if "Alpha" in r["text"]][0]
    assert low["buildDisabled"] is True, low
    assert low["hasPicker"] is True
    assert "Set the specialty before building." in low["text"]
    assert "nephrology at 0.38" in low["text"].lower() or "Nephrology at 0.38" in low["text"]
    assert ok["buildDisabled"] is False, ok
    assert ok["hasPicker"] is False
    assert "Hepatology" in ok["text"]
