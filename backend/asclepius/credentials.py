"""Credential tiering — blurbs, verification dossiers, and the Tier B leak gate
(Contributors view + tiered export feature).

The governing rule (spec §4): buyer-facing "Export Data" records carry credential
ATTRIBUTES only (Tier A); anything that identifies or locates the physician
(Tier B) lives in the private vault and is released ONLY inside a "Further
Credential Summary" dossier under NDA / non-circumvention.

This module is pure (no DB / no HTTP). It produces:
  * a generalized, non-identifying ``blurb`` from Tier A attributes,
  * independent verification handles (NPPES / ABMS lookup links) from Tier B,
  * the dossier JSON (Tier A + Tier B + handles + §9 notice + watermark),
  * a dependency-free PDF rendering of the dossier, and
  * the ``find_tier_b_leak`` scanner used by the export hard-gate.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from asclepius.constants import (
    ASCLEPIUS_CONFIG_VERSION,
    ASCLEPIUS_TAXONOMY_VERSION,
    CREDENTIAL_SUMMARY_LEGAL_DISCLAIMER,
    CREDENTIAL_SUMMARY_WATERMARK,
    TIER_A_SHIP_FIELDS,
    TIER_B_FORBIDDEN_KEYS,
    TIER_B_VERIFY_FIELDS,
    company_name,
    non_circumvention_notice,
)


# ─── Tier A blurb (generalized; never identifying) ────────────────────────────
def _years_phrase(years: Any) -> Optional[str]:
    try:
        y = int(years)
    except (TypeError, ValueError):
        return None
    return f"~{y} yrs active practice" if y > 0 else None


def generalized_blurb(ship: Dict[str, Any], *, fallback_specialty: Optional[str] = None) -> str:
    """A short generalized credential summary from Tier A only — e.g.
    "Board-certified, fellowship-trained nephrologist, ~17 yrs active practice,
    dialysis/transplant focus. NPI-verified." No institution or name."""
    ship = ship or {}
    specialty = ship.get("primary_specialty") or fallback_specialty or "clinician"
    # Read better as a practitioner noun ("nephrology" -> "nephrology specialist")
    # without risky irregular pluralization.
    sp = str(specialty).strip()
    if sp and sp.lower() not in ("clinician",) and not sp.lower().endswith(("ist", "ian", "specialist")):
        specialty = f"{sp} specialist"
    bits: List[str] = []

    if ship.get("board_certifications"):
        bits.append("Board-certified")
    if ship.get("fellowship_trained"):
        bits.append("fellowship-trained")

    lead = ", ".join(bits)
    # "Board-certified, fellowship-trained nephrologist"
    head = f"{lead} {specialty}".strip() if lead else str(specialty).capitalize()

    tail: List[str] = []
    yp = _years_phrase(ship.get("years_in_active_practice"))
    if yp:
        tail.append(yp)
    subs = ship.get("subspecialties") or []
    if isinstance(subs, list) and subs:
        tail.append("/".join(str(s) for s in subs[:3]) + " focus")
    elif isinstance(subs, str) and subs.strip():
        tail.append(subs.strip() + " focus")

    sentence = head + (", " + ", ".join(tail) if tail else "")
    sentence = sentence[0].upper() + sentence[1:] if sentence else sentence
    if not sentence.endswith("."):
        sentence += "."
    if ship.get("credentials_verified"):
        sentence += " NPI-verified."
    return sentence


# ─── Structured record-level credential block (Buyer Response PRD §6 E2) ──────
# Fields that must NEVER appear at ANY tier of the structured block — the buyer must
# be able to trust the credential without being able to route around us to the
# physician (disintermediation risk). ``years_experience`` becomes a BAND: an exact
# integer plus specialty plus state is close to identifying in a small subspecialty.
_NEVER_IN_CREDENTIAL_BLOCK = (
    "npi", "name", "legal_name", "full_name", "institution", "organization", "email",
    "years_experience", "years_in_active_practice", "medical_license_number",
    "license_number", "license", "city", "address", "phone",
)


def years_experience_band(years: Any) -> Optional[str]:
    """Collapse exact years of experience into a band (Buyer Response PRD §6 E2). An
    exact integer never ships — it is close to identifying in a small subspecialty."""
    if years in (None, ""):
        return None
    try:
        y = int(years)
    except (TypeError, ValueError):
        return None
    if y < 0:
        return None
    for lo, hi in ((0, 1), (2, 4), (5, 9), (10, 14), (15, 19), (20, 24)):
        if y <= hi:
            return f"{lo}-{hi}"
    return "25+"


def structured_credential_block(
    *, id_hashed: str, credential: str, ship: Optional[Dict[str, Any]] = None,
    verify: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """The record-level structured credential block (Buyer Response PRD §6 E2):
    board certification is VERIFIABLE, identity is NOT. Emits board_certified,
    certifying_board, board_specialty, subspecialty, certification_status, the
    third-party verification method + authority (the credibility move: someone
    auditable verified the credential, without the buyer being able to verify WHICH
    physician), a years-experience BAND, and license/NPI-verified booleans — never the
    NPI, name, institution, email, exact years, or license number themselves."""
    ship = ship or {}
    verify = verify or {}
    subs = ship.get("subspecialties") or []
    if isinstance(subs, str):
        subs = [subs] if subs.strip() else []
    block = {
        "id_hashed": id_hashed,
        "credential": credential,
        "board_certified": bool(ship.get("board_certifications")),
        "certifying_board": ship.get("certifying_board"),
        "board_specialty": ship.get("primary_specialty"),
        "subspecialty": [str(s) for s in subs[:3]],
        "certification_status": ship.get("certification_status") or (
            "active" if ship.get("board_certifications") else None),
        "certification_verified_at": ship.get("certification_verified_at"),
        "verification_method": ship.get("verification_method") or (
            "ABMS_certification_matters" if ship.get("credentials_verified") else None),
        "verification_authority": ship.get("verification_authority") or (
            "American Board of Medical Specialties" if ship.get("credentials_verified") else None),
        "years_experience_band": years_experience_band(ship.get("years_in_active_practice")),
        "practice_setting": ship.get("practice_setting"),
        "state_licensed": bool(verify.get("license_state")),
        "npi_verified": bool(ship.get("credentials_verified") or verify.get("npi")),
    }
    # Belt-and-suspenders: guarantee no identifying key leaked into the block.
    for k in _NEVER_IN_CREDENTIAL_BLOCK:
        block.pop(k, None)
    return block


# ─── Independent verification handles (from Tier B) ───────────────────────────
def verification_handles(verify: Dict[str, Any]) -> Dict[str, Any]:
    """Public, independent lookup handles a lab can use to verify the credential
    without us as the source of truth: the NPPES NPI registry and the ABMS board-
    certification lookup."""
    verify = verify or {}
    handles: Dict[str, Any] = {}
    npi = (str(verify.get("npi")) if verify.get("npi") else "").strip()
    if npi:
        handles["nppes_npi_lookup"] = f"https://npiregistry.cms.hhs.gov/provider-view/{npi}"
        handles["nppes_npi_api"] = (
            f"https://npiregistry.cms.hhs.gov/api/?number={npi}&version=2.1"
        )
    handles["abms_certification_lookup"] = "https://www.certificationmatters.org/find-my-doctor/"
    state = (verify.get("license_state") or "").strip()
    lic = (str(verify.get("medical_license_number")) if verify.get("medical_license_number") else "").strip()
    if state and lic:
        handles["state_license_board"] = (
            f"State medical board verification ({state}) — license {lic}"
        )
    return handles


# ─── Dossier assembly ─────────────────────────────────────────────────────────
def build_dossier(
    *,
    id_hashed: str,
    organization: Optional[str],
    role_title: Optional[str],
    blurb: Optional[str],
    ship: Dict[str, Any],
    verify: Dict[str, Any],
    recipient: Optional[str] = None,
    generated_by: Optional[str] = None,
    generated_at: Optional[str] = None,
) -> Dict[str, Any]:
    """The full credential record (Tier B + Tier A + verification handles),
    keyed by ``hashed_annotator_id`` so the lab can match the dossier to the
    exact records they received via Export Data (spec §6)."""
    ship = dict(ship or {})
    verify = dict(verify or {})
    generated_at = generated_at or (datetime.utcnow().isoformat() + "Z")
    resolved_blurb = blurb or generalized_blurb(ship)

    return {
        "document_type": "credential_verification_summary",
        "watermark": CREDENTIAL_SUMMARY_WATERMARK,
        "company": company_name(),
        "hashed_annotator_id": id_hashed,  # matches the shipped records
        "organization": organization,
        "role_title": role_title,
        "blurb": resolved_blurb,
        # Tier A — the same attributes that ship on the records.
        "credential_attributes": {
            "hashed_annotator_id": id_hashed,
            **{k: ship.get(k) for k in TIER_A_SHIP_FIELDS if k in ship and k != "hashed_annotator_id"},
        },
        # Tier B — the private, identifying credentials (vault).
        "identifying_credentials": {k: verify.get(k) for k in TIER_B_VERIFY_FIELDS if k in verify},
        # Independent verification handles.
        "verification_handles": verification_handles(verify),
        "non_circumvention_notice": non_circumvention_notice(),
        "legal_disclaimer": CREDENTIAL_SUMMARY_LEGAL_DISCLAIMER,
        "intended_recipient": recipient,
        "generated_by": generated_by,
        "generated_at": generated_at,
        "taxonomy_version": ASCLEPIUS_TAXONOMY_VERSION,
        "config_version": ASCLEPIUS_CONFIG_VERSION,
    }


# ─── Tier B leak gate (THE CORE RULE) ─────────────────────────────────────────
def _iter_keys(obj: Any):
    """Yield every dict key appearing anywhere in a nested structure."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _iter_keys(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_keys(item)


def find_tier_b_leak(mapped: Dict[str, Any]) -> Optional[str]:
    """Return the name of the first Tier B / identifying field found anywhere in a
    mapped, buyer-facing record — or None if the record is clean. Used by the
    export hard-gate to reject the whole batch loudly on any leak."""
    forbidden = {k.lower() for k in TIER_B_FORBIDDEN_KEYS}
    for key in _iter_keys(mapped):
        if isinstance(key, str) and key.lower() in forbidden:
            return key
    return None


# Only these high-specificity LOCATOR / identifier vault fields feed the value
# scan. Institution / pedigree names (medical_school, residency, fellowship) and
# short codes (license_state) are deliberately excluded: they can legitimately
# appear in clinical text (e.g. "per the UCSF protocol"), so scanning them would
# false-positive and block valid exports. Those fields can never reach a shipped
# record anyway — they are withheld by the field-name leak gate.
_VALUE_SCAN_FIELDS = (
    "full_legal_name",
    "npi",
    "medical_license_number",
    "practice_address",
    "practice_contact",
    "practice_phone",
    "practice_email",
)

# Minimum value length to scan, so a coincidental short token never trips it.
_VALUE_SCAN_MIN_LEN = 6


def find_tier_b_value_leak(mapped: Dict[str, Any], verify_values: List[str]) -> Optional[str]:
    """Defense in depth: return the first Tier B *value* (a legal name, NPI,
    license number, or practice locator from the vault) that appears verbatim in
    the serialized record, or None. Only used when the relevant vault values are
    known (per-contributor / per-org export)."""
    if not verify_values:
        return None
    blob = json.dumps(mapped, ensure_ascii=False).lower()
    for val in verify_values:
        v = str(val or "").strip().lower()
        if len(v) >= _VALUE_SCAN_MIN_LEN and v in blob:
            return val
    return None


def collect_verify_values(verify_blocks: List[Dict[str, Any]]) -> List[str]:
    """Flatten the high-specificity identifying values from one or more vault
    dicts into a flat list of strings to scan exported records against. Only the
    locator/identifier fields in ``_VALUE_SCAN_FIELDS`` are included."""
    out: List[str] = []
    for vb in verify_blocks or []:
        for key in _VALUE_SCAN_FIELDS:
            v = (vb or {}).get(key)
            if isinstance(v, (str, int)) and str(v).strip():
                out.append(str(v).strip())
    return out


# ─── Dependency-free PDF rendering ────────────────────────────────────────────
# The writer itself moved to asclepius/pdf_render.py when the signed data
# licensing agreement needed the same one. The names below are re-exported so
# this module's call sites, and anything vendored against them, are unchanged.

from asclepius.pdf_render import (  # noqa: E402
    MAX_CHARS as _MAX_CHARS,
    assemble_pdf as _assemble_pdf,
    pdf_escape as _pdf_escape,
    render_text_pdf as _render_text_pdf,
    wrap as _wrap,
)

_WATERMARK_SIZE = 8   # kept: the dossier's banner size is its own decision


def _dossier_lines(dossier: Dict[str, Any]) -> List[tuple]:
    """Produce ``(kind, text)`` tuples; kind ∈ {head, sub, body, gap}."""
    lines: List[tuple] = []

    def head(t: str):
        lines.append(("head", t))

    def sub(t: str):
        lines.append(("sub", t))

    def body(t: str):
        for w in _wrap(t):
            lines.append(("body", w))

    def gap():
        lines.append(("gap", ""))

    head("Credential Verification Summary")
    body(f"{dossier.get('company', '')}")
    body(f"Hashed annotator id: {dossier.get('hashed_annotator_id', '')}")
    if dossier.get("organization"):
        body(f"Organization: {dossier['organization']}")
    if dossier.get("role_title"):
        body(f"Role: {dossier['role_title']}")
    body(f"Generated: {dossier.get('generated_at', '')}")
    if dossier.get("intended_recipient"):
        body(f"Intended recipient: {dossier['intended_recipient']}")
    gap()
    body(dossier.get("blurb", ""))
    gap()

    sub("Identifying credentials (Tier B — verification only)")
    ic = dossier.get("identifying_credentials") or {}
    if ic:
        for k, v in ic.items():
            body(f"  - {k.replace('_', ' ')}: {v}")
    else:
        body("  - (none on file)")
    gap()

    sub("Credential attributes (Tier A — matches shipped records)")
    ca = dossier.get("credential_attributes") or {}
    for k, v in ca.items():
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v)
        body(f"  - {k.replace('_', ' ')}: {v}")
    gap()

    sub("Independent verification handles")
    for k, v in (dossier.get("verification_handles") or {}).items():
        body(f"  - {k.replace('_', ' ')}: {v}")
    gap()

    sub("Non-circumvention & confidentiality notice")
    body(dossier.get("non_circumvention_notice", ""))
    gap()
    body(dossier.get("legal_disclaimer", ""))
    return lines


def render_dossier_pdf(dossier: Dict[str, Any]) -> bytes:
    """Render the dossier to a valid multi-page PDF (Helvetica), with the
    confidential watermark on every page. Dependency-free."""
    watermark = dossier.get("watermark") or CREDENTIAL_SUMMARY_WATERMARK
    return _render_text_pdf(_dossier_lines(dossier), banner=watermark)


