"""Gap U2, the frontend half: the attestation and the reject control.

``frontend/asclepius/asclepius.js`` is 14k lines and is a conflict magnet owned
by a later PR, so the edit that added this surface is three small localized
pieces: one new card function, one line adding it to the confidence section's
parts array, one clause in the submit gate, plus the payload field. These tests
inspect those pieces at the source level, which is the same discipline
``test_asclepius_batches_ui`` and ``test_review_tier``'s DOM assertions use for
this file.

What is pinned here is not the copy. It is the three things that make the
attestation fair to enforce: the physician is TOLD what attesting means, the
reject control is right beside it and is never disabled, and the checkbox blocks
the label without ever blocking the rejection.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_FRONTEND = Path(__file__).resolve().parent.parent.parent / "frontend" / "asclepius"
JS = (_FRONTEND / "asclepius.js").read_text(encoding="utf-8")
CSS = (_FRONTEND / "asclepius.css").read_text(encoding="utf-8")


def _fn(src: str, name: str) -> str:
    """The body of one top-level function, by brace matching."""
    start = src.index(f"function {name}(")
    depth, i = 0, src.index("{", start)
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError(f"unterminated {name}")


CARD = _fn(JS, "renderClinicalValidityCard")
REJECT = _fn(JS, "rejectCaseAsInvalid")
GATE = _fn(JS, "updateSubmitState")
PAYLOAD = _fn(JS, "buildSubmissionPayload")
CONFIDENCE = _fn(JS, "renderConfidenceSection")


# ─── The card is on the labeling surface ─────────────────────────────────────
def test_the_attestation_card_is_mounted_on_the_case_labeling_surface():
    """A card nothing composes is a card nobody sees. The confidence section is
    where the physician commits, so the parts array above it is the mount."""
    assert "renderClinicalValidityCard()" in CONFIDENCE


def test_the_attestation_sits_above_the_submit_button_and_not_inside_it():
    """Wedging it into the commit moment would turn a signed assertion into one
    more box somebody clears on the way past, which is the same reasoning the
    decisive-action card is placed on."""
    parts = CONFIDENCE[CONFIDENCE.index("const parts ="):]
    order = parts[:parts.index("]")]
    assert order.index("renderClinicalValidityCard") < order.index("confidenceCard")


def test_the_physician_is_told_what_attesting_costs_them():
    """The whole point of the attestation is that responsibility moves. A
    checkbox that does not say so is a checkbox nobody agreed to the meaning
    of, and the meeting was explicit that the doctor signs a statement."""
    low = CARD.lower()
    assert "clinically valid" in low
    assert "not paid" in low          # the consequence, in the physician's words
    assert "on you" in low            # where responsibility lands


def test_rejecting_is_offered_beside_attesting_and_costs_nothing():
    """"They X out invalid cases" is the honest path, and it is only fair to
    hold a physician to an attestation because rejecting is free. A doctor who
    has to hunt for the honest path takes the dishonest one."""
    assert "not clinically valid" in CARD.lower()
    assert "rejectCaseAsInvalid" in CARD
    low = CARD.lower()
    assert "costs you nothing" in low
    assert "never counts against" in low


def test_the_reject_control_is_never_disabled_by_the_attestation():
    """The checkbox gates the LABEL. If the same state could disable the reject
    button, an unattested physician would have no way out of a broken case."""
    assert "disabled" not in REJECT
    reject_btn = CARD[CARD.index("asc-validity-reject") - 400:
                      CARD.index("asc-validity-reject") + 200]
    assert "disabled" not in reject_btn


# ─── The gate ────────────────────────────────────────────────────────────────
def test_submit_is_blocked_until_the_case_is_attested_or_rejected():
    """The meeting put the attestation BEFORE labeling. Client-side this is the
    only place a staged gate can be expressed, and the hint has to name both
    ways out rather than only the one we would prefer."""
    assert "attest_clinically_valid !== true" in GATE
    clause = GATE[GATE.index("attest_clinically_valid !== true"):][:400]
    assert "attest" in clause and "reject" in clause


def test_the_gate_only_applies_where_the_card_renders():
    """The card returns null outside V3/V4, so gating submit on it anywhere else
    would disable the button under a surface that never shows the checkbox."""
    assert "if (!isV3()) return null;" in CARD
    clause = GATE[GATE.index("attest_clinically_valid !== true") - 200:
                  GATE.index("attest_clinically_valid !== true")]
    assert "isV3()" in clause


# ─── The payload ─────────────────────────────────────────────────────────────
def test_the_attestation_is_sent_as_a_tri_state_and_never_coerced():
    """None means this client asserted nothing, which the server keeps distinct
    from an explicit false. A `!!` here would turn "did not say" into "said no"
    on every legacy draft resumed after this shipped, and only an explicit
    assertion can ever be found false later."""
    block = PAYLOAD[PAYLOAD.index("prompt_review: {"):]
    block = block[:block.index("independent_answer")]
    assert "attest_clinically_valid" in block
    assert "typeof" in block and "'boolean'" in block
    assert "!!d.prompt_review.attest_clinically_valid" not in block


def test_rejecting_posts_directly_and_never_rides_the_gated_submit_path():
    """The staged submit path client-gates on required state (confidence_set,
    the attestation itself) that is still unset exactly where this card mounts,
    so routing the rejection through submitEvaluation() made the button a
    silent no-op. Rejecting mirrors flagPrompt: a direct POST to /submissions,
    where the backend's Stage-1 flag branch runs before it validates a verdict
    or a rubric, so a half-filled case rejects cleanly and produces zero
    records. No second bespoke endpoint: one definition of what a rejection
    does."""
    assert "'flagged'" in REJECT
    assert "'/submissions'" in REJECT
    assert "buildSubmissionPayload" in REJECT
    # The two client gates that swallowed the click must never come back.
    assert "submitEvaluation" not in REJECT
    assert "confidence_set" not in REJECT
    # And it finishes like a flag does: the draft dies and the next task loads,
    # instead of the case sitting half-rejected in the workspace.
    assert "clearDraft" in REJECT
    assert "renderEvalView" in REJECT


def test_unchecking_withdraws_the_attestation_instead_of_asserting_false():
    """Check-then-uncheck means "I am no longer asserting", not "I assert the
    opposite". Recording an explicit false there would invent a statement a
    finding could later be made against, and nulling the Stage-1 verdict would
    erase the prompt-gate answer the physician DID give."""
    assert "box.checked ? true : null" in CARD
    assert re.search(r"pr\.verdict = box\.checked \? 'valid' :", CARD)
    assert "verdictBeforeAttest" in CARD


# ─── Paint ───────────────────────────────────────────────────────────────────
def test_every_class_the_attestation_card_emits_has_a_rule():
    """The repo-wide scanner only catches styled-but-never-emitted. This is the
    half nothing else checks, and a class with no rule is invisible in review
    and invisible on screen."""
    emitted = set(re.findall(r"class: '([^']+)'", CARD))
    names = {c for blob in emitted for c in blob.split() if c.startswith("asc-")}
    assert names, "the card emitted no asc- classes at all"
    missing = [c for c in sorted(names) if f".{c}" not in CSS]
    assert not missing, f"classes with no CSS rule: {missing}"


def test_the_reject_control_is_not_painted_as_a_safety_event():
    """Pink is reserved for safety events in this stylesheet. Rejecting a case
    is the correct, expected, unpenalised answer to a bad case, and painting it
    as an alarm would tell the physician the opposite of what the copy says."""
    block = CSS[CSS.index(".asc-validity-row"):]
    block = block[:block.index("/* ─── §2 info-dot")]
    assert "--pink" not in block, block
