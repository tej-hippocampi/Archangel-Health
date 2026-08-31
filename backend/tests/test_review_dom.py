"""PRD-1 — the reviewer surface actually renders, and renders the right thing.

Source-grepping a frontend module proves it was written, not that it works. This
repo has already paid for that lesson: a surface can be complete, correct and
INVISIBLE for a whole build round because nothing mounted it and the failure was
quiet. So these tests execute ``review.js`` against the DOM shim and assert what
lands in the document — including the failure case, which is part of the contract
rather than a nicety.

They drive it through the contract it actually ships with. Review is no longer a
standalone page: it is ``window.AsclepiusReview.render(el, ctx)``, mounted inside
the evaluation portal, and the ctx it receives here is built from the REAL ``h``
and ``clear`` extracted out of ``asclepius.js``. That is the point of the change
and therefore the point of the harness — if the shell's hyperscript and the
review module ever stop agreeing, these tests are where it shows.

The properties under test are the ones the PRD says are load-bearing:

  * the case renders as the LABELER'S CHART, from the same module, with lab
    trends as trends — no JSON.stringify anywhere on the surface;
  * reasoning-step divergence between A and B is marked automatically;
  * the judgment controls are reachable without scrolling past the case;
  * the keyboard path completes a clean accept;
  * both cards are GREEN and neither is orange — the accent carries meaning;
  * ``.asc-answers`` contains EXACTLY the two cards — a third child lands in
    cell 2 and pushes B to row 2, which has already shipped as a bug once;
  * the countdown's value comes from the API response, never from the client;
  * a preview draw is visibly a preview and records nothing;
  * a failed draw renders a VISIBLE error, never a silent placeholder.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _asclepius_harness import _extract_function  # noqa: E402

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
_DOM_SHIM = Path(__file__).resolve().parent / "_asclepius_dom.js"
_REVIEW_JS = _FRONTEND / "review.js"
_CASE_PANEL_JS = _FRONTEND / "case_panel.js"
_PORTAL_JS = _FRONTEND / "asclepius.js"
_INDEX_HTML = _FRONTEND / "index.html"
_CSS = _FRONTEND / "asclepius.css"
# The heading that opens the PRD-R block. Named once: three tests split the
# stylesheet on it, and it has already been edited out from under them by an
# unrelated house-style rule.
_PRD_R_CSS_HEADING = "PRD-R: the paired review surface"
# ...and the heading of the block PRD-1 added, which sits BEFORE it so the
# splits above keep meaning what they meant.
_PRD_1_CSS_HEADING = "PRD-1: review inside the shell"


def _run_node(script: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed in this environment")
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\n{proc.stderr}\n{proc.stdout}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _code(source: str) -> str:
    """The module minus its comments.

    These files talk ABOUT the things they no longer do — "it used to be
    `window.open(...)`", "`JSON.stringify` is why review was slow" — because the
    reason is the durable part. A grep that cannot tell prose from code is a test
    that gets deleted the first time somebody documents a rule.
    """
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


def _shell_hyperscript() -> str:
    """The SHELL's ``h`` / ``appendChildren`` / ``clear``, verbatim.

    Not a stand-in. PRD-1 §1 named "its own ``h()``" as one of the reasons review
    did not feel like the same product, and the fix was to hand the module the
    shell's. Extracting the real thing is what makes that assertion behavioural:
    a divergence in event-name casing or array handling fails here rather than in
    a physician's browser.
    """
    src = _PORTAL_JS.read_text(encoding="utf-8")
    return "\n".join(_extract_function(src, name) for name in ("h", "appendChildren", "clear"))


# The browser globals the review module touches that the shared shim does not
# provide. Installed here rather than in the shim so the shim stays exactly what
# the other DOM suites already depend on.
_HARNESS = """
require(%(shim)s);
const timers = [];
globalThis.setInterval = (fn) => { timers.push(fn); return timers.length; };
globalThis.clearInterval = () => {};
globalThis.__timers = timers;
globalThis.__tick = function () { timers.slice().forEach((fn) => fn()); };

// Agent P's heartbeat client, when the page has one. The review surface talks to
// it through exactly three calls — start(payload, progressKey), stop(reason),
// state() — and reads server-attested seconds from state(), never a local clock.
// `__sessionCalls` records every one, so a test can assert what the module asked
// for AND what it passed. The method set is data-driven so a build of P's client
// that predates `stop` can be simulated.
globalThis.__sessionCalls = [];
const SESSION_STATE = %(session_state)s;
const SESSION_METHODS = %(session_methods)s;
if (SESSION_STATE !== null) {
  const client = {};
  if (SESSION_METHODS.indexOf('start') !== -1) {
    client.start = function (s, key) {
      globalThis.__sessionCalls.push(['start', s && s.session_id, key === undefined ? null : key]);
    };
  }
  if (SESSION_METHODS.indexOf('stop') !== -1) {
    client.stop = function (reason) {
      globalThis.__sessionCalls.push(['stop', reason === undefined ? null : reason]);
    };
  }
  if (SESSION_METHODS.indexOf('state') !== -1) {
    client.state = function () { return SESSION_STATE; };
  }
  globalThis.window.AsclepiusSession = client;
}

// ─── the SHELL's hyperscript, extracted from asclepius.js ────────────────────
%(hyperscript)s

// ─── the shell's fetch helper, in the shape review.js consumes ──────────────
const ROUTES = %(routes)s;
const calls = [];
// Draws that have not answered yet. A route marked `defer` parks its resolver
// here instead of resolving, so a test can navigate away, re-render, or click
// again while a request is genuinely in flight — which is the only way to
// exercise the generation guard, and the only way the bug it prevents happens.
globalThis.__pending = [];
globalThis.__seq = {};
function api(path, opts) {
  opts = opts || {};
  calls.push({ url: '/api/asclepius' + path, method: opts.method || 'GET',
               body: opts.body || null });
  // The URL keeps its query string (a test asserts `?preview=true` was asked
  // for); the ROUTES table is keyed on the path alone.
  const hit = ROUTES[path.split('?')[0]];
  if (!hit) return Promise.resolve({});
  if (hit.defer) {
    // `bodies` lets successive calls to one route answer differently, so a test
    // can tell WHICH draw painted.
    const seq = (hit.bodies || [])[globalThis.__seq[path] || 0];
    globalThis.__seq[path] = (globalThis.__seq[path] || 0) + 1;
    return new Promise((resolve, reject) => {
      globalThis.__pending.push({ path: path, resolve: resolve, reject: reject,
                                  hit: hit, body: seq === undefined ? hit.body : seq });
    });
  }
  if (hit.status >= 400) {
    const detail = hit.body && hit.body.detail;
    const err = { status: hit.status, detail: detail,
                  message: typeof detail === 'string' ? detail
                    : 'Request failed (' + hit.status + ')' };
    return Promise.reject(err);
  }
  return Promise.resolve(hit.body);
}
// Answer every parked request with the body its route declares.
function __answer(list) {
  globalThis.__pending = [];
  list.forEach((r) => {
    if (r.hit.status >= 400) r.reject({ status: r.hit.status, message: 'boom' });
    else r.resolve(r.body);
  });
}
globalThis.__flush = function () { __answer(globalThis.__pending.slice()); };
// NEWEST FIRST. Requests do not come back in the order they were sent, and a
// stale response landing AFTER a fresh one is the only ordering in which the
// generation guard is the thing standing between a reviewer and the wrong pair.
globalThis.__flushNewestFirst = function () {
  __answer(globalThis.__pending.slice().reverse());
};
globalThis.__rerender = function () {
  const fresh = document.createElement('div');
  fresh.className = 'asc-wrap asc-wrap-review';
  host.appendChild(fresh);
  globalThis.__freshHost = fresh;
  window.AsclepiusReview.render(fresh, CTX);
};
globalThis.__calls = calls;

require(%(case_panel)s);
require(%(module)s);

const host = document.createElement('div');
host.id = 'ascRoot';
document.register(host);
document.body.appendChild(host);

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
function findByTag(el, tag, out) {
  out = out || [];
  if (el.tagName === tag) out.push(el);
  (el.children || []).forEach((c) => findByTag(c, tag, out));
  return out;
}
// Click a button by its dataset key/value, anywhere under the host.
globalThis.__click = function (key, value) {
  const hits = [];
  (function walk(el) {
    if (el.dataset && el.dataset[key] === value) hits.push(el);
    (el.children || []).forEach(walk);
  })(host);
  if (!hits.length) throw new Error('no element with data-' + key + '=' + value);
  hits[0].dispatch('click', { currentTarget: hits[0], target: hits[0] });
  return hits[0];
};
// A browser dispatches keydown at the FOCUSED element. Defaulting to
// document.body would have made every keyboard test blind to what focus is
// doing — and focus is now how the aim reaches assistive technology, so a
// harness that ignores it cannot see the thing under test.
globalThis.__key = function (key, target) {
  return document.dispatch('keydown', {
    key: key, target: target || document.activeElement || document.body });
};
globalThis.__focused = function () {
  var el = document.activeElement;
  if (!el) return null;
  return { tag: el.tagName, role: el.getAttribute('role'),
           state: el.dataset ? (el.dataset.state || el.dataset.fork || null) : null,
           text: el.textContent, checked: el.getAttribute('aria-checked'),
           group: (el.parentNode && el.parentNode.getAttribute)
             ? el.parentNode.getAttribute('aria-labelledby') : null };
};
// What a screen reader would be told: the group's accessible name, resolved
// through aria-labelledby exactly as the accessibility tree resolves it.
globalThis.__focusedGroupName = function () {
  var el = document.activeElement;
  if (!el || !el.parentNode || !el.parentNode.getAttribute) return null;
  var id = el.parentNode.getAttribute('aria-labelledby');
  if (!id) return el.parentNode.getAttribute('aria-label');
  var found = null;
  (function walk(n) {
    if (n.getAttribute && n.getAttribute('id') === id) found = n;
    (n.children || []).forEach(walk);
  })(host);
  return found ? found.textContent : null;
};
globalThis.__type = function (index, text) {
  const areas = findByTag(host, 'TEXTAREA');
  areas[index].value = text;
  areas[index].dispatch('input', { currentTarget: areas[index], target: areas[index] });
};
function submitButton() {
  const btns = findByTag(host, 'BUTTON').filter(
    (b) => b.textContent === 'Submit adjudication');
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
  const grids = findByClass(host, 'asc-answers');
  const judgment = findByClass(host, 'asc-rv-judgment')[0] || null;
  const caseFold = findByClass(host, 'asc-rv-case')[0] || null;
  return {
    text: host.textContent,
    classes: classesOf(host),
    tags: findByTag(host, 'PRE').length,
    grids: grids.map((g) => ({
      childCount: g.children.length,
      childClasses: g.children.map((c) => c.className),
    })),
    greenCards: findByClass(host, 'asc-answer-physician').length,
    eyebrows: findByClass(host, 'asc-answer-eyebrow').map((e) => e.textContent),
    clock: findByClass(host, 'asc-session-clock').map((e) => e.textContent),
    note: findByClass(host, 'asc-session-note').map((e) => e.textContent),
    errors: findByClass(host, 'asc-inline-error').map((e) => e.textContent),
    preview: findByClass(host, 'asc-rv-preview').map((e) => e.textContent),
    forks: findByClass(host, 'asc-rv-step-fork').map((e) => e.dataset.stepIdx),
    forkRows: findByClass(host, 'asc-rv-fork-row').map((e) => e.dataset.forkIdx),
    labTables: findByClass(host, 'asc-lab-table').length,
    caseTabs: findByClass(host, 'asc-case-tab').map((e) => e.textContent),
    caseOpen: caseFold ? !!caseFold.open : null,
    // DOM order: is the whole judgment panel reachable before the case body?
    judgmentBeforeCaseBody: judgment !== null,
    calls: globalThis.__calls.map((c) => c.url),
    sessionCalls: globalThis.__sessionCalls,
    drafts: globalThis.__drafts,
  };
};
globalThis.__host = host;

// The shell owns storage; the module asks for it through the ctx. Seeded so a
// restore can be driven, and observable so a save can be asserted.
globalThis.__drafts = %(drafts)s || {};
const DRAFTS = {
  save: function (id, value) { globalThis.__drafts[id] = value; },
  load: function (id) { return globalThis.__drafts[id] || null; },
  clear: function (id) { delete globalThis.__drafts[id]; },
};

const CTX = {
  h: h,
  clear: clear,
  api: api,
  drafts: %(with_drafts)s ? DRAFTS : undefined,
  toast: function () {},
  loadingCard: function (t) { return h('div', { class: 'asc-empty' }, t); },
  fmtDate: function (d) { return String(d); },
  casePanelCtx: function () {
    return { h: h, clear: clear, fetchAssetBlobUrl: function () { return Promise.reject(new Error('no assets')); } };
  },
  specialties: [{ specialty: 'nephrology', accent: 'green' }],
  preview: %(preview)s,
  goHome: function () { globalThis.__wentHome = true; },
};
window.AsclepiusReview.render(host, CTX);

// The module's boot chain is promise-based; drain the microtask queue first.
setTimeout(() => {
  const extra = %(drive)s;
  if (extra) { new Function(extra)(); }
  // Four macrotasks, so a drive script can schedule its own work and still be
  // reported on: a re-render's boot chain has to drain before its draw exists
  // to be answered.
  let ticks = 0;
  (function settle() {
    if (ticks++ < 4) { setTimeout(settle, 0); return; }
    const rep = globalThis.__report();
    rep.posts = globalThis.__calls.filter((c) => c.method === 'POST');
    rep.pending = globalThis.__pending.length;
    rep.drawCount = globalThis.__calls.filter(
      (c) => c.url.indexOf('/review/pair/next') !== -1).length;
    console.log(JSON.stringify(rep));
  })();
}, 0);
"""


def _me(**over):
    me = {
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
        "preview_only": False,
    }
    me.update(over)
    return me


# A real multimodal case: the bilirubin/GGT trajectory the PRD names. Rendering
# this as a TREND rather than a JSON dump is the whole of §1.1.
_CASE = {
    "specialty": "nephrology",
    "case_source": "real_deid",
    "demographics": {"age_band": "60-69", "sex": "F"},
    "problem_list": [{"condition": "Post-ERCP biliary stricture"}],
    "medications": [{"drug": "Ursodiol", "dose": "300 mg", "route": "PO", "freq": "BID"}],
    "lab_panels": [
        {"panel": "LFT", "collected_offset_days": -19,
         "results": [{"analyte": "Gamma GT", "value": 1361, "unit": "U/L", "flag": "HH"},
                     {"analyte": "Bilirubin", "value": 1.1, "unit": "mg/dL"}]},
        {"panel": "LFT", "collected_offset_days": -9,
         "results": [{"analyte": "Gamma GT", "value": 237, "unit": "U/L", "flag": "H"},
                     {"analyte": "Bilirubin", "value": 2.4, "unit": "mg/dL", "flag": "H"}]},
        {"panel": "LFT", "collected_offset_days": 0,
         "results": [{"analyte": "Gamma GT", "value": 62, "unit": "U/L"},
                     {"analyte": "Bilirubin", "value": 4.8, "unit": "mg/dL", "flag": "HH"}]},
    ],
    "notes": [{"note_type": "Progress", "author_role": "hepatology", "text": "Stent patent."}],
}

_STEPS_A = [{"text": "Confirm the stent is patent."},
            {"text": "Read GGT against bilirubin."},
            {"text": "Repeat imaging at 72 hours."}]
_STEPS_B = [{"text": "Confirm the stent is patent."},
            {"text": "Treat as cholangitis empirically."},
            {"text": "Repeat imaging at 72 hours."}]


def _pair_body(*, session=None, steps=True, preview=False, case=True):
    a_answer = {"verdict": "A_better", "chosen_id": "A",
                "from_scratch": {"ideal_answer": "Calcium gluconate first."}}
    b_answer = {"verdict": "B_better", "chosen_id": "B",
                "from_scratch": {"ideal_answer": "Emergent dialysis."}}
    if steps:
        a_answer["reasoning_steps"] = _STEPS_A
        b_answer["reasoning_steps"] = _STEPS_B
    pair = {
        "task_id": "t-1",
        "task": {"task_id": "t-1", "specialty": "nephrology",
                 "prompt": "K+ 7.1 with peaked T waves. Next step?",
                 "case": _CASE if case else None,
                 "candidate_answers": [{"id": "A", "text": "IV calcium"},
                                       {"id": "B", "text": "Dialyze"}]},
        "answers": [
            {"label": "A", "confidence": "high", "answer": a_answer},
            {"label": "B", "confidence": "medium", "answer": b_answer},
        ],
        "blinded": True,
    }
    if preview:
        pair["preview"] = True
        pair["draw_token"] = "preview:t-1"
    body = {"pair": pair, "session": session}
    if preview:
        body["preview"] = True
    return body


_EMPTY_QUEUE = {"/review/pair/next": {
    "status": 200,
    "body": {"pair": None, "session": None, "message": "No cases awaiting review."},
}}


def _routes(**over):
    routes = {
        "/review/me": {"status": 200, "body": _me()},
        "/review/pair/next": {"status": 200, "body": _pair_body()},
        "/review/stats": {"status": 200,
                          "body": {"review_ready": 4, "awaiting_second": 2, "adjudicated": 9}},
        "/review/double-label/next": {"status": 200, "body": {"task": None}},
        "/review/pair/t-1": {"status": 200,
                             "body": {"review": {"review_id": "rev-1"},
                                      "review_status": "reviewed",
                                      "identifier_flags": [],
                                      "corrections_withheld": False}},
    }
    routes.update(over)
    return routes


def _render(routes, drive: str = "", session_state=None,
            session_methods=("start", "stop", "state"), preview=False,
            drafts=None, with_drafts=True) -> dict:
    """``session_state`` is what Agent P's ``AsclepiusSession.state()`` returns.
    ``None`` means the page has no heartbeat client at all — which is what a
    reviewer sees before P's script is on the page.

    ``session_methods`` is the method set that client exposes, so a build
    predating one of them can be simulated."""
    return _run_node(_HARNESS % {
        "shim": json.dumps(str(_DOM_SHIM)),
        "module": json.dumps(str(_REVIEW_JS)),
        "case_panel": json.dumps(str(_CASE_PANEL_JS)),
        "hyperscript": _shell_hyperscript(),
        "routes": json.dumps(routes),
        "drive": json.dumps(drive) if drive else "null",
        "session_state": json.dumps(session_state) if session_state is not None else "null",
        "session_methods": json.dumps(list(session_methods)),
        "preview": "true" if preview else "false",
        "drafts": json.dumps(drafts or {}),
        "with_drafts": "true" if with_drafts else "false",
    })


# ═══ the module runs at all ══════════════════════════════════════════════════
def test_the_module_renders_through_the_shells_contract():
    out = _render(_routes())
    assert "/api/asclepius/review/pair/next" in out["calls"]
    assert out["errors"] == [] or not any(out["errors"])


def test_the_page_draws_a_PAIR_not_a_single_submission():
    out = _render(_routes())
    assert "/api/asclepius/review/pair/next" in out["calls"]
    assert "/api/asclepius/review/next" not in out["calls"]


# ═══ §2.1 — one product, two roles ═══════════════════════════════════════════
def test_review_is_a_view_in_the_shell_not_a_second_page():
    """§2.1 / §6.1. The standalone page is gone: no `review.html`, no
    `window.open`, and the portal routes to it with `switchView`."""
    assert not (_FRONTEND / "review.html").exists(), "the standalone review page is back"
    src = _code(_PORTAL_JS.read_text(encoding="utf-8"))
    assert "window.open('/asclepius/review'" not in src
    assert "switchView('review')" in src
    assert "view === 'review'" in src
    # ...and the module is loaded by the shell that mounts it.
    html = _INDEX_HTML.read_text(encoding="utf-8")
    srcs = re.findall(r'<script[^>]+src="[^"]*/(\w[\w.-]*\.js)"', html)
    assert "review.js" in srcs, "the review module is never loaded"
    assert "case_panel.js" in srcs, "the shared case panel is never loaded"


def test_the_module_exposes_the_same_contract_the_admin_sections_use():
    src = _REVIEW_JS.read_text(encoding="utf-8")
    assert "window.AsclepiusReview" in src
    assert "render: function (el, ctx)" in src
    portal = _PORTAL_JS.read_text(encoding="utf-8")
    assert "window.AsclepiusReview.render(host, reviewSectionCtx())" in portal


def test_the_module_has_no_hyperscript_or_token_of_its_own():
    """§1: its own `h()` and its own `localStorage` read are two of the four
    reasons review was structurally a different application."""
    src = _code(_REVIEW_JS.read_text(encoding="utf-8"))
    assert "function h(tag" not in src, "review.js grew its own hyperscript again"
    assert "localStorage" not in src, "review.js reads the session token itself again"
    assert "CTX.h" in src


def test_leaving_review_stops_the_paid_clock():
    """Navigating away is a no-work transition, exactly like an empty queue. A
    reviewer who opens the Guide must not go on accruing paid time against it."""
    out = _render(
        _routes(**{"/review/pair/next":
                   {"status": 200, "body": _pair_body(session=_SESSION)}}),
        "window.AsclepiusReview.teardown();",
        session_state={"continuous_seconds": 60, "min_seconds": 1200, "qualified": False},
    )
    assert [c for c in out["sessionCalls"] if c[0] == "stop"]
    portal = _PORTAL_JS.read_text(encoding="utf-8")
    assert "teardownReview()" in portal, "the shell never tears the module down"


# ═══ §2.2 — parity: the reviewer reads the LABELER'S chart ═══════════════════
def test_the_review_surface_renders_the_shared_case_panel_module():
    """§2.2 / §5 'Parity'. The chart is the SAME component, asserted by module:
    both surfaces call `window.AsclepiusCasePanel.render`. A fork would drift,
    and the first drift would be invisible — the reviewer would simply be reading
    a slightly older chart, and disagreeing with the labeler about it."""
    review = _code(_REVIEW_JS.read_text(encoding="utf-8"))
    portal = _code(_PORTAL_JS.read_text(encoding="utf-8"))
    for src, who in ((review, "review.js"), (portal, "asclepius.js")):
        assert "window.AsclepiusCasePanel" in src, f"{who} does not use the shared panel"
        assert "mod.render(" in src, f"{who} does not render through the shared panel"
    # And the panel is not reimplemented on either side.
    assert "SPECIALTY_UI" not in review and "SPECIALTY_UI" not in portal, (
        "the case panel was forked back into a surface"
    )
    assert "SPECIALTY_UI" in _code(_CASE_PANEL_JS.read_text(encoding="utf-8"))


def test_no_json_stringify_reaches_the_review_dom():
    """§5. `pretty()` used to dump labs, studies and medications through
    `JSON.stringify(value, null, 1)` into a <pre>. You cannot adjudicate a
    trajectory from a JSON dump."""
    src = _code(_REVIEW_JS.read_text(encoding="utf-8"))
    assert "JSON.stringify" not in src
    out = _render(_routes())
    assert out["tags"] == 0, "a <pre> survived on the review surface"
    assert "rv-mono" not in " ".join(out["classes"])


_OPEN_LABS_TAB = """
var tabs = [];
(function walk(el){ if (el.className && el.className.split(/\\s+/).indexOf('asc-case-tab') !== -1) tabs.push(el);
                    (el.children||[]).forEach(walk); })(globalThis.__host);
var labs = tabs.filter(function (t) { return t.getAttribute('data-tab') === 'labs'; })[0];
if (!labs) throw new Error('no labs tab');
labs.dispatch('click', { currentTarget: labs, target: labs });
"""


def test_lab_trends_render_as_trends_not_as_a_blob():
    """The GGT 1361 → 237 → 62 trajectory against a bilirubin that ROSE is the
    adjudication. It has to be a table with one column per collection offset."""
    out = _render(_routes(), _OPEN_LABS_TAB)
    assert out["labTables"] == 1, "the labs did not render as a trend table"
    assert "Labs (trend)" in out["caseTabs"]
    assert "Patient" in out["caseTabs"]
    for value in ("1361", "237", "62", "Gamma GT"):
        assert value in out["text"], value
    # The offsets are columns, so the trajectory is readable left to right.
    for day in ("day -19", "day -9", "day 0"):
        assert day in out["text"], day


def test_the_case_is_folded_until_doubted():
    """The docstring's instinct is right and is kept: the case is folded away by
    default. What is behind the fold changed, not whether it is folded."""
    out = _render(_routes())
    assert out["caseOpen"] is False
    assert "The case — open only if you doubt something" in out["text"]


# ═══ §2.3 — the 5-10 minute layout ═══════════════════════════════════════════
def test_the_judgment_controls_are_present_with_the_case_collapsed():
    """§5 'Layout / speed'. The verdict and all four dimensions are in the DOM,
    above the fold, while the case is still collapsed — the reviewer never
    scrolls past the chart to reach them."""
    out = _render(_routes())
    assert out["caseOpen"] is False
    assert out["judgmentBeforeCaseBody"], "there is no pinned judgment panel"
    for label in ("Which is stronger?", "Clinically correct", "Reasoning holds",
                  "Nothing decisive missing", "Grader is usable"):
        assert label in out["text"], label
    css = _CSS.read_text(encoding="utf-8")
    block = css.split(_PRD_1_CSS_HEADING)[1].split(_PRD_R_CSS_HEADING)[0]
    rule = block.split(".asc-rv-judgment {")[1].split("}")[0]
    assert "position: sticky" in rule, "the judgment panel is not pinned"


def test_reasoning_step_divergence_is_marked_when_both_sides_carry_steps():
    """§2.3's single highest-leverage addition. A and B agree at steps 1 and 3
    and part company at step 2; the reviewer's attention belongs at the fork."""
    out = _render(_routes())
    # Marked on BOTH columns — the mark says "they parted company here", never
    # "this column is the wrong one".
    assert out["forks"] == ["1", "1"], out["forks"]
    assert out["forkRows"] == ["1"], out["forkRows"]
    assert "Where the reasoning forks" in out["text"]
    assert "Step 2 — they diverge" in out["text"]


def test_nothing_diverges_when_only_one_side_carried_steps():
    """Absent is a valid value; a fabricated fork is not."""
    out = _render(_routes(**{"/review/pair/next":
                             {"status": 200, "body": _pair_body(steps=False)}}))
    assert out["forks"] == []
    assert out["forkRows"] == []
    assert "Where the reasoning forks" not in out["text"]


_ALL_KEYS = """
globalThis.__key('A');
globalThis.__key('ArrowLeft');
globalThis.__key('ArrowLeft');
globalThis.__key('ArrowLeft');
globalThis.__key('ArrowLeft');
"""


def test_the_keyboard_completes_a_clean_accept():
    """§2.3 'One-key accept'. A, then four ← to agree on every dimension (each
    advances the aim), then Enter. Six keystrokes, no mouse."""
    out = _render(_routes(), _ALL_KEYS + "globalThis.__key('Enter');")
    assert out["posts"], "the keyboard path posted nothing"
    body = out["posts"][0]["body"]
    assert body["verdict"] == "accept"
    assert body["stronger"] == "A"
    assert body["accepted_side"] == "A"
    assert set(body["dimensions"].values()) == {"agree"}


def test_every_documented_key_binds():
    """§5: A/B/N, 1-4, arrows and Enter all bind."""
    drive = """
globalThis.__key('B');
globalThis.__stronger = globalThis.__probeStronger();
globalThis.__key('N');
globalThis.__neither = globalThis.__probeStronger();
globalThis.__key('3');
globalThis.__key('ArrowRight');
globalThis.__key('4');
globalThis.__key('c');
globalThis.__key('1');
globalThis.__key('ArrowLeft');
globalThis.__key('2');
globalThis.__key('ArrowLeft');
globalThis.__report = (function (o) { return function () {
  var r = o();
  r.stronger = globalThis.__stronger; r.neither = globalThis.__neither;
  r.dims = globalThis.__probeDims();
  return r; }; })(globalThis.__report);
"""
    probes = """
globalThis.__probeStronger = function () {
  var segs = [];
  (function walk(el){ if (el.className === 'asc-rv-seg') segs.push(el);
                      (el.children||[]).forEach(walk); })(globalThis.__host);
  var on = segs[0].children.filter(function (b) { return b.classList.contains('is-on'); });
  return on.length ? on[0].dataset.state : null;
};
globalThis.__probeDims = function () {
  var rows = [];
  (function walk(el){ if (el.dataset && el.dataset.dimIdx !== undefined) rows.push(el);
                      (el.children||[]).forEach(walk); })(globalThis.__host);
  return rows.map(function (row) {
    var segs = [];
    (function walk(el){ if (el.className === 'asc-rv-seg') segs.push(el);
                        (el.children||[]).forEach(walk); })(row);
    var on = segs[0].children.filter(function (b) { return b.classList.contains('is-on'); });
    return on.length ? on[0].dataset.state : null;
  });
};
"""
    out = _render(_routes(), probes + drive)
    assert out["stronger"] == "B", "the B key did not set the comparison"
    assert out["neither"] == "equivalent", "the N key did not set 'neither'"
    # 1-4 aim; ← agree, → disagree, C can't assess. Each answer advances the
    # aim, so dimension 2 is answered by the ← that followed the ← on dimension 1.
    # C is a letter rather than ↓ because ↓ is how a reviewer scrolls the case.
    assert out["dims"] == ["agree", "agree", "disagree", "cannot_assess"], out["dims"]


def test_the_arrow_that_scrolls_the_case_is_not_a_shortcut():
    """`cannot_assess` is reachable from the keyboard (C), and ↓ still scrolls.
    A shortcut that eats the scroll key on a page whose whole point is reading a
    chart costs more than it saves."""
    probes = """
globalThis.__probeDims = function () {
  var rows = [];
  (function walk(el){ if (el.dataset && el.dataset.dimIdx !== undefined) rows.push(el);
                      (el.children||[]).forEach(walk); })(globalThis.__host);
  return rows.map(function (row) {
    var segs = [];
    (function walk(el){ if (el.className === 'asc-rv-seg') segs.push(el);
                        (el.children||[]).forEach(walk); })(row);
    var on = segs[0].children.filter(function (b) { return b.classList.contains('is-on'); });
    return on.length ? on[0].dataset.state : null;
  });
};
"""
    drive = probes + """
globalThis.__downEvent = globalThis.__key('ArrowDown');
globalThis.__report = (function (o) { return function () {
  var r = o(); r.dims = globalThis.__probeDims();
  r.downPrevented = globalThis.__downEvent.defaultPrevented; return r; }; })(globalThis.__report);
"""
    out = _render(_routes(), drive)
    assert out["dims"] == [None, None, None, None], "↓ answered a dimension"
    assert out["downPrevented"] is False, "↓ no longer scrolls the case"


def test_a_focused_control_keeps_its_own_enter():
    """Tabbing to 'open the case' and pressing Enter must open the case, not
    submit the adjudication. That is the worst possible misfire on this screen."""
    drive = _ALL_KEYS + """
var summaries = [];
(function walk(el){ if (el.tagName === 'SUMMARY') summaries.push(el);
                    (el.children||[]).forEach(walk); })(globalThis.__host);
globalThis.__key('Enter', summaries[0]);
"""
    out = _render(_routes(), drive)
    assert not out["posts"], "Enter on a focused <summary> submitted the review"


def test_enter_submits_only_when_a_verdict_and_all_four_dimensions_are_set():
    """§5. Enter on a half-answered review adjudicates nothing — a keystroke that
    could produce an unexamined accept is a keystroke that grades a physician's
    work by accident."""
    drive = """
globalThis.__key('A');
globalThis.__key('Enter');
globalThis.__afterPartial = globalThis.__calls.filter(function (c) { return c.method === 'POST'; }).length;
globalThis.__key('ArrowLeft');
globalThis.__key('ArrowLeft');
globalThis.__key('ArrowLeft');
globalThis.__key('Enter');
globalThis.__afterThree = globalThis.__calls.filter(function (c) { return c.method === 'POST'; }).length;
globalThis.__key('ArrowLeft');
globalThis.__key('Enter');
globalThis.__report = (function (o) { return function () {
  var r = o(); r.afterPartial = globalThis.__afterPartial;
  r.afterThree = globalThis.__afterThree; return r; }; })(globalThis.__report);
"""
    out = _render(_routes(), drive)
    assert out["afterPartial"] == 0, "Enter submitted with no dimensions answered"
    assert out["afterThree"] == 0, "Enter submitted with one dimension unanswered"
    assert len(out["posts"]) == 1, "the completed review did not submit"


def test_the_keyboard_never_fires_while_the_reviewer_is_typing():
    """A reviewer writing 'Anticoagulate' into the corrections box must not set
    the comparison to A on the first letter."""
    drive = """
globalThis.__click('state', 'equivalent');
globalThis.__click('verdict', 'reject');
var areas = [];
(function walk(el){ if (el.tagName === 'TEXTAREA') areas.push(el);
                    (el.children||[]).forEach(walk); })(globalThis.__host);
globalThis.__key('A', areas[0]);
globalThis.__key('B', areas[0]);
globalThis.__typedStronger = globalThis.__probeStronger();
globalThis.__report = (function (o) { return function () {
  var r = o(); r.typedStronger = globalThis.__typedStronger; return r; }; })(globalThis.__report);
"""
    probes = """
globalThis.__probeStronger = function () {
  var segs = [];
  (function walk(el){ if (el.className === 'asc-rv-seg') segs.push(el);
                      (el.children||[]).forEach(walk); })(globalThis.__host);
  var on = segs[0].children.filter(function (b) { return b.classList.contains('is-on'); });
  return on.length ? on[0].dataset.state : null;
};
"""
    out = _render(_routes(), probes + drive)
    assert out["typedStronger"] == "equivalent", "typing moved the comparison"


# ═══ §3 — the divergence lands in the payload ════════════════════════════════
def test_the_submitted_payload_carries_the_step_divergence():
    drive = """
globalThis.__key('A');
globalThis.__key('ArrowLeft');
globalThis.__key('ArrowLeft');
globalThis.__key('ArrowLeft');
globalThis.__key('ArrowLeft');
globalThis.__click('fork', 'A');
globalThis.__key('Enter');
"""
    out = _render(_routes(), drive)
    body = out["posts"][0]["body"]
    assert body["step_divergence"] == [{"index": 1, "judged": "A"}], body.get("step_divergence")


def test_no_step_divergence_is_sent_when_only_one_side_carried_steps():
    """§3: 'Absent is a valid value; a fabricated empty array is not.'"""
    drive = _ALL_KEYS + "globalThis.__key('Enter');"
    out = _render(_routes(**{"/review/pair/next":
                             {"status": 200, "body": _pair_body(steps=False)}}), drive)
    assert out["posts"], "nothing was submitted"
    assert "step_divergence" not in out["posts"][0]["body"]


def test_an_unjudged_fork_still_ships_as_a_fork():
    """The reviewer marked nothing at the fork. That is a real answer — the same
    rule `cannot_assess` encodes one control down — and the fork itself is still
    a measurement worth keeping."""
    drive = _ALL_KEYS + "globalThis.__key('Enter');"
    out = _render(_routes(), drive)
    assert out["posts"][0]["body"]["step_divergence"] == [{"index": 1, "judged": None}]


# ═══ §4.1 — the preview guard ════════════════════════════════════════════════
def test_a_preview_draw_asks_for_one_and_starts_no_session():
    out = _render(_routes(**{"/review/pair/next":
                             {"status": 200, "body": _pair_body(preview=True)}}),
                  preview=True,
                  session_state={"continuous_seconds": 60, "min_seconds": 1200,
                                 "qualified": False})
    assert "/api/asclepius/review/pair/next?preview=true" in out["calls"]
    assert not [c for c in out["sessionCalls"] if c[0] == "start"], (
        "a preview opened a paid session"
    )
    assert out["clock"] == []


def test_the_preview_banner_renders_whenever_preview_is_true():
    out = _render(_routes(**{"/review/pair/next":
                             {"status": 200, "body": _pair_body(preview=True)}}),
                  preview=True)
    assert out["preview"], "no preview banner"
    assert "nothing you submit here is recorded" in out["preview"][0].lower()


def test_the_server_can_force_preview_on_an_operator_who_did_not_ask():
    """§4.1. `preview_only` is the SERVER's answer to 'is this a real reviewer or
    an operator reaching the surface through the admin override'. The client
    never re-derives it from a tier."""
    routes = _routes(**{"/review/me": {"status": 200, "body": _me(preview_only=True)},
                        "/review/pair/next": {"status": 200,
                                              "body": _pair_body(preview=True)}})
    out = _render(routes)
    assert "/api/asclepius/review/pair/next?preview=true" in out["calls"]
    assert out["preview"], "an operator drew a pair with no preview banner"


def test_a_preview_submits_nothing():
    drive = _ALL_KEYS + "globalThis.__key('Enter');"
    out = _render(_routes(**{"/review/pair/next":
                             {"status": 200, "body": _pair_body(preview=True)}}),
                  drive, preview=True)
    assert not out["posts"], "a preview recorded an adjudication"
    assert any("nothing you submit here is recorded" in e.lower() for e in out["errors"])


def test_a_real_draw_echoes_the_draw_token_back_so_the_server_can_refuse_it():
    drive = _ALL_KEYS + "globalThis.__key('Enter');"
    out = _render(_routes(), drive)
    assert "draw_token" in out["posts"][0]["body"]


# ═══ the accent carries meaning ══════════════════════════════════════════════
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


# ═══ the countdown — it must never invent a second ═══════════════════════════
# The failure this replaces: the page read the credited seconds once at draw and
# then ADDED WALL-CLOCK DRIFT, with no heartbeat ever reaching the server. At
# 20:00 it rendered "This session has met its minimum" while the server had
# credited zero seconds. Under a "20 continuous minutes or $0" structure that is
# not a cosmetic bug — it is the page telling a physician they have been paid.
_SESSION = {"session_id": "ws-1"}


def test_the_countdown_renders_only_server_attested_seconds():
    out = _render(
        _routes(**{"/review/pair/next":
                   {"status": 200, "body": _pair_body(session=_SESSION)}}),
        session_state={"continuous_seconds": 751, "min_seconds": 1200,
                       "qualified": False},
    )
    assert out["clock"] == ["Session · 12:31 of 20:00"]
    # The module hands P's client the server's session and then only READS it.
    assert any(c[0] == "start" and c[1] == "ws-1" for c in out["sessionCalls"])


def test_every_beat_names_the_case_it_is_beating_for():
    """A heartbeat that names no work is not evidence of anything. The progress
    key must be the TASK — only this surface knows what a unit of review work is,
    which is why payments cannot supply it (PRD-P §8)."""
    out = _render(
        _routes(**{"/review/pair/next":
                   {"status": 200, "body": _pair_body(session=_SESSION)}}),
        session_state={"continuous_seconds": 10, "min_seconds": 1200,
                       "qualified": False},
    )
    starts = [c for c in out["sessionCalls"] if c[0] == "start"]
    assert starts, "P's client was never started"
    assert starts[0][1] == "ws-1"
    assert starts[0][2] == "t-1", "the beat names no work"


def test_an_empty_queue_stops_the_clock_the_reviewer_cannot_see():
    """P's client beats until told to stop or the tab hides. When the queue
    empties this surface sets SESSION to null and hides the clock — so a reviewer
    idling on an empty queue kept accruing paid time AND could not see that they
    were, because the clock was correctly hidden. Twenty minutes of that is $100.

    The empty-queue state is the one thing only this surface knows."""
    out = _render(_routes(**_EMPTY_QUEUE),
                  session_state={"continuous_seconds": 60, "min_seconds": 1200,
                                 "qualified": False})
    stops = [c for c in out["sessionCalls"] if c[0] == "stop"]
    assert stops, "the queue emptied and the beats carried on"
    assert stops[0][1], "stop was called without a reason"
    assert out["clock"] == []          # ...and the clock is still correctly hidden


def test_a_served_pair_never_stops_the_session():
    out = _render(
        _routes(**{"/review/pair/next":
                   {"status": 200, "body": _pair_body(session=_SESSION)}}),
        session_state={"continuous_seconds": 60, "min_seconds": 1200,
                       "qualified": False},
    )
    assert not [c for c in out["sessionCalls"] if c[0] == "stop"]


def test_a_failed_draw_stops_the_session_too():
    """Same shape as the empty queue: an error screen is not work, and the
    reviewer looking at it cannot tell that time is still being counted."""
    out = _render(
        _routes(**{"/review/pair/next": {"status": 500, "body": {"detail": "boom"}}}),
        session_state={"continuous_seconds": 60, "min_seconds": 1200,
                       "qualified": False},
    )
    assert [c for c in out["sessionCalls"] if c[0] == "stop"]


def test_the_page_survives_a_session_client_without_stop():
    """Feature-detected, like every other call across this seam. A build of P's
    client that predates `stop` must not take the review surface down with it —
    this seam has already produced one silent failure from a method that was
    guarded, present in the guard, and simply not there."""
    out = _render(_routes(**_EMPTY_QUEUE),
                  session_state={"continuous_seconds": 60, "min_seconds": 1200,
                                 "qualified": False},
                  session_methods=("start", "state"))
    assert "No cases awaiting review" in out["text"]
    assert out["errors"] == [] or not any(out["errors"])


def test_a_resumed_session_carries_no_nonce_and_that_is_fine():
    """P hands out a null nonce on a resumed open — a live one on every open
    turned idempotence into an unlimited credential dispenser. This surface
    forwards the session opaquely and must not grow code that expects one."""
    resumed = {"session_id": "ws-1", "nonce": None}
    out = _render(
        _routes(**{"/review/pair/next":
                   {"status": 200, "body": _pair_body(session=resumed)}}),
        session_state={"continuous_seconds": 300, "min_seconds": 1200,
                       "qualified": False},
    )
    assert out["clock"] == ["Session · 5:00 of 20:00"]
    assert any(c[0] == "start" for c in out["sessionCalls"])
    src = _REVIEW_JS.read_text(encoding="utf-8")
    assert "nonce" not in src, "the review surface reads a payments field it must not"


def test_the_clock_does_not_advance_on_its_own():
    """The heart of it. Firing every interval the module registered must not move
    a single digit, because the only thing that moves the clock is the server's
    own count coming back through the heartbeat client."""
    drive = """
globalThis.__before = globalThis.__report().clock[0];
for (var i = 0; i < 60; i++) globalThis.__tick();
globalThis.__after = globalThis.__report().clock[0];
globalThis.__report = (function (o) { return function () {
  var r = o(); r.before = globalThis.__before; r.after = globalThis.__after; return r; };
})(globalThis.__report);
"""
    out = _render(
        _routes(**{"/review/pair/next":
                   {"status": 200, "body": _pair_body(session=_SESSION)}}),
        drive,
        session_state={"continuous_seconds": 300, "min_seconds": 1200,
                       "qualified": False},
    )
    assert out["before"] == "Session · 5:00 of 20:00"
    assert out["after"] == out["before"], "the surface advanced its own clock"


def test_the_page_computes_no_session_time_of_its_own():
    """Source guard behind the behavioural one: no local session arithmetic
    survives. A clock derived from Date.now() is the defect, not its symptom."""
    src = _REVIEW_JS.read_text(encoding="utf-8")
    assert "sessionSeconds" not in src
    assert "_seenAt" not in src
    assert "credited_seconds" not in src, "R must not name P's fields (PRD R §4)"
    assert "1200" not in src, "a session length is hardcoded on the review surface"


def test_under_two_minutes_the_copy_changes_and_the_colour_does_not():
    """Lime means 'needs attention'. Pink means critical and blocking, and a
    running clock is not an emergency."""
    out = _render(
        _routes(**{"/review/pair/next":
                   {"status": 200, "body": _pair_body(session=_SESSION)}}),
        session_state={"continuous_seconds": 1120, "min_seconds": 1200,
                       "qualified": False},
    )
    assert out["clock"] == ["Session · 18:40 of 20:00"]
    assert out["note"] and "before this session qualifies" in out["note"][0]
    # The clock element's class is the same one in both states.
    assert out["classes"].count("asc-session-clock") == 1
    css = _CSS.read_text(encoding="utf-8")
    block = css.split(_PRD_R_CSS_HEADING)[1]
    clock_rule = block.split(".asc-session-clock")[1].split("}")[0]
    assert "--lime" in clock_rule and "--pink" not in clock_rule


def test_only_the_server_may_say_a_session_has_qualified():
    out = _render(
        _routes(**{"/review/pair/next":
                   {"status": 200, "body": _pair_body(session=_SESSION)}}),
        session_state={"continuous_seconds": 1500, "min_seconds": 1200,
                       "qualified": True},
    )
    assert out["note"] and "met its minimum" in out["note"][0]

    # ...and it does NOT say so on elapsed time alone.
    not_yet = _render(
        _routes(**{"/review/pair/next":
                   {"status": 200, "body": _pair_body(session=_SESSION)}}),
        session_state={"continuous_seconds": 1500, "min_seconds": 1200,
                       "qualified": False},
    )
    assert not any("met its minimum" in n for n in not_yet["note"])


def test_a_reviewer_with_no_heartbeat_client_is_told_so_explicitly():
    """An absent clock and a working-but-unpaid clock are indistinguishable, and
    under the $0 cliff that difference is the reviewer's whole fee. Say it."""
    out = _render(_routes(**{"/review/pair/next":
                             {"status": 200, "body": _pair_body(session=_SESSION)}}))
    assert out["clock"], "a reviewer whose time is not being counted saw nothing"
    assert "not being timed" in out["clock"][0].lower()
    assert out["note"] and "not accruing" in out["note"][0].lower()


def test_no_clock_at_all_when_the_server_opened_no_session():
    """Distinct from the case above: the server did not open a session, so there
    is nothing to time and nothing to warn about."""
    out = _render(_routes())
    assert out["clock"] == []


def test_the_shell_loads_agent_ps_heartbeat_client_before_the_review_module():
    """The seam this lived in: earnings.js builds `window.AsclepiusSession` and
    documents that the review surface calls it. Now that review is mounted inside
    the portal, the portal's page is where the ordering has to hold."""
    html = _INDEX_HTML.read_text(encoding="utf-8")
    srcs = re.findall(r'<script[^>]+src="[^"]*/(\w[\w.-]*\.js)"', html)
    assert "earnings.js" in srcs, "the heartbeat client is never loaded"
    assert srcs.index("earnings.js") < srcs.index("review.js"), \
        "review.js must not boot before the session client it consumes"
    assert srcs.index("case_panel.js") < srcs.index("review.js"), \
        "the review surface would render before the chart module exists"
    assert srcs.index("case_panel.js") < srcs.index("asclepius.js"), \
        "the labeler would render before the chart module exists"


# ═══ failure is visible ══════════════════════════════════════════════════════
def test_a_500_on_the_draw_renders_a_visible_error_not_a_silent_placeholder():
    out = _render(_routes(**{"/review/pair/next":
                             {"status": 500, "body": {"detail": "Queue exploded"}}}))
    assert any("Queue exploded" in e for e in out["errors"])
    assert "Retry" in out["text"]


def test_a_non_reviewer_gets_an_honest_state_rather_than_a_bare_403():
    out = _render(_routes(**{"/review/me": {"status": 200, "body": _me(can_review=False)}}))
    assert "does not have the reviewer tier" in out["text"]
    assert "/api/asclepius/review/pair/next" not in out["calls"]


def test_a_chart_that_will_not_draw_is_a_visible_failure():
    """A structured case whose panel did not render looks exactly like a
    text-only task. A physician must never adjudicate from the question alone
    without being told the chart is missing."""
    src = _REVIEW_JS.read_text(encoding="utf-8")
    assert "The clinical chart failed to load" in src
    portal = _PORTAL_JS.read_text(encoding="utf-8")
    assert "The clinical chart failed to load" in portal


# ═══ the judgment actually produces the right payload ════════════════════════
def _answer_all_dimensions():
    # Four dimension rows and the stronger row all share data-state, so click by
    # position: pick 'agree' in each dimension segment, then the stronger choice.
    return """
var segs = [];
(function walk(el){ if (el.className === 'asc-rv-seg') segs.push(el);
                    (el.children||[]).forEach(walk); })(globalThis.__host);
// segs[0] is "Which is stronger?"; the next four are the dimensions.
segs.slice(1, 5).forEach(function (s) {
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
globalThis.__disabledBefore = globalThis.__submitState();
globalThis.__type(0, 'Both miss the calcium step entirely.');
globalThis.__disabledAfter = globalThis.__submitState();
globalThis.__report = (function (orig) { return function () {
  var r = orig(); r.disabledBefore = globalThis.__disabledBefore;
  r.disabledAfter = globalThis.__disabledAfter; return r; }; })(globalThis.__report);
"""
    out = _render(_routes(), drive)
    assert out["disabledBefore"] is True, "reject-both was submittable with no reason"
    assert out["disabledAfter"] is False, "a reason was given and submit stayed blocked"


def test_accept_with_edits_names_the_physician_it_edits():
    """The side was hardcoded null once, so an edited accept anchored to
    ``pair_sub_a`` — the canonical oldest — instead of the physician whose answer
    the reviewer actually corrected. Per-labeler signal lost on every one."""
    drive = _answer_all_dimensions() + """
globalThis.__click('state', 'B');
globalThis.__click('verdict', 'accept_with_edits');
globalThis.__type(0, 'Right call, but the dose is wrong.');
globalThis.__submit();
"""
    out = _render(_routes(), drive)
    assert out["posts"], "no adjudication was posted"
    body = out["posts"][0]["body"]
    assert body["verdict"] == "accept_with_edits"
    assert body["stronger"] == "B"
    assert body["accepted_side"] == "B", "the edited accept named no physician"


def test_a_withheld_correction_is_reported_rather_than_silently_advanced():
    """The server returns ``corrections_withheld`` specifically so a reviewer can
    rewrite a note that will not ship. The surface used to discard the response
    and draw the next case — the reviewer finds out months later, or never."""
    drive = _answer_all_dimensions() + """
globalThis.__click('state', 'equivalent');
globalThis.__click('verdict', 'reject');
globalThis.__type(0, 'Per Dr Chen the K+ was 6.2 on 3/14.');
globalThis.__submit();
"""
    out = _render(_routes(**{"/review/pair/t-1": {
        "status": 200,
        "body": {"review": {"review_id": "rev-1"}, "review_status": "reviewed",
                 "identifier_flags": ["name", "date"], "corrections_withheld": True},
    }}), drive)
    text = out["text"]
    assert "withheld" in text.lower() or "not be shipped" in text.lower(), text
    assert "name" in text and "date" in text, "the reviewer is not told what was flagged"
    # And it does NOT silently move on to the next case.
    assert out["calls"].count("/api/asclepius/review/pair/next") == 1


def test_corrections_are_revealed_not_always_present():
    """An empty textarea under every review invites the reviewer to feel they owe
    prose on an accept. They don't."""
    src = _REVIEW_JS.read_text(encoding="utf-8")
    assert "display:none" in src
    assert "correctionsBox.style.display" in src


# ═══ the surface has to be reachable ═════════════════════════════════════════
def test_the_portal_routes_to_the_review_console_in_shell():
    """§6.1, restated for the unified Tasks surface. The review console's route
    from the portal is the review CARD on the Tasks dashboard — one surface for
    every kind of work, with the backend deciding what appears on it — and it now
    switches VIEW rather than opening a tab."""
    src = _PORTAL_JS.read_text(encoding="utf-8")
    assert "asc-dash-card-review" in src, "the Tasks surface has no review card"
    assert "dest: 'review'" not in src, "the retired Review rail tab came back"
    router = src.split("function setPanel(")[1][:1600]
    assert "switchView('review')" in router


def test_the_review_card_is_gated_on_the_servers_capability_never_a_tier():
    """The same rule every gated surface follows: the client reads the capability
    list the server put on the session. Re-deriving 'is this a reviewer?' in the
    frontend is the two-state check this codebase removed on purpose."""
    src = _PORTAL_JS.read_text(encoding="utf-8")
    card = src.split("asc-dash-card-review")[0][-600:]
    assert "sessionCan('review')" in card
    # And the destination re-checks it, so a hand-typed state change cannot open
    # a section the session was never granted.
    router = src.split("function setPanel(")[1][:1600]
    assert "sessionCan('review')" in router
    view = src.split("function renderReviewView()")[1][:600]
    assert "sessionCan('review')" in view


# ═══ §4 — the admin Evaluate chooser ═════════════════════════════════════════
def test_the_admin_evaluate_button_opens_a_two_way_chooser():
    src = _PORTAL_JS.read_text(encoding="utf-8")
    assert "openEvaluateChooser" in src
    chooser = src.split("function openEvaluateChooser(")[1].split("\n  }\n")[0]
    assert "'labeler'" in chooser and "'reviewer'" in chooser
    assert "adjudicate a pair" in chooser and "build an answer" in chooser
    # Dismiss on outside click and on Escape, with focus returned to the button.
    close = src.split("function closeEvaluateChooser(")[1].split("\n  }\n")[0]
    assert "removeEventListener('mousedown'" in close
    assert "removeEventListener('keydown'" in close
    assert "anchor.focus()" in close
    assert "'Escape'" in chooser
    # Remembered for the TAB, never the browser.
    assert "sessionStorage.setItem(EVAL_CHOICE_KEY" in chooser
    assert "localStorage.setItem(EVAL_CHOICE_KEY" not in src


def test_the_reviewer_row_of_the_chooser_always_previews():
    """§4.1. Clicking through the reviewer surface must not adjudicate a real
    pair. The chooser sets preview mode before it switches."""
    src = _PORTAL_JS.read_text(encoding="utf-8")
    chooser = src.split("function openEvaluateChooser(")[1].split("\n  }\n")[0]
    assert "state.reviewPreview = true;" in chooser
    assert "switchView('review')" in chooser


# ═══ the rules ═══════════════════════════════════════════════════════════════
def test_the_modules_never_use_innerHTML():
    for path in (_REVIEW_JS, _CASE_PANEL_JS):
        src = path.read_text(encoding="utf-8")
        assert "innerHTML" not in src, path.name
        assert "outerHTML" not in src, path.name
        assert "insertAdjacentHTML" not in src, path.name


def test_mobile_collapses_through_the_existing_breakpoint():
    """Follow the established pattern, do not invent a second breakpoint."""
    css = _CSS.read_text(encoding="utf-8")
    assert "@media (max-width: 880px)" in css
    breakpoint = css.split("@media (max-width: 880px)")[1].split("}")[0] + "}"
    assert ".asc-answers { grid-template-columns: 1fr; }" in breakpoint
    # And the PRD-R block reuses that grid rather than defining a second one.
    assert _PRD_R_CSS_HEADING in css
    prd_r = css.split(_PRD_R_CSS_HEADING)[1]
    assert "grid-template-columns" not in prd_r


def test_no_raw_hex_is_introduced_by_the_review_css():
    """Design system: do not introduce a hex value outside _tokens.css. The
    review page's own <style> block used to sit outside this guard entirely —
    four raw #fff and a `var(--orange)` on a physician judgment control — which
    is one of the reasons those rules moved into this file."""
    css = _CSS.read_text(encoding="utf-8")
    prd_1 = css.split(_PRD_1_CSS_HEADING)[1].split(_PRD_R_CSS_HEADING)[0]
    prd_r = css.split(_PRD_R_CSS_HEADING)[1]
    assert re.search(r"#[0-9a-fA-F]{3,8}\b", prd_1) is None, _PRD_1_CSS_HEADING
    assert re.search(r"#[0-9a-fA-F]{3,8}\b", prd_r) is None, _PRD_R_CSS_HEADING
    # Orange means MODEL OUTPUT in this product. The review blocks are physician
    # judgment controls; the rest of the stylesheet legitimately uses orange, so
    # the check is scoped to the two blocks rather than to everything after them.
    assert "--orange" not in prd_1, (
        "orange is model output; no physician judgment control may carry it")
    review_r = prd_r.split("/* ═══════════════════════════════════════════════════════════")[0]
    assert "--orange" not in review_r, (
        "orange is model output; no physician judgment control may carry it")


def test_the_preview_banner_survives_an_empty_queue():
    """§4.1 says PERSISTENT. An operator who previews into an empty queue, or
    into an error, must still be told that nothing here is recorded — otherwise
    the banner is only on the one screen they were least likely to misread."""
    out = _render(_routes(**{"/review/pair/next": {
        "status": 200,
        "body": {"pair": None, "preview": True, "session": None,
                 "message": "No cases awaiting review."}}}), preview=True)
    assert out["preview"], "the preview banner vanished with the queue"
    assert "No cases awaiting review" in out["text"]


# ═══ asynchrony: a response for a screen that no longer exists ═══════════════
#
# Every draw is asynchronous and every render replaces the host element, so a
# response can arrive for a screen that is gone. These are the three ways that
# happens, and all three used to end badly: a crash inside a promise handler, a
# stale pair painted over a fresh one, or two pairs claimed server-side with the
# first stranded behind a 45-minute lease.
_DEFERRED_DRAW = {"/review/pair/next": {"status": 200, "defer": True,
                                        "body": _pair_body()}}


def _two_draws():
    """One deferred route that answers the first draw with case t-1 and the
    second with t-2, so a test can name which one painted."""
    first, second = _pair_body(), _pair_body()
    second["pair"]["task_id"] = "t-2"
    second["pair"]["task"]["task_id"] = "t-2"
    second["pair"]["task"]["prompt"] = "SECOND CASE: sodium 118, seizing. Next step?"
    return {"/review/pair/next": {"status": 200, "defer": True,
                                  "bodies": [first, second]}}


def test_a_draw_that_lands_after_teardown_paints_nothing_and_throws_nothing():
    """Navigate to the Guide mid-draw. The module owns no element any more, so
    the resolving promise must return rather than clear(null)."""
    drive = """
window.AsclepiusReview.teardown();
globalThis.__flush();
"""
    out = _render(_routes(**_DEFERRED_DRAW), drive)
    # It survived (node would have exited non-zero on an unhandled rejection
    # from a throw inside the handler) and painted nothing into the dead host.
    assert out["grids"] == [], "a torn-down surface rendered a pair"
    assert out["errors"] == [] or not any(out["errors"])


def test_a_stale_draw_never_paints_over_a_fresh_one():
    """Re-mount mid-draw — which is what returning from the Guide does — and let
    the FIRST draw answer last, which is an ordering the network is entitled to
    produce. The reviewer must be left looking at the case they are actually
    holding, not at one a previous mount claimed."""
    drive = """
globalThis.__rerender();
// The second mount's own draw only exists once its boot chain has drained, so
// answer both from a later tick; newest first, so the stale response is the one
// that lands last and would otherwise win.
setTimeout(globalThis.__flushNewestFirst, 0);
globalThis.__report = (function (o) { return function () {
  var r = o(); r.freshText = globalThis.__freshHost.textContent; return r; };
})(globalThis.__report);
"""
    out = _render(_routes(**_two_draws()), drive)
    assert "SECOND CASE" in out["freshText"], "the fresh mount never got its pair"
    assert "peaked T waves" not in out["freshText"], (
        "a stale draw painted over the case the reviewer is holding"
    )


def test_two_clicks_on_retry_draw_one_pair_not_two():
    """Each draw CLAIMS a pair. A double click used to claim two and abandon the
    first for its whole lease, which is a case nobody can review for 45 minutes
    and a queue that looks emptier than it is."""
    drive = """
globalThis.__flush();                 // let the first draw settle
"""
    routes = _routes(**{"/review/pair/next": {"status": 500, "body": {"detail": "boom"}}})
    # A failed draw renders Retry; click it twice in the same tick.
    out = _render(routes, """
var btns = [];
(function walk(el){ if (el.tagName === 'BUTTON') btns.push(el);
                    (el.children||[]).forEach(walk); })(globalThis.__host);
var retry = btns.filter(function (b) { return b.textContent === 'Retry'; })[0];
retry.dispatch('click', { currentTarget: retry, target: retry });
retry.dispatch('click', { currentTarget: retry, target: retry });
""")
    # One draw on boot, one from the FIRST Retry click; the second is swallowed.
    assert out["drawCount"] == 2, out["drawCount"]


def test_the_evaluate_chooser_is_not_offered_to_a_session_that_cannot_review():
    """A qa_reviewer is an admin for the header and NOT an admin for the
    capability table (`capabilities.granted` overrides for role 'admin' alone).
    Offering them the chooser offers a door that bounces straight back to the
    dashboard, so without the capability the button stays what it was."""
    src = _code(_PORTAL_JS.read_text(encoding="utf-8"))
    nav = src.split("aria-haspopup")[0][-700:]
    assert "sessionCan('review')" in nav, "the chooser is offered without the capability"
    assert "canChoose ? openEvaluateChooser(e.currentTarget) : switchView('eval')" in src

# NOTE — there is deliberately no test for `drawing` being cleared only by the
# live generation, even though the code does exactly that.
#
# A test was written for it and DELETED: it passed against the mutated module
# too, because the only ways to re-enter loadNext() (Retry, "Check again", "Next
# case", a submit) all require a rendered screen, and while a draw is in flight
# the screen is the loading state, which has no controls. So the ordering cannot
# be observed from outside, and a test that cannot fail is worse than no test —
# it reads as coverage.
#
# The ordering stays because it is correct by construction: the flag belongs to
# whichever generation is drawing, and a generation that ends without its reply
# has it cleared by render() or teardown(). Written down here rather than
# asserted, because that is the honest form of this particular claim.


def test_the_view_reset_cannot_run_before_the_review_teardown():
    """An ordering invariant that the Tasks-resets-the-view rule made
    load-bearing.

    `setPanel` does two things to a reviewer leaving the surface: it tears the
    module down (stopping Agent P's beats and unbinding the keyboard), and — for
    a non-admin — it resets `state.view` to 'home'. The teardown is conditional
    on `state.view === 'review'`. So if the reset ever moves ABOVE it, the
    condition is already false by the time it is read, the teardown silently
    stops firing, and a reviewer who clicked Tasks goes on accruing paid time
    against a dashboard with no work on it. That is the exact failure the
    empty-queue stop exists to prevent, reintroduced by a line order.

    Nothing about this is visible in the rendered output, which is why it is
    asserted on the source.
    """
    src = _code(_PORTAL_JS.read_text(encoding="utf-8"))
    body = src.split("function setPanel(")[1].split("\n  }\n")[0]
    teardown = body.index("teardownReview()")
    reset = body.index("state.view = 'home'")
    assert teardown < reset, (
        "the view reset now runs before the review teardown, so the teardown's "
        "own condition is false when it is read and the session never stops"
    )
    # ...and the teardown is still skipped only for 'tasks', which is the one
    # destination that keeps the reviewer on the surface.
    assert "state.view === 'review' && dest !== 'tasks'" in body


# ═══ what the labeler captured actually reaches the reviewer ═════════════════
#
# Every test above this block asserts a NEGATIVE (no identity leak, no ordering
# tell, no colour bias) or the reviewer's own output shape. Nothing asserted
# that the reviewer can see what the physician wrote, which is why a phantom
# `citations` key sat in the server whitelist and in this module's renderer for
# months while no submission has ever carried one. Citations live nested as
# `evidence_anchor`/`evidence_anchors` in six places, so a reviewer had never
# seen a single citation a labeler entered, while one of the four things they
# grade is rubric quality and the premium export SKU is literally "grounded".

_ANCHOR = {"citation_text": "KDIGO 2024 hyperkalemia", "identifier": "KDIGO-2024-3.2",
           "source_type": "guideline"}


def _rich_pair_body():
    """A pair carrying the fields that were captured and never rendered."""
    body = _pair_body()
    a = body["pair"]["answers"][0]["answer"]
    a["verdict"] = "A_better"
    a["prompt_review"] = {"reviewed": True, "verdict": "valid",
                          "note": "The question is answerable as written."}
    a["independent_answer"] = {"text": "Stabilise the myocardium first.",
                               "kind": "full", "evidence_anchors": [_ANCHOR]}
    a["chosen_revision"] = {"edited": True, "revised_text": "IV calcium gluconate now.",
                            "why_better_tags": ["safer", "more_specific"],
                            "why_better_notes": "It sequences the treatment.",
                            "evidence_anchors": [_ANCHOR]}
    a["rejected_critique"] = {
        "error_tags": ["dosing_error"],
        "severities": {"dosing_error": "severe"},
        "error_tag_reasons": {"dosing_error": "wrong_units"},
        "why_worse": "It dialyses before stabilising.",
        "failure_tags": [{"mode": "hallucinated_fact", "note": "Invents a potassium threshold",
                          "tier": "critical"}],
        "error_tag_anchors": {"dosing_error": _ANCHOR},
    }
    a["reasoning_steps"] = [
        {"text": "Stabilise the myocardium.", "corrected": True,
         "original_text": "Give insulin first.", "correction_reason": "wrong_order",
         "step_error_tag": "sequencing", "label": "bad",
         "critique": "Insulin does not protect the heart.",
         "evidence_anchors": [_ANCHOR]},
        {"text": "Then shift potassium.", "confirmed": True, "label": "good"},
    ]
    a["rubric"] = [{"text": "Must give calcium before insulin.", "points": 3.0,
                    "axes": ["safety", "accuracy"], "tier": "critical", "critical": True,
                    "specific": True, "evidence_anchor": _ANCHOR}]
    return body


def _rich_routes():
    return _routes(**{"/review/pair/next": {"status": 200, "body": _rich_pair_body()}})


def test_the_reviewer_sees_a_citation_the_labeler_entered():
    """The defect this block exists for. Anchors are nested, and the renderer
    was looking for a top-level key that has never existed."""
    out = _render(_rich_routes())
    assert "KDIGO 2024 hyperkalemia" in out["text"]


def test_a_citation_is_rendered_beside_the_claim_it_supports():
    """A citation divorced from its claim is not reviewable, so anchors render
    under the thing they ground rather than in one fold at the bottom."""
    out = _render(_rich_routes())
    text = out["text"]
    claim = text.find("IV calcium gluconate now.")
    cite = text.find("KDIGO-2024-3.2")
    assert claim != -1 and cite != -1
    # The first citation occurrence follows the claim it grounds.
    assert cite > claim


def test_the_model_failure_taxonomy_reaches_the_reviewer():
    """A named export SKU that was captured and never shown."""
    out = _render(_rich_routes())
    assert "hallucinated_fact" in out["text"]
    assert "Invents a potassium threshold" in out["text"]


def test_error_severity_and_reason_reach_the_reviewer():
    out = _render(_rich_routes())
    assert "severe" in out["text"]
    assert "wrong_units" in out["text"]


def test_the_reviewer_sees_what_the_physician_did_to_each_step():
    """"They endorsed the model" and "they rewrote it" are different pieces of
    work and rendered identically before."""
    out = _render(_rich_routes())
    text = out["text"]
    assert "corrected" in text
    assert "confirmed" in text
    assert "Give insulin first." in text, "the model's original is not shown"
    assert "Insulin does not protect the heart." in text


def test_the_rubric_criticality_and_axes_reach_the_reviewer():
    """The grader hard-fails on a critical negative, and a reviewer grading
    "rubric quality" was shown neither the tier nor the axes."""
    out = _render(_rich_routes())
    text = out["text"]
    assert "critical" in text
    assert "safety" in text


def test_why_better_tags_reach_the_reviewer():
    out = _render(_rich_routes())
    assert "more_specific" in out["text"]


def test_the_stage_one_signoff_reaches_the_reviewer():
    out = _render(_rich_routes())
    assert "The question is answerable as written." in out["text"]


def test_a_ten_second_stance_is_distinguishable_from_a_full_blind_answer():
    """Without `kind` they render identically, and they are not the same work."""
    out = _render(_rich_routes())
    assert "full" in out["text"]


def test_the_phantom_citations_key_is_gone_from_the_renderer():
    src = _code(_REVIEW_JS.read_text(encoding="utf-8"))
    assert "a.citations" not in src, "the renderer still reads a key nothing produces"


def test_a_pair_with_none_of_these_fields_still_renders():
    """The additions are all conditional; a sparse answer must not blank the
    card."""
    out = _render(_routes())
    assert out["errors"] == [] or not any(out["errors"])
    assert "Calcium gluconate first." in out["text"]


# ═══ the judgment survives a refresh ═════════════════════════════════════════
#
# R lived only in memory, so a stray reload mid-adjudication threw away a
# senior physician's reading of a hard pair. Tolerable for four segmented
# controls; not tolerable now the card is worth reading properly.
#
# Storage belongs to the SHELL. This module having its own localStorage read
# was one of the reasons review was structurally a different application, and
# `test_the_module_has_no_hyperscript_or_token_of_its_own` pins that it has not
# grown one back, so the draft arrives through the ctx like `h` and `api`.

_PROBE_CORRECTIONS = """
globalThis.__report = (function (o) { return function () {
  var r = o();
  var boxes = [];
  (function walk(el){ if (el.style && el.style.display === 'none') boxes.push(el);
                      (el.children||[]).forEach(walk); })(globalThis.__host);
  var areas = [];
  (function walk(el){ if (el.tagName === 'TEXTAREA') areas.push(el);
                      (el.children||[]).forEach(walk); })(globalThis.__host);
  r.hiddenBoxes = boxes.length;
  r.textareaValues = areas.map(function (a) { return a.value || ''; });
  var segs = [];
  (function walk(el){ if (el.className === 'asc-rv-seg') segs.push(el);
                      (el.children||[]).forEach(walk); })(globalThis.__host);
  r.aimedStronger = (function () {
    if (!segs.length) return null;
    var on = segs[0].children.filter(function (b) { return b.classList.contains('is-on'); });
    return on.length ? on[0].dataset.state : null;
  })();
  return r; }; })(globalThis.__report);
"""


def test_a_judgment_is_saved_as_it_is_made():
    out = _render(_routes(), "globalThis.__click('state', 'A');")
    saved = out["drafts"].get("t-1")
    assert saved, "nothing was stored for the pair being judged"
    assert saved["stronger"] == "A"


def test_free_text_is_saved_as_it_is_typed():
    drive = """
globalThis.__click('state', 'equivalent');
globalThis.__click('verdict', 'reject');
globalThis.__type(0, 'half-written reason');
"""
    out = _render(_routes(), drive)
    assert out["drafts"]["t-1"]["notes"] == "half-written reason"


def test_a_saved_judgment_comes_back_after_a_reload():
    prior = {"t-1": {"verdict": "reject", "stronger": "equivalent", "acceptedSide": None,
                     "dimensions": {}, "notes": "half-written", "edited": ""}}
    out = _render(_routes(), _PROBE_CORRECTIONS, drafts=prior)
    assert "half-written" in out["textareaValues"]
    assert out["aimedStronger"] == "equivalent"


def test_restoring_drives_the_real_handlers_not_just_the_classes():
    """A restored judgment that LOOKS selected but did not run the side effects
    (the corrections box opening, acceptedSide coupling to `stronger`) is worse
    than no restore: the reviewer submits something they never chose."""
    prior = {"t-1": {"verdict": "reject", "stronger": "equivalent", "acceptedSide": None,
                     "dimensions": {}, "notes": "a reason", "edited": ""}}
    out = _render(_routes(), _PROBE_CORRECTIONS, drafts=prior)
    # reject opens the corrections box; a class-only restore leaves it hidden.
    assert out["hiddenBoxes"] == 0, "the corrections box stayed hidden after restore"


def test_a_preview_leaves_no_trace_a_real_reviewer_could_resume_into():
    """An operator sightseeing must not leave a judgment a real reviewer could
    resume into."""
    out = _render(
        _routes(**{"/review/pair/next": {"status": 200, "body": _pair_body(preview=True)}}),
        "globalThis.__click('state', 'A');",
        preview=True,
    )
    assert out["drafts"] == {}


def test_submitting_clears_the_local_copy():
    """Once the server has the judgment the local copy stops being a recovery
    aid and starts being a stale one."""
    drive = """
globalThis.__click('state', 'A');
globalThis.__click('verdict', 'accept:A');
globalThis.__click('dim-state-0', 'agree');
"""
    prior = {"t-1": {"verdict": "accept", "stronger": "A", "acceptedSide": "A",
                     "dimensions": {"clinical_accuracy": "agree",
                                    "reasoning_quality": "agree",
                                    "completeness": "agree",
                                    "rubric_quality": "agree"},
                     "notes": "", "edited": ""}}
    out = _render(_routes(), "if (globalThis.__submitState() === false) globalThis.__submit();",
                  drafts=prior)
    assert "t-1" not in out["drafts"]


def test_a_shell_that_offers_no_draft_store_still_works():
    """The module degrades to the old in-memory behaviour rather than throwing
    on a shell that predates the contract."""
    out = _render(_routes(), with_drafts=False)
    assert out["errors"] == [] or not any(out["errors"])


def test_the_restored_clock_does_not_bill_the_gap():
    """`startedAt` is deliberately NOT restored: it measures time on this case
    in this sitting, and a draft resumed tomorrow would bill the interval."""
    src = _REVIEW_JS.read_text(encoding="utf-8")
    assert "startedAt is NOT restored" in src
    assert "R.startedAt = saved" not in _code(src)


# ═══ the aim has to be perceivable to someone who cannot see it ══════════════
#
# The keyboard flow used to signal the aimed dimension with a left-edge
# box-shadow and nothing else. A reviewer on a screen reader pressed 3, heard
# nothing, pressed the left arrow, and had no way to know which dimension they
# had just answered. Focus is the fix: it is the one signal every assistive
# technology already follows, so the platform announces the group and the answer
# without anything bespoke to keep in sync.
def test_the_judgment_controls_are_radiogroups_with_names():
    """`.is-on` is paint. A screen reader reads roles and state, so a control
    that looks answered reads as unanswered without them."""
    probe = """
globalThis.__groups = function () {
  var out = [];
  (function walk(el){
    if (el.getAttribute && el.getAttribute('role') === 'radiogroup') {
      var kids = (el.children || []).filter(function (c) {
        return c.getAttribute && c.getAttribute('role') === 'radio'; });
      out.push({ named: !!(el.getAttribute('aria-labelledby') || el.getAttribute('aria-label')),
                 radios: kids.length });
    }
    (el.children||[]).forEach(walk);
  })(globalThis.__host);
  return out;
};
globalThis.__report = (function (o) { return function () {
  var r = o(); r.groups = globalThis.__groups(); return r; }; })(globalThis.__report);
"""
    out = _render(_routes(), probe)
    groups = out["groups"]
    # stronger + four dimensions + one fork + the verdict row.
    assert len(groups) == 7, groups
    assert all(g["named"] for g in groups), "a judgment group has no accessible name"
    assert all(g["radios"] >= 2 for g in groups), groups


def test_selecting_an_option_sets_aria_checked_not_just_a_class():
    drive = """
globalThis.__click('state', 'A');
globalThis.__click('verdict', 'reject');
globalThis.__checked = [];
(function walk(el){
  if (el.getAttribute && el.getAttribute('aria-checked') === 'true') {
    globalThis.__checked.push(el.dataset.state || el.dataset.verdict || el.dataset.fork);
  }
  (el.children||[]).forEach(walk);
})(globalThis.__host);
globalThis.__report = (function (o) { return function () {
  var r = o(); r.checked = globalThis.__checked; return r; }; })(globalThis.__report);
"""
    out = _render(_routes(), drive)
    assert "A" in out["checked"], "the stronger choice is not exposed as checked"
    assert "reject" in out["checked"], "the verdict is not exposed as checked"


def test_aiming_a_dimension_moves_real_focus_to_it():
    """Pressing 3 must put focus on the third dimension's group, so it is
    announced. Asserted through the group's accessible NAME, which is what a
    screen reader would actually say."""
    drive = """
globalThis.__key('3');
globalThis.__afterThree = globalThis.__focusedGroupName();
globalThis.__key('1');
globalThis.__afterOne = globalThis.__focusedGroupName();
globalThis.__report = (function (o) { return function () {
  var r = o(); r.afterThree = globalThis.__afterThree; r.afterOne = globalThis.__afterOne;
  return r; }; })(globalThis.__report);
"""
    out = _render(_routes(), drive)
    assert out["afterThree"] and "Nothing decisive missing" in out["afterThree"], out["afterThree"]
    assert out["afterOne"] and "Clinically correct" in out["afterOne"], out["afterOne"]


def test_answering_carries_the_focus_to_the_next_dimension():
    """The aim advances so four presses answer four dimensions. Focus has to
    advance with it, or the announcement describes the wrong row."""
    drive = """
globalThis.__key('1');
globalThis.__key('ArrowLeft');
globalThis.__afterAnswer = globalThis.__focusedGroupName();
globalThis.__focusState = globalThis.__focused();
globalThis.__report = (function (o) { return function () {
  var r = o(); r.afterAnswer = globalThis.__afterAnswer; r.focusState = globalThis.__focusState;
  return r; }; })(globalThis.__report);
"""
    out = _render(_routes(), drive)
    assert "Reasoning holds" in (out["afterAnswer"] or ""), out["afterAnswer"]
    # ...and what it landed on is a radio, not some container.
    assert out["focusState"]["role"] == "radio"


def test_the_last_dimension_still_announces_its_own_answer():
    """There is nothing to advance to, so focus stays put rather than being
    dropped — otherwise the fourth answer is the one nobody hears."""
    drive = """
globalThis.__key('4');
globalThis.__key('ArrowRight');
globalThis.__report = (function (o) { return function () {
  var r = o(); r.name = globalThis.__focusedGroupName(); r.f = globalThis.__focused();
  return r; }; })(globalThis.__report);
"""
    out = _render(_routes(), drive)
    assert "Grader is usable" in (out["name"] or ""), out["name"]
    assert out["f"]["state"] == "disagree" and out["f"]["checked"] == "true"


def test_arriving_on_the_surface_never_steals_focus():
    """Stealing focus on render is its own accessibility problem, and would yank
    the viewport into the judgment panel before the reviewer has read the pair.
    Only a deliberate keystroke moves it."""
    out = _render(_routes(), """
globalThis.__report = (function (o) { return function () {
  var r = o(); r.f = globalThis.__focused(); return r; }; })(globalThis.__report);
""")
    assert out["f"] is None or out["f"]["role"] != "radio", out["f"]


def test_each_group_is_one_tab_stop_not_three():
    """Roving tabindex. Three tab stops per dimension would be twelve on this
    screen before the verdict row is reached."""
    probe = """
globalThis.__report = (function (o) { return function () {
  var r = o(); r.tabbable = [];
  (function walk(el){
    if (el.getAttribute && el.getAttribute('role') === 'radiogroup') {
      var kids = (el.children||[]).filter(function (c) {
        return c.getAttribute && c.getAttribute('tabindex') === '0'; });
      r.tabbable.push(kids.length);
    }
    (el.children||[]).forEach(walk);
  })(globalThis.__host);
  return r; }; })(globalThis.__report);
"""
    out = _render(_routes(), probe)
    assert out["tabbable"] == [1] * len(out["tabbable"]), out["tabbable"]
    assert len(out["tabbable"]) == 7


def test_the_tab_stop_follows_the_selection():
    """The other half of the roving contract. If the tab stop stays on the first
    option after a different one is chosen, a reviewer who tabs back into the
    group lands on an option they did not pick — and Space there would silently
    change their answer."""
    drive = """
globalThis.__click('state', 'equivalent');
globalThis.__report = (function (o) { return function () {
  var r = o();
  var segs = [];
  (function walk(el){ if (el.className === 'asc-rv-seg') segs.push(el);
                      (el.children||[]).forEach(walk); })(globalThis.__host);
  var stop = segs[0].children.filter(function (b) { return b.getAttribute('tabindex') === '0'; });
  r.stopState = stop.length === 1 ? stop[0].dataset.state : ('n=' + stop.length);
  return r; }; })(globalThis.__report);
"""
    out = _render(_routes(), drive)
    assert out["stopState"] == "equivalent", out["stopState"]


def test_the_focus_ring_is_not_clipped_by_the_pill():
    """`.asc-rv-seg` clips to its pill shape, which would cut the base
    `:focus-visible` outline in half on the two end segments — so a keyboard
    reviewer could not see which option the aim was on."""
    css = _CSS.read_text(encoding="utf-8")
    block = css.split(_PRD_1_CSS_HEADING)[1].split(_PRD_R_CSS_HEADING)[0]
    assert "overflow: hidden" in block.split(".asc-rv-seg {")[1].split("}")[0]
    rule = block.split(".asc-rv-seg button:focus-visible")[1].split("}")[0]
    assert "outline" in rule and "-2px" in rule, rule
