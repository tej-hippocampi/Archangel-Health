"""Allergy-derived flags. Single rule today (PRD §4.4)."""

from __future__ import annotations

from triage.types import AllergiesInput


_PERIOP_RELEVANT_KEYWORDS = (
    # Anesthetic agents and adjuncts
    "anesth", "propofol", "succinyl", "rocuron", "vecuron",
    "lidocaine", "bupivacaine",
    # Contrast
    "contrast", "iodinat", "gadolinium",
    # Latex
    "latex",
    # Surgical antibiotic prophylaxis
    "cefazolin", "cephalosporin", "cephalexin",
    "vancomycin", "clindamycin",
    "penicillin", "amoxicillin",
)


def derive_allergy_flags(allergies: AllergiesInput) -> set[str]:
    flags: set[str] = set()
    for a in allergies.allergies:
        if a.reaction_type != "ANAPHYLAXIS":
            continue
        substance = (a.substance or "").lower()
        if any(k in substance for k in _PERIOP_RELEVANT_KEYWORDS):
            flags.add("PERIOP_ANAPHYLAXIS_HISTORY")
            break
    return flags
