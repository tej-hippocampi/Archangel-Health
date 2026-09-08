"""Unknown board validity is unanswered, and stays unanswered all the way down.

Onboarding Master PRD, Phase 3 — PRD C §6-E, and §2 invariant 1.

THE BUG, end to end. The parser emits ``active: None`` for a board certification,
because "is this certification currently valid" is a compliance answer about
today and a document written last year cannot give it. That ``None`` became
``false`` in the wizard, and ``YesNoToggle`` renders ``false`` as a SELECTED
"No" — so every physician who uploaded a CV was shown a negative attestation,
already made on their behalf, on every certification they hold. Manually added
rows had the opposite defect: they defaulted to ``true``, an affirmative nobody
gave.

Changing the UI type alone would have been unsafe, which is why this file exists
rather than a one-line diff. The scoring path read
``_truthy(active) is not False`` — so an unanswered row scored exactly like a
confirmed one, and moving the wizard to ``null`` without touching that would have
turned "we never asked" into "yes, currently certified" for every applicant at
once. §6-E names the shape and this pins both halves together.

The split §6-E asks for, and which is the thing to hold on to when reading this:

  * a CLAIM — this physician holds a certification in nephrology — is one fact;
  * CONFIRMED CURRENT VALIDITY — and it is in date today — is a different one.

``feature_vector``'s ``board_certified_active`` is the second and now requires an
explicit Yes. ``domain_match`` and ``propose_tier``'s board weight are the first
and are deliberately unchanged, so no physician loses credit they already had.
"""

from __future__ import annotations

import json
import pathlib

from asclepius import tiering
from asclepius import credentialing

_LANDING = pathlib.Path(__file__).resolve().parents[2] / "landing" / "src" / "app" / "components"
_STEPS = (_LANDING / "onboarding" / "steps.tsx").read_text(encoding="utf-8")
_WIZARD = (_LANDING / "OnboardingWizard.tsx").read_text(encoding="utf-8")
_PRIMITIVES = (_LANDING / "onboarding" / "primitives.tsx").read_text(encoding="utf-8")


def _user(*certs, board_cert_text: str = "", specialty: str = "nephrology"):
    """A physician row shaped the way the scorer reads one."""
    return {
        "specialty": specialty,
        "board_cert": board_cert_text,
        "credentials_json": json.dumps({"boardCertifications": list(certs)}),
    }


def _cert(board="ABIM", specialty="Internal Medicine", subspecialty="", active=None):
    return {"board": board, "specialty": specialty,
            "subspecialty": subspecialty, "active": active}


def _board_active(user) -> float:
    return tiering.feature_vector(user, case_domain=None).get("board_certified_active", 0.0)


# ── The wizard never answers for them ───────────────────────────────────────

def test_a_new_row_starts_unanswered():
    assert 'active: null }' in _STEPS or "active: null," in _STEPS
    assert "active: true" not in _STEPS, "a row that arrives saying Yes is an attestation"


def test_the_cv_import_writes_unanswered_rather_than_no():
    assert "active: null" in _WIZARD
    assert "active: false" not in _WIZARD, "false is an answer nobody gave"
    assert "active: true" not in _WIZARD


def test_the_toggle_can_represent_unanswered():
    """It already could — `value: boolean | null` — and `false` visibly selects
    No. That is why writing `false` was never a neutral placeholder."""
    assert "value: boolean | null;" in _PRIMITIVES
    assert "const active = value === opt.v;" in _PRIMITIVES


def test_the_type_carries_the_third_state():
    assert "active: boolean | null;" in _STEPS


# ── The scorer requires an explicit yes ─────────────────────────────────────

def test_an_unanswered_certification_earns_no_active_credit():
    """THE REGRESSION THIS FILE EXISTS TO PREVENT. Before, `is not False` meant
    silence scored the same as a confirmed Yes — so moving the wizard to null
    without this would have promoted every unanswered applicant at once."""
    assert _board_active(_user(_cert(active=None))) == 0.0


def test_an_explicit_no_earns_no_active_credit():
    assert _board_active(_user(_cert(active=False))) == 0.0


def test_an_explicit_yes_earns_the_credit():
    assert _board_active(_user(_cert(active=True))) == 1.0


def test_a_certification_with_no_board_named_earns_nothing():
    assert _board_active(_user(_cert(board="", active=True))) == 0.0


def test_legacy_board_text_no_longer_manufactures_an_active_certification():
    """The OR fallback §6-E names. ``users.board_cert`` is a text projection of
    the FIRST board row's name, written at signup and never consulting `active`
    at all — so typing a board name granted "active certification" even to
    somebody who had explicitly answered No."""
    assert _board_active(_user(board_cert_text="ABIM Nephrology")) == 0.0
    assert _board_active(
        _user(_cert(active=False), board_cert_text="ABIM Nephrology")) == 0.0


def test_the_encoder_no_longer_declares_a_column_it_stopped_reading():
    assert "board_cert" not in tiering.ENCODER_USER_COLUMNS
    assert "specialty" in tiering.ENCODER_USER_COLUMNS


# ── The claim signal is a different fact and does not move ──────────────────

def test_an_unanswered_subspecialty_still_matches_its_domain():
    """CLAIM, not current validity. An unanswered validity question does not
    make a nephrology subspecialty stop being a nephrology subspecialty, and
    §6-E's whole instruction is to separate the two rather than delete one."""
    user = _user(_cert(subspecialty="nephrology", active=None))
    score, why = tiering.domain_match(user, "nephrology")
    assert score == 1.0
    assert "subspecialty" in why


def test_an_explicit_no_is_excluded_from_the_domain_match():
    """A physician who says the certification is not current has told us
    something about it, and that answer is respected."""
    user = _user(_cert(subspecialty="nephrology", active=False))
    score, _ = tiering.domain_match(user, "nephrology")
    assert score < 1.0


def test_the_tier_proposal_keeps_its_board_claim_credit():
    """NOTHING IS REVOKED. §6-E forbids retroactively removing access, and this
    is the weight that would have done it: `propose_tier` awards its
    `board_certified` points from the claimed board text, exactly as before, so
    a physician who had that credit yesterday has it today."""
    proposal = credentialing.propose_tier({
        "board_cert": "American Board of Internal Medicine, Nephrology",
        "specialty": "nephrology",
        "credentials_json": json.dumps({"boardCertifications": [_cert(active=None)]}),
    })
    assert any("board certified" in r for r in proposal["reasons"]), proposal["reasons"]


# ── Nothing stored is rewritten ─────────────────────────────────────────────

def test_no_migration_rewrites_a_stored_answer():
    """ADDITIVE ONLY (§2 invariant 3, §6-E). An existing `true` keeps its credit
    and an existing `false` keeps its refusal; only NEW rows start null. The
    alternative — sweeping old values to null — would erase explicit human
    answers, and sweeping them to confirmed would invent them."""
    assert _board_active(_user(_cert(active=True))) == 1.0
    assert _board_active(_user(_cert(active=False))) == 0.0


def test_the_reviewer_gate_reads_the_corrected_signal():
    """`tr_eligibility` requires an active board certification. It now means
    what it says: a physician nobody asked is "we cannot decide this one", which
    is the admin band, and the module's own docstring says that is the correct
    destination for exactly that."""
    gates = {"eligible": True}
    unanswered = tiering.tr_eligibility(
        _user(_cert(active=None)), gates,
        tiering.feature_vector(_user(_cert(active=None)), case_domain="nephrology"),
        calibration_passed=True)
    assert "active board certification" in unanswered["missing"]
