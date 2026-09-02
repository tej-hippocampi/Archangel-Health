"""TL / TR classification — hard gates, a nine-parameter score, and the learning loop.

**Credentials are scaffolding.** Everything in the scored layer below exists to answer one
question — *"is this physician a reliable adjudicator on THIS case's domain?"* — during the
window before we have measured their work. Structured implicit peer review of clinical care
reaches κ ≈ 0.5, which is "adequate for aggregate comparisons but inadequate for reliable
evaluations of single patients using one or two reviewers": reliability comes from the rubric
and the structure, not from the person. Every serious annotation operator tiers primarily on
measured agreement with gold, not on credentials. So this module is built to make its own
credential features matter LESS over time — see ``CREDENTIAL_DECAY`` and §5.4 below.
**Do not reinstate the credential weights when measured quality is available. That is the point.**

─── The three findings that shape the design ──────────────────────────────────────────────

1. **TR is a per-domain role, not a seniority rank.** The nearest real-world analogue, a
   clinical trial's Clinical Events Committee, selects for "expertise in the disease or
   condition and treatments studied" — not general eminence. Clinical expertise is stored as
   domain-specific illness scripts. *A cardiologist is a novice on a nephrology case.* So the
   unit of decision is ``P(TR | physician, case domain)``, never ``P(TR | physician)``, and
   ``domain_match`` is both a scored feature AND a hard clause of TR eligibility.

2. **Years in practice is a bad feature and a legal liability.** The Choudhry systematic review
   found an *inverse* relationship between years in practice and quality of care, and it is
   simultaneously the most direct available proxy for age. It appears here exactly once, as the
   capped binary ``post_residency_ge_3yr``. There is no continuous years term and there must
   never be one.

3. **Credentials are a cold-start prior, not the answer.** See the paragraph above.

─── The architecture, in one paragraph ────────────────────────────────────────────────────

``hard_gates()`` decides *eligibility* — policy, seven rules, never learned, never overridable
by a score. ``feature_vector()`` encodes a physician-and-case into nine numbers, refusing by
construction to read any protected proxy. ``score()`` ranks *within* the eligible set.
``propose()`` composes the three and returns a tier proposal with plain-word reasons ranked by
contribution. ``apply_decision_batch()`` folds admin overrides back into the weights by
regularized Bayesian logistic regression, with four guardrails that are asserted rather than
assumed. The score PROPOSES; a human DECIDES; the human's decision is the training signal.

─── The honest ceiling (PRD C §5.5) ───────────────────────────────────────────────────────

After ~100 admin decisions this will have meaningfully sharpened the 3–5 features that vary
most in the applicant pool, and barely moved the rare ones. **It will not discover a criterion
that is not encoded here.** It corrects the relative weighting of criteria that are — which is
exactly the thing hand-tuned rules get wrong — and that is the ceiling of a nine-parameter
model on a hundred labels. Anyone expecting more will be disappointed by a working system.
"""

from __future__ import annotations

import json
import logging
import math
import random
import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from asclepius import capabilities as caps
from asclepius import credentialing
from asclepius import specialties as asc_specialties

log = logging.getLogger("asclepius.tiering")

# ─── Tier vocabulary ─────────────────────────────────────────────────────────────────────
# capabilities.TIERS is THE enumeration (context pack §3.3). This module does not write a
# second list of tier strings; it aliases the two rungs it is allowed to propose.
TL = caps.LABELER      # task labeler — builds a case answer from scratch
TR = caps.REVIEWER     # task reviewer — grades what two TLs produced
# 'advisor' is deliberately absent. An advisory relationship is negotiated and carries equity;
# it is not the output of an NPI lookup. propose() raises if a future edit ever routes to it.


# ═══════════════════════════════════════════════════════════════════════════════════════════
# §3.2 — Features and priors
# ═══════════════════════════════════════════════════════════════════════════════════════════
# m = prior mean (the hand-written rule), q = prior precision (1/variance).
#
# THE RULE IS THE PRIOR. A rule we are sure of gets high precision and barely moves; a rule we
# are guessing at gets low precision and is corrected fast by the data. That single idea is
# what makes this a learning system rather than a weights table someone edits by feel.
#
# Nine scored features plus an intercept. The budget is not arbitrary: with ~100 admin
# decisions, eight was ~12 events per variable, the edge of what a logistic model can fit
# without inventing structure. Spending the ninth on ``practice_first_pass`` is a deliberate
# purchase, made for the same reason ``calibration_z`` was: it is a WORK SAMPLE rather than a
# credential, and every other term in here is a proxy for the thing this one measures
# directly. Every applicant now produces it, so unlike the calibration exam it is dense.
FEATURES: Dict[str, Tuple[float, float]] = {
    "intercept":              (-2.5, 1.00),   # TR is the minority role, deliberately
    "board_certified_active": ( 1.2, 6.25),
    "subspecialty_certified": ( 0.9, 4.00),   # ONLY counts when domain_match > 0 — see below
    "domain_match":           ( 1.6, 4.00),   # 1.0 subspecialty · 0.5 specialty · 0.0 neither
    "post_residency_ge_3yr":  ( 0.5, 6.25),   # capped binary — NEVER continuous (finding 2)
    "currently_practicing":   ( 0.8, 11.1),   # >=4 clinical half-days/month, last 12 months
    "continuing_cert":        ( 0.4, 11.1),
    "structured_review_exp":  ( 0.7, 6.25),   # CEC/DSMB, journal peer review, board item
                                              # writing, guideline panel, core faculty or PD
    "calibration_z":          ( 1.1, 4.00),
    "practice_first_pass":    ( 0.4, 4.00),   # capped binary — first attempt only (see below)
}

# ``practice_first_pass`` is 1.0 only when the applicant passed the practice case on their
# FIRST attempt, and 0.0 otherwise. Two things follow from that shape, both deliberate.
#
# It is not continuous in attempts. Retry count measures interruption, a browser reload and
# how familiar somebody is with the interface at least as much as it measures judgment, and
# the gate forces an eventual pass regardless, so counting attempts would mostly encode who
# had a quiet afternoon.
#
# The prior is modest (0.4) and the precision low-ish (4.00), which says: we believe a clean
# first pass is mild evidence of a good reviewer, and we are ready to be corrected quickly if
# the admin decisions disagree. It cannot open a hard gate, and it is not a substitute for
# board certification or domain match, both of which carry far more weight.

# Outside the eight-feature budget, so it does not consume events-per-variable. Late-binding:
# it is structurally zero until a physician has enough completed work to estimate accuracy
# from, at which point it is the strongest term in the model — by design.
MEASURED_QUALITY_FEATURE = "measured_quality_z"
MEASURED_QUALITY: Tuple[float, float] = (2.0, 1.56)

# §3.3 — protected proxies, pinned at zero forever.
#
# These are instantiated as REAL features with m = 0.0 and q = PINNED_PRECISION, persisted as
# real rows in tiering_weights, and passed through the learning update on every batch — where
# they are asserted to have moved by exactly 0.0. That is the difference between a promise and
# a proof. It is also the concrete artifact you hand a bias auditor: not "we don't use sex",
# but "here is the row, here is its weight, here is the test that fails if it ever changes".
PINNED_ZERO: Tuple[str, ...] = (
    "sex", "img_status", "grad_year", "med_school_tier",
    "practice_region", "age", "name_origin",
)
PINNED_PRECISION = 1e6

# Credential-record keys that must never be read by the encoder. Kept separate from
# PINNED_ZERO: that pins a feature that exists; this prevents a feature from being created.
FORBIDDEN_CREDENTIAL_KEYS: frozenset = frozenset({
    "medicalSchool", "medicalSchoolName", "medicalSchoolYear", "medSchool", "medSchoolTier",
    "gradYear", "graduationYear", "dateOfBirth", "dob", "birthDate", "age",
    "sex", "gender", "ecfmgCertified", "imgStatus", "isImg", "usmleScore",
    "practiceZip", "zip", "zipCode", "practiceRegion", "nameOrigin",
    "selfRatedExpertise", "confidence",   # physician confidence barely moves between easy and
                                          # hard cases — uninformative to inversely informative
})

ALL_WEIGHT_NAMES: Tuple[str, ...] = tuple(FEATURES) + (MEASURED_QUALITY_FEATURE,) + PINNED_ZERO

# §3 thresholds on s = Σ mᵢxᵢ, with P(TR) = σ(s).
TR_THRESHOLD = 1.0
TL_THRESHOLD = -1.0

# §5.4 hand-off to measured quality.
MEASURED_QUALITY_MIN_TASKS = 20    # below this, measured_quality_z is structurally 0.0
CREDENTIAL_DECAY = 0.6             # multiply credential features by this...
DECAY_AT_N_TASKS = 50              # ...once the physician has this many completed tasks

# Which features are "credentials" for the purposes of the decay above. domain_match is NOT
# among them: domain is a property of the case-physician pair and stays load-bearing forever
# (finding 1). calibration_z is a work sample, not a credential, and also stays.
CREDENTIAL_FEATURES: Tuple[str, ...] = (
    "board_certified_active", "subspecialty_certified", "post_residency_ge_3yr",
    "currently_practicing", "continuing_cert", "structured_review_exp",
)

# §5.3 exploration.
EXPLORATION_RATE = 0.15

# §5.2 guardrails.
MAX_DELTA_M = 0.25       # per batch, per feature
NEWTON_STEPS = 40
# Not in the PRD, added deliberately (see PROCESS REVIEW note in the module docstring of
# apply_decision_batch): precision only ever grows under the stated update, so after enough
# batches every feature freezes and a genuine shift in admin policy can never be tracked. A
# model that has frozen looks exactly like a model that has converged. Capping q at 400
# (σ = 0.05) keeps the tail of the learning curve alive without letting one noisy batch swing
# a well-established weight.
Q_MAX = 400.0


# ═══════════════════════════════════════════════════════════════════════════════════════════
# §2 — Hard gates. Rules, never learned. The score cannot open one.
# ═══════════════════════════════════════════════════════════════════════════════════════════
# Tri-state, for the same reason verify_npi is tri-state: "we could not check" is not "this
# person failed". A gate that collapses UNKNOWN into FAIL rejects real physicians on the day
# an upstream registry is slow; a gate that collapses UNKNOWN into PASS is not a gate.

PASS = "pass"
FAIL = "fail"
UNKNOWN = "unknown"

GATE_LABELS: Dict[str, str] = {
    "A1": "NPI verified against NPPES (individual, active, family name matches)",
    "A2": "State licence number + state on file",
    "A3": "Degree MD / DO / IMG-equivalent",
    "A4": "Residency complete — not currently in training",
    "A5": "Not on the OIG LEIE exclusion list",
    "A6": "No active board disciplinary action",
    "A7": "Signed confidentiality + independence attestation",
}

_DEGREES_OK = {
    "md", "do", "mbbs", "mbchb", "mb bs", "mb chb", "bmbs", "mbbch", "md/phd", "do/phd",
    "m.d.", "d.o.", "mbbs (img)", "img", "img-equivalent",
    # Primary medical qualifications that are not called any of the above.
    # Germany awards no degree in the usual sense: physicians finish with the
    # Staatsexamen and practise on the Approbation, while "Dr. med." is an
    # optional research thesis. "MD" also means different things by country --
    # the primary qualification across much of Europe, a POSTGRADUATE specialty
    # degree in India -- so this list accepts the words doctors actually hold
    # and leaves judging them to A1 and the registry behind it.
    "staatsexamen", "approbation", "mbchb (img)", "bm", "bm bch", "mb bchir",
    "cand med", "licenciado en medicina", "laurea in medicina", "mudr",
}


def _creds(user: Dict[str, Any]) -> Dict[str, Any]:
    return credentialing._json_field(user, "credentials_json")


def _attest(user: Dict[str, Any]) -> Dict[str, Any]:
    return credentialing._json_field(user, "attestations_json")


def _truthy(value: Any) -> Optional[bool]:
    """Tri-state coercion. ``None`` for "not answered" — which is NOT False. A checkbox that
    was never rendered and a checkbox the physician declined are different facts."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    low = str(value).strip().casefold()
    if low in ("true", "yes", "y", "1", "on", "complete", "completed"):
        return True
    if low in ("false", "no", "n", "0", "off"):
        return False
    return None


def _int_or_none(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def hard_gates(
    user: Dict[str, Any],
    *,
    leie_status: Optional[str] = None,
    duplicate_npi: bool = False,
    now_year: Optional[int] = None,
) -> Dict[str, Any]:
    """A1..A7. Returns ``{"gates": {id: {state, label, detail}}, "eligible", "blocked",
    "undetermined"}``.

    ``eligible`` is True only when **all seven** are PASS. One FAIL ⇒ eligible for neither
    tier. Any UNKNOWN ⇒ not eligible, but ``blocked`` is False — the difference between "this
    person does not qualify" and "we have not finished checking", which the admin queue must be
    able to tell apart.

    ``leie_status`` is supplied by the caller (``store.leie_status``) rather than fetched
    here, so this stays a pure function and I/O lives in exactly one place — the same rule
    ``credentialing`` follows for NPPES.
    """
    creds = _creds(user)
    attest = _attest(user)
    now_year = now_year or datetime.utcnow().year
    g: Dict[str, Dict[str, Any]] = {}

    def put(gid: str, state: str, detail: str) -> None:
        g[gid] = {"state": state, "label": GATE_LABELS[gid], "detail": detail}

    # ── A1 · Primary medical-registry identity ──────────────────────────────────────────
    # Reads the persisted tri-state, never re-derives it. npi_verified is 1 / 0 / NULL and
    # NULL genuinely means "not checked" — collapsing it here would undo the whole of §1.1.
    #
    # For a doctor licensed outside the US the answering registry is theirs, not NPPES, and
    # the outcomes do not map one-to-one. A miss in a register that admits it is incomplete
    # (INCONCLUSIVE), or a country with no queryable register at all (DOCUMENT_ONLY), is
    # UNKNOWN — not yet established — and lands in the admin queue with the certificate to
    # read. Neither may ever be a FAIL: that would reject a real physician for the sins of
    # someone else's database.
    npi_flag = user.get("npi_verified")
    npi_payload = credentialing._json_field(user, "npi_payload_json")
    licensure = str(user.get("country_of_licensure") or "US").strip().upper() or "US"
    if licensure == "US":
        if duplicate_npi:
            put("A1", FAIL, "Another account already claims this NPI")
        elif npi_flag == 1:
            put("A1", PASS, "NPPES: individual, active, family name matches")
        elif npi_flag == 0:
            put("A1", FAIL, f"NPPES: {npi_payload.get('reason') or 'no corroborating record'}")
        else:
            put("A1", UNKNOWN, "NPI not yet checked — registry unreachable or check pending")
    else:
        registry_payload = credentialing._json_field(user, "registry_payload_json")
        registry_name = registry_payload.get("registry") or "the national registry"
        outcome = registry_payload.get("result")
        registry_flag = user.get("registry_verified")
        if duplicate_npi:
            put("A1", FAIL, "Another account already claims this registration number")
        elif registry_flag == 1:
            put("A1", PASS, f"{registry_name}: registration corroborates the signup")
        elif outcome == "mismatch":
            put("A1", FAIL,
                f"{registry_name}: {registry_payload.get('reason') or 'record does not corroborate'}")
        elif outcome == "not_found":
            put("A1", FAIL, f"{registry_name}: no registration record")
        elif outcome == "document_only":
            put("A1", UNKNOWN,
                f"{registry_name} has no register we can query — verify from the uploaded document")
        elif outcome in ("inconclusive", "unavailable"):
            put("A1", UNKNOWN,
                f"{registry_name} could not confirm ({registry_payload.get('reason') or 'no match'})"
                " — routed to document review")
        else:
            put("A1", UNKNOWN, f"{registry_name} check pending")

    # ── A2 · State licence ──────────────────────────────────────────────────────────────
    # NPPES returns taxonomies[].license and .state for free, so the cross-check costs nothing.
    # "Active and unrestricted" is verified out of band by a human; the attestation records
    # that they claimed it, and the state board record is what an admin actually opens.
    lic = str(creds.get("licenseNumber") or "").strip()
    lic_state = str(creds.get("licenseState") or "").strip().upper()
    reg_tax = (npi_payload.get("record") or {}).get("taxonomy") or {}
    reg_lic = str(reg_tax.get("license") or "").strip()
    reg_state = str(reg_tax.get("state") or "").strip().upper()
    if licensure != "US":
        # There is no state licence to hold: registration IS the licence in most
        # of the world, and A1 already judged it. Asking twice would mean a
        # doctor could clear their own registry and still be marked incomplete
        # for lacking a document their country does not issue.
        registration = str(creds.get("registrationNumber") or user.get("registry_id") or "").strip()
        if not registration:
            put("A2", UNKNOWN, "No registration number on file")
        elif user.get("registry_verified") == 1:
            put("A2", PASS, f"Registration {registration} confirmed by the national registry")
        else:
            put("A2", UNKNOWN, f"Registration {registration} on file — pending registry or document review")
    elif not lic or not lic_state:
        put("A2", UNKNOWN, "No licence number / state on file")
    elif reg_lic and reg_state and (
            reg_lic.casefold() != lic.casefold() or reg_state != lic_state):
        # A divergence is a review flag, not a rejection: NPPES licence data is
        # self-reported by the provider and is frequently stale.
        put("A2", UNKNOWN,
            f"Licence {lic} ({lic_state}) differs from NPPES {reg_lic} ({reg_state}) — verify")
    else:
        put("A2", PASS, f"Licence {lic} ({lic_state})")

    # ── A3 · Degree ─────────────────────────────────────────────────────────────────────
    # ECFMG certification satisfies this as ONE OF SEVERAL equivalent routes, never as a
    # score input (§3.3). An IMG and a US-MD clear A3 identically and are indistinguishable
    # downstream — feature_vector() cannot read img_status at all.
    degree = str(creds.get("degree") or "").strip()
    cred_word = str(((npi_payload.get("record") or {}).get("credential")) or "").strip()
    if not degree:
        put("A3", UNKNOWN, "No degree recorded")
    elif degree.strip().casefold().replace(".", "").replace(" ", "") in {
            d.replace(".", "").replace(" ", "") for d in _DEGREES_OK}:
        put("A3", PASS, degree + (f" (NPPES: {cred_word})" if cred_word else ""))
    else:
        put("A3", FAIL, f"{degree} is not a physician degree we accept")

    # ── A4 · Residency complete ─────────────────────────────────────────────────────────
    # Attestation + year. The YEAR is consumed here and at post_residency_ge_3yr, and is
    # discarded at both — it never reaches a feature as a continuous value (finding 2).
    # Still in training is UNKNOWN, not FAIL. A resident or fellow is a doctor
    # who has not finished yet, and a completion year in the future is them
    # answering the question correctly. FAIL means "this record does not
    # qualify" and is read that way everywhere downstream, which would turn
    # every trainee into a rejected signup instead of a colleague who is not
    # reviewer-eligible yet. UNKNOWN keeps them out of the reviewer tier --
    # eligibility still needs every gate to PASS -- while leaving them a
    # perfectly good labeler.
    done = _truthy(creds.get("residencyCompleted"))
    end_year = _int_or_none(creds.get("residencyCompletionYear"))
    if done is False:
        detail = (f"In training; expects to finish in {end_year}" if end_year
                  else "Currently in training")
        put("A4", UNKNOWN, detail)
    elif done is None and end_year is None:
        put("A4", UNKNOWN, "Residency completion not attested")
    elif end_year is not None and end_year > now_year:
        put("A4", UNKNOWN, f"In training; expects to finish in {end_year}")
    else:
        put("A4", PASS, f"Residency complete{f' ({end_year})' if end_year else ''}")

    # ── A5 · OIG exclusion ──────────────────────────────────────────────────────────────
    # Free, monthly, and a hard gate. Tri-state for the same reason as A1: a LEIE file we
    # could not load is not a clean physician, and it is certainly not an excluded one.
    if leie_status == "excluded":
        put("A5", FAIL, "Listed on the OIG LEIE exclusion list")
    elif leie_status == "clear" and licensure == "US":
        put("A5", PASS, "Not on the OIG LEIE list")
    elif licensure != "US":
        # The LEIE is a list of people excluded from US federal health
        # programmes. Absence from it says nothing about a doctor who was never
        # in them, so this cannot PASS on their behalf — and being unresolved
        # here is precisely why an international signup always reaches a human.
        put("A5", UNKNOWN,
            "OIG LEIE is US-only — no equivalent exclusion check for this country")
    else:
        put("A5", UNKNOWN, "OIG LEIE list not loaded — cannot check")

    # ── A6 · Board discipline ───────────────────────────────────────────────────────────
    no_action = _truthy(attest.get("attestNoDisciplinaryAction"))
    if no_action is True:
        put("A6", PASS, "Attested: no active board disciplinary action")
    elif no_action is False:
        put("A6", FAIL, "Disclosed an active board disciplinary action")
    else:
        put("A6", UNKNOWN, "Disciplinary-action attestation not signed")

    # ── A7 · Confidentiality + independence ─────────────────────────────────────────────
    conf = _truthy(attest.get("attestConfidentiality"))
    indep = _truthy(attest.get("attestIndependentJudgment"))
    if conf is True and indep is True:
        put("A7", PASS, "Confidentiality + independent-judgment attestations signed")
    elif conf is False or indep is False:
        put("A7", FAIL, "Declined the confidentiality or independence attestation")
    else:
        put("A7", UNKNOWN, "Confidentiality / independence attestations not signed")

    states = [v["state"] for v in g.values()]
    return {
        "gates": g,
        "eligible": all(s == PASS for s in states),
        "blocked": any(s == FAIL for s in states),
        "undetermined": [k for k, v in g.items() if v["state"] == UNKNOWN],
        "failed": [k for k, v in g.items() if v["state"] == FAIL],
    }


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Domain match — the feature that outranks every general-seniority term
# ═══════════════════════════════════════════════════════════════════════════════════════════
# Config, not code: the served set comes from the specialty registry, so adding a specialty
# stays a registry edit. The stems map how the world writes a specialty ("Cardiovascular
# Disease" is how NPPES spells cardiology) onto how the registry names it.

_DOMAIN_STEMS: Dict[str, Tuple[str, ...]] = {
    "nephrology": ("nephrology", "nephrologist", "renal", "kidney", "dialysis",
                   "transplant nephrology"),
    "cardiology": ("cardiology", "cardiologist", "cardiovascular", "cardiac",
                   "electrophysiology", "heart failure", "interventional cardiology"),
    "oncology":   ("oncology", "oncologist", "hematology", "hematologist", "haematology",
                   "medical oncology", "neoplas"),
}


def normalize_domain(text: str) -> Optional[str]:
    """Map free text onto a served specialty, or None for "could not determine".

    None is never treated as a divergence and never as a match — the same discipline the
    credentialing module applies to unmapped NPPES taxonomy wording.
    """
    low = (text or "").strip().casefold()
    if not low:
        return None
    if low in _DOMAIN_STEMS and asc_specialties.is_enabled(low):
        return low
    for served, stems in _DOMAIN_STEMS.items():
        if not asc_specialties.is_enabled(served):
            continue
        if any(stem in low for stem in stems):
            return served
    return None


def domain_match(user: Dict[str, Any], case_domain: Optional[str]) -> Tuple[float, str]:
    """``P(TR | physician, CASE DOMAIN)`` starts here. Returns ``(value, why)``.

    * **1.0** — a *subspecialty* board certification in this domain. This is the CEC criterion:
      expertise in the condition studied.
    * **0.5** — the declared primary specialty or the NPPES primary taxonomy is this domain,
      without a subspecialty certification in it.
    * **0.0** — neither. A distinguished cardiologist scores 0.0 on a nephrology case, and that
      is the correct answer, not a bug in the encoding.
    """
    want = normalize_domain(case_domain or "")
    if want is None:
        return 0.0, "case domain not recognised"
    creds = _creds(user)

    # Subspecialty certification in the domain — the strongest available signal.
    for bc in (creds.get("boardCertifications") or []):
        if not isinstance(bc, dict):
            continue
        if _truthy(bc.get("active")) is False:
            continue
        sub = normalize_domain(str(bc.get("subspecialty") or ""))
        if sub == want:
            return 1.0, f"subspecialty board certification in {want}"
    # A fellowship in the domain is the same claim with a different evidence source.
    for fel in (creds.get("fellowship") or []):
        if isinstance(fel, dict) and normalize_domain(str(fel.get("specialty") or "")) == want:
            return 1.0, f"fellowship trained in {want}"

    # Specialty-level match.
    for source, label in (
        (str(creds.get("primarySpecialty") or ""), "declared primary specialty"),
        (str(user.get("specialty") or ""), "account specialty"),
        (str((credentialing._json_field(user, "npi_payload_json").get("record") or {})
             .get("taxonomy", {}).get("desc") or ""), "NPPES primary taxonomy"),
    ):
        if normalize_domain(source) == want:
            return 0.5, f"{label} is {want}"
    for sub in (creds.get("subspecialties") or []):
        if normalize_domain(str(sub)) == want:
            return 0.5, f"listed subspecialty {want}"

    return 0.0, f"no {want} expertise on file"


# ═══════════════════════════════════════════════════════════════════════════════════════════
# The encoder
# ═══════════════════════════════════════════════════════════════════════════════════════════

# The EXACT set of credential-record keys ``feature_vector`` reads. Declared rather than
# discovered so the import-time guard above can be a real check. Adding a read here without
# adding it to this set is a bug; adding a forbidden one fails the import.
ENCODER_CREDENTIAL_KEYS: frozenset = frozenset({
    "boardCertifications",          # -> board_certified_active, subspecialty_certified
    "fellowship",                   # -> domain_match
    "primarySpecialty",             # -> domain_match
    "subspecialties",               # -> domain_match
    "residencyCompletionYear",      # -> post_residency_ge_3yr (BINARY; year is discarded)
    "clinicalHalfDaysPerMonth",     # -> currently_practicing (ordinal, not magnitude)
    "practiceStatus",               # -> currently_practicing; leave != disengagement (H1)
    "halfDaysBeforeLeave",          # -> currently_practicing when on protected leave (H1)
    "currentlyActive",              # -> currently_practicing fallback
    "continuingCertification",      # -> continuing_cert
    "structuredReviewExperience",   # -> structured_review_exp
})

# ─── currently_practicing (AUDIT H1) ─────────────────────────────────────────────────────
# This feature was `1.0 if half_days >= 4 else 0.0` — a hard binary cliff at part-time
# practice, worth 0.80, the third-largest term in the model. Two things were wrong with it and
# neither is a matter of taste:
#
#   * **Part-time clinical practice is not evenly distributed by sex, caregiving status, or
#     disability.** A cliff at "half-time-ish" converts a protected characteristic into a
#     0.80-point penalty by proxy. `sex` and `age` are pinned to zero precisely so the model
#     cannot reach them; a cliff here reaches them anyway, unpinned and undisclosed.
#   * **A physician on parental or medical leave scored identically to one who had left
#     medicine.** Those are different facts about different people. Leave is a temporary
#     absence from a practice; the illness scripts the feature is a proxy for are intact.
#
# The fix is not to delete the feature. Currency of clinical practice is a real, non-protected
# criterion and it is what a CEC selects for. The fix is to make the encoding say what the
# criterion actually is:
#
#   1.0  practising at the usual professional cadence, OR on protected leave from such a
#        practice — the pre-leave figure is what is scored, not the leave
#   0.5  practising, part-time — currently seeing patients, at a lower cadence
#   0.0  not currently in clinical practice at all
#
# Three ordinal levels, mirroring `domain_match`'s 1.0/0.5/0.0, and still not a magnitude:
# twenty half-days is not worth more than four, exactly as before. What changed is that
# *three* is no longer worth the same as *none*.
#
# Residual exposure, disclosed rather than argued away: 0.5 is still 0.40 below 1.0, so
# part-time practice still costs something. It is halved, it is monitored per-group by the
# fairness table, and it is in the counsel memo. See docs/PRD_C_COUNSEL_MEMO.md §1.
FULL_PRACTICE_HALF_DAYS = 4      # per month, averaged over the last 12
PRACTISING_FULL = 1.0
PRACTISING_PART = 0.5
PRACTISING_NONE = 0.0

# Statuses that must never be scored as disengagement. Deliberately enumerated rather than
# inferred from a zero half-day count: "0 half-days because I am on 12 weeks of parental
# leave" and "0 half-days because I stopped seeing patients in 2019" are the same number and
# opposite facts, and only an explicit field can tell them apart.
_PROTECTED_LEAVE_STATUSES = frozenset({"on_leave", "parental_leave", "medical_leave",
                                       "caregiving_leave", "military_leave", "sabbatical"})
_NOT_PRACTISING_STATUSES = frozenset({"not_practising", "not_practicing", "retired",
                                      "non_clinical"})


def _practising_level(half_days: Optional[int]) -> Optional[float]:
    """Ordinal level from a half-day count, or None when there is no count to read."""
    if half_days is None:
        return None
    if half_days >= FULL_PRACTICE_HALF_DAYS:
        return PRACTISING_FULL
    if half_days >= 1:
        return PRACTISING_PART
    return PRACTISING_NONE


def _practising_value(creds: Dict[str, Any]) -> float:
    """``currently_practicing``, on the three-level scale above."""
    status = str(creds.get("practiceStatus") or "").strip().casefold()

    if status in _PROTECTED_LEAVE_STATUSES:
        # Score the practice as it stood BEFORE the leave. A blank pre-leave figure is a
        # missing answer, not a zero — the same tri-state discipline the gates use, and the
        # difference matters most for exactly the people this clause protects.
        prior = _int_or_none(creds.get("halfDaysBeforeLeave"))
        if prior is None:
            prior = _int_or_none(creds.get("clinicalHalfDaysPerMonth")) or None
        level = _practising_level(prior)
        if level is None or level == PRACTISING_NONE:
            return PRACTISING_PART
        return level

    if status in _NOT_PRACTISING_STATUSES:
        return PRACTISING_NONE

    level = _practising_level(_int_or_none(creds.get("clinicalHalfDaysPerMonth")))
    if level is not None:
        return level
    # No half-day figure at all: fall back to the older yes/no field so a physician who
    # onboarded before this release is not silently zeroed.
    return PRACTISING_FULL if _truthy(creds.get("currentlyActive")) is True else PRACTISING_NONE


# AUDIT LOW — the encoder reads the users row directly, so without this its blast radius is
# `SELECT *`: every column any of the four agents ever adds to `users` becomes reachable by
# the scoring path, and a future `users.practice_zip` would be one `.get()` away from the
# model. Declared here and enforced by an AST walk over the real source in
# test_tiering_audit_c.py, which is the check that actually holds — the two frozensets below
# are only as good as the test that compares them to the code.
ENCODER_USER_COLUMNS: frozenset = frozenset({
    "board_cert",         # -> board_certified_active (corroborates the credential record)
    "specialty",          # -> domain_match
    "credentials_json",   # -> the credential record itself, filtered by ENCODER_CREDENTIAL_KEYS
    "npi_payload_json",   # -> NPPES primary taxonomy, for domain_match
})

_STRUCTURED_REVIEW_KINDS = frozenset({
    "cec", "dsmb", "cec_dsmb", "journal_peer_review", "board_item_writing",
    "guideline_panel", "core_faculty", "program_director",
})


def feature_vector(
    user: Dict[str, Any],
    *,
    case_domain: Optional[str] = None,
    calibration_z: Optional[float] = None,
    practice_first_pass: Optional[bool] = None,
    measured_quality_z: Optional[float] = None,
    n_tasks: int = 0,
) -> Dict[str, float]:
    """Encode one (physician, case domain) pair into the model's inputs.

    Every protected proxy is absent by construction, not by discipline: the encoder reads a
    fixed list of keys and ``_assert_no_forbidden_reads`` fails loudly if a future edit adds
    one of ``FORBIDDEN_CREDENTIAL_KEYS``. The pinned features are NOT emitted here — nothing
    collects them — but the learning update carries them anyway so their immobility is proven
    on real batches rather than asserted in a comment.
    """
    creds = _creds(user)
    dm, dm_why = domain_match(user, case_domain)

    # board_certified_active — any currently-active board certification.
    board_active = any(
        isinstance(bc, dict) and _truthy(bc.get("active")) is not False
        and str(bc.get("board") or "").strip()
        for bc in (creds.get("boardCertifications") or [])
    ) or bool(str(user.get("board_cert") or "").strip())

    # subspecialty_certified — encoded as x · 1[domain_match > 0].
    #
    # THIS ENCODING IS LOAD-BEARING. Without the indicator, a board-certified nephrologist
    # scores as a TR on a cardiology case: the subspecialty term fires on the strength of
    # holding *a* subspecialty, with no reference to whether it is the right one. Verified as
    # a real failure of the naive encoding, and the reason an off-domain subspecialist now
    # lands in the admin band — which is the correct behaviour, not a near-miss.
    has_subspec = any(
        isinstance(bc, dict) and str(bc.get("subspecialty") or "").strip()
        and _truthy(bc.get("active")) is not False
        for bc in (creds.get("boardCertifications") or [])
    )
    subspec = 1.0 if (has_subspec and dm > 0) else 0.0

    # post_residency_ge_3yr — the ONLY place residency year is read, and it leaves as a
    # boolean. There is no continuous years term in this model and there must never be one.
    end_year = _int_or_none(creds.get("residencyCompletionYear"))
    now_year = datetime.utcnow().year
    ge3 = 1.0 if (end_year is not None and (now_year - end_year) >= 3) else 0.0

    practising = _practising_value(creds)

    cont_cert = 1.0 if _truthy(creds.get("continuingCertification")) is True else 0.0

    kinds = creds.get("structuredReviewExperience") or []
    struct = 1.0 if any(
        str(k).strip().casefold() in _STRUCTURED_REVIEW_KINDS for k in kinds
        if isinstance(k, (str, bytes))
    ) else 0.0

    vec: Dict[str, float] = {
        "intercept": 1.0,
        "board_certified_active": 1.0 if board_active else 0.0,
        "subspecialty_certified": subspec,
        "domain_match": float(dm),
        "post_residency_ge_3yr": ge3,
        "currently_practicing": practising,
        "continuing_cert": cont_cert,
        "structured_review_exp": struct,
        "calibration_z": float(calibration_z or 0.0),
        # Read from the tutorial record, never from the credential blob. An
        # applicant who has not sat the practice case encodes 0.0, which is the
        # same value as a failed first attempt: absence of evidence and
        # evidence of a miss are both "no positive signal here", and inventing
        # a third state would give the model something to fit that we cannot
        # actually observe.
        "practice_first_pass": 1.0 if practice_first_pass else 0.0,
    }

    # §5.4 — the hand-off. Measured quality is structurally unavailable below 20 tasks and
    # dominant above it, and the credential features are decayed once there is enough measured
    # signal to stand on. Credentials are scaffolding; this is the scaffolding coming down.
    mq = 0.0
    if n_tasks >= MEASURED_QUALITY_MIN_TASKS and measured_quality_z is not None:
        mq = float(measured_quality_z)
    vec[MEASURED_QUALITY_FEATURE] = mq
    if n_tasks >= DECAY_AT_N_TASKS:
        for name in CREDENTIAL_FEATURES:
            vec[name] *= CREDENTIAL_DECAY

    vec["_domain_match_why"] = dm_why  # type: ignore[assignment]  # popped by score()
    return vec


def _assert_encoder_reads_nothing_forbidden() -> None:
    """Checked at import, so a forbidden read cannot reach production at all.

    The guarantee is deliberately about the ENCODER, not about the data: a legacy row may
    legitimately still carry a ``medicalSchool`` key, and deleting stored data is not this
    module's job. What must be true is that ``feature_vector`` never looks at one — so the
    exact set of credential keys the encoder reads is declared, and the intersection with
    ``FORBIDDEN_CREDENTIAL_KEYS`` is asserted empty right here.

    An earlier draft of this function was a no-op with a docstring claiming it raised. That is
    worse than having no guard: a reviewer reads the name, believes the property is enforced,
    and stops looking. If you add a key to ``ENCODER_CREDENTIAL_KEYS``, this either passes or
    the module refuses to import.
    """
    overlap = ((ENCODER_CREDENTIAL_KEYS | ENCODER_USER_COLUMNS)
               & FORBIDDEN_CREDENTIAL_KEYS)
    if overlap:
        raise AssertionError(
            f"the encoder would read protected proxies {sorted(overlap)} — "
            "PRD C §3.3 forbids collecting, deriving or logging them")


_assert_encoder_reads_nothing_forbidden()


def scored_vector(vec: Dict[str, Any]) -> Dict[str, float]:
    """Drop the annotation keys, keep the numbers."""
    return {k: float(v) for k, v in vec.items() if not k.startswith("_")}


# ═══════════════════════════════════════════════════════════════════════════════════════════
# The score
# ═══════════════════════════════════════════════════════════════════════════════════════════

def sigmoid(z: float) -> float:
    # Split on the sign so neither exp() overflows at the tails.
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def default_weights() -> Dict[str, Dict[str, float]]:
    """The priors, as the shape the store persists: ``{name: {m, q, pinned}}``."""
    out: Dict[str, Dict[str, float]] = {}
    for name, (m, q) in FEATURES.items():
        out[name] = {"m": m, "q": q, "pinned": 0}
    m, q = MEASURED_QUALITY
    out[MEASURED_QUALITY_FEATURE] = {"m": m, "q": q, "pinned": 0}
    for name in PINNED_ZERO:
        out[name] = {"m": 0.0, "q": PINNED_PRECISION, "pinned": 1}
    return out


def score(vec: Dict[str, Any], weights: Optional[Dict[str, Dict[str, float]]] = None
          ) -> Dict[str, Any]:
    """``s = Σ mᵢxᵢ``, ``P(TR) = σ(s)``, plus the per-feature contributions ranked by
    magnitude — an admin who cannot see *why* will ignore the score, and then the scoring
    system is decoration."""
    w = weights or default_weights()
    x = scored_vector(vec)
    contributions = {name: w.get(name, {}).get("m", 0.0) * val for name, val in x.items()}
    s = sum(contributions.values())
    ranked = sorted(contributions.items(), key=lambda kv: abs(kv[1]), reverse=True)
    return {
        "s": s,
        "p_tr": sigmoid(s),
        "contributions": contributions,
        "ranked": [(n, c) for n, c in ranked if abs(c) > 1e-12],
        "band": TR if s >= TR_THRESHOLD else (TL if s <= TL_THRESHOLD else "admin"),
    }


# ═══════════════════════════════════════════════════════════════════════════════════════════
# §3.1 — TR eligibility, and the composition that makes DoD §7.1 and §7.2 both true
# ═══════════════════════════════════════════════════════════════════════════════════════════

def tr_eligibility(
    user: Dict[str, Any],
    gates: Dict[str, Any],
    vec: Dict[str, Any],
    *,
    calibration_passed: Optional[bool] = None,
) -> Dict[str, Any]:
    """``TR_eligible ⇔ A1..A7 ∧ board certified active ∧ domain_match ≥ 0.5
    ∧ calibration exam ≥ gate ∧ ≥3 years post-residency``.

    Every clause traces to a documented CEC or reader-study criterion; none is a proxy for a
    protected class. Returns the unmet clauses by name so the admin surface can say exactly
    what is missing rather than "not eligible".
    """
    missing: List[str] = []
    if not gates["eligible"]:
        missing.append("hard gates A1–A7")
    if vec.get("board_certified_active", 0.0) <= 0:
        missing.append("active board certification")
    if float(vec.get("domain_match", 0.0)) < 0.5:
        missing.append("domain match for this case")
    if calibration_passed is not True:
        missing.append("calibration exam at the TR gate")
    if vec.get("post_residency_ge_3yr", 0.0) <= 0:
        missing.append("3 years post-residency")
    return {"eligible": not missing, "missing": missing}


def propose(
    user: Dict[str, Any],
    *,
    case_domain: Optional[str] = None,
    calibration: Optional[Dict[str, Any]] = None,
    measured_quality_z: Optional[float] = None,
    n_tasks: int = 0,
    leie_status: Optional[str] = None,
    duplicate_npi: bool = False,
    weights: Optional[Dict[str, Dict[str, float]]] = None,
    explore: bool = False,
    rng: Optional[random.Random] = None,
) -> Dict[str, Any]:
    """The whole decision, composed. Returns the payload the admin queue renders and the
    learning loop stores.

    **Gate-first, and the ordering is the point.** PRD §2 says a hard-gate failure is eligible
    for neither tier and §7.2 requires that a high score never override one, so the gates are
    evaluated before the score exists and a FAIL short-circuits to ``proposed_tier = None``.

    **A blocked TR lands in the admin band, never in TL.** This is the one place the composed
    behaviour is not a restatement of the PRD, and it is what makes §7.1 true. Worked through
    with the PRD's own priors: a board-certified, practising, calibrated nephrologist scores
    ``s = +4.70`` on a nephrology case and ``s = +2.20`` on a cardiology case — because the
    intercept and the six general-seniority terms carry 2.2 on their own, and only the
    domain-linked terms (``domain_match`` 1.6 + ``subspecialty_certified`` 0.9) fall away.
    ``+2.20`` is above the ``+1.0`` TR threshold, so a score-only reading promotes a
    nephrologist onto a cardiology case, which §8 names as the definitive sign of a wrong
    encoding. The TR eligibility rule blocks it. Routing the blocked case to TL instead would
    be equally wrong: nothing about being off-domain says they are a poor labeler. "The model
    cannot decide this one" is the honest answer, and the admin band is where that goes.
    """
    gate_result = hard_gates(user, leie_status=leie_status, duplicate_npi=duplicate_npi)
    cal_z = None
    cal_passed = None
    if calibration:
        cal_z = calibration.get("calibration_z")
        cal_passed = calibration.get("tr_gate_passed")

    vec = feature_vector(
        user, case_domain=case_domain, calibration_z=cal_z,
        practice_first_pass=caps.practice_first_pass(user),
        measured_quality_z=measured_quality_z, n_tasks=n_tasks,
    )
    w = weights or default_weights()

    # §5.3 — Thompson sampling on borderline decisions. Sampling w̃ ~ N(m, diag(q⁻¹)) and
    # scoring with w̃ over-promotes candidates the model is UNCERTAIN about rather than ones
    # it thinks are weak, which is exactly the coverage the admin-override label set lacks.
    # Pinned features are excluded from the sampling: a pinned weight with q = 1e6 would draw
    # ±0.001 rather than 0, and "approximately zero forever" is not the guarantee §3.3 makes.
    w_eff = w
    was_exploration = False
    if explore:
        w_eff = thompson_sample(w, rng=rng)
        was_exploration = True

    sc = score(vec, w_eff)
    elig = tr_eligibility(user, gate_result, vec, calibration_passed=cal_passed)

    reasons = _plain_reasons(vec, sc, gate_result, elig)

    if gate_result["blocked"]:
        proposed: Optional[str] = None
        band = "blocked"
    elif gate_result["undetermined"]:
        # Not a rejection. We have not finished checking, and a proposal built on an
        # unfinished check is a guess wearing a number.
        proposed = None
        band = "admin"
    elif sc["band"] == TR and not elig["eligible"]:
        proposed = None
        band = "admin"
    elif sc["band"] == TR:
        proposed = TR
        band = TR
    elif sc["band"] == TL:
        proposed = TL
        band = TL
    else:
        proposed = None
        band = "admin"

    if proposed not in (None, TR, TL):  # pragma: no cover — guard against a future edit
        raise AssertionError(
            "propose() may only propose reviewer, labeler, or defer to the admin.")

    return {
        "case_domain": normalize_domain(case_domain or "") or (case_domain or None),
        "features": scored_vector(vec),
        "score": sc["s"],
        "p_tr": sc["p_tr"],
        "band": band,
        "proposed_tier": proposed,
        "proposed_tier_word": caps.tier_word(proposed) if proposed else "Admin decision",
        "thresholds": {"tr": TR_THRESHOLD, "tl": TL_THRESHOLD},
        "distance_to_tr": sc["s"] - TR_THRESHOLD,
        "distance_to_tl": sc["s"] - TL_THRESHOLD,
        "ranked_contributions": [{"feature": n, "contribution": round(c, 4)}
                                 for n, c in sc["ranked"]],
        "reasons": reasons,
        "gates": gate_result["gates"],
        "gates_eligible": gate_result["eligible"],
        "gates_failed": gate_result["failed"],
        "gates_undetermined": gate_result["undetermined"],
        "tr_eligible": elig["eligible"],
        "tr_missing": elig["missing"],
        "was_exploration": was_exploration,
        "domain_match_why": vec.get("_domain_match_why"),
        "n_tasks": n_tasks,
        "credential_decay_applied": n_tasks >= DECAY_AT_N_TASKS,
    }


_FEATURE_WORDS = {
    "intercept": "baseline — TR is the minority role",
    "board_certified_active": "holds an active board certification",
    "subspecialty_certified": "subspecialty certified IN this case's domain",
    "domain_match": "domain expertise matches this case",
    "post_residency_ge_3yr": "at least 3 years post-residency",
    "currently_practicing": "currently practising (≥4 clinical half-days/month)",
    "continuing_cert": "participating in continuing certification",
    "structured_review_exp": "has structured review experience (CEC/DSMB, peer review, boards)",
    "calibration_z": "calibration exam performance",
    "practice_first_pass": "passed the practice case first time",
    MEASURED_QUALITY_FEATURE: "measured agreement on completed work",
}


def _plain_reasons(vec: Dict[str, Any], sc: Dict[str, Any], gates: Dict[str, Any],
                   elig: Dict[str, Any]) -> List[str]:
    """Ranked by contribution, in words. An admin who cannot see why will ignore the score."""
    out: List[str] = []
    for name, contribution in sc["ranked"]:
        word = _FEATURE_WORDS.get(name, name)
        sign = "+" if contribution > 0 else "−"
        out.append(f"{sign}{abs(contribution):.2f}  {word}")
    for gid in gates["failed"]:
        out.append(f"BLOCKER  {gid}: {gates['gates'][gid]['detail']}")
    for gid in gates["undetermined"]:
        out.append(f"UNRESOLVED  {gid}: {gates['gates'][gid]['detail']}")
    for miss in elig["missing"]:
        if miss != "hard gates A1–A7":
            out.append(f"TR needs: {miss}")
    return out


def thompson_sample(weights: Dict[str, Dict[str, float]],
                    rng: Optional[random.Random] = None) -> Dict[str, Dict[str, float]]:
    """``w̃ ~ N(m, diag(q⁻¹))``, pinned features held exactly at their mean.

    ``rng`` is injectable so the exploration path is testable — an untestable stochastic
    branch in a decision system is a branch nobody ever verifies.
    """
    r = rng or random.Random()
    out: Dict[str, Dict[str, float]] = {}
    for name, row in weights.items():
        if row.get("pinned"):
            out[name] = dict(row)
            continue
        q = max(float(row.get("q") or 1.0), 1e-9)
        out[name] = dict(row, m=r.gauss(float(row["m"]), math.sqrt(1.0 / q)))
    return out


def should_explore(rng: Optional[random.Random] = None) -> bool:
    return (rng or random.Random()).random() < EXPLORATION_RATE


# ═══════════════════════════════════════════════════════════════════════════════════════════
# §5.2 — the update. Regularized Bayesian logistic regression.
# ═══════════════════════════════════════════════════════════════════════════════════════════

class SingularSystem(ArithmeticError):
    """``_solve`` was handed a system it cannot solve.

    AUDIT M4. This used to be swallowed: a singular pivot was perturbed by ``1e-9`` and then
    divided by, so ``_solve([[0,0],[0,0]], [1,1])`` returned ``[1e9, 1e9]`` with no exception —
    a Newton direction of a billion, silently. Harmless today, because the Hessian is positive
    definite by construction (``hess[i][i]`` is seeded with ``q0[i] ≥ 1.0``) and the Armijo
    search plus the 0.25 clip would absorb a bad direction anyway. But *"harmless because
    three other things happen to catch it"* is the property that quietly stops being true, and
    it is load-bearing: trusting the Newton direction is what makes the line search a
    correctness argument rather than a hope.
    """


def _solve(a: List[List[float]], b: List[float]) -> List[float]:
    """Gaussian elimination with partial pivoting. Deliberately dependency-free: the problem
    is at most 17-dimensional, so a linear-algebra dependency would buy nothing and would make
    this module's import graph a deployment question.

    Raises ``SingularSystem`` rather than returning a plausible-looking number.
    """
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            raise SingularSystem(
                f"no usable pivot in column {col} of a {n}x{n} system")
        m[col], m[piv] = m[piv], m[col]
        pv = m[col][col]
        for r in range(n):
            if r == col:
                continue
            f = m[r][col] / pv
            if f:
                for c in range(col, n + 1):
                    m[r][c] -= f * m[col][c]
    out = []
    for i in range(n):
        if abs(m[i][i]) < 1e-12:
            raise SingularSystem(f"degenerate diagonal at {i} in a {n}x{n} system")
        out.append(m[i][n] / m[i][i])
    return out


def fit_batch(
    weights: Dict[str, Dict[str, float]],
    observations: Sequence[Tuple[Dict[str, float], int, float]],
) -> Dict[str, Dict[str, float]]:
    """Minimize ``L(w) = ½ Σᵢ qᵢ(wᵢ − mᵢ)² + Σⱼ λⱼ log(1 + exp(−yⱼ w·xⱼ))`` by Newton steps,
    then update the precisions.

    ``observations`` is ``[(features, y, weight)]`` with ``y = +1`` for TR and ``−1`` for TL.
    ``weight`` is the per-observation multiplier — §5.3 feeds shadow-TR outcomes at double
    weight because they are a *real* outcome label rather than an opinion label.

    Three terms, three jobs: the prior term says *don't drift far from the rule unless the data
    insists*; the likelihood term fits; the precision update says *each observation buys
    certainty in proportion to how informative it was*.

    Pure — it does not touch the store, does not clip, and does not assert. The guardrails live
    in ``apply_decision_batch`` because they are policy about *when* to write, not arithmetic.
    """
    names = [n for n in ALL_WEIGHT_NAMES if n in weights]
    n = len(names)
    m0 = [float(weights[nm]["m"]) for nm in names]
    q0 = [float(weights[nm]["q"]) for nm in names]
    pinned = [bool(weights[nm].get("pinned")) for nm in names]
    w = m0[:]

    rows = [([float(x.get(nm, 0.0)) for nm in names], int(y), float(lam))
            for x, y, lam in observations]

    def objective(wv: List[float]) -> float:
        total = 0.5 * sum(q0[i] * (wv[i] - m0[i]) ** 2 for i in range(n))
        for xs, y, lam in rows:
            z = sum(wv[i] * xs[i] for i in range(n))
            # log(1 + exp(-y z)), computed in the stable branch so a saturated z is 0.0
            # rather than an OverflowError.
            u = -y * z
            total += lam * (u + math.log1p(math.exp(-u)) if u > 0 else math.log1p(math.exp(u)))
        return total

    for _ in range(NEWTON_STEPS):
        grad = [q0[i] * (w[i] - m0[i]) for i in range(n)]
        hess = [[0.0] * n for _ in range(n)]
        for i in range(n):
            hess[i][i] = q0[i]
        for xs, y, lam in rows:
            z = sum(w[i] * xs[i] for i in range(n))
            # d/dw of log(1+exp(-y z)) = -y σ(-y z) x
            g = -y * sigmoid(-y * z) * lam
            p = sigmoid(z)
            hcoef = p * (1.0 - p) * lam
            for i in range(n):
                if xs[i] == 0.0:
                    continue
                grad[i] += g * xs[i]
                for j in range(n):
                    if xs[j] != 0.0:
                        hess[i][j] += hcoef * xs[i] * xs[j]
        # Pinned rows are removed from the system rather than solved and reset afterwards:
        # a pinned weight must not even participate in the Newton direction, or an
        # off-diagonal Hessian term lets it steer the unpinned ones through the solve.
        for i in range(n):
            if pinned[i]:
                grad[i] = 0.0
                for j in range(n):
                    hess[i][j] = 0.0
                    hess[j][i] = 0.0
                hess[i][i] = 1.0
        try:
            step = _solve(hess, grad)
        except SingularSystem:
            # Degrade to "this batch teaches nothing" rather than 500 — and say so at ERROR,
            # because silently learning nothing is exactly the §8 failure. The prior is
            # already in `w`, so breaking here leaves the weights where they were.
            log.error("[tiering] singular Hessian; batch abandoned, weights left at the prior",
                      exc_info=True)
            w = m0[:]
            break
        # Backtracking line search. UNDAMPED Newton is wrong here and fails in a way that is
        # specifically dangerous for this system: on a batch where the admins consistently
        # contradict the prior, the full step overshoots into the saturated tail of the
        # logistic, where the likelihood gradient vanishes and the remaining prior gradient
        # sends w straight back to m. The result is a model that reports a delta of EXACTLY
        # ZERO after two hundred informative decisions — which is indistinguishable from a
        # converged model, which is precisely the failure §8 warns about. Verified: with 7+
        # non-zero features the undamped version returned Δm = 0.0 on a batch that should
        # have moved every weight to its clip.
        #
        # Armijo condition on a convex objective; guaranteed to make progress.
        gdotd = sum(grad[i] * step[i] for i in range(n))   # = gᵀH⁻¹g >= 0, the decrement
        base = objective(w)
        t = 1.0
        for _ls in range(30):
            cand = [w[i] - t * step[i] for i in range(n)]
            if objective(cand) <= base - 1e-4 * t * gdotd:
                break
            t *= 0.5
        else:
            break   # no step improves the objective — we are at the optimum
        w = [w[i] - t * step[i] for i in range(n)]
        if max((abs(t * s) for s in step), default=0.0) < 1e-9:
            break

    # qᵢ ← qᵢ + Σⱼ xᵢⱼ² pⱼ(1 − pⱼ)
    q_new = q0[:]
    for xs, _y, lam in rows:
        z = sum(w[i] * xs[i] for i in range(n))
        p = sigmoid(z)
        info = p * (1.0 - p) * lam
        for i in range(n):
            if xs[i] != 0.0:
                q_new[i] += (xs[i] ** 2) * info

    out: Dict[str, Dict[str, float]] = {}
    for i, nm in enumerate(names):
        if pinned[i]:
            out[nm] = {"m": 0.0, "q": PINNED_PRECISION, "pinned": 1}
        else:
            out[nm] = {"m": w[i], "q": min(q_new[i], Q_MAX), "pinned": 0}
    return out


def apply_guardrails(
    before: Dict[str, Dict[str, float]],
    after: Dict[str, Dict[str, float]],
) -> Dict[str, Dict[str, float]]:
    """Clip ``|Δm| ≤ MAX_DELTA_M`` per feature per batch, and hold every pinned row exactly.

    One opinionated admin on one bad night must not rewrite the model. The clip is applied
    AFTER the fit rather than as a constraint inside it, so the fit stays a clean convex
    problem and the policy stays legible.
    """
    out: Dict[str, Dict[str, float]] = {}
    for name, row in after.items():
        prior = before.get(name, {"m": 0.0, "q": 1.0, "pinned": 0})
        if prior.get("pinned") or name in PINNED_ZERO:
            out[name] = {"m": 0.0, "q": PINNED_PRECISION, "pinned": 1}
            continue
        m_prev = float(prior["m"])
        delta = float(row["m"]) - m_prev
        clipped = max(-MAX_DELTA_M, min(MAX_DELTA_M, delta))
        out[name] = {
            "m": m_prev + clipped,
            "q": min(max(float(row["q"]), float(prior["q"])), Q_MAX),
            "pinned": 0,
        }
    return out


def assert_pinned_immobile(weights: Dict[str, Dict[str, float]]) -> None:
    """Asserted after every update, not just by convention (§5.2).

    This is the cheap, auditable proof that the learned model cannot rediscover a protected
    characteristic through admin behaviour. If it ever fires, the correct response is to stop
    writing weights — not to relax the assertion.
    """
    for name in PINNED_ZERO:
        row = weights.get(name)
        if row is None:
            raise AssertionError(f"pinned feature {name!r} vanished from the weight table")
        if float(row["m"]) != 0.0:
            raise AssertionError(
                f"pinned feature {name!r} moved to m={row['m']!r}; it must be exactly 0.0")
        if not row.get("pinned"):
            raise AssertionError(f"pinned feature {name!r} lost its pinned flag")


def apply_decision_batch(
    store: Any,
    *,
    limit: int = 500,
    actor: Optional[str] = None,
) -> Dict[str, Any]:
    """Fold every un-applied admin decision into the weights, exactly once.

    The four mandatory guardrails, and where each lives:

    * **Clip** ``|Δmᵢ| ≤ 0.25`` per batch — ``apply_guardrails``.
    * **Pinned features never move** — enforced inside ``fit_batch`` (removed from the Newton
      system), re-enforced in ``apply_guardrails``, and *asserted* here before anything is
      written. Three layers because it is the one property an auditor will actually test.
    * **Never replay** — the batch is selected on ``applied_at IS NULL`` and stamped in the
      same transaction as the weight write. ``store.apply_tiering_batch`` owns that atomicity.
    * **Hard gates are frozen** — structural. Gates are not features; there is no weight for
      them to learn and no path from this function to ``hard_gates``.

    Returns the per-feature delta. **The delta is logged on every run** (§8): a model that has
    silently stopped updating looks exactly like a model that has converged, and the only cheap
    way to tell them apart is to watch the number that should be shrinking actually shrink.
    """
    pending = store.pending_tiering_decisions(limit=limit)
    before = store.get_tiering_weights() or default_weights()
    if not pending:
        log.info("[tiering] no pending decisions; weights unchanged (%d features)", len(before))
        return {"applied": 0, "deltas": {}, "weights": before, "decision_ids": [],
                "quarantined": 0, "quarantined_ids": []}

    observations: List[Tuple[Dict[str, float], int, float]] = []
    used: List[str] = []
    quarantined: List[str] = []
    for row in pending:
        try:
            feats = json.loads(row["features_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(feats, dict):
            continue

        # AUDIT M3 — one non-finite value kills the batch AND consumes it.
        #
        # ``isinstance(v, (int, float))`` accepts ``float('nan')``, and NaN round-trips
        # through JSON. From there: it reaches ``_solve``, every comparison against it is
        # False, so the Armijo test fails all thirty backtracks, the Newton loop breaks at the
        # first step leaving ``w = m0`` — and then every decision in the batch is stamped
        # ``applied_at``. **The observations are consumed forever and nothing was learned.**
        # The only signal was the "moved NO weight" warning, which is precisely the
        # converged-vs-dead ambiguity §8 is about.
        #
        # So: skip the row, and do NOT mark it applied. A poisoned row is a bug in whatever
        # wrote it, the underlying decision is still good, and marking it applied is the one
        # action that makes it unrecoverable. It is surfaced in the return value and named in
        # the log so it gets fixed rather than accumulating silently.
        numeric = {k: float(v) for k, v in feats.items()
                   if isinstance(v, (int, float)) and not isinstance(v, bool)}
        if not all(math.isfinite(v) for v in numeric.values()):
            quarantined.append(row["decision_id"])
            continue

        admin_tier = (row.get("admin_tier") or "").strip()
        if admin_tier == TR:
            y = 1
        elif admin_tier == TL:
            y = -1
        else:
            # No usable label: an 'advisor' appointment or a deferred row is not a statement
            # about TL-vs-TR. It is still marked applied so it cannot be picked up forever.
            used.append(row["decision_id"])
            continue
        # §5.3 — a shadow-TR trial scored against a settled gold adjudication is a REAL
        # outcome label, not an opinion label, and is the only thing that breaks the
        # circularity of learning from decisions the system itself routed to an admin.
        lam = 2.0 if row.get("outcome_source") == "shadow_tr" else 1.0
        observations.append((numeric, y, lam))
        used.append(row["decision_id"])

    if quarantined:
        log.error("[tiering] %d decision(s) carry a non-finite feature and were NOT applied "
                  "— fix the rows and re-run: %s", len(quarantined), ", ".join(quarantined))

    if not observations:
        store.apply_tiering_batch(used, weights=None)
        log.info("[tiering] %d decisions carried no TL/TR label; weights unchanged", len(used))
        return {"applied": 0, "marked": len(used), "deltas": {}, "weights": before,
                "decision_ids": used, "quarantined": len(quarantined),
                "quarantined_ids": quarantined}

    fitted = fit_batch(before, observations)
    after = apply_guardrails(before, fitted)
    assert_pinned_immobile(after)

    deltas = {n: round(float(after[n]["m"]) - float(before.get(n, {}).get("m", 0.0)), 6)
              for n in after}
    dq = {n: round(float(after[n]["q"]) - float(before.get(n, {}).get("q", 0.0)), 6)
          for n in after}

    store.apply_tiering_batch(used, weights=after)

    moved = {k: v for k, v in deltas.items() if abs(v) > 1e-9}
    log.info("[tiering] applied %d observations from %d decisions · Δm %s · Δq %s",
             len(observations), len(used),
             json.dumps(moved, sort_keys=True),
             json.dumps({k: v for k, v in dq.items() if abs(v) > 1e-9}, sort_keys=True))
    if not moved:
        # Not an error — a converged model legitimately produces this. It is logged at
        # WARNING because it is indistinguishable from a broken update, and the person
        # reading the log is the only one who can tell.
        log.warning("[tiering] batch of %d moved NO weight. Converged, or stuck? "
                    "Check that features_json is populated.", len(observations))

    return {
        "applied": len(observations),
        "marked": len(used),
        "deltas": deltas,
        "delta_q": dq,
        "weights": after,
        "decision_ids": used,
        # AUDIT M3: surfaced, not just logged. A quarantined row that only appears in a log
        # line is a row nobody fixes, and the batch it belongs to keeps under-learning.
        "quarantined": len(quarantined),
        "quarantined_ids": quarantined,
        "actor": actor,
    }


# ═══════════════════════════════════════════════════════════════════════════════════════════
# §5.4 — One-Coin Dawid–Skene, the hand-off to measured quality
# ═══════════════════════════════════════════════════════════════════════════════════════════

def one_coin_dawid_skene(
    observations: Sequence[Tuple[str, str, str]],
    *,
    gold: Optional[Dict[str, str]] = None,
    iterations: int = 60,
    tol: float = 1e-7,
) -> Dict[str, Any]:
    """Estimate each annotator's accuracy from unaggregated labels.

    ``observations`` is ``[(item_id, annotator_id, label)]``. Two independent labels plus a TR
    adjudication is exactly the input this needs, and the TR resolution is a near-gold label —
    pass it via ``gold`` and the corresponding items are clamped rather than inferred.

    One-coin (a single accuracy parameter per annotator, errors spread uniformly over the
    remaining classes) rather than a full confusion matrix: with the number of items per
    physician available here, a full matrix has more parameters than data and overfits into
    confident nonsense. One-coin performs well on small datasets and is the right choice.

    Returns ``{"accuracy": {annotator: p}, "consensus": {item: label}, "n_items": {...}}``.
    """
    items: Dict[str, List[Tuple[str, str]]] = {}
    labels: set = set()
    annotators: set = set()
    for item_id, ann, label in observations:
        if label is None:
            continue
        items.setdefault(item_id, []).append((ann, label))
        labels.add(label)
        annotators.add(ann)
    if not items or len(labels) < 2:
        return {"accuracy": {}, "consensus": {}, "n_items": {},
                "reason": "insufficient_data"}

    classes = sorted(labels)
    k = len(classes)
    gold = gold or {}
    # Initialize from majority vote.
    acc = {a: 0.7 for a in annotators}
    posterior: Dict[str, Dict[str, float]] = {}

    for _ in range(iterations):
        # E-step
        new_posterior: Dict[str, Dict[str, float]] = {}
        for item_id, obs in items.items():
            if item_id in gold and gold[item_id] in classes:
                new_posterior[item_id] = {c: (1.0 if c == gold[item_id] else 0.0)
                                          for c in classes}
                continue
            logp = {c: 0.0 for c in classes}
            for ann, label in obs:
                a = min(max(acc.get(ann, 0.7), 1e-6), 1 - 1e-6)
                for c in classes:
                    logp[c] += math.log(a) if label == c else math.log((1 - a) / (k - 1))
            mx = max(logp.values())
            exps = {c: math.exp(logp[c] - mx) for c in classes}
            tot = sum(exps.values()) or 1.0
            new_posterior[item_id] = {c: exps[c] / tot for c in classes}
        # M-step
        num: Dict[str, float] = {a: 0.0 for a in annotators}
        den: Dict[str, float] = {a: 0.0 for a in annotators}
        for item_id, obs in items.items():
            post = new_posterior[item_id]
            for ann, label in obs:
                num[ann] += post.get(label, 0.0)
                den[ann] += 1.0
        new_acc = {a: ((num[a] + 1.0) / (den[a] + k)) if den[a] else 0.7   # Laplace prior
                   for a in annotators}
        shift = max((abs(new_acc[a] - acc[a]) for a in annotators), default=0.0)
        acc, posterior = new_acc, new_posterior
        if shift < tol:
            break

    consensus = {i: max(p.items(), key=lambda kv: kv[1])[0] for i, p in posterior.items()}
    n_items = {a: sum(1 for obs in items.values() for ann, _ in obs if ann == a)
               for a in annotators}
    return {"accuracy": acc, "consensus": consensus, "n_items": n_items, "classes": classes}


def parse_leie_csv(text: str) -> List[Dict[str, Any]]:
    """Parse the OIG LEIE monthly CSV into ``[{npi, excl_type, excl_date}]``.

    Only rows carrying a real NPI are usable: the join in gate A5 is on NPI, and the LEIE's
    name columns are far too ambiguous to match a physician on. Roughly half the file has a
    blank NPI, and those rows are dropped rather than name-matched — a false positive on this
    gate ends someone's participation, so the only acceptable match is an exact identifier.

    Tolerant of the header casing and column-order drift the file has shown across years:
    columns are located by name, never by index.
    """
    import csv
    import io as _io

    rows: List[Dict[str, Any]] = []
    reader = csv.DictReader(_io.StringIO(text))
    if not reader.fieldnames:
        return rows
    lookup = {(name or "").strip().upper(): name for name in reader.fieldnames}
    npi_col = lookup.get("NPI")
    if not npi_col:
        return rows
    type_col = lookup.get("EXCLTYPE") or lookup.get("EXCL_TYPE")
    date_col = lookup.get("EXCLDATE") or lookup.get("EXCL_DATE")
    for rec in reader:
        npi = re.sub(r"\D", "", str(rec.get(npi_col) or ""))
        if len(npi) != 10 or npi == "0000000000":
            continue
        rows.append({
            "npi": npi,
            "excl_type": (rec.get(type_col) or "").strip() if type_col else None,
            "excl_date": (rec.get(date_col) or "").strip() if date_col else None,
        })
    return rows


# The estimator's resolution, measured rather than assumed. Over 60 synthetic runs, One-Coin
# Dawid–Skene on 60 items with a 30% gold subset separated a 0.95-accuracy annotator from a
# 0.55 one in 60/60 runs, but got the fine-grained 0.95 > 0.75 > 0.55 ordering wrong in 8/60.
# That is estimator variance at these sample sizes, not a defect — and it has a design
# consequence: measured_quality_z carries the largest prior weight in the model (m = 2.0), so
# an unshrunk noisy z would swing s by more than the entire domain_match term on the strength
# of a coin flip. Hence MQ_SHRINKAGE_K below.
MQ_SHRINKAGE_K = 40      # items at which the estimate is credited at half strength
MQ_MIN_ITEMS = 10        # below this, no z at all


def measured_quality_z(accuracy: float, population: Iterable[float],
                       *, n_items: Optional[int] = None) -> Optional[float]:
    """Turn a Dawid–Skene accuracy into the model's ``measured_quality_z``.

    A z-score needs a population. With fewer than five peers there is no meaningful spread, so
    this returns None and the feature stays structurally zero rather than inventing a scale —
    the same discipline ``normalize_domain`` applies to unrecognised specialty wording.

    ``n_items`` is the number of co-labelled items this physician actually appears in, which
    is what the estimate's precision depends on — NOT their total task count. The z is shrunk
    toward zero by ``n / (n + MQ_SHRINKAGE_K)``, so a physician with 10 co-labelled items
    contributes a fifth of what one with 200 does. Omitting ``n_items`` applies no shrinkage
    and is intended only for tests that are checking the standardization itself.
    """
    pop = [float(p) for p in population if p is not None]
    if len(pop) < 5:
        return None
    if n_items is not None and n_items < MQ_MIN_ITEMS:
        return None
    mean = sum(pop) / len(pop)
    var = sum((p - mean) ** 2 for p in pop) / (len(pop) - 1)
    sd = math.sqrt(var)
    if sd < 1e-6:
        return 0.0
    z = max(-3.0, min(3.0, (float(accuracy) - mean) / sd))
    if n_items is not None:
        z *= n_items / (n_items + MQ_SHRINKAGE_K)
    return z
