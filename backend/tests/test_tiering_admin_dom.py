"""PRD C §6 — the admin surface actually renders.

Source-grepping a frontend module proves it was written, not that it works. This repo has
already paid for that lesson twice: a verification queue that was complete, correct and
INVISIBLE for a whole build round because the tab probed a global nobody defined.

So these execute the shipped ``onboarding.js`` against the DOM shim with a scripted API and
assert what lands in the document — including the design-system rule that carries meaning:
**pink is a genuine blocker, and a merely-low score is not pink.**
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
_DOM_SHIM = Path(__file__).resolve().parent / "_asclepius_dom.js"


def _strip_js_comments(src: str) -> str:
    """Both files talk ABOUT the things they must not do, in comments explaining why. A grep
    that cannot tell prose from code is a test that gets deleted the first time someone
    documents a rule."""
    import re
    return re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", src, flags=re.S))


def _run_node(script: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed in this environment")
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\n{proc.stderr}\n{proc.stdout}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


# onboarding.js talks to the network with bare `fetch` and reads its bearer from
# localStorage, so both are stubbed rather than a ctx.api being handed in.
_HARNESS = """
require(%(shim)s);
var ROUTES = %(routes)s;
var calls = [];
global.localStorage = { getItem: function () { return 'test-token'; },
                        setItem: function () {}, removeItem: function () {} };
global.fetch = function (url, opts) {
  calls.push({ url: url, method: (opts && opts.method) || 'GET',
               body: opts && opts.body ? JSON.parse(opts.body) : null });
  var path = url.replace('/api/asclepius', '');
  var payload = ROUTES[path];
  if (payload === undefined) payload = ROUTES[path.split('?')[0]] || {};
  return Promise.resolve({
    ok: true, status: 200,
    json: function () { return Promise.resolve(payload); },
  });
};
global.URL = { createObjectURL: function () { return 'blob:x'; } };
window.open = function () {};
eval(require('fs').readFileSync(%(module)s, 'utf8'));

function textOf(el) {
  if (el.nodeValue != null) return el.nodeValue;
  return (el.childNodes || []).map(textOf).join(' ');
}
function walk(el, out) {
  out = out || [];
  (el.childNodes || []).forEach(function (c) {
    if (!c.tagName) return;
    out.push(c);
    walk(c, out);
  });
  return out;
}
function withClass(root, cls) {
  return walk(root).filter(function (e) {
    return (e.className || '').split(' ').indexOf(cls) !== -1;
  });
}
function byTag(root, tag) {
  return walk(root).filter(function (e) { return e.tagName === tag; });
}
function clickText(root, label) {
  var hit = byTag(root, 'BUTTON').filter(function (b) {
    return textOf(b).indexOf(label) !== -1;
  });
  if (!hit.length) throw new Error('no button matching ' + label);
  hit[0].dispatch('click');
  return hit[0];
}
function later(fn) { setTimeout(fn, 5); }
"""

_TIERING_OK = {
    "case_domain": "nephrology",
    "features": {"domain_match": 1.0, "board_certified_active": 1.0},
    "score": 4.7,
    "p_tr": 0.991,
    "band": "reviewer",
    "proposed_tier": "reviewer",
    "thresholds": {"tr": 1.0, "tl": -1.0},
    "ranked_contributions": [{"feature": "domain_match", "contribution": 1.6}],
    "reasons": ["+1.60  domain expertise matches this case",
                "+1.20  holds an active board certification"],
    "gates": {
        "A1": {"state": "pass", "label": "NPI verified", "detail": "NPPES: active"},
        "A5": {"state": "unknown", "label": "Not on the OIG LEIE exclusion list",
               "detail": "OIG LEIE list not loaded — cannot check"},
    },
    "gates_eligible": False,
    "gates_failed": [],
    "gates_undetermined": ["A5"],
    "tr_eligible": True,
    "tr_missing": [],
    "was_exploration": False,
    "domain_match_why": "subspecialty board certification in nephrology",
    "calibration": {"composite": 0.92, "tr_gate_passed": True},
    "leie_loaded_at": None,
}

_TIERING_BLOCKED = dict(
    _TIERING_OK,
    score=2.2, band="blocked", proposed_tier=None,
    gates={"A1": {"state": "fail", "label": "NPI verified",
                  "detail": "Another account already claims this NPI"}},
    gates_failed=["A1"], gates_undetermined=[], tr_eligible=False,
    reasons=["+1.60  domain expertise matches this case",
             "BLOCKER  A1: Another account already claims this NPI"],
)

_TIERING_LOW = dict(
    _TIERING_OK,
    score=-1.8, band="labeler", proposed_tier="labeler",
    gates={"A1": {"state": "pass", "label": "NPI verified", "detail": "NPPES: active"}},
    gates_failed=[], gates_undetermined=[], tr_eligible=False,
    tr_missing=["calibration exam at the TR gate"],
    reasons=["-2.50  baseline — TR is the minority role"],
)


def _dossier(tiering):
    return {
        "user_id": "u1", "email": "doc@example.com", "full_name": "Jane Doe",
        "specialty": "nephrology", "score": 70, "proposed_tier": "reviewer",
        "reasons": [], "blockers": [], "has_cv": False,
        "npi": {"npi": "1234567893", "result": "verified", "recheck_pending": False},
        "tiering": tiering,
    }


def _queue(tiering):
    row = _dossier(tiering)
    return {"status": "pending", "count": 1, "total": 1, "queue": [row], "has_more": False}


def _script(tiering, tail):
    routes = {
        "/verify/queue": _queue(tiering),
        "/verify/recheck-pending": {"count": 0},
        "/verify/queue/u1": _dossier(tiering),
        "/verify/tiering/u1/decide": {"ok": True,
                                      "decision": {"admin_tier": "labeler", "was_flip": 1}},
    }
    return (_HARNESS % {"shim": json.dumps(str(_DOM_SHIM)),
                        "module": json.dumps(str(_FRONTEND / "onboarding.js")),
                        "routes": json.dumps(routes)}) + tail


_OPEN_ROW = """
var root = document.createElement('div');
window.AsclepiusVerification.mount(root, {});
later(function () {
  clickText(root, 'Jane Doe');
  later(function () {
"""


def test_the_tier_proposal_and_its_reasons_render_in_the_dossier():
    """§6: the proposed tier with its reasons in plain words, ranked by contribution."""
    out = _run_node(_script(_TIERING_OK, _OPEN_ROW + """
    var text = textOf(root);
    console.log(JSON.stringify({
      hasProposal: text.indexOf('proposes Reviewer') !== -1,
      hasReason: text.indexOf('domain expertise matches this case') !== -1,
      hasThresholdLine: text.indexOf('above the reviewer threshold') !== -1,
      hasScore: text.indexOf('4.70') !== -1,
      hasWhy: text.indexOf('subspecialty board certification in nephrology') !== -1,
      hasOverride: byTag(root, 'BUTTON').filter(function (b) {
        return textOf(b).indexOf('Record decision') !== -1; }).length,
    }));
  });
});
"""))
    assert out["hasProposal"] is True
    assert out["hasReason"] is True
    assert out["hasThresholdLine"] is True
    assert out["hasScore"] is True
    assert out["hasWhy"] is True
    # The override control — because the override IS the training signal.
    assert out["hasOverride"] == 1


def test_a_genuine_blocker_is_pink_and_an_unresolved_gate_is_not():
    """Design system §5: pink means flag / critical / blocking. An UNRESOLVED gate is 'we
    could not check', which is not a blocker — painting it pink is how an admin learns to
    distrust the colour."""
    blocked = _run_node(_script(_TIERING_BLOCKED, _OPEN_ROW + """
    console.log(JSON.stringify({
      pinkCount: withClass(root, 'vq-flag').length,
      pinkText: withClass(root, 'vq-flag').map(textOf).join(' | '),
      badge: withClass(root, 'vq-badge-blocked').length,
    }));
  });
});
"""))
    assert blocked["pinkCount"] > 0
    assert "Another account already claims this NPI" in blocked["pinkText"]
    assert blocked["badge"] == 1

    unresolved = _run_node(_script(_TIERING_OK, _OPEN_ROW + """
    console.log(JSON.stringify({
      pinkText: withClass(root, 'vq-flag').map(textOf).join(' | '),
      mutedText: withClass(root, 'vq-attempt').map(textOf).join(' | '),
    }));
  });
});
"""))
    assert "OIG LEIE list not loaded" not in unresolved["pinkText"]
    assert "OIG LEIE list not loaded" in unresolved["mutedText"]


def test_a_merely_low_score_is_never_pink():
    """§6, stated explicitly in the PRD: 'A merely-low score is not pink.'"""
    out = _run_node(_script(_TIERING_LOW, _OPEN_ROW + """
    console.log(JSON.stringify({
      pinkText: withClass(root, 'vq-flag').map(textOf).join(' | '),
      text: textOf(root),
    }));
  });
});
"""))
    assert out["pinkText"].strip() == ""
    assert "below the labeler threshold" in out["text"]
    assert "Not yet reviewer-eligible: calibration exam at the TR gate" in out["text"]


def test_the_exploration_badge_appears_only_on_a_deliberate_probe():
    """An admin must know a proposal was a probe rather than the model's best guess —
    otherwise they read a Thompson-sampled promotion as the model's considered view."""
    explored = dict(_TIERING_OK, was_exploration=True)
    on = _run_node(_script(explored, _OPEN_ROW + """
    console.log(JSON.stringify({ lime: withClass(root, 'asc-badge-lime').length,
                                 text: textOf(root) }));
  });
});
"""))
    assert on["lime"] == 1
    assert "Exploration" in on["text"]

    off = _run_node(_script(_TIERING_OK, _OPEN_ROW + """
    console.log(JSON.stringify({ lime: withClass(root, 'asc-badge-lime').length }));
  });
});
"""))
    assert off["lime"] == 0


def test_recording_a_decision_posts_the_tier_and_the_case_domain():
    out = _run_node(_script(_TIERING_OK, _OPEN_ROW + """
    var sel = byTag(root, 'SELECT').filter(function (s) {
      return (s.attributes['aria-label'] || '') === 'Record tier'; })[0];
    sel.value = 'labeler';
    clickText(root, 'Record decision');
    later(function () {
      var post = calls.filter(function (c) { return c.method === 'POST'; });
      console.log(JSON.stringify({ n: post.length, url: post[0] && post[0].url,
                                   body: post[0] && post[0].body }));
    });
  });
});
"""))
    assert out["n"] == 1
    assert out["url"].endswith("/verify/tiering/u1/decide")
    assert out["body"]["tier"] == "labeler"
    assert out["body"]["case_domain"] == "nephrology"


def test_a_tiering_failure_renders_a_visible_error_not_a_silent_placeholder():
    """Context pack §5: if a module fails to load, render a VISIBLE error. The same rule
    applies to a row whose proposal could not be computed."""
    out = _run_node(_script({"error": "tiering proposal unavailable for this row"},
                            _OPEN_ROW + """
    console.log(JSON.stringify({
      errors: withClass(root, 'vq-error').map(textOf),
    }));
  });
});
"""))
    assert any("tiering proposal unavailable" in e for e in out["errors"])


# ═══ AUDIT UI — no raw tier token ever reaches a human ════════════════════════

_TIERING_WORDED = dict(
    _TIERING_OK,
    proposed_tier_word="Reviewer",
    tier_options=[{"value": "labeler", "word": "Labeler"},
                  {"value": "reviewer", "word": "Reviewer"}],
    available_domains=[{"value": "nephrology", "word": "Nephrology"},
                       {"value": "cardiology", "word": "Cardiology"}],
)


def test_ui_no_raw_tier_token_is_rendered_to_a_human():
    """Context pack §3, stated twice: "Never render a raw tier token to a human."
    `proposes labeler`, a <select> whose labels are `labeler`/`reviewer`, and
    `Recorded labeler` were three violations on one panel."""
    out = _run_node(_script(_TIERING_WORDED, _OPEN_ROW + """
    var sel = byTag(root, 'SELECT').filter(function (s) {
      return (s.attributes['aria-label'] || '') === 'Record tier'; })[0];
    sel.value = 'labeler';
    clickText(root, 'Record decision');
    later(function () {
      console.log(JSON.stringify({
        text: textOf(root),
        optionLabels: byTag(root, 'OPTION').map(textOf),
      }));
    });
  });
});
"""))
    # The words a human sees.
    assert "proposes Reviewer" in out["text"]
    assert "Recorded Labeler" in out["text"]
    # And no raw token anywhere in the rendered text.
    for token in ("proposes labeler", "proposes reviewer", "Recorded labeler",
                  "Recorded reviewer"):
        assert token not in out["text"], token
    assert "labeler" not in " ".join(out["optionLabels"])
    assert "Labeler" in " ".join(out["optionLabels"])


def test_ui_the_specialty_list_comes_from_the_server():
    """`var domains = ['nephrology','cardiology','oncology']` hardcoded in the client is wrong
    the week a specialty is added, and wrong silently — the picker just lacks an entry."""
    out = _run_node(_script(_TIERING_WORDED, _OPEN_ROW + """
    var sel = byTag(root, 'SELECT').filter(function (s) {
      return (s.attributes['aria-label'] || '').indexOf('case domain') !== -1; })[0];
    console.log(JSON.stringify({ options: byTag(sel, 'OPTION').map(function (o) {
      return o.value; }) }));
  });
});
"""))
    # Exactly what the server sent — two, not the three that used to be hardcoded.
    assert out["options"] == ["nephrology", "cardiology"]

    code = _strip_js_comments((_FRONTEND / "onboarding.js").read_text(encoding="utf-8"))
    assert "nephrology" not in code, "a specialty name is still hardcoded in the client"



def test_no_innerhtml_anywhere_in_the_touched_frontend():
    """Context pack §5: zero innerHTML — every server string goes through textContent or
    h() children."""
    for name in ("onboarding.js", "admin_physicians.js"):
        code = _strip_js_comments((_FRONTEND / name).read_text(encoding="utf-8"))
        assert "innerHTML" not in code, name


# ═══ AUDIT M2 — the candidate-facing screens exist and are reachable ══════════

_EXAM = {
    "attempt_id": "catt_1", "specialty": "nephrology", "size": 2, "resumed": False,
    "items": [
        {"item_id": "i1", "specialty": "nephrology", "stem": "A 62-year-old on metformin…",
         "answers": [{"id": "A", "text": "Hold metformin"}, {"id": "B", "text": "Continue"}],
         "steps": [{"id": "s1", "text": "eGFR is 28"}, {"id": "s2", "text": "so continue"}],
         "criteria": ["eGFR threshold", "dose adjustment"]},
        {"item_id": "i2", "specialty": "nephrology", "stem": "Hyponatraemia at 112…",
         "answers": [{"id": "A", "text": "3% saline"}, {"id": "B", "text": "Fluid restrict"}],
         "steps": [{"id": "s1", "text": "Symptomatic"}, {"id": "s2", "text": "so restrict"}],
         "criteria": ["correction rate"]},
    ],
}


def _candidate_script(routes, tail):
    return (_HARNESS % {"shim": json.dumps(str(_DOM_SHIM)),
                        "module": json.dumps(str(_FRONTEND / "onboarding.js")),
                        "routes": json.dumps(routes)}) + tail


def test_m2_the_calibration_exam_has_a_candidate_facing_screen():
    """AUDIT M2: `grep -rn "calibration/exam" frontend/ landing/` returned NO MATCHES. The
    exam was reachable only by raw API call, so even after items are keyed no physician could
    sit it through the product. A gate nobody can attempt is a gate nobody passes."""
    out = _run_node(_candidate_script({"/verify/calibration/exam": _EXAM}, """
var root = document.createElement('div');
window.AsclepiusCalibration.mount(root, {});
later(function () {
  var text = textOf(root);
  console.log(JSON.stringify({
    hasGlobal: typeof window.AsclepiusCalibration.mount === 'function',
    stem: text.indexOf('62-year-old on metformin') !== -1,
    bothAnswers: text.indexOf('Hold metformin') !== -1 && text.indexOf('Continue') !== -1,
    steps: text.indexOf('eGFR is 28') !== -1,
    criteria: text.indexOf('eGFR threshold') !== -1,
    radios: byTag(root, 'INPUT').filter(function (i) {
      return i.attributes.type === 'radio'; }).length,
    progress: text.indexOf('1 of 2') !== -1,
  }));
});
"""))
    assert out["hasGlobal"] is True
    assert out["stem"] and out["bothAnswers"] and out["steps"] and out["criteria"]
    assert out["radios"] >= 4          # 2 answers + 2 steps on the first item
    assert out["progress"] is True


def test_m2_the_exam_screen_never_receives_or_renders_a_key():
    out = _run_node(_candidate_script({"/verify/calibration/exam": _EXAM}, """
var root = document.createElement('div');
window.AsclepiusCalibration.mount(root, {});
later(function () {
  console.log(JSON.stringify({ text: textOf(root),
                               src: require('fs').readFileSync(%s, 'utf8') }));
});
""" % json.dumps(str(_FRONTEND / "onboarding.js"))))
    for leak in ("better_answer_id", "break_step", "load_bearing"):
        assert leak not in out["text"]


def test_m2_the_exam_submits_every_answered_item_in_the_raw_shape():
    out = _run_node(_candidate_script(
        {"/verify/calibration/exam": _EXAM,
         "/verify/calibration/catt_1/submit": {"ok": True, "composite": 0.9,
                                               "tr_gate_passed": True,
                                               "subscores": {"choice": 1.0}}}, """
var root = document.createElement('div');
window.AsclepiusCalibration.mount(root, {});
later(function () {
  function pick(name, value) {
    byTag(root, 'INPUT').filter(function (i) {
      return i.attributes.name === name && i.value === value;
    }).forEach(function (i) { i.checked = true; i.dispatch('change'); });
  }
  pick('answer-i1', 'A'); pick('step-i1', 's1');
  clickText(root, 'Next');
  later(function () {
    pick('answer-i2', 'B'); pick('step-i2', 's2');
    clickText(root, 'Submit');
    later(function () {
      var post = calls.filter(function (c) { return c.method === 'POST'; });
      console.log(JSON.stringify({ n: post.length, body: post[0] && post[0].body,
                                   after: textOf(root) }));
    });
  });
});
"""))
    assert out["n"] == 1
    responses = out["body"]["responses"]
    assert responses["i1"]["better_answer_id"] == "A"
    assert responses["i1"]["break_step"] == "s1"
    assert responses["i2"]["better_answer_id"] == "B"
    # The result is shown, not swallowed.
    assert "90%" in out["after"] or "0.9" in out["after"]


def test_m2_an_unready_exam_says_so_rather_than_failing_silently():
    """503 is 'not ready', not 'you failed'. The physician must not read it as a rejection."""
    out = _run_node((_HARNESS % {
        "shim": json.dumps(str(_DOM_SHIM)),
        "module": json.dumps(str(_FRONTEND / "onboarding.js")),
        "routes": json.dumps({}),
    }).replace("ok: true, status: 200,",
               "ok: false, status: 503,").replace(
        "return Promise.resolve(payload);",
        "return Promise.resolve({detail: 'nephrology: 0 admissible keyed items, need 12'});") + """
var root = document.createElement('div');
window.AsclepiusCalibration.mount(root, {});
later(function () {
  console.log(JSON.stringify({ text: textOf(root),
                               errors: withClass(root, 'asc-error').length }));
});
""")
    assert "not ready" in out["text"].lower() or "not yet" in out["text"].lower()
    assert "fail" not in out["text"].lower()


def test_m2_the_demographics_screen_exists_and_is_voluntary():
    """The fairness monitor is structurally correct, correctly pseudonymised — and permanently
    empty, because nothing collected the input."""
    out = _run_node(_candidate_script({"/verify/demographics/u1": {"ok": True}}, """
var root = document.createElement('div');
window.AsclepiusDemographics.mount(root, { userId: 'u1' });
later(function () {
  var text = textOf(root);
  var opts = byTag(root, 'OPTION').map(function (o) { return o.value; });
  console.log(JSON.stringify({
    voluntary: text.toLowerCase().indexOf('voluntary') !== -1,
    saysWhy: text.toLowerCase().indexOf('fairness') !== -1
             || text.toLowerCase().indexOf('bias') !== -1,
    saysNotUsed: text.toLowerCase().indexOf('never') !== -1,
    hasPreferNot: opts.indexOf('prefer_not_to_say') !== -1,
    selects: byTag(root, 'SELECT').length,
  }));
});
"""))
    assert out["voluntary"] and out["saysWhy"] and out["saysNotUsed"]
    # "Prefer not to say" must be offered on every dimension — a form without it coerces.
    assert out["hasPreferNot"] is True
    assert out["selects"] >= 2


def test_m2_demographics_posts_only_what_was_answered():
    out = _run_node(_candidate_script({"/verify/demographics/u1": {"ok": True}}, """
var root = document.createElement('div');
window.AsclepiusDemographics.mount(root, { userId: 'u1' });
later(function () {
  var sels = byTag(root, 'SELECT');
  sels[0].value = 'woman'; sels[0].dispatch('change');
  clickText(root, 'Submit');
  later(function () {
    var post = calls.filter(function (c) { return c.method === 'POST'; });
    console.log(JSON.stringify({ url: post[0] && post[0].url, body: post[0] && post[0].body }));
  });
});
"""))
    assert out["url"].endswith("/verify/demographics/u1")
    demo = out["body"]["demographics"]
    assert demo == {"gender": "woman"}, demo   # unanswered dimensions are absent, not blank


def test_m2_both_screens_self_mount_on_a_hash_route():
    """onboarding.js is already loaded by index.html on every portal page, so a hash route is
    the only way to make these reachable without editing asclepius.js — which is outside Agent
    C's write allowlist."""
    out = _run_node(_candidate_script({"/verify/calibration/exam": _EXAM}, """
console.log(JSON.stringify({
  calibration: typeof window.AsclepiusCalibration.openIfRouted === 'function',
  demographics: typeof window.AsclepiusDemographics.openIfRouted === 'function',
}));
"""))
    assert out["calibration"] is True and out["demographics"] is True
    src = (_FRONTEND / "onboarding.js").read_text(encoding="utf-8")
    assert "#/calibration" in src and "#/demographics" in src


# ═══ AUDIT UI — the model-health panel is a table, not a debug console ════════

def test_ui_model_health_renders_a_real_table_not_space_padded_strings():
    """Space-padding does not align in a proportional face, and `vq-reason` is not mono. This
    is the screen where you prove to yourself, and to a regulator, that the model is behaving."""
    code = _strip_js_comments((_FRONTEND / "admin_physicians.js").read_text(encoding="utf-8"))
    assert "'PINNED  '" not in code and '"PINNED  "' not in code
    assert "asc-table" in code
    assert "asc-mono" in code
    # The two error classes are visually distinct and the file used them interchangeably.
    # They now split by MEANING: asc-error is reserved for the one blocking case — a module
    # that did not load, so physicians cannot be approved at all.
    assert code.count("'asc-error'") == 1, "asc-error is the blocking case only"
    assert code.count("'asc-inline-error'") >= 2
    # Raw stored tokens (race_ethnicity, prefer_not_to_say) must not reach a human here.
    assert "humanize(" in code
    """asclepius.css is outside Agent C's write allowlist (context pack §2), so every class
    the new panels use must already exist in it. A class with no rule renders as unstyled
    text, which looks like a broken page rather than a missing style."""
    css = (_FRONTEND / "asclepius.css").read_text(encoding="utf-8")
    used = {
        "vq-flag", "vq-reason", "vq-attempt", "vq-badge", "vq-badge-blocked",
        "vq-badge-reviewer", "vq-badge-labeler", "vq-section-label", "vq-dossier",
        "vq-actions", "vq-tier-select", "vq-btn", "vq-error", "vq-facts",
        "asc-badge", "asc-badge-lime", "asc-card", "asc-card-pad", "asc-error",
    }
    missing = sorted(c for c in used if f".{c}" not in css)
    assert not missing, f"classes with no rule in asclepius.css: {missing}"
