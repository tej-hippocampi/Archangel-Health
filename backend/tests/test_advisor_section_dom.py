"""Advisor PRD §6.2 — the Advisor section actually renders.

Source-grepping a frontend module proves it was written, not that it works.
The lesson this repo already paid for: a section can be complete, correct and
INVISIBLE for a whole build round because nothing mounted it and the failure was
quiet. So these tests execute advisor.js against the DOM shim and assert what
lands in the document — including the failure case, which is part of the
contract rather than a nicety.
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


# The same ctx shape asclepius.js hands its section modules, with ``api`` as a
# scripted stub so the section renders against a known payload.
_JS_CTX = """
require(%(shim)s);
function h(tag, attrs) {
  var el = document.createElement(tag);
  if (attrs) for (var k in attrs) {
    var v = attrs[k];
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v; else el.setAttribute(k, v);
  }
  for (var i = 2; i < arguments.length; i++) appendChild_(el, arguments[i]);
  return el;
}
function isText_(c) { return typeof c === 'string' || typeof c === 'number'; }
function appendChild_(el, c) {
  if (c == null || c === '' || c === false) return;
  if (Array.isArray(c)) { c.forEach(function (x) { appendChild_(el, x); }); return; }
  el.appendChild(isText_(c) ? document.createTextNode(String(c)) : c);
}
var apiCalls = [];
var ROUTES = %(routes)s;
var ctx = {
  h: h,
  clear: function (el) { while (el.firstChild) el.removeChild(el.firstChild); },
  api: function (path, opts) {
    apiCalls.push({ path: path, method: (opts && opts.method) || 'GET',
                    body: (opts && opts.body) || null });
    return Promise.resolve(ROUTES[path] || {});
  },
  toast: function () {},
  loadingCard: function (t) { return h('div', {}, t); },
  downloadBlob: function () {},
  fmtDate: function (d) { return String(d); },
  openPipeline: function () {},
};
function textOf(el) {
  if (el.nodeValue != null) return el.nodeValue;
  return (el.childNodes || []).map(textOf).join(' ');
}
function classesOf(el) {
  var out = el.className ? [el.className] : [];
  (el.childNodes || []).forEach(function (c) { if (c.tagName) out = out.concat(classesOf(c)); });
  return out;
}
// The DOM shim's selector engine understands #id / .class / [attr=val], but not
// a bare tag selector — walk by tagName rather than assume querySelectorAll.
function byTag(el, tag) {
  var out = [];
  (el.childNodes || []).forEach(function (c) {
    if (!c.tagName) return;
    if (c.tagName === tag) out.push(c);
    out = out.concat(byTag(c, tag));
  });
  return out;
}
function clickTab(root, label) {
  var hit = byTag(root, 'BUTTON').filter(function (b) {
    return textOf(b).indexOf(label) !== -1;
  });
  if (!hit.length) throw new Error('no tab button matching ' + label);
  // The shim dispatches by name; there is no synthesized .click().
  hit[0].dispatch('click');
  return hit.length;
}
eval(require('fs').readFileSync(%(module)s, 'utf8'));
"""

_REFERRALS = {
    "referral_code": "HCWA4TN2",
    "invite_url": "https://portal.example/asclepius/join?ref=HCWA4TN2",
    "total": 3, "active": 2,
    "counts": {"invited": 1, "signed_up": 0, "verified": 0, "approved": 2, "declined": 0},
    "referrals": [
        {"referral_id": "ref-1", "invitee_name": "Dr Sarah Chen",
         "invitee_email": "chen@example.com", "note": "Stanford ortho",
         "status": "approved", "status_word": "Active",
         "invited_at": "2026-07-01T10:00:00", "resolved_at": "2026-07-09T10:00:00"},
        {"referral_id": "ref-2", "invitee_name": "Dr Amit Rao",
         "invitee_email": "rao@example.com", "note": None,
         "status": "invited", "status_word": "Invited",
         "invited_at": "2026-07-20T10:00:00", "resolved_at": None},
    ],
}


def _script(routes: dict, tail: str) -> str:
    return (_JS_CTX % {"shim": json.dumps(str(_DOM_SHIM)),
                       "module": json.dumps(str(_FRONTEND / "advisor.js")),
                       "routes": json.dumps(routes)}) + tail


def test_the_advisor_section_defines_the_global_asclepius_js_mounts():
    out = _run_node(_script({}, """
console.log(JSON.stringify({
  hasGlobal: !!window.AdvisorSection,
  hasRender: !!(window.AdvisorSection && typeof window.AdvisorSection.render === 'function'),
}));
"""))
    assert out["hasGlobal"] is True
    assert out["hasRender"] is True


def test_the_referrals_tab_renders_the_funnel_and_the_invite_form():
    out = _run_node(_script(
        {"/advisor/referrals": _REFERRALS},
        """
var body = document.createElement('div');
window.AdvisorSection.reset();
window.AdvisorSection.render(body, ctx);
setTimeout(function () {
  console.log(JSON.stringify({
    text: textOf(body), classes: classesOf(body), apiCalls: apiCalls,
  }));
}, 20);
"""))
    text = out["text"]
    # The three destinations from the PRD's sketch.
    for tab in ("Your referrals", "Awaiting your review", "Your sign-offs"):
        assert tab in text
    # The funnel, in plain words — never a raw status token.
    assert "3 invited" in text and "2 active" in text
    assert "Dr Sarah Chen" in text and "Active" in text
    assert "Dr Amit Rao" in text and "Invited" in text
    assert "signed_up" not in text, "a raw status token reached the page"
    # The invite affordance and the shareable link.
    assert "Invite a physician" in text
    assert "HCWA4TN2" in text
    assert out["apiCalls"][0]["path"] == "/advisor/referrals"


def test_the_referrals_table_shows_no_credentialing_internals():
    """A referrer is not entitled to the credentialing file of the person they
    referred. The server whitelists; this proves the client does not go looking
    for more, even if a future payload carried it."""
    leaky = json.loads(json.dumps(_REFERRALS))
    leaky["referrals"][0].update({
        "npi": "1234567893", "tier_score": 88,
        "verification_status": "approved", "verification_notes": "strong CV",
    })
    out = _run_node(_script({"/advisor/referrals": leaky}, """
var body = document.createElement('div');
window.AdvisorSection.reset();
window.AdvisorSection.render(body, ctx);
setTimeout(function () { console.log(JSON.stringify({ text: textOf(body) })); }, 20);
"""))
    for leaked in ("1234567893", "88", "strong CV"):
        assert leaked not in out["text"], f"the referrals table rendered {leaked!r}"


def test_the_queue_tab_lists_only_artifact_types_the_server_returned():
    """An absent key means 'you do not hold that capability', which is a
    different fact from an empty list — and must not render as '0 awaiting'."""
    out = _run_node(_script(
        {"/advisor/queue": {"queue": {
            "export_bundle": [{"export_id": "exp-77", "record_count": 240,
                               "profile": "default", "signoffs": 0,
                               "latest_verdict": None}],
            "task_batch": [],
        }}},
        """
var body = document.createElement('div');
window.AdvisorSection.reset();
window.AdvisorSection.render(body, ctx);
// The first render is async (it awaits the referrals fetch), so the tab button
// only exists on the next tick. Click THEN, and read one tick after that so the
// queue fetch has resolved too.
setTimeout(function () {
  clickTab(body, 'Awaiting your review');
  setTimeout(function () {
    console.log(JSON.stringify({ text: textOf(body), apiCalls: apiCalls }));
  }, 20);
}, 20);
"""))
    text = out["text"]
    assert "Outbound bundles" in text
    assert "exp-77" in text
    assert "Task batches" in text          # present but empty -> honest empty state
    assert "Nothing awaiting you here" in text
    # The two types the server did NOT return must not appear at all.
    assert "Hospital intake" not in text
    assert "Product specs" not in text


def test_intake_findings_render_from_the_reports_real_shape():
    """The residual-identifier scan is the substance of an intake review.

    The report the ingestion pipeline actually writes nests the scan under
    ``verification.{status,findings}`` — not a flat ``residual_identifiers``
    list. Reading keys that do not exist renders a tidy '—' on every row: the
    page looks fine, the advisor sees nothing, and nobody can tell. This
    fixture is shaped like the real thing on purpose.
    """
    report = {
        "verification": {"status": "flagged", "verifier": "regex_v2",
                         "findings": [{"kind": "name"}, {"kind": "date"}]},
        "completeness": "verified",
        "missing_modalities": ["pathology"],
        "quarantine_reason": "residual identifiers present",
    }
    out = _run_node(_script(
        {"/advisor/queue": {"queue": {"inbound_upload": [
            {"upload_id": "upl-1", "status": "quarantined", "signoffs": 0}]}},
         "/advisor/artifacts/inbound_upload/upl-1": {
             "artifact_type": "inbound_upload", "artifact_id": "upl-1",
             "upload": {"upload_id": "upl-1", "status": "quarantined"},
             "n_cases": 1,
             "cases": [{"ingest_case_id": "ic-1", "specialty": "nephrology",
                        "status": "quarantined", "case": {"presentation": "CKD"},
                        "report": report}],
             "signoffs": []}},
        """
var body = document.createElement('div');
window.AdvisorSection.reset();
window.AdvisorSection.render(body, ctx);
setTimeout(function () {
  clickTab(body, 'Awaiting your review');
  setTimeout(function () {
    var rows = byTag(body, 'TR').filter(function (r) {
      return textOf(r).indexOf('upl-1') !== -1;
    });
    if (rows.length) rows[0].dispatch('click');
    setTimeout(function () {
      console.log(JSON.stringify({ text: textOf(body) }));
    }, 20);
  }, 20);
}, 20);
"""))
    text = out["text"]
    assert "residual identifiers: 2 flagged" in text, (
        "the de-identification scan did not reach the advisor — findingsText is "
        "reading keys the intake report does not have")
    assert "completeness verified" in text
    assert "missing: pathology" in text
    assert "quarantined: residual identifiers present" in text


def test_the_verdict_form_says_plainly_that_it_does_not_block():
    """An advisor must never believe they are holding up a shipment when they
    are not — sign-off is recorded, never blocking."""
    src = (_FRONTEND / "advisor.js").read_text(encoding="utf-8")
    assert "does not block" in src
    assert "related-party attestation" in src
    # And the three verdicts are all offered, with the comment rules stated.
    for verdict in ("approved", "approved_with_comments", "changes_requested"):
        assert verdict in src


def test_a_missing_advisor_module_renders_a_visible_error():
    """The absence case is part of the contract. A quiet placeholder is what hid
    a finished feature for an entire build round."""
    src = (_FRONTEND / "asclepius.js").read_text(encoding="utf-8")
    mount = src.split("function renderAdvisorView")[1][:1500]
    assert "asc-error" in mount
    assert "failed to load" in mount
    # The branch must RENDER something. An early return that leaves an empty
    # shell is the silent failure wearing different clothes.
    assert "appendChild" in mount
