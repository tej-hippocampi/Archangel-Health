"""``fhir_r4`` adapter (EHR PRD §6) — a FHIR R4 ``Bundle`` (JSON) →
ClinicalCase fragments. Dependency-free: a Bundle is JSON; we traverse the
resource shapes named in the PRD mapping table directly.

| FHIR resource                              | → fragment                       |
|--------------------------------------------|----------------------------------|
| Patient                                    | demographics (age band + sex)    |
| Observation (category=laboratory)          | lab_panels[].results[]           |
| Observation (category=vital-signs)         | vitals                           |
| Condition                                  | problem_list[]                   |
| MedicationStatement / MedicationRequest    | medications[]                    |
| DocumentReference / DiagnosticReport text  | notes[]                          |
| ImagingStudy / Media / Binary(image/*)     | counted in _imaging_skipped      |

Identifier discipline: Patient.name / identifier / address / telecom are never
read. Note authors reduce to a generalized role. Dates stay RAW here
(``collected_at``) — ``timeline.normalize_timeline`` destroys the calendar.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any, Dict, List, Optional, Tuple

# Separators a partner plausibly writes a required-modalities declaration with. The
# real PGNMID bundle uses ' + '; the original ,/; split turned the whole declaration
# into one unmatchable token and quarantined a valid case (Audit PRD §P1). ' and '
# only splits before a lowercase word so "H and E" / "kappa and lambda" survive.
_MODALITY_DELIMS = re.compile(r"[,;+\n]| and (?=[a-z])", re.I)

from asclepius.case_formats import age_to_band
from asclepius.cases import STUDY_ASSET_MIMES
from asclepius.timeline import parse_datetime

# DICOM / ImagingStudy references still go to the §3 DICOM path (or are counted as
# skipped imaging). A FHIR ``Media`` carrying an inline base64 raster in a supported
# MIME is NO LONGER skipped imaging — it becomes a Study with an image asset
# (Buyer Response PRD §2 A2). Anything else still counts as ``_imaging_skipped`` so
# the honest count survives.
_IMAGING_TYPES = ("ImagingStudy", "ImagingSelection")
_RASTER_MEDIA_TYPES = ("Media",)

# A partner-declared "this is synthetic" extension is METADATA, never a control
# signal (Buyer Response PRD §2 A5). It is set by the sender, on the sender's honor,
# in a field we do not validate — so honoring it would let any bundle disable our
# PHI guard by asserting a boolean. The de-id pipeline runs identically on every
# bundle. Recorded for provenance; never branched on.
_NEVER_TRUSTED_EXTENSIONS = (
    "https://archangel.health/fhir/StructureDefinition/synthetic-patient",
)

# Explicit, allowlisted answer-key sealing (Buyer Response PRD §3 B1). A ``Basic``
# resource we do not recognize is treated as potentially answer-bearing and
# withheld — the asymmetry is deliberate: withholding a benign Basic costs one
# quarantine and a schema update, while admitting an answer-bearing one silently
# invalidates every score computed from the case.
_SEALED_BASIC_IDS = ("sealed-ground-truth",)
_SEALED_CODE_MARKERS = ("answer key", "ground truth", "sealed", "do not release",
                        "remove before")
_TASK_BASIC_IDS = ("eval-task",)

# Interpretive language that must never sit in a neutral study LABEL (§3 B2): if a
# partner puts it in ``content.title`` we demote the whole caption to findings and
# synthesize a blander label from the modality.
_INTERPRETIVE_MARKERS = (
    "unmask", "restrict", "monotypic", "hypercellular", "deposit", "dysmorphic",
    "consistent with", "diagnostic of", "suggestive of", "compatible with",
    "positive for", "negative for", "absent", "duplicated", "cast",
)

# A study TITLE that names a diagnosis is a leaked answer even without a hedge word
# ("Crescentic glomerulonephritis", "Amyloidosis") — the label is emitted model-
# visible regardless of study_findings_policy, so it must be demoted too (§3 B2). We
# cannot enumerate every diagnosis, so we detect diagnosis MORPHOLOGY + common
# oncologic/renal terms. False positives cost a blander label; a false negative leaks
# the answer, so we bias toward demotion.
_DIAGNOSIS_MORPHOLOGY = (
    "itis", "osis", "pathy", "emia", "aemia", "oma ", "carcinoma", "sarcoma",
    "lymphoma", "leukemia", "melanoma", "adenoma", "nephritis", "glomerulo",
    "amyloid", "malignan", "neoplas", "metasta", "carcinom", "nephropathy",
    "vasculitis", "myeloma", "gammopathy", "mgrs", "pgnmid",
)


def _looks_diagnostic(text: str) -> bool:
    """True when a study title reads as a diagnosis/interpretation rather than a
    neutral modality descriptor (§3 B2). Combines the hedge/interpretive markers with
    diagnosis morphology so a bare-diagnosis title is demoted out of the label."""
    low = (text or "").lower()
    if any(m in low for m in _INTERPRETIVE_MARKERS):
        return True
    # A word ending in a diagnosis suffix (…itis/…osis/…pathy/…oma/…emia).
    if re.search(r"\b[a-z]{3,}(itis|osis|pathy|emia|aemia|oma|nephropathy|sclerosis)\b", low):
        return True
    return any(m in low for m in _DIAGNOSIS_MORPHOLOGY)


class FhirParseError(ValueError):
    """Not a parseable FHIR R4 Bundle — the bundle entry should quarantine."""


def _codeable_text(cc: Any) -> str:
    if not isinstance(cc, dict):
        return ""
    if cc.get("text"):
        return str(cc["text"])
    for coding in cc.get("coding") or []:
        if coding.get("display"):
            return str(coding["display"])
        if coding.get("code"):
            return str(coding["code"])
    return ""


def _loinc_of(cc: Any) -> Optional[str]:
    for coding in (cc or {}).get("coding") or []:
        if "loinc" in str(coding.get("system") or "").lower() and coding.get("code"):
            return str(coding["code"])
    return None


def _obs_category(res: Dict[str, Any]) -> str:
    for cat in res.get("category") or []:
        for coding in (cat or {}).get("coding") or []:
            code = str(coding.get("code") or "").lower()
            if code in ("laboratory", "vital-signs"):
                return code
    return ""


def _quantity(res: Dict[str, Any]) -> tuple:
    q = res.get("valueQuantity") or {}
    if q:
        return q.get("value"), q.get("unit") or q.get("code")
    if res.get("valueString") is not None:
        return res.get("valueString"), None
    if res.get("valueCodeableConcept"):
        return _codeable_text(res["valueCodeableConcept"]), None
    return None, None


def _flag_of(res: Dict[str, Any]) -> str:
    for interp in res.get("interpretation") or []:
        for coding in (interp or {}).get("coding") or []:
            code = str(coding.get("code") or "").upper()
            if code in ("L", "H", "LL", "HH"):
                return code
    return ""


def _ref_range(res: Dict[str, Any]) -> tuple:
    for rr in res.get("referenceRange") or []:
        lo = (rr.get("low") or {}).get("value")
        hi = (rr.get("high") or {}).get("value")
        if lo is not None or hi is not None:
            return lo, hi
    return None, None


def _effective(res: Dict[str, Any]) -> str:
    return str(res.get("effectiveDateTime") or res.get("issued")
               or (res.get("effectivePeriod") or {}).get("start") or "")


def _note_from_attachment(att: Dict[str, Any]) -> Optional[str]:
    if not isinstance(att, dict):
        return None
    ct = str(att.get("contentType") or "")
    if ct.startswith("image/") or ct == "application/dicom":
        return None  # imaging content is never decoded
    if att.get("data"):
        try:
            return base64.b64decode(att["data"]).decode("utf-8", errors="replace")
        except Exception:
            return None
    return None


def _dosage_bits(res: Dict[str, Any]) -> Dict[str, Optional[str]]:
    for d in res.get("dosage") or res.get("dosageInstruction") or []:
        dose = None
        for dr in d.get("doseAndRate") or []:
            dq = dr.get("doseQuantity") or {}
            if dq.get("value") is not None:
                dose = f"{dq.get('value')} {dq.get('unit') or ''}".strip()
                break
        route = _codeable_text(d.get("route")) or None
        freq = None
        timing = ((d.get("timing") or {}).get("code") or {})
        if timing:
            freq = _codeable_text(timing) or None
        if not freq and d.get("text"):
            freq = str(d["text"])[:60]
        return {"dose": dose, "route": route, "freq": freq}
    return {"dose": None, "route": None, "freq": None}


# ─── Extension helpers ────────────────────────────────────────────────────────
def _ext_by_suffix(res: Dict[str, Any], suffix: str) -> Optional[Any]:
    """Return the first extension value whose ``url`` ends with ``suffix`` (partners
    version their StructureDefinition URLs, so match the tail, not the whole URL)."""
    for ext in res.get("extension") or []:
        url = str((ext or {}).get("url") or "")
        if url.endswith(suffix) or url.rsplit("/", 1)[-1] == suffix:
            for k, v in ext.items():
                if k.startswith("value"):
                    return v
    return None


def _has_interpretive_language(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _INTERPRETIVE_MARKERS)


# ─── Media → Study (Buyer Response PRD §2 A2, §3 B2) ──────────────────────────
def _modality_of(res: Dict[str, Any], specialty: str) -> str:
    """Map a FHIR Media to a Study modality. Nephrology pathology (biopsy IF/EM/LM,
    urine microscopy) maps to ``pathology``; imaging keywords map to their modality;
    everything else is ``other``."""

    text = " ".join([
        _codeable_text(res.get("modality")), _codeable_text(res.get("type")),
        str(((res.get("content") or {}).get("title")) or ""),
        " ".join(str((n or {}).get("text") or "") for n in (res.get("note") or [])),
    ]).lower()
    # Pathology first: EM/IF/biopsy/microscopy are pathology, and "electron" contains
    # "ct" as a substring — a naive imaging check would mis-label EM as CT.
    if any(kw in text for kw in ("biopsy", "immunofluoresc", "electron", "pathol",
                                 "histolog", "sediment", "microscop", "paraffin",
                                 "frozen", "pronase", "stain")):
        return "pathology"
    # Imaging modalities matched on WORD boundaries (never as a substring).
    for kw, mod in (("ecg", "ecg"), ("echo", "echo"), ("cath", "cath"),
                    ("pet", "pet"), ("mri", "mri"), ("ct", "ct")):
        if re.search(rf"\b{kw}\b", text):
            return mod
    return "other"


def _split_media_caption(res: Dict[str, Any]) -> Tuple[str, str]:
    """(label, findings) for a Media (Buyer Response PRD §3 B2).

    ``content.title`` → label (a neutral descriptor, always model-visible);
    ``note[].text`` → findings (the interpretation, whose visibility is governed by
    ``study_findings_policy``). FHIR does not distinguish these, so partners keep
    mixing them: when a title contains interpretive language, demote the whole title
    into findings and synthesize a neutral label from the modality — better a
    blander label than a leaked finding."""
    title = str(((res.get("content") or {}).get("title")) or "").strip()
    note_texts = [str((n or {}).get("text") or "").strip()
                  for n in (res.get("note") or []) if (n or {}).get("text")]
    findings = " ".join(t for t in note_texts if t).strip()
    if title and _looks_diagnostic(title):
        # The title itself leaks a finding/diagnosis — demote it and use a neutral
        # modality-derived fallback (§3 B2: better a blander label than a leaked one).
        findings = (title + (" " + findings if findings else "")).strip()
        modality = _modality_of(res, "")
        title = {"pathology": "Pathology image", "ecg": "ECG", "echo": "Echocardiogram",
                 "ct": "CT image", "mri": "MRI image", "pet": "PET image"}.get(
                    modality, "Clinical image")
    return (title or "Clinical image"), findings


def _media_to_study(res: Dict[str, Any], *, specialty: str) -> Optional[Dict[str, Any]]:
    """A FHIR Media carrying an inline base64 raster in a supported MIME → a Study
    with an image asset (Buyer Response PRD §2 A2). Returns None (→ counted as
    ``_imaging_skipped``, honestly) when the content is not a supported raster."""
    from asclepius import assets

    content = res.get("content") or {}
    mime = (content.get("contentType") or "").lower()
    b64 = content.get("data")
    if not b64 or mime not in STUDY_ASSET_MIMES:
        return None
    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception:
        return None
    # assets.process_upload strips EXIF/XMP metadata, normalizes, hashes, and stores
    # content-addressed. Metadata stripping is not optional: scanner/camera metadata
    # routinely carries institution, device serial, and acquisition timestamps — PHI
    # the free-text scanner would never catch because it is not in any text field.
    try:
        stored = assets.process_upload(raw, mime, source="partner_deidentified")
    except Exception:
        return None
    title, findings = _split_media_caption(res)
    return {
        "modality": _modality_of(res, specialty),
        "label": title,
        "findings": findings,
        "measurements": [],
        "asset": {
            "asset_id": stored["asset_id"], "sha256": stored["sha256"],
            "mime": stored["mime"], "width": stored.get("width"),
            "height": stored.get("height"),
            "byte_size": stored.get("byte_size") or 0,
        },
    }


# ─── Provenance → source_refs + case_provenance (Buyer Response PRD §2 A3) ─────
def _classify_source_type(title: str) -> Optional[str]:
    low = (title or "").lower()
    if "guideline" in low or "kdigo" in low or "consensus guideline" in low:
        return "guideline"
    if "consensus" in low or "report" in low:
        return "consensus"
    if "benchmark" in low or "eval" in low or "agent" in low:
        return "benchmark"
    return "primary"


def _provenance_source_refs(res: Dict[str, Any]) -> List[Dict[str, Any]]:
    refs: List[Dict[str, Any]] = []
    for ent in res.get("entity") or []:
        role = str((ent or {}).get("role") or "").lower()
        if role and role != "source":
            continue
        what = (ent or {}).get("what") or {}
        title = str(what.get("display") or _codeable_text(what.get("type")) or "").strip()
        ident = None
        idf = what.get("identifier") or {}
        if isinstance(idf, dict):
            ident = idf.get("value") or idf.get("system")
        if not ident:
            ident = what.get("reference")
        if not title:
            continue
        refs.append({
            "title": title,
            "identifier": str(ident) if ident else None,
            "source_type": _classify_source_type(title),
            "role": "source",
        })
    return refs


def _provenance_case_provenance(res: Dict[str, Any]) -> Dict[str, Any]:
    agents: List[str] = []
    for ag in res.get("agent") or []:
        role = _codeable_text((ag or {}).get("type"))
        who = str(((ag or {}).get("who") or {}).get("display") or "")
        # Keep a GENERALIZED role, never an identity — prefer the coded type, and
        # only fall back to a who.display that does not look like a person name.
        label = role or (who if who and " " not in who else "")
        if label:
            agents.append(label)
    disclaimers: List[str] = []
    for ext in res.get("extension") or []:
        for k, v in (ext or {}).items():
            if k.startswith("value") and isinstance(v, str) and v.strip():
                disclaimers.append(v.strip())
    return {"agents": agents, "disclaimers": disclaimers, "recorded": None}


# ─── Basic disposition — task vs sealed answer key (Buyer Response PRD §3 B1) ──
def _basic_disposition(res: Dict[str, Any]) -> str:
    """'task' | 'sealed' | 'unknown' — fail CLOSED to 'sealed'.

    A Basic resource we do not recognize is treated as potentially answer-bearing
    and withheld. Withholding a benign Basic costs one quarantine and a schema
    update; admitting an answer-bearing one silently invalidates every score
    computed from the case. There is no symmetric version of this trade."""
    rid = (res.get("id") or "").strip().lower()
    code = (_codeable_text(res.get("code")) or "").lower()
    if rid in _SEALED_BASIC_IDS or any(m in code for m in _SEALED_CODE_MARKERS):
        return "sealed"
    if rid in _TASK_BASIC_IDS:
        return "task"
    return "unknown"  # → sealed


def _parse_required_modalities(value: Any) -> List[str]:
    """Normalize the required-modalities extension into tokens.

    Partners write this as prose. The real bundle uses ' + ' as the separator; the
    original ,/; split turned the entire declaration into one unmatchable token and
    quarantined a valid case (Audit PRD §P1). Split on every separator a human
    plausibly uses."""
    if value is None:
        return []
    items = value if isinstance(value, list) else _MODALITY_DELIMS.split(str(value))
    return [t.strip() for t in items if t and t.strip()]


def _basic_eval_task(res: Dict[str, Any]) -> Dict[str, Any]:
    """Capture the partner's authored question (Buyer Response PRD §2 A4): the
    ``task-prompt``, ``difficulty`` (a CLAIM → ``declared_difficulty``, never
    measured), and ``required-modalities`` (the completeness assertion)."""
    prompt = _ext_by_suffix(res, "task-prompt") or _ext_by_suffix(res, "task_prompt")
    difficulty = (_ext_by_suffix(res, "difficulty")
                  or _ext_by_suffix(res, "declared-difficulty"))
    req = (_ext_by_suffix(res, "required-modalities")
           or _ext_by_suffix(res, "required_modalities"))
    return {
        "task_prompt": str(prompt).strip() if prompt else None,
        "declared_difficulty": str(difficulty).strip() if difficulty else None,
        "required_modalities": _parse_required_modalities(req),
    }


def _basic_sealed(res: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the sealed answer key payload from a sealed Basic (Buyer Response PRD
    §3 B1). The payload NEVER enters case fragments — it is returned separately so
    ingestion can store it encrypted, keyed to the ingest case, readable only by the
    adjudication surface."""
    payload: Any = _ext_by_suffix(res, "sealed-json") or _ext_by_suffix(res, "sealed_json")
    parsed: Any = None
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except Exception:
            parsed = {"raw": payload}
    elif payload is not None:
        parsed = payload
    return {
        "basic_id": res.get("id"),
        "code": _codeable_text(res.get("code")),
        "answer_key": parsed,
    }


# Hedge/interpretive phrasing that marks the interpretive LEAP a DiagnosticReport
# conclusion can perform (Buyer Response PRD §2 A6) — the sentence a case is built to
# test whether the model makes. These are the "reasoning link" phrases ONLY; the
# pathology-finding markers (deposit/restrict/dysmorphic) are deliberately NOT here,
# because a flow report's "small kappa-restricted population is present" is an
# OBJECTIVE finding that must ship, not a hint.
_CONCLUSION_INTERPRETIVE_MARKERS = (
    "may be", "may represent", "clinically significant", "in the setting of",
    "criteria for", "concerning for", "suggests", "suggestive of", "consistent with",
    "significance", "raises the", "favor", "likely represents", "in keeping with",
    "compatible with", "worrisome for", "in the context of",
    # Direct-assertion diagnostic phrasing — a conclusion that NAMES the diagnosis is
    # the reasoning leap the case tests, not an objective result (Buyer Response §2 A6).
    "diagnostic of", "diagnostic for", "characteristic of", "pathognomonic",
    "no evidence of", "represents a", "represents ", "positive for a",
    "positive for an", "negative for a", "most consistent with", "in favor of",
    "indicative of", "confirms", "confirming",
)


def _split_report_conclusion(conclusion: str) -> Tuple[str, str]:
    """(objective, interpretive) for a DiagnosticReport conclusion (Buyer Response
    PRD §2 A6). The objective finding always ships; a sentence that performs the
    interpretive link is withheld from the model-facing prompt. A conclusion that
    names the reasoning link is not evidence, it is a hint — and on a case built to
    test whether the model makes that link, a hint is indistinguishable from the
    answer.

    Sentence-level split: any sentence carrying interpretive/hedge language is
    interpretive; the rest is objective. If none is flagged, the whole conclusion is
    objective (nothing is withheld gratuitously)."""

    text = (conclusion or "").strip()
    if not text:
        return "", ""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    objective, interpretive = [], []
    for s in sentences:
        low = s.lower()
        if any(m in low for m in _CONCLUSION_INTERPRETIVE_MARKERS):
            interpretive.append(s)
        else:
            objective.append(s)
    return " ".join(objective).strip(), " ".join(interpretive).strip()


def parse(raw: Any, *, specialty: str = "general", manifest: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """FHIR R4 Bundle JSON (str/bytes/dict) → ClinicalCase fragments."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception as exc:
            raise FhirParseError(f"not valid JSON: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("resourceType") != "Bundle":
        raise FhirParseError("not a FHIR R4 Bundle (resourceType != 'Bundle')")

    # Guard each entry: a malformed bundle can carry a non-dict entry (a JSON null or
    # string). ``e.get`` on a non-dict would raise AttributeError and discard the
    # WHOLE bundle; instead we skip only the bad entry.
    resources = [e.get("resource") for e in (raw.get("entry") or [])
                 if isinstance(e, dict) and isinstance(e.get("resource"), dict)]

    frag: Dict[str, Any] = {
        "demographics": {}, "lab_panels": [], "notes": [], "medications": [],
        "problem_list": [], "vitals": {}, "studies": [], "source_refs": [],
        "_imaging_skipped": 0, "_patient_keys": [],
        # Answer-adjacent + author metadata routed OUT of the case body (never merged
        # into fragments a model can see) — ingestion.py consumes these:
        "_sealed_ground_truth": None,     # §3 B1 — stored encrypted, separately
        "_eval_task": None,               # §2 A4 — task prompt + declared difficulty
        "_case_provenance": None,         # §2 A3 — Provenance.agent + disclaimers
        "_synthetic_declared": False,     # §2 A5 — recorded, NEVER trusted
    }

    birth_date = None
    lab_by_key: Dict[tuple, Dict[str, Any]] = {}
    report_names: Dict[str, str] = {}   # DiagnosticReport date → panel name
    latest_obs = None

    # First pass: DiagnosticReport names (so lab panels get real names) + latest date.
    for res in resources:
        rt = res.get("resourceType")
        if rt == "DiagnosticReport":
            eff = _effective(res)
            d = parse_datetime(eff)
            if d and _codeable_text(res.get("code")):
                report_names[str(d)] = _codeable_text(res.get("code"))
        if rt in ("Observation", "DiagnosticReport"):
            d = parse_datetime(_effective(res))
            if d and (latest_obs is None or d > latest_obs):
                latest_obs = d

    for res in resources:
        rt = res.get("resourceType")
        if rt in _IMAGING_TYPES or (
            rt == "Binary" and str(res.get("contentType") or "").startswith(("image/", "application/dicom"))
        ):
            frag["_imaging_skipped"] += 1
            continue

        if rt in _RASTER_MEDIA_TYPES:
            # Media → Study.asset (Buyer Response PRD §2 A2). A supported inline
            # raster becomes a Study with an image asset + split caption; anything
            # else is counted as skipped imaging so the honest count survives.
            study = _media_to_study(res, specialty=specialty)
            if study is not None:
                frag["studies"].append(study)
            else:
                frag["_imaging_skipped"] += 1
            continue

        if rt == "Basic":
            # Explicit, allowlisted sealing (Buyer Response PRD §3 B1). Fail closed:
            # an unrecognized Basic is treated as answer-bearing and withheld.
            disp = _basic_disposition(res)
            if disp == "task":
                frag["_eval_task"] = _basic_eval_task(res)
            else:  # 'sealed' or 'unknown' → sealed; NEVER enters fragments
                sealed = _basic_sealed(res)
                # Keep the first sealed key (a bundle should carry exactly one).
                if frag["_sealed_ground_truth"] is None:
                    frag["_sealed_ground_truth"] = sealed
            continue

        if rt == "Provenance":
            frag["source_refs"].extend(_provenance_source_refs(res))
            if frag["_case_provenance"] is None:
                frag["_case_provenance"] = _provenance_case_provenance(res)
            continue

        if rt == "Patient":
            if res.get("id"):
                frag["_patient_keys"].append(str(res["id"]))
            gender = str(res.get("gender") or "").lower()
            if gender in ("male", "female"):
                frag["demographics"]["sex"] = "M" if gender == "male" else "F"
            birth_date = parse_datetime(res.get("birthDate"))
            # A partner-declared synthetic flag is METADATA, never a control signal
            # (Buyer Response PRD §2 A5): recorded for provenance, never branched on —
            # the de-id guard runs identically regardless.
            for ext in res.get("extension") or []:
                if str((ext or {}).get("url") or "") in _NEVER_TRUSTED_EXTENSIONS:
                    frag["_synthetic_declared"] = bool(ext.get("valueBoolean"))
            # Deliberately read-and-discarded, NEVER merged into fragments: name /
            # identifier / telecom / address (incl. a 3-digit ZIP prefix, dropped
            # entirely rather than reasoned about — Safe Harbor permits it only above
            # 20,000 population).

        elif rt == "Observation":
            cat = _obs_category(res)
            value, unit = _quantity(res)
            if value is None:
                continue
            name = _codeable_text(res.get("code")) or "Observation"
            if cat == "vital-signs":
                frag["vitals"][name] = f"{value} {unit}".strip() if unit else value
                # Vitals collapse into ONE flat dict, so they carry one timing marker
                # for the set: the LATEST vital-sign date (the most recent set is what
                # a "current vitals" read returns). ``timeline`` converts it to a
                # relative offset so V5 can gate the set. Reserved key, stripped
                # before the dict is ever returned to an agent.
                veff = _effective(res)
                if veff:
                    prior = frag.get("_vitals_at")
                    if not prior or str(veff) > str(prior):
                        frag["_vitals_at"] = str(veff)
                continue
            if cat != "laboratory":
                continue
            eff = _effective(res)
            d = parse_datetime(eff)
            key = (str(d) if d else "", )
            panel = lab_by_key.setdefault(key, {
                "panel": report_names.get(str(d), "Labs") if d else "Labs",
                "results": [],
                **({"collected_at": eff} if eff else {"collected_offset_days": 0}),
            })
            lo, hi = _ref_range(res)
            result: Dict[str, Any] = {"analyte": name, "value": value, "flag": _flag_of(res)}
            loinc = _loinc_of(res.get("code"))
            if loinc:
                result["loinc"] = loinc
            if unit:
                result["unit"] = unit
            if lo is not None:
                result["ref_low"] = lo
            if hi is not None:
                result["ref_high"] = hi
            panel["results"].append(result)

        elif rt == "Condition":
            cond = _codeable_text(res.get("code"))
            if cond:
                problem = {
                    "condition": cond,
                    "since": str(res.get("onsetDateTime") or res.get("recordedDate") or "") or None,
                }
                # The chart-RECORDING date (distinct from clinical onset in ``since``)
                # → timeline converts it to a relative offset so a problem recorded
                # AFTER the decision point can be held out by V5 (it IS the answer).
                recorded = res.get("recordedDate")
                if recorded:
                    problem["recorded_at"] = str(recorded)
                frag["problem_list"].append(problem)

        elif rt in ("MedicationStatement", "MedicationRequest"):
            drug = _codeable_text(res.get("medicationCodeableConcept"))
            if drug:
                med = {"drug": drug, **_dosage_bits(res)}
                # The ORDER date → relative offset, so a drug started after the
                # decision point (which reveals the diagnosis) can be held out.
                authored = res.get("authoredOn") or res.get("effectiveDateTime") or (
                    (res.get("effectivePeriod") or {}).get("start"))
                if authored:
                    med["authored_on"] = str(authored)
                frag["medications"].append(med)

        elif rt in ("DocumentReference", "Composition", "DiagnosticReport"):
            texts: List[str] = []
            for content in res.get("content") or []:
                t = _note_from_attachment((content or {}).get("attachment") or {})
                if t:
                    texts.append(t)
            for form in res.get("presentedForm") or []:
                t = _note_from_attachment(form)
                if t:
                    texts.append(t)
            note_type = _codeable_text(res.get("type")) or ("Report" if rt == "DiagnosticReport" else "Progress")
            role = (specialty or "clinician").lower()
            # Carry the note's authoring date (DocumentReference.date /
            # Composition.date / DiagnosticReport.effective[DateTime]) so
            # ``timeline.normalize_timeline`` can convert it to a relative
            # ``collected_offset_days`` — the temporal cutoff for narratives (V5). The
            # raw date is destroyed by timeline; it never survives to the case/export.
            note_date = _effective(res) or (res.get("date") or "")

            def _note(**kw):
                # Every note carries the authoring date (V5 timing) AND respects the
                # objective/interpretive visibility gate (Buyer Response PRD §2 A6).
                if note_date:
                    kw["collected_at"] = note_date
                frag["notes"].append(kw)

            for t in texts:
                if rt == "DiagnosticReport":
                    # A DiagnosticReport narrative body routinely EMBEDS the
                    # impression/conclusion inline (§2 A6) — split it the same way as
                    # the structured conclusion so the interpretive sentence in the
                    # body is withheld too, not just the separate conclusion field.
                    objective, interpretive = _split_report_conclusion(t.strip())
                    if objective:
                        _note(note_type=note_type[:40], author_role=role,
                              text=objective, model_visible=True)
                    if interpretive:
                        _note(note_type=(note_type[:28] + " interpretation"),
                              author_role=role, text=interpretive, model_visible=False,
                              withheld_reason="interpretive_conclusion")
                else:
                    _note(note_type=note_type[:40], author_role=role, text=t.strip())
            # DiagnosticReport.conclusion is answer-bearing (Buyer Response PRD §2 A6):
            # split the objective result from the interpretive tail and gate the
            # latter. The objective finding always ships; the interpretation is
            # withheld from the model prompt and preserved for the annotator + key.
            if rt == "DiagnosticReport":
                objective, interpretive = _split_report_conclusion(
                    str(res.get("conclusion") or ""))
                if objective:
                    _note(note_type=note_type[:40], author_role=role,
                          text=objective, model_visible=True)
                if interpretive:
                    _note(note_type=(note_type[:28] + " interpretation"),
                          author_role=role, text=interpretive, model_visible=False,
                          withheld_reason="interpretive_conclusion")

    # Age band: birthDate against the bundle's latest observation date — the age
    # AT THE ENCOUNTER, banded (never an exact age or the birth date itself).
    if birth_date and latest_obs:
        years = latest_obs.year - birth_date.year - (
            (latest_obs.month, latest_obs.day) < (birth_date.month, birth_date.day)
        )
        band = age_to_band(years)
        if band:
            frag["demographics"]["age_band"] = band

    frag["lab_panels"] = list(lab_by_key.values())
    # Suggested index anchor (PRD §7 "latest encounter/lab collection datetime"):
    # the adapter sees ALL observation timestamps (incl. vitals), so it proposes
    # the true latest; the manifest's index_event still overrides downstream.
    if latest_obs is not None:
        frag["_index_event"] = str(latest_obs)
    return frag
