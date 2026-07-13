"""Format adapters (Real EHR Ingestion PRD §6) — native partner shapes →
ClinicalCase-shaped FRAGMENTS.

Each adapter exposes ``parse(raw, *, specialty="general", manifest=None) ->
Dict`` returning partial ClinicalCase fragments (``lab_panels`` may carry raw
``collected_at`` date strings — ``timeline.normalize_timeline`` converts them to
relative offsets downstream; the adapter NEVER emits a finished case).

Contract rules every adapter obeys:
  * Direct identifiers (names, MRNs, addresses, phone numbers) are NEVER copied
    out of the source — not even "to be scrubbed later". Fields that can only
    hold an identifier (HL7 PID-5, FHIR Patient.name/identifier/address) are
    dropped at the parser, so they cannot leak past a downstream bug.
  * Demographics reduce to ``age_band`` + ``sex`` only.
  * Imaging is never parsed: imaging resources are counted in the fragment's
    ``_imaging_skipped`` (the bundle-level policy — reject the entry, keep the
    case — is applied by ingestion; a case that is ONLY imaging dies there).
  * Dependency-free by design: FHIR R4 is JSON and HL7 v2 is pipe-delimited —
    parsed directly (per the HL7 v2-to-FHIR ORU_R01 mapping) so ingestion adds
    zero runtime dependencies. ``fhir.resources``/``python-hl7`` can be swapped
    in behind the same ``parse`` contract later without downstream change.
"""

from asclepius.adapters import fhir_r4, hl7v2, lab_csv, note_text  # noqa: F401

__all__ = ["fhir_r4", "hl7v2", "lab_csv", "note_text"]
