"""Subspecialty channels: the room inside the room.

Two nephrologists share #nephrology and can still have almost nothing to say to
each other. One runs a home-dialysis programme, the other reads transplant
biopsies; the guidelines they argue about, the literature worth their evening
and the AI tools being sold to them are different. #dialysis and
#transplant-nephrology are where those two conversations actually happen.

Config only, exactly like ``countries.py``: this module never touches a
database, so the community plane can import it without reaching into the
asclepius plane. The caller that knows which subspecialties have members
(main.py at startup, and the morning run) passes them in.

**The alias map is the point of this file.** Subspecialties are stored as
free text in ``credentials_json`` (``ship.subspecialties``), so the same
practice arrives written five ways. Slugifying that text directly yields
``#ckd``, ``#chronic-kidney-disease`` and ``#c-k-d`` as three rooms of one
person each, which is the failure the country list already learned to avoid. A
curated alias map collapses the variants onto one slug, and a subspecialty
nobody has mapped yet simply has no room until someone adds it here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class Subspecialty:
    slug: str          # channel slug
    name: str          # display name, used in the room description
    aliases: Tuple[str, ...]   # every spelling seen in the wild


#: Curated, one entry per subspecialty, aliases written in whatever form a
#: physician actually types. Ordered by parent specialty for reading only; the
#: lookup is flat.
SUBSPECIALTIES: Tuple[Subspecialty, ...] = (
    # ── Nephrology ──────────────────────────────────────────────────────────
    Subspecialty("dialysis", "dialysis", (
        "dialysis", "hemodialysis", "haemodialysis", "peritoneal dialysis",
        "home dialysis", "renal replacement therapy", "esrd", "eskd",
        "end stage renal disease", "end stage kidney disease",
    )),
    Subspecialty("transplant-nephrology", "transplant nephrology", (
        "transplant", "transplantation", "transplant nephrology",
        "kidney transplant", "kidney transplantation", "renal transplant",
        "renal transplantation",
    )),
    Subspecialty("ckd", "chronic kidney disease", (
        "ckd", "chronic kidney disease", "chronic renal failure",
        "chronic kidney disease ckd", "chronic renal insufficiency",
    )),
    Subspecialty("glomerular-disease", "glomerular disease", (
        "glomerular disease", "glomerulonephritis", "glomerulopathy",
        "gn", "glomerular diseases",
    )),
    Subspecialty("onconephrology", "onconephrology", (
        "onconephrology", "onco nephrology", "cancer nephrology",
    )),
    Subspecialty("critical-care-nephrology", "critical care nephrology", (
        "critical care nephrology", "acute kidney injury", "aki",
        "crrt", "continuous renal replacement therapy",
    )),
    Subspecialty("pediatric-nephrology", "pediatric nephrology", (
        "pediatric nephrology", "paediatric nephrology", "peds nephrology",
    )),
    Subspecialty("hypertension", "hypertension", (
        "hypertension", "resistant hypertension", "htn",
    )),
    # ── Cardiology ──────────────────────────────────────────────────────────
    Subspecialty("interventional-cardiology", "interventional cardiology", (
        "interventional cardiology", "interventional", "cardiac catheterization",
        "structural heart", "structural heart disease",
    )),
    Subspecialty("electrophysiology", "cardiac electrophysiology", (
        "electrophysiology", "cardiac electrophysiology", "ep",
        "arrhythmia", "arrhythmias",
    )),
    Subspecialty("heart-failure", "heart failure", (
        "heart failure", "advanced heart failure", "hf",
        "heart failure and transplant", "cardiac transplant",
    )),
    Subspecialty("cardiac-imaging", "cardiac imaging", (
        "cardiac imaging", "cardiovascular imaging", "echocardiography",
        "echo", "cardiac mri", "nuclear cardiology",
    )),
    Subspecialty("preventive-cardiology", "preventive cardiology", (
        "preventive cardiology", "preventative cardiology", "lipidology",
        "cardiac rehabilitation",
    )),
    # ── Oncology ────────────────────────────────────────────────────────────
    Subspecialty("medical-oncology", "medical oncology", (
        "medical oncology", "med onc", "solid tumor oncology",
    )),
    Subspecialty("radiation-oncology", "radiation oncology", (
        "radiation oncology", "rad onc", "radiotherapy",
    )),
    Subspecialty("surgical-oncology", "surgical oncology", (
        "surgical oncology", "surg onc",
    )),
    Subspecialty("hematology-oncology", "hematology oncology", (
        "hematology oncology", "haematology oncology", "heme onc",
        "hematology", "haematology", "malignant hematology",
    )),
    Subspecialty("neuro-oncology", "neuro oncology", (
        "neuro oncology", "neurooncology",
    )),
    Subspecialty("gynecologic-oncology", "gynecologic oncology", (
        "gynecologic oncology", "gynaecologic oncology", "gyn onc",
    )),
    # ── Hepatology ──────────────────────────────────────────────────────────
    Subspecialty("transplant-hepatology", "transplant hepatology", (
        "transplant hepatology", "liver transplant", "liver transplantation",
    )),
    Subspecialty("viral-hepatitis", "viral hepatitis", (
        "viral hepatitis", "hepatitis b", "hepatitis c", "hbv", "hcv",
    )),
    Subspecialty("fatty-liver-disease", "fatty liver disease", (
        "fatty liver disease", "nafld", "nash", "masld", "mash",
        "metabolic dysfunction associated steatotic liver disease",
    )),
    Subspecialty("cirrhosis-and-portal-hypertension", "cirrhosis and portal hypertension", (
        "cirrhosis", "portal hypertension", "decompensated cirrhosis",
        "hepatorenal syndrome",
    )),
    # ── Cross-cutting ───────────────────────────────────────────────────────
    Subspecialty("critical-care", "critical care", (
        "critical care", "intensive care", "icu", "intensivist",
        "critical care medicine",
    )),
    Subspecialty("palliative-care", "palliative care", (
        "palliative care", "palliative medicine", "hospice",
        "hospice and palliative medicine",
    )),
    Subspecialty("clinical-informatics", "clinical informatics", (
        "clinical informatics", "biomedical informatics", "health informatics",
        "medical informatics",
    )),
)

_PUNCT = re.compile(r"[^a-z0-9]+")


def normalize(raw: Optional[str]) -> str:
    """The comparison form for a free-text subspecialty.

    Dots go first and separately, so ``C.K.D.`` lands on ``ckd`` rather than on
    ``c k d``: an abbreviation written with periods is the single most common
    variant this map has to absorb.
    """
    text = (raw or "").strip().lower()
    if not text:
        return ""
    text = text.replace(".", "").replace("'", "").replace("’", "")
    text = _PUNCT.sub(" ", text)
    return " ".join(text.split())


def _build_alias_index() -> Dict[str, Subspecialty]:
    index: Dict[str, Subspecialty] = {}
    for sub in SUBSPECIALTIES:
        for alias in (sub.slug,) + sub.aliases:
            key = normalize(alias)
            if key:
                index.setdefault(key, sub)
    return index


#: alias (normalized) -> subspecialty. Built once; a duplicate alias resolves to
#: whichever entry declared it first, and a test holds the map collision-free so
#: that never silently decides which room a physician lands in.
ALIAS_INDEX: Dict[str, Subspecialty] = _build_alias_index()


def get(raw: Optional[str]) -> Optional[Subspecialty]:
    """The subspecialty a free-text string names, or None when unmapped."""
    return ALIAS_INDEX.get(normalize(raw))


def slugs_for(values: Optional[Iterable[Any]]) -> List[str]:
    """Every mapped subspecialty slug in a member's free-text list, deduped.

    Order-preserving, because a member's first-listed subspecialty is the one
    they identify with and the rail should read that way.
    """
    out: List[str] = []
    if isinstance(values, str):
        values = [values]
    for raw in values or ():
        sub = get(str(raw) if raw is not None else "")
        if sub and sub.slug not in out:
            out.append(sub.slug)
    return out


def channel_defs(names: Iterable[str]) -> List[Dict[str, object]]:
    """Channel definitions for the subspecialties that actually have members.

    Only those, for the country-channel reason: a rail listing twenty-six
    subspecialties, twenty-four of them empty, is a directory rather than a
    community. An unmapped subspecialty produces nothing at all, which is the
    deliberate cost of curating the map.
    """
    seen: List[Subspecialty] = []
    for raw in names or ():
        sub = get(raw)
        if sub and sub not in seen:
            seen.append(sub)
    return [
        {
            "slug": sub.slug,
            "name": sub.slug,
            "description": (
                f"Colleagues who practise {sub.name}: the literature, the "
                "referral patterns, and the AI being sold into it."
            ),
            "post_policy": "all",
            "grp": "subspecialty",
            "subspecialty": sub.slug,
        }
        for sub in seen
    ]
