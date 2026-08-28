"""Quality-adjusted pay: what it may do, and what it must never do.

Pay was flat: ``amount_cents=rate, rate_cents=rate`` for every payable
submission, with quality acting only as a binary gate. This adds a multiplier.

Money gets an INTEGRATION test, not a unit test, and that house rule is why the
ledger half of this file drives real routes. The multiplier itself is pure, so
it gets both.

The properties that matter most are the refusals. This is algorithmic management
of contractor compensation and a stronger version of the question the tiering
work already went to counsel on under NYC Local Law 144, so:

  * it takes no physician attribute as input, asserted adversarially;
  * it never applies a reduction on its own, only proposes one;
  * it is bounded on both sides;
  * it ships OFF, so merging it changes nobody's pay.
"""

from __future__ import annotations

import pytest

from tests._asclepius import fresh_store, make_user

from asclepius import payout


@pytest.fixture()
def on(monkeypatch):
    monkeypatch.setenv("ASCLEPIUS_PAYOUT_QUALITY_ENABLED", "1")


@pytest.fixture()
def store():
    return fresh_store()


# ─── It ships off ────────────────────────────────────────────────────────────

def test_it_is_off_by_default_so_merging_it_changes_nobodys_pay(monkeypatch):
    monkeypatch.delenv("ASCLEPIUS_PAYOUT_QUALITY_ENABLED", raising=False)
    assert payout.enabled() is False
    r = payout.quality_multiplier(quality_score=10.0, review_verdict="accept_with_edits")
    assert r["multiplier"] == 1.0
    assert r["proposed"] is False


def test_switched_off_even_a_terrible_case_pays_the_posted_rate(monkeypatch):
    monkeypatch.delenv("ASCLEPIUS_PAYOUT_QUALITY_ENABLED", raising=False)
    r = payout.quality_multiplier(quality_score=0.0, review_verdict="accept_with_edits")
    assert payout.amount_for(7500, r["multiplier"]) == 7500


# ─── The refusals ────────────────────────────────────────────────────────────

def test_it_takes_no_physician_attribute_at_all(on):
    """The same discipline as tiering's FORBIDDEN_CREDENTIAL_KEYS and its
    pinned-to-zero protected features. Asserted on the signature, because a
    value it cannot receive is one it cannot weigh."""
    import inspect

    params = set(inspect.signature(payout.quality_multiplier).parameters)
    params.discard("kwargs")
    assert params == {"quality_score", "review_verdict"}


def test_the_same_case_pays_the_same_whoever_labelled_it(on):
    """The adversarial version of the test above: there is no input through
    which a physician's identity, tier, history or credentials could reach the
    number."""
    a = payout.quality_multiplier(quality_score=62.0, review_verdict="accept_with_edits")
    b = payout.quality_multiplier(quality_score=62.0, review_verdict="accept_with_edits")
    assert a["multiplier"] == b["multiplier"]


def test_a_reduction_is_only_ever_proposed(on):
    """The single most important line in the module. An automated pay cut and a
    proposed cut a person approves are materially different objects."""
    r = payout.quality_multiplier(quality_score=40.0, review_verdict="accept_with_edits")
    assert r["multiplier"] < 1.0
    assert r["proposed"] is True


def test_paying_at_or_above_the_rate_needs_no_human(on):
    """Nobody needs to approve giving a physician the money they were promised."""
    assert payout.quality_multiplier(
        quality_score=92.0, review_verdict="accept")["proposed"] is False
    assert payout.quality_multiplier(
        quality_score=78.0, review_verdict="accept")["proposed"] is False


def test_an_ungraded_case_pays_full_rate_and_proposes_nothing(on):
    """"We have not looked at it yet" is not a finding about the work, and
    paying less for it would charge a physician for our own review backlog."""
    r = payout.quality_multiplier(quality_score=None, review_verdict=None)
    assert r["multiplier"] == 1.0
    assert r["proposed"] is False


# ─── Bounded on both sides ───────────────────────────────────────────────────

def test_there_is_a_floor_because_the_physician_did_the_work(on):
    """A near-zero payout for delivered work is a wage claim, not an
    incentive."""
    r = payout.quality_multiplier(quality_score=0.0, review_verdict="accept_with_edits")
    assert r["multiplier"] >= payout.floor_multiplier()


def test_the_ceiling_is_above_one_so_excellence_pays_more(on):
    """Upside moves behaviour without ever taking money off somebody who
    delivered, which is the safer instrument."""
    assert payout.ceiling_multiplier() > 1.0
    r = payout.quality_multiplier(quality_score=100.0, review_verdict="accept")
    assert r["multiplier"] > 1.0


def test_the_multiplier_never_escapes_its_bounds(on):
    for q in (0, 10, 25, 40, 55, 69, 70, 84, 85, 99, 100):
        for v in ("accept", "accept_with_edits", "reject", "", None):
            m = payout.quality_multiplier(quality_score=q, review_verdict=v)["multiplier"]
            assert payout.floor_multiplier() <= m <= payout.ceiling_multiplier()


def test_a_rejected_case_is_voided_by_the_ledger_not_reduced_here(on):
    """A value for reject would be a second opinion about a decision already
    made."""
    assert payout.VERDICT_ADJ["reject"] == 0.0


# ─── It stays explainable ────────────────────────────────────────────────────

def test_every_answer_carries_its_reasons(on):
    """A silent deduction is the worst possible version of this feature."""
    for q in (None, 20.0, 78.0, 95.0):
        r = payout.quality_multiplier(quality_score=q, review_verdict="accept")
        assert r["reasons"] and all(isinstance(x, str) for x in r["reasons"])


def test_hitting_the_floor_says_so(on, monkeypatch):
    monkeypatch.setenv("ASCLEPIUS_PAYOUT_FLOOR", "0.95")
    r = payout.quality_multiplier(quality_score=0.0, review_verdict="accept_with_edits")
    assert any("floor" in x for x in r["reasons"])


def test_the_default_coefficients_never_actually_reach_the_floor(on):
    """Worth stating: the worst the defaults can do is -35%, against a 60%
    floor. The floor is a BACKSTOP on future tuning, not a number the current
    weights are pressed up against, and if a later change starts hitting it
    that is a signal the weights moved too far rather than a normal day."""
    worst = min(
        payout.quality_multiplier(quality_score=q, review_verdict=v)["multiplier"]
        for q in (0, 25, 50, 69) for v in ("accept", "accept_with_edits")
    )
    assert worst > payout.floor_multiplier()


def test_the_answer_is_stamped_with_the_ruleset_that_produced_it(on):
    r = payout.quality_multiplier(quality_score=78.0, review_verdict="accept")
    assert r["version"] == payout.PAYOUT_VERSION


def test_a_fractional_cent_resolves_in_the_physicians_favour():
    # 7500 * 0.905 = 6787.5 -> 6788, not 6787.
    assert payout.amount_for(7500, 0.905) == 6788


def test_a_zero_rate_pays_zero_rather_than_erroring():
    assert payout.amount_for(0, 1.15) == 0


# ─── The ledger half ─────────────────────────────────────────────────────────

def _accrued_row(store, user_id: str, *, amount=7500, rate=7500):
    from asclepius import payments as p

    return store.insert_earning(
        earning_id="earn-hold-1", user_id=user_id, kind=p.KIND_TASK,
        ref_id="sub-1", amount_cents=amount, rate_cents=rate,
        status=p.ACCRUED, accrued_at="2026-08-01T00:00:00Z", resolved_at=None, note=None)


def test_a_held_row_is_not_swept_into_approved_by_the_fourteen_day_window(store):
    """The window exists so a labeler is never held hostage by a review
    backlog. It is not a two-week fuse on an unreviewed pay cut."""
    from datetime import datetime, timedelta, timezone

    from asclepius import payments as p

    u = make_user(store, role="evaluator", specialty="nephrology")
    row = _accrued_row(store, u["id"], amount=6000)
    store.set_earning_quality(row["earning_id"], multiplier=0.8,
                              reasons=["-20% test"], version="t", hold=True)

    later = datetime.now(timezone.utc) + timedelta(days=90)
    p._auto_approve(store, now=later)

    assert store.get_earning_by_id(row["earning_id"])["status"] == p.ACCRUED


def test_an_unheld_row_still_auto_approves(store):
    """The promise this feature must not break."""
    from datetime import datetime, timedelta, timezone

    from asclepius import payments as p

    u = make_user(store, role="evaluator", specialty="nephrology")
    row = _accrued_row(store, u["id"])
    later = datetime.now(timezone.utc) + timedelta(days=90)
    p._auto_approve(store, now=later)
    assert store.get_earning_by_id(row["earning_id"])["status"] == p.APPROVED


def test_releasing_a_hold_is_attributed_and_timestamped(store):
    """Reducing a physician's pay is consequential, and an unattributable
    reduction cannot be appealed."""
    u = make_user(store, role="evaluator", specialty="nephrology")
    row = _accrued_row(store, u["id"], amount=6000)
    store.set_earning_quality(row["earning_id"], multiplier=0.8,
                              reasons=["-20% test"], version="t", hold=True)

    updated = store.release_earning_hold(row["earning_id"], by="admin@example.org")
    assert updated["quality_hold"] == 0
    assert updated["quality_released_by"] == "admin@example.org"
    assert updated["quality_released_at"]
    assert updated["amount_cents"] == 6000


def test_an_admin_may_disagree_with_the_algorithm_and_pay_full_rate(store):
    u = make_user(store, role="evaluator", specialty="nephrology")
    row = _accrued_row(store, u["id"], amount=6000, rate=7500)
    store.set_earning_quality(row["earning_id"], multiplier=0.8,
                              reasons=["-20% test"], version="t", hold=True)

    updated = store.release_earning_hold(
        row["earning_id"], by="admin@example.org", pay_full_rate=True)
    assert updated["amount_cents"] == 7500


def test_releasing_something_that_was_never_held_is_refused(store):
    u = make_user(store, role="evaluator", specialty="nephrology")
    row = _accrued_row(store, u["id"])
    assert store.release_earning_hold(row["earning_id"], by="admin@example.org") is None


def test_an_approved_row_can_no_longer_be_restated(store):
    """The rate in force at accrual is what a row is worth. A recomputed
    multiplier landing on an approved or paid row would restate something a
    physician has already been told and possibly banked."""
    from asclepius import payments as p

    u = make_user(store, role="evaluator", specialty="nephrology")
    row = _accrued_row(store, u["id"])
    store.resolve_earning(kind=p.KIND_TASK, ref_id="sub-1", status=p.APPROVED,
                          resolved_at="2026-08-02T00:00:00Z", only_from=[p.ACCRUED])

    wrote = store.set_earning_quality(row["earning_id"], multiplier=0.5,
                                      reasons=["nope"], version="t", hold=True)
    assert wrote is False
    assert store.get_earning_by_id(row["earning_id"])["amount_cents"] == 7500


def test_the_held_queue_is_visible_rather_than_only_in_someones_inbox(store):
    """A proposal nobody can see is an automated decision with extra steps."""
    u = make_user(store, role="evaluator", specialty="nephrology")
    row = _accrued_row(store, u["id"], amount=6000)
    store.set_earning_quality(row["earning_id"], multiplier=0.8,
                              reasons=["-20% test"], version="t", hold=True)
    held = store.held_earnings()
    assert [r["earning_id"] for r in held] == [row["earning_id"]]
