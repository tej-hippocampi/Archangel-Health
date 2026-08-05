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
      hasProposal: text.indexOf('proposes reviewer') !== -1,
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


def test_no_innerhtml_anywhere_in_the_touched_frontend():
    """Context pack §5: zero innerHTML — every server string goes through textContent or
    h() children."""
    import re
    for name in ("onboarding.js", "admin_physicians.js"):
        src = (_FRONTEND / name).read_text(encoding="utf-8")
        # Strip comments first: both files talk ABOUT innerHTML in a "never do this"
        # comment, and a grep that cannot tell prose from code is a test that has to be
        # deleted the first time someone documents the rule.
        code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        code = re.sub(r"//[^\n]*", "", code)
        assert "innerHTML" not in code, name


def test_no_new_css_classes_were_invented_outside_the_owned_files():
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
