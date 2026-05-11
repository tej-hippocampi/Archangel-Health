"""Procedure-driven flags (PRD §5.4)."""

from __future__ import annotations

from triage.types import InitialTierInput


def derive_procedure_flags(input: InitialTierInput) -> set[str]:
    flags: set[str] = set()

    if input.procedure.is_emergency:
        flags.add("EMERGENCY_CASE")

    summary = input.studies_summary
    if input.procedure.anchor_procedure_family == "CABG" and summary.get("low_ef_30"):
        flags.add("CABG_WITH_EF_UNDER_30")

    return flags
