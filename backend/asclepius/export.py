"""Export & delivery (PRD §5 step 5, §7.5; opt §2, §1.3, §1.4, §1.5).

Builds a buyer-ready delivery batch on local disk under ``ASCLEPIUS_EXPORT_DIR``:
  * ``records.jsonl``        one mapped + schema-validated record per line
  * ``batch.json``           manifest (counts, content hashes, profile, filters)
  * ``data_dictionary.md``   field definitions per record type
  * ``datasheet.md``         Datasheets-for-Datasets-style provenance/credentials
  * ``quality_report.md``    grounded %, Cohen's κ, QA pass rate, flag counts,
                             contributor breakdown

Export is a **field-mapping layer** (``profiles.py``): the internal canonical
record is mapped to the target buyer profile and EVERY emitted line is validated
against that profile's JSON Schema BEFORE writing. Any invalid line fails the
whole batch loudly — no partial silent exports (opt §2). Filters: specialty,
difficulty, record type, date range, grounded tier, confidence floor, min
agreement score, buyer request id (opt §2).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from asclepius import agreement as asc_agreement
from asclepius import credentials as asc_credentials
from asclepius import profiles
from asclepius.constants import (
    ASCLEPIUS_CONFIG_VERSION,
    ASCLEPIUS_TAXONOMY_VERSION,
    KAPPA_THRESHOLD,
)

JSONL_NAME = "records.jsonl"
MANIFEST_NAME = "batch.json"
DICTIONARY_NAME = "data_dictionary.md"
DATASHEET_NAME = "datasheet.md"
QUALITY_NAME = "quality_report.md"
# Grader export (FEAT-2): shipped alongside the data when the batch carries rubric
# records, so a buyer can run rubric-based LLM-as-judge scoring out of the box.
GRADER_PROMPT_NAME = "grader_prompt.txt"
SCORE_PY_NAME = "score.py"

_COMPANION_FILES = [JSONL_NAME, MANIFEST_NAME, DICTIONARY_NAME, DATASHEET_NAME, QUALITY_NAME]

_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


class ExportValidationError(ValueError):
    """A mapped line failed the target profile's JSON Schema — the batch is
    rejected wholesale (opt §2: no partial silent exports)."""


def export_root() -> Path:
    root = Path(os.getenv("ASCLEPIUS_EXPORT_DIR") or "/tmp/asclepius-exports").resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _new_export_id() -> str:
    return "exp-" + datetime.utcnow().strftime("%Y%m%d-%H%M%S-%f")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _rec_modality(rec: Dict[str, Any]) -> str:
    """Record modality (Synthetic Multimodal Cases PRD §5, §8): 'multimodal' when
    the record carries a structured case, else 'text'. Stamped into
    ``payload.context.modality`` by packaging for multimodal tasks only, so every
    legacy/text record reads as 'text'."""
    return ((rec.get("payload") or {}).get("context") or {}).get("modality") or "text"


def _rec_case_source(rec: Dict[str, Any]) -> Optional[str]:
    """Provenance of a multimodal record's case: 'synthetic' or 'real_deid'
    (PRD §5). None for text records."""
    return ((rec.get("payload") or {}).get("context") or {}).get("case_source")


def _case_answer_key(store: Any, rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The held-out answer key for a multimodal record's case (Multimodal PRD §7).
    Read from the SERVER-SIDE task (never the buyer-facing public case, which has
    it stripped), so a benchmark buyer who opts in gets the ground truth while the
    default export keeps it withheld. None for text records or if unavailable."""
    if _rec_modality(rec) != "multimodal":
        return None
    tid = rec.get("task_id") or (rec.get("payload") or {}).get("task_id")
    if not tid:
        return None
    task = store.get_task(tid) or {}
    gt = ((task.get("case") or {}) or {}).get("ground_truth")
    return gt or None


def _counts(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_type: Dict[str, int] = {}
    by_specialty: Dict[str, int] = {}
    by_portal_version: Dict[str, int] = {}
    by_modality: Dict[str, int] = {}
    by_case_source: Dict[str, int] = {}
    for r in records:
        by_type[r["type"]] = by_type.get(r["type"], 0) + 1
        sp = r.get("specialty") or "unknown"
        by_specialty[sp] = by_specialty.get(sp, 0) + 1
        # V1 (classic) vs V2 (assisted) breakdown (Asclepius V2). Legacy records
        # with no stamp are counted as v1 (they predate the assisted flow).
        pv = (r.get("payload") or {}).get("portal_version") or "v1"
        by_portal_version[pv] = by_portal_version.get(pv, 0) + 1
        # Text vs multimodal (structured-case) breakdown (Multimodal PRD §8).
        mod = _rec_modality(r)
        by_modality[mod] = by_modality.get(mod, 0) + 1
        cs = _rec_case_source(r)
        if cs:
            by_case_source[cs] = by_case_source.get(cs, 0) + 1
    return {
        "by_type": by_type,
        "by_specialty": by_specialty,
        "by_portal_version": by_portal_version,
        "by_modality": by_modality,
        "by_case_source": by_case_source,
        "total": len(records),
    }


# ─── Filtering (opt §2) ───────────────────────────────────────────────────────
def _passes_filters(
    rec: Dict[str, Any],
    *,
    difficulty: Optional[str],
    grounded_only: bool,
    confidence_floor: Optional[str],
    min_agreement: Optional[float],
    buyer_request_id: Optional[str],
    annotator_ids: Optional[set],
    portal_version: Optional[str] = None,
    modality: Optional[str] = None,
    case_source: Optional[str] = None,
    submission_id: Optional[str] = None,
) -> bool:
    payload = rec.get("payload") or {}
    # Single-task scoping (Exports rework): export exactly one submission's
    # records. The submission id is a top-level record column (and mirrored into
    # the payload at packaging time) — accept either.
    if submission_id and rec.get("submission_id") != submission_id and payload.get("submission_id") != submission_id:
        return False
    if difficulty and (payload.get("context") or {}).get("difficulty") != difficulty:
        return False
    # V1/V2 cohort filter (Asclepius V2): ship or analyze one product version at
    # a time. Unstamped legacy records count as v1.
    if portal_version and (payload.get("portal_version") or "v1") != portal_version:
        return False
    # Text vs multimodal cohort filter (Multimodal PRD §8): package a text-only or
    # a structured-multimodal batch. Legacy/text records read as 'text'.
    if modality and _rec_modality(rec) != modality:
        return False
    # Case provenance filter (synthetic vs real_deid). Only meaningful for
    # multimodal records; a text record has no case_source and is excluded.
    if case_source and _rec_case_source(rec) != case_source:
        return False
    if grounded_only and not bool(payload.get("grounded")):
        return False
    if confidence_floor:
        floor = _CONFIDENCE_RANK.get(confidence_floor, 0)
        have = _CONFIDENCE_RANK.get(payload.get("confidence") or "", -1)
        if have < floor:
            return False
    if min_agreement is not None:
        score = payload.get("agreement_score")
        if score is None or score < min_agreement:
            return False
    if buyer_request_id and payload.get("buyer_request_id") != buyer_request_id:
        return False
    # Contributor / organization scoping: only records this annotator (or set of
    # annotators in an org) labeled. Keyed on the hashed annotator id stamped onto
    # every record at packaging time.
    if annotator_ids is not None and payload.get("annotator_id_hashed") not in annotator_ids:
        return False
    return True


# ─── Companions ───────────────────────────────────────────────────────────────
def _data_dictionary_md(profile_name: str) -> str:
    return f"""# Asclepius Export — Data Dictionary

Buyer profile: `{profile_name}` · Taxonomy version: `{ASCLEPIUS_TAXONOMY_VERSION}` · Config version: `{ASCLEPIUS_CONFIG_VERSION}`

Each line in `{JSONL_NAME}` is one JSON record mapped to the target buyer profile.
The `type` field selects the schema. Canonical fields (pre-mapping) below.

## type = "preference" (hh-rlhf reward models / RLHF / DPO)
| field | meaning |
| --- | --- |
| `prompt` | the clinical question / case (flat variant) |
| `chosen` | better answer — string (flat) or messages array (chat variant) |
| `rejected` | worse answer — string (flat) or messages array (chat variant) |
| `rationale` | free-text reason the chosen answer is better |
| `evidence_anchor` | `{{citation_text, source_type, identifier}}` grounding the rationale |
| `why_better_tags` | structured tags: more_accurate, safer, better_reasoning, clearer, better_dosing |
| `error_tags_on_rejected` | error taxonomy tags applied to the rejected answer |
| `error_tag_anchors` | optional `{{error_tag: evidence_anchor}}` |
| `error_severities` | optional per-tag severity (low/medium/high) |
| `error_tag_reasons` | optional structured `{{error_tag: reason}}` from a controlled vocabulary (dose_too_high, contraindicated, …) |
| `stance` | the evaluator's pre-reveal quick take (anchoring guard) — context signal, NOT a gold completion; null on full-blind-answer tasks |
| `assist` | model-assist provenance `{{prelabeled, suggested_verdict, suggested_error_tags, suggested_rationale, suggested_step_labels, confidence}}` — suggestions shown to the annotator, stored next to the human finals for override-rate analysis; null when unassisted |
| `confidence` | annotator confidence: low/medium/high |
| `grounded` | true when the rationale carries a valid evidence anchor (premium tier) |
| `agreement_score` | inter-annotator agreement (null if single-labeled) |

## type = "ideal_answer" (SFT / instruction tuning)
| field | meaning |
| --- | --- |
| `prompt` | the clinical question / case |
| `completion` | specialist ideal/revised answer (alias of `ideal_answer`; instruction/response on some profiles) |
| `approach_notes` | how the specialist reasoned / why it is correct |
| `independent` | true when written blind, BEFORE the A/B answers were revealed (uncontaminated premium SFT) |
| `stance` | pre-reveal quick take (see preference) — never present together with `independent` |
| `evidence_anchor` | optional grounding citation |

## type = "reasoning_trace" (PRM800K process reward model)
| field | meaning |
| --- | --- |
| `prompt` | the clinical question / case |
| `steps` | ordered `[{{step, text, label, suggested_label, step_reward, evidence_anchor}}]`; `label` ∈ good/neutral/bad is the HUMAN action; `suggested_label` is the model pre-grade shown to the annotator (null when unassisted) |
| `final_answer` | the resulting answer |

## Provenance & rights (every record)
| field | meaning |
| --- | --- |
| `annotator_credential` | e.g. board_certified_nephrology — the premium signal |
| `annotator_specialty` / `annotator_years_experience` | annotator credential metadata |
| `annotator_id_hashed` | stable hashed annotator id (no PII) |
| `submission_id` / `task_id` | lineage |
| `source` | `lab_supplied` vs `internal_prompt_bank` |
| `buyer_request_id` | the buyer request the record answers (opt §2.5) |
| `taxonomy_version` / `config_version` | versioning |
| `portal_version` | evaluator product flow that produced the record: `v1` (classic) or `v2` (assisted). Stage-1 prompt review + record types are identical across both; V2 adds quick-stance capture, model-assist provenance, and structured reasons |
| `license` / `ip_cleared` / `contains_phi` | rights attestation (opt §1.4) |
| `captured_at` | submission capture timestamp |
"""


def _synthetic_records(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        r for r in records
        if (r.get("payload") or {}).get("source") == "internal_prompt_bank"
        or (r.get("payload") or {}).get("generation")
    ]


def _seed_corpus_ratified(records: List[Dict[str, Any]]) -> Optional[bool]:
    """Tri-state ratification of the synthetic prompts in this batch:
    ``True`` (all synthetic records came from a clinician-ratified corpus),
    ``False`` (at least one did not), or ``None`` (no synthetic records)."""
    synthetic = _synthetic_records(records)
    if not synthetic:
        return None
    return all(
        bool(((r.get("payload") or {}).get("generation") or {}).get("seed_corpus_ratified"))
        for r in synthetic
    )


def _prompts_clinician_reviewed(records: List[Dict[str, Any]]) -> bool:
    """True when every record in the set carries ``prompt_clinician_reviewed`` —
    i.e. the prompt was signed off as clinically valid at evaluation time (Eval
    Flow Upgrade §2). False on an empty set so it never upgrades a no-op batch."""
    if not records:
        return False
    return all(
        bool((r.get("payload") or {}).get("prompt_clinician_reviewed")) for r in records
    )


def _synthetic_provenance_md(records: List[Dict[str, Any]]) -> str:
    """A buyer-facing note when any prompts were auto-generated (PRD §9.1)."""
    synthetic = _synthetic_records(records)
    if not synthetic:
        return ""
    versions = sorted({
        ((r.get("payload") or {}).get("generation") or {}).get("seed_corpus_version")
        for r in synthetic
        if ((r.get("payload") or {}).get("generation") or {}).get("seed_corpus_version")
    })
    ratified = _seed_corpus_ratified(records)
    reviewed = _prompts_clinician_reviewed(synthetic)
    if ratified:
        ratify_line = (
            "- The seed corpus driving generation is **clinician-ratified**."
        )
    elif reviewed:
        # Eval Flow Upgrade §2: even when the seed corpus is not batch-ratified, a
        # credentialed specialist reviewed and accepted each prompt as clinically
        # valid at evaluation time — a real provenance upgrade.
        ratify_line = (
            "- ✅ **Every prompt was clinician-reviewed at evaluation** "
            "(`prompt_clinician_reviewed: true`): a credentialed specialist signed "
            "off on the prompt as clinically valid before answering it. Prompts the "
            "specialist judged invalid were flagged and excluded from this dataset."
        )
    else:
        ratify_line = (
            "- ⚠️ **The seed corpus driving generation is NOT yet clinician-ratified** "
            "(`seed_corpus_ratified: false`). These prompts are AI-drafted and pending "
            "nephrologist sign-off; treat the prompt material as provisional. The expert "
            "training signal is the specialist's chosen/ideal answer and revision — not "
            "the synthetic prompt itself."
        )
    return f"""

## Synthetic prompt provenance (Seedmaker)
- **{len(synthetic)}/{len(records)}** records derive from internally auto-generated
  prompts (`source: internal_prompt_bank`), not lab-supplied content.
- Prompts were synthesized by the Asclepius Seedmaker engine, grounded in a
  curated nephrology seed corpus{(' (versions: ' + ', '.join(versions) + ')') if versions else ''},
  then novelty-/contamination-checked and passed an error-likelihood quality gate
  before any specialist evaluated them.
{ratify_line}
- The AI generated only the prompt and two candidate answers (the material to be
  judged); all grounding/evidence anchors and the chosen/ideal answers are the
  credentialed specialist's work. Generated prompts are never auto-marked grounded."""


def _scope_section_md(scope: Optional[Dict[str, Any]]) -> str:
    """An auto-generated aggregate credential line for contributor/organization-
    scoped exports (spec §5), e.g. "All records labeled by an NPI-verified, board-
    certified, fellowship-trained nephrologist (~17 yrs, active practice)." Derived
    from Tier A only — never identifying."""
    if not scope:
        return ""
    label = scope.get("label") or scope.get("type") or "scope"
    blurb = (scope.get("blurb") or "").strip()
    lines = [f"\n## Contributor scope\n- Scope: **{scope.get('type', 'contributor')}** — {label}"]
    if scope.get("type") == "contributor" and blurb:
        lines.append(f"- All records in this batch labeled by: {blurb}")
    elif scope.get("type") == "organization":
        n = scope.get("contributor_count")
        lines.append(
            f"- All records in this batch labeled by credentialed contributors at "
            f"**{label}**" + (f" ({n} contributor(s))" if n else "") + "."
        )
    lines.append(
        "- Identifying credentials are withheld from this batch by design and are "
        "available only via a Further Credential Summary under NDA / non-circumvention, "
        "matched by `annotator_id_hashed`."
    )
    return "\n".join(lines)


def _stance_semantics_md(records: List[Dict[str, Any]]) -> str:
    """Datasheet copy for quick-stance captures (Speed Optimization §1) — only
    emitted when the batch actually carries stance-mode records."""
    if not any((r.get("payload") or {}).get("stance") for r in records):
        return ""
    return (
        "\nIndependent stance captured pre-reveal (anchoring guard); the gold "
        "answer is the specialist-refined chosen answer. A record's `stance` "
        "field is the evaluator's blind quick take, not a gold completion."
    )


def _multimodal_section_md(records: List[Dict[str, Any]], counts: Dict[str, Any]) -> str:
    """Datasheet copy for structured-multimodal cases (Multimodal PRD §5, §8) —
    only emitted when the batch actually carries multimodal records. Reports the
    modality mix, case provenance, the no-imaging/PHI-free contract, and whether
    the held-out answer key is bundled."""
    mm = [r for r in records if _rec_modality(r) == "multimodal"]
    if not mm:
        return ""
    by_source = counts.get("by_case_source") or {}
    source_lines = ", ".join(f"{k} — {v}" for k, v in sorted(by_source.items())) or "n/a"
    return f"""
## Multimodal cases (structured clinical cases)
- **{len(mm)}/{counts['total']}** records are structured-multimodal: each carries a
  PHI-free clinical case (demographics as age bands, lab panels with reference
  ranges + flags, free-text notes, meds/problems/vitals) alongside the question.
- Case provenance: {source_lines}. `synthetic` cases are AI-authored against a
  clinician-curated archetype and PHI-scanned; `real_deid` cases are de-identified
  from real encounters (Safe Harbor).
- **No imaging.** Cases are text + structured tabular data only; there are no
  DICOM/image modalities in this dataset.
- Lab timing is relative (`collected_offset_days` from an index event), never
  calendar dates — a further de-identification guard.
- Held-out answer key: the case's ground truth is **withheld** from the
  buyer-facing record by default; it ships (under `answer_key`) only for explicit
  benchmark exports.
"""


def _datasheet_md(*, export_id: str, profile_name: str, counts: Dict[str, Any],
                  records: List[Dict[str, Any]], contributors: List[Dict[str, Any]],
                  scope: Optional[Dict[str, Any]] = None) -> str:
    credentials = sorted({(r.get("payload") or {}).get("annotator_credential") or "unspecified" for r in records})
    specialties = sorted(counts["by_specialty"].keys())
    type_lines = "\n".join(f"- `{k}`: {v}" for k, v in sorted(counts["by_type"].items()))
    contrib_lines = "\n".join(
        f"- {c.get('credential')} ({c.get('specialty') or 'n/a'}): "
        f"{c.get('submissions')} submissions, {c.get('total_hours')}h"
        for c in contributors
    ) or "- n/a"
    return f"""# Datasheet — Asclepius Expert Evaluation Export `{export_id}`

Generated: {datetime.utcnow().isoformat()}Z · Buyer profile: `{profile_name}`

## Motivation
Credentialed-specialist judgments comparing AI-generated answers to medical
prompts, packaged as hh-rlhf preference pairs, {{prompt, completion}} SFT
examples, and PRM800K-style step-level reasoning traces for frontier-lab training.

## Composition
- Total records: **{counts['total']}**
{type_lines}
- Specialties: {", ".join(specialties) or "n/a"}
- By product version: {", ".join(f"{k} — {v}" for k, v in sorted(counts.get('by_portal_version', {}).items())) or "n/a"} (V1 classic · V2 assisted · V3 seamless synthetic · **V4 REAL de-identified cases**)
- By modality: {", ".join(f"{k} — {v}" for k, v in sorted(counts.get('by_modality', {}).items())) or "n/a"} (text vs structured-multimodal case)
{_scope_section_md(scope)}
{_multimodal_section_md(records, counts)}
{_synthetic_provenance_md(records)}

## Collection process
Answers were evaluated in the Asclepius portal. Each submission was
auto-packaged, schema-validated (completeness, time-floor, PHI scan, dedupe,
contamination), double-checked by an LLM consistency critic, and gated through
human QA (sampled + all flagged) before becoming export-ready.
{_stance_semantics_md(records)}

## Annotator credentials (aggregate)
{chr(10).join("- " + c for c in credentials)}

### Contributor breakdown
{contrib_lines}

## Preprocessing
Field mapping to the buyer profile + per-line JSON-Schema validation. No record
is emitted unless it validates against the target schema.

## Recommended uses
Training / evaluating medical LLMs (reward modeling, SFT, process supervision).

## Limitations
- Evaluation artifacts, not medical advice; not for direct clinical use.
- Synthetic / de-identified prompts; no PHI (scanned defensively).
- Agreement reported as Cohen's κ on a double-labeled subset; single-labeled
  records carry no agreement score.

## Rights & privacy
- `contains_phi: false` (asserted + residual-identifier scanned).
- `ip_cleared: true`; `license` stamped on every record.
"""


def _multimodal_quality_md(records: List[Dict[str, Any]], counts: Dict[str, Any]) -> str:
    """Quality-report block for the multimodal case judge (Multimodal PRD §5): the
    mean case-judge dimensions over the shipped multimodal records, so a buyer can
    see the structured cases cleared the coherence / multimodal-necessity /
    ground-truth-determinable / reasoning-divergence gates. Empty when the batch
    has no multimodal records."""
    mm = [r for r in records if _rec_modality(r) == "multimodal"]
    if not mm:
        return ""
    dims = ("coherence", "multimodal_necessity", "ground_truth_determinable", "reasoning_divergence_potential")
    sums: Dict[str, float] = {d: 0.0 for d in dims}
    n: Dict[str, int] = {d: 0 for d in dims}
    for r in mm:
        cj = ((r.get("payload") or {}).get("generation") or {}).get("case_judge") or {}
        for d in dims:
            v = cj.get(d)
            if isinstance(v, (int, float)):
                sums[d] += float(v)
                n[d] += 1
    dim_lines = "\n".join(
        f"- {d}: {round(sums[d] / n[d], 3) if n[d] else 'n/a'} (n={n[d]})" for d in dims
    )
    by_source = counts.get("by_case_source") or {}
    source_lines = "\n".join(f"- {k}: {v}" for k, v in sorted(by_source.items())) or "- n/a"
    return f"""
## Multimodal cases (structured-case judge)
- Multimodal records: **{len(mm)}/{counts['total']}**
- Case provenance:
{source_lines}
- Mean case-judge dimensions (every shipped case cleared the generation-time floors):
{dim_lines}
"""


def _quality_report_md(*, export_id: str, profile_name: str, records: List[Dict[str, Any]],
                       stats: Dict[str, Any]) -> str:
    counts = _counts(records)
    grounded = sum(1 for r in records if (r.get("payload") or {}).get("grounded"))
    grounded_pct = round(100 * grounded / counts["total"], 1) if counts["total"] else 0.0
    agreement_vals = [
        (r.get("payload") or {}).get("agreement_score")
        for r in records
        if (r.get("payload") or {}).get("agreement_score") is not None
    ]
    avg_agreement = round(sum(agreement_vals) / len(agreement_vals), 3) if agreement_vals else None
    conf: Dict[str, int] = {}
    for r in records:
        c = (r.get("payload") or {}).get("confidence") or "n/a"
        conf[c] = conf.get(c, 0) + 1
    type_lines = "\n".join(f"- `{k}`: {v}" for k, v in sorted(counts["by_type"].items()))
    portal_lines = "\n".join(
        f"- {k} ({dict(v1='classic', v2='assisted', v3='seamless synthetic', v4='REAL de-identified cases').get(k, 'assisted')}): {v}"
        for k, v in sorted(counts.get("by_portal_version", {}).items())
    ) or "- n/a"
    conf_lines = "\n".join(f"- {k}: {v}" for k, v in sorted(conf.items()))
    mm_section = _multimodal_quality_md(records, counts)
    qa = stats.get("qa_pass_rate") or {}
    kappa = stats.get("kappa") or {}
    by_spec = kappa.get("by_specialty") or {}
    kappa_spec_lines = "\n".join(f"- {sp}: {v}" for sp, v in sorted(by_spec.items())) or "- n/a"
    flags = stats.get("flag_counts") or {}
    contributors = stats.get("contributors") or []
    contrib_lines = "\n".join(
        f"- {c.get('credential')} ({c.get('specialty') or 'n/a'}): "
        f"{c.get('submissions')} subs, {c.get('total_hours')}h, "
        f"premium {c.get('premium_submissions')} ({c.get('premium_hours')}h)"
        for c in contributors
    ) or "- n/a"
    return f"""# Quality Report — Asclepius Export `{export_id}`

Generated: {datetime.utcnow().isoformat()}Z · Buyer profile: `{profile_name}`

## Totals by record type
- Total records: **{counts['total']}**
{type_lines}

## By product version (V1 classic / V2 assisted)
{portal_lines}
{mm_section}
## Grounded (evidence-anchored) premium tier
- Grounded records: **{grounded}/{counts['total']}** (**{grounded_pct}%**)

## Inter-annotator agreement (Cohen's κ, opt §1.3)
- Aggregate κ (double-labeled subset, n={kappa.get('n')}): **{kappa.get('overall')}**
- Observed agreement: {kappa.get('observed_agreement')}
- κ threshold for substantial agreement: {KAPPA_THRESHOLD}
- By specialty:
{kappa_spec_lines}

## Confidence distribution
{conf_lines}

## QA & integrity flags
- QA pass rate (export-ready / reviewed): **{qa.get('pass_rate')}** ({qa.get('passed')}/{qa.get('reviewed')})
- Average agreement (this batch): {avg_agreement if avg_agreement is not None else "n/a"}
- Too-fast (time-floor) flags: {flags.get('too_fast', 0)}
- Duplicate flags: {flags.get('duplicate', 0)}
- Contamination flags: {flags.get('contamination', 0)}
- PHI flags: {flags.get('phi', 0)}

## Contributor breakdown (credential mix, hours, counts)
{contrib_lines}

Taxonomy version: `{ASCLEPIUS_TAXONOMY_VERSION}` · Config version: `{ASCLEPIUS_CONFIG_VERSION}`
"""


def _flag_counts(store: Any) -> Dict[str, int]:
    # TODO(scale): full-table scan; fine at pod scale. Aggregate via SQL or a
    # rollup table if submission volume grows large.
    counts = {"too_fast": 0, "duplicate": 0, "contamination": 0, "phi": 0}
    for s in store.list_submissions(limit=100000):
        val = s.get("validation") or {}
        for issue in val.get("issues") or []:
            if issue == "too_fast":
                counts["too_fast"] += 1
            elif issue == "duplicate":
                counts["duplicate"] += 1
            elif issue.startswith("contamination"):
                counts["contamination"] += 1
            elif issue.startswith("phi"):
                counts["phi"] += 1
    return counts


_GRADER_PROMPT = """You are grading a candidate clinical answer against a set of \
PHYSICIAN-AUTHORED, weighted rubric criteria (HealthBench-shaped). Each criterion has:
  - text:   what a correct answer must include (positive points) or must never say (negative points)
  - points: signed weight — award POSITIVE points if the answer satisfies a positive criterion; \
subtract (award the negative) if the answer commits a negative criterion
  - axis:   accuracy | completeness | safety | reasoning | grounding | communication

Rules:
- Judge ONLY against the listed criteria; do not invent criteria or use outside preferences.
- A positive criterion is met only if the answer clearly satisfies it. A negative criterion is \
triggered only if the answer clearly commits it.
- Be conservative and cite the exact span of the answer that satisfies/violates each criterion.

Return ONLY JSON:
{
  "per_criterion": [ {"text": "<criterion text>", "points": <signed>, "met": true|false, "awarded": <points if met else 0>, "evidence": "<span or ''>"} ],
  "score": <sum of awarded>,
  "max_points": <sum of positive criterion points>,
  "normalized": <score / max_points, 0..1>
}
"""

_SCORE_PY = '''#!/usr/bin/env python3
"""Rubric-based LLM-as-judge scorer for an Asclepius export (FEAT-2).

Reads the rubric records from ``records.jsonl`` and scores a candidate answer
against each rubric\'s weighted criteria using an LLM judge with ``grader_prompt.txt``.

Usage:
    export ANTHROPIC_API_KEY=...           # or OPENAI_API_KEY with --provider openai
    python score.py --answer "the candidate answer text" [--task-id T] [--provider anthropic]
    python score.py --answers-file answers.jsonl   # {"task_id":..., "answer":...} per line

With no API key it prints the rubric(s) it WOULD score so the pipeline is inspectable offline.
This file is a runnable scaffold — adapt the model id / provider to your stack.
"""
import argparse, json, os, sys, pathlib

HERE = pathlib.Path(__file__).parent
PROMPT = (HERE / "grader_prompt.txt").read_text(encoding="utf-8")


def load_rubrics():
    rubrics = []
    with open(HERE / "records.jsonl", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("type") == "rubric":
                rubrics.append(rec)
    return rubrics


def grade(answer, rubric, provider="anthropic"):
    user = ("PROMPT:\\n" + (rubric.get("prompt") or "") + "\\n\\nRUBRIC CRITERIA:\\n"
            + json.dumps(rubric.get("criteria") or [], indent=2)
            + "\\n\\nCANDIDATE ANSWER:\\n" + answer)
    key = os.getenv("ANTHROPIC_API_KEY") if provider == "anthropic" else os.getenv("OPENAI_API_KEY")
    if not key:
        return {"skipped": "no_api_key", "max_points": rubric.get("max_points"),
                "criteria": rubric.get("criteria")}
    # Choose your judge model via GRADER_MODEL (this scaffold is model-agnostic on
    # purpose — pick the frontier model your team scores with).
    grader_model = os.getenv("GRADER_MODEL")
    if not grader_model:
        raise SystemExit("Set GRADER_MODEL to the judge model id for your provider "
                         "(e.g. a current Anthropic or OpenAI model).")
    if provider == "anthropic":
        import anthropic  # pip install anthropic
        client = anthropic.Anthropic(api_key=key)
        resp = client.messages.create(model=grader_model,
                                       max_tokens=1500, system=PROMPT,
                                       messages=[{"role": "user", "content": user}])
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    else:
        from openai import OpenAI  # pip install openai
        client = OpenAI(api_key=key)
        resp = client.chat.completions.create(model=grader_model,
                                              messages=[{"role": "system", "content": PROMPT},
                                                        {"role": "user", "content": user}])
        text = resp.choices[0].message.content
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1]) if start != -1 else {"raw": text}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--answer")
    ap.add_argument("--answers-file")
    ap.add_argument("--task-id")
    ap.add_argument("--provider", default="anthropic", choices=["anthropic", "openai"])
    args = ap.parse_args()
    rubrics = load_rubrics()
    if args.task_id:
        rubrics = [r for r in rubrics if (r.get("task_id") or r.get("prompt")) == args.task_id]
    if not rubrics:
        print("No rubric records found in records.jsonl", file=sys.stderr)
        return
    if args.answers_file:
        answers = [json.loads(l) for l in open(args.answers_file) if l.strip()]
    elif args.answer is not None:
        answers = [{"answer": args.answer}]
    else:
        # No answer given: print the rubrics so the buyer can see the scoring function.
        print(json.dumps(rubrics, indent=2)); return
    for a in answers:
        for r in rubrics:
            print(json.dumps({"task_id": r.get("task_id"),
                              "result": grade(a["answer"], r, provider=args.provider)}, indent=2))


if __name__ == "__main__":
    main()
'''


def build_export(
    store: Any,
    *,
    created_by: Optional[str],
    profile: str = "default",
    specialty: Optional[str] = None,
    difficulty: Optional[str] = None,
    record_type: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    grounded_only: bool = False,
    confidence_floor: Optional[str] = None,
    min_agreement: Optional[float] = None,
    buyer_request_id: Optional[str] = None,
    portal_version: Optional[str] = None,
    modality: Optional[str] = None,
    case_source: Optional[str] = None,
    include_answer_key: bool = False,
    include_mock: bool = False,
    note: Optional[str] = None,
    include_exported: bool = False,
    annotator_id_hashed: Optional[str] = None,
    annotator_ids: Optional[List[str]] = None,
    verify_values: Optional[List[str]] = None,
    scope: Optional[Dict[str, Any]] = None,
    submission_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble + persist an export batch from export-ready records.

    Maps every record through the buyer profile, validates each line against the
    profile schema (failing the whole batch on any invalid line), runs the Tier B
    leak gate on every line, writes the JSONL + companions + manifest, marks the
    records ``exported``, and logs a provenance event. Raises ``ValueError`` when
    nothing matches the filters and ``ExportValidationError`` when a mapped line
    fails its schema OR carries any Tier B (identifying) field.

    ``annotator_id_hashed`` scopes the batch to one contributor's records;
    ``annotator_ids`` scopes it to a set (e.g. every contributor in an
    organization). ``verify_values`` enables a defense-in-depth value scan against
    the relevant private-vault values.

    ``include_exported`` re-includes already-shipped records so an admin can
    re-package / re-download a fresh bundle of everything (records stay in the DB
    permanently; export is non-destructive).

    ``modality`` / ``case_source`` scope the batch to text-only or
    structured-multimodal records (and by case provenance). ``include_answer_key``
    (default OFF, Multimodal PRD §7) attaches each multimodal case's held-out
    answer key under a non-forbidden ``answer_key`` field for *benchmark* buyers —
    the raw ``ground_truth`` key stays forbidden by the leak gate, so the answer
    key never ships by accident; it ships only on explicit opt-in.

    ``include_mock`` (default OFF) is the ONLY way mock/sandbox-contributor records
    enter a batch. By default every record whose annotator is a mock contributor is
    hard-excluded, so a demo on the live portal never contaminates a shipped
    training batch (internal demo tool)."""
    prof = profiles.load_profile(profile)
    profile_name = prof.get("name") or profile

    # Mock/sandbox contributor isolation: hard-exclude their records unless an admin
    # explicitly opts in. Computed once (empty set when including mock or none exist).
    mock_ids: set = set()
    if not include_mock:
        try:
            mock_ids = store.mock_annotator_id_hashes()
        except Exception:
            mock_ids = set()

    annotator_id_set: Optional[set] = None
    if annotator_id_hashed:
        annotator_id_set = {annotator_id_hashed}
    elif annotator_ids is not None:
        annotator_id_set = set(annotator_ids)

    candidates = store.list_records(
        status="export_ready",
        rtype=record_type,
        specialty=specialty,
        since=since,
        until=until,
    )
    if include_exported:
        candidates = candidates + store.list_records(
            status="exported",
            rtype=record_type,
            specialty=specialty,
            since=since,
            until=until,
        )
    records = [
        r
        for r in candidates
        if (not mock_ids or (r.get("payload") or {}).get("annotator_id_hashed") not in mock_ids)
        and _passes_filters(
            r,
            difficulty=difficulty,
            grounded_only=grounded_only,
            confidence_floor=confidence_floor,
            min_agreement=min_agreement,
            buyer_request_id=buyer_request_id,
            annotator_ids=annotator_id_set,
            portal_version=portal_version,
            modality=modality,
            case_source=case_source,
            submission_id=submission_id,
        )
    ]
    if not records:
        raise ValueError("No export-ready records match the selected filters.")

    export_id = _new_export_id()
    exported_at = datetime.utcnow().isoformat()

    # 1. Map + validate EVERY line before writing anything (fail loud, fail whole).
    lines: List[str] = []
    emitted: List[Dict[str, Any]] = []
    for rec in records:
        payload = dict(rec.get("payload") or {})
        payload.pop("record_id", None)
        payload["exported_at"] = exported_at
        rtype = payload.get("type") or rec.get("type")
        mapped = profiles.map_record(prof, payload)
        if mapped is None:
            # Record type not emitted by this profile — skip it.
            continue
        schema = profiles.schema_for(prof, rtype)
        if schema:
            errs = profiles.validate_against_schema(mapped, schema)
            if errs:
                raise ExportValidationError(
                    f"Record {rec.get('record_id')} ({rtype}) failed profile "
                    f"{profile_name!r} schema: {errs[0]}"
                )
        # Benchmark opt-in (Multimodal PRD §7): attach the case's held-out answer
        # key under ``answer_key`` (NOT the forbidden raw ``ground_truth`` key), so
        # a benchmark buyer can score models. Added after schema validation but
        # BEFORE the leak gate, so the gate still scans it for stray Tier B keys.
        if include_answer_key:
            ak = _case_answer_key(store, rec)
            if ak:
                mapped["answer_key"] = ak
        # THE CORE RULE (spec §4, §5): buyer-facing records carry credential
        # ATTRIBUTES only. Reject the whole batch loudly if ANY Tier B
        # (identifying / locating) field appears in ANY record.
        leak = asc_credentials.find_tier_b_leak(mapped)
        if leak is not None:
            raise ExportValidationError(
                f"Tier B leak: record {rec.get('record_id')} ({rtype}) contains the "
                f"identifying field {leak!r}, which must never ship in an Export Data "
                f"batch. Tier B credentials are released only via Further Credential "
                f"Summary. Batch rejected."
            )
        if verify_values:
            vleak = asc_credentials.find_tier_b_value_leak(mapped, verify_values)
            if vleak is not None:
                raise ExportValidationError(
                    f"Tier B value leak: record {rec.get('record_id')} ({rtype}) "
                    f"contains a private-vault value ({vleak!r}). Batch rejected."
                )
        lines.append(json.dumps(mapped, ensure_ascii=False, sort_keys=True))
        emitted.append(rec)

    if not emitted:
        raise ValueError(
            f"No records match the buyer profile {profile_name!r} record types."
        )

    out_dir = export_root() / export_id
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    # 2. JSONL
    jsonl_text = "".join(line + "\n" for line in lines)
    jsonl_path = out_dir / JSONL_NAME
    jsonl_path.write_text(jsonl_text, encoding="utf-8")

    # 3. stats for the quality report
    contributors = store.contributor_stats()
    kappa = asc_agreement.aggregate_kappa(store.list_agreement_observations())
    stats = {
        "status_counts": store.status_counts(),
        "qa_pass_rate": store.qa_pass_rate(),
        "average_agreement": store.average_agreement(),
        "kappa": kappa,
        "flag_counts": _flag_counts(store),
        "contributors": contributors,
    }
    counts = _counts(emitted)

    # 4. companions
    (out_dir / DICTIONARY_NAME).write_text(_data_dictionary_md(profile_name), encoding="utf-8")
    (out_dir / DATASHEET_NAME).write_text(
        _datasheet_md(
            export_id=export_id, profile_name=profile_name, counts=counts,
            records=emitted, contributors=contributors, scope=scope,
        ),
        encoding="utf-8",
    )
    (out_dir / QUALITY_NAME).write_text(
        _quality_report_md(export_id=export_id, profile_name=profile_name, records=emitted, stats=stats),
        encoding="utf-8",
    )

    # Grader export (FEAT-2): when the batch carries rubric records, ship a
    # ready-to-run rubric-based LLM-as-judge scorer (grader_prompt.txt + score.py)
    # — the "eval alongside dataset" a buyer can run out of the box.
    companion_files = list(_COMPANION_FILES)
    if any(r.get("type") == "rubric" for r in emitted):
        (out_dir / GRADER_PROMPT_NAME).write_text(_GRADER_PROMPT, encoding="utf-8")
        (out_dir / SCORE_PY_NAME).write_text(_SCORE_PY, encoding="utf-8")
        companion_files += [GRADER_PROMPT_NAME, SCORE_PY_NAME]

    # 5. manifest with content hashes (opt §1.4, §5)
    filters = {
        "profile": profile_name,
        "specialty": specialty,
        "difficulty": difficulty,
        "record_type": record_type,
        "since": since,
        "until": until,
        "grounded_only": grounded_only,
        "confidence_floor": confidence_floor,
        "min_agreement": min_agreement,
        "buyer_request_id": buyer_request_id,
        "portal_version": portal_version,
        "modality": modality,
        "case_source": case_source,
        "include_answer_key": include_answer_key,
        # Mock/sandbox records are hard-excluded unless explicitly included.
        "include_mock": include_mock,
        "mock_excluded": (not include_mock and bool(mock_ids)),
        "annotator_id_hashed": annotator_id_hashed,
        "annotator_ids": sorted(annotator_id_set) if annotator_id_set else None,
    }
    content_hashes = {JSONL_NAME: _sha256_text(jsonl_text)}
    for name in companion_files:
        if name in (JSONL_NAME, MANIFEST_NAME):
            continue
        content_hashes[name] = _sha256_text((out_dir / name).read_text(encoding="utf-8"))
    manifest = {
        "export_id": export_id,
        "created_at": exported_at,
        "created_by": created_by,
        "profile": profile_name,
        "preference_variant": prof.get("preference_variant", "flat"),
        "record_count": len(emitted),
        "submission_count": len({r["submission_id"] for r in emitted}),
        "counts": counts,
        "grounded_count": sum(1 for r in emitted if (r.get("payload") or {}).get("grounded")),
        "multimodal_count": sum(1 for r in emitted if _rec_modality(r) == "multimodal"),
        "synthetic_prompt_count": len(_synthetic_records(emitted)),
        # Tri-state: true (all synthetic prompts from a ratified corpus), false
        # (some unratified — see datasheet warning), or null (no synthetic prompts).
        "seed_corpus_ratified": _seed_corpus_ratified(emitted),
        "kappa": kappa,
        "filters": filters,
        "note": note,
        "scope": scope,
        "tier_b_leak_gate": "passed",
        "files": companion_files,
        "content_hashes": content_hashes,
        "rubric_count": sum(1 for r in emitted if r.get("type") == "rubric"),
        "dir_path": str(out_dir),
        "destination": "local_disk",  # future seam: a cloud writer pushes here.
    }
    (out_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # 6. mark exported + provenance
    record_ids = [r["record_id"] for r in emitted]
    submission_ids = sorted({r["submission_id"] for r in emitted})
    store.mark_records_exported(record_ids, export_id)
    for sid in submission_ids:
        store.update_submission(sid, status="exported")
        store.log_event(
            entity_type="submission", entity_id=sid, event_type="exported",
            actor=created_by, payload={"export_id": export_id},
        )

    store.insert_export(
        export_id=export_id,
        created_by=created_by,
        record_count=len(emitted),
        filters=filters,
        dir_path=str(out_dir),
        manifest=manifest,
    )
    store.log_event(
        entity_type="export", entity_id=export_id, event_type="export_built",
        actor=created_by, payload={"record_count": len(emitted), "filters": filters},
    )
    return manifest


def zip_export(export: Dict[str, Any]) -> bytes:
    """Zip an export directory into an in-memory archive for download."""
    dir_path = Path(export.get("dir_path") or "")
    # Use the manifest's actual file list (may include the FEAT-2 grader files);
    # fall back to the base companions for older manifests.
    files = ((export.get("manifest") or {}).get("files")) or _COMPANION_FILES
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if dir_path.is_dir():
            for name in files:
                fp = dir_path / name
                if fp.exists():
                    zf.write(fp, arcname=name)
        else:
            zf.writestr(MANIFEST_NAME, json.dumps(export.get("manifest") or {}, indent=2))
    buf.seek(0)
    return buf.read()
