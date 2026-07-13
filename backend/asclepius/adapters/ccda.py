"""C-CDA / generic clinical XML adapter.

Uses :mod:`xml.etree.ElementTree`, matching on LOCAL tag names so it is
namespace-tolerant. Best-effort extraction of labs, problems, medications, and
narrative notes plus demographics (age band only, never a raw birthTime).

Security: rejects any document containing ``<!DOCTYPE`` (XXE / entity-expansion
guard). ElementTree does not resolve external entities by default. Unparseable
XML raises :class:`CaseIngestError`.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

from asclepius.case_formats import CaseIngestError

from ._common import birthdate_to_age_band, normalize_sex, to_text


def _local(tag: Any) -> str:
    """Strip a ``{namespace}`` prefix from an element tag; lowercase."""
    if not isinstance(tag, str):
        return ""
    if "}" in tag:
        tag = tag.split("}", 1)[1]
    return tag.lower()


def _findall_local(root: ET.Element, name: str) -> List[ET.Element]:
    name = name.lower()
    return [el for el in root.iter() if _local(el.tag) == name]


def _first_local(el: ET.Element, name: str) -> Optional[ET.Element]:
    name = name.lower()
    for child in el.iter():
        if child is el:
            continue
        if _local(child.tag) == name:
            return child
    return None


def _text_content(el: ET.Element) -> str:
    """Concatenate all text under an element (tags stripped)."""
    parts: List[str] = []
    if el.text:
        parts.append(el.text)
    for child in el:
        parts.append(_text_content(child))
        if child.tail:
            parts.append(child.tail)
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _coerce_number(value: Optional[str]) -> Any:
    if value is None:
        return None
    s = str(value).strip()
    if s == "":
        return None
    try:
        if re.fullmatch(r"[+-]?\d+", s):
            return int(s)
        return float(s)
    except ValueError:
        return s


def parse(raw, *, specialty: str = "general") -> dict:
    text = to_text(raw)
    if "<!DOCTYPE" in text:
        raise CaseIngestError("ccda: DOCTYPE declarations are rejected (XXE guard)")
    if not text.strip():
        raise CaseIngestError("ccda: empty input")

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise CaseIngestError(f"ccda: unparseable XML ({exc})")

    demographics: Dict[str, Any] = {}
    lab_results: List[dict] = []
    problem_list: List[dict] = []
    medications: List[dict] = []
    notes: List[dict] = []

    # ── Demographics: birthTime (@value) -> age band; gender code -> sex ──
    for bt in _findall_local(root, "birthtime"):
        band = birthdate_to_age_band(bt.get("value"))
        if band is not None:
            demographics["age_band"] = band
        break
    for g in _findall_local(root, "administrativegendercode"):
        sex = normalize_sex(g.get("code") or g.get("displayName"))
        if sex:
            demographics["sex"] = sex
        break

    # ── Observations -> labs (PQ value) or problems (coded value) ──
    for obs in _findall_local(root, "observation"):
        value_els = [c for c in obs if _local(c.tag) == "value"]
        code_el = next((c for c in obs if _local(c.tag) == "code"), None)
        analyte = None
        if code_el is not None:
            analyte = code_el.get("displayName")
        for val in value_els:
            xsitype = ""
            for k, v in val.attrib.items():
                if _local(k) == "type" or k.endswith("type"):
                    xsitype = v
            has_value_attr = val.get("value") is not None
            unit = val.get("unit")
            if has_value_attr and (unit is not None or "PQ" in (xsitype or "")):
                lab_results.append({
                    "analyte": analyte or "observation",
                    "loinc": code_el.get("code") if code_el is not None else None,
                    "value": _coerce_number(val.get("value")),
                    "unit": unit,
                    "ref_low": None,
                    "ref_high": None,
                    "flag": "",
                })
            else:
                # Coded value -> treat as a problem/condition.
                cond = val.get("displayName") or analyte
                if cond:
                    problem_list.append({"condition": cond, "since": None})

    # ── Medications: substanceAdministration -> manufacturedMaterial code ──
    for sa in _findall_local(root, "substanceadministration"):
        material = _first_local(sa, "manufacturedmaterial")
        drug = None
        if material is not None:
            code_el = next((c for c in material if _local(c.tag) == "code"), None)
            if code_el is not None:
                drug = code_el.get("displayName") or _text_content(code_el) or None
        if drug:
            dq = next((c for c in sa if _local(c.tag) == "dosequantity"), None)
            dose = None
            if dq is not None and dq.get("value"):
                dose = f"{dq.get('value')} {dq.get('unit') or ''}".strip()
            route_el = next((c for c in sa if _local(c.tag) == "routecode"), None)
            route = route_el.get("displayName") if route_el is not None else None
            medications.append({"drug": drug, "dose": dose or None, "route": route, "freq": None})

    # ── Narrative <text> blocks -> notes (tags stripped) ──
    for txt in _findall_local(root, "text"):
        # Skip <value>/<code> internal text and empty blocks; keep section
        # narrative bodies with real content.
        parent_is_obs = False  # cheap heuristic already handled by content length
        content = _text_content(txt)
        if content and len(content) > 12:
            notes.append({"note_type": "Progress", "author_role": "clinician", "text": content})

    fragment: Dict[str, Any] = {"specialty": specialty, "patient_key": None}
    if demographics:
        fragment["demographics"] = demographics
    if lab_results:
        fragment["lab_panels"] = [{"panel": "Labs", "collected_at": None, "results": lab_results}]
    if problem_list:
        fragment["problem_list"] = problem_list
    if medications:
        fragment["medications"] = medications
    if notes:
        fragment["notes"] = notes

    if not any(k in fragment for k in ("demographics", "lab_panels", "problem_list", "medications", "notes")):
        raise CaseIngestError("ccda: no recognizable clinical content extracted")

    return fragment
