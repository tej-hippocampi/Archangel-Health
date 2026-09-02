"""The practice case as a vetting signal, and the guardrails that make it one.

The meeting asked for the practice case to feed the decision about a physician,
which it did not: the result was written to the tutorial record and read by
nobody. The tempting version of this is a bonus bolted onto the score. That
would be the one change most likely to quietly break the fairness posture of
this model, because an unprincipled term is exactly what an adverse-impact
audit cannot follow.

So it is a real feature: a row in FEATURES with a prior, a real weight row
carried through the learning update under the same clamp as every other term,
and a stated encoding. What this file pins is that it behaves like one.
"""

from __future__ import annotations

import pytest

from tests._asclepius import fresh_store, make_user

from asclepius import capabilities as caps
from asclepius import tiering

from tests.test_tiering_score import (
    VALID_NPI, _passed_calibration, _physician,
)

FEATURE = "practice_first_pass"


def _with_gate(store, user, **gate):
    """Put a practice-case gate blob on the user the way a submit would."""
    state = store.get_tutorial_state(user["id"]) or {}
    state["gate"] = gate
    store.set_tutorial_state(user["id"], state)
    return store.get_user_by_id(user["id"])


# ─── The encoding ────────────────────────────────────────────────────────────
def test_a_first_time_pass_encodes_one_and_everything_else_encodes_zero():
    """Capped binary, deliberately. Retry count measures interruption, a reload
    and interface familiarity at least as much as judgment, and the gate forces
    an eventual pass regardless, so counting attempts would mostly encode who
    had a quiet afternoon."""
    store = fresh_store()
    user = _physician(store)

    first = _with_gate(store, user, state=caps.GATE_PASSED,
                       attempts=1, first_attempt_pass=True)
    assert tiering.feature_vector(
        first, practice_first_pass=caps.practice_first_pass(first))[FEATURE] == 1.0

    later = _with_gate(store, user, state=caps.GATE_PASSED,
                       attempts=4, first_attempt_pass=False)
    assert tiering.feature_vector(
        later, practice_first_pass=caps.practice_first_pass(later))[FEATURE] == 0.0


def test_never_sat_and_failed_first_look_the_same_to_the_model():
    """Absence of evidence and evidence of a miss are both "no positive signal
    here". A third state would give the model something to fit that we cannot
    actually observe."""
    store = fresh_store()
    unsat = _physician(store)
    failed = _with_gate(store, _physician(store), state=caps.GATE_LOCKED,
                        attempts=2, first_attempt_pass=False)

    for u in (unsat, failed):
        assert tiering.feature_vector(
            u, practice_first_pass=caps.practice_first_pass(u))[FEATURE] == 0.0


def test_a_grandfathered_account_reads_false_rather_than_true():
    """Those accounts predate the practice case, so there is no first attempt
    to have passed. Reading them as a pass would invent a signal."""
    store = fresh_store()
    old = _with_gate(store, _physician(store), state=caps.GATE_GRANDFATHERED,
                     source="migration")
    assert caps.practice_first_pass(old) is False


def test_the_stamp_is_not_recomputed_from_a_climbing_attempt_count():
    """A physician who passed first time and later reopened the case for
    practice must still read as a first-time pass: attempts keeps climbing, so
    the signal has to be the stamp written on the day."""
    store = fresh_store()
    replayed = _with_gate(store, _physician(store), state=caps.GATE_PASSED,
                          attempts=7, first_attempt_pass=True)
    assert caps.practice_first_pass(replayed) is True


# ─── The guardrails ──────────────────────────────────────────────────────────
def test_it_is_a_real_weight_row_and_not_a_bonus():
    """A term outside the weight table is a term the learning update cannot
    correct and an auditor cannot trace."""
    assert FEATURE in tiering.FEATURES
    assert FEATURE in tiering.ALL_WEIGHT_NAMES
    assert FEATURE in tiering.default_weights()

    m, q = tiering.FEATURES[FEATURE]
    assert 0 < m < 1.0, "a work sample is mild evidence, not a promotion on its own"
    assert q <= 4.00, "the prior must be loose enough for admin decisions to correct it"


def test_it_reads_from_the_tutorial_record_and_never_from_credentials():
    """The encoder's forbidden-key rule is what keeps protected proxies out of
    this model, and a new feature is exactly when someone reaches for a
    credential blob by habit."""
    assert FEATURE not in tiering.FORBIDDEN_CREDENTIAL_KEYS
    assert not (set(tiering.FEATURES) & tiering.FORBIDDEN_CREDENTIAL_KEYS)


def test_it_cannot_open_a_hard_gate():
    """Hard gates are the things no score may buy its way past. A work sample
    is evidence about judgment, not about whether somebody holds a licence."""
    store = fresh_store()
    user = _with_gate(store, _physician(store), state=caps.GATE_PASSED,
                      attempts=1, first_attempt_pass=True)
    gates = tiering.hard_gates(user, leie_status=store.leie_status(VALID_NPI))
    assert FEATURE not in str(gates)


def test_every_pinned_proxy_still_moves_by_exactly_zero_alongside_it():
    """The whole point of spending the ninth slot carefully: adding a feature
    must not disturb the immobility proof that is handed to a bias auditor."""
    store = fresh_store()
    before = tiering.default_weights()
    for name in tiering.PINNED_ZERO:
        assert before[name]["m"] == 0.0
    assert FEATURE in before


def test_the_admin_sees_it_in_words():
    """An admin who cannot see why will ignore the score."""
    from asclepius.tiering import _FEATURE_WORDS
    assert FEATURE in _FEATURE_WORDS
    assert "practice case" in _FEATURE_WORDS[FEATURE]
