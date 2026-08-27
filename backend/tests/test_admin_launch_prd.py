"""Admin Launch PRD §7 — the launch-blocker behaviours, end to end.

Grouped the way the PRD groups them: Void, Decide, Advisor, Invite, Export, Pay.

The one that matters most is in Decide. **Approving WITH the recommendation must
still post to /tiering/{id}/decide.** ``apply_decision_batch()`` learns from
agreement and disagreement equally, so a console that only recorded overrides
would hand the model a training set made entirely of its own mistakes and it
would drift badly — while every screen kept looking healthy. That failure is
invisible from the outside, which is exactly why it is pinned here twice: once
against the DOM (the console really does post it) and once against the store
(the observation really does land).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402

client = TestClient(A.app)

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
_DOM_SHIM = Path(__file__).resolve().parent / "_asclepius_dom.js"


@pytest.fixture(autouse=True)
def _isolated():
    A.fresh_store()
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


def _admin():
    return A.make_user(_store(), role="admin")


def _approved_doctor(**kw):
    store = _store()
    u = A.make_user(store, role="evaluator", specialty="nephrology", **kw)
    with store._conn() as conn:
        conn.execute("UPDATE users SET verification_status = 'approved' WHERE id = ?",
                     (u["id"],))
    return store.get_user_by_id(u["id"])


def _pending_doctor(**kw):
    store = _store()
    u = A.make_user(store, role="evaluator", specialty="nephrology", tier=None, **kw)
    with store._conn() as conn:
        conn.execute("UPDATE users SET verification_status = 'pending' WHERE id = ?",
                     (u["id"],))
    return store.get_user_by_id(u["id"])


def _earning(user, status, *, ref, cents=7500, kind="task"):
    return _store().insert_earning(
        earning_id=f"e-{ref}", user_id=user["id"], kind=kind, ref_id=ref,
        amount_cents=cents, rate_cents=cents, status=status,
        accrued_at="2026-08-01T00:00:00",
        resolved_at=None if status == "accrued" else "2026-08-02T00:00:00")


def _outstanding(user):
    return _store().earnings_payable_for_user(user["id"])["outstanding_cents"]


def _void(admin_h, earning_id, reason="duplicate submission", expect=200):
    r = client.post(f"/api/asclepius/admin/earnings/{earning_id}/void",
                    json={"reason": reason}, headers=admin_h)
    assert r.status_code == expect, r.text
    return r.json()


# ═══ Void (§4.4) ══════════════════════════════════════════════════════════════
def test_voiding_an_accrued_row_drops_the_total_by_exactly_that_amount():
    admin, doc = _admin(), _approved_doctor()
    _earning(doc, "accrued", ref="s1", cents=7500)
    _earning(doc, "approved", ref="s2", cents=2500)
    before = _outstanding(doc)
    assert before == 10000

    out = _void(A.headers_for(admin), "e-s1")
    assert out["voided"] is True
    # The SERVER's recomputed figure comes back, so the console never subtracts
    # locally and cannot drift away from the ledger.
    assert out["totals"]["outstanding_cents"] == 2500
    assert _outstanding(doc) == before - 7500


def test_voiding_the_same_earning_twice_drops_the_total_once():
    """Idempotent on earning_id. A double-click must not double-decrement."""
    admin, doc = _admin(), _approved_doctor()
    _earning(doc, "accrued", ref="s1", cents=7500)
    _earning(doc, "approved", ref="s2", cents=2500)

    first = _void(A.headers_for(admin), "e-s1")
    second = _void(A.headers_for(admin), "e-s1", reason="clicked again")

    assert first["voided"] is True
    # The second call reports that it changed nothing — the same end state and a
    # very different thing to tell an operator.
    assert second["voided"] is False
    assert second["totals"]["outstanding_cents"] == 2500
    assert _outstanding(doc) == 2500
    # And the first reason stands: a replay does not overwrite the audit trail.
    row = _store().get_earning_by_id("e-s1")
    assert row["void_reason"] == "duplicate submission"


def test_voiding_a_paid_row_is_a_conflict():
    """Money already left. Refunds are a treasury operation, out of scope."""
    admin, doc = _admin(), _approved_doctor()
    _earning(doc, "paid", ref="s1")
    body = _void(A.headers_for(admin), "e-s1", expect=409)
    assert "already been paid" in body["detail"]
    assert _store().get_earning_by_id("e-s1")["status"] == "paid"


@pytest.mark.parametrize("payload", [{}, {"reason": ""}, {"reason": "x"}])
def test_a_void_with_no_usable_reason_is_rejected(payload):
    """A void that cannot be explained cannot be audited or appealed."""
    admin, doc = _admin(), _approved_doctor()
    _earning(doc, "accrued", ref="s1")
    r = client.post("/api/asclepius/admin/earnings/e-s1/void",
                    json=payload, headers=A.headers_for(admin))
    assert r.status_code == 422, r.text
    assert _store().get_earning_by_id("e-s1")["status"] == "accrued"


def test_a_voided_row_is_still_listed_marked_and_worth_nothing():
    """Hiding it would make a decision somebody made disappear from the only
    screen that records it."""
    admin, doc = _admin(), _approved_doctor()
    _earning(doc, "accrued", ref="s1", cents=7500)
    _void(A.headers_for(admin), "e-s1", reason="patient identifiers in the answer")

    r = client.get(f"/api/asclepius/admin/earnings?user_id={doc['id']}",
                   headers=A.headers_for(admin))
    rows = r.json()["rows"]
    row = next(x for x in rows if x["earning_id"] == "e-s1")
    assert row["status"] == "void"
    assert row["void_reason"] == "patient identifiers in the answer"
    assert row["voided_by"] and row["voided_at"]
    # It contributes nothing to what we owe.
    assert _outstanding(doc) == 0


def test_a_void_is_attributable():
    admin, doc = _admin(), _approved_doctor()
    _earning(doc, "accrued", ref="s1")
    _void(A.headers_for(admin), "e-s1", reason="off-rubric answer")

    events = _store().list_events(entity_type="earning", entity_id="e-s1")
    voided = [e for e in events if e["event_type"] == "earning_voided"]
    assert len(voided) == 1
    assert voided[0]["actor"] == admin["email"]
    assert voided[0]["payload"]["reason"] == "off-rubric answer"


def test_time_on_task_is_never_reported_as_zero_when_it_is_unknown():
    """§4.3: a zero meaning "unknown" is how an operator voids honest work."""
    admin, doc = _admin(), _approved_doctor()
    store = _store()
    task_id = f"t-{A.uniq()}"
    store.insert_task(task_id=task_id, specialty="nephrology", difficulty="hard",
                      prompt="Which answer is better?")
    sid = f"sub-{A.uniq()}"
    with store._conn() as conn:
        conn.execute(
            "INSERT INTO submissions (submission_id, task_id, evaluator_id, "
            " time_spent_sec, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 0, 'submitted', '2026-08-01T00:00:00', '2026-08-01T00:00:00')",
            (sid, task_id, doc["id"]))
    _earning(doc, "accrued", ref=sid)

    r = client.get(f"/api/asclepius/admin/earnings?user_id={doc['id']}",
                   headers=A.headers_for(admin))
    row = r.json()["rows"][0]
    assert row["seconds"] is None, "an unrecorded duration was reported as 0 seconds"
    assert row["case_id"] == task_id
    assert row["specialty"] == "nephrology"


# ═══ Decide (§3.3) — the whole learning loop depends on this ══════════════════
def _run_node(script: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed in this environment")
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\n{proc.stderr}\n{proc.stdout}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


_CONSOLE_HARNESS = """
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
var CALLS = [];
var RESPONSES = %(responses)s;
var ctx = {
  h: h,
  clear: function (el) { while (el.firstChild) el.removeChild(el.firstChild); },
  api: function (path, opts) {
    CALLS.push({ path: path, method: (opts && opts.method) || 'GET',
                 body: (opts && opts.body) || null });
    if (!(path in RESPONSES)) return Promise.reject({ message: 'no stub for ' + path });
    return Promise.resolve(RESPONSES[path]);
  },
  toast: function () {},
  loadingCard: function (t) { return h('div', {}, t); },
  downloadBlob: function (path, name) { CALLS.push({ path: path, method: 'DOWNLOAD',
                                                     body: name }); },
  fmtDate: function (d) { return String(d); },
  openPipeline: function () {},
};
eval(require('fs').readFileSync(%(module)s, 'utf8'));
function textOf(el) {
  if (el.nodeValue != null) return el.nodeValue;
  return (el.childNodes || []).map(textOf).join(' ');
}
function find(el, pred, acc) {
  acc = acc || [];
  if (pred(el)) acc.push(el);
  (el.childNodes || []).forEach(function (c) { if (c.tagName) find(c, pred, acc); });
  return acc;
}
function clickText(root, label) {
  var hit = find(root, function (e) {
    return e.tagName === 'BUTTON' && textOf(e).indexOf(label) !== -1; });
  if (!hit.length) throw new Error('no button matching ' + label
                                   + ' -- have: ' + textOf(root));
  hit[0].dispatch('click');
  return hit[0];
}
function later(fn, n) { setTimeout(n > 1 ? function () { later(fn, n - 1); } : fn, 0); }
var body = document.createElement('div');
"""

_QUEUE = {"status": "pending", "count": 1, "total": 1, "has_more": False,
          "queue": [{"user_id": "u1", "email": "jane@clinic.org",
                     "full_name": "Jane Doe", "specialty": "cardiology"}]}
_NO_SIGNUPS = {"signups": [], "counts": {"total": 0}, "awaiting_review": 0,
               "can_resend": True}
_DOSSIER = {
    "user_id": "u1", "email": "jane@clinic.org", "full_name": "Jane Doe",
    "specialty": "cardiology", "score": 82, "proposed_tier": "reviewer",
    "tier_words": {"labeler": "Labeler", "reviewer": "Reviewer"},
    "reasons": ["+25 NPI verified against NPPES (MD)",
                "+20 board certified",
                "±0 NPI check unavailable — retry pending (not held against them)"],
    "blockers": [], "has_cv": False, "cv_ok": False,
    "npi": {"npi": "1234567893", "result": "verified", "recheck_pending": False},
    "tiering": {"proposed_tier": "reviewer", "score": 4.7},
}


def _console_decision(button_label: str) -> dict:
    """Open the pending physician and press one of the approve buttons."""
    responses = {
        "/admin/physicians": {"physicians": [], "counts": {"all": 0}},
        "/verify/queue?status=pending": _QUEUE,
        "/admin/signups": _NO_SIGNUPS,
        "/verify/queue/u1": _DOSSIER,
        "/verify/tiering/u1/decide": {"ok": True, "decision": {"was_flip": 0}},
        "/verify/queue/u1/approve": {"ok": True, "tier": "reviewer"},
        "/verify/tiering-weights": {"pending_decisions": 7, "weights": []},
    }
    script = (_CONSOLE_HARNESS % {
        "shim": json.dumps(str(_DOM_SHIM)),
        "module": json.dumps(str(_FRONTEND / "admin_physicians.js")),
        "responses": json.dumps(responses),
    }) + """
window.AdminPhysiciansSection.reset();
window.AdminPhysiciansSection.render(body, ctx);
later(function () {
  find(body, function (e) { return e.tagName === 'TR'; })
    .filter(function (r) { return textOf(r).indexOf('Jane Doe') !== -1; })[0]
    .dispatch('click');
  later(function () {
    clickText(body, %s);
    later(function () {
      console.log(JSON.stringify({ calls: CALLS, text: textOf(body) }));
    }, 6);
  }, 6);
}, 6);
""" % json.dumps(button_label)
    return _run_node(script)


def test_approving_WITH_the_recommendation_still_posts_to_decide():
    """§8 item 9 — the line the whole learning loop depends on.

    The recommendation here is Reviewer and the admin presses Approve as
    Reviewer, i.e. agrees. ``apply_decision_batch()`` learns from agreement and
    disagreement equally: forty agreements are what tell the model its weights
    are right. A console that posted only on override would give the model a
    training set made entirely of its own mistakes.

    Invisible if it is wrong, which is why it is asserted rather than read.
    """
    out = _console_decision("Approve as Reviewer")
    posts = [c for c in out["calls"] if c["method"] == "POST"]
    paths = [c["path"] for c in posts]

    assert "/verify/tiering/u1/decide" in paths, (
        "approving WITH the recommendation did not post to /decide — the learning "
        "loop is receiving only its own mistakes"
    )
    decide = next(c for c in posts if c["path"] == "/verify/tiering/u1/decide")
    assert decide["body"]["tier"] == "reviewer"
    # §3.1: no case_domain. The server defaults to the physician's own declared
    # specialty, which is the question actually being answered at signup.
    assert "case_domain" not in decide["body"]

    # ORDER: decide, then approve. decide records the observation without
    # re-approving, so it is safe first. The other order is not — approve
    # succeeding and decide then failing leaves the physician live with no
    # training signal and nothing to reconcile it.
    assert paths.index("/verify/tiering/u1/decide") < paths.index("/verify/queue/u1/approve")


def test_approving_AGAINST_the_recommendation_posts_too():
    out = _console_decision("Approve as Labeler")
    posts = [c for c in out["calls"] if c["method"] == "POST"]
    decide = next(c for c in posts if c["path"] == "/verify/tiering/u1/decide")
    assert decide["body"]["tier"] == "labeler"
    approve = next(c for c in posts if c["path"] == "/verify/queue/u1/approve")
    assert approve["body"]["tier"] == "labeler"


def test_the_recorded_line_is_shown_after_a_decision():
    out = _console_decision("Approve as Reviewer")
    assert "Recorded." in out["text"]
    # Read from /verify/tiering-weights, not invented client-side.
    assert "7 decisions" in out["text"]


def test_a_failing_decide_means_approve_never_runs():
    """The physician stays pending rather than going live unlabelled."""
    responses = {
        "/admin/physicians": {"physicians": [], "counts": {"all": 0}},
        "/verify/queue?status=pending": _QUEUE,
        "/admin/signups": _NO_SIGNUPS,
        "/verify/queue/u1": _DOSSIER,
        # /verify/tiering/u1/decide deliberately absent → the promise rejects.
        "/verify/queue/u1/approve": {"ok": True},
    }
    script = (_CONSOLE_HARNESS % {
        "shim": json.dumps(str(_DOM_SHIM)),
        "module": json.dumps(str(_FRONTEND / "admin_physicians.js")),
        "responses": json.dumps(responses),
    }) + """
window.AdminPhysiciansSection.reset();
window.AdminPhysiciansSection.render(body, ctx);
later(function () {
  find(body, function (e) { return e.tagName === 'TR'; })
    .filter(function (r) { return textOf(r).indexOf('Jane Doe') !== -1; })[0]
    .dispatch('click');
  later(function () {
    clickText(body, 'Approve as Reviewer');
    later(function () {
      console.log(JSON.stringify({ calls: CALLS, text: textOf(body) }));
    }, 6);
  }, 6);
}, 6);
"""
    out = _run_node(script)
    approves = [c for c in out["calls"] if c["path"] == "/verify/queue/u1/approve"]
    assert approves == [], "approve ran after decide failed"
    # And the operator is told, rather than left looking at a screen that did nothing.
    assert "no stub" in out["text"] or "could not" in out["text"].lower()


def test_the_decision_reaches_the_training_set_exactly_once():
    """The console posts decide AND approve, and approve records an observation
    of its own. Both folded would double-count one admin click into the
    likelihood and advance the pending counter by two — silently."""
    admin, doc = _admin(), _pending_doctor()
    ah = A.headers_for(admin)
    store = _store()

    r = client.post(f"/api/asclepius/verify/tiering/{doc['id']}/decide",
                    json={"tier": "reviewer"}, headers=ah)
    assert r.status_code == 200, r.text
    r = client.post(f"/api/asclepius/verify/queue/{doc['id']}/approve",
                    json={"tier": "reviewer"}, headers=ah)
    assert r.status_code == 200, r.text

    pending = [d for d in store.pending_tiering_decisions(limit=100)
               if d["user_id"] == doc["id"]]
    assert len(pending) == 1, f"one click produced {len(pending)} observations"
    assert pending[0]["admin_tier"] == "reviewer"
    assert store.get_user_by_id(doc["id"])["verification_status"] == "approved"


def test_approve_on_its_own_still_records_its_observation():
    """An API client that never calls /decide must not silently stop teaching
    the model — the de-duplication is about one click, not about skipping."""
    admin, doc = _admin(), _pending_doctor()
    r = client.post(f"/api/asclepius/verify/queue/{doc['id']}/approve",
                    json={"tier": "labeler"}, headers=A.headers_for(admin))
    assert r.status_code == 200, r.text
    pending = [d for d in _store().pending_tiering_decisions(limit=100)
               if d["user_id"] == doc["id"]]
    assert len(pending) == 1


def test_propose_tier_never_returns_advisor():
    """Advisor is a negotiated relationship with equity and a signed agreement,
    not the output of an NPI check and a years-in-practice weight."""
    from asclepius import credentialing

    for i in range(200):
        user = {
            "npi_verified": 1 if i % 2 else 0,
            "npi_payload_json": json.dumps({
                "result": "verified" if i % 2 else "not_found",
                "record": {"credential": "MD", "taxonomy": {"desc": "Nephrology"}},
            }),
            "board_cert": "ABIM" if i % 3 else None,
            "years_experience": i % 45,
            "email_domain_class": ("academic", "hospital", "consumer")[i % 3],
            "specialty": ("nephrology", "cardiology", "oncology")[i % 3],
            "linkedin_url": "https://linkedin.com/in/x" if i % 4 else None,
        }
        out = credentialing.propose_tier(user, duplicate_npi=bool(i % 7 == 0))
        assert out["proposed_tier"] in (None, "labeler", "reviewer"), out


# ═══ Advisor (§2.2) ═══════════════════════════════════════════════════════════
def test_admin_physicians_returns_advisor_status():
    admin = _admin()
    doc = _approved_doctor()
    with _store()._conn() as conn:
        conn.execute("UPDATE users SET advisor_since = ?, tier = 'reviewer' WHERE id = ?",
                     ("2026-05-01T00:00:00", doc["id"]))

    rows = client.get("/api/asclepius/admin/physicians",
                      headers=A.headers_for(admin)).json()["physicians"]
    row = next(r for r in rows if r["id"] == doc["id"])
    assert row["is_advisor"] is True
    assert row["advisor_since"] == "2026-05-01T00:00:00"
    # Still a reviewer. Advisor is not a tier and never lands in users.tier.
    assert row["tier"] == "reviewer"
    assert row["tier_word"] == "Reviewer"


def test_an_advisor_renders_reviewer_plus_advisor_never_unassigned():
    """The quiet-wrong bug this file's own docstring has warned about since its
    first version: tierBadge alone paints "Unassigned" over a medical advisor."""
    responses = {
        "/admin/physicians": {
            "physicians": [{"id": "u9", "name": "Osman Faheem",
                            "email": "drosman@example.com", "phone": "4016800666",
                            "specialty": "cardiology", "tier": "reviewer",
                            "tier_word": "Reviewer", "is_advisor": True,
                            "advisor_since": "2026-05-01T00:00:00",
                            "verification_status": "approved", "slack_joined": True}],
            "counts": {"all": 1}},
        "/verify/queue?status=pending": {"queue": [], "count": 0, "total": 0},
        "/admin/signups": _NO_SIGNUPS,
    }
    script = (_CONSOLE_HARNESS % {
        "shim": json.dumps(str(_DOM_SHIM)),
        "module": json.dumps(str(_FRONTEND / "admin_physicians.js")),
        "responses": json.dumps(responses),
    }) + """
window.AdminPhysiciansSection.reset();
window.AdminPhysiciansSection.render(body, ctx);
later(function () {
  console.log(JSON.stringify({ text: textOf(body) }));
}, 6);
"""
    out = _run_node(script)
    assert "Reviewer" in out["text"]
    assert "Advisor" in out["text"]
    assert "Unassigned" not in out["text"]


def test_advisor_is_not_a_tier_in_the_capability_layer():
    from asclepius import capabilities as caps

    assert "advisor" not in caps.TIERS
    assert "advisor" not in getattr(caps, "TIER_WORDS", {})


# ═══ Invite (§5.1) ════════════════════════════════════════════════════════════
def _invite(admin_h, user_id, expect=200):
    r = client.post("/api/asclepius/admin/community/invite",
                    json={"user_id": user_id}, headers=admin_h)
    assert r.status_code == expect, r.text
    return r.json()


def test_an_unapproved_physician_cannot_be_invited():
    """The link opens a room of credential-verified peers."""
    admin, doc = _admin(), _pending_doctor()
    body = _invite(A.headers_for(admin), doc["id"], expect=409)
    assert "approved" in body["detail"]


def test_inviting_someone_already_joined_sends_nothing():
    admin, doc = _admin(), _approved_doctor()
    with _store()._conn() as conn:
        conn.execute("UPDATE users SET slack_joined = 1 WHERE id = ?", (doc["id"],))

    out = _invite(A.headers_for(admin), doc["id"])
    assert out["already_joined"] is True
    assert out["sent"] is False
    # No token was minted for someone who is already inside.
    assert _store().latest_community_invite_for_user(doc["id"]) is None


def test_inviting_an_unknown_user_is_a_404():
    admin = _admin()
    _invite(A.headers_for(admin), "nobody", expect=404)


def test_mark_community_welcomed_claims_exactly_once_under_concurrency():
    """The guarded UPDATE is the arbiter, not a read-then-write. Under a
    multi-worker deploy a concurrent approve + invite-redemption for the same
    physician must not both win."""
    import threading

    store = _store()
    doc = _approved_doctor()
    results = []
    barrier = threading.Barrier(8)

    def claim():
        barrier.wait()
        results.append(store.mark_community_welcomed(doc["id"]))

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(True) == 1, results
    assert results.count(False) == 7


def test_the_invite_stores_only_a_hash_of_the_token():
    """A database read must not be able to mint a working link."""
    store = _store()
    doc = _approved_doctor()
    store.create_community_invite(user_id=doc["id"], email=doc["email"],
                                  token_hash="deadbeef" * 8,
                                  expires_at="2099-01-01T00:00:00",
                                  created_by="admin@example.com")
    row = store.latest_community_invite_for_user(doc["id"])
    assert row["token_hash"] == "deadbeef" * 8
    assert "token" not in {k for k in row if k != "token_hash"}
    assert row["redeemed_at"] is None
    # And redemption is a single-winner guarded UPDATE.
    assert store.redeem_community_invite("deadbeef" * 8) is True
    assert store.redeem_community_invite("deadbeef" * 8) is False


def test_an_expired_or_unknown_invite_link_is_refused():
    r = client.get("/community/join/not-a-real-token", follow_redirects=False)
    assert r.status_code == 404


# ═══ Export (§4.3) ════════════════════════════════════════════════════════════
def _case_with_record(doc):
    """A task, a submission, and a PACKAGED record.

    Built through ``packaging.package_submission`` rather than by hand: the
    export runs the buyer profile's schema over every record, so a hand-rolled
    payload would prove only that the fixture was wrong.
    """
    from asclepius import packaging as asc_packaging

    store = _store()
    task = store.insert_task(
        specialty="nephrology", difficulty="hard",
        prompt="Which answer is better?",
        candidate_answers=[{"id": "a", "text": "Hold metformin at eGFR 28."},
                           {"id": "b", "text": "Continue metformin."}])
    task_id = task["task_id"]

    sid = f"sub-{A.uniq()}"
    with store._conn() as conn:
        conn.execute(
            "INSERT INTO submissions (submission_id, task_id, evaluator_id, verdict, "
            " chosen_id, rejected_id, confidence, time_spent_sec, status, "
            " payload_json, created_at, updated_at) "
            "VALUES (?, ?, ?, 'A_better', 'a', 'b', 'high', 320, 'submitted', "
            "        '{}', '2026-08-01T00:00:00', '2026-08-01T00:00:00')",
            (sid, task_id, doc["id"]))
    submission = store.get_submission(sid)

    for payload in asc_packaging.package_submission(task, submission, store):
        store.insert_record(
            submission_id=sid, task_id=task_id, rtype=payload["type"],
            specialty="nephrology", status="export_ready", payload=payload)
    return task_id, sid


def test_case_export_returns_json_with_a_filename():
    admin, doc = _admin(), _approved_doctor()
    task_id, sid = _case_with_record(doc)
    _earning(doc, "approved", ref=sid)

    r = client.get("/api/asclepius/admin/earnings/e-%s/case-export" % sid,
                   headers=A.headers_for(admin))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")
    assert "attachment" in r.headers["content-disposition"]
    assert task_id in r.headers["content-disposition"]
    payload = r.json()
    assert payload["case_id"] == task_id
    assert payload["specialty"] == "nephrology"
    assert payload["cases"] and payload["cases"][0]["case_id"] == task_id

    # No physician identity anywhere in it. The file is shaped like an export
    # bundle and named like one; an email address in it is an identity leak one
    # forward away from a buyer.
    blob = json.dumps(payload)
    assert doc["email"] not in blob, "the spot-check carries the physician's email"
    assert doc["id"] not in blob, "the spot-check carries the physician's user id"
    assert "paid_to" not in payload


def test_case_export_matches_the_export_pipelines_shaping():
    """A buyer-facing bundle and an admin spot-check must never be able to
    disagree about what a case contains."""
    from asclepius import export as asc_export

    admin, doc = _admin(), _approved_doctor()
    task_id, sid = _case_with_record(doc)
    _earning(doc, "approved", ref=sid)

    spot = client.get("/api/asclepius/admin/earnings/e-%s/case-export" % sid,
                      headers=A.headers_for(admin)).json()["cases"][0]

    store = _store()
    bundle = asc_export.export_by_case(store, created_by=admin["id"], case_id=task_id)
    shipped = json.loads(
        (asc_export.export_root() / bundle["export_id"] / "cases.jsonl")
        .read_text(encoding="utf-8").strip().splitlines()[0])

    # Same keys, same case, same labels — one serializer, two readers.
    assert set(spot) == set(shipped)
    assert spot["case_id"] == shipped["case_id"]
    assert spot["n_labelers"] == shipped["n_labelers"]
    assert spot["consensus"] == shipped["consensus"]
    assert spot["labels"] == shipped["labels"]


def test_a_review_session_row_has_no_single_case_to_export():
    """Saying so beats handing an operator a plausible-looking empty bundle."""
    admin, doc = _admin(), _approved_doctor()
    _earning(doc, "approved", ref="sess-1", kind="review_session")
    r = client.get("/api/asclepius/admin/earnings/e-sess-1/case-export",
                   headers=A.headers_for(admin))
    assert r.status_code == 409
    assert "not a single case" in r.json()["detail"]


def test_case_export_is_admin_only():
    doc = _approved_doctor()
    _earning(doc, "approved", ref="s1")
    r = client.get("/api/asclepius/admin/earnings/e-s1/case-export",
                   headers=A.headers_for(doc))
    assert r.status_code in (401, 403)


# ═══ Pay (§4.5) ═══════════════════════════════════════════════════════════════
def test_an_equity_only_physician_cannot_be_marked_paid():
    """Advisors on equity_only hold equity and are not paid per case.

    The column is written directly: the advisor-retirement migration clears
    ``equity_only`` at store init, so it can only be set afterwards.
    """
    admin = _admin()
    doc = _approved_doctor()
    with _store()._conn() as conn:
        conn.execute("UPDATE users SET compensation_model = 'equity_only' WHERE id = ?",
                     (doc["id"],))
    _earning(doc, "approved", ref="s1")

    r = client.post("/api/asclepius/admin/earnings/pay",
                    json={"user_id": doc["id"], "earning_ids": ["e-s1"],
                          "payout_batch_id": "2026-08-25-wise"},
                    headers=A.headers_for(admin))
    assert r.status_code == 409, r.text
    assert "equity-only" in r.json()["detail"]
    assert _store().get_earning_by_id("e-s1")["status"] == "approved"


def test_send_payment_marks_rows_paid_and_stamps_the_batch():
    admin, doc = _admin(), _approved_doctor()
    _earning(doc, "approved", ref="s1", cents=7500)
    _earning(doc, "approved", ref="s2", cents=2500)

    r = client.post("/api/asclepius/admin/earnings/pay",
                    json={"user_id": doc["id"], "earning_ids": ["e-s1", "e-s2"],
                          "payout_batch_id": "2026-08-25-wise"},
                    headers=A.headers_for(admin))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["marked"] == 2
    assert out["totals"]["outstanding_cents"] == 0
    assert out["totals"]["paid_cents"] == 10000
    for eid in ("e-s1", "e-s2"):
        row = _store().get_earning_by_id(eid)
        assert row["status"] == "paid"
        assert row["payout_batch_id"] == "2026-08-25-wise"


def test_replaying_a_payment_batch_pays_nobody_twice():
    admin, doc = _admin(), _approved_doctor()
    _earning(doc, "approved", ref="s1", cents=7500)
    body = {"user_id": doc["id"], "earning_ids": ["e-s1"],
            "payout_batch_id": "2026-08-25-wise"}
    first = client.post("/api/asclepius/admin/earnings/pay", json=body,
                        headers=A.headers_for(admin)).json()
    second = client.post("/api/asclepius/admin/earnings/pay", json=body,
                         headers=A.headers_for(admin)).json()
    assert first["marked"] == 1
    assert second["marked"] == 0
    assert second["already_in_batch"] == 1
    assert second["totals"]["paid_cents"] == 7500


def test_pay_is_admin_only():
    doc = _approved_doctor()
    _earning(doc, "approved", ref="s1")
    r = client.post("/api/asclepius/admin/earnings/pay",
                    json={"user_id": doc["id"], "payout_batch_id": "b"},
                    headers=A.headers_for(doc))
    assert r.status_code in (401, 403)


# ═══ §6 — the NPI tri-state survives the trip to the screen ═══════════════════
def test_an_unavailable_npi_check_is_never_collapsed_into_not_found():
    """credentialing.py:11 — on launch day NPPES may rate-limit us, and treating
    "we could not check" as "this person is not real" is the failure mode. This
    exact collapse has shipped and been caught once in this codebase."""
    from asclepius import credentialing

    unavailable = credentialing.propose_tier({
        "npi_verified": 0,
        "npi_payload_json": json.dumps({"result": "unavailable"}),
        "specialty": "nephrology",
    })
    not_found = credentialing.propose_tier({
        "npi_verified": 0,
        "npi_payload_json": json.dumps({"result": "not_found"}),
        "specialty": "nephrology",
    })
    assert any("unavailable" in r and "not held against them" in r
               for r in unavailable["reasons"])
    assert any("not found in NPPES" in r for r in not_found["reasons"])
    # Two different sentences for two different facts.
    assert unavailable["reasons"] != not_found["reasons"]


def test_a_neutral_reason_line_is_rendered_grey_not_green():
    """§6: a ±0 line is not a credit. Painting it like a +n line is how "we
    could not check" turns into "this person is verified" in an operator's head.

    Stated as "only a leading + is a credit" rather than "±0 is muted", because
    the second form misses the negative lines — see
    test_a_negative_reason_line_is_never_rendered_as_a_credit.
    """
    source = (_FRONTEND / "admin_physicians.js").read_text(encoding="utf-8")
    code = re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", source, flags=re.S))
    assert "function reasonLine(" in code
    # vq-attempt is the muted class; vq-reason is the normal one.
    assert "vq-attempt" in code and "vq-reason" in code
    assert "s.charAt(0) === '+'" in code, "credits are not distinguished at all"


# ═══ §5.2 — the PHI rule survives the community redesign ══════════════════════
def test_the_phi_warning_is_still_a_standing_deterrent():
    """It is the only PHI guard on a free-text surface. The home panel must not
    have restyled it into something ignorable, or replaced it."""
    source = (_FRONTEND / "community.js").read_text(encoding="utf-8")
    assert "Do not post patient-identifiable information." in source
    # Still rendered from the composer, on every channel, never dismissible.
    assert source.count("cm-phi-notice") >= 2
    css = (_FRONTEND / "community.css").read_text(encoding="utf-8")
    assert ".cm-phi-notice" in css


def test_the_community_home_panel_points_at_the_real_asset_path():
    """frontend/ is served at /static; backend/assets is /email-assets and is
    for email only. There is no frontend/asclepius/assets/ directory."""
    source = (_FRONTEND / "community.js").read_text(encoding="utf-8")
    assert "/static/asclepius/ah-mark.png" in source
    assert (_FRONTEND / "ah-mark.png").exists()
    assert "asclepius/assets/" not in source


def test_an_explicitly_empty_selection_never_pays_the_whole_physician():
    """``earning_ids: []`` and an omitted ``earning_ids`` are different requests.

    Omitted means "every approved row this physician has" — mark_paid's
    user-scoped mode. An empty list means the caller selected nothing, and a
    ``default_factory=list`` plus ``or None`` silently widened that into paying
    everything. On a money endpoint the two must not be the same request.
    """
    admin, doc = _admin(), _approved_doctor()
    _earning(doc, "approved", ref="s1", cents=7500)
    _earning(doc, "approved", ref="s2", cents=2500)

    r = client.post("/api/asclepius/admin/earnings/pay",
                    json={"user_id": doc["id"], "earning_ids": [],
                          "payout_batch_id": "2026-08-25-wise"},
                    headers=A.headers_for(admin))
    assert r.status_code == 422, r.text
    assert _store().earnings_payable_for_user(doc["id"])["paid_cents"] == 0
    for eid in ("e-s1", "e-s2"):
        assert _store().get_earning_by_id(eid)["status"] == "approved"

    # Omitting the key entirely still means "pay them everything".
    r = client.post("/api/asclepius/admin/earnings/pay",
                    json={"user_id": doc["id"], "payout_batch_id": "2026-08-25-wise"},
                    headers=A.headers_for(admin))
    assert r.status_code == 200, r.text
    assert r.json()["marked"] == 2


def test_a_negative_reason_line_is_never_rendered_as_a_credit():
    """§6 in its general form. credentialing emits negative lines WITHOUT a
    leading plus — ``-4 consumer email domain (not disqualifying)`` — so keying
    the muted style on ``±0`` alone let a deduction render with exactly the
    weight of a ``+25``."""
    source = (_FRONTEND / "admin_physicians.js").read_text(encoding="utf-8")
    code = re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", source, flags=re.S))
    assert "s.charAt(0) === '+'" in code, (
        "the credit test is not keyed on the leading plus, so a negative line "
        "renders like a credit"
    )

    from asclepius import credentialing
    reasons = credentialing.propose_tier({
        "npi_verified": 1,
        "npi_payload_json": json.dumps({"result": "verified", "record": {"credential": "MD"}}),
        "email_domain_class": "consumer",
        "specialty": "nephrology",
    })["reasons"]
    negative = [r for r in reasons if not r.startswith("+") and not r.startswith("±0")]
    assert negative, "no negative reason line was produced; the fixture needs updating"
    assert all(r.lstrip().startswith("-") for r in negative), negative


def test_a_decision_cannot_be_submitted_twice_from_one_screen():
    """A second submission is a second approval AND a second training
    observation. The button's ``disabled`` attribute is what a browser honours;
    the in-flight flag is what makes that hold independently of the DOM."""
    source = (_FRONTEND / "admin_physicians.js").read_text(encoding="utf-8")
    code = re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", source, flags=re.S))
    assert "let inFlight = false;" in code
    assert code.count("if (inFlight) return;") >= 2, (
        "both the approve and the reject paths must refuse a re-entrant click"
    )
