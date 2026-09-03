"""Welcome package v2 — the §6 suite.

Five groups, in the PRD's own order:

    css      the brace-balance guard that makes §0's bug a build failure, and
             the containment rule that stops the next one
    model    required vs optional, monotonic done, rewritable defer, migration
    cadence  sessions_seen × state → walkthrough | reentry | banner | none
    gate     /tasks/next refuses real work while a required stop is open, and
             the tutorial is exempt
    ui       the re-entry page's inverted buttons, Esc, tab order; the banner

§0's bug — one unclosed brace that nested 69 walkthrough rules inside a
``max-width: 1100px`` media query, so the whole walkthrough was styled on a
split screen and raw ``<ul>`` on a full one — was invisible to every test in this
repo because CSS does not fail loudly. It parses, it applies to nothing, and it
looks fine in the one window size the author had open. The first two tests here
are the ten lines of Python §0 asks for, and they are the reason that class of
bug is now a red shard rather than a screenshot somebody happens to take.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asclepius import first_run as fr  # noqa: E402
from asclepius.schemas import FIRST_RUN_STOPS  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from main import app  # noqa: E402
from tests._asclepius import fresh_store, headers_for, make_user  # noqa: E402

from test_first_run_dom import _ctx, _run_node  # noqa: E402

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
_CSS = _FRONTEND / "asclepius.css"
_FIRST_RUN_JS = _FRONTEND / "first_run.js"
_PORTAL_JS = _FRONTEND / "asclepius.js"

_VERSION = 1


# ═════════════════════════════════════════════════════════════════════════════
# css
# ═════════════════════════════════════════════════════════════════════════════

def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def test_the_stylesheet_closes_every_brace_it_opens():
    """§0: brace depth is zero at EOF, and never negative on the way there.

    Depth 1 at EOF is what shipped: ``@media (max-width: 1100px) {`` for the Task
    Routing grid was never closed, so every rule after it — the entire
    walkthrough, the checklist card, the letter typography, the choice cards —
    applied only below 1100px. A physician on a full screen got unstyled markup
    and nobody noticed, because a stylesheet with a missing brace is still a
    valid stylesheet.

    Negative depth is checked too: a stray closing brace is the same bug wearing
    the opposite sign, and it silently un-nests everything after it.
    """
    src = _strip_comments(_CSS.read_text(encoding="utf-8"))
    depth = 0
    line = 1
    for ch in src:
        if ch == "\n":
            line += 1
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            assert depth >= 0, f"asclepius.css:{line} closes a brace that was never opened"
    assert depth == 0, (
        f"asclepius.css does not close {depth} block(s) it opened — every rule "
        f"after the unclosed one is nested inside it and applies only where it does")


def _rules_with_selector(css: str, needle: str):
    """Every rule whose selector mentions ``needle``, with its enclosing at-rules.

    A hand-rolled walk rather than a CSS parser: this file has no build step and
    no node_modules to lean on, and the guard has to run in the CI shard exactly
    as the rest of the suite does.
    """
    src = _strip_comments(css)
    stack, buf, found = [], "", []
    for ch in src:
        if ch == "{":
            stack.append(buf.strip())
            buf = ""
            if needle in stack[-1]:
                found.append((stack[-1], [x for x in stack[:-1] if x.startswith("@")]))
        elif ch == "}":
            if stack:
                stack.pop()
            buf = ""
        else:
            buf += ch
    return found


def test_no_walkthrough_rule_is_trapped_in_a_foreign_media_query():
    """§0/§4.4: the walkthrough's rules live at the top level, and the ONE media
    query allowed to contain them is the walkthrough's own single-column collapse.

    This is the containment half of the guard. Balanced braces alone would not
    have caught §0's bug if the missing ``}`` had been somewhere that still
    balanced by EOF — what actually broke the product was walkthrough rules
    ending up inside somebody else's breakpoint.
    """
    css = _CSS.read_text(encoding="utf-8")
    # WIDTH queries only. A `prefers-reduced-motion` block cannot hide a rule at
    # some screen sizes and not others — it is scoped to a preference, applies at
    # every width, and only ever switches motion off. Policing it would force the
    # walkthrough to choose between honouring §3's reduced-motion rule and passing
    # this guard, which is not a trade worth making to widen a net that is aimed
    # at exactly one failure: styling that silently applies at some widths only.
    def widths(media):
        return tuple(m for m in media if "width" in m)

    nested = [(sel, widths(media)) for sel, media in _rules_with_selector(css, ".asc-fr-")]
    nested = [(sel, media) for sel, media in nested if media]
    assert nested, (
        "the walkthrough should still declare its own collapse breakpoint; if it "
        "genuinely has none, delete this assertion rather than the guard below")
    declared = {media for _, media in nested}
    assert declared == {("@media (max-width: 900px)",)}, (
        "a .asc-fr- rule is nested inside a width media query the walkthrough did "
        f"not declare for itself: {sorted(declared)}")


def test_the_walkthrough_stage_is_two_columns_wide_and_one_column_narrow():
    """§6: the stage grid collapses at the breakpoint and not before.

    Asserted against the declared rules rather than a headless browser — there is
    no jsdom in this repo's test dependencies, and a test that skips when a
    dependency is absent is a test that never runs in CI. The property that
    matters is the same one either way: two columns is the base rule, one column
    is the breakpoint rule, and neither is nested anywhere unexpected.
    """
    css = _strip_comments(_CSS.read_text(encoding="utf-8"))
    base = re.search(r"\.asc-fr-stage\s*\{([^}]*)\}", css)
    assert base, ".asc-fr-stage has no base rule at all"
    assert "grid-template-columns" in base.group(1)
    assert "280px" in base.group(1), (
        "the base stage rule should still be the stop plus the checklist rail")
    collapse = re.search(
        r"@media \(max-width: 900px\) \{[^@]*?\.asc-fr-stage \{([^}]*)\}", css, re.S)
    assert collapse, "the walkthrough declares no single-column collapse"
    assert "minmax(0, 1fr)" in collapse.group(1) and "280px" not in collapse.group(1)


def test_the_reentry_page_and_banner_classes_are_all_styled():
    """§4.4 names the new classes; none of them may ship unstyled."""
    css = _CSS.read_text(encoding="utf-8")
    for cls in ("asc-fr-reentry", "asc-fr-reentry-row", "asc-fr-banner",
                "asc-fr-banner-dots", "asc-fr-eyebrow", "asc-fr-later"):
        assert re.search(r"\." + cls + r"[\s,{:.]", css), f"{cls} is emitted but never styled"


def test_motion_respects_prefers_reduced_motion():
    """§3: 150–200ms, and nothing moves for a physician who asked it not to."""
    css = _CSS.read_text(encoding="utf-8")
    # There is more than one such block in this stylesheet; find the walkthrough's
    # rather than whichever comes first.
    blocks = [b for b in re.findall(
        r"@media \(prefers-reduced-motion: reduce\) \{(.*?)\n\}", css, re.S)]
    assert blocks, "the stylesheet declares no reduced-motion block at all"
    walkthrough = [b for b in blocks if ".asc-fr-" in b]
    assert walkthrough, "the walkthrough's own surfaces do not honour reduced motion"
    assert all("transition: none" in b for b in walkthrough)
    # And the motion it DOES use stays inside §3's 150-200ms envelope.
    for ms in re.findall(r"\.asc-fr-[^{}]*\{[^{}]*?transition:[^;]*?(\d+)ms", css, re.S):
        assert 150 <= int(ms) <= 200, f"{ms}ms is outside §3's 150-200ms range"


def test_both_frontend_modules_parse():
    """§6's ``node --check``. A syntax error in either file is a blank portal."""
    node = subprocess.run(["which", "node"], capture_output=True, text=True)
    if node.returncode != 0:
        pytest.skip("node is not installed in this environment")
    for path in (_FIRST_RUN_JS, _PORTAL_JS):
        proc = subprocess.run(["node", "--check", str(path)],
                              capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, f"{path.name} does not parse:\n{proc.stderr}"


# ═════════════════════════════════════════════════════════════════════════════
# model
# ═════════════════════════════════════════════════════════════════════════════

def test_the_required_optional_split_covers_the_six_declared_stops():
    """A stop in neither list would be unreachable by every rule in this PRD."""
    assert set(fr.REQUIRED_STOPS) | set(fr.OPTIONAL_STOPS) == set(FIRST_RUN_STOPS)
    assert not set(fr.REQUIRED_STOPS) & set(fr.OPTIONAL_STOPS)
    assert fr.REQUIRED_STOPS == ("welcome", "start", "practice")


@pytest.mark.parametrize("stop", fr.REQUIRED_STOPS)
def test_a_required_skip_migrates_to_not_done(stop):
    """§1: "for required stops, skipped → null (they must actually do it —
    today's data shows real accounts with practice skipped, which the product
    should not have allowed)."."""
    out = fr.normalize_stops({stop: "skipped"})
    assert stop not in out


@pytest.mark.parametrize("stop", fr.OPTIONAL_STOPS)
def test_an_optional_skip_migrates_to_deferred(stop):
    """§1: an optional skip was a real answer; it becomes "asked, declined"."""
    assert fr.normalize_stops({stop: "skipped"}) == {stop: fr.DEFERRED}


def test_done_survives_the_migration_untouched():
    """The migration must never un-finish work somebody actually did."""
    all_done = {s: "done" for s in FIRST_RUN_STOPS}
    assert fr.normalize_stops(all_done) == all_done


def test_an_unrecognised_outcome_is_not_treated_as_finished_work():
    """Deny by default, exactly as the practice gate does: an outcome nobody can
    name must not silently count as a completed stop."""
    assert fr.normalize_stops({"practice": "banana"}) == {}
    assert fr.normalize_stops({"manual": "banana"}) == {"manual": fr.DEFERRED}
    assert fr.normalize_stops("not a dict") == {}
    assert fr.normalize_stops(None) == {}


def test_sessions_seen_backfills_to_one():
    """§1. Zero would read as "this is your first login" for someone returning."""
    for raw in (None, 0, -4, "nonsense", {}):
        assert fr.normalize({"sessions_seen": raw}, version=_VERSION)["sessions_seen"] == 1
    assert fr.normalize({"sessions_seen": 7}, version=_VERSION)["sessions_seen"] == 7


def test_deferred_stops_never_complete_the_checklist():
    """§1: "completed_at is set when all six are done. Deferred stops never
    complete it."

    And the stale stamp is CLEARED. The old model wrote completed_at as soon as
    every stop carried any outcome, so accounts that skipped all three optional
    stops are stored as complete right now. Trusting that stamp would silence the
    re-entry cadence for exactly the physicians it was written for.
    """
    stale = {
        "version": _VERSION,
        "stops": {s: "done" for s in fr.REQUIRED_STOPS},
        "completed_at": "2026-01-01T00:00:00Z",
    }
    stale["stops"].update({s: "skipped" for s in fr.OPTIONAL_STOPS})
    out = fr.normalize(stale, version=_VERSION)
    assert out["completed_at"] is None
    assert all(out["stops"][s] == fr.DEFERRED for s in fr.OPTIONAL_STOPS)

    real = dict(stale, stops={s: "done" for s in FIRST_RUN_STOPS})
    assert fr.normalize(real, version=_VERSION)["completed_at"] == "2026-01-01T00:00:00Z"


def test_normalize_never_raises_on_junk():
    """A bad read costs one extra walkthrough; a raise costs a physician who
    cannot open the portal. Same trade every other reader in this codebase makes.
    """
    for junk in (None, [], "", 0, {"stops": 7}, {"stops": {"welcome": 3}},
                 {"version": "x", "stops": None, "sessions_seen": []}):
        state = fr.normalize(junk, version=_VERSION)
        assert state["version"] == _VERSION
        assert isinstance(state["stops"], dict)
        assert state["sessions_seen"] >= 1


# ═════════════════════════════════════════════════════════════════════════════
# cadence — parametrized on sessions_seen × state, per §6
# ═════════════════════════════════════════════════════════════════════════════

_REQUIRED_DONE = {s: "done" for s in fr.REQUIRED_STOPS}
_ALL_DONE = {s: "done" for s in FIRST_RUN_STOPS}


@pytest.mark.parametrize("sessions", [1, 2, 3, 4, 12, 400])
def test_required_unfinished_is_always_the_walkthrough(sessions):
    """§6: "required unfinished → walkthrough regardless of sessions_seen".

    The cadence never gives up on the three stops that are the product. A
    physician on their fortieth login who still has not done the practice case
    meets the walkthrough, not a banner suggesting it.
    """
    for stops in ({}, {"welcome": "done"}, {"welcome": "done", "start": "done"}):
        state = {"stops": stops, "sessions_seen": sessions}
        assert fr.mode(state) == fr.MODE_WALKTHROUGH, (stops, sessions)


@pytest.mark.parametrize("sessions,expected", [
    (1, fr.MODE_REENTRY),
    (2, fr.MODE_REENTRY),
    (3, fr.MODE_REENTRY),
    (4, fr.MODE_BANNER),
    (5, fr.MODE_BANNER),
    (99, fr.MODE_BANNER),
])
def test_the_reentry_page_is_offered_twice_and_then_goes_quiet(sessions, expected):
    """§2/§3: "ask twice, then go quiet". Sessions 2 and 3 get the page; from 4
    there is a banner and nothing in the way.

    Session 1 is in the table as reentry for completeness, but a physician on
    login 1 with the required stops done is mid-walkthrough — the module carries
    them from stop to stop without re-consulting this function, and the shell
    only asks it at boot.
    """
    state = {"stops": dict(_REQUIRED_DONE), "sessions_seen": sessions}
    assert fr.mode(state) == expected


@pytest.mark.parametrize("sessions", [1, 2, 3, 4, 99])
def test_nothing_is_shown_once_every_stop_is_done(sessions):
    assert fr.mode({"stops": _ALL_DONE, "sessions_seen": sessions}) == fr.MODE_NONE


def test_a_deferred_optional_stop_still_counts_as_remaining():
    """The bug §1 exists to fix: under the old model a skip closed the stop, so
    after one round of skips ``shouldRun`` was false forever and the walkthrough
    never returned."""
    stops = dict(_REQUIRED_DONE, **{s: "deferred" for s in fr.OPTIONAL_STOPS})
    assert fr.optional_remaining(stops) == fr.OPTIONAL_STOPS
    assert fr.mode({"stops": stops, "sessions_seen": 2}) == fr.MODE_REENTRY
    assert fr.mode({"stops": stops, "sessions_seen": 9}) == fr.MODE_BANNER


def test_a_dismissed_account_is_never_dropped_back_into_onboarding():
    """The migration-safety rule, and the reason it is checked FIRST.

    The store's one-time backfill stamped every already-approved account with
    ``dismissed_at`` and an EMPTY stops map — that is how physicians who had been
    labeling for months were kept out of "Welcome to Archangel Health". Those rows
    have three required stops open and always will. Testing the required stops
    ahead of the stamp would undo that migration and drop the entire existing
    roster into an onboarding they finished long before it was written.
    """
    backfilled = {"stops": {}, "sessions_seen": 1, "dismissed_at": "2026-01-01T00:00:00Z"}
    assert fr.mode(backfilled) == fr.MODE_NONE
    # Still true many logins later, and still true with the optional stops open.
    assert fr.mode(dict(backfilled, sessions_seen=50)) == fr.MODE_NONE
    with_optional_open = dict(backfilled, stops={s: "deferred" for s in fr.OPTIONAL_STOPS})
    assert fr.mode(with_optional_open) == fr.MODE_NONE


def test_the_python_and_javascript_cadence_agree():
    """§2 says "one function, one place", and it lives in two languages because
    the shell has to choose a screen before it can ask anybody.

    So the two are checked against each other over the whole cross product rather
    than trusted to stay in step — a drift here is a physician seeing a different
    screen from the one the server gated them for.
    """
    cases = []
    stop_sets = [
        {},
        {"welcome": "done"},
        {"welcome": "done", "start": "done"},
        dict(_REQUIRED_DONE),
        dict(_REQUIRED_DONE, community="deferred"),
        dict(_REQUIRED_DONE, community="done", earnings="deferred", manual="deferred"),
        dict(_ALL_DONE),
        {"welcome": "done", "start": "skipped", "practice": "done", "manual": "skipped"},
    ]
    for stops in stop_sets:
        for sessions in (1, 2, 3, 4, 10):
            for dismissed in (None, "2026-01-01T00:00:00Z"):
                state = {"version": _VERSION, "stops": stops,
                         "sessions_seen": sessions, "dismissed_at": dismissed}
                cases.append({"state": state, "python": fr.mode(state)})

    out = _run_node(_ctx() + """
      var CASES = %s;
      var W = window.FirstRunWalkthrough;
      console.log(JSON.stringify({
        js: CASES.map(function (c) { return W.mode({ first_run: c.state }); }),
      }));
    """ % json.dumps(cases))
    for case, js in zip(cases, out["js"]):
        assert case["python"] == js, (
            f"python says {case['python']!r} and javascript says {js!r} for "
            f"{case['state']!r}")


# ═════════════════════════════════════════════════════════════════════════════
# ui — the re-entry page (§4.2) and the banner (§4.3)
# ═════════════════════════════════════════════════════════════════════════════

def _reentry_user(sessions: int = 2, stops: dict | None = None) -> dict:
    return {"role": "evaluator",
            "first_run": {"version": _VERSION,
                          "stops": stops if stops is not None else dict(_REQUIRED_DONE),
                          "sessions_seen": sessions}}


def test_the_reentry_pages_primary_is_leaving_not_finishing():
    """§4.2: "Deliberately inverted from the walkthrough: on re-entry the default
    is leaving."

    One primary, and it is "Go to my cases". "Finish these now" is the secondary.
    Never two filled buttons (§3, "one primary per screen").
    """
    out = _run_node(_ctx(user=_reentry_user()) + """
      window.FirstRunWalkthrough.reentry(ctx);
      done(function () {
        console.log(JSON.stringify({
          primaries: find(rootNode, 'asc-btn-primary').map(textOf).map(function (t) { return t.trim(); }),
          secondaries: find(rootNode, 'asc-fr-skip').map(textOf).map(function (t) { return t.trim(); }),
          rows: find(rootNode, 'asc-fr-reentry-row').map(function (r) { return textOf(r).trim(); }),
          title: find(rootNode, 'asc-fr-title').map(textOf),
          closes: find(rootNode, 'asc-fr-demo-close').length,
          checklists: find(rootNode, 'asc-fr-checklist').length,
        }));
      });
    """)
    assert len(out["primaries"]) == 1
    assert "Go to my cases" in out["primaries"][0]
    assert out["secondaries"] == ["Finish these now"]
    assert "Finish your onboarding" in " ".join(out["title"])
    # One row per remaining optional stop, each with an honest time estimate.
    assert len(out["rows"]) == 3
    assert any("2 min" in r for r in out["rows"])
    # §4.2: no close ✕ — the primary IS the close. And no checklist rail: this is
    # a short interstitial, not the walkthrough.
    assert out["closes"] == 0
    assert out["checklists"] == 0


def test_the_reentry_primary_comes_first_in_tab_order():
    """§3, keyboard first: Tab lands on "Go to my cases" and Enter leaves."""
    out = _run_node(_ctx(user=_reentry_user()) + """
      window.FirstRunWalkthrough.reentry(ctx);
      done(function () {
        var actions = find(rootNode, 'asc-fr-actions')[0];
        console.log(JSON.stringify({
          order: (actions.childNodes || []).filter(function (c) { return c.tagName === 'BUTTON'; })
                   .map(function (b) { return textOf(b).trim(); }),
        }));
      });
    """)
    assert "Go to my cases" in out["order"][0], (
        "the primary must precede the secondary in the DOM, or Tab reaches "
        "'Finish these now' first and Enter re-enrols a physician who was leaving")


def test_leaving_the_reentry_page_defers_everything_in_one_request():
    """§4.2: "Leaving writes deferred on every remaining optional stop."

    ONE request, not three. Three PATCHes would each read the stored blob, each
    write their own stop, and the last one home would erase the other two — so
    leaving would reliably record one stop deferred out of three.
    """
    out = _run_node(_ctx(user=_reentry_user()) + """
      window.FirstRunWalkthrough.reentry(ctx);
      done(function () {
        find(rootNode, 'asc-btn-primary')[0].dispatch('click');
        done(function () {
          console.log(JSON.stringify({
            calls: apiCalls.filter(function (c) { return c.path === '/me/first-run'; }),
            handoffs: handoffs,
          }));
        });
      });
    """)
    assert out["calls"] == [{"path": "/me/first-run", "method": "PATCH",
                            "body": {"action": "defer_all"}}]
    assert out["handoffs"] == ["exit"], "the primary goes straight to the dashboard"


def test_escape_leaves_the_reentry_page():
    """§3: "the re-entry page's skip is reachable by Tab → Enter; Esc also skips."."""
    out = _run_node(_ctx(user=_reentry_user()) + """
      window.FirstRunWalkthrough.reentry(ctx);
      done(function () {
        document.dispatch('keydown', { key: 'Escape' });
        done(function () {
          console.log(JSON.stringify({
            calls: apiCalls.filter(function (c) { return c.path === '/me/first-run'; }),
            handoffs: handoffs,
          }));
        });
      });
    """)
    assert out["handoffs"] == ["exit"]
    assert out["calls"] == [{"path": "/me/first-run", "method": "PATCH",
                            "body": {"action": "defer_all"}}]


def test_escape_stops_working_once_the_reentry_page_is_left():
    """The re-entry page's Esc handler is DOCUMENT-level, so it must not outlive
    the screen it belongs to.

    Navigating away through the rail replaces the screen without telling the
    module. A handler left behind would turn a stray Esc on the dashboard into
    "defer everything and bounce them somewhere they did not ask to go" — a
    silent write, triggered by a key that means cancel.
    """
    out = _run_node(_ctx(user=_reentry_user()) + """
      window.FirstRunWalkthrough.reentry(ctx);
      done(function () {
        window.FirstRunWalkthrough.teardown();     // what the shell does on navigation
        document.dispatch('keydown', { key: 'Escape' });
        done(function () {
          console.log(JSON.stringify({
            calls: apiCalls.filter(function (c) { return c.path === '/me/first-run'; }),
            handoffs: handoffs,
          }));
        });
      });
    """)
    assert out["calls"] == [], "a torn-down page still wrote to the server"
    assert out["handoffs"] == [], "a torn-down page still navigated"


def test_the_shell_tears_the_walkthrough_down_on_every_navigation():
    """The other half of the rule above, on the shell's side: `teardown` was
    previously never called at all, so nothing dropped those handlers."""
    js = _PORTAL_JS.read_text(encoding="utf-8")
    assert "function teardownFirstRun()" in js
    assert "window.FirstRunWalkthrough.teardown()" in js
    setpanel = js[js.index("function setPanel(dest)"):]
    setpanel = setpanel[:setpanel.index("if (dest === 'community')")]
    assert "teardownFirstRun();" in setpanel, "the rail can navigate away untorn-down"
    ctx_fn = js[js.index("function firstRunCtx()"):]
    ctx_fn = ctx_fn[:ctx_fn.index("\n  }")]
    assert "teardownFirstRun();" in ctx_fn, "exit can leave handlers behind"


def test_a_reentry_row_opens_that_one_stop_and_comes_back():
    """§4.2: "Do it → opens that single stop and returns here."

    Not "and re-enrols them in the rest of the walkthrough". A physician who
    clicked one row asked for one thing.
    """
    out = _run_node(_ctx(user=_reentry_user()) + """
      window.FirstRunWalkthrough.reentry(ctx);
      done(function () {
        find(rootNode, 'asc-fr-reentry-row')[0].dispatch('click');   // community
        done(function () {
          var atStop = textOf(rootNode);
          find(rootNode, 'asc-btn-primary')[0].dispatch('click');    // "Open the community"
          done(function () {
            console.log(JSON.stringify({
              atStop: atStop,
              back: find(rootNode, 'asc-fr-reentry-row').map(function (r) { return textOf(r).trim(); }),
              handoffs: handoffs,
            }));
          });
        });
      });
    """)
    assert "working alongside" in out["atStop"], "the row did not open the community stop"
    assert "community" in out["handoffs"]
    # Back on the re-entry page, with the finished stop gone from the list.
    assert len(out["back"]) == 2
    assert not any("community" in r.lower() for r in out["back"])


def test_finish_these_now_runs_the_remaining_stops_in_order():
    """§4.2: the secondary "starts the remaining optional stops in order".

    In order, and only the ones remaining. A physician who did the community
    months ago and deferred the other two must not be walked back through the
    community stop before reaching the ones they asked for — and each stop must
    advance to the next rather than bouncing back to this page, which is what
    the row-level "Do it →" does and is a different request.
    """
    stops = dict(_REQUIRED_DONE, community="done", earnings="deferred", manual="deferred")
    out = _run_node(_ctx(user=_reentry_user(stops=stops)) + """
      window.FirstRunWalkthrough.reentry(ctx);
      done(function () {
        find(rootNode, 'asc-fr-skip')[0].dispatch('click');   // "Finish these now"
        done(function () {
          var first = textOf(rootNode);
          find(rootNode, 'asc-fr-skip')[0].dispatch('click'); // defer it, advance
          done(function () {
            console.log(JSON.stringify({ first: first, second: textOf(rootNode) }));
          });
        });
      });
    """)
    assert "How you get paid." in out["first"], (
        "it should open earnings, not replay the community stop that is done")
    assert "lives in the manual" in out["second"], (
        "each stop should advance to the next, not bounce back to the page")


def test_the_reentry_page_is_skipped_entirely_when_nothing_is_left():
    """An empty box with a button that says "go to my cases" is a worse way of
    going to their cases than going to their cases."""
    out = _run_node(_ctx(user=_reentry_user(stops=dict(_ALL_DONE))) + """
      window.FirstRunWalkthrough.reentry(ctx);
      done(function () {
        console.log(JSON.stringify({ handoffs: handoffs }));
      });
    """)
    assert out["handoffs"] == ["exit"]


def test_the_required_stops_render_no_skip_control():
    """§4.1, asserted across all three rather than on the one that had a button.

    Stop 3 hands straight to the tutorial and has no chrome of its own, so it is
    covered by the model rule (a required stop cannot be deferred) rather than by
    counting buttons.
    """
    for stops, expect_skips in (
            ({}, 0),                                        # welcome
            ({"welcome": "done"}, 0),                       # choose your start
    ):
        out = _run_node(_ctx(user={"first_run": {"version": _VERSION, "stops": stops}}) + """
          window.FirstRunWalkthrough.start(ctx);
          done(function () { done(function () {
            console.log(JSON.stringify({ skips: find(rootNode, 'asc-fr-skip').length }));
          }); });
        """)
        assert out["skips"] == expect_skips, stops


def test_every_optional_stop_offers_do_this_later_and_writes_deferred():
    """§4.1: "Skip for now" becomes "Do this later", and it writes `deferred`."""
    for stop, prior in (
            ("community", dict(_REQUIRED_DONE)),
            ("earnings", dict(_REQUIRED_DONE, community="done")),
            ("manual", dict(_REQUIRED_DONE, community="done", earnings="done")),
    ):
        out = _run_node(_ctx(user={"first_run": {"version": _VERSION, "stops": prior}}) + """
          window.FirstRunWalkthrough.start(ctx);
          done(function () { done(function () {
            var skip = find(rootNode, 'asc-fr-skip')[0];
            var label = textOf(skip).trim();
            skip.dispatch('click');
            console.log(JSON.stringify({
              label: label,
              calls: apiCalls.filter(function (c) { return c.path === '/me/first-run'; }),
            }));
          }); });
        """)
        assert out["label"] == "Do this later", stop
        assert out["calls"][-1]["body"] == {"action": "defer", "stop": stop}


def test_the_banner_replaces_the_chip_on_the_dashboard_only():
    """§4.3. The banner and the chip are two volumes of one door, never both."""
    js = _PORTAL_JS.read_text(encoding="utf-8")
    assert "firstRunMode() === 'banner' ? firstRunBannerEl() : firstRunChipEl()" in js
    # The banner is built only in the dashboard renderer; the chip is the general
    # surface and keeps its own guard.
    assert js.count("firstRunBannerEl()") == 2, "one definition, one call site"
    assert "if (!firstRunPending()) return null;" in js


def test_the_banner_is_passive_and_not_dismissible():
    """§4.3: "Not dismissible. It's 56px tall and it goes away by finishing."

    And it is a region, not an alert: it must never steal focus or interrupt a
    screen reader mid-sentence. It is the "quiet" §3 asks for.
    """
    js = _PORTAL_JS.read_text(encoding="utf-8")
    body = js[js.index("function firstRunBannerEl()"):]
    body = body[:body.index("\n  /** \"Finish setup")]
    assert "role: 'region'" in body
    assert "aria-label" in body
    assert "dismiss" not in body.lower(), "the banner must offer no dismissal"
    assert "'Finish onboarding'" in body
    assert "openFirstRunReentry()" in body, (
        "the banner and the re-entry page must be the same flow at two volumes")
    # Six dots and a tabular count, not a percentage.
    assert "asc-fr-banner-dot" in body
    assert "' of ' + p.total" in body
    css = _CSS.read_text(encoding="utf-8")
    banner_css = css[css.index(".asc-fr-banner {"):]
    banner_css = banner_css[:banner_css.index("\n}")]
    assert "min-height: 56px" in banner_css


# ═════════════════════════════════════════════════════════════════════════════
# gate — §5's server-side enforcement, and the lockouts it must not cause
# ═════════════════════════════════════════════════════════════════════════════

def _walkthrough_user(store, **stops):
    """A physician who is demonstrably mid-walkthrough.

    The `welcome` stop is recorded because that is the walkthrough's very first
    screen — an account carrying no first-run state at all has never been asked
    to do one, and the gate deliberately does not bite there (see the lockout
    tests below).
    """
    user = make_user(store)
    state = store.get_first_run(user["id"])
    state["stops"] = dict({"welcome": "done"}, **stops)
    store.set_first_run(user["id"], state)
    return user


def test_tasks_next_refuses_real_work_while_a_required_stop_is_open():
    """§5: 409 `first_run_incomplete`, with the two things left to do."""
    store = fresh_store()
    user = _walkthrough_user(store)          # start + practice still open
    c = TestClient(app)
    r = c.get("/api/asclepius/tasks/next", headers=headers_for(user))
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "first_run_incomplete"
    assert detail["remaining"] == ["start", "practice"]
    # A physician meeting this has done nothing wrong and there is exactly one
    # thing for them to do next, so the refusal carries it — same shape as the
    # practice gate's 403 rather than prose they have to interpret.
    assert detail["action"]["kind"] == "resume_first_run"
    assert r.headers.get("X-Asclepius-First-Run") == "incomplete"


def test_tasks_next_opens_once_the_required_stops_are_done():
    """§2's promise: "after the practice case ... never more than one click from
    Start new case". The optional stops gate nothing."""
    store = fresh_store()
    user = _walkthrough_user(store, start="done", practice="done",
                             community="deferred", earnings="deferred",
                             manual="deferred")
    c = TestClient(app)
    r = c.get("/api/asclepius/tasks/next", headers=headers_for(user))
    assert r.status_code != 409, r.text


def test_the_tutorial_is_exempt_from_the_first_run_gate():
    """§5: "The tutorial endpoint is exempt."

    It must be: the practice case is one of the required stops, so a gate that
    covered it would refuse the physician the one thing that opens the gate.
    """
    store = fresh_store()
    user = _walkthrough_user(store)
    c = TestClient(app)
    r = c.patch("/api/asclepius/me/tutorial", json={"action": "start"},
                headers=headers_for(user))
    assert r.status_code == 200, r.text
    r = c.get("/api/asclepius/tutorial/case", headers=headers_for(user))
    assert r.status_code != 409, r.text


def test_the_gate_never_locks_out_an_account_with_no_walkthrough_state():
    """The lockout this gate is shaped to avoid, part one.

    An account with no first-run state has never been asked to do a walkthrough,
    so there is nothing it can be failing: accounts provisioned outside the
    portal, accounts whose practice gate was opened by an admin or a migration,
    and every fixture in this suite. All are real working physicians by every
    other measure the product has, and a literal reading of §5 would take real
    cases away from all of them at once.
    """
    store = fresh_store()
    user = make_user(store)                  # practice gate open, first_run empty
    assert store.get_first_run(user["id"])["stops"] == {}
    c = TestClient(app)
    r = c.get("/api/asclepius/tasks/next", headers=headers_for(user))
    assert r.status_code != 409, r.text


def test_the_gate_never_locks_out_a_backfilled_veteran():
    """The lockout this gate is shaped to avoid, part two.

    The store's one-time backfill stamped every already-approved account with
    `dismissed_at` and an empty stops map — that is how physicians who had been
    labeling for months were kept out of "Welcome to Archangel Health". Those
    rows have three required stops open and always will.
    """
    store = fresh_store()
    user = make_user(store)
    state = store.get_first_run(user["id"])
    state["dismissed_at"] = "2026-01-01T00:00:00Z"
    state["stops"] = {"welcome": "done"}
    store.set_first_run(user["id"], state)
    c = TestClient(app)
    r = c.get("/api/asclepius/tasks/next", headers=headers_for(user))
    assert r.status_code != 409, r.text


def test_the_gate_never_locks_out_a_grandfathered_account():
    """`grandfathered` is this codebase's existing word for "predates the
    requirement". Re-gating those accounts would undo the migration that let
    them keep working."""
    store = fresh_store()
    user = make_user(store)
    tut = store.get_tutorial_state(user["id"])
    tut["gate"] = {"state": "grandfathered"}
    store.set_tutorial_state(user["id"], tut)
    state = store.get_first_run(user["id"])
    state["stops"] = {"welcome": "done"}
    store.set_first_run(user["id"], state)
    c = TestClient(app)
    r = c.get("/api/asclepius/tasks/next", headers=headers_for(user))
    assert r.status_code != 409, r.text


# ═════════════════════════════════════════════════════════════════════════════
# cadence — the clock itself
# ═════════════════════════════════════════════════════════════════════════════

def test_sessions_seen_increments_once_per_login_not_once_per_reload():
    """§5: "guard with a per-session flag so a reload doesn't double-count".

    Keyed on the token's `jti`. Without it the clock runs at the speed of page
    loads and every physician is past the re-entry page before they have seen it
    twice — which is the entire cadence, gone, from one missing guard.
    """
    store = fresh_store()
    user = make_user(store)
    c = TestClient(app)

    first = headers_for(user)                       # one login → one token
    for _ in range(5):                              # a reload, a second tab, a re-paint
        r = c.get("/api/asclepius/auth/me", headers=first)
        assert r.status_code == 200, r.text
    assert r.json()["first_run"]["sessions_seen"] == 1, (
        "the FIRST session this clock observes is session one, not two — a "
        "brand-new account must not meet the re-entry page a login early")

    second = headers_for(user)                      # a genuine new sign-in
    r = c.get("/api/asclepius/auth/me", headers=second)
    assert r.json()["first_run"]["sessions_seen"] == 2

    third = headers_for(user)
    c.get("/api/asclepius/auth/me", headers=third)
    r = c.get("/api/asclepius/auth/me", headers=third)
    assert r.json()["first_run"]["sessions_seen"] == 3


def test_auth_me_returns_the_stops_and_the_cadence_clock():
    """§5: "GET /auth/me returns first_run with sessions_seen and per-stop
    outcomes" — and nothing the portal has no use for."""
    store = fresh_store()
    user = make_user(store)
    c = TestClient(app)
    fr_payload = c.get("/api/asclepius/auth/me", headers=headers_for(user)).json()["first_run"]
    assert set(fr_payload) == {"version", "stops", "sessions_seen",
                               "completed_at", "dismissed_at"}
    # The idempotency key is a token id. Nothing on screen reads it, so it does
    # not ride in a payload returned on every request.
    assert "last_session_counted" not in fr_payload


def test_the_session_clock_does_not_run_for_non_physicians():
    """Admins and QA reviewers never see any of these screens; counting sessions
    for accounts that can never read the count is a write per request for
    nothing."""
    store = fresh_store()
    admin = make_user(store, role="admin")
    c = TestClient(app)
    for _ in range(3):
        c.get("/api/asclepius/auth/me", headers=headers_for(admin))
    assert store.get_first_run(admin["id"])["last_session_counted"] is None
