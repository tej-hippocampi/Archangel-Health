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


def find_tier_b_value_leak(mapped: Dict[str, Any], verify_values: List[str]) -> Optional[str]:
    """Defense in depth: return the first Tier B *value* (e.g. a legal name or NPI
    from the vault) that appears verbatim in the serialized record, or None. Only
    used when the relevant vault values are known (per-contributor / per-org
    export)."""
    if not verify_values:
        return None
    blob = json.dumps(mapped, ensure_ascii=False).lower()
    for val in verify_values:
        v = str(val or "").strip().lower()
        if len(v) >= 4 and v in blob:
            return val
    return None


def collect_verify_values(verify_blocks: List[Dict[str, Any]]) -> List[str]:
    """Flatten the identifying values from one or more vault dicts into a flat
    list of strings to scan exported records against."""
    out: List[str] = []
    for vb in verify_blocks or []:
        for v in (vb or {}).values():
            if isinstance(v, (str, int)) and str(v).strip():
                out.append(str(v).strip())
    return out


# ─── Dependency-free PDF rendering ────────────────────────────────────────────
# A minimal, correct single-font PDF writer. Avoids adding a heavyweight PDF
# dependency to the runtime: the dossier is plain text (watermark, §9 notice,
# credential fields), which this lays out across as many Letter pages as needed.

_PAGE_W, _PAGE_H = 612, 792           # US Letter, 72 dpi
_MARGIN = 54
_LINE_H = 14
_FONT_SIZE = 10
_HEAD_SIZE = 15
_WATERMARK_SIZE = 8
_MAX_CHARS = 92                        # wrap width at 10pt Helvetica


def _pdf_escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _wrap(text: str, width: int = _MAX_CHARS) -> List[str]:
    out: List[str] = []
    for raw in (text or "").split("\n"):
        raw = raw.rstrip()
        if not raw:
            out.append("")
            continue
        line = ""
        for word in raw.split(" "):
            if not line:
                line = word
            elif len(line) + 1 + len(word) <= width:
                line += " " + word
            else:
                out.append(line)
                line = word
            # hard-break absurdly long tokens
            while len(line) > width:
                out.append(line[:width])
                line = line[width:]
        out.append(line)
    return out


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
    rows = _dossier_lines(dossier)

    # Paginate into content streams.
    usable_top = _PAGE_H - _MARGIN - 24      # leave room for the watermark band
    usable_bottom = _MARGIN
    lines_per_page = int((usable_top - usable_bottom) / _LINE_H)

    pages: List[str] = []
    cur: List[str] = []

    def flush():
        if cur:
            pages.append("".join(cur))

    def begin_page():
        cur.clear()
        # watermark band
        cur.append("BT /F1 %d Tf 1 0 0 1 %d %d Tm (%s) Tj ET\n" % (
            _WATERMARK_SIZE, _MARGIN, _PAGE_H - _MARGIN + 6, _pdf_escape(watermark)))

    y_state = {"y": usable_top}
    begin_page()
    count = 0
    for kind, text in rows:
        if count >= lines_per_page:
            flush()
            begin_page()
            y_state["y"] = usable_top
            count = 0
        y = y_state["y"]
        if kind == "gap":
            pass
        else:
            size = _HEAD_SIZE if kind == "head" else (_FONT_SIZE if kind != "sub" else 11)
            cur.append("BT /F1 %d Tf 1 0 0 1 %d %d Tm (%s) Tj ET\n" % (
                size, _MARGIN, int(y), _pdf_escape(text)))
        y_state["y"] = y - (_LINE_H + (6 if kind in ("head", "sub") else 0))
        count += 1
    flush()
    if not pages:
        begin_page()
        flush()

    return _assemble_pdf(pages)


def _assemble_pdf(page_streams: List[str]) -> bytes:
    """Build the PDF object graph (catalog, pages, per-page content + font)."""
    objects: List[bytes] = []

    def add(obj: bytes) -> int:
        objects.append(obj)
        return len(objects)  # 1-based object number

    font_num = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    # Reserve catalog (1) and pages (2) numbers by placing them first conceptually;
    # we build content + page objects, then pages tree, then catalog.
    page_obj_nums: List[int] = []
    content_nums: List[int] = []
    for stream in page_streams:
        data = stream.encode("latin-1", "replace")
        content = b"<< /Length %d >>\nstream\n%s\nendstream" % (len(data), data)
        content_nums.append(add(content))

    # placeholder for pages tree number (filled after page objects exist)
    pages_tree_num = len(objects) + len(page_streams) + 1
    for cnum in content_nums:
        page = (
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
            % (pages_tree_num, _PAGE_W, _PAGE_H, font_num, cnum)
        )
        page_obj_nums.append(add(page))

    kids = b" ".join(b"%d 0 R" % n for n in page_obj_nums)
    pages_tree = b"<< /Type /Pages /Count %d /Kids [%s] >>" % (len(page_obj_nums), kids)
    pages_tree_actual = add(pages_tree)
    assert pages_tree_actual == pages_tree_num, (pages_tree_actual, pages_tree_num)
    catalog_num = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_tree_num)

    # Serialize with a cross-reference table.
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0] * (len(objects) + 1)
    for i, obj in enumerate(objects, start=1):
        offsets[i] = len(out)
        out += b"%d 0 obj\n" % i
        out += obj
        out += b"\nendobj\n"
    xref_pos = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for i in range(1, len(objects) + 1):
        out += b"%010d 00000 n \n" % offsets[i]
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\n" % (len(objects) + 1, catalog_num)
    out += b"startxref\n%d\n%%%%EOF\n" % xref_pos
    return bytes(out)
