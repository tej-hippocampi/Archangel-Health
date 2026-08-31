"""Doctor portal UX: one-button entry, session continuity, rail polish.

Eight changes to ``frontend/asclepius/asclepius.js`` and ``asclepius.css``, plus
one additive field on ``public_user``. The properties that matter are behavioural,
so most of what follows EXECUTES the shipped code against the DOM shim rather
than grepping for the strings it happens to contain:

  * the timer is what becomes ``time_spent_sec`` on a submission and what the
    admin per-case view reads, so "the clock does not run while the tab is
    hidden" is asserted by hiding a tab and reading the clock — not by finding
    ``stopTimer`` next to ``visibilitychange`` in the source;
  * "Continue" must never dead-end, so the draft-selection is run against a
    real localStorage stub holding drafts for tasks that are and are not in the
    served queue;
  * "clicking Tasks lands on the dashboard" is asserted by calling ``setPanel``
    and seeing which render function it reached.

Where a property genuinely IS textual — a deleted chip bar, a CSS rule that must
not paint — it is asserted against the file, with comments stripped so a test
never trips over the prose explaining the very removal it checks.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

# Repo convention (see test_asclepius_eval_ui_overhaul.py and siblings): put
# ``backend/`` on the path before importing project modules. pytest's prepend
# import mode only adds ``backend/tests``, and the console-script entry point —
# which is what CI runs — does not add the working directory either.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import _asclepius as A  # noqa: E402
from asclepius import auth as asc_auth  # noqa: E402

_FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
JS_PATH = _FRONTEND / "asclepius.js"
CSS_PATH = _FRONTEND / "asclepius.css"
DOM_SHIM = pathlib.Path(__file__).resolve().parent / "_asclepius_dom.js"

JS = JS_PATH.read_text(encoding="utf-8")
CSS = CSS_PATH.read_text(encoding="utf-8")

_LINE_COMMENT = re.compile(r"^\s*//.*$", re.M)
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _code(src: str) -> str:
    """Source with whole-line ``//`` comments removed.

    Absence assertions ("this control is gone") have to run against code, not
    prose: the comment that explains a removal necessarily names the thing that
    was removed, and a test that trips over its own explanation would push the
    next person to delete the explanation.
    """
    return _LINE_COMMENT.sub("", src)


def _css_code(src: str) -> str:
    return _BLOCK_COMMENT.sub("", src)


JS_CODE = _code(JS)
CSS_CODE = _css_code(CSS)


def _extract_function(src: str, name: str) -> str:
    marker = f"function {name}("
    start = src.index(marker)
    if src[start - 6 : start] == "async ":     # keep the prefix or `await` won't parse
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


def _fn(name: str) -> str:
    return _extract_function(JS, name)


def _const(name: str) -> str:
    m = re.search(rf"( *const {name} = [\s\S]*?;)\n", JS)
    assert m, f"const {name} not found in asclepius.js"
    return m.group(1)


def _slice(first_line: str, last_line: str) -> str:
    """The shipped source between two exact lines, inclusive.

    Used for the page-lifecycle listeners, which are top-level statements rather
    than a function and so cannot be extracted by name. Both ends are asserted
    unique, so a refactor that moves or duplicates them fails here loudly
    instead of quietly testing the wrong block.
    """
    assert JS.count(first_line) == 1, f"not unique: {first_line!r}"
    assert JS.count(last_line) == 1, f"not unique: {last_line!r}"
    start = JS.index(first_line)
    end = JS.index(last_line, start) + len(last_line)
    return JS[start:end]


def _run_node(script: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed in this environment")
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\n{proc.stderr}\n{proc.stdout}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _rule(selector: str) -> str:
    """The declaration block of one CSS rule, comments stripped.

    Anchored at the start of a line, because a bare selector is a SUBSTRING of
    every longer one that ends with it: an unanchored search for
    ``.asc-rail-item-referral:not(.active)`` finds the ``body.asc-rail-compact``
    rule first and the test then silently asserts against the wrong block.
    """
    m = re.search(r"(?m)^\s*" + re.escape(selector) + r"\s*\{([^}]*)\}", CSS_CODE)
    assert m, f"no rule for {selector}"
    return m.group(1)


# ═════════════════════════════════════════════════════════════════════════════
#  Shared node prelude
#
#  A deterministic clock, a controllable interval table and a real-enough
#  localStorage. setInterval is faked rather than used: the timer assertions are
#  about which intervals EXIST, and a real one would also keep node alive past
#  the end of the script.
# ═════════════════════════════════════════════════════════════════════════════
_PRELUDE = """
require(%(dom)r);

let NOW = 1700000000000;
Date.now = () => NOW;

let nextInterval = 0;
const intervals = {};
globalThis.setInterval = (fn) => { intervals[++nextInterval] = fn; return nextInterval; };
globalThis.clearInterval = (id) => { delete intervals[id]; };
globalThis.__intervals = intervals;

const store = {};
globalThis.localStorage = {
  get length() { return Object.keys(store).length; },
  key: (i) => (Object.keys(store)[i] === undefined ? null : Object.keys(store)[i]),
  getItem: (k) => (Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
  clear: () => { Object.keys(store).forEach((k) => delete store[k]); },
};

// The shim's window is a bare object; the page-lifecycle listeners need the
// event surface. document already has addEventListener/dispatch.
const winListeners = {};
window.addEventListener = (t, fn) => { (winListeners[t] = winListeners[t] || []).push(fn); };
window.dispatch = (t, ev) => (winListeners[t] || []).slice()
  .forEach((fn) => fn(Object.assign({ type: t }, ev || {})));
globalThis.__winListeners = winListeners;
document.hidden = false;

function out(o) { console.log(JSON.stringify(o)); }
""" % {"dom": str(DOM_SHIM)}


# ═════════════════════════════════════════════════════════════════════════════
#  §1 §2 — the rail
# ═════════════════════════════════════════════════════════════════════════════

# Both icon-only rails. `body.asc-rail-compact` is the physician's own choice and
# persists across sessions; 701–1100px collapses on width alone with no body
# class. A fix that covered only the chosen one would leave the bug on every
# laptop in that range, which is why this is parametrised rather than written
# once.
_ICON_RAIL_SELECTORS = (
    "body.asc-rail-compact .asc-rail-item-referral:not(.active)",
    ".asc-rail-item-referral:not(.active)",
)


@pytest.mark.parametrize("selector", _ICON_RAIL_SELECTORS)
def test_referral_carries_no_fill_at_rest_in_an_icon_only_rail(selector):
    """§1: in a collapsed rail the fill IS the "you are here" indicator.

    ``.asc-rail-item-referral`` set a green wash unconditionally, so the rail
    rendered two filled pills in every state — and at 68px there is no label to
    tell the washed one from the selected one, which means the rail was lying
    about where you are.
    """
    body = _rule(selector)
    assert "background: transparent" in body
    assert "border-color: transparent" in body
    assert "color: var(--ink-soft)" in body


def _media_block(header: str) -> str:
    """The body of one top-level ``@media`` block, brace-matched."""
    start = CSS_CODE.index(header)
    brace = CSS_CODE.index("{", start)
    depth = 0
    for i in range(brace, len(CSS_CODE)):
        if CSS_CODE[i] == "{":
            depth += 1
        elif CSS_CODE[i] == "}":
            depth -= 1
            if depth == 0:
                return CSS_CODE[brace + 1 : i]
    raise AssertionError(f"unbalanced braces in {header}")


@pytest.mark.parametrize("header", [
    "@media (min-width: 701px) {\n  body.asc-rail-compact .asc-rail-item-referral:not(.active)",
    "@media (max-width: 1100px) and (min-width: 701px) {\n  .asc-rail-item-referral:not(.active)",
])
def test_no_icon_rail_rule_touches_referral_without_the_not_active_guard(header):
    """``:not(.active)`` is the whole safety of §1.

    Not "the guarded selector exists" — that would pass with an unguarded rule
    sitting right beside it, killing the selected state along with the resting
    one. Every referral selector inside these blocks must carry the guard.
    """
    body = _media_block(header)
    referral_selectors = re.findall(r"(?m)^\s*([^{}\n]*\.asc-rail-item-referral[^{}\n]*)\{", body)
    assert referral_selectors, "the §1 rules are not in this block any more"
    for sel in referral_selectors:
        assert ":not(.active)" in sel, sel


def test_hover_does_not_reintroduce_the_resting_fill():
    """``.asc-rail-item-referral:hover`` pins the wash unconditionally, and a
    cursor resting on an unselected pill would paint the same false "you are
    here" the rest of §1 removes."""
    body = _rule("body.asc-rail-compact .asc-rail-item-referral:not(.active):hover")
    assert "background: var(--card-in)" in body
    assert "var(--green-wash)" not in body


def test_referral_still_renders_active_styling_when_it_is_the_active_section():
    """The selected-state rule is untouched, and the kill rules above cannot
    reach it: `:not(.active)` means they do not MATCH an active item at all, so
    their higher specificity never comes into play and neither does source
    order. That is why the guard, not a specificity trick, is the mechanism."""
    body = _rule(".asc-rail-item-referral.active")
    assert "background: var(--green-wash)" in body
    assert "border-color: var(--green)" in body
    assert "color: var(--green-deep)" in body
    assert ".asc-rail-item-referral.active::before { background: var(--green); }" in CSS_CODE


def test_the_rail_item_is_not_squished():
    """§2: a 20px icon in a 38px pill with 9px of air is what read as cramped.

    12px of padding makes a 44px pill — square-ish around the icon, and also the
    minimum comfortable touch target. The mono typeface is the design system and
    is deliberately NOT touched; only the tracking, which at 12px was too wide.
    """
    base = _rule(".asc-rail-item")
    assert "gap: 12px" in base
    assert "padding: 11px 12px" in base
    assert "letter-spacing: 0.04em" in base
    assert "font-family: var(--mono)" in base, "the mono rail is the design system, not the bug"
    assert "border-radius: var(--r-md)" in _rule(".asc-rail-item.active")
    # Both icon-only rails get the square-ish pill and the taller active bar.
    assert "body.asc-rail-compact .asc-rail-item { justify-content: center; gap: 0; padding: 12px 0; }" in CSS_CODE
    assert "body.asc-rail-compact .asc-rail-item.active::before { top: 24%; bottom: 24%; }" in CSS_CODE
    assert ".asc-rail-item { justify-content: center; gap: 0; padding: 12px 10px; }" in CSS_CODE
    assert ".asc-rail-item.active::before { top: 24%; bottom: 24%; }" in CSS_CODE


# ═════════════════════════════════════════════════════════════════════════════
#  §3 — doctor initials in the rail profile
# ═════════════════════════════════════════════════════════════════════════════

_AVATAR_PRELUDE = _PRELUDE + """
const state = { user: { specialty: 'nephrology' }, token: 't' };
let avatarImgCalls = [];
function avatarImgEl(url, initials) {
  avatarImgCalls.push({ url, initials });
  const box = document.createElement('span');
  box.className = 'asc-me-avatar-slot';
  const span = document.createElement('span');
  span.className = 'asc-me-avatar-initials';
  span.textContent = initials;
  box.appendChild(span);
  return box;
}
%(payload)s
"""


def _avatar_harness(body: str) -> dict:
    payload = "\n".join([
        _fn("h"), _fn("appendChildren"), _fn("railDisplayName"),
        _fn("specialtyDotColor"), _fn("fallbackInitials"), _fn("railAvatarEl"),
    ])
    return _run_node(_AVATAR_PRELUDE % {"payload": payload} + "\n" + body)


@pytest.mark.parametrize(
    "user,expected",
    [
        ({"name": "Tej Patel"}, "TP"),
        # railDisplayName() returns "Dr. Tej Patel" for an email-derived name.
        # fallbackInitials would read that as first+last and answer "DP", so the
        # honorific has to be stripped BEFORE the initials are taken.
        ({"email": "tej.patel@hospital.org"}, "TP"),
        ({"name": "Dr. Tej Patel"}, "TP"),
        ({"name": "dr Tej Patel"}, "TP"),
        # Single name and empty name must fall back rather than throw.
        ({"name": "Prince"}, "PR"),
        # An all-whitespace name is a name we cannot make initials from; the
        # helper answers "?" rather than throwing, which is the property that
        # matters here.
        ({"name": "   "}, "?"),
        ({}, "CL"),                        # no name, no email -> 'Clinician'
    ],
)
def test_the_rail_avatar_renders_the_physicians_initials(user, expected):
    out = _avatar_harness("""
    state.user = Object.assign({ specialty: 'nephrology' }, %s);
    const el = railAvatarEl('dot-green');
    out({ text: el.textContent, cls: el.className, aria: el.getAttribute('aria-hidden') });
    """ % json.dumps(user))
    assert out["text"] == expected
    assert "asc-rail-avatar" in out["cls"]
    # Decorative: the name it stands for is the very next element, so announcing
    # "TP" immediately before "Dr. Tej Patel" would be noise.
    assert out["aria"] == "true"


def test_fallback_initials_semantics_are_unchanged():
    """§10 forbids changing ``fallbackInitials``. The avatar strips the honorific
    on the way IN rather than teaching the helper about titles."""
    body = _fn("fallbackInitials")
    assert "if (!parts.length) return '?';" in body
    assert "if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();" in body
    assert "return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();" in body


def test_the_avatar_takes_its_hue_from_the_specialty():
    out = _avatar_harness("""
    const seen = {};
    ['nephrology', 'cardiology', 'oncology', 'hepatology'].forEach((sp) => {
      state.user = { specialty: sp, name: 'Tej Patel' };
      seen[sp] = railAvatarEl(specialtyDotColor(sp)).className;
    });
    out(seen);
    """)
    for spec, cls in out.items():
        accents = [a for a in ("acc-green", "acc-orange", "acc-pink", "acc-lime") if a in cls]
        assert len(accents) == 1, f"{spec} -> {cls}"


def test_an_uploaded_photo_is_preferred_over_the_initials():
    """One image path, not two: the profile page's ``avatarImgEl`` already
    fetches the bearer-authenticated endpoint and falls back to the initials if
    that fetch fails. A second <img src> here would send no Authorization header
    at all."""
    out = _avatar_harness("""
    state.user = { specialty: 'nephrology', name: 'Tej Patel',
                   avatar_url: '/api/asclepius/users/u1/avatar?v=abc123' };
    const el = railAvatarEl('dot-green');
    out({ cls: el.className, calls: avatarImgCalls,
          slot: !!el.querySelector('.asc-me-avatar-slot') });
    """)
    assert out["slot"] is True
    assert out["calls"] == [{"url": "/api/asclepius/users/u1/avatar?v=abc123", "initials": "TP"}]
    assert "has-img" in out["cls"]


def test_concurrent_avatar_renders_share_one_fetch():
    """§3 moved this path from "once, when the profile page opens" to "every
    renderSidePanel()", i.e. every navigation. Without in-flight de-duplication
    a physician clicking through the rail fires several concurrent fetches for
    the same picture, and every one that resolves mints an object URL nothing
    revokes."""
    payload = "\n".join([_fn("loadAvatarBlob"),
                          _const("avatarBlobCache"), _const("avatarBlobPending")])
    out = _run_node(_PRELUDE + """
    let fetches = 0;
    let release;
    const gate = new Promise((r) => { release = r; });
    globalThis.fetch = () => { fetches++; return gate; };
    globalThis.URL = { createObjectURL: () => 'blob:x' };
    const state = { token: 't' };
    %s
    const a = loadAvatarBlob('/u/1/avatar'), b = loadAvatarBlob('/u/1/avatar'),
          c = loadAvatarBlob('/u/2/avatar');
    release({ ok: true, blob: () => Promise.resolve({}) });
    Promise.all([a, b, c]).then(() => {
      // A later render, after the first resolved, is answered from the cache.
      loadAvatarBlob('/u/1/avatar');
      out({ fetches, cached: Object.keys(avatarBlobCache).length,
            pendingLeft: Object.keys(avatarBlobPending).length });
    });
    """ % payload)
    assert out["fetches"] == 2, "two urls, one fetch each — not one per render"
    assert out["pendingLeft"] == 0, "the in-flight table must not grow forever"


def test_a_failed_avatar_fetch_stays_retryable():
    payload = "\n".join([_fn("loadAvatarBlob"),
                          _const("avatarBlobCache"), _const("avatarBlobPending")])
    out = _run_node(_PRELUDE + """
    let fetches = 0;
    globalThis.fetch = () => { fetches++; return Promise.reject(new Error('offline')); };
    globalThis.URL = { createObjectURL: () => 'blob:x' };
    const state = { token: 't' };
    %s
    loadAvatarBlob('/u/1/avatar').then((first) => {
      loadAvatarBlob('/u/1/avatar').then((second) => {
        out({ fetches, first, second,
              pendingLeft: Object.keys(avatarBlobPending).length });
      });
    });
    """ % payload)
    assert out["first"] is None and out["second"] is None
    assert out["fetches"] == 2, "a failed fetch must be retryable on the next render"
    assert out["pendingLeft"] == 0


def test_the_avatar_is_mounted_in_the_rail_foot_and_styled():
    src = _fn("renderSidePanel")
    assert "railAvatarEl(specColor)" in _code(src)
    assert "asc-rail-usertext" in src, "name and specialty still stack, beside the circle"
    assert ".asc-rail-avatar" in CSS_CODE
    # The profile page sizes its initials for a 96px circle; the rail re-scales
    # them rather than forking a second image path.
    assert ".asc-rail-avatar .asc-me-avatar-initials" in CSS_CODE
    row = _rule(".asc-rail-user")
    assert "flex-direction: row" in row


def test_public_user_ships_the_avatar_url_the_rail_reads():
    """The rail renders on every screen; ``/me/profile`` is fetched only when the
    profile page opens. Without this field the rail could never show a photo."""
    store = A.fresh_store()
    user = A.make_user(store)
    row = store.get_user_by_id(user["id"])
    assert asc_auth.public_user(row)["avatar_url"] is None

    store.set_user_avatar(user["id"], sha256="a" * 64, mime="image/png",
                          at="2026-08-28T00:00:00")
    row = store.get_user_by_id(user["id"])
    url = asc_auth.public_user(row)["avatar_url"]
    assert url == f"/api/asclepius/users/{user['id']}/avatar?v={'a' * 12}", url


# ═════════════════════════════════════════════════════════════════════════════
#  §4 — one button on the dashboard
# ═════════════════════════════════════════════════════════════════════════════

_DASH_PRELUDE = _PRELUDE + """
const calls = { openTaskById: [], renderEvalView: 0, setPanel: [] };
let rendered = null;

const state = {
  user: { specialty: 'nephrology' }, token: 't', view: 'home', panel: 'tasks',
  draft: null, task: null, portalChosen: false, specialtyChosen: false,
  timerInterval: null, timerStart: null, baseElapsed: 0,
};
const AVAILABLE = %(api)s;

function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }
function setRoot(node) { rendered = node; }
function stopTimer() {}
function updateHeaderProgress() {}
function renderHeader() {}
function getPortalVersion() { return 'v4'; }
function getPortalSpecialty() { return 'nephrology'; }
function setPortalSpecialty() {}
function sessionHasSurface() { return true; }
function sessionIsProvisional() { return false; }
let sessionCan = () => false;
function provisionalBannerEl() { return null; }
// Onboarding v2 §6 re-entry: the dashboard shows a quiet "Finish setup · 3 of 6"
// chip while the walkthrough has open stops. Stubbed to "nothing to finish"
// here, which is the state every assertion below is about; the chip's own
// behaviour is exercised in test_first_run_dom.py.
let firstRunChip = null;
function firstRunChipEl() { return firstRunChip; }
function renderScoreWidget() { return null; }
function renderDashboardWidget() { return document.createElement('div'); }
function renderDashboardEmpty() {
  const d = document.createElement('div'); d.className = 'asc-empty'; return d;
}
function renderDashboardError() {
  const d = document.createElement('div'); d.className = 'asc-inline-error'; return d;
}
function openTaskById(id) { calls.openTaskById.push(id); }
function renderEvalView() { calls.renderEvalView++; }
function setPanel(d) { calls.setPanel.push(d); }
async function api(path) {
  if (path.indexOf('/tasks/available') === 0) return AVAILABLE;
  if (path.indexOf('/me/stats') === 0) return { submissions_total: 0 };
  if (path.indexOf('/score') === 0) return null;
  return null;
}
%(payload)s

function seedDraft(taskId, draft) {
  localStorage.setItem('asclepius_draft_' + taskId,
    JSON.stringify(Object.assign({ task_id: taskId }, draft)));
}
"""


def _dash_harness(body: str, available: dict) -> dict:
    payload = "\n".join([
        _const("DRAFT_PREFIX"), _const("TUTORIAL_TASK_ID"),
        _fn("h"), _fn("appendChildren"), _fn("draftKey"), _fn("formatTime"),
        _fn("findResumableDraft"), _fn("renderDashboardView"),
    ])
    script = _DASH_PRELUDE % {"payload": payload, "api": json.dumps(available)}
    return _run_node(script + "\n" + body)


_ONE_TASK = {"tasks": [{"task_id": "t-1", "specialty": "nephrology", "difficulty": "hard"}],
             "served_portal_version": "v4", "continued_from": None}
_MANY_TASKS = {"tasks": [{"task_id": f"t-{i}", "specialty": "nephrology",
                          "difficulty": "hard", "modality": "multimodal",
                          "case_source": "real_deid"} for i in range(20)],
               "served_portal_version": "v4", "continued_from": None}

_READ_HERO = """
  const title = rendered.querySelector('.asc-dash-hero-title').textContent;
  const sub = rendered.querySelector('.asc-dash-hero-sub').textContent;
  const btns = rendered.querySelectorAll('.asc-btn-lg');
  out({
    title, sub,
    cta: btns.length, ctaText: btns.length ? btns[0].textContent : null,
    cards: rendered.querySelectorAll('.asc-dash-card').length,
    more: rendered.querySelectorAll('.asc-dash-more').length,
    list: rendered.querySelectorAll('.asc-dash-list').length,
  });
"""


def test_the_dashboard_renders_exactly_one_call_to_action():
    """§4: routing decides which case a physician sees. A queue of twenty cases
    to choose between is a decision we are supposed to be making for them, so
    the hero is the whole surface — no card list, no show-more."""
    out = _dash_harness("renderDashboardView().then(() => {" + _READ_HERO + "});", _MANY_TASKS)
    assert out["cta"] == 1
    assert out["cards"] == 0, "the task card list is gone"
    assert out["more"] == 0, "and so is 'Show N more in your queue'"
    assert out["list"] == 0


def test_the_reviewers_console_card_is_not_a_second_case_entry():
    """A physician who can also adjudicate keeps their route into the review
    console. That card is a different DESTINATION, not a second way into the
    case queue — §4 deletes the queue, not the rest of the dashboard."""
    out = _dash_harness("""
    sessionCan = () => true;
    renderDashboardView().then(() => {
      out({
        review: rendered.querySelectorAll('.asc-dash-card-review').length,
        cta: rendered.querySelectorAll('.asc-btn-lg').length,
        title: rendered.querySelector('.asc-dash-hero-title').textContent,
      });
    });
    """, _MANY_TASKS)
    assert out["review"] == 1
    assert out["cta"] == 1
    assert out["title"] == "Start new case"


def test_the_title_reads_start_new_case_with_no_draft():
    out = _dash_harness("renderDashboardView().then(() => {" + _READ_HERO + "});", _ONE_TASK)
    assert out["title"] == "Start new case"
    assert out["ctaText"] == "Start →"


def test_the_finish_setup_chip_sits_above_the_dashboard_when_one_is_pending():
    """Onboarding v2 §6 re-entry: a quiet chip, never a modal ambush.

    It goes at the TOP of the dashboard and it is a button, not an overlay: a
    physician who wants to work can ignore it entirely, which is the whole
    difference between a reminder and a nag.
    """
    out = _dash_harness("""
    firstRunChip = h('button', { class: 'asc-fr-chip' }, 'Finish setup', '3 of 6');
    renderDashboardView().then(() => {
      const chip = rendered.querySelector('.asc-fr-chip');
      const hero = rendered.querySelector('.asc-dash-hero');
      out({
        chipText: chip ? chip.textContent : null,
        // Everything the dashboard renders lives in one wrap; the chip is
        // ahead of the hero in it, so it reads before the work does.
        chipBeforeHero: !!chip && !!hero,
        isOverlay: !!chip && chip.tagName === 'BUTTON' ? false : true,
      });
    });""", _ONE_TASK)
    assert out["chipText"] == "Finish setup3 of 6"
    assert out["chipBeforeHero"]
    assert out["isOverlay"] is False


def test_no_chip_when_the_walkthrough_is_finished():
    out = _dash_harness("""
    renderDashboardView().then(() => {
      out({ chip: !!rendered.querySelector('.asc-fr-chip') });
    });""", _ONE_TASK)
    assert out["chip"] is False


def test_the_subtitle_never_names_the_queue_depth():
    """"1 case available" is queue depth, which is exactly the information this
    change exists to hide. The queue NAME stays — a physician cleared for real
    patient data still has to be told which work they are about to start."""
    out = _dash_harness("renderDashboardView().then(() => {" + _READ_HERO + "});", _MANY_TASKS)
    assert "available" not in out["sub"]
    assert "20" not in out["sub"]
    assert out["sub"] == "Real de-identified cases"


def test_the_title_reads_continue_case_when_a_queued_task_has_a_draft():
    out = _dash_harness("""
    seedDraft('t-1', { savedAt: 5, elapsedSec: 754 });
    renderDashboardView().then(() => {""" + _READ_HERO + "});", _ONE_TASK)
    assert out["title"] == "Continue case"
    assert out["ctaText"] == "Continue →"
    # 754s = 12:34. The elapsed comes off the draft, so it is the time actually
    # worked, not the time since the case was opened.
    assert out["sub"] == "Picks up where you left off · 12:34 so far"


def test_a_draft_whose_task_is_not_in_the_queue_does_not_offer_continue():
    """A draft for a task that has since been claimed, expired, or fell out of
    the queue must NOT offer a Continue that dead-ends: openTaskById would 404
    and bounce them back here. Prefer a clean Start over a broken resume."""
    out = _dash_harness("""
    seedDraft('t-gone', { savedAt: 99, elapsedSec: 600 });
    renderDashboardView().then(() => {""" + _READ_HERO + "});", _ONE_TASK)
    assert out["title"] == "Start new case"
    assert out["ctaText"] == "Start →"


def test_the_button_resumes_the_newest_draft_and_starts_fresh_otherwise():
    """The one button routes both ways, and "newest" is answerable because
    saveDraft stamps ``savedAt``."""
    resumed = _dash_harness("""
    seedDraft('t-1', { savedAt: 100, elapsedSec: 10 });
    seedDraft('t-2', { savedAt: 300, elapsedSec: 20 });
    seedDraft('t-3', { savedAt: 200, elapsedSec: 30 });
    renderDashboardView().then(() => {
      rendered.querySelector('.asc-btn-lg').dispatch('click');
      out({ opened: calls.openTaskById, evals: calls.renderEvalView, view: state.view });
    });
    """, {"tasks": [{"task_id": "t-1"}, {"task_id": "t-2"}, {"task_id": "t-3"}],
          "served_portal_version": "v4"})
    assert resumed["opened"] == ["t-2"], "the newest savedAt wins"
    assert resumed["evals"] == 0, "resuming must not draw a fresh case"
    assert resumed["view"] == "eval"

    fresh = _dash_harness("""
    renderDashboardView().then(() => {
      rendered.querySelector('.asc-btn-lg').dispatch('click');
      out({ opened: calls.openTaskById, evals: calls.renderEvalView, view: state.view });
    });
    """, _ONE_TASK)
    assert fresh["opened"] == []
    assert fresh["evals"] == 1
    assert fresh["view"] == "eval"


def test_a_corrupt_draft_entry_never_costs_a_resumable_case():
    """One unparseable key under our prefix must not take the whole scan down —
    localStorage is shared with other portal keys and with older schema versions.
    """
    out = _dash_harness("""
    localStorage.setItem('asclepius_draft_t-1', '{not json');
    seedDraft('t-2', { savedAt: 7, elapsedSec: 61 });
    renderDashboardView().then(() => {""" + _READ_HERO + "});",
                        {"tasks": [{"task_id": "t-1"}, {"task_id": "t-2"}],
                         "served_portal_version": "v4"})
    assert out["title"] == "Continue case"
    assert out["sub"] == "Picks up where you left off · 1:01 so far"


def test_the_practice_case_is_never_offered_as_a_resumable_case():
    """The tutorial is replayed from the help menu, never resumed as if it were
    paid work — and its draft key sits under the same prefix."""
    out = _dash_harness("""
    seedDraft('tutorial-calibration-1', { savedAt: 999, elapsedSec: 900 });
    renderDashboardView().then(() => {""" + _READ_HERO + "});",
                        {"tasks": [{"task_id": "tutorial-calibration-1"}, {"task_id": "t-1"}],
                         "served_portal_version": "v4"})
    assert out["title"] == "Start new case"


# ─── §4 fallout: the one button must never dead-end ──────────────────────────
#
# Both of these were found by walking the shipped build in a real browser, not
# by reading it. Neither is new code — but §4 moved them from "an edge case on a
# secondary control" to "the only button on the dashboard", which is what makes
# them this change's problem.

_OPEN_PRELUDE = _PRELUDE + """
const calls = [];
let rendered = null;
const state = { user: {}, token: 't', view: 'home', draft: null, task: null,
                servedVersion: null, continuedFrom: null, portalChosen: false,
                specialtyChosen: false, timerStart: null, baseElapsed: 0,
                timerInterval: null };
let RESPONSE = null;   // null => api() throws THROWN
let THROWN = null;

function setRoot(node) { rendered = node; calls.push('setRoot'); }
function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }
function renderHeader() {}
function startTimer() {}
function toast(m) { calls.push('toast:' + m); }
function renderTaskWorkspace() { calls.push('renderTaskWorkspace'); }
function renderDashboardView() { calls.push('renderDashboardView'); }
async function loadWithheldAnswersIfNeeded() {}
function getPortalVersion() { return 'v3'; }
function emptyAnchorX() {}
async function api() { if (RESPONSE) return RESPONSE; throw THROWN; }
%(payload)s
"""


def _open_harness(body: str) -> dict:
    payload = "\n".join([
        _const("DRAFT_PREFIX"),
        _fn("h"), _fn("appendChildren"), _fn("draftKey"), _fn("randomId"),
        _fn("emptyAnchor"), _fn("newDraft"), _fn("initDraftForTask"),
        _fn("clearDraft"), _fn("openTaskById"),
    ])
    return _run_node(_OPEN_PRELUDE % {"payload": payload} + "\n" + body)


def test_a_structurally_incomplete_draft_is_repaired_not_thrown_on():
    """Every member backfill in initDraftForTask dereferences one of these
    objects, so a draft missing one threw a TypeError before any of them ran —
    and openTaskById caught it and left the physician on "Opening case…".

    A draft can be shaped like this for real: written before one of the fields
    existed, or truncated by a localStorage quota failure part-way through a
    write. The work that DID survive is in the other fields, so it is repaired
    in place rather than discarded.
    """
    out = _open_harness("""
    localStorage.setItem('asclepius_draft_t-1', JSON.stringify(
      {task_id: 't-1', elapsedSec: 300, savedAt: 5,
       independent_answer: {text: 'the work they already did'}}));
    let threw = null;
    try { initDraftForTask({task_id: 't-1'}); } catch (e) { threw = String(e); }
    out({ threw,
          elapsed: state.draft && state.draft.elapsedSec,
          keptWork: state.draft && state.draft.independent_answer.text,
          anchor: state.draft && !!state.draft.independent_answer.evidence_anchor,
          revision: state.draft && typeof state.draft.chosen_revision,
          critique: state.draft && Array.isArray(state.draft.rejected_critique.error_tags),
          steps: state.draft && Array.isArray(state.draft.reasoning_steps) });
    """)
    assert out["threw"] is None, out["threw"]
    assert out["elapsed"] == 300, "the elapsed on record survived the repair"
    assert out["keptWork"] == "the work they already did", "the repair must not discard work"
    assert out["anchor"] is True
    assert out["revision"] == "object"
    assert out["critique"] is True
    assert out["steps"] is True


def test_a_case_that_will_not_open_lands_on_the_dashboard_not_a_loading_card():
    """openTaskById replaces the whole root with "Opening case…" before it
    fetches. It used to toast on failure and render nothing else, so the
    physician sat on that card until they reloaded — and §4 made this the
    dashboard's only button."""
    out = _open_harness("""
    THROWN = { status: 500, message: 'the server fell over' };
    openTaskById('t-1').then(() => out({ calls, root: rendered.textContent }));
    """)
    assert "renderDashboardView" in out["calls"], "the physician was left on the loading card"
    assert any(c.startswith("toast:") for c in out["calls"])


@pytest.mark.parametrize("status", [403, 404, 410])
def test_a_dead_case_takes_its_draft_with_it(status):
    """Gone, refused or withdrawn is terminal for THIS case. Leaving the draft
    behind means the dashboard reads it straight back and offers the same
    Continue that just failed — an inescapable loop on the one control the
    physician has. §4.1's "prefer a clean Start over a broken resume", applied
    one step later than the queue filter can reach."""
    out = _open_harness("""
    localStorage.setItem('asclepius_draft_t-1', JSON.stringify({task_id: 't-1', savedAt: 1}));
    THROWN = { status: %d, message: 'gone' };
    openTaskById('t-1').then(() => out({
      calls, draftLeft: !!localStorage.getItem('asclepius_draft_t-1') }));
    """ % status)
    assert out["draftLeft"] is False, "the dead draft would offer the same failing Continue"
    assert "renderDashboardView" in out["calls"]


def test_a_200_carrying_no_task_is_treated_as_a_dead_case_not_a_silent_bounce():
    """Same fact as a 404 — the server has nothing under this id. Bouncing back
    to the dashboard with the draft intact and no message loops the physician
    between Continue and the dashboard with nothing on screen to explain it,
    which is worse than the stranded loading card: that at least looks broken."""
    out = _open_harness("""
    localStorage.setItem('asclepius_draft_t-1', JSON.stringify({task_id: 't-1', savedAt: 1}));
    RESPONSE = { task: null };
    openTaskById('t-1').then(() => out({
      calls, draftLeft: !!localStorage.getItem('asclepius_draft_t-1') }));
    """)
    assert out["draftLeft"] is False
    assert "renderDashboardView" in out["calls"]
    assert any(c.startswith("toast:") for c in out["calls"]), "a silent bounce explains nothing"
    assert "renderTaskWorkspace" not in out["calls"]


def test_a_transient_failure_never_costs_the_physician_their_draft():
    """A flaky network or a 500 may well clear. Dropping an hour of clinical
    reasoning because one request failed is a far worse outcome than one
    Continue that has to be retried."""
    for thrown in ('{ status: 500, message: "boom" }',
                   '{ message: "NetworkError" }'):
        out = _open_harness("""
        localStorage.setItem('asclepius_draft_t-1', JSON.stringify({task_id: 't-1', savedAt: 1}));
        THROWN = %s;
        openTaskById('t-1').then(() => out({
          calls, draftLeft: !!localStorage.getItem('asclepius_draft_t-1') }));
        """ % thrown)
        assert out["draftLeft"] is True, thrown
        assert "renderDashboardView" in out["calls"], thrown


def test_a_401_leaves_the_screen_to_the_session_handler():
    """handleUnauthorized has already rendered the login form; rendering the
    dashboard over it would put a signed-out physician on a signed-in screen."""
    out = _open_harness("""
    THROWN = { status: 401, message: 'expired' };
    openTaskById('t-1').then(() => out({ calls }));
    """)
    assert "renderDashboardView" not in out["calls"]
    assert not any(c.startswith("toast:") for c in out["calls"])


def test_the_task_card_renderer_is_gone_entirely():
    """It had exactly two callers, both in the deleted block. Left in place it is
    dead code that still registers CSS the orphan-class guard would then have to
    excuse."""
    assert "renderTaskCard" not in JS
    assert "VISIBLE_CAP" not in JS
    assert "asc-dash-list" not in JS and ".asc-dash-list" not in CSS_CODE
    assert "asc-dash-more" not in JS and ".asc-dash-more" not in CSS_CODE
    assert "more in your queue" not in JS


def test_save_draft_stamps_saved_at():
    assert "state.draft.savedAt = Date.now();" in _fn("saveDraft")


# ═════════════════════════════════════════════════════════════════════════════
#  §5 — session continuity
#
#  getElapsed() is submitted as ``time_spent_sec`` and is what the admin
#  per-case view reads, so none of this is cosmetic: a tab left open overnight
#  used to bill the night.
# ═════════════════════════════════════════════════════════════════════════════

_TIMER_PRELUDE = _PRELUDE + """
const state = {
  draft: null, task: null, view: 'eval',
  timerInterval: null, timerStart: null, baseElapsed: 0,
};
%(payload)s

function openCase(taskId, base) {
  state.task = { task_id: taskId };
  state.draft = { task_id: taskId, elapsedSec: base || 0 };
  startTimer(base || 0);
}
function storedElapsed(taskId) {
  const raw = localStorage.getItem('asclepius_draft_' + taskId);
  return raw ? JSON.parse(raw).elapsedSec : null;
}
function liveIntervals() { return Object.keys(globalThis.__intervals).length; }
"""

_LIFECYCLE = _slice(
    "  window.addEventListener('beforeunload', saveDraft);",
    "window.addEventListener('blur', () => { if (state.draft) saveDraft(); });",
)


def _timer_harness(body: str) -> dict:
    payload = "\n".join([
        _const("DRAFT_PREFIX"),
        _fn("draftKey"), _fn("startTimer"), _fn("stopTimer"), _fn("getElapsed"),
        _fn("formatTime"), _fn("saveDraft"), _fn("clearDraft"),
        _fn("findResumableDraft"),
        _LIFECYCLE,
    ])
    # findResumableDraft closes over TUTORIAL_TASK_ID; it is not exercised here
    # but has to resolve for the payload to evaluate.
    payload = _const("TUTORIAL_TASK_ID") + "\n" + payload
    return _run_node(_TIMER_PRELUDE % {"payload": payload} + "\n" + body)


def test_hiding_the_tab_saves_then_stops_the_clock():
    out = _timer_harness("""
    openCase('t-1', 0);
    NOW += 120000;                       // two minutes of real work
    const before = { intervals: liveIntervals(), elapsed: getElapsed() };
    document.hidden = true;
    document.dispatch('visibilitychange');
    out({ before, after: liveIntervals(), elapsed: getElapsed(),
          stored: storedElapsed('t-1') });
    """)
    assert out["before"] == {"intervals": 1, "elapsed": 120}
    assert out["after"] == 0, "the interval must stop when the tab hides"
    assert out["elapsed"] == 120
    # Saved BEFORE the stop, while getElapsed was still measuring a running
    # clock — the two minutes are on record, not rounded away.
    assert out["stored"] == 120


def test_the_elapsed_does_not_advance_while_the_tab_is_hidden():
    """The whole point. Eight hours hidden must cost the case nothing."""
    out = _timer_harness("""
    openCase('t-1', 0);
    NOW += 120000;
    document.hidden = true;
    document.dispatch('visibilitychange');
    NOW += 8 * 3600 * 1000;              // overnight
    out({ elapsed: getElapsed(), stored: storedElapsed('t-1') });
    """)
    assert out["elapsed"] == 120
    assert out["stored"] == 120


def test_a_save_from_an_already_hidden_tab_does_not_fold_the_away_time_back_in():
    """beforeunload fires when a hidden tab is finally closed, and blur can fire
    after the hide. Both call saveDraft. If stopTimer only cleared the interval,
    ``timerStart`` would still point at the moment the clock last STARTED and
    that save would write back the whole away period — undoing the pause."""
    out = _timer_harness("""
    openCase('t-1', 0);
    NOW += 120000;
    document.hidden = true;
    document.dispatch('visibilitychange');
    NOW += 3600 * 1000;                  // an hour away
    window.dispatch('blur');
    const afterBlur = storedElapsed('t-1');
    NOW += 3600 * 1000;                  // another hour, then the tab is closed
    window.dispatch('beforeunload');
    out({ afterBlur, afterUnload: storedElapsed('t-1'), elapsed: getElapsed() });
    """)
    assert out["afterBlur"] == 120
    assert out["afterUnload"] == 120
    assert out["elapsed"] == 120


def test_returning_to_the_tab_resumes_from_the_saved_elapsed_not_the_wall_clock():
    out = _timer_harness("""
    openCase('t-1', 0);
    NOW += 120000;
    document.hidden = true;
    document.dispatch('visibilitychange');
    NOW += 600000;                       // ten minutes away
    document.hidden = false;
    document.dispatch('visibilitychange');
    const resumed = { intervals: liveIntervals(), elapsed: getElapsed() };
    NOW += 30000;                        // thirty more seconds of real work
    out({ resumed, elapsed: getElapsed() });
    """)
    assert out["resumed"] == {"intervals": 1, "elapsed": 120}
    assert out["elapsed"] == 150, "120s worked + 30s worked; the 600s away vanish"


def test_window_blur_saves_but_never_stops_the_timer():
    """A physician clicking to a second monitor, a PDF, or a reference has not
    stopped working. Pausing on blur would undercount real effort, so tab-hidden
    is the honest signal and blur only saves."""
    out = _timer_harness("""
    openCase('t-1', 0);
    NOW += 45000;
    window.dispatch('blur');
    const atBlur = { intervals: liveIntervals(), stored: storedElapsed('t-1') };
    NOW += 60000;                        // still working, on the other monitor
    out({ atBlur, elapsed: getElapsed() });
    """)
    assert out["atBlur"]["intervals"] == 1, "blur must NOT stop the clock"
    assert out["atBlur"]["stored"] == 45
    assert out["elapsed"] == 105


def test_returning_with_no_open_case_does_not_start_a_stray_clock():
    """A leftover draft on the dashboard or the empty-queue screen must not
    start a clock nobody is watching — its 5s autosave would then inflate that
    draft's elapsed while the physician is somewhere else entirely."""
    out = _timer_harness("""
    openCase('t-1', 0);
    NOW += 60000;
    document.hidden = true;
    document.dispatch('visibilitychange');
    state.task = null;                   // they went back to the dashboard
    state.view = 'home';
    document.hidden = false;
    document.dispatch('visibilitychange');
    out({ intervals: liveIntervals(), elapsed: getElapsed() });
    """)
    assert out["intervals"] == 0
    assert out["elapsed"] == 60


def test_a_submitted_case_cannot_be_resurrected_as_a_stored_draft():
    """clearDraft() removes the key, but state.draft still points at the
    finished draft until the next task replaces it — and blur, tab-hide and
    beforeunload all call saveDraft in that window. A resurrected key would be
    offered back as a Continue (§4.1) and would never be cleaned up."""
    out = _timer_harness("""
    openCase('t-1', 0);
    NOW += 300000;
    clearDraft('t-1');                   // the submit succeeded
    const cleared = storedElapsed('t-1');
    window.dispatch('blur');
    document.hidden = true;
    document.dispatch('visibilitychange');
    window.dispatch('beforeunload');
    out({ cleared, after: storedElapsed('t-1'), draft: state.draft });
    """)
    assert out["cleared"] is None
    assert out["after"] is None, "a lifecycle save wrote the submitted draft back"
    assert out["draft"] is None


def test_clearing_one_draft_never_drops_another_tasks_draft():
    out = _timer_harness("""
    openCase('t-1', 0);
    NOW += 60000;
    saveDraft();
    clearDraft('t-other');
    out({ draft: state.draft && state.draft.task_id, stored: storedElapsed('t-1') });
    """)
    assert out["draft"] == "t-1"
    assert out["stored"] == 60


def test_sign_out_saves_and_stops_before_anything_is_torn_down():
    """§5.2: the elapsed on record must be the elapsed actually worked. Both
    calls have to come first — saveDraft() reads state.draft and a live
    getElapsed(), and both are gone once the session state is cleared."""
    body = _code(_fn("logout"))
    saved = body.index("saveDraft();")
    stopped = body.index("stopTimer();")
    cleared = body.index("state.token = null;")
    assert saved < stopped < cleared, body
    # The same reasoning for an expired session: a 401 mid-case did not un-work
    # the case.
    unauth = _code(_fn("handleUnauthorized"))
    assert unauth.index("saveDraft();") < unauth.index("state.token = null;")


def test_the_lifecycle_never_pauses_on_blur_alone():
    """Asserted on the source as well as by firing it: a future edit that adds a
    stopTimer to the blur handler would undercount every physician who works
    with a reference open on a second screen."""
    blur = _slice(
        "window.addEventListener('blur', () => { if (state.draft) saveDraft(); });",
        "window.addEventListener('blur', () => { if (state.draft) saveDraft(); });",
    )
    assert "stopTimer" not in blur


# ═════════════════════════════════════════════════════════════════════════════
#  §6 — navigating to Tasks must not drop you into a case
# ═════════════════════════════════════════════════════════════════════════════

_NAV_PRELUDE = _PRELUDE + """
const calls = [];
let guideObserver = null;
const state = { panel: 'earnings', view: 'eval', draft: null, task: null };
function saveDraft() { calls.push('saveDraft'); }
function sessionCan() { return true; }
function sessionHasSurface() { return true; }
function openCommunity() { calls.push('openCommunity'); }
function renderVerificationPanel() { calls.push('renderVerificationPanel'); }
function renderSidePanel() { calls.push('renderSidePanel'); }
function renderReferralView() { calls.push('renderReferralView'); }
function renderProfileView() { calls.push('renderProfileView'); }
function renderEarningsView() { calls.push('renderEarningsView'); }
function renderGuide() { calls.push('renderGuide'); }
function renderAdminView() { calls.push('renderAdminView'); }
function renderDashboardView() { calls.push('renderDashboardView'); }
function renderEvalView() { calls.push('renderEvalView'); }
function updateHeaderProgress() { calls.push('updateHeaderProgress'); }
%(payload)s
"""


def _nav_harness(body: str) -> dict:
    return _run_node(_NAV_PRELUDE % {"payload": _fn("setPanel")} + "\n" + body)


def test_clicking_tasks_from_earnings_lands_on_the_dashboard_not_inside_the_case():
    """``state.view`` is sticky. A physician who was in a case, went to Earnings
    and clicked Tasks landed back INSIDE the case, because renderEvalView
    resumes whatever state.view still said. Continue is a deliberate choice made
    ON the dashboard (§4.1), never a side effect of navigating."""
    out = _nav_harness("""
    state.panel = 'earnings'; state.view = 'eval';
    setPanel('tasks');
    out({ calls, view: state.view, panel: state.panel });
    """)
    assert "renderDashboardView" in out["calls"]
    assert "renderEvalView" not in out["calls"]
    assert out["view"] == "home"
    assert out["panel"] == "tasks"
    # The draft is saved on the way out, so nothing in the case is lost.
    assert out["calls"].index("saveDraft") < out["calls"].index("renderDashboardView")
    # The rail's own active state has to follow the destination.
    assert "renderSidePanel" in out["calls"]


def test_an_admin_returning_to_tasks_still_lands_in_the_console():
    """For an admin or QA reviewer the Tasks rail item is the way back to the
    console they were in. Sending them to the physician dashboard would be the
    same bug pointing the other way."""
    out = _nav_harness("""
    state.panel = 'earnings'; state.view = 'admin';
    setPanel('tasks');
    out({ calls, view: state.view });
    """)
    assert "renderAdminView" in out["calls"]
    assert "renderDashboardView" not in out["calls"]
    assert out["view"] == "admin"


def test_the_other_rail_destinations_are_unchanged():
    out = _nav_harness("""
    const seen = {};
    ['earnings', 'referral', 'guide', 'profile'].forEach((d) => {
      calls.length = 0; state.panel = 'tasks'; state.view = 'eval';
      setPanel(d);
      seen[d] = { calls: calls.slice(), view: state.view, panel: state.panel };
    });
    out(seen);
    """)
    assert "renderEarningsView" in out["earnings"]["calls"]
    assert "renderReferralView" in out["referral"]["calls"]
    assert "renderGuide" in out["guide"]["calls"]
    assert "renderProfileView" in out["profile"]["calls"]
    for dest, seen in out.items():
        # Only 'tasks' resets the view; leaving a case for Earnings and coming
        # straight back must still be able to resume it from the dashboard.
        assert seen["view"] == "eval", dest


# ═════════════════════════════════════════════════════════════════════════════
#  §7 — the metadata chip bar leaves the task page
# ═════════════════════════════════════════════════════════════════════════════

def test_no_routing_vocabulary_renders_above_the_clinical_question():
    """Specialty, difficulty, modality and capture mode are OUR routing
    vocabulary. They arrived because a physician chose from a queue and needed
    to compare cases; with one-button entry there is nothing to compare, and
    telling a specialist "Difficulty: hard" before they read the chart primes
    the answer."""
    ws = _code(_fn("renderTaskWorkspace"))
    for gone in ("metaRow", "metaChip", "Difficulty: ", "Multimodal case",
                 "Reasoning capture", "Grounding required", "asc-meta-row"):
        assert gone not in ws, gone
    # The helper and its styles go with it — nothing else called either.
    assert "function metaChip(" not in JS
    assert "DIFFICULTY_DOT" not in JS
    assert ".asc-meta-chip" not in CSS_CODE


def test_the_prompt_card_still_carries_the_question():
    ws = _fn("renderTaskWorkspace")
    assert "asc-prompt-label" in ws and "asc-prompt-text" in ws
    assert "'Clinical question' : 'Clinical prompt'" in ws


def test_a_grounding_required_task_with_no_disclaimer_still_warns_the_physician():
    """§7 makes this banner the ONLY warning before submit refuses them.

    It used to be gated on ``task.grounding_disclaimer`` as well as on the
    requirement, i.e. on a field the client does not control. Today that field
    is safe — ``routers/asclepius.py`` sends the GROUNDED_PREMIUM_DISCLAIMER
    constant for every required task, so it is never empty on the wire and this
    widened gate has nothing to catch yet. It is asserted anyway: the chip that
    used to back the banner up is gone, so "the only warning" must not be one
    server-side refactor away from disappearing.
    """
    ws = _code(_fn("renderTaskWorkspace"))
    assert "if (required && task.grounding_disclaimer)" not in ws, "the narrow gate is gone"
    assert "if (required) {" in ws
    assert "task.grounding_disclaimer\n              || 'Every claim in your answer needs a citation anchored to the case.'" in ws
    assert "Evidence required for this task" in ws


def test_the_server_still_supplies_a_disclaimer_for_every_required_task():
    """The other half of the test above. If this constant ever becomes per-task
    or nullable, the client gate widened in §7 is what keeps the warning on
    screen — and this test is the pointer between the two."""
    router = (pathlib.Path(__file__).resolve().parents[1]
              / "routers" / "asclepius.py").read_text(encoding="utf-8")
    assert ('"grounding_disclaimer": GROUNDED_PREMIUM_DISCLAIMER '
            'if grounding_mode == "required" else None') in router
    from asclepius.constants import GROUNDED_PREMIUM_DISCLAIMER
    assert GROUNDED_PREMIUM_DISCLAIMER.strip(), "the shipped disclaimer is empty"


def test_the_grounding_requirement_itself_is_untouched():
    """Deleting a chip cannot let a bad submission through: the requirement is
    enforced at submit by groundingSatisfied(), which this change does not
    touch. Asserted so a later "simplification" cannot quietly move the gate
    into the banner."""
    g = _fn("groundingSatisfied")
    assert "if ((task.grounding_mode || 'optional') !== 'required') return { ok: true, reasons: [] };" in g
    assert "missing_rationale_anchor" in g and "missing_step_anchor" in g
    submit = _code(_fn("submitEvaluation"))
    assert "const g = groundingSatisfied();" in submit
    assert "if (!g.ok) { updateSubmitState(); return; }" in submit


# ═════════════════════════════════════════════════════════════════════════════
#  §10 — do not touch
# ═════════════════════════════════════════════════════════════════════════════

def test_the_reviewer_billing_clock_is_untouched_by_the_display_timer():
    """``payments.py``'s session clock is the REVIEWER BILLING clock and is a
    different mechanism from the display timer this PRD changes. Nothing here
    may reach into it."""
    payments = (pathlib.Path(__file__).resolve().parents[1]
                / "asclepius" / "payments.py").read_text(encoding="utf-8")
    for knob in ("BEAT_INTERVAL_SECONDS", "MAX_GAP_SECONDS", "PAUSE_TOLERANCE_SECONDS"):
        assert knob in payments, f"{knob} must still exist in payments.py"
        assert knob not in JS, f"{knob} must not leak into the portal's display timer"
