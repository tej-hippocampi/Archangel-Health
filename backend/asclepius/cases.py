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


def assert_multimodal_content(case: Optional[Dict[str, Any]]) -> None:
    """Hard content gate for a multimodal case (BUG-1 §2). Raises
    :class:`MultimodalContentError` unless the case carries:

      * ≥ ``ASCLEPIUS_CASE_MIN_LAB_PANELS`` (default 1) lab panel(s) with, in
        total, ≥ ``ASCLEPIUS_CASE_MIN_LAB_RESULTS`` (default 2) results that each
        carry analyte + value + unit + (ref range OR flag);
      * ≥ 1 note with ≥ ``ASCLEPIUS_CASE_MIN_NOTE_CHARS`` (default 200) chars;
      * ≥ 1 problem AND ≥ 1 medication.

    A multimodal case that has no labs is not a multimodal case. Called in
    ``critic.generate_case`` before returning and again in ``generation.py``
    before ``insert_task`` — defense in depth so an empty case can never be
    stamped ``modality='multimodal'``."""
    c = as_dict(case) or {}
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
    return bool(case and (case.get("lab_panels") or case.get("notes")))
