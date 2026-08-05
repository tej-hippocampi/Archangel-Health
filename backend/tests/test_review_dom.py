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
globalThis.__tick = function () { timers.slice().forEach((fn) => fn()); };

// Agent P's heartbeat client, when the page has one. `state()` is the ONLY
// contract the review surface consumes: server-attested seconds, never a local
// clock. `__sessionCalls` records what the review page asked of it.
globalThis.__sessionCalls = [];
const SESSION_STATE = %(session_state)s;
if (SESSION_STATE !== null) {
  globalThis.window.AsclepiusSession = {
    start: function (s) { globalThis.__sessionCalls.push(['start', s && s.session_id]); },
    state: function () { globalThis.__sessionCalls.push(['state']); return SESSION_STATE; },
  };
}

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
    sessionCalls: globalThis.__sessionCalls,
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


def _render(routes, drive: str = "", session_state=None) -> dict:
    """``session_state`` is what Agent P's ``AsclepiusSession.state()`` returns.
    ``None`` means the page has no heartbeat client at all — which is what a
    reviewer sees today, before P's script is on the page."""
    return _run_node(_HARNESS % {
        "shim": json.dumps(str(_DOM_SHIM)),
        "module": json.dumps(str(_REVIEW_JS)),
        "routes": json.dumps(routes),
        "drive": json.dumps(drive) if drive else "null",
        "session_state": json.dumps(session_state) if session_state is not None else "null",
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


# ═══ the countdown — U1: it must never invent a second ═══════════════════════
# The failure this replaces: the page read `credited_seconds` once at draw and
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
    # The page hands P's client the server's session and then only READS it.
    assert ["start", "ws-1"] in out["sessionCalls"]
    assert ["state"] in out["sessionCalls"]


def test_the_clock_does_not_advance_on_its_own():
    """The heart of U1. Firing every interval the page registered must not move
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
    assert out["after"] == out["before"], "the page advanced its own clock"


def test_the_page_computes_no_session_time_of_its_own():
    """Source guard behind the behavioural one: no local session arithmetic
    survives. A clock derived from Date.now() is the defect, not its symptom."""
    src = _REVIEW_JS.read_text(encoding="utf-8")
    assert "sessionSeconds" not in src
    assert "_seenAt" not in src
    assert "credited_seconds" not in src, "R must not name P's fields (PRD R §4)"


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
    block = css.split("PRD-R — the paired review surface")[1]
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
    """U2. An absent clock and a working-but-unpaid clock are indistinguishable,
    and under the $0 cliff that difference is the reviewer's whole fee. Say it."""
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


def test_the_page_loads_agent_ps_heartbeat_client_before_its_own_module():
    """The seam U1 lived in: earnings.js builds `window.AsclepiusSession` and
    documents that the review surface calls it, but it was script-tagged into
    index.html — a different page. Nothing loaded it here."""
    import re
    html = _REVIEW_HTML.read_text(encoding="utf-8")
    # Compare the actual <script src> tags, in document order — prose mentioning
    # a filename is not a load.
    srcs = re.findall(r'<script[^>]+src="[^"]*/(\w[\w.-]*\.js)"', html)
    assert "earnings.js" in srcs, "the heartbeat client is never loaded"
    assert srcs.index("earnings.js") < srcs.index("review.js"), \
        "review.js must not boot before the session client it consumes"


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


def test_accept_with_edits_names_the_physician_it_edits():
    """M3. The side was hardcoded null, so an edited accept anchored to
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
    """U4. The server returns ``corrections_withheld`` specifically so a reviewer
    can rewrite a note that will not ship. The page discarded the response and
    drew the next case — the reviewer finds out months later, or never."""
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


def test_the_review_page_stylesheet_is_inside_the_design_guard():
    """U5. The guard scanned asclepius.css only, so review.html became a
    stylesheet location outside it — four raw #fff, and `var(--orange)` on a
    physician judgment control. Orange means MODEL OUTPUT in this product."""
    import re
    html = _REVIEW_HTML.read_text(encoding="utf-8")
    style = html.split("<style>")[1].split("</style>")[0]
    assert re.search(r"#[0-9a-fA-F]{3,8}\b", style) is None, \
        "raw hex in the review page's stylesheet"
    assert "--orange" not in style, \
        "orange is model output; no physician judgment control may carry it"


def test_corrections_are_revealed_not_always_present():
    """An empty textarea under every review invites the reviewer to feel they owe
    prose on an accept. They don't."""
    src = _REVIEW_JS.read_text(encoding="utf-8")
    assert "display:none" in src
    assert "correctionsBox.style.display" in src


# ═══ U3 — the surface has to be reachable ════════════════════════════════════
_PORTAL_JS = _FRONTEND / "asclepius.js"


def test_the_portal_has_a_route_to_the_review_console():
    """U3. Nothing in the portal linked to /asclepius/review. A promoted
    reviewer signed in and saw Tasks · Community · Advisor · Guide — the review
    console linked BACK to the portal, and nothing linked forward. It fell in the
    gap between two ownership lists, which is why a surface can be complete,
    correct and unreachable."""
    src = _PORTAL_JS.read_text(encoding="utf-8")
    assert "/asclepius/review" in src, "the review console has no route from the portal"
    # It is in the rail, with an icon, like every other destination.
    assert "dest: 'review'" in src
    assert "review:" in src.split("RAIL_ICONS")[1][:2000]


def test_the_review_entry_is_gated_on_the_servers_capability_never_a_tier():
    """The same rule the Advisor entry follows: the client reads the capability
    list the server put on the session. Re-deriving 'is this a reviewer?' in the
    frontend is the two-state check this codebase removed on purpose."""
    import re
    src = _PORTAL_JS.read_text(encoding="utf-8")
    entry = re.search(r"\{[^{}]*dest:\s*'review'[^{}]*\}", src)
    assert entry, "no rail entry for the review console"
    assert "capability: 'review'" in entry.group(0)
    # And the destination re-checks it, so a hand-typed state change cannot open
    # a section the session was never granted.
    router = src.split("function setPanel(")[1][:1200]
    assert "sessionCan('review')" in router


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
