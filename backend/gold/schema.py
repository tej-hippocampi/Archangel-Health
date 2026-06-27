"""Canonical exported-record builder + validator (PRD §7).

The exported record is *the product*. ``build_record`` turns a stored
``gold_visits`` row (already decrypted by ``store._row_to_dict``) into the
canonical schema; ``validate_record`` checks a record against that schema and is
reused by the export endpoint and the Phase-6 test fixtures.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from typing import Any, Dict, List, Optional

from gold.config import (
    taxonomy_sections,
    taxonomy_severities,
    taxonomy_types,
    workflow_tasks as taxonomy_workflow_tasks,
)
from gold.deid import residual_identifiers

# 1.1.0: machine-readable correction/provenance block, workflow tasks, content
# hash, pseudonymous reviewer block, de-id assurance metadata, train-ready split.
SCHEMA_VERSION = "1.1.0"


def hash_clinician(actor: str) -> str:
    """Stable pseudonymous clinician id (repo convention: sha256 hexdigest)."""
    return hashlib.sha256((actor or "").encode("utf-8")).hexdigest()


def record_id_for(tenant_slug: str, record_num: int) -> str:
    slug = (tenant_slug or "tenant").strip() or "tenant"
    return f"{slug}-gold-{int(record_num):06d}"


def _edit_stats(before: str, after: str) -> Dict[str, Any]:
    """Char-level edit distance + ratio between draft (before) and gold (after)."""
    before = before or ""
    after = after or ""
    sm = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    changed = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "replace":
            changed += max(i2 - i1, j2 - j1)
        elif tag == "delete":
            changed += i2 - i1
        elif tag == "insert":
            changed += j2 - j1
    denom = max(len(before), len(after), 1)
    return {"edit_distance_chars": changed, "edit_ratio": round(changed / denom, 4)}


def derive_tasks(visit: Dict[str, Any]) -> List[str]:
    """Workflow tasks each record serves (always note_generation + derived)."""
    tasks = ["note_generation"]
    billing = visit.get("billing_codes") or []
    systems = {str(c.get("system", "")).upper() for c in billing if isinstance(c, dict)}
    if "ICD-10" in systems or "ICD10" in systems:
        tasks.append("icd10_coding")
    if "CPT" in systems:
        tasks.append("cpt_coding")
    if visit.get("prior_auth") or visit.get("prior_auth_deid"):
        tasks.append("prior_auth")
    # Surgeon-confirmed extras from the review UI.
    valid = set(taxonomy_workflow_tasks())
    for t in visit.get("tasks") or []:
        if t in valid and t not in tasks:
            tasks.append(t)
    # Stable ordered de-dup.
    seen: List[str] = []
    for t in tasks:
        if t not in seen:
            seen.append(t)
    return seen


def _content_hash(payload: Dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _residual_texts(
    transcript: str,
    gold: str,
    draft: str,
    labels: List[Dict[str, Any]],
    prior_auth: Optional[Dict[str, Any]],
) -> List[str]:
    """Every free-text field that the residual-identifier gate must inspect."""
    texts: List[str] = [transcript or "", gold or "", draft or ""]
    for lbl in labels or []:
        if isinstance(lbl, dict):
            texts.append(lbl.get("original_text") or "")
            texts.append(lbl.get("corrected_text") or "")
    if isinstance(prior_auth, dict):
        texts.append(prior_auth.get("justification_text") or "")
    return texts


def build_record(visit: Dict[str, Any], *, deid_model_version: str = "") -> Dict[str, Any]:
    """Build the canonical export record from a decrypted visit dict.

    Only ever emits de-identified text — the raw ``ai_draft_note`` / raw label
    text / raw justification are never surfaced.
    """
    created_at = (visit.get("created_at") or "")[:10]

    transcript_deid = visit.get("transcript_deid") or ""
    draft_deid = visit.get("ai_draft_note_deid") or ""
    gold_deid = visit.get("gold_note_deid") or ""
    labels = visit.get("error_labels_deid")
    if labels is None:
        labels = visit.get("error_labels") or []
    prior_auth = visit.get("prior_auth_deid")
    if prior_auth is None:
        prior_auth = visit.get("prior_auth")

    num_labels = len(labels) if isinstance(labels, list) else 0
    stats = _edit_stats(draft_deid, gold_deid)
    was_edited = num_labels > 0 or stats["edit_distance_chars"] > 0

    workflow_outputs = {
        "billing_codes": visit.get("billing_codes") or [],
        "prior_auth": prior_auth,
    }
    tasks = derive_tasks(visit)

    residual_clean = not any(
        residual_identifiers(t)
        for t in _residual_texts(transcript_deid, gold_deid, draft_deid, labels, prior_auth)
    )

    record: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "record_id": record_id_for(visit.get("tenant_slug") or "", visit.get("record_num") or 0),
        "tenant_slug": visit.get("tenant_slug") or "",
        "specialty": visit.get("specialty") or "",
        "encounter_type": visit.get("encounter_type") or "",
        "split": visit.get("split") or "train",
        "tasks": tasks,
        "consent": {
            "consent_given": bool(visit.get("consent_given")),
            "consent_method": visit.get("consent_method") or "",
            "consent_timestamp": visit.get("consent_timestamp") or "",
            "baa_on_file": bool(visit.get("baa_on_file")),
        },
        "deidentification": {
            "standard": "HIPAA Safe Harbor",
            "method": visit.get("deid_method") or "automated + human QA",
            "method_detail": visit.get("deid_method_detail") or "regex",
            "deid_model_version": deid_model_version,
            "verified_by_operator": bool(visit.get("verified_by_operator")),
            "qa_operator_id_hashed": hash_clinician(visit.get("approved_by") or ""),
            "residual_scan_passed": residual_clean,
        },
        "reviewer": {
            "role": visit.get("submitted_by_role") or "surgeon",
            "specialty": visit.get("specialty") or "",
            "id_hashed": visit.get("clinician_id_hashed") or "",
        },
        "audio_metadata": {
            "duration_sec": visit.get("audio_duration_sec"),
            "difficulty_tags": visit.get("difficulty_tags") or [],
            "languages": visit.get("languages") or [],
        },
        "transcript_deid": transcript_deid,
        "ai_draft_note": draft_deid,
        "gold_note": gold_deid,
        "correction": {
            "was_edited": was_edited,
            "edit_distance_chars": stats["edit_distance_chars"],
            "edit_ratio": stats["edit_ratio"],
            "draft_note_deid": draft_deid,
            "gold_note_deid": gold_deid,
            "num_error_labels": num_labels,
        },
        "error_labels": labels,
        "workflow_outputs": workflow_outputs,
        "clinician_review_seconds": visit.get("clinician_review_seconds"),
        "clinician_id_hashed": visit.get("clinician_id_hashed") or "",
        "created_at": created_at,
    }
    record["content_sha256"] = _content_hash({
        "transcript_deid": transcript_deid,
        "ai_draft_note": draft_deid,
        "gold_note": gold_deid,
        "error_labels": labels,
        "workflow_outputs": workflow_outputs,
    })
    return record


def validate_record(record: Dict[str, Any]) -> List[str]:
    """Return a list of human-readable schema violations (empty == valid)."""
    errors: List[str] = []

    def req(obj: Dict[str, Any], key: str, types: tuple) -> Any:
        if key not in obj:
            errors.append(f"missing field: {key}")
            return None
        if not isinstance(obj[key], types):
            errors.append(f"field {key} must be {', '.join(t.__name__ for t in types)}")
        return obj.get(key)

    if not isinstance(record, dict):
        return ["record must be an object"]

    req(record, "schema_version", (str,))
    req(record, "record_id", (str,))
    req(record, "specialty", (str,))
    req(record, "encounter_type", (str,))
    transcript = req(record, "transcript_deid", (str,))
    draft = req(record, "ai_draft_note", (str,))
    gold = req(record, "gold_note", (str,))
    if isinstance(gold, str) and not gold.strip():
        errors.append("gold_note (de-identified) must not be empty")
    req(record, "clinician_id_hashed", (str,))
    req(record, "content_sha256", (str,))

    tasks = record.get("tasks")
    if not isinstance(tasks, list) or "note_generation" not in (tasks or []):
        errors.append("tasks must be a list containing at least 'note_generation'")

    corr = record.get("correction")
    if not isinstance(corr, dict):
        errors.append("correction block is required")
    else:
        for k in ("was_edited", "edit_distance_chars", "draft_note_deid", "gold_note_deid"):
            if k not in corr:
                errors.append(f"correction.{k} is required")

    consent = req(record, "consent", (dict,)) or {}
    if isinstance(consent, dict):
        if consent.get("consent_given") is not True:
            errors.append("consent.consent_given must be true for an exportable record")
        if not consent.get("consent_method"):
            errors.append("consent.consent_method is required")
        if consent.get("baa_on_file") is not True:
            errors.append("consent.baa_on_file must be true (BAA gating)")

    deid = req(record, "deidentification", (dict,)) or {}
    if isinstance(deid, dict):
        if deid.get("standard") != "HIPAA Safe Harbor":
            errors.append("deidentification.standard must be 'HIPAA Safe Harbor'")
        if deid.get("verified_by_operator") is not True:
            errors.append("deidentification.verified_by_operator must be true (human QA)")
        if deid.get("residual_scan_passed") is not True:
            errors.append("deidentification.residual_scan_passed must be true")

    audio = req(record, "audio_metadata", (dict,)) or {}
    if isinstance(audio, dict):
        if not isinstance(audio.get("difficulty_tags", []), list):
            errors.append("audio_metadata.difficulty_tags must be a list")
        if not isinstance(audio.get("languages", []), list):
            errors.append("audio_metadata.languages must be a list")

    labels = record.get("error_labels")
    if not isinstance(labels, list):
        errors.append("error_labels must be a list")
    else:
        valid_types = taxonomy_types()
        valid_sev = taxonomy_severities()
        valid_sec = taxonomy_sections()
        for i, lbl in enumerate(labels):
            if not isinstance(lbl, dict):
                errors.append(f"error_labels[{i}] must be an object")
                continue
            if lbl.get("type") not in valid_types:
                errors.append(f"error_labels[{i}].type '{lbl.get('type')}' not in taxonomy")
            if lbl.get("severity") not in valid_sev:
                errors.append(f"error_labels[{i}].severity '{lbl.get('severity')}' invalid")
            if lbl.get("section") and lbl.get("section") not in valid_sec:
                errors.append(f"error_labels[{i}].section '{lbl.get('section')}' invalid")
            if lbl.get("clinician_verified") is not True:
                errors.append(f"error_labels[{i}].clinician_verified must be true")

    wf = req(record, "workflow_outputs", (dict,)) or {}
    if isinstance(wf, dict):
        if not isinstance(wf.get("billing_codes", []), list):
            errors.append("workflow_outputs.billing_codes must be a list")
        else:
            for i, code in enumerate(wf.get("billing_codes", [])):
                if not isinstance(code, dict):
                    errors.append(f"billing_codes[{i}] must be an object")
                    continue
                if code.get("system") not in ("ICD-10", "CPT"):
                    errors.append(f"billing_codes[{i}].system must be ICD-10 or CPT")
                if not code.get("code"):
                    errors.append(f"billing_codes[{i}].code is required")

    # Residual-identifier gate (A3): NOTHING with a direct identifier may export.
    scan_targets: List[tuple] = [
        ("transcript_deid", transcript if isinstance(transcript, str) else ""),
        ("gold_note", gold if isinstance(gold, str) else ""),
        ("ai_draft_note", draft if isinstance(draft, str) else ""),
    ]
    for i, lbl in enumerate(record.get("error_labels") or []):
        if isinstance(lbl, dict):
            scan_targets.append((f"error_labels[{i}].original_text", lbl.get("original_text") or ""))
            scan_targets.append((f"error_labels[{i}].corrected_text", lbl.get("corrected_text") or ""))
    pa = (record.get("workflow_outputs") or {}).get("prior_auth")
    if isinstance(pa, dict):
        scan_targets.append(("workflow_outputs.prior_auth.justification_text", pa.get("justification_text") or ""))

    for field, text in scan_targets:
        hits = residual_identifiers(text)
        if hits:
            errors.append(f"{field} still contains residual identifiers: {', '.join(hits)}")

    return errors
