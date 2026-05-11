"""Reclassify obvious OTC/supplement lines from currentMedications into herbalSupplementsOTC."""

from __future__ import annotations

from typing import Any, Dict, List

# Lowercase substrings: if a med line matches, treat as supplement/herbal/OTC not Rx list.
_SUPPLEMENT_HINTS = (
    "fish oil",
    "omega-3",
    "omega 3",
    "melatonin",
    "vitamin",
    "turmeric",
    "probiotic",
    "magnesium",
    "zinc",
    "elderberry",
    "coq10",
    "coenzyme",
    "glucosamine",
    "chondroitin",
    "echinacea",
    "ashwagandha",
    "ginkgo",
    "biotin",
    "fiber gummy",
    "fiber supplement",
    "calcium ",
    "calcium,",
    "d3",
    "d-3",
    "b12",
    "b-12",
    "multivitamin",
    "gummy",
    "st. john",
    "st john",
    "garlic",
    "green tea",
    "creatine",
    "collagen",
    "l-theanine",
    "theanine",
    "folate",
    "supplement",
    "otc",
    "over the counter",
    "herbal",
)


def _line_text(item: Any) -> str:
    if item is None:
        return ""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for k in ("name", "value", "drug", "medication", "text"):
            v = item.get(k)
            if v is not None:
                return str(v)
        return str(item)
    return str(item)


def _is_supplement_line(item: Any) -> bool:
    t = _line_text(item).lower()
    if not t:
        return False
    for h in _SUPPLEMENT_HINTS:
        if h in t:
            return True
    return False


def _ensure_list_meds_key(blob: Any) -> List[Any]:
    """Get list from field update value (list or {value: list})."""
    if blob is None:
        return []
    if isinstance(blob, list):
        return list(blob)
    if isinstance(blob, dict):
        v = blob.get("value")
        if isinstance(v, list):
            return list(v)
    return []


def _set_list_meds_key(container: Dict[str, Any], key: str, items: List[Any]) -> None:
    if not items and key not in container:
        return
    cur = container.get(key)
    if isinstance(cur, dict):
        cur = dict(cur)
        cur["value"] = items
        cur["source"] = "interview"
        container[key] = cur
    else:
        container[key] = {"value": items, "source": "interview"}


def normalize_section5_field_updates(updates: Dict[str, Any]) -> None:
    """
    In-place: move items from currentMedications to herbalSupplementsOTC when they look
    like supplements/vitamins/OTC herbals, not typical prescription drugs.
    """
    if "currentMedications" not in updates and "herbalSupplementsOTC" not in updates:
        return
    med_blob = updates.get("currentMedications")
    otc_blob = updates.get("herbalSupplementsOTC")
    current = _ensure_list_meds_key(med_blob)
    otc = _ensure_list_meds_key(otc_blob)
    if not current:
        return
    stay: List[Any] = []
    for item in current:
        if _is_supplement_line(item):
            otc.append(item)
        else:
            stay.append(item)
    if len(stay) < len(current):
        _set_list_meds_key(updates, "currentMedications", stay)
        _set_list_meds_key(updates, "herbalSupplementsOTC", otc)
    elif otc and otc != _ensure_list_meds_key(otc_blob):
        _set_list_meds_key(updates, "herbalSupplementsOTC", otc)
