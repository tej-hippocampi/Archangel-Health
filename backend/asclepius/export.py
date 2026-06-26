"""Export export-ready records to the format a lab needs.

Produces a JSONL string (one record/line, mapped through a buyer profile) plus
companion markdown docs (data dictionary, datasheet, quality report) so a batch
is buyer-ingestible with zero rework.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Optional

from .buyer_profiles import apply_profile, get_profile


def build_jsonl(records: list[dict[str, Any]], profile: dict[str, Any]) -> str:
    included = set(profile.get("record_types") or [])
    lines = []
    for rec in records:
        if included and rec.get("type") not in included:
            continue
        mapped = apply_profile(rec, profile)
        lines.append(json.dumps(mapped, ensure_ascii=False))
    return "\n".join(lines) + ("\n" if lines else "")


def _counts(records: list[dict[str, Any]], included: set[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    grounded = 0
    for r in records:
        if included and r.get("type") not in included:
            continue
        out[r.get("type")] = out.get(r.get("type"), 0) + 1
        if r.get("grounded"):
            grounded += 1
    out["_grounded"] = grounded
    return out


def build_companions(
    records: list[dict[str, Any]],
    profile: dict[str, Any],
    *,
    batch_id: str,
    filters: dict[str, Any],
    generated_at: str,
) -> dict[str, str]:
    included = set(profile.get("record_types") or [])
    counts = _counts(records, included)
    emitted = sum(v for k, v in counts.items() if not k.startswith("_"))

    data_dictionary = f"""# Data Dictionary — {profile['name']}

{profile.get('description', '')}

## Record types in this batch
{chr(10).join(f"- `{t}`: {counts.get(t, 0)} records" for t in (profile.get('record_types') or []))}

## Canonical field reference
- `prompt` — the clinical question/case shown to the model (synthetic or de-identified; no PHI).
- `chosen` / `rejected` — preferred vs non-preferred AI answer (preference records).
- `ideal_answer` — specialist-authored or specialist-revised answer (SFT records).
- `steps` — ordered reasoning steps, each independently labelable (reasoning-trace records).
- `annotator_credential` — board certification + specialty of the credentialed evaluator.
- `grounded` — true when the record carries at least one cited clinical-guideline evidence anchor.
- `confidence` — evaluator's self-rated confidence (low/medium/high).
- `submission_id` — provenance link back to the source evaluation.

## Field mapping applied for this buyer
{json.dumps(profile.get('field_map', {}), indent=2)}
"""

    datasheet = f"""# Datasheet — Asclepius Expert Evaluation Batch `{batch_id}`

_Generated {generated_at}. Format: Datasheets for Datasets (Gebru et al.)._

## Motivation
Expert-evaluated AI answers to medical prompts, produced for AI model training
(reward modeling, SFT, process-reward modeling). First-party, credentialed-
specialist labels.

## Composition
- Records emitted: **{emitted}**
- By type: {", ".join(f"{t}={counts.get(t,0)}" for t in (profile.get('record_types') or [])) or "—"}
- Grounded (guideline-cited) records: **{counts.get('_grounded', 0)}**
- Filters applied: `{json.dumps(filters)}`

## Collection process
Credentialed clinicians evaluated blinded A/B AI answers, revised the better
answer or authored an ideal answer, tagged errors, and optionally captured
step-level reasoning. Every record carries annotator credentials + provenance.

## PHI / privacy
**No PHI.** Prompts are synthetic or de-identified; an automated PHI scan runs
on every submission. Records contain no patient identifiers.

## Recommended uses
Reward-model / DPO preference training, supervised fine-tuning, and process-
reward (step-level) modeling in clinical reasoning.

## Limitations
Single-pod annotator base (specialty-weighted). Inter-annotator agreement is
reported where a double-labeled subset exists; otherwise single-rater.
"""

    quality_report = f"""# Quality Report — batch `{batch_id}`

_Generated {generated_at}._

- Total records emitted: **{emitted}**
- Grounded (evidence-anchored) records: **{counts.get('_grounded', 0)}** ({(100*counts.get('_grounded',0)/emitted):.1f}% of batch){' ' if emitted else ''}
- Record-type breakdown: {", ".join(f"{t}={counts.get(t,0)}" for t in (profile.get('record_types') or [])) or "—"}
- Buyer profile: `{profile['name']}`
- Verification: every record passed auto-validation + the QA gate before reaching export-ready.

> Inter-annotator agreement (Cohen's κ) is computed on the double-labeled
> subset when present; see the admin dashboard for the live figure.
""" if emitted else f"# Quality Report — batch `{batch_id}`\n\nNo records matched the selected filters.\n"

    return {
        "data_dictionary.md": data_dictionary,
        "datasheet.md": datasheet,
        "quality_report.md": quality_report,
    }


def build_export(
    records: list[dict[str, Any]],
    profile_name: str,
    *,
    filters: Optional[dict[str, Any]] = None,
    generated_at: Optional[str] = None,
) -> dict[str, Any]:
    """Return {batch_id, profile, jsonl, files{...}, count}."""
    profile = get_profile(profile_name)
    filters = filters or {}
    generated_at = generated_at or (datetime.utcnow().replace(microsecond=0).isoformat() + "Z")
    batch_id = f"batch-{uuid.uuid4().hex[:8]}"
    jsonl = build_jsonl(records, profile)
    files = build_companions(
        records, profile, batch_id=batch_id, filters=filters, generated_at=generated_at
    )
    count = jsonl.count("\n") if jsonl.strip() else 0
    files["records.jsonl"] = jsonl
    files["batch.json"] = json.dumps(
        {
            "batch_id": batch_id,
            "profile": profile["name"],
            "record_count": count,
            "filters": filters,
            "generated_at": generated_at,
        },
        indent=2,
    )
    return {
        "batch_id": batch_id,
        "profile": profile["name"],
        "count": count,
        "jsonl": jsonl,
        "files": files,
    }
