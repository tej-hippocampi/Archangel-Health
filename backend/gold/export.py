"""JSONL export + data dictionary generation (PRD §5.5, §9.8).

Produces the deliverable dataset: one canonical record per line (validated
against ``gold.schema``) plus a generated ``data_dictionary.md``. ``tenant_slug``
and ``clinician_id_hashed`` are internal provenance — they are pseudonymized in
the delivered output unless the caller opts to keep them.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from gold import schema

_SFT_SYSTEM = "You are a clinical scribe. Produce the clinical note for this visit."


def _deid_model_version() -> str:
    try:
        from ai.model_config import resolve
        return resolve("gold_deid").get("model", "")
    except Exception:
        return ""


def _pseudonymize(record: Dict[str, Any]) -> Dict[str, Any]:
    """Strip internal provenance for buyer delivery (default).

    ``tenant_slug`` is removed entirely; ``record_id`` is rebuilt on a stable
    pseudonym so records can't be re-linked to the tenant.
    """
    out = dict(record)
    slug = out.get("tenant_slug") or "tenant"
    pseudo = hashlib.sha256(slug.encode("utf-8")).hexdigest()[:8]
    num = (out.get("record_id") or "").rsplit("-", 1)[-1]
    out["record_id"] = f"gold-{pseudo}-{num}"
    out.pop("tenant_slug", None)
    return out


def build_export(
    visits: List[Dict[str, Any]],
    *,
    pseudonymize: bool = True,
) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Build canonical rich-record JSONL text from decrypted visit dicts.

    Returns ``(jsonl_text, exported_records, rejected)`` where ``rejected`` is a
    list of ``{record_id, errors}`` for visits that failed schema validation —
    including the residual-identifier gate — and are excluded from the JSONL.
    """
    model_version = _deid_model_version()
    lines: List[str] = []
    exported: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    for visit in visits:
        record = schema.build_record(visit, deid_model_version=model_version)
        errors = schema.validate_record(record)
        if errors:
            rejected.append({"record_id": record.get("record_id"), "errors": errors})
            continue
        delivered = _pseudonymize(record) if pseudonymize else record
        lines.append(json.dumps(delivered, ensure_ascii=False))
        exported.append(delivered)
    return ("\n".join(lines) + ("\n" if lines else "")), exported, rejected


def sft_messages_jsonl(records: List[Dict[str, Any]]) -> str:
    """Train-ready chat/messages JSONL (one line per record) for SFT loaders."""
    lines: List[str] = []
    for r in records:
        row = {
            "messages": [
                {"role": "system", "content": _SFT_SYSTEM},
                {"role": "user", "content": r.get("transcript_deid") or ""},
                {"role": "assistant", "content": r.get("gold_note") or ""},
            ],
            "metadata": {
                "record_id": r.get("record_id"),
                "specialty": r.get("specialty"),
                "tasks": r.get("tasks") or [],
                "difficulty_tags": (r.get("audio_metadata") or {}).get("difficulty_tags") or [],
                "schema_version": r.get("schema_version"),
                "split": r.get("split") or "train",
            },
        }
        lines.append(json.dumps(row, ensure_ascii=False))
    return "\n".join(lines) + ("\n" if lines else "")


DATA_DICTIONARY_MD = """# Gold Standard Dataset — Data Dictionary

Schema version: __SCHEMA_VERSION__
De-identification standard: HIPAA Safe Harbor (automated + human QA)

Each line in the `.jsonl` file is one clinician-verified, de-identified visit
record. Records are emitted only after: patient consent, automated PHI scrub,
human QA approval, and a `baa_on_file = true` gate.

## Top-level fields

| Field | Type | Description |
|---|---|---|
| `schema_version` | string | Record schema version (e.g. `1.1.0`). |
| `record_id` | string | Pseudonymous record identifier. |
| `content_sha256` | string | Hash of the de-identified payload (dedup / contamination check). |
| `specialty` | string | Clinical specialty of the encounter. |
| `encounter_type` | string | Encounter type (e.g. "post-op follow-up"). |
| `split` | string | Dataset split (`train` by default). |
| `tasks` | array | Workflow tasks served (e.g. `note_generation`, `icd10_coding`). |
| `consent` | object | Consent + BAA provenance (see below). |
| `deidentification` | object | De-id standard, method, assurance metadata (see below). |
| `reviewer` | object | Pseudonymous reviewer: `{role, specialty, id_hashed}`. |
| `audio_metadata` | object | Duration, difficulty tags, languages. |
| `transcript_deid` | string | De-identified conversation transcript. |
| `ai_draft_note` | string | De-identified model draft — the "before", NOT ground truth. |
| `gold_note` | string | Clinician-verified, de-identified note — the label ("after"). |
| `correction` | object | Quantified diff/provenance of the human change (see below). |
| `error_labels` | array | Tagged corrections the clinician made to the draft. |
| `workflow_outputs` | object | Billing codes + optional prior-auth outcome. |
| `clinician_review_seconds` | number | Seconds the clinician spent reviewing. |
| `clinician_id_hashed` | string | Pseudonymous clinician id (SHA-256). |
| `created_at` | string | Visit date (YYYY-MM-DD). |

## `correction`
| Field | Type | Description |
|---|---|---|
| `was_edited` | bool | Whether the clinician changed the draft (false = "model was right"). |
| `edit_distance_chars` | number | Char-level edit distance draft→gold. |
| `edit_ratio` | number | `edit_distance_chars / max(len(draft), len(gold))`. |
| `draft_note_deid` | string | De-identified AI draft (the "before"). |
| `gold_note_deid` | string | De-identified gold note (the "after"). |
| `num_error_labels` | number | Count of clinician error labels. |

## `deidentification`
| Field | Type | Description |
|---|---|---|
| `standard` | string | Always `HIPAA Safe Harbor`. |
| `method` | string | Human-readable method summary. |
| `method_detail` | string | Layers that ran, e.g. `regex+llm`. |
| `deid_model_version` | string | LLM model used for the LLM de-id layer. |
| `verified_by_operator` | bool | Always `true` (independent human QA). |
| `qa_operator_id_hashed` | string | Pseudonymous QA operator id (SHA-256). |
| `residual_scan_passed` | bool | Always `true` (residual-identifier gate). |

## `consent`
| Field | Type | Description |
|---|---|---|
| `consent_given` | bool | Always `true` for exported records. |
| `consent_method` | string | `in_app_verbal` or `e_signature`. |
| `consent_timestamp` | string | ISO-8601 timestamp of consent. |
| `baa_on_file` | bool | Always `true` for exported records. |

## `audio_metadata`
| Field | Type | Description |
|---|---|---|
| `duration_sec` | number | Audio duration in seconds. |
| `difficulty_tags` | array | e.g. `background_noise`, `translator_present`, `accent`. |
| `languages` | array | ISO language codes present in the conversation. |

## `error_labels[]`
| Field | Type | Description |
|---|---|---|
| `type` | string | Error taxonomy type (e.g. `medication_error`, `hallucination`). |
| `subtype` | string | Optional taxonomy subtype. |
| `severity` | string | `low` / `medium` / `high`. |
| `section` | string | Note section the error appeared in. |
| `original_text` | string | The model's incorrect text. |
| `corrected_text` | string | The clinician's correction. |
| `clinician_verified` | bool | Always `true`. |

## `workflow_outputs`
| Field | Type | Description |
|---|---|---|
| `billing_codes[]` | array | `{system: "ICD-10"\\|"CPT", code, verified_by}`. |
| `prior_auth` | object\\|null | `{drug_or_service, justification_text, outcome}`. |

The pairing of `gold_note` + `error_labels` is the supervised-fine-tuning /
evaluation payload; `workflow_outputs` captures the conversation→workflow pairs.
"""


def data_dictionary_md() -> str:
    return DATA_DICTIONARY_MD.replace("__SCHEMA_VERSION__", schema.SCHEMA_VERSION)


def _breakdown(records: List[Dict[str, Any]], key_fn) -> Dict[str, int]:
    c: Counter = Counter()
    for r in records:
        for v in key_fn(r):
            c[v] += 1
    return dict(sorted(c.items(), key=lambda kv: (-kv[1], kv[0])))


def _date_range(records: List[Dict[str, Any]]) -> Tuple[str, str]:
    dates = sorted(d for d in (r.get("created_at") for r in records) if d)
    if not dates:
        return ("n/a", "n/a")
    return (dates[0], dates[-1])


def dataset_card_md(records: List[Dict[str, Any]]) -> str:
    """A datasheet/data-card (Datasheets for Datasets style) for the export."""
    n = len(records)
    lo, hi = _date_range(records)
    specialties = _breakdown(records, lambda r: [r.get("specialty") or "unknown"])
    tasks = _breakdown(records, lambda r: r.get("tasks") or [])
    difficulty = _breakdown(records, lambda r: (r.get("audio_metadata") or {}).get("difficulty_tags") or [])
    edited = sum(1 for r in records if (r.get("correction") or {}).get("was_edited"))

    def tbl(d: Dict[str, int]) -> str:
        if not d:
            return "_(none)_"
        return "\n".join(f"| `{k}` | {v} |" for k, v in d.items())

    return f"""# Gold Standard Dataset — Dataset Card

Schema version: {schema.SCHEMA_VERSION}
Records: {n}  •  Visit date range: {lo} → {hi}  •  Edited (vs. "model was right"): {edited}/{n}

## Dataset summary
Clinician-verified, de-identified clinical-conversation records. Each record
pairs a de-identified visit transcript with a clinician-approved "gold" note,
plus a machine-readable provenance of how the AI draft was corrected. Intended
for supervised fine-tuning and evaluation of clinical scribe / coding /
prior-auth models. **The product is the data record.**

## Provenance
- Source: single-tenant clinical visits captured in-encounter (tablet/web).
- Consent basis: per-visit patient consent (in-app verbal or e-signature),
  recorded in `consent`.
- Collection method: in-visit audio → STT → LLM draft note → clinician gold
  label + error tagging → de-identification → independent human QA.
- Visit date range: {lo} → {hi}.

## De-identification
- Standard: **HIPAA Safe Harbor**.
- Method: automated regex baseline + optional LLM / Presidio layer + **mandatory
  independent human QA** (`verified_by_operator = true`).
- Residual-PHI gate: every record is re-scanned for direct identifiers before
  export; any hit rejects the record (`residual_scan_passed = true` on all
  exported records).

## Intended use
SFT + evaluation data for clinical documentation models (note generation,
ICD-10 / CPT coding, prior-auth justification). Not for clinical
decision-making.

## Licensing
Non-exclusive. Buyers receive a pseudonymized copy; tenant/clinician identities
are SHA-256 hashed and never delivered in raw form.

## Breakdowns
### By specialty
| specialty | records |
|---|---|
{tbl(specialties)}

### By task
| task | records |
|---|---|
{tbl(tasks)}

### By difficulty tag
| tag | records |
|---|---|
{tbl(difficulty)}

## Known limitations
- Single-tenant pilot; specialty mix reflects one health system.
- De-identification is best-effort Safe Harbor; rare residual identifiers may
  evade automated + human review despite the residual-PHI gate.
- `ai_draft_note` is model scaffolding, not ground truth — only `gold_note` is
  the label.
"""


def croissant_json(records: List[Dict[str, Any]], *, jsonl_name: str = "gold_records.jsonl") -> Dict[str, Any]:
    """Minimal MLCommons Croissant sidecar describing the JSONL fields."""
    def field(name: str, dtype: str, desc: str) -> Dict[str, Any]:
        return {
            "@type": "cr:Field",
            "@id": f"records/{name}",
            "name": name,
            "description": desc,
            "dataType": dtype,
            "source": {"fileObject": {"@id": jsonl_name}, "extract": {"column": name}},
        }

    return {
        "@context": {
            "@vocab": "https://schema.org/",
            "cr": "http://mlcommons.org/croissant/",
            "dataType": "cr:dataType",
        },
        "@type": "sc:Dataset",
        "name": "gold_standard_clinical_conversations",
        "description": "Clinician-verified, de-identified clinical conversation records for SFT/eval.",
        "version": schema.SCHEMA_VERSION,
        "license": "Non-exclusive (commercial); pseudonymized delivery.",
        "recordSet": [{
            "@type": "cr:RecordSet",
            "@id": "records",
            "name": "records",
            "field": [
                field("record_id", "sc:Text", "Pseudonymous record id."),
                field("content_sha256", "sc:Text", "Hash of de-identified payload."),
                field("specialty", "sc:Text", "Clinical specialty."),
                field("tasks", "sc:Text", "Workflow tasks served."),
                field("transcript_deid", "sc:Text", "De-identified transcript (input)."),
                field("gold_note", "sc:Text", "De-identified clinician-verified note (label)."),
                field("ai_draft_note", "sc:Text", "De-identified AI draft (before)."),
                field("schema_version", "sc:Text", "Record schema version."),
                field("split", "sc:Text", "Dataset split."),
            ],
        }],
        "recordCount": len(records),
    }
