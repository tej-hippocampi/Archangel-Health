"""Clinical identity for onboarding, independent of the paid case registry."""
from __future__ import annotations

import json
import re
from typing import Any

# Longest phrases win: radiation oncology is not medical oncology, and a
# neurosurgeon must never be routed as a neurologist. This vocabulary does not
# enable a specialty in the paid queue or grant any clinical capability.
_NAMES = {
    "dermatology": ("dermatologist", "dermatologists", "dermatologic surgery", "derm"),
    "neurology": ("neurologist", "neurologists", "neuro"),
    "nephrology": ("nephrologist", "renal medicine", "renal", "nephro"),
    "cardiology": ("cardiologist", "cardiovascular medicine", "cardiovascular disease", "cardio"),
    "oncology": ("oncologist", "medical oncology", "hematology oncology", "haematology oncology", "heme onc", "hem onc"),
    "radiation oncology": ("radiation oncologist",),
    "surgical oncology": ("surgical oncologist",),
    "pediatric oncology": ("paediatric oncology", "pediatric oncologist"),
    "pediatric cardiology": ("paediatric cardiology", "pediatric cardiologist"),
    "pediatric neurology": ("paediatric neurology", "pediatric neurologist", "child neurology"),
    "hepatology": ("hepatologist", "liver medicine"),
    "gastroenterology": ("gastroenterologist",),
    "endocrinology": ("endocrinologist",),
    "rheumatology": ("rheumatologist",),
    "hematology": ("haematology", "hematologist", "haematologist"),
    "pulmonology": ("pulmonologist", "pulmonary medicine", "respiratory medicine"),
    "pulmonary and critical care": ("pulmonary critical care",),
    "critical care": ("critical care medicine", "intensive care", "intensivist"),
    "infectious diseases": ("infectious disease",),
    "allergy and immunology": ("allergy immunology", "allergist", "immunologist"),
    "internal medicine": ("internist", "general internal medicine"),
    "family medicine": ("family physician", "general practice", "general practitioner"),
    "emergency medicine": ("emergency physician",),
    "pediatrics": ("paediatrics", "pediatrician", "paediatrician"),
    "neonatology": ("neonatologist",),
    "geriatrics": ("geriatric medicine", "geriatrician"),
    "psychiatry": ("psychiatrist",),
    "child and adolescent psychiatry": ("child psychiatry", "pediatric psychiatry"),
    "obstetrics and gynecology": ("obstetrics gynecology", "obstetrics gynaecology", "ob gyn", "obgyn", "gynecologist", "gynaecologist"),
    "maternal fetal medicine": ("maternal foetal medicine",),
    "ophthalmology": ("ophthalmologist",),
    "otolaryngology": ("otolaryngologist", "ear nose and throat", "ent"),
    "urology": ("urologist",),
    "orthopedic surgery": ("orthopaedic surgery", "orthopedics", "orthopaedics", "orthopedic surgeon", "orthopaedic surgeon"),
    "neurosurgery": ("neurosurgeon", "neurological surgery"),
    "cardiothoracic surgery": ("cardiothoracic surgeon", "cardiac surgery", "cardiovascular surgery"),
    "vascular surgery": ("vascular surgeon",),
    "colorectal surgery": ("colorectal surgeon", "colon and rectal surgery", "colon rectal surgery"),
    "transplant surgery": ("transplant surgeon", "transplantation surgery"),
    "nuclear medicine": ("nuclear medicine physician", "nuclear radiology"),
    "general surgery": ("general surgeon",),
    "plastic surgery": ("plastic surgeon",),
    "anesthesiology": ("anaesthesiology", "anesthesiologist", "anaesthetist", "anaesthesia", "anesthesia"),
    "radiology": ("radiologist", "diagnostic radiology"),
    "interventional radiology": ("interventional radiologist",),
    "pathology": ("pathologist", "anatomic pathology", "anatomical pathology"),
    # National specialty titles describe clinical scope, not US equivalence.
    # Autopsy experience alone does not confer anatomic/clinical pathology.
    "forensic medicine": ("legal medicine", "clinical forensic medicine", "forensic and legal medicine",
        "legal and forensic medicine", "forensic medicine legal medicine", "forensic physician", "forensic medical practitioner",
        "rechtsmedizin", "rechtsmediziner", "rechtsmedizinerin", "sudska medicina", "sudske medicine",
        "судска медицина", "судске медицине"),
    "forensic pathology": ("forensic pathologist",),
    "forensic genetics": ("forensic geneticist",),
    "physical medicine and rehabilitation": ("physical medicine rehabilitation", "physiatry", "physiatrist", "pm r"),
    "pain medicine": ("pain management",),
    "palliative medicine": ("palliative care", "hospice and palliative medicine"),
    "sports medicine": (),
    "sleep medicine": (),
    "occupational medicine": (),
    "preventive medicine": (),
    "medical genetics": ("clinical genetics", "geneticist"),
}

# Clinical identity vocabulary shared with the community. This does not enable
# paid case generation or change a physician's permissions.
CLINICAL_SPECIALTIES = tuple(_NAMES)


def normalize(value: Any) -> str:
    text = str(value or "").strip().casefold()
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def matching_fields(value: Any) -> set[str]:
    text = " " + normalize(value) + " "
    spans = [(m.start(), m.end(), name) for name, aliases in _NAMES.items()
             for term in (name, *aliases)
             for m in re.finditer(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text)]
    for modifier, pattern in (("pediatric", r"(?:pediatric|paediatric)\s+$"),
                              ("forensic", r"forensic\s+$")):
        spans = [(start, end, modifier + " " + name
                  if not name.startswith(modifier + " ") and re.search(pattern, text[:start])
                  else name) for start, end, name in spans]
    # A nested parent (oncology in radiation oncology) is less specific. Two
    # separate fields are ambiguous and must never be broken by word length.
    names = {name for start, end, name in spans if not any(
        left <= start and right >= end and (left < start or right > end)
        for left, right, _ in spans)}
    return names


def match(value: Any) -> str | None:
    names = matching_fields(value)
    return next(iter(names)) if len(names) == 1 else None


def is_specialty_title(value: Any) -> bool:
    """Bare specialty labels count as identity evidence; incidental mentions do not."""
    text = normalize(value)
    return any(text == term for name, aliases in _NAMES.items() for term in (name, *aliases))


def canonical(value: Any) -> str:
    text = normalize(value)
    if not text or text in {"general", "other", "unknown", "n a", "none", "unspecified"}:
        return ""
    if len(text) > 100:
        return ""
    # Pediatric subspecialties must not silently become their adult parent.
    pediatric = re.sub(r"\bpaediatric\b", "pediatric", text)
    if pediatric.startswith("pediatric ") and pediatric not in _NAMES:
        return "pediatric " + (match(pediatric[10:]) or pediatric[10:])
    return match(text) or text


def _object(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    try:
        result = json.loads(value or "{}")
        return result if isinstance(result, dict) else {}
    except (TypeError, ValueError):
        return {}


def resolve(user: dict) -> dict:
    """The doctor-confirmed declaration wins over every automated suggestion.

    In particular, recover declarations discarded by old provisioning without
    rewriting either the original credential record or the physician profile.
    A resolved, evidence-attributed CV can recover an unconfirmed legacy profile.
    Older parses lack that provenance and cannot override a saved profile.
    Never send the CV or physician identity to case generation.
    """
    credentials = _object(user.get("credentials_json") or user.get("credentials"))
    cv = _object(user.get("cv_parsed_json") or user.get("cv_parsed") or credentials.get("cvParsed"))
    # Legacy display extraction could contradict the key or even turn an
    # ambiguous (null) decision into Nephrology. Use display only when no key
    # was recorded at all, for compatibility with older imported records.
    cv_value = cv.get("specialty") if "specialty" in cv else cv.get("specialty_display")
    if cv.get("ok") is False or cv.get("specialty_status") in ("ambiguous", "missing"):
        cv_value = None
    attributed_cv = cv_value if cv.get("ok") is True and cv.get("specialty_status") == "resolved" else None
    for source, value in (("confirmed", credentials.get("primarySpecialty")),
                          ("cv", attributed_cv),
                          ("profile", user.get("specialty")),
                          ("cv", cv_value)):
        specialty = canonical(value)
        if specialty:
            return {"specialty": specialty, "applied_with": str(value).strip(), "source": source}
    return {"specialty": "", "applied_with": "", "source": "missing"}
