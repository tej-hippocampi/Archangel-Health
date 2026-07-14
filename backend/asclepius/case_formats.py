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

The concrete parsers live in ``asclepius/adapters/`` (EHR Ingestion PRD §6):
``lab_csv``, ``fhir_r4``, ``hl7v2``, and ``note_text`` are REAL, dependency-free
implementations registered below; ``dicom`` is registered only to reject. The
single-file path is ``ingest_real_deid`` (adapter → timeline normalization →
guard); multi-file bundle assembly is orchestrated by ``ingestion.py``.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from pydantic import ValidationError

from asclepius.cases import ClinicalCase, as_dict
from asclepius.validation import residual_identifiers

# Source formats a real de-identified export can arrive in. ``dicom`` is present
# only to reject (no imaging). Keep in sync with the adapter registry below.
CASE_FORMATS = ("lab_csv", "fhir_r4", "hl7v2", "note_text", "dicom")


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


# ─── Format adapters ──────────────────────────────────────────────────────────
def _reject_imaging(raw: Any, *, specialty: str = "general", manifest: Any = None) -> Dict[str, Any]:
    raise ImagingRejected(
        "DICOM/imaging is never a gradable modality (PRD §2): cases are text + "
        "structured tabular data only. Imaging exports are rejected at ingest."
    )


def _adapter(fmt: str) -> Callable[..., Dict[str, Any]]:
    """Lazily resolve the real parser (EHR PRD §6). Lazy so ``asclepius.adapters``
    (which imports ``age_to_band`` from this module) never forms an import cycle,
    and a broken adapter module degrades to a per-format ingest error instead of
    breaking every import of case_formats."""
    def _dispatch(raw: Any, *, specialty: str = "general", manifest: Any = None) -> Dict[str, Any]:
        from asclepius import adapters as _adapters
        mod = getattr(_adapters, fmt, None)
        if mod is None or not hasattr(mod, "parse"):
            raise CaseFormatNotImplemented(f"no parser registered for {fmt!r}")
        return mod.parse(raw, specialty=specialty, manifest=manifest)
    return _dispatch


# name → adapter(raw, *, specialty, manifest) -> ClinicalCase FRAGMENTS
# (pre-normalize, pre-deidentify). ``dicom`` is registered only to reject.
FORMATS: Dict[str, Callable[..., Dict[str, Any]]] = {
    "lab_csv": _adapter("lab_csv"),
    "fhir_r4": _adapter("fhir_r4"),
    "hl7v2": _adapter("hl7v2"),
    "note_text": _adapter("note_text"),
    "dicom": _reject_imaging,
}


def _strip_meta(fragments: Dict[str, Any]) -> Dict[str, Any]:
    """Drop adapter-internal keys (``_patient_keys``, ``_imaging_skipped``…) so
    they never reach the ClinicalCase model or a stored case."""
    return {k: v for k, v in (fragments or {}).items() if not str(k).startswith("_")}


def ingest_real_deid(
    raw: Any, fmt: str, *, specialty: str = "general",
    manifest: Optional[Dict[str, Any]] = None, index_event: Optional[str] = None,
) -> Dict[str, Any]:
    """Parse ONE real de-identified export into a stored-ready ClinicalCase dict:

        adapter → ``timeline.normalize_timeline`` (shifted dates → relative
        offsets; the B1 bridge — ordering is load-bearing, PRD §7) →
        ``deidentify()`` hard guard → ClinicalCase, stamped ``real_deid``.

    Raises ``CaseIngestError`` for an unknown format, an imaging export, an
    unnormalizable timeline, UNRESOLVED date-like tokens (quarantine — we never
    guess), or a case that fails the de-identification guard. Multi-file bundle
    assembly lives in ``ingestion.py``; this is the single-file path."""
    from asclepius.timeline import TimelineError, normalize_timeline

    adapter = FORMATS.get(fmt)
    if adapter is None:
        raise CaseIngestError(
            f"unknown case format {fmt!r}; supported: {', '.join(CASE_FORMATS)}."
        )
    try:
        parsed = adapter(raw, specialty=specialty, manifest=manifest)
    except CaseIngestError:
        raise  # ImagingRejected / CaseFormatNotImplemented pass through untouched
    except Exception as exc:
        # Adapter-native parse errors (LabCsvError, FhirParseError, …) surface as
        # one clean, quarantinable ingest error — never a raw 500.
        raise CaseIngestError(f"{fmt} parse failed: {exc}") from exc
    try:
        # Anchor precedence: explicit arg > partner manifest > the adapter's own
        # suggestion (its latest encounter/observation datetime) > latest lab.
        normalized, report = normalize_timeline(
            _strip_meta(parsed),
            index_event=(index_event
                         or (manifest or {}).get("index_event")
                         or parsed.get("_index_event")),
        )
    except TimelineError as exc:
        raise CaseIngestError(f"timeline normalization failed: {exc}") from exc
    if report.get("unresolved"):
        raise CaseIngestError(
            "unresolved date-like tokens after timeline normalization ("
            + ", ".join(report["unresolved"][:5])
            + "); refusing to guess — review in quarantine."
        )
    safe = deidentify(normalized)
    try:
        case = ClinicalCase(**{**safe, "case_source": "real_deid",
                               "specialty": safe.get("specialty") or specialty})
    except ValidationError as exc:
        # extra="forbid" (BUG-1): a real export whose structure drifts from the
        # ClinicalCase schema surfaces as ONE clean, quarantinable ingest error —
        # never an uncaught 500 and never a silent extra="ignore" data drop. This
        # matches the guard on the bundle path in ingestion.py.
        raise CaseIngestError(f"case failed the ClinicalCase schema: {exc}") from exc
    return case.model_dump()
