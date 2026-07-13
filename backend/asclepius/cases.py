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

from pydantic import BaseModel, Field

# Out-of-range flags on a lab result (HL7-style). "" = within range.
LAB_FLAGS = ("", "L", "H", "LL", "HH")

# Case provenance — synthetic now; real de-identified EHR later (same model).
CASE_SOURCES = ("synthetic", "real_deid")

# Task modality. "text" is the classic one-line prompt; "multimodal" carries a case.
MODALITIES = ("text", "multimodal")


class LabResult(BaseModel):
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

    panel: str
    collected_offset_days: int = 0
    results: List[LabResult] = Field(default_factory=list)


class ClinicalNote(BaseModel):
    """A de-identified narrative (FHIR DocumentReference). ``author_role`` is a
    generalized category ("nephrology", "ICU") — never a person's name."""

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
    """A task is multimodal when it carries a structured case (or an explicit
    modality flag). Central so router/packaging/frontend agree on one rule."""
    if not task:
        return False
    if (task.get("modality") or "text") == "multimodal":
        return True
    return bool(task.get("case"))


# ─── The V4 wall (Data Provider Portal PRD §8) ────────────────────────────────
# A real, de-identified patient case is a V4 task and ONLY a V4 task. The rule is
# a biconditional on CASE PROVENANCE (never the client-declared version label):
#     case_source == "real_deid"  ⇔  portal_version == "v4"
# Enforced in three places server-side — routing (store queries filter by
# provenance), derivation (:func:`derive_portal_version` below, at submit), and
# packaging (``packaging.v4_wall_violation``). Never enforced in the UI.
REAL_CASE_SOURCE = "real_deid"
REAL_CASE_PORTAL_VERSION = "v4"


class V4WallViolation(ValueError):
    """A submission's declared portal_version contradicts its case provenance —
    e.g. a client claiming ``v3`` on a real patient case, or ``v4`` on a synthetic
    one. The router converts this to a 400; packaging converts it to needs_qa."""


def task_case_source(task: Optional[Dict[str, Any]]) -> Optional[str]:
    """Provenance of a task's case: ``'real_deid'`` | ``'synthetic'`` for a
    multimodal task, or ``None`` for a text task (no case). Reads the case's own
    stamp — the single source of truth, set at ingest/generation."""
    if not task:
        return None
    case = task.get("case")
    if not case or not isinstance(case, dict):
        return None
    return case.get("case_source") or "synthetic"


def is_real_case_task(task: Optional[Dict[str, Any]]) -> bool:
    """True iff the task carries a real de-identified patient case (a V4 task)."""
    return task_case_source(task) == REAL_CASE_SOURCE


def derive_portal_version(task: Optional[Dict[str, Any]], declared: Optional[str]) -> str:
    """Resolve the authoritative portal_version for a submission, enforcing the V4
    wall server-side (PRD §8.2 derivation). The CASE decides, not the client:

      * A real case (``case_source == 'real_deid'``) is always ``v4``. A client
        that explicitly declared v1/v2/v3 on a real case is contradicting the
        provenance → :class:`V4WallViolation`.
      * A synthetic/text task can never be ``v4`` — ``v4`` is reserved for real
        cases. A client declaring ``v4`` on a synthetic task → :class:`V4WallViolation`.

    ``declared`` is the client's raw (un-normalized) value so an *explicit*
    contradiction is distinguishable from an absent/defaulted one."""
    d = (declared or "").strip() or None
    if is_real_case_task(task):
        if d is not None and d != REAL_CASE_PORTAL_VERSION:
            raise V4WallViolation(
                f"declared portal_version={d!r} on a real de-identified case; a "
                f"real case is a {REAL_CASE_PORTAL_VERSION!r} task and only a "
                f"{REAL_CASE_PORTAL_VERSION!r} task."
            )
        return REAL_CASE_PORTAL_VERSION
    # Not a real case: v4 is not allowed.
    if d == REAL_CASE_PORTAL_VERSION:
        raise V4WallViolation(
            f"declared portal_version={REAL_CASE_PORTAL_VERSION!r} on a "
            f"non-real case; {REAL_CASE_PORTAL_VERSION!r} is reserved for "
            f"case_source={REAL_CASE_SOURCE!r}."
        )
    return d or ""
