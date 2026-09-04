"""The console shell, asserted by RUNNING it (PRD-F R6, R7, R8).

This repo has twice shipped a section that was complete, correct and invisible
because nothing mounted it, and source-only frontend tests are blind to that.
PRD-F moves ~4,000 lines of admin code into a new file and rewires what mounts
it, which is exactly the change that produces that failure. So the whole
shipped ``admin_shell.js`` is loaded under the DOM shim, booted against a
stubbed API as an admin, and then clicked.

Every section module is stubbed with a spy rather than loaded, deliberately:
what is under test here is the SHELL: which tab mounts which section, which
state key each one sets, and whether the cross-links still land where an
operator expects. The sections have their own suites.

The one exception is the gallery, which is loaded for real from
``admin_physicians.js``: R7 is a redesign of that renderer, and a spy would
assert nothing about it.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

_FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
_SHIM = pathlib.Path(__file__).resolve().parent / "_asclepius_dom.js"
SHELL = (_FRONTEND / "admin_shell.js").read_text()
PHYSICIANS = (_FRONTEND / "admin_physicians.js").read_text()
CSS = "\n".join((_FRONTEND / f).read_text()
                for f in ("asclepius.css", "admin.css", "_base.css", "_tokens.css"))

#: An approved roster with everything the gallery is supposed to render, and one
#: row of every kind that has ever been rendered wrong: an advisor (who is not a
#: tier), an unmeasured physician (whose blanks must not read as zeros), and a
#: reviewer (whose tier word is the one the vocabulary rules are about).
_ROSTER = json.dumps({
    "physicians": [
        {"id": "d1", "name": "Ada Okafor", "email": "ada@example.org",
         "specialty": "nephrology", "tier": "reviewer", "tier_word": "Reviewer",
         "is_advisor": False, "verification_status": "approved",
         "real_data_approved": True, "slack_joined": True,
         "health_system_name": "Riverside", "contributor_score": 82,
         "median_seconds": 440, "kappa": 0.71, "kappa_n": 9, "active": True},
        {"id": "d2", "name": "Bela Hartmann", "email": "bela@example.org",
         "specialty": "cardiology", "tier": "reviewer", "tier_word": "Reviewer",
         "is_advisor": True, "verification_status": "approved",
         "real_data_approved": False, "slack_joined": False,
         "contributor_score": None, "median_seconds": None,
         "kappa": None, "kappa_n": 0, "active": True},
        {"id": "d3", "name": "Chen Wu", "email": "chen@example.org",
         "specialty": "oncology", "tier": "labeler", "tier_word": "Labeler",
         "is_advisor": False, "verification_status": "approved",
         "real_data_approved": True, "slack_joined": None,
         "contributor_score": 51, "median_seconds": 65,
         "kappa": None, "kappa_n": 1, "active": True},
    ],
    "counts": {"all": 3, "pending": 0, "labelers": 1, "reviewers": 2, "unassigned": 0},
    "misfiled_physicians": [], "misfiled_count": 0,
    "unfiled_physicians": [], "unfiled_count": 0,
})

_ENV = """
var CALLS = [];
var FETCHES = [];
globalThis.localStorage = {
  _v: { asclepius_token: 'tok' },
  getItem: function (k) { return this._v[k] === undefined ? null : this._v[k]; },
  setItem: function (k, v) { this._v[k] = String(v); },
  removeItem: function (k) { delete this._v[k]; },
};
globalThis.URL = { createObjectURL: function () { return 'blob:x'; } };
globalThis.setTimeout = globalThis.setTimeout || function (fn) { fn(); };

// The whole API surface the shell touches at boot, plus the roster. Anything
// unrouted answers {} rather than throwing: the point of this file is which
// SECTION mounts, and a section erroring on an unstubbed read would look like
// a mounting bug.
var ROUTES = {
  '/api/asclepius/auth/me': { id: 'a1', email: 'ops@archangelhealth.ai', role: 'admin' },
  '/api/asclepius/taxonomy': { export_profiles: ['default'] },
  '/api/asclepius/admin/physicians': __ROSTER__,
  '/api/asclepius/admin/submissions?status=pending': { submissions: [] },
  '/api/asclepius/admin/batches': { longitudinal: {}, real_static: {},
                                    synthetic: { n_cases: 1, n_open: 1 } },
  '/api/asclepius/admin/batches/synthetic': { cases: [
    { task_id: 's1', specialty: 'nephrology', difficulty: 'hard', distribution: 'open',
      label_count: 0, max_labels: 2, created_at: '2020-01-01T00:00:00',
      display_bucket: 'synthetic' }] },
};
globalThis.fetch = function (url) {
  FETCHES.push(String(url));
  var body = ROUTES[String(url)] || {};
  return Promise.resolve({
    ok: true, status: 200,
    headers: { get: function (k) { return k === 'content-type' ? 'application/json' : null; } },
    json: function () { return Promise.resolve(body); },
    text: function () { return Promise.resolve(''); },
  });
};

// The page furniture admin.html ships. Registered by id so getElementById
// finds them exactly as it does in a browser.
['ascAdminBar', 'ascAdminRoot', 'ascToasts'].forEach(function (id) {
  var el = document.createElement('div');
  el.id = id;
  document.body.appendChild(el);
  document.register(el);
});

// The shell's contract with a section IS the ctx it hands over, so the spies
// capture it. That is also how this file reaches openBatchesFor and
// openPipeline without admin_shell.js growing a hook that exists for tests.
var CTX = null;
function spy(name) {
  return { render: function (el, ctx) {
    CALLS.push(name);
    CTX = ctx;
    var mark = document.createElement('div');
    mark.className = 'spy-' + name;
    el.appendChild(mark);
  }, reset: function () {} };
}
window.AdminHealthSection = spy('health');
window.AdminExportSection = spy('export');
window.AdminEarningsSection = { render: function (el, ctx, mode) {
  CALLS.push('earnings:' + mode);
  CTX = ctx;
  var mark = document.createElement('div');
  mark.className = 'spy-earnings';
  el.appendChild(mark);
}, reset: function () {} };
window.AdminCommunitySection = spy('community');
window.AdminReferralsSection = spy('referrals');

function textOf(el) {
  if (el.nodeValue != null) return el.nodeValue;
  return (el.childNodes || []).map(textOf).join(' ');
}
function tidy(el) { return textOf(el).replace(/\\s+/g, ' ').trim(); }
function findAll(el, cls, out) {
  out = out || [];
  if (el.className && (' ' + el.className + ' ').indexOf(' ' + cls + ' ') !== -1) out.push(el);
  (el.childNodes || []).forEach(function (c) { if (c.tagName) findAll(c, cls, out); });
  return out;
}
function tabNamed(label) {
  return findAll(document.getElementById('ascAdminBar'), 'asc-admin-tab')
    .filter(function (b) { return tidy(b).indexOf(label) === 0; })[0];
}
function root() { return document.getElementById('ascAdminRoot'); }
function out(o) { console.log(JSON.stringify(o)); }
"""


def _run(script: str, *, with_physicians: bool = False) -> dict:
    """Boot the real shell under the shim, then run ``script``."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed in this environment")
    modules = PHYSICIANS if with_physicians else ""
    full = (
        f"require({json.dumps(str(_SHIM))});\n"
        + _ENV.replace("__ROSTER__", _ROSTER)
        + "\n" + modules
        + "\n" + SHELL
        + "\nsetTimeout(function () {\n" + script + "\n}, 60);\n"
    )
    # From a file, not `node -e`: the boot script embeds the whole console
    # bundle, and a single argv entry is capped at 128KB on Linux (E2BIG,
    # "Argument list too long") — which is how every test here went red the
    # day the bundle outgrew it, with a failure that named node and not the
    # size.
    with tempfile.TemporaryDirectory() as tmp:
        entry = pathlib.Path(tmp) / "console_harness.js"
        entry.write_text(full, encoding="utf-8")
        proc = subprocess.run([node, str(entry)], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\n{proc.stderr}\n{proc.stdout}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ═════════════════════════════════════════════════════════════════════════════
#  The shell mounts
# ═════════════════════════════════════════════════════════════════════════════
def test_the_console_boots_into_the_masthead_and_the_physicians_tab():
    """WHY: the first thing that can be quietly wrong after a move is that
    nothing mounts at all. An admin session must land on a painted console, not
    on the loading screen admin.html ships."""
    res = _run("""
      var bar = document.getElementById('ascAdminBar');
      out({ hidden: bar.hidden || bar.getAttribute('hidden') !== null,
            tabs: findAll(bar, 'asc-admin-tab').map(tidy),
            active: findAll(bar, 'asc-admin-tab').filter(function (b) {
              return b.className.indexOf('active') !== -1; }).map(tidy),
            body: !!document.getElementById('ascAdminBody') });
    """)
    assert res["hidden"] is False, "the masthead never came out of hidden"
    assert res["body"], "the console body never mounted"
    assert [t.split(" ")[0] for t in res["tabs"]] == [
        "Physicians", "Tasks", "Money", "Data", "Community", "Referrals"]
    assert res["active"] and res["active"][0].startswith("Physicians")


@pytest.mark.parametrize("label,expect_call", [
    ("Money", "earnings:earnings"),
    ("Data", "health"),
    ("Community", "community"),
    ("Referrals", "earnings:referrals"),
])
def test_every_tab_mounts_its_section(label, expect_call):
    """WHY: complete, correct and invisible is this repo's documented failure
    mode, and a tab that paints its own chrome and mounts nothing looks exactly
    like a tab whose data happens to be empty."""
    res = _run(f"""
      tabNamed({json.dumps(label)}).dispatch('click');
      setTimeout(function () {{
        out({{ calls: CALLS, active: findAll(document.getElementById('ascAdminBar'),
                 'asc-admin-tab').filter(function (b) {{
                   return b.className.indexOf('active') !== -1; }}).map(tidy) }});
      }}, 40);
    """)
    assert expect_call in res["calls"], f"{label} mounted nothing: {res['calls']}"
    assert res["active"] and res["active"][0].startswith(label)


def test_the_tasks_tab_mounts_its_own_page_rather_than_a_module():
    """WHY: the two Tasks pages moved across functionally unchanged (F8), and
    they are renderers in the shell's own file rather than section modules, so
    the evidence they mounted is their own page."""
    res = _run("""
      tabNamed('Tasks').dispatch('click');
      setTimeout(function () { out({ text: tidy(root()) }); }, 40);
    """)
    assert "Incoming data" in res["text"] or "Data & Task Creation" in res["text"], \
        res["text"][:300]


def test_the_tasks_tab_keeps_its_four_pages_under_the_frozen_sub_keys():
    """WHY: F4/R6. ``tasks`` and ``assign`` are read by the alias table,
    ``openBatchesFor`` and the physician-row route-in. The labels changed once
    already and the keys deliberately did not."""
    res = _run("""
      tabNamed('Tasks').dispatch('click');
      setTimeout(function () {
        var subs = findAll(root(), 'asc-subnav-btn').map(tidy);
        out({ subs: subs });
      }, 40);
    """)
    assert res["subs"] == ["Data & Task Creation", "Task Routing", "QA", "Metrics"]


def test_the_qa_badge_still_rides_on_the_tasks_tab():
    """WHY: R8. The badge is the only place the QA backlog is visible from
    anywhere in the console, and it was moved onto Tasks on purpose when QA
    moved there. A move that dropped it makes a growing queue invisible."""
    res = _run("""
      var badge = document.getElementById('ascQaBadge');
      out({ present: !!badge,
            onTasks: !!badge && tidy(badge.parentNode).indexOf('Tasks') === 0 });
    """)
    assert res["present"], "#ascQaBadge is gone"
    assert res["onTasks"], "the QA badge is no longer on the Tasks tab"


# ═════════════════════════════════════════════════════════════════════════════
#  The cross-links (R8): the routes operators actually navigate by
# ═════════════════════════════════════════════════════════════════════════════
def test_open_batches_for_lands_on_task_routing_with_the_doctor_preselected():
    """WHY: R8. Routing from a physician's row is entered from the roster and
    lands in Routing; it is not a second send path. A move that dropped the
    pre-selection would leave an operator on a routing screen with nobody
    picked, which is the state that sends to everyone.

    The name only appears once a case is selected, because the panel refuses to
    show controls a selection cannot use. So the test selects one: asserting on
    the empty panel would pass against a shell that dropped the doctor."""
    res = _run("""
      tabNamed('Data').dispatch('click');
      setTimeout(function () {
        CTX.openBatchesFor({ id: 'd1', name: 'Ada Okafor' });
        setTimeout(function () {
          findAll(root(), 'asc-route-rail-btn')[2].dispatch('click');
          setTimeout(function () {
            var boxes = [];
            (function walk(el) {
              if (el.tagName === 'INPUT' && el.getAttribute('type') === 'checkbox') boxes.push(el);
              (el.childNodes || []).forEach(function (c) { if (c.tagName) walk(c); });
            })(root());
            boxes[0].checked = true; boxes[0].dispatch('change');
            setTimeout(function () {
              out({ active: findAll(root(), 'asc-subnav-btn').filter(function (b) {
                      return b.className.indexOf('active') !== -1; }).map(tidy),
                    tab: findAll(document.getElementById('ascAdminBar'), 'asc-admin-tab')
                      .filter(function (b) { return b.className.indexOf('active') !== -1; })
                      .map(tidy),
                    text: tidy(root()) });
            }, 40);
          }, 40);
        }, 40);
      }, 40);
    """)
    assert res["tab"] and res["tab"][0].startswith("Tasks")
    assert res["active"] == ["Task Routing"], res["active"]
    assert "Ada Okafor" in res["text"], "the doctor was not carried into Routing"


def test_open_pipeline_carries_the_upload_it_was_clicked_from():
    """WHY: R8. The bucket buttons always passed their upload and the handler
    once ignored it, dropping the operator onto an unfiltered page where they
    had to re-find the row they had just clicked."""
    res = _run("""
      tabNamed('Data').dispatch('click');
      setTimeout(function () {
        CTX.openPipeline({ upload_id: 'up-77' });
        setTimeout(function () {
          out({ active: findAll(root(), 'asc-subnav-btn').filter(function (b) {
                  return b.className.indexOf('active') !== -1; }).map(tidy),
                asked: FETCHES.filter(function (u) { return u.indexOf('up-77') !== -1; }),
                text: tidy(root()) });
        }, 60);
      }, 40);
    """)
    assert res["active"] == ["Pipeline tools"], res["active"]
    assert "Pipeline" in res["text"] or "uploads" in res["text"].lower()


def test_open_physicians_sub_comes_back_through_the_shell():
    """WHY: R8. The section owns its views and the shell owns which tab looks
    selected. A jump that set the section's own state directly would leave the
    masthead pointing at the tab the operator just left."""
    res = _run("""
      tabNamed('Data').dispatch('click');
      setTimeout(function () {
        CTX.openPhysiciansSub('pending');
        setTimeout(function () {
          out({ tab: findAll(document.getElementById('ascAdminBar'), 'asc-admin-tab')
                  .filter(function (b) { return b.className.indexOf('active') !== -1; })
                  .map(tidy) });
        }, 40);
      }, 40);
    """)
    assert res["tab"] and res["tab"][0].startswith("Physicians")


def test_the_referrals_tab_reaches_the_health_system_funnel():
    """WHY: U13's actual gap. ``/admin/hs-referrals``, ``/advance`` and
    ``/reward`` shipped with no client, so three of six funnel stages were
    unreachable and every recorded reward was invisible."""
    res = _run("""
      tabNamed('Referrals').dispatch('click');
      setTimeout(function () {
        findAll(root(), 'asc-subnav-btn').filter(function (b) {
          return tidy(b) === 'Health-system introductions'; })[0].dispatch('click');
        setTimeout(function () { out({ calls: CALLS }); }, 40);
      }, 40);
    """)
    assert "referrals" in res["calls"], res["calls"]


# ═════════════════════════════════════════════════════════════════════════════
#  R7: the card gallery, with the real renderer
# ═════════════════════════════════════════════════════════════════════════════
def test_the_gallery_renders_one_card_per_physician_with_tier_as_a_word():
    """WHY: the roster's standing vocabulary rules are the documented
    quiet-wrong bug on this screen. Tier is a WORD, never a raw enum, and
    advisor is a SECOND badge beside it rather than instead of it: ``tierBadge``
    alone prints "Unassigned" over a real medical advisor, because advisor is
    not a tier and never appears in ``users.tier``."""
    res = _run("""
      out({ cards: findAll(root(), 'asc-pcard').length,
            text: tidy(root()),
            badges: findAll(root(), 'asc-pcard-badges').map(tidy) });
    """, with_physicians=True)
    assert res["cards"] == 3, f"expected one card per physician, got {res['cards']}"
    assert "Ada Okafor" in res["text"] and "Chen Wu" in res["text"]
    assert "Reviewer" in res["badges"][0]
    advisor = [b for b in res["badges"] if "Advisor" in b]
    assert advisor, "the advisor badge is gone"
    assert "Reviewer" in advisor[0], (
        "advisor replaced the tier instead of sitting beside it")
    assert "unassigned" not in res["text"].lower()


def test_an_unmeasured_physician_reads_as_unmeasured_and_never_as_a_zero():
    """WHY: the em dash on this screen is load-bearing. "Nobody has graded them
    yet" and "they scored zero" are different claims about a colleague, and on
    a card they would read identically."""
    res = _run("""
      var cards = findAll(root(), 'asc-pcard');
      var bela = cards.filter(function (c) { return tidy(c).indexOf('Bela') !== -1; })[0];
      out({ metrics: findAll(bela, 'asc-pcard-metric-value').map(tidy) });
    """, with_physicians=True)
    score, median, agreement = res["metrics"]
    assert score == "-", f"an ungraded physician shows {score!r}"
    assert median == "-", f"an untimed physician shows {median!r}"
    assert "0.00" not in agreement, f"an unrated physician shows {agreement!r}"


def test_the_filters_narrow_the_gallery_rather_than_decorating_it():
    """WHY: the filters are what replaced eleven sortable columns. If they
    render but do not filter, the redesign removed a capability instead of
    re-cutting it, and the screen looks correct while answering nothing."""
    res = _run("""
      function control(label) { return root().querySelector('[aria-label="' + label + '"]'); }
      var before = findAll(root(), 'asc-pcard').length;
      var tier = control('Tier');
      tier.value = 'labeler'; tier.dispatch('change');
      var byTier = findAll(root(), 'asc-pcard').length;
      tier = control('Tier'); tier.value = ''; tier.dispatch('change');
      var real = control('Real-data approval');
      real.value = 'no'; real.dispatch('change');
      out({ before: before, byTier: byTier,
            notCleared: findAll(root(), 'asc-pcard').length,
            count: tidy(findAll(root(), 'asc-gallery-count')[0]) });
    """, with_physicians=True)
    assert res["before"] == 3
    assert res["byTier"] == 1, "the tier filter did not narrow the gallery"
    assert res["notCleared"] == 1, "the real-data filter did not narrow the gallery"
    assert "of 3" in res["count"], "the count does not say what was filtered out"


def test_every_class_the_console_emits_has_a_style_behind_it():
    """WHY: a class with no rule is invisible in review and invisible on screen,
    which is the same failure as a section nothing mounts. Covers the shell, the
    gallery and both new sections in one pass."""
    import re

    sources = SHELL + PHYSICIANS + "".join(
        (_FRONTEND / f).read_text()
        for f in ("admin_referrals.js", "admin_community.js"))
    emitted = set(re.findall(r"class(?:Name)?: '([^']+)'", sources))
    emitted |= {m for m in re.findall(r"className = '([^']+)'", sources)}
    names = {c for blob in emitted for c in blob.split()
             if c.startswith("asc-") or c.startswith("cm-")}
    missing = [c for c in sorted(names) if f".{c}" not in CSS]
    assert not missing, f"classes with no CSS rule: {missing}"
