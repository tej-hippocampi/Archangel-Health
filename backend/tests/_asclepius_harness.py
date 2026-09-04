"""Build a browser harness that renders the REAL shipped Asclepius components.

The eval portal is a dependency-free IIFE with no build step and no component
framework, so there is nothing to import and mount. This extracts the render
functions from ``asclepius.js`` verbatim, stubs only the app state and I/O they
touch, and assembles a page that links the REAL stylesheets. What a browser then
paints is what the app paints.

Why this exists: two defects shipped through a green test suite because they were
only visible once rendered — ``.asc-sev-pill``'s ``text-transform: capitalize``
Title-Casing §11's plain-language axis labels, and the rubric slider painting in
the browser's default blue in a palette that has no blue. Neither is reachable
from source assertions: the CSS was correct in isolation, and the JS emitted the
right strings. Only the cascade, in a real engine, showed the result.

Fidelity matters here. An earlier throwaway version of this harness hand-wrote
the answer-card markup and skipped ``sectionCard``, which made the page look
broken in ways the app is not — so every component below is built by calling the
shipped function, never by hand.
"""

from __future__ import annotations

import json
import pathlib
import re

FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
JS_PATH = FRONTEND / "asclepius.js"

# Render functions pulled from the shipped source, in dependency order.
_FUNCS = (
    "h", "appendChildren", "isSpecificText", "tierForPoints", "autoGrow",
    "criterionAxes", "renderRubricCriterionCard", "setStepConfirmed", "stepStatusOf",
    "stepsSkeleton", "scrollByInstant", "repaintStepRow", "renderStepsListV3",
    "buildStepRowV3", "chooseSpecialty", "renderSpecialtyPicker",
    "renderExperienceBadge", "compareSubstages", "sectionCard", "renderAnswerCard",
    "renderAnswersInto", "verdictButton",
)
# Module-level constants those functions close over.
_CONSTS = (
    "_VAGUE_MARKERS", "_UNIT_RE", "_CLINICAL_RE", "TIER_DEFAULT_PTS",
    "TIER_CHOICES", "AXIS_LABELS", "SUBSTAGE_META",
)


def _extract_function(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
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


def _extract_const(src: str, name: str) -> str:
    m = re.search(rf"(const {name} = [\s\S]*?;)\n", src)
    assert m, f"const {name} not found in asclepius.js"
    return m.group(1)


def axis_labels() -> dict:
    """``AXIS_LABELS`` as data — the source of truth for what §11 promises the
    physician reads. Used to assert the rendered text equals the authored text."""
    block = re.search(
        r"const AXIS_LABELS = \{(.*?)\n  \};", JS_PATH.read_text(encoding="utf-8"), re.S
    ).group(1)
    out = {}
    for key, label in re.findall(r"(\w+):\s*\['((?:[^'\\]|\\.)*)'", block):
        out[key] = label.replace("\\'", "'")
    return out


def tier_labels() -> list:
    block = re.search(
        r"const TIER_CHOICES = \[(.*?)\];", JS_PATH.read_text(encoding="utf-8"), re.S
    ).group(1)
    return re.findall(r"\['[a-z]+',\s*'([^']+)'", block)


_PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<link rel="stylesheet" href="_tokens.css">
<link rel="stylesheet" href="_base.css">
<link rel="stylesheet" href="asclepius.css">
</head>
<body class="asc-body">
<header class="asc-header"><div class="asc-header-inner">
  <a class="asc-logo" href="#"><span class="asc-logo-mark">.</span><strong>Asclepius</strong></a>
  <nav class="asc-nav"><button class="asc-nav-btn active">Evaluate</button>
  <button class="asc-nav-btn">Guide</button><button class="asc-nav-btn">Stats</button></nav>
  <!-- The right-hand group mirrors index.html. Without it the header has no
       right edge to measure, and "is the bar full-bleed or a centred island"
       is not an answerable question. -->
  <div class="asc-header-right">
    <div class="asc-user-badge"><span class="asc-user-email">mockadmin</span>
    <span class="asc-user-role">evaluator · nephrology</span></div>
    <button type="button" class="asc-btn asc-btn-ghost asc-btn-sm">Sign out</button>
  </div>
</div></header>
<main class="asc-main"><div class="asc-wrap" id="root"></div></main>
<script>
window.__harnessErrors = [];
window.addEventListener('error', (e) => window.__harnessErrors.push(String(e.message)));

const state = {
  draft: { rubric: [], rubricCursor: 0, portal_version: 'v3', verdict: 'A_better',
           chosen_id: 'A' },
  taxonomy: {}, specialties: null, _openStep: 1, splitting: false, showFullText: false,
  task: { task_id: 't', grounding_mode: 'optional', capture_reasoning: true,
          candidate_answers: [{ id: 'A', text: __A__ }, { id: 'B', text: __B__ }] },
};
let steps = [];
function activeSteps() { return steps; }
function draftVersion() { return 'v3'; }
function isV3() { return true; }
function isAssisted() { return true; }
function assistData() { return null; }
function computeAnswerDiff() { return null; }
function refreshAnswerHighlight() {}
function selectVerdict() {}
function getPortalSpecialty() { return 'cardiology'; }
function setPortalSpecialty() {}
function renderEvalView() {}
function saveDraft() {}
function updateSubmitState() {}
function renderRationale() {}
function syncStepsCont() {}
function isValidAnchor() { return true; }
function emptyAnchor() { return {}; }
function newAuthoredStep() { return { text: '', added: true }; }
function withMic(el) { return el; }
function renderAnchorBlock() { return document.createElement('div'); }
function infoDot() {
  const s = document.createElement('span'); s.className = 'asc-info-dot';
  s.textContent = '?'; return s;
}
function specialtyDot(sp) {
  return ({ nephrology: 'asc-dot-green', cardiology: 'asc-dot-orange',
            oncology: 'asc-dot-pink' })[sp] || 'asc-dot-lime';
}
function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }
async function api() {
  return { specialties: [
    { specialty: 'nephrology', enabled: true }, { specialty: 'cardiology', enabled: true },
    { specialty: 'oncology', enabled: true }, { specialty: 'hepatology', enabled: false }] };
}

__PAYLOAD__

const root = document.getElementById('root');
root.appendChild(renderExperienceBadge());

// Compare substage (§3 §4 §13) — real answer cards in their real card shell.
const answers = h('div', { class: 'asc-answers', id: 'ascAnswers' });
renderAnswersInto(answers);
root.appendChild(sectionCard('compare', null,
  answers,
  h('div', { class: 'asc-verdicts', style: 'margin-top:16px' },
    verdictButton('A_better', 'A is better', '1'),
    verdictButton('B_better', 'B is better', '2'),
    verdictButton('both_inadequate', 'Both inadequate', '3', true)),
  h('div', { class: 'asc-assist-hint', id: 'ascAssistHint' })));

// Reasoning substage (§5 §6 §7 §8).
steps = [0, 1, 2, 3].map((i) => ({
  text: 'Step ' + (i + 1) + '. ' + __STEP__,
  original_text: __STEP__,
  suggested_label: i === 0 ? 'good' : (i === 2 ? 'bad' : null),
  suggested_critique: i === 2 ? 'this conflates dilute urine with volume depletion' : null,
  confirmed: i === 0, corrected: i === 1, added: false, step_note: '',
}));
root.appendChild(sectionCard('reasoning', null,
  h('p', { class: 'asc-help', style: 'margin:4px 0 12px' },
    'Confirm each step, or open it and say what\\u2019s off \\u2014 one step at a time.'),
  h('div', { class: 'asc-steps', id: 'ascStepsList' }),
  h('div', { style: 'margin-top:12px;display:flex;gap:8px;flex-wrap:wrap' },
    h('button', { class: 'asc-btn asc-btn-subtle asc-btn-sm', type: 'button' }, '+ Add step'))));
renderStepsListV3('ascStepsList');

// Rubric substage (§9 §10 §11 §12).
const crits = [{ text: 'gives thrombolytics in a suspected aortic dissection',
                 points: -9, axes: ['safety', 'accuracy'], source: 'manual' }];
root.appendChild(sectionCard('rubric', null,
  h('p', { class: 'asc-help', style: 'margin:4px 0 12px' },
    'List what a correct answer must get right and what it must never do.'),
  renderRubricCriterionCard(crits, 0)));

// A second criterion card in the POSITIVE polarity, so the stem toggle's other
// state is painted too (§9's lime wash, and a +N readout).
const pos = [{ text: 'caps the correction at 8 mEq/L per 24 hours', points: 7,
               axes: ['safety'], source: 'manual' }];
const posCard = renderRubricCriterionCard(pos, 0);
posCard.id = 'ascPositiveCriterion';
root.appendChild(posCard);
</script></body></html>"""

_ANSWER_A = (
    "Start hypertonic saline 3% at 1 mL/kg/h with sodium checks every 2 hours. The urine "
    "osmolality of 120 mOsm/kg is inappropriately dilute for a serum sodium of 121, which "
    "points away from SIADH and toward primary polydipsia, so the correction must be capped "
    "at 8 mEq/L per 24 hours."
)
_ANSWER_B = (
    "This is SIADH. Fluid-restrict to 800 mL/day and add salt tabs. Recheck the sodium in "
    "the morning."
)
# Deliberately longer than any bar it is rendered into, so §8's CSS ellipsis is
# actually exercised. A string that happens to fit would make
# test_original_bar_truncates_at_the_true_edge pass without testing anything.
_STEP = (
    "The urine osmolality of 120 mOsm/kg is inappropriately dilute for this degree of "
    "hyponatremia, which argues against SIADH as the sole driver here and points instead "
    "toward primary polydipsia or a reset osmostat, so the correction rate has to be "
    "capped and the sodium rechecked every two hours until the trajectory is clear."
)


_ADMIN_PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<title>Archangel Health Operations (harness)</title>
<link rel="stylesheet" href="_tokens.css">
<link rel="stylesheet" href="_base.css">
<link rel="stylesheet" href="asclepius.css">
<link rel="stylesheet" href="admin.css">
</head>
<body class="asc-body asc-admin-body">
<header class="asc-admin-bar" id="ascAdminBar" hidden></header>
<main class="asc-main" id="ascAdminRoot" aria-live="polite"></main>
<div class="asc-toast-region" id="ascToasts"></div>
<script>
window.__harnessErrors = [];
window.addEventListener('error', (e) => window.__harnessErrors.push(String(e.message)));

// file:// is an opaque origin in Chromium, so the real localStorage either
// throws or is useless here. The console reads exactly one key from it.
try { localStorage.setItem('asclepius_token', 'harness'); }
catch (e) {
  Object.defineProperty(window, 'localStorage', { value: {
    _v: { asclepius_token: 'harness' },
    getItem(k) { return this._v[k] === undefined ? null : this._v[k]; },
    setItem(k, v) { this._v[k] = String(v); },
    removeItem(k) { delete this._v[k]; },
  } });
}

const ROUTES = {
  '/api/asclepius/auth/me': { id: 'a1', email: 'ops@archangelhealth.ai', role: 'admin' },
  '/api/asclepius/taxonomy': { export_profiles: ['default'] },
  '/api/asclepius/admin/physicians': __ROSTER__,
  '/api/asclepius/admin/submissions?status=pending': { submissions: [] },
  '/api/asclepius/admin/community/summary': __SUMMARY__,
  '/api/asclepius/admin/hs-referrals': __HSREF__,
};
window.fetch = (url) => Promise.resolve({
  ok: true, status: 200,
  headers: { get: (k) => (k === 'content-type' ? 'application/json' : null) },
  json: () => Promise.resolve(ROUTES[String(url)] || {}),
  text: () => Promise.resolve(''),
});

// The roster, the community summary and the referral funnel are REAL: they
// are the three surfaces PRD-F redesigned or built, and a spy section paints
// nothing for a rendered-appearance guard to be wrong about. The three
// unchanged sections stay stubbed — they carry their own suites and would need
// half the admin API stubbed to draw.
const spy = () => ({ render() {}, reset() {} });
window.AdminHealthSection = spy();
window.AdminExportSection = spy();
window.AdminEarningsSection = spy();
</script>
<script src="admin_physicians.js"></script>
<script src="admin_community.js"></script>
<script src="admin_referrals.js"></script>
<script src="admin_shell.js"></script>
</body></html>"""

#: Enough physicians to fill more than one row of the gallery at 1280px, with
#: the three rows that have historically rendered wrong: an advisor (advisor is
#: not a tier), an unmeasured account (blanks that must not read as zeros), and
#: a reviewer (whose tier word the vocabulary rules are about).
_ADMIN_ROSTER = {
    "physicians": [
        {"id": f"d{i}", "name": name, "email": f"d{i}@example.org",
         "specialty": specialty, "tier": tier, "tier_word": tier.title(),
         "is_advisor": advisor, "verification_status": "approved",
         "real_data_approved": bool(i % 2), "slack_joined": True,
         "health_system_name": "Riverside General",
         "contributor_score": None if i == 1 else 70 + i,
         "median_seconds": None if i == 1 else 300 + i * 20,
         "kappa": None if i == 1 else 0.6 + i / 100,
         "kappa_n": 0 if i == 1 else 8, "active": True}
        for i, (name, specialty, tier, advisor) in enumerate([
            ("Ada Okafor", "nephrology", "reviewer", False),
            ("Bela Hartmann", "cardiology", "reviewer", True),
            ("Chen Wu", "oncology", "labeler", False),
            ("Dara Nwosu", "hepatology", "labeler", False),
            ("Elif Demir", "nephrology", "labeler", False),
            ("Farid Haddad", "cardiology", "reviewer", False),
        ])
    ],
    "counts": {"all": 6, "pending": 0, "labelers": 3, "reviewers": 3, "unassigned": 0},
    "misfiled_physicians": [], "misfiled_count": 0,
    "unfiled_physicians": [], "unfiled_count": 0,
}


#: The community tab's payload, shaped exactly as
#: ``GET /admin/community/summary`` returns it. Prose-heavy on purpose: the
#: capitalize guard needs sentences to be wrong about, and the console's
#: sentences live in card subtitles and empty states.
_ADMIN_SUMMARY = {
    "window_days": 7,
    "generated_at": "2026-09-01T12:00:00+00:00",
    "totals": {"posts": 41, "replies": 18, "voices": 9, "reactions": 26,
               "members": 132, "case_rooms": 4},
    "channels": [
        {"slug": "general", "name": "General", "post_policy": "all",
         "posts": 22, "voices": 7, "last_at": "2026-09-01T09:00:00"},
        {"slug": "medical-ai-news", "name": "Medical AI news", "post_policy": "admin",
         "posts": 5, "voices": 1, "last_at": "2026-08-30T09:00:00"},
        {"slug": "research-and-opportunities", "name": "Research", "post_policy": "admin",
         "posts": 0, "voices": 0, "last_at": None},
    ],
    "recent": [
        {"id": 91, "channel_slug": "general", "author": "Ada Okafor", "is_reply": False,
         "body": "The relay handoff on the CKD walk reads much better now that the "
                 "predecessor note sits above the question.",
         "created_at": "2026-09-01T09:00:00"},
        {"id": 92, "channel_slug": "general", "author": "Chen Wu", "is_reply": True,
         "body": "Agreed, and the sodium correction cap is stated in the rubric now.",
         "created_at": "2026-09-01T09:20:00"},
    ],
    "unanswered": [
        {"id": 88, "channel_slug": "general", "author": "Bela Hartmann",
         "body": "Has anyone graded a case where the chart contradicts the discharge "
                 "summary on the admission date?",
         "created_at": "2026-08-29T11:00:00"},
    ],
    "rooms": [
        {"id": "dm-1", "title": "Case walk: nephrology 4 points", "case_ref": "tr-1",
         "participants": 3, "created_at": "2026-08-28T08:00:00"},
    ],
}

#: One introduction at each end of the funnel, so both the "record a stage"
#: control and the "reward paid" state paint.
_ADMIN_HSREF = {
    "referrals": [
        {"hs_referral_id": "hsr-1", "hs_name": "Riverside General",
         "contact_name": "Dr Priya Menon", "contact_role": "Chief Medical Officer",
         "contact_email": "priya@riverside.example.org", "status": "booked",
         "relationship": "Worked together in fellowship", "invited_at": "2026-08-20T10:00:00",
         "referrer_name": "Ada Okafor", "referrer_email": "ada@example.org",
         "note": "She asked for the de-identification memo before the call.",
         "reward_state": None, "fraud_flag": None},
        {"hs_referral_id": "hsr-2", "hs_name": "Northgate Health",
         "contact_name": "Dr Sam Idris", "contact_role": "Director of Data",
         "contact_email": "sam@northgate.example.org", "status": "signed",
         "relationship": "Former colleague", "invited_at": "2026-07-02T10:00:00",
         "referrer_name": "Chen Wu", "referrer_email": "chen@example.org",
         "note": None, "reward_state": "paid", "fraud_flag": None},
    ],
    "total": 2,
}


def build_admin(dest: pathlib.Path) -> pathlib.Path:
    """The admin console, rendered by its OWN shipped bundle (PRD-F R9).

    Unlike ``build`` above, nothing is extracted or re-assembled here: the page
    loads ``admin_shell.js`` and ``admin_physicians.js`` exactly as admin.html
    does and lets them boot. That is possible because the console is now a page
    rather than a view inside another app, which is most of the point of PRD-F
    and is worth having the harness demonstrate.
    """
    dest.mkdir(parents=True, exist_ok=True)
    for asset in ("_tokens.css", "_base.css", "asclepius.css", "admin.css",
                  "admin_shell.js", "admin_physicians.js", "admin_community.js",
                  "admin_referrals.js"):
        (dest / asset).write_text((FRONTEND / asset).read_text(encoding="utf-8"),
                                  encoding="utf-8")
    html = dest / "admin_harness.html"
    html.write_text(
        _ADMIN_PAGE
        .replace("__ROSTER__", json.dumps(_ADMIN_ROSTER))
        .replace("__SUMMARY__", json.dumps(_ADMIN_SUMMARY))
        .replace("__HSREF__", json.dumps(_ADMIN_HSREF)),
        encoding="utf-8")
    return html


def build(dest: pathlib.Path) -> pathlib.Path:
    """Write the harness page plus the real stylesheets into ``dest``.

    Returns the path to the HTML. The stylesheets are COPIED rather than linked
    across directories so the page loads them over ``file://`` without needing a
    server, and so a test can never accidentally pick up a stale copy.
    """
    src = JS_PATH.read_text(encoding="utf-8")
    payload = "\n".join(
        [_extract_const(src, c) for c in _CONSTS]
        + [_extract_function(src, f) for f in _FUNCS]
    )
    page = (
        _PAGE.replace("__PAYLOAD__", payload)
        .replace("__A__", json.dumps(_ANSWER_A))
        .replace("__B__", json.dumps(_ANSWER_B))
        .replace("__STEP__", json.dumps(_STEP))
    )
    dest.mkdir(parents=True, exist_ok=True)
    for sheet in ("_tokens.css", "_base.css", "asclepius.css"):
        (dest / sheet).write_text((FRONTEND / sheet).read_text(encoding="utf-8"), encoding="utf-8")
    html = dest / "harness.html"
    html.write_text(page, encoding="utf-8")
    return html
