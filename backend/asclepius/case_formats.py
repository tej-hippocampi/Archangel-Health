"""Real de-identified case ingestion — the format-adapter seam
(Synthetic Multimodal Cases PRD §2, §5, §10).

Synthetic cases are authored by the generation engine (``critic.generate_case``).
The *other* provenance is ``real_deid``: a case parsed from a real, already
de-identified clinical export. This module is the drop-in seam for that adapter —
one registry keyed by source format, plus a de-identification guard every
inbound case must pass before it can be stamped ``case_source="real_deid"``.

Why a separate module: ``cases.py`` owns the PHI-free value model + serialization
and must stay import-light (routers, packaging, and value all import it). The
ingest adapters pull in format-specific parsing (CSV / FHIR / HL7v2) and belong
behind their own seam so nothing downstream depends on them.

Design invariants carried from the model (PRD §2):
  * **No imaging.** ``dicom`` is registered only to REJECT — images are never a
    gradable modality, so an imaging export can never become a case.
  * **Relative offsets, age bands, no PHI.** ``deidentify`` collapses exact ages
    into bands (90+ merged), scans every free-text field with the shared
    Safe-Harbor scanner, and refuses a case that still carries residual
    identifiers. Calendar→relative-offset conversion is the adapter's job (the
    absolute dates never enter the model), asserted here as a post-condition.

The concrete parsers are intentionally unimplemented (``CaseFormatNotImplemented``)
so the seam ships as a wired, tested contract without pretending to parse formats
we have not yet validated against real exports. When an adapter lands, it slots
into ``FORMATS`` and every downstream path (generation source, value model,
export filters, datasheet) already handles ``real_deid`` with zero change.
"""

from __future__ import annotations

import importlib
from typing import Any, Callable, Dict, List, Optional

from asclepius.cases import ClinicalCase, as_dict
from asclepius.validation import residual_identifiers

# Source formats a real de-identified export can arrive in. ``dicom`` is present
# only to reject (no imaging). Keep in sync with the adapter registry below.
CASE_FORMATS = ("lab_csv", "fhir_r4", "hl7v2", "ccda", "note_text", "dicom")


class CaseIngestError(ValueError):
    """A real_deid case could not be ingested (unknown format, parse failure, or
    it failed the de-identification guard). Never a partial/silent ingest."""


class CaseFormatNotImplemented(CaseIngestError):
    """The format is recognized but its adapter has not landed yet — a wired seam,
    not a silent no-op."""


class ImagingRejected(CaseIngestError):
    """An imaging export was submitted. Images are never a gradable modality
    (PRD §2) — the case is rejected outright."""


# ─── De-identification guard ──────────────────────────────────────────────────
def age_to_band(age: Optional[int]) -> Optional[str]:
    """Collapse an exact age into a Safe-Harbor age band. 90+ merges into a single
    bucket (HIPAA §164.514(b)(2)(i)(C)); None → None."""
    if age is None:
        return None
    try:
        a = int(age)
    except (TypeError, ValueError):
        return None
    if a < 0:
        return None
    if a >= 90:
        return "90+"
    lo = (a // 10) * 10
    return f"{lo}-{lo + 9}"


def _case_text_fields(case: Any) -> List[str]:
    """EVERY string a residual identifier could hide in, collected by walking the
    whole case recursively. Drift-proof by construction: any field the case model
    grows (note ``author_role``/``note_type``, a lab ``panel``/``analyte``/``unit``,
    a med field, a vitals value, a dict key) is scanned automatically, so the guard
    can never fall behind what ``render_case_prompt``/the shipped record emit.

    A field-by-field allowlist here was the bug: it scanned note ``text`` but not
    ``author_role``/``note_type`` or the lab labels, so a real-EHR provider name or
    phone in one of those fields would pass the guard and ship into ``task.prompt``.
    """
    out: List[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, str):
            out.append(node)
        elif isinstance(node, dict):
            for k, v in node.items():
                if isinstance(k, str):
                    out.append(k)
                _walk(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                _walk(v)
        elif node is not None:
            out.append(str(node))

    _walk(case)
    return out


def deidentify(case: Any) -> Dict[str, Any]:
    """Return a PHI-free case dict, or raise ``CaseIngestError`` if it cannot be
    made safe. This is the gate every ``real_deid`` case must clear before it is
    stamped and stored — the same Safe-Harbor bar synthetic generation is held to.

    Enforced now:
      * Demographics carry an age BAND only; an ``age`` key is collapsed to a band
        and dropped, and an out-of-policy exact age never survives.
      * Every free-text field is scanned with the shared residual-identifier
        scanner; any hit (name/MRN/SSN/phone/email/**calendar date**) rejects the
        case — the caller must map dates to ``collected_offset_days`` first.
      * ``collected_offset_days`` must be an int (relative), never a date string.
    """
    c = as_dict(case)
    if not c:
        raise CaseIngestError("empty case")
    c = dict(c)

    demo = dict(c.get("demographics") or {})
    if demo.get("age") is not None and not demo.get("age_band"):
        demo["age_band"] = age_to_band(demo.get("age"))
    demo.pop("age", None)  # exact age never survives
    c["demographics"] = demo

    # Relative offsets only — a stray date string here means the adapter skipped
    # the calendar→offset mapping.
    for lp in c.get("lab_panels") or []:
        off = lp.get("collected_offset_days", 0)
        if not isinstance(off, int):
            raise CaseIngestError(
                f"lab panel {lp.get('panel')!r} has a non-relative collected_offset_days "
                f"({off!r}); map calendar dates to integer day offsets before ingest."
            )

    # Residual-identifier scan across every free-text field (Safe Harbor).
    kinds: List[str] = []
    for text in _case_text_fields(c):
        kinds.extend(residual_identifiers(text))
    if kinds:
        raise CaseIngestError(
            "residual identifiers detected (" + ", ".join(sorted(set(kinds))) + "); "
            "case is not de-identified and cannot be ingested as real_deid."
        )
    return c


# ─── Format adapters (seam) ───────────────────────────────────────────────────
def _not_implemented(fmt: str) -> Callable[..., Dict[str, Any]]:
    def _adapter(raw: Any, *, specialty: str = "general") -> Dict[str, Any]:
        raise CaseFormatNotImplemented(
            f"the {fmt!r} case adapter is not implemented yet. The seam is wired: "
            f"add a parser that maps {fmt!r} → a ClinicalCase dict and register it in "
            f"FORMATS, and real_deid ingestion works end-to-end with no other change."
        )
    return _adapter


def _reject_imaging(raw: Any, *, specialty: str = "general") -> Dict[str, Any]:
    raise ImagingRejected(
        "DICOM/imaging is never a gradable modality (PRD §2): cases are text + "
        "structured tabular data only. Imaging exports are rejected at ingest."
    )


def _load_adapter(fmt: str, module_name: str) -> Callable[..., Dict[str, Any]]:
    """Bind a format to its adapter's ``parse`` function, degrading to the wired
    ``_not_implemented`` seam if the adapter module is absent or fails to import.
    An adapter is thus a drop-in: the moment ``asclepius/adapters/<module>.py``
    exposing ``parse(raw, *, specialty)`` lands, that format goes live — with no
    change here and no risk of a broken/partial adapter file taking down the whole
    ``case_formats`` import."""
    try:
        mod = importlib.import_module(f"asclepius.adapters.{module_name}")
        fn = getattr(mod, "parse", None)
        return fn if callable(fn) else _not_implemented(fmt)
    except Exception:
        return _not_implemented(fmt)


# name → adapter(raw, *, specialty) -> ClinicalCase fragment dict (pre-timeline,
# pre-deidentify). ``dicom`` is registered only to reject (no imaging).
FORMATS: Dict[str, Callable[..., Dict[str, Any]]] = {
    "lab_csv": _load_adapter("lab_csv", "lab_csv"),
    "fhir_r4": _load_adapter("fhir_r4", "fhir_r4"),
    "hl7v2": _load_adapter("hl7v2", "hl7v2"),
    "ccda": _load_adapter("ccda", "ccda"),
    "note_text": _load_adapter("note_text", "note_text"),
    "dicom": _reject_imaging,
}


def ingest_real_deid(
    raw: Any, fmt: str, *, specialty: str = "general", index_event: Any = None
) -> Dict[str, Any]:
    """Parse a real de-identified export into a stored-ready ClinicalCase dict:
    dispatch to the format adapter, **normalize the timeline** (calendar dates →
    relative integer day offsets — Data Provider Portal PRD §2 B1), run the
    de-identification guard, coerce through the ClinicalCase model, and stamp
    ``case_source="real_deid"``.

    The timeline step is what makes a real, date-shifted export pass ``deidentify``
    at all: the adapter emits raw ``collected_at`` dates + dates inside note text,
    and this converts every one to a relative offset/token before the date-
    rejecting guard runs. For a multi-file bundle assembled per patient, use
    ``asclepius.ingestion`` instead (it also verifies de-id + routes to quarantine).

    Raises ``CaseIngestError`` for an unknown format, a not-yet-implemented
    adapter, an imaging export, or a case that fails de-identification."""
    adapter = FORMATS.get(fmt)
    if adapter is None:
        raise CaseIngestError(
            f"unknown case format {fmt!r}; supported: {', '.join(CASE_FORMATS)}."
        )
    parsed = adapter(raw, specialty=specialty)
    # Calendar → relative offsets BEFORE the de-id guard (the B1 fix).
    from asclepius.timeline import normalize_case_timeline

    normalized, _ = normalize_case_timeline(parsed, index_event=index_event)
    safe = deidentify(normalized)
    case = ClinicalCase(**{**safe, "case_source": "real_deid", "specialty": safe.get("specialty") or specialty})
    return case.model_dump()
