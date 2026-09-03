"""The approve/reject screen shows what it already fetched.

GET /verify/queue/{id} returns credentials, attestations, registry, flags,
duplicate_claims, years_experience and board_cert. The decision screen read
about a third of that: email, phone, NPI, specialty, clinical role,
organisation, LinkedIn, submitted date, and a CV download button.

So an admin approving a physician could not see their board certifications, the
thing the decision is nominally about, could not see the attestations they
signed, could not see the plausibility flags, and for a NON-US physician saw a
blank where their registration number belongs, because the screen showed only
NPI.

All of those renderers existed. They were wired to the POST-approval profile
screen instead of to the decision. This file asserts they now serve both, and
that they are still defined once.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

_FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
_MODULE = _FRONTEND / "admin_physicians.js"
_DOM_SHIM = pathlib.Path(__file__).parent / "_asclepius_dom.js"
JS = _MODULE.read_text(encoding="utf-8")

_LINE_COMMENT = re.compile(r"(?m)^\s*//.*$")
_BLOCK_COMMENT = re.compile(r"/\*[\s\S]*?\*/")


def _code(src: str) -> str:
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", src))


_HARNESS = """
require(%(shim)s);
const CALLS = [];
const RESPONSES = %(responses)s;
function later(fn, n) { if (n <= 0) { fn(); return; } setTimeout(function () { later(fn, n - 1); }, 0); }
function textOf(n) { return n && n.textContent ? n.textContent : ''; }
function find(root, pred) {
  const out = [];
  (function walk(n) {
    if (!n) return;
    if (pred(n)) out.push(n);
    (n.childNodes || []).forEach(walk);
  })(root);
  return out;
}
function h(tag, attrs) {
  const el = document.createElement(tag);
  attrs = attrs || {};
  Object.keys(attrs).forEach(function (k) {
    const v = attrs[k];
    if (v == null || v === false) return;
    if (k === 'class') el.className = v;
    else if (k === 'html') el.innerHTML = v;
    else if (k.slice(0, 2) === 'on') el.addEventListener(k.slice(2).toLowerCase(), v);
    else el.setAttribute(k, v);
  });
  for (let i = 2; i < arguments.length; i++) {
    const c = arguments[i];
    (Array.isArray(c) ? c : [c]).forEach(function (x) {
      if (x == null || x === false) return;
      el.appendChild(typeof x === 'object' ? x : document.createTextNode(String(x)));
    });
  }
  return el;
}
const ctx = {
  h: h,
  clear: function (n) { while (n.firstChild) n.removeChild(n.firstChild); },
  api: function (p) {
    CALLS.push(p);
    if (Object.prototype.hasOwnProperty.call(RESPONSES, p)) return Promise.resolve(RESPONSES[p]);
    return Promise.reject({ status: 404, message: 'no stub for ' + p });
  },
  loadingCard: function (t) { return h('div', {}, t); },
  fmtDate: function (s) { return String(s).slice(0, 10); },
  downloadBlob: function () {},
  toast: function () {},
};
const body = document.createElement('div');
require(%(module)s);
%(body)s
"""


def _run(dossier: dict, queue_rows=None, signups=None) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed in this environment")
    queue = {"status": "pending", "count": 1, "total": 1, "has_more": False,
             "queue": queue_rows if queue_rows is not None else [
                 {"user_id": "u1", "email": "jane@clinic.org", "full_name": "Jane Doe",
                  "specialty": "cardiology"}]}
    responses = {
        "/admin/physicians": {"physicians": [], "counts": {"all": 0}},
        "/verify/queue?status=pending": queue,
        "/admin/signups": signups if signups is not None else {
            "signups": [], "counts": {"total": 0}, "awaiting_review": 0, "can_resend": True},
        "/verify/queue/u1": dossier,
        "/verify/tiering-weights": {"pending_decisions": 0, "weights": []},
    }
    script = (_HARNESS % {
        "shim": json.dumps(str(_DOM_SHIM.resolve())),
        "module": json.dumps(str(_MODULE)),
        "responses": json.dumps(responses),
        "body": """
window.AdminPhysiciansSection.reset();
window.AdminPhysiciansSection.render(body, ctx);
later(function () {
  const row = find(body, function (e) { return e.tagName === 'TR'; })
    .filter(function (r) { return textOf(r).indexOf('Jane Doe') !== -1; })[0];
  if (row) row.dispatch('click');
  later(function () {
    console.log(JSON.stringify({ text: textOf(body), calls: CALLS }));
  }, 8);
}, 8);
""",
    })
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\n{proc.stderr}\n{proc.stdout}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


_BASE = {
    "user_id": "u1", "email": "jane@clinic.org", "full_name": "Jane Doe",
    "specialty": "cardiology", "score": 82, "proposed_tier": "reviewer",
    "tier_words": {"labeler": "Labeler", "reviewer": "Reviewer"},
    "reasons": ["+25 NPI verified against NPPES (MD)"],
    "blockers": [], "has_cv": False, "cv_ok": False,
    "npi": {"npi": "1234567893", "result": "verified"},
    "tiering": {"proposed_tier": "reviewer", "score": 4.7},
}


# ─── The bug this exists for ─────────────────────────────────────────────────
def test_a_non_us_physician_shows_their_registration_not_a_blank_npi():
    """THE REGRESSION, and it is invisible from grepping the source: the screen
    had its own identity renderer that only knew about NPI, so a GMC- or
    SCFHS-registered doctor's card was blank where their credential belongs."""
    d = dict(_BASE)
    d["npi"] = {"npi": None, "result": None}
    d["registry"] = {
        "is_us": False, "country": "SA", "registry_name": "SCFHS",
        "id_label": "SCFHS number", "identifier": "1234567",
        "verified": None, "lookup_url": "https://scfhs.example/verify",
        "note": "SCFHS has no public API; check by hand at the link above.",
    }
    out = _run(d)
    assert "SCFHS" in out["text"], "the registry name is still missing"
    assert "1234567" in out["text"], "the registration number is still missing"
    assert "https://scfhs.example/verify" in out["text"], "no way to check by hand"
    # The note is computed, serialized, and used to render nowhere.
    assert "check by hand at the link above" in out["text"]


def test_board_certifications_reach_the_screen_the_decision_is_made_on():
    d = dict(_BASE)
    d["credentials"] = {
        "fullLegalName": "Jane Doe",
        "licenseNumber": "A-99881",
        "licenseState": "CA",
        "boardCertifications": [
            {"board": "ABIM", "specialty": "Cardiovascular Disease", "active": True}],
        "residency": [{"institution": "UCSF", "year": "2012"}],
    }
    d["attestations"] = {"signedInitials": "JD", "attestWorkQuality": True}
    out = _run(d)
    assert "ABIM" in out["text"]
    assert "Cardiovascular Disease" in out["text"]
    assert "A-99881" in out["text"], "the licence number is still missing"
    assert "JD" in out["text"], "the signature is still shown to nobody"


def test_a_legacy_account_without_the_certifications_array_is_not_blank():
    """board_cert is the derived string older accounts carry. Without the
    fallback, the one line the decision is nominally about renders empty."""
    d = dict(_BASE)
    d["credentials"] = {"fullLegalName": "Jane Doe"}
    d["board_cert"] = "ABIM — Nephrology"
    out = _run(d)
    assert "ABIM" in out["text"]


def test_duplicate_claimants_are_visible_before_the_buttons():
    """Two accounts on one NPI is decision-changing and was invisible here."""
    d = dict(_BASE)
    d["duplicate_claims"] = [
        {"email": "other@clinic.org", "verification_status": "approved"}]
    out = _run(d)
    text = out["text"]
    assert "other@clinic.org" in text
    assert text.index("other@clinic.org") < text.index("Approve as"), (
        "a duplicate claim rendered below the decision buttons"
    )


def test_a_cv_conflict_is_open_beside_the_typed_record_not_buried():
    """Deterministic Python diffing two stored blobs, already written for the
    agent, and rendered nowhere. It is what "the CV says a different residency
    year" looks like, and it belongs next to what was typed."""
    d = dict(_BASE)
    d["credentials"] = {"fullLegalName": "Jane Doe"}
    d["cv_conflicts"] = [
        {"field": "Residency year", "cv": "2011", "stated": "2013"}]
    out = _run(d)
    assert "Residency year" in out["text"]
    assert "2011" in out["text"] and "2013" in out["text"]


def test_flags_are_the_first_thing_on_the_screen():
    """Not a verdict: the card is titled "Worth a look" and only exists when
    there is something to look at. Putting it first is the difference between
    reading carefully and skimming."""
    d = dict(_BASE)
    d["flags"] = [{"severity": "high", "field": "npi", "issue": "name_mismatch",
                   "detail": "NPPES returns a different surname"}]
    out = _run(d)
    text = out["text"]
    assert "Worth a look" in text
    assert text.index("Worth a look") < text.index("Recommendation")


def test_the_npi_check_state_keeps_its_five_values_on_this_screen():
    """npiWord collapses to a tri-state, and folding UNAVAILABLE into NOT_FOUND
    has shipped and been caught once in this codebase already."""
    d = dict(_BASE)
    d["npi"] = {"npi": "1234567893", "result": "unavailable"}
    out = _run(d)
    assert "unavailable" in out["text"]


def test_background_research_stays_below_the_decision_buttons():
    """An existing invariant, and the reorder is exactly the change that could
    have broken it: the research is text the applicant's own web presence
    controls."""
    d = dict(_BASE)
    d["agent_research"] = [{"claim": "Listed at Example Clinic",
                            "source_url": "https://example.org"}]
    out = _run(d)
    text = out["text"]
    assert "Approve as" in text
    research_at = text.find("Example Clinic")
    assert research_at > text.index("Approve as"), "research moved above the decision"


# ─── The anti-duplication ratchet ────────────────────────────────────────────
@pytest.mark.parametrize("fn", ["identityRows", "flagsCard", "credentialsAsTyped",
                                "npiPayloadCard", "kvBlock"])
def test_each_shared_renderer_is_defined_exactly_once(fn):
    """The bug this file exists for happened because the decision screen grew
    its own copy of a renderer the profile already had. Without this, the next
    change forks it again."""
    assert _code(JS).count(f"function {fn}(") == 1, fn


def test_the_profile_call_into_credentials_is_unchanged():
    """credentialsAsTyped gained a third argument. It is defaulted, so the
    profile path stays byte-identical rather than quietly gaining a card."""
    code = _code(JS)
    assert "function credentialsAsTyped(h, c, a, extra)" in code
    assert "extra = extra || {};" in code


# ─── The queue itself ────────────────────────────────────────────────────────
_SIGNUP = {"email": "half@clinic.org", "name": "Half Finished", "specialty": "renal",
           "stage_index": 3, "stage_total": 6, "stage_word": "Credentials",
           "kind": "director", "health_system_id": "hs1"}


def _queue_only(rows, signups):
    """Render the console and stop at the queue: no row is clicked."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed in this environment")
    responses = {
        "/admin/physicians": {"physicians": [], "counts": {"all": 0}},
        "/verify/queue?status=pending": {"status": "pending", "count": len(rows),
                                         "total": len(rows), "has_more": False,
                                         "queue": rows},
        "/admin/signups": {"signups": signups, "counts": {"total": len(signups)},
                           "awaiting_review": len(rows), "can_resend": True},
        "/verify/tiering-weights": {"pending_decisions": 0, "weights": []},
    }
    script = (_HARNESS % {
        "shim": json.dumps(str(_DOM_SHIM.resolve())),
        "module": json.dumps(str(_MODULE)),
        "responses": json.dumps(responses),
        "body": """
window.AdminPhysiciansSection.reset();
window.AdminPhysiciansSection.render(body, ctx);
later(function () {
  const trs = find(body, function (e) { return e.tagName === 'TR'; });
  const names = trs.map(function (r) { return textOf(r); });
  // Structural, not textual: the waiting cell also renders an amber badge, so
  // "does this row carry a look chip" has to be asked of a specific cell rather
  // than of the row's text. Name, specialty, waiting, practice case, proposed,
  // LOOK, chevron: the practice-case column landed between waiting and
  // proposed, which is why this is index 5 and not 4.
  const looks = trs.map(function (r) {
    const tds = (r.childNodes || []).filter(function (c) { return c.tagName === 'TD'; });
    const cell = tds.length >= 6 ? tds[5] : null;
    return { row: textOf(r), look: cell ? textOf(cell) : null };
  });
  console.log(JSON.stringify({ text: textOf(body), rows: names, looks: looks }));
}, 8);
""",
    })
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\n{proc.stderr}\n{proc.stdout}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _row(uid, name, created_at, **kw):
    base = {"user_id": uid, "email": f"{uid}@clinic.org", "full_name": name,
            "specialty": "cardiology", "created_at": created_at,
            "blockers": [], "flag_count": 0}
    base.update(kw)
    return base


def test_the_urgent_count_counts_only_the_people_who_can_be_decided():
    """THE REGRESSION for this half. The chip received the union of the
    verification queue and the mid-wizard funnel, so it read "6 waiting" when
    one account could actually be decided: false urgency on the one number an
    admin reads before deciding whether to open the tab at all."""
    out = _queue_only([_row("u1", "Jane Doe", "2026-08-01T00:00:00Z")],
                      [dict(_SIGNUP), dict(_SIGNUP), dict(_SIGNUP),
                       dict(_SIGNUP), dict(_SIGNUP)])
    assert "Waiting on your decision (1)" in out["text"]
    assert "Still filling in the wizard (5)" in out["text"]
    assert "(6)" not in out["text"]


def test_the_longest_wait_is_at_the_top():
    """The store returns created_at DESC because that ordering is shared with
    the approved roster. Newest-first is exactly wrong for a queue whose promise
    is 24 hours: it puts the person who has waited longest at the bottom."""
    out = _queue_only([
        _row("u1", "Newest", "2026-08-30T00:00:00Z"),
        _row("u2", "Oldest", "2026-08-01T00:00:00Z"),
        _row("u3", "Middle", "2026-08-15T00:00:00Z"),
    ], [])
    order = [r for r in out["rows"] if "est" in r or "Middle" in r]
    joined = " | ".join(order)
    assert joined.index("Oldest") < joined.index("Middle") < joined.index("Newest"), joined


def test_a_physician_waiting_more_than_a_day_is_marked():
    out = _queue_only([_row("u1", "Jane Doe", "2020-01-01T00:00:00Z")], [])
    assert "asc-badge-amber" in _MODULE.read_text(encoding="utf-8")
    assert "d" in out["text"]


def test_a_clean_row_carries_no_look_chip():
    """An always-present count makes every row look flagged, which is the same
    as none of them being flagged."""
    clean = _queue_only([_row("u1", "Jane Doe", "2026-08-01T00:00:00Z")], [])
    flagged = _queue_only([_row("u2", "Flagged Person", "2026-08-01T00:00:00Z",
                               blockers=["no npi"], flag_count=2)], [])
    jane = [r for r in clean["looks"] if "Jane Doe" in r["row"]][0]
    other = [r for r in flagged["looks"] if "Flagged Person" in r["row"]][0]
    assert other["look"] == "3", "one blocker plus two flags should read 3"
    assert jane["look"] == "", "a clean row must render no chip at all"


def test_a_mid_wizard_signup_is_still_not_clickable_into_a_decision():
    """Carried forward under the two-table layout: there is no account to
    approve, and this remains the only screen they appear on at all."""
    out = _queue_only([], [dict(_SIGNUP)])
    assert "Signup incomplete" in out["text"]
    assert "step 3/6" in out["text"]
    assert "Nobody is waiting on a decision." in out["text"]
