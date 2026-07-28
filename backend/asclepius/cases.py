"""Multimodal clinical cases — the ClinicalCase model + serialization
(Synthetic Multimodal Cases PRD §2, §5).

A task can be a small, realistic clinical *case* — a structured lab panel + one
or more EHR-style notes (plus vitals, meds, problem list, lab trends) — that the
specialist reasons ACROSS. This module owns the PHI-free, FHIR-mappable value
model + the text serialization that becomes ``task.prompt`` so packaging, export,
and every existing buyer profile keep working unchanged.

Design invariants (PRD §2 rules):
  * **No imaging.** There is no image field; images are never a gradable modality.
  * **Trends via relative offsets.** Labs carry ``collected_offset_days`` (0 =
    index day, −7 = a week earlier) — the clinically-vital interval survives, the
    identifying calendar date never exists.
  * **PHI-free by construction.** Age BANDS only (never exact age; 90+ collapsed),
    generalized author roles, no names/MRNs/dates/locations.
  * **Answer key is internal.** ``ground_truth`` / ``hard_hook`` /
    ``reasoning_divergence`` are generation/QA metadata — ``public_case`` strips
    them before a case is blinded to an evaluator or shipped to a buyer (only a
    held-out benchmark export may include the key behind an explicit flag).

The models are FHIR-mappable (Observation/Condition/MedicationStatement/
DocumentReference) so the real-de-identified-EHR adapter (a later phase) is a
drop-in with zero downstream change.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

# Out-of-range flags on a lab result (HL7-style). "" = within range.
LAB_FLAGS = ("", "L", "H", "LL", "HH")

# ── Content floors for a MULTIMODAL case (BUG-1 §2) ──────────────────────────
# A "multimodal" case that carries no labs is not a multimodal case. These are
# the hard content requirements ``assert_multimodal_content`` enforces before a
# generated case can be stored + stamped ``modality="multimodal"``. Env-tunable
# so the bar can be moved without a code change.
import os as _os


def _content_int(name: str, default: int) -> int:
    try:
        return int(_os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


class MultimodalContentError(ValueError):
    """A case does not carry the minimum multimodal content (labs + note +
    problem + medication). Raised by :func:`assert_multimodal_content` so the
    caller drops it as ``case_gen_failed`` — an empty case can never be stamped
    ``modality='multimodal'`` (BUG-1)."""

# Case provenance — synthetic now; real de-identified EHR later (same model).
CASE_SOURCES = ("synthetic", "real_deid")

# Task modality. "text" is the classic one-line prompt; "multimodal" carries a case.
MODALITIES = ("text", "multimodal")

# ── Structured studies (Specialty Hyper-Personalization PRD §3) ──────────────
# Cardiology reasoning lives in the ECG/echo/cath; oncology in pathology/imaging/
# molecular. Representing these as free-text notes only makes the cases weaker and
# the data less structured. ``Study`` is a light, additive, backward-compatible
# modality: existing nephrology cases simply carry ``studies: []``.
STUDY_MODALITIES = (
    "ecg", "echo", "cath", "ct", "mri", "pet", "pathology", "molecular", "other",
)

# Per-specialty REQUIRED study modalities (PRD §3 — "multimodal" must mean the
# RIGHT modality per specialty). Nephrology is labs-driven and unchanged.
CARDIOLOGY_STUDY_MODALITIES = ("ecg", "echo")
ONCOLOGY_IMAGING_MODALITIES = ("ct", "mri", "pet")
# Oncology is valid with ≥1 of pathology / imaging / molecular.
ONCOLOGY_STUDY_MODALITIES = ("pathology", "molecular") + ONCOLOGY_IMAGING_MODALITIES

# Accepted image asset MIME types for a V4 real-de-identified study (Image PRD §3.1).
STUDY_ASSET_MIMES = ("image/png", "image/jpeg", "application/pdf")


class LabResult(BaseModel):
    # extra="forbid" (BUG-1 §1): a key-name mismatch from the LLM (e.g. "result"
    # for "value", "reference_range" for ref_low/ref_high) must RAISE — never be
    # silently dropped into a structurally-valid-but-empty result.
    model_config = ConfigDict(extra="forbid")

    analyte: str
    loinc: Optional[str] = None            # FHIR/LOINC code when known (real data)
    value: Any                             # numeric or string (e.g. "muddy-brown casts")
    unit: Optional[str] = None
    ref_low: Optional[Any] = None
    ref_high: Optional[Any] = None
    flag: str = ""                         # L | H | LL | HH | ""


class LabPanel(BaseModel):
    """A grouped Observation set (BMP, ABG, urine studies…). ``collected_offset_days``
    is a RELATIVE day (0 = index, negative = earlier) so a trend across panels is
    preserved while no calendar date exists."""

    # extra="forbid" (BUG-1 §1): reject a mis-named panel key (e.g. "labs",
    # "tests") rather than coerce it into an empty panel.
    model_config = ConfigDict(extra="forbid")

    panel: str
    collected_offset_days: int = 0
    results: List[LabResult] = Field(default_factory=list)


class ClinicalNote(BaseModel):
    """A de-identified narrative (FHIR DocumentReference). ``author_role`` is a
    generalized category ("nephrology", "ICU") — never a person's name."""

    # extra="forbid" (BUG-1 §1): a mis-named note body key (e.g. "body",
    # "content") must raise, not silently produce an empty note.
    model_config = ConfigDict(extra="forbid")

    note_type: str = "Progress"            # H&P | Progress | Consult | Nursing
    author_role: str = "clinician"
    text: str = ""


class ProblemItem(BaseModel):
    condition: str
    since: Optional[str] = None            # generalized ("2019", "chronic") — never a date


class MedicationItem(BaseModel):
    drug: str
    dose: Optional[str] = None
    route: Optional[str] = None
    freq: Optional[str] = None


class Demographics(BaseModel):
    age_band: Optional[str] = None         # "70-79", "90+" — never an exact age
    sex: Optional[str] = None


class GroundTruth(BaseModel):
    """INTERNAL ONLY (generation/QA). The objectively correct, guideline/lab-
    anchored answer used to seed candidate answers + gate hardness. ``public_case``
    strips this — it never reaches a blinded evaluator or a normal export."""

    answer: str = ""
    rationale: Optional[str] = None
    evidence: Optional[Dict[str, Any]] = None   # anchor-shaped; kept dict to avoid a schema cycle
    key_data: List[str] = Field(default_factory=list)


class StudyAsset(BaseModel):
    """A reference to a real de-identified image asset attached to a Study (V4 Image
    PRD §2). The image BYTES are NEVER stored on the ClinicalCase or in asclepius.db —
    only this opaque reference, resolved via the content-addressed asset store
    (:mod:`asclepius.assets`). ``sha256`` is identity, dedupe, AND the A/B integrity
    check (the same bytes must reach both frontier providers)."""

    model_config = ConfigDict(extra="forbid")

    asset_id: str                          # opaque id; resolves via the asset store
    mime: str                              # image/png | image/jpeg | application/pdf
    sha256: str                            # content hash — identity + A/B integrity
    width: Optional[int] = None            # px (raster); page count for PDF
    height: Optional[int] = None
    byte_size: int = 0
    page: Optional[int] = None             # for a multi-page PDF, the rendered page
    page_count: Optional[int] = None       # total pages (PDF)
    source: str = "partner_deidentified"   # provenance stamp (never a partner id)


class Study(BaseModel):
    """A structured study report — the decisive signal in cardiology/oncology cases
    often lives here (PRD §3). ``findings`` is the structured report text (the
    reasoning anchor, always required even when an image ``asset`` is attached);
    ``measurements`` reuse :class:`LabResult` so EF %, valve gradient, PET SUVmax, and
    molecular VAF are structured + gradeable, not buried in prose."""

    # extra="forbid" (BUG-1 §1): a mis-named study key must raise, not be dropped.
    model_config = ConfigDict(extra="forbid")

    modality: str                          # ecg | echo | cath | ct | mri | pet | pathology | molecular | other
    label: str = ""                        # "12-lead ECG", "TTE", "Core biopsy", "NGS panel"
    findings: str = ""                     # the structured report text (decisive signal)
    measurements: List[LabResult] = Field(default_factory=list)
    impression: Optional[str] = None
    asset: Optional[StudyAsset] = None      # NEW (V4): optional real image reference


class ClinicalCase(BaseModel):
    # extra="forbid" (BUG-1 §1): THE critical one. If the LLM returns ``labs``
    # instead of ``lab_panels`` (or nests results differently, or omits them),
    # pydantic's default ``extra='ignore'`` would silently drop the key and yield
    # a structurally-valid ClinicalCase with ZERO labs — a silent empty case.
    # Forbidding extras makes that a hard ValidationError → caught by the caller
    # → counted as ``case_gen_failed``. Never again a silent empty case.
    model_config = ConfigDict(extra="forbid")

    case_id: Optional[str] = None
    case_source: str = "synthetic"         # synthetic | real_deid
    specialty: str = "general"
    demographics: Demographics = Field(default_factory=Demographics)
    problem_list: List[ProblemItem] = Field(default_factory=list)
    medications: List[MedicationItem] = Field(default_factory=list)
    vitals: Dict[str, Any] = Field(default_factory=dict)
    lab_panels: List[LabPanel] = Field(default_factory=list)
    notes: List[ClinicalNote] = Field(default_factory=list)
    # Structured studies (PRD §3): ECG/echo/cath (cardiology), pathology/imaging/
    # molecular (oncology), renal biopsy/US (nephrology). Additive + backward
    # compatible — existing cases carry an empty list.
    studies: List[Study] = Field(default_factory=list)
    # ── internal-only generation/QA metadata (never shipped raw) ──
    ground_truth: Optional[GroundTruth] = None
    hard_hook: Optional[str] = None
    reasoning_divergence: Optional[str] = None


# Keys stripped from a case before it is blinded to an evaluator or shipped — the
# answer key must never leak (mirrors how ``intended_flawed_id`` is stripped).
_INTERNAL_CASE_KEYS = ("ground_truth", "hard_hook", "reasoning_divergence")


def public_case(case: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return the case with all internal answer-key metadata removed. Accepts a
    dict (the stored/serialized shape) and returns a new dict; None → None. Safe
    to call on an already-public case (idempotent)."""
    if not case or not isinstance(case, dict):
        return None
    return {k: v for k, v in case.items() if k not in _INTERNAL_CASE_KEYS}


def _lab_result_ok(r: Dict[str, Any]) -> bool:
    """A lab result carries enough to force INTERPRETATION (not just reading):
    an analyte, a value, a unit, and a reference range OR an out-of-range flag."""
    if not isinstance(r, dict):
        return False
    if not str(r.get("analyte") or "").strip():
        return False
    if r.get("value") in (None, ""):
        return False
    if not str(r.get("unit") or "").strip():
        return False
    has_range = (r.get("ref_low") is not None) or (r.get("ref_high") is not None)
    has_flag = bool(str(r.get("flag") or "").strip())
    return has_range or has_flag


def _study_modality(s: Dict[str, Any]) -> str:
    return str((s or {}).get("modality") or "").strip().lower()


def study_has_valid_asset(s: Optional[Dict[str, Any]]) -> bool:
    """True when a study carries a resolvable image asset (V4 Image PRD §2): an
    ``asset`` with an ``asset_id``, a ``sha256``, and an accepted ``mime``."""
    if not isinstance(s, dict):
        return False
    a = s.get("asset")
    if not isinstance(a, dict):
        return False
    return bool(
        str(a.get("asset_id") or "").strip()
        and str(a.get("sha256") or "").strip()
        and str(a.get("mime") or "").strip().lower() in STUDY_ASSET_MIMES
    )


def required_study_modalities(specialty: str) -> tuple:
    """The study modalities a case of ``specialty`` MUST carry ≥1 of (PRD §3).
    Nephrology (and any unlisted specialty) has no study requirement."""
    sp = (specialty or "").strip().lower()
    if sp == "cardiology":
        return CARDIOLOGY_STUDY_MODALITIES
    if sp == "oncology":
        return ONCOLOGY_STUDY_MODALITIES
    return ()


def _assert_specialty_studies(specialty: str, studies: List[Dict[str, Any]]) -> None:
    """Strengthen the gate per specialty (PRD §3): a cardiology case must carry ≥1
    ``ecg``/``echo`` study; an oncology case ≥1 of ``pathology``/imaging/``molecular``.
    Nephrology is unchanged (labs-driven, no study requirement)."""
    required = required_study_modalities(specialty)
    if not required:
        return
    present = {_study_modality(s) for s in studies}
    if not (present & set(required)):
        raise MultimodalContentError(
            f"{specialty} case must carry ≥1 study of modality {sorted(required)}; "
            f"found {sorted(m for m in present if m) or 'none'}"
        )


def assert_multimodal_content(case: Optional[Dict[str, Any]]) -> None:
    """Hard content gate for a multimodal case (BUG-1 §2; PRD §3). Raises
    :class:`MultimodalContentError` unless the case carries:

      * the per-specialty STUDY requirement (cardiology ≥1 ecg/echo; oncology ≥1
        pathology/imaging/molecular; nephrology none) — this STRENGTHENS the gate;
      * AND either the text content floor OR — for a V4 real image case — ≥1 study
        carrying a valid image ``asset`` with non-empty ``findings`` (V4 Image PRD §3).

    The text content floor (never weakened):
      * ≥ ``ASCLEPIUS_CASE_MIN_LAB_PANELS`` (default 1) lab panel(s) with, in
        total, ≥ ``ASCLEPIUS_CASE_MIN_LAB_RESULTS`` (default 2) results that each
        carry analyte + value + unit + (ref range OR flag);
      * ≥ 1 note with ≥ ``ASCLEPIUS_CASE_MIN_NOTE_CHARS`` (default 200) chars;
      * ≥ 1 problem AND ≥ 1 medication.

    Called in ``critic.generate_case`` before returning and again in
    ``generation.py`` before ``insert_task`` — defense in depth so an empty case can
    never be stamped ``modality='multimodal'``."""
    c = as_dict(case) or {}
    studies = [s for s in (c.get("studies") or []) if isinstance(s, dict)]

    # (1) Per-specialty study requirement — always enforced (strengthen).
    _assert_specialty_studies(c.get("specialty") or "", studies)

    # (2) A V4 image-bearing study (valid asset + non-empty findings) satisfies the
    #     multimodal content requirement on its own (V4 Image PRD §3.2). The text
    #     gate below is skipped ONLY for such real image cases — never weakened for
    #     text cases.
    image_study = any(
        study_has_valid_asset(s) and str(s.get("findings") or "").strip()
        for s in studies
    )
    if image_study and (c.get("case_source") == "real_deid"):
        if not (c.get("problem_list") or []):
            raise MultimodalContentError("image case has an empty problem_list")
        return

    # (3) The text content floor (never weakened).
    min_panels = _content_int("ASCLEPIUS_CASE_MIN_LAB_PANELS", 1)
    min_results = _content_int("ASCLEPIUS_CASE_MIN_LAB_RESULTS", 2)
    min_note_chars = _content_int("ASCLEPIUS_CASE_MIN_NOTE_CHARS", 200)

    panels = [p for p in (c.get("lab_panels") or []) if isinstance(p, dict)]
    if len(panels) < min_panels:
        raise MultimodalContentError(
            f"case has {len(panels)} lab panel(s); needs ≥ {min_panels}"
        )
    good_results = sum(
        1 for p in panels for r in (p.get("results") or []) if _lab_result_ok(r)
    )
    if good_results < min_results:
        raise MultimodalContentError(
            f"case has {good_results} well-formed lab result(s) "
            f"(analyte+value+unit+range/flag); needs ≥ {min_results}"
        )

    notes = c.get("notes") or []
    if not any(len(str((n or {}).get("text") or "").strip()) >= min_note_chars for n in notes):
        raise MultimodalContentError(
            f"case has no clinical note ≥ {min_note_chars} chars"
        )

    if not (c.get("problem_list") or []):
        raise MultimodalContentError("case has an empty problem_list")
    if not (c.get("medications") or []):
        raise MultimodalContentError("case has an empty medication list")


def case_type_signature(case: Optional[Dict[str, Any]]) -> str:
    """The modality signature a buyer filters on (PRD §2), e.g.
    ``multimodal:labs+ecg+echo`` or ``multimodal:real+ct_image+molecular``. Derived
    from the case's ``lab_panels``, ``notes``, and ``studies`` (image-bearing studies
    add an ``_image`` suffix + a ``real`` prefix for a real de-identified case)."""
    c = as_dict(case) or {}
    parts: List[str] = []
    is_real = c.get("case_source") == "real_deid"
    if is_real:
        parts.append("real")
    if c.get("lab_panels"):
        parts.append("labs")
    if c.get("notes"):
        parts.append("notes")
    seen = set()
    for s in (c.get("studies") or []):
        if not isinstance(s, dict):
            continue
        m = str(s.get("modality") or "study").strip().lower()
        tag = m + ("_image" if study_has_valid_asset(s) else "")
        if tag not in seen:
            seen.add(tag)
            parts.append(tag)
    if not parts:
        return "multimodal"
    return "multimodal:" + "+".join(parts)


def as_dict(case: Any) -> Optional[Dict[str, Any]]:
    """Normalize a ClinicalCase / dict / None into a plain dict (or None)."""
    if case is None:
        return None
    if isinstance(case, ClinicalCase):
        return case.model_dump()
    if isinstance(case, BaseModel):
        return case.model_dump()
    if isinstance(case, dict):
        return case
    return None


def _fmt_offset(offset: int) -> str:
    if offset == 0:
        return "day 0 (today)"
    return f"day {offset:+d}"


def _ref_range(r: Dict[str, Any]) -> str:
    lo, hi = r.get("ref_low"), r.get("ref_high")
    if lo is None and hi is None:
        return "—"
    return f"{'' if lo is None else lo}–{'' if hi is None else hi}"


def _render_labs(lab_panels: List[Dict[str, Any]]) -> str:
    if not lab_panels:
        return ""
    # Oldest → newest so a reader sees the trend direction naturally.
    panels = sorted(lab_panels, key=lambda p: int(p.get("collected_offset_days", 0) or 0))
    lines: List[str] = ["Labs:"]
    for p in panels:
        lines.append(f"  [{p.get('panel', 'Panel')}] — {_fmt_offset(int(p.get('collected_offset_days', 0) or 0))}")
        lines.append(f"    {'Analyte':<26}{'Value':<12}{'Unit':<12}{'Ref':<14}Flag")
        for r in p.get("results", []) or []:
            analyte = str(r.get("analyte", ""))[:25]
            value = str(r.get("value", ""))
            unit = str(r.get("unit") or "")
            flag = str(r.get("flag") or "")
            lines.append(f"    {analyte:<26}{value:<12}{unit:<12}{_ref_range(r):<14}{flag}")
    return "\n".join(lines)


def _render_studies(studies: List[Dict[str, Any]]) -> str:
    """Render structured studies (ECG/echo/cath/imaging/pathology/molecular) as text
    so a model MUST read the decisive finding into its reasoning (PRD §3). Numeric
    ``measurements`` render as a compact table (EF, gradient, SUVmax, VAF)."""
    studies = [s for s in (studies or []) if isinstance(s, dict)]
    if not studies:
        return ""
    lines: List[str] = ["Studies:"]
    for s in studies:
        modality = str(s.get("modality") or "study").upper()
        label = str(s.get("label") or "").strip()
        head = f"  [{modality}]" + (f" — {label}" if label else "")
        if s.get("asset"):
            head += "  (image attached)"
        lines.append(head)
        findings = str(s.get("findings") or "").strip()
        if findings:
            lines.append(f"    Findings: {findings}")
        measurements = [r for r in (s.get("measurements") or []) if isinstance(r, dict)]
        if measurements:
            lines.append(f"    {'Measure':<26}{'Value':<12}{'Unit':<12}{'Ref':<14}Flag")
            for r in measurements:
                analyte = str(r.get("analyte", ""))[:25]
                value = str(r.get("value", ""))
                unit = str(r.get("unit") or "")
                flag = str(r.get("flag") or "")
                lines.append(f"    {analyte:<26}{value:<12}{unit:<12}{_ref_range(r):<14}{flag}")
        impression = str(s.get("impression") or "").strip()
        if impression:
            lines.append(f"    Impression: {impression}")
    return "\n".join(lines)


def render_case_prompt(case: Any, question: str) -> str:
    """Render a human/model-readable prompt: the clinical question, then the case
    (labs as a compact table, notes verbatim, meds/problems/vitals). This string
    becomes ``task.prompt`` so packaging/export/buyers are unchanged (PRD §5).

    Always renders the PUBLIC case — the answer key is stripped even if a
    caller passes the full case by mistake."""
    c = public_case(as_dict(case)) or {}
    parts: List[str] = []
    q = (question or "").strip()
    if q:
        parts.append(f"CLINICAL QUESTION:\n{q}")

    header = "CLINICAL CASE"
    demo = c.get("demographics") or {}
    who = " ".join(x for x in [demo.get("sex"), (f"age {demo['age_band']}" if demo.get("age_band") else None)] if x)
    parts.append(header + (f"\nPatient: {who}" if who else ""))

    problems = c.get("problem_list") or []
    if problems:
        parts.append("Problem list:\n" + "\n".join(
            f"  - {p.get('condition','')}" + (f" (since {p['since']})" if p.get("since") else "")
            for p in problems))

    meds = c.get("medications") or []
    if meds:
        parts.append("Medications:\n" + "\n".join(
            "  - " + " ".join(x for x in [m.get("drug"), m.get("dose"), m.get("route"), m.get("freq")] if x)
            for m in meds))

    vitals = c.get("vitals") or {}
    if vitals:
        parts.append("Vitals: " + ", ".join(f"{k} {v}" for k, v in vitals.items() if v is not None))

    labs = _render_labs(c.get("lab_panels") or [])
    if labs:
        parts.append(labs)

    studies = _render_studies(c.get("studies") or [])
    if studies:
        parts.append(studies)

    for n in c.get("notes") or []:
        role = n.get("author_role") or "clinician"
        parts.append(f"[{n.get('note_type','Note')} — {role}]\n{(n.get('text') or '').strip()}")

    return "\n\n".join(p for p in parts if p and p.strip())


def is_multimodal(task: Dict[str, Any]) -> bool:
    """A task is multimodal when it carries a structured case WITH CONTENT (labs
    and/or notes) or an explicit modality flag (BUG-1 §3). Central so
    router/packaging/frontend/value agree on one CONTENT-derived rule — a case
    dict that carries no labs and no notes is NOT multimodal (an empty case can
    never be labeled multimodal by presence alone)."""
    if not task:
        return False
    if (task.get("modality") or "text") == "multimodal":
        return True
    case = task.get("case")
    return bool(case and (case.get("lab_panels") or case.get("notes") or case.get("studies")))
