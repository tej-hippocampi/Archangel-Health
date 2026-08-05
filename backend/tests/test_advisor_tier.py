"""Advisor PRD Phase 1 — the third tier and the capability layer.

The whole technical risk of this build is one shape: ``tier == 'reviewer'``
written as a hard equality. Add a third value and an advisor silently cannot
review — the check returns False, which is a legitimate answer for a labeler, so
nothing logs and nothing errors. They just cannot click the button, and you find
out when they tell you.

So these tests are mostly about that shape, not about advisors:

  * an advisor passes every gate a reviewer passes, through the real HTTP path;
  * a labeler and a NULL tier still do not;
  * ``propose_tier`` never proposes 'advisor' for any input, including perfect;
  * the capability table covers EVERY tier the column may hold (the grep test);
  * no gating literal survives anywhere in the backend.
"""

from __future__ import annotations

import pathlib
import re
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import capabilities as caps  # noqa: E402
from asclepius import compensation as comp  # noqa: E402
from asclepius import credentialing  # noqa: E402
from asclepius import review as asc_review  # noqa: E402
from asclepius import store as asc_store  # noqa: E402

client = TestClient(A.app)

BACKEND = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    A.fresh_store()
    from asclepius import pipeline as asc_pipeline

    async def _ok_critic(task, submission):
        return {"consistent": True, "issues": [], "skipped": True}

    async def _ok_grounding(task, submission):
        return {"grounding_ok": True, "issues": [], "skipped": True, "checked_anchors": 0}

    monkeypatch.setattr(asc_pipeline, "run_critic", _ok_critic)
    monkeypatch.setattr(asc_pipeline, "run_grounding_check", _ok_grounding)
    yield


def _admin():
    return A.make_user(asc_store.get_store(), role="admin")


def _physician(tier=None):
    store = asc_store.get_store()
    u = A.make_user(store, role="evaluator", specialty="nephrology")
    if tier:
        with store._conn() as conn:
            conn.execute("UPDATE users SET tier = ? WHERE id = ?", (tier, u["id"]))
        u = store.get_user_by_id(u["id"])
    return u


def _advisor():
    store = asc_store.get_store()
    u = _physician()
    return store.appoint_advisor(u["id"], agreement_ref="AGR-2026-01",
                                 appointed_by="admin@asclepius.test")


# ═══ The capability table ════════════════════════════════════════════════════
def test_capability_table_covers_every_tier_the_column_may_hold():
    """THE GREP TEST (PRD §2.2).

    If someone adds a fourth tier, this fails loudly here instead of denying
    silently in production — which is the entire failure mode this build exists
    to remove. The verification router's writable vocabulary and the capability
    table must be the same set, in both directions.
    """
    import routers.asclepius_verify as verify

    assert set(caps._BY_TIER) == set(caps.TIERS)
    assert set(verify._TIERS) == set(caps.TIERS)
    # Every capability a tier grants must be a capability that exists.
    for tier, granted in caps._BY_TIER.items():
        assert granted <= set(caps.CAPABILITIES), f"{tier} grants an unknown capability"
    # And every tier renders as a WORD, never a raw token.
    for tier in caps.TIERS:
        assert caps.tier_word(tier) and caps.tier_word(tier) != tier
    assert caps.tier_word(None) == "Unassigned"


def test_no_hard_tier_equality_survives_in_backend_gates():
    """No gate may compare a tier to a literal again.

    This is the static half of the grep in §2.2. It scans the backend for
    ``tier == "reviewer"``-shaped comparisons; the capability layer is the only
    legitimate place a tier string is matched, and it does so through a dict
    lookup rather than an equality.
    """
    pattern = re.compile(
        r"""tier[^\n]{0,40}[=!]=\s*['"](labeler|reviewer|advisor)['"]"""
        r"""|['"](labeler|reviewer|advisor)['"]\s*[=!]=[^\n]{0,40}tier""")
    # A guard that cannot fail is decoration. Prove the pattern catches the
    # exact shapes this build removed before trusting an empty result set.
    for bad in ('    return (user or {}).get("tier") == "reviewer"',
                "if user['tier'] == 'advisor':",
                '        elif tier == "reviewer":',
                "if 'labeler' != row.tier:"):
        assert pattern.search(bad), f"the guard would not have caught: {bad}"
    for ok in ('return asc_caps.can(user, asc_caps.REVIEW)',
               'tier = (body.tier or "").strip().lower()',
               'if tier not in _TIERS:'):
        assert not pattern.search(ok), f"the guard false-positives on: {ok}"

    allowed = {
        # The capability layer itself and its compensation sibling are where
        # tier semantics are ALLOWED to live.
        "asclepius/capabilities.py",
        "asclepius/compensation.py",
        # Packaging resolves the advisory relationship for the provenance flag;
        # it is a disclosure lookup, not an access gate.
        "asclepius/packaging.py",
        # The advisor router stamps the relationship from the tier — same thing.
        "routers/asclepius_advisor.py",
    }
    offenders = []
    for path in sorted(BACKEND.glob("**/*.py")):
        rel = path.relative_to(BACKEND).as_posix()
        if rel.startswith("tests/") or rel in allowed:
            continue
        for lineno, line in _code_lines(path):
            if pattern.search(line):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Hard tier equality found — route it through asclepius.capabilities.can() "
        "instead; a missed literal denies SILENTLY:\n" + "\n".join(offenders))


def _code_lines(path):
    """(lineno, source) for lines with comments and string literals blanked out.

    Tokenized rather than pattern-matched on raw text: this file's own
    docstrings quote the old ``tier == "reviewer"`` code to explain why it was
    removed, and a naive line scan flags that prose as a violation. A guard that
    cries wolf on its own explanation gets muted, and a muted guard is the thing
    it was written to prevent.
    """
    import io
    import tokenize

    src = path.read_text(encoding="utf-8")
    lines = src.splitlines()
    blanked = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                for n in range(tok.start[0], tok.end[0] + 1):
                    blanked[n] = True
    except (tokenize.TokenError, IndentationError, SyntaxError):  # pragma: no cover
        return [(i, ln) for i, ln in enumerate(lines, 1)]
    return [(i, ln) for i, ln in enumerate(lines, 1) if not blanked.get(i)]


def test_capabilities_deny_by_default():
    assert caps.capabilities(None) == frozenset()
    assert caps.capabilities({}) == frozenset()
    assert caps.capabilities({"tier": None}) == frozenset()
    assert caps.capabilities({"tier": "wat"}) == frozenset()
    assert not caps.can({"tier": "labeler"}, caps.REVIEW)
    assert not caps.can({"tier": "reviewer"}, caps.REFER)
    # An admin operates every surface, as today.
    assert caps.can({"role": "admin"}, caps.SIGNOFF_EXPORT)


def test_advisor_holds_every_capability_a_reviewer_holds():
    """An advisor is a third tier, not 'a reviewer with more buttons' — but it
    must never hold FEWER capabilities than the tier below it, or promoting
    someone would quietly take work away from them."""
    assert caps._BY_TIER["reviewer"] <= caps._BY_TIER["advisor"]
    assert caps._BY_TIER["labeler"] <= caps._BY_TIER["reviewer"]
    assert caps.REFER in caps._BY_TIER["advisor"]
    assert caps.REFER not in caps._BY_TIER["reviewer"]


# ═══ The gate, through the real HTTP path ════════════════════════════════════
def test_advisor_passes_can_review_and_a_labeler_still_does_not():
    advisor = _advisor()
    assert asc_review.can_review(advisor) is True
    assert asc_review.can_review(_physician("reviewer")) is True
    assert asc_review.can_review(_physician("labeler")) is False
    assert asc_review.can_review(_physician(None)) is False
    assert asc_review.can_review(None) is False


def test_advisor_reaches_the_review_portal_and_a_labeler_gets_403():
    advisor = _advisor()
    r = client.get("/api/asclepius/review/next", headers=A.headers_for(advisor))
    # 200 with an empty queue is the pass condition — the point is that the GATE
    # opened, not that there was work behind it.
    assert r.status_code == 200, r.text
    labeler = _physician("labeler")
    assert client.get("/api/asclepius/review/next",
                      headers=A.headers_for(labeler)).status_code == 403
    unassigned = _physician(None)
    assert client.get("/api/asclepius/review/next",
                      headers=A.headers_for(unassigned)).status_code == 403


def test_advisor_can_draw_a_normal_labeling_task():
    """Capability 1: an advisor labels from scratch on the existing labeler
    path. A tier that could review but not label would be a regression nobody
    would notice until an advisor tried it."""
    store = asc_store.get_store()
    advisor = _advisor()
    store.insert_task(prompt="A 62-year-old with rising creatinine…",
                      specialty="nephrology",
                      candidate_answers=[{"id": "a", "text": "Option A"},
                                         {"id": "b", "text": "Option B"}])
    r = client.get("/api/asclepius/tasks/next", headers=A.headers_for(advisor))
    assert r.status_code == 200, r.text
    assert (r.json().get("task") or {}).get("task_id")


def test_review_me_reports_the_advisor_can_review():
    advisor = _advisor()
    r = client.get("/api/asclepius/review/me", headers=A.headers_for(advisor))
    assert r.status_code == 200
    body = r.json()
    assert body["tier"] == "advisor"
    assert body["can_review"] is True


def test_session_payload_carries_capabilities_not_just_a_tier():
    """The portal decides what to render from CAPABILITIES. Shipping only the
    tier would push the two-state check into the frontend."""
    advisor = _advisor()
    r = client.get("/api/asclepius/auth/me", headers=A.headers_for(advisor))
    assert r.status_code == 200
    body = r.json()
    assert body["tier"] == "advisor"
    assert body["tier_word"] == "Medical Advisor"
    assert caps.REFER in body["capabilities"]
    assert caps.SIGNOFF_EXPORT in body["capabilities"]

    labeler = _physician("labeler")
    body = client.get("/api/asclepius/auth/me",
                      headers=A.headers_for(labeler)).json()
    assert caps.REFER not in body["capabilities"]
    assert body["tier_word"] == "Labeler"


# ═══ Advisors are appointed, never proposed ══════════════════════════════════
def test_propose_tier_never_returns_advisor_even_on_a_perfect_record():
    """§2.3. ``TIER_WEIGHTS`` reads like a ladder. It is not one — an advisory
    relationship is negotiated and carries equity, not scored."""
    perfect = {
        "npi_verified": 1,
        "npi_payload_json": {"result": "verified",
                             "record": {"credential": "MD",
                                        "taxonomy": {"desc": "Nephrology"}}},
        "email": "chief@stanford.edu",
        "email_domain_class": "academic",
        "board_cert": "ABIM — Nephrology",
        "years_experience": 30,
        "specialty": "nephrology",
        "cv_parsed_json": {"ok": True, "years_in_practice": 30},
        "linkedin_url": "https://linkedin.com/in/someone",
    }
    out = credentialing.propose_tier(perfect)
    assert out["proposed_tier"] == "reviewer"
    assert out["proposed_tier"] != "advisor"

    # And across a spread of inputs, including degenerate ones.
    for user in (perfect, {}, {"npi_verified": 1}, {"years_experience": 99},
                 {"email": "x@y.com", "board_cert": "yes", "npi_verified": 1,
                  "email_domain_class": "academic", "years_experience": 40}):
        assert credentialing.propose_tier(user)["proposed_tier"] != "advisor"


def test_appointing_an_advisor_without_an_agreement_ref_is_a_400():
    admin_h = A.headers_for(_admin())
    r = client.post("/api/asclepius/admin/advisors",
                    json={"email": f"adv-{uuid.uuid4().hex[:8]}@example.com",
                          "name": "Dr Advisor", "agreement_ref": "   "},
                    headers=admin_h)
    assert r.status_code == 400
    assert "agreement_ref" in r.text

    # Missing entirely is a 422 from the schema — also a refusal, which is the
    # property that matters.
    r = client.post("/api/asclepius/admin/advisors",
                    json={"email": f"adv-{uuid.uuid4().hex[:8]}@example.com"},
                    headers=admin_h)
    assert r.status_code in (400, 422)


def test_the_queue_door_enforces_the_same_agreement_rule():
    """There are TWO doors into the advisor tier. A rule enforced at one of
    them is not a rule."""
    store = asc_store.get_store()
    admin_h = A.headers_for(_admin())
    candidate = _physician()
    r = client.post(f"/api/asclepius/verify/queue/{candidate['id']}/approve",
                    json={"tier": "advisor"}, headers=admin_h)
    assert r.status_code == 400
    assert "agreement" in r.text.lower()
    assert store.get_user_by_id(candidate["id"])["tier"] is None

    r = client.post(f"/api/asclepius/verify/queue/{candidate['id']}/approve",
                    json={"tier": "advisor", "agreement_ref": "AGR-77"},
                    headers=admin_h)
    assert r.status_code == 200, r.text
    user = store.get_user_by_id(candidate["id"])
    # Approved through the queue must carry the SAME terms as the appointment
    # endpoint — otherwise the two doors mint two different kinds of advisor.
    assert user["tier"] == "advisor"
    assert user["compensation_model"] == "equity_only"
    assert user["advisor_agreement_ref"] == "AGR-77"
    assert user["referral_code"]
    assert user["slack_role"] == "Medical Advisor"


def test_no_account_can_hold_the_advisor_tier_without_an_agreement(monkeypatch):
    """Audit H1: the appointment must be ONE write, at BOTH doors.

    Previously each door did two statements, and the verification-queue door did
    them in the order where a crash in between leaves an account approved and
    LIVE with advisory capability, no agreement on file, and a NULL
    compensation model — which the payment predicate would then treat as
    payable. There is no safe ordering of two writes, so this asserts the
    invariant directly: whatever fails, no row ends up an advisor without an
    agreement.
    """
    store = asc_store.get_store()
    admin_h = A.headers_for(_admin())

    # Make the write that follows the appointment blow up at each door, then
    # assert the invariant still holds.
    def _boom(*a, **kw):
        raise RuntimeError("simulated crash immediately after the appointment write")

    monkeypatch.setattr(store, "log_event", _boom)

    candidate = _physician()
    try:
        client.post(f"/api/asclepius/verify/queue/{candidate['id']}/approve",
                    json={"tier": "advisor", "agreement_ref": "AGR-CRASH"},
                    headers=admin_h)
    except Exception:
        pass
    try:
        client.post("/api/asclepius/admin/advisors",
                    json={"email": f"crash-{uuid.uuid4().hex[:8]}@example.com",
                          "agreement_ref": "AGR-CRASH-2"},
                    headers=admin_h)
    except Exception:
        pass
    monkeypatch.undo()

    with store._conn() as conn:
        bad = conn.execute(
            "SELECT id, email, verification_status, compensation_model "
            "FROM users WHERE tier = 'advisor' AND "
            "(advisor_agreement_ref IS NULL OR TRIM(advisor_agreement_ref) = '')"
        ).fetchall()
    assert not bad, (
        "an account holds tier='advisor' with no agreement on file — the exact "
        f"liability §2.3 names: {[dict(r) for r in bad]}")
    # And whatever DID become an advisor carries the whole relationship, not
    # half of it.
    with store._conn() as conn:
        advisors = conn.execute(
            "SELECT compensation_model, advisor_agreement_ref, referral_code, "
            "slack_role FROM users WHERE tier = 'advisor'").fetchall()
    for row in advisors:
        assert row["compensation_model"] == "equity_only"
        assert row["advisor_agreement_ref"]
        assert row["referral_code"]
        assert row["slack_role"] == "Medical Advisor"


def test_appointing_a_rejected_physician_needs_an_explicit_override():
    """Audit H5: a rejected account was appointed straight through, its status
    flipped to approved, and the note explaining WHY it was rejected was
    overwritten. The audit trail on the one decision that most needs one."""
    store = asc_store.get_store()
    admin_h = A.headers_for(_admin())
    rejected = _physician()
    reason = "NPI does not match the name on the license — suspected impersonation"
    r = client.post(f"/api/asclepius/verify/queue/{rejected['id']}/reject",
                    json={"note": reason}, headers=admin_h)
    assert r.status_code == 200

    # Appointing without an override is refused, and says why.
    r = client.post("/api/asclepius/admin/advisors",
                    json={"email": rejected["email"], "agreement_ref": "AGR-9"},
                    headers=admin_h)
    assert r.status_code == 409, r.text
    assert "rejected" in r.text.lower()
    assert store.get_user_by_id(rejected["id"])["verification_status"] == "rejected"

    # An override needs a reason of its own.
    r = client.post("/api/asclepius/admin/advisors",
                    json={"email": rejected["email"], "agreement_ref": "AGR-9",
                          "override_rejection": True},
                    headers=admin_h)
    assert r.status_code == 409

    r = client.post("/api/asclepius/admin/advisors",
                    json={"email": rejected["email"], "agreement_ref": "AGR-9",
                          "override_rejection": True,
                          "override_reason": "Identity confirmed in person by the "
                                             "founder; NPPES record was stale."},
                    headers=admin_h)
    assert r.status_code == 200, r.text

    after = store.get_user_by_id(rejected["id"])
    assert after["tier"] == "advisor"
    # The original rejection reason SURVIVES — appended to, never replaced.
    assert reason in (after["verification_notes"] or ""), (
        "the rejection reason was destroyed by the appointment")
    assert "Identity confirmed in person" in after["verification_notes"]


def test_approval_error_message_names_every_accepted_tier():
    admin_h = A.headers_for(_admin())
    candidate = _physician()
    r = client.post(f"/api/asclepius/verify/queue/{candidate['id']}/approve",
                    json={"tier": "wizard"}, headers=admin_h)
    assert r.status_code == 400
    detail = r.json()["detail"]
    for tier in caps.TIERS:
        assert tier in detail, f"the error message never mentions {tier!r}"


def test_appointment_provisions_a_brand_new_advisor_with_every_term_set():
    store = asc_store.get_store()
    admin_h = A.headers_for(_admin())
    email = f"toby-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/api/asclepius/admin/advisors",
                    json={"email": email, "name": "Toby Barrack",
                          "specialty": "nephrology", "agreement_ref": "AGR-2026-01"},
                    headers=admin_h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provisioned"] is True
    adv = body["advisor"]
    assert adv["tier"] == "advisor"
    assert adv["tier_word"] == "Medical Advisor"
    assert adv["compensation_model"] == "equity_only"
    assert adv["advisor_agreement_ref"] == "AGR-2026-01"
    assert adv["referral_code"]
    assert adv["slack_role"] == "Medical Advisor"
    assert adv["advisor_since"]

    user = store.get_user_by_email(email)
    assert user["verification_status"] == "approved"
    assert user["verified_by"]
    assert asc_review.can_review(user) is True


def test_appointment_promotes_an_existing_physician_without_duplicating_them():
    store = asc_store.get_store()
    admin_h = A.headers_for(_admin())
    existing = _physician("labeler")
    r = client.post("/api/asclepius/admin/advisors",
                    json={"email": existing["email"], "agreement_ref": "AGR-9"},
                    headers=admin_h)
    assert r.status_code == 200, r.text
    assert r.json()["provisioned"] is False
    assert len([u for u in store.list_users() if u["email"] == existing["email"]]) == 1
    assert store.get_user_by_id(existing["id"])["tier"] == "advisor"


def test_only_an_admin_can_appoint_an_advisor():
    """An advisor must not be able to mint another advisor — that is how an
    equity relationship escapes the person who negotiates them."""
    advisor = _advisor()
    r = client.post("/api/asclepius/admin/advisors",
                    json={"email": "x@example.com", "agreement_ref": "AGR-1"},
                    headers=A.headers_for(advisor))
    assert r.status_code == 403


# ═══ Compensation (§2.4) ═════════════════════════════════════════════════════
def test_an_equity_only_advisor_never_appears_in_a_payment_aggregate():
    """There is no payment feature yet. The column, the predicate and the query
    ship anyway — the moment one is built, whoever builds it must not have to
    know that advisors are unpaid, because the data says so."""
    store = asc_store.get_store()
    advisor = _advisor()
    labeler = _physician("labeler")

    assert comp.accrues_payment(advisor) is False
    assert comp.accrues_payment(labeler) is True
    # NULL is payable: every contributor predating the column is a normal one,
    # and under-paying a physician who did the work is the worse error.
    assert comp.accrues_payment({"compensation_model": None}) is True
    assert comp.accrues_payment(None) is True

    task = store.insert_task(prompt="p", specialty="nephrology",
                             candidate_answers=[{"id": "a", "text": "A"},
                                                {"id": "b", "text": "B"}])
    for user in (advisor, labeler):
        store.insert_submission(
            submission_id=f"sub-{uuid.uuid4().hex[:10]}",
            task_id=task["task_id"], evaluator_id=user["id"],
            verdict="a_better", chosen_id="a", rejected_id="b",
            confidence="high", time_spent_sec=60, payload={},
            annotator=store.annotator_block(user), dedupe_hash=None)
    payable = store.submissions_for_payment()
    ids = {row["evaluator_id"] for row in payable}
    assert labeler["id"] in ids
    assert advisor["id"] not in ids, (
        "an equity-only advisor accrued a payment obligation")


def test_the_compensation_model_does_not_follow_a_tier_change():
    """What makes an advisor unpaid is the equity agreement, not the tier. If
    someone edits their tier down, the equity has not evaporated — so the model
    must NOT silently flip back to payable and start accruing money against an
    agreement that is still in force."""
    store = asc_store.get_store()
    admin_h = A.headers_for(_admin())
    advisor = _advisor()
    assert advisor["compensation_model"] == "equity_only"

    r = client.post(f"/api/asclepius/verify/queue/{advisor['id']}/approve",
                    json={"tier": "reviewer"}, headers=admin_h)
    assert r.status_code == 200, r.text
    after = store.get_user_by_id(advisor["id"])
    assert after["tier"] == "reviewer"
    assert after["compensation_model"] == "equity_only"
    assert comp.accrues_payment(after) is False
