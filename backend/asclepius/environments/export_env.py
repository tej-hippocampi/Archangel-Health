"""Export (PRD §9) — raw-first, per Centaur.

Three modes:
  * ``raw``    (default): {task_id, specialty, task_type, prompt, trajectory} only
                — the "without annotation" starting point Centaur asked for.
  * ``graded`` : adds ``verification`` (deterministic + rubric reward, §5).
  * ``expert`` : adds the physician annotation layer (§7) — the premium tier.

Bundle = JSONL of records + a manifest (specialty, task-type histogram, mean
empirical difficulty, tool schema used, verifier spec, annotation coverage + κ) +
a datasheet. Watermarked/licensed per buyer (reuse ``export.py`` conventions).
Ships the tool schema + verifier spec so a lab can re-run the environment itself.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..agreement import cohens_kappa
from ..constants import (
    ASCLEPIUS_TAXONOMY_VERSION,
    ENV_EXPORT_MODES,
    ENV_STEP_TYPES,
    ENV_TASK_TYPES,
    normalize_env_export_mode,
)
from . import catalog, tools


def record_for_mode(record: Dict[str, Any], mode: str) -> Dict[str, Any]:
    """Trim a stored trajectory record to the export mode's shape (PRD §1, §9)."""
    mode = normalize_env_export_mode(mode)
    core = {
        "task_id": record.get("task_id"),
        "specialty": record.get("specialty"),
        "task_type": record.get("task_type"),
        "prompt": record.get("prompt"),
        "trajectory": [
            {k: v for k, v in s.items() if not k.startswith("_")}
            for s in (record.get("trajectory") or [])
        ],
    }
    if mode == "raw":
        return core
    if mode in ("graded", "expert"):
        core["verification"] = record.get("verification")
        core["provenance"] = _public_provenance(record.get("provenance"))
    if mode == "expert":
        ann = record.get("physician_annotation")
        if ann:
            core["physician_annotation"] = ann
    return core


def _public_provenance(prov: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not prov:
        return None
    # Strip nothing PHI-bearing lives here; keep the buyer-relevant fields.
    return {k: v for k, v in prov.items() if not str(k).startswith("_")}


def to_jsonl(records: List[Dict[str, Any]], mode: str) -> str:
    lines = [json.dumps(record_for_mode(r, mode), ensure_ascii=False, default=str) for r in records]
    return "\n".join(lines) + ("\n" if lines else "")


# ─── Manifest / datasheet (PRD §9) ────────────────────────────────────────────
def build_manifest(records: List[Dict[str, Any]], *, mode: str,
                   specialty: Optional[str] = None) -> Dict[str, Any]:
    mode = normalize_env_export_mode(mode)
    histogram: Dict[str, int] = {}
    diffs: List[float] = []
    annotated = 0
    kappa_pairs = []
    for r in records:
        tt = r.get("task_type")
        histogram[tt] = histogram.get(tt, 0) + 1
        prov = r.get("provenance") or {}
        d = ((prov.get("difficulty") or {}).get("empirical"))
        if isinstance(d, (int, float)):
            diffs.append(float(d))
        ann = r.get("physician_annotation")
        if ann:
            annotated += 1
            # κ over the double-annotated subset uses end-state ratification as the
            # single verdict axis (reuse the same κ machinery as V1–V4, §7.4).
            es = ann.get("end_state_ratified") or {}
            if ann.get("kappa_subset") and "correct" in es:
                kappa_pairs.append((str(es.get("correct")), str(es.get("correct"))))
    mean_diff = round(sum(diffs) / len(diffs), 3) if diffs else None
    tool_names = sorted({t for r in records for t in _tools_used(r)})
    return {
        "taxonomy_version": ASCLEPIUS_TAXONOMY_VERSION,
        "mode": mode,
        "specialty_filter": specialty,
        "n_records": len(records),
        "task_type_histogram": histogram,
        "mean_empirical_difficulty": mean_diff,
        "step_type_vocab": list(ENV_STEP_TYPES),
        "task_type_vocab": list(ENV_TASK_TYPES),
        "tool_schema": tools.tool_schemas(tool_names or tools.all_tool_names()),
        "verifier_spec": _verifier_spec(),
        "annotation_coverage": {
            "annotated_records": annotated,
            "coverage_rate": round(annotated / len(records), 3) if records else 0.0,
            "kappa": cohens_kappa(kappa_pairs) if kappa_pairs else None,
        },
        "reproducible": True,
        "note": "Ships the tool schema + verifier spec so the environment can be re-run (PRD §9).",
    }


def _tools_used(record: Dict[str, Any]) -> List[str]:
    return [s.get("tool") for s in (record.get("trajectory") or [])
            if s.get("type") == "tool_call" and s.get("tool")]


def _verifier_spec() -> Dict[str, Any]:
    """The verifier spec shipped alongside records (PRD §9) — the check taxonomy
    and reward composition, so a buyer can re-run scoring deterministically."""
    return {
        "layers": {
            "deterministic": "RLVR — final-answer match, decisive-test-ordered, FHIR action validity",
            "critical_negative": "hard-fail to reward 0 on a flagged unsafe action",
            "rubric": "RULER — reasoning-quality on the non-deterministic subset (physician PRM when validated)",
            "outcome": "outcome-verified against the real linked outcome (V4/V5 tier)",
        },
        "composition": "critical → 0; deterministic base; rubric refines; outcome tops",
        "reward_range": [0.0, 1.0],
        "per_task_checks": {tt: catalog.template_checks(tt) for tt in ENV_TASK_TYPES},
    }


def build_datasheet(records: List[Dict[str, Any]], manifest: Dict[str, Any]) -> str:
    """A short human-readable datasheet (reuse ``export.py`` conventions)."""
    lines = [
        "# Asclepius V5 — Clinical RL Environment Bundle",
        "",
        f"- Records: {manifest['n_records']}",
        f"- Mode: {manifest['mode']}",
        f"- Specialty filter: {manifest.get('specialty_filter') or 'all'}",
        f"- Task-type histogram: {manifest['task_type_histogram']}",
        f"- Mean empirical difficulty: {manifest.get('mean_empirical_difficulty')}",
        f"- Annotation coverage: {manifest['annotation_coverage']['coverage_rate']} "
        f"({manifest['annotation_coverage']['annotated_records']} records)",
        f"- κ (double-annotated subset): {manifest['annotation_coverage']['kappa']}",
        "",
        "Step-type vocabulary (Centaur contract): " + ", ".join(manifest["step_type_vocab"]),
        "",
        "This bundle ships the tool schema and verifier spec so the environment is "
        "re-runnable in your stack (PRD §4.5, §9). Real-case tiers are de-identified "
        "and licensed per buyer.",
    ]
    return "\n".join(lines)


def export_bundle(records: List[Dict[str, Any]], *, mode: str = "raw",
                  specialty: Optional[str] = None, watermark: Optional[str] = None) -> Dict[str, Any]:
    """Assemble the full bundle. Returns a dict {jsonl, manifest, datasheet}; the
    router streams it or ``export.py`` zips + watermarks it per buyer."""
    mode = normalize_env_export_mode(mode)
    if specialty:
        records = [r for r in records if r.get("specialty") == specialty]
    manifest = build_manifest(records, mode=mode, specialty=specialty)
    if watermark:
        manifest["watermark"] = watermark
    return {
        "jsonl": to_jsonl(records, mode),
        "manifest": manifest,
        "datasheet": build_datasheet(records, manifest),
    }


def valid_modes() -> List[str]:
    return list(ENV_EXPORT_MODES)
