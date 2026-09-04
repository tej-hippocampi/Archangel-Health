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
import logging
import os
import re
import zipfile
import realm as _realm
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("asclepius.export")

from asclepius import agreement as asc_agreement
from asclepius import credentials as asc_credentials
from asclepius import packaging as asc_packaging
from asclepius import profiles
from asclepius import trajectory as asc_trajectory
from asclepius.constants import (
    ASCLEPIUS_CONFIG_VERSION,
    ASCLEPIUS_TAXONOMY_VERSION,
    KAPPA_THRESHOLD,
)

JSONL_NAME = "records.jsonl"
CASES_NAME = "cases.jsonl"
MANIFEST_NAME = "batch.json"
DICTIONARY_NAME = "data_dictionary.md"
DATASHEET_NAME = "datasheet.md"
QUALITY_NAME = "quality_report.md"

#: Buyer-facing label for each portal version. ONE table: a version added to one
#: document and missed in another is how `v5` rendered as "assisted" in the quality
#: report while the datasheet listed it correctly. An unknown version must read as
#: unknown, never fall through to a plausible default.
PORTAL_VERSION_LABELS = {
    "v1": "classic",
    "v2": "assisted",
    "v3": "seamless synthetic",
    "v4": "REAL de-identified cases",
    "v5": "REAL longitudinal chart walks",
}


#: Buyer-facing definition of each ``case_source``. Keys are checked against
#: ``cases.CASE_SOURCES`` by test, so this cannot quietly become a second,
#: divergent definition of the vocabulary.
CASE_SOURCE_GLOSS = {
    "real_deid": "`real_deid` is a real encounter de-identified by the data "
                 "partner before transfer",
    "synthetic": "`synthetic` is authored against a clinician-curated archetype",
}


def _portal_version_legend() -> str:
    """The portal-version legend, rendered from the one table. Hand-maintained
    copies of this vocabulary are how `v5` came to render as "assisted" in one
    document while another listed it correctly."""
    return " · ".join(f"{k.upper()} {v}" for k, v in PORTAL_VERSION_LABELS.items())


# Grader export (FEAT-2): shipped alongside the data when the batch carries rubric
# records, so a buyer can run rubric-based LLM-as-judge scoring out of the box.
GRADER_PROMPT_NAME = "grader_prompt.txt"
SCORE_PY_NAME = "score.py"
# Eval pack (Rubric Rigor FIX-5.2): the grader files + validity report + the rubric
# records form a STANDALONE, re-licensable-per-model-version SKU — a reusable scoring
# function whose discriminative validity is tied to the model version it was proven
# against, so it re-licenses each time the buyer moves to a new frontier model.
EVAL_PACK_NAME = "EVAL_PACK.md"
VALIDITY_REPORT_NAME = "validity_report.json"

_COMPANION_FILES = [JSONL_NAME, MANIFEST_NAME, DICTIONARY_NAME, DATASHEET_NAME, QUALITY_NAME]

_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


class ExportValidationError(ValueError):
    """A mapped line failed the target profile's JSON Schema — the batch is
    rejected wholesale (opt §2: no partial silent exports)."""


def _export_root_path() -> Path:
    """The configured export dir, made absolute WITHOUT creating or resolving it.

    ``export_root`` adds the ``mkdir`` (and ``.resolve()``) for real filesystem use.
    A durability *check* must do neither, for the two reasons
    ``ingestion._ingest_root_path`` already spells out and this module had not
    learned:

      * **mkdir** turns "not durable" into an exception. Probing a path the
        process cannot create — an unmounted ``/data`` on a non-root runner, a
        root-owned ``/run`` — raises ``PermissionError`` instead of returning the
        very answer the check exists to give. The endpoint catches it and reports
        ``durable: false, "durability check raised: …"``, which is a DIFFERENT
        claim from "this path is ephemeral": one says the export store will be
        wiped, the other says we could not tell. The storage banner exists so an
        operator can trust that difference.
      * **resolve** rewrites a path through its symlinks, so ``/tmp`` on a system
        where it links elsewhere no longer matches the ephemeral prefix list and
        the check silently misses. The default here IS ``/tmp/asclepius-exports``,
        so that is the one path this probe must never fail to recognise.
    """
    return Path(os.path.abspath(_realm.paths(_realm.LIVE)["exports"]))


# ─── Exclusivity (audit U5) ───────────────────────────────────────────────────
# The one constraint the founder placed on otherwise-unrestricted reuse of
# incoming data: "unless they have the licensing agreement, that doesn't mean you
# can go sell it to 5 other people unless they didn't pay exclusively." So an
# export carries an optional licence, and a licence is either exclusive or not.
EXCLUSIVE = "exclusive"
NON_EXCLUSIVE = "non_exclusive"
LICENCE_TERMS = (EXCLUSIVE, NON_EXCLUSIVE)


class ExclusiveLicenseConflict(ValueError):
    """This batch contains records already promised exclusively to someone else.

    Carries ``conflicts`` (one entry per blocking licence: licence id, buyer,
    originating export, overlap count and a sample of overlapping record ids) so
    the operator is told WHICH agreement is in the way rather than that the export
    failed. Nothing is written and no record is marked exported when this raises."""

    def __init__(self, message: str, conflicts: List[Dict[str, Any]]):
        super().__init__(message)
        self.conflicts = conflicts


def _conflict_message(conflicts: List[Dict[str, Any]], licensed_to: Optional[str]) -> str:
    parts = []
    for c in conflicts:
        who = c.get("buyer_label") or c.get("buyer_key")
        sample = ", ".join(c.get("overlap_sample") or [])
        expiry = c.get("expires_at") or "no expiry"
        parts.append(
            f"licence {c['license_id']} (exclusive to {who}, from export "
            f"{c['export_id']}, {expiry}) covers {c['overlap_count']} of these "
            f"records, including {sample}"
        )
    who_for = f" to {licensed_to}" if licensed_to else ""
    return (
        f"Export refused{who_for}: it overlaps an exclusive licence held by another "
        "buyer. " + "; ".join(parts) + ". Release or narrow the licence, or narrow "
        "this cut, before exporting."
    )


def validate_license_expiry(value: Optional[str]) -> Optional[str]:
    """Vet ``license_expires_at`` at the API boundary, before anything is written.

    Expiry is enforced by LEXICAL comparison against naive-UTC ISO stamps
    (``expires_at > now`` in ``store.exclusive_license_conflicts``), so a
    malformed value like '12/31/2026' sorts before every current stamp and reads
    as already expired: the licence records fine and then silently never blocks
    anyone. Refusing it at the door is the only moment somebody is looking.

    Returns the string to store: as typed for naive dates/datetimes, converted
    to naive UTC for offset-aware ones (an offset kept verbatim would be
    compared as if it were UTC and enforce the wrong instant). Raises
    ValueError, which every export boundary turns into a 400, on a value
    ``fromisoformat`` cannot parse.
    """
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            "license_expires_at must be an ISO date or datetime "
            f"(for example 2027-01-31 or 2027-01-31T00:00:00), got {text!r}."
        ) from None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None).isoformat()
    return text


def enforce_exclusivity(
    store: Any, record_ids: List[str], *, licensed_to: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Refuse a batch that would re-sell records already committed exclusively.

    Enforced at RECORD level, not bundle level, because the meeting that asked for
    this also decided data may be split and recombined: a check that only knows
    about whole bundles is defeated by re-cutting the same records under a
    different filter, which is the ordinary way an export gets built here.

    ``licensed_to`` is the buyer this batch is going to. When it matches the
    holder of the exclusive licence, the export proceeds: a buyer re-taking
    delivery of their own exclusive data breaches nothing. When it is None we
    cannot show the batch is going to the holder, so the conflict stands.

    WHAT THIS DELIBERATELY DOES NOT COVER, and why.

    1. Identity is ``record_id``, so exclusivity attaches to the exported
       artifacts, not to the underlying clinical content. If a THIRD physician
       labels a case after that case shipped exclusively, their record is a new
       record and does not conflict. Blocking it would be the wrong default:
       a partial cut of a multi-labeler case is a normal thing to sell, and
       auto-blocking every sibling record would silently stop legitimate sales
       with no way for the operator to see why. The commitment therefore stores
       its ``case_ids`` too, so the admin register can show which cases are
       touched and a human can make that call. Whether a sibling label counts as
       the same data is a contract question, not a code question.

    2. Only builders that route through ``build_export`` are gated. The V5
       agentic-trajectory export (``/api/asclepius/environments/export``) and the
       gold-set export are separate artifact builders over separate tables, and
       neither reads or writes the records table this register is keyed on. They
       are ungated today, and closing that needs its own identity scheme rather
       than a call added here.

    3. Aggregate statistics computed store-wide (Cohen's kappa, the quality
       report, the failure taxonomy) are not gated, because they are derived
       numbers over the whole corpus rather than the licensed records."""
    conflicts = store.exclusive_license_conflicts(record_ids, buyer_key=licensed_to)
    if conflicts:
        raise ExclusiveLicenseConflict(_conflict_message(conflicts, licensed_to), conflicts)
    return conflicts


def export_root() -> Path:
    """The export dir, created and ready to write into. Real filesystem use only —
    a durability check wants ``_export_root_path`` instead.

    Realm-scoped (Sandbox PRD §1.1): a sandbox export is built under
    ``<root>/sandbox/`` so a sandbox bundle never sits beside a live one."""
    root = Path(_realm.paths()["exports"]).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def export_storage_durable() -> tuple:
    """(ok, detail) — will built bundles survive a redeploy?

    ``ASCLEPIUS_EXPORT_DIR`` defaults to ``/tmp/asclepius-exports``, which is
    erased on every deploy. The ``exports`` ROW survives (it is in the database),
    so history keeps listing the batch — and its Download button then hands the
    buyer an archive containing only ``batch.json``, because ``zip_export``
    rebuilds what it can from the stored manifest and there is nothing else left.
    A delivered buyer, following the link in the email we sent them, downloads a
    dataset with no data in it.

    **Reported, never fail-closed** — deliberately, unlike the database. A lost
    bundle is RECOVERABLE: records are permanent and export is non-destructive,
    so the batch can simply be cut again. Refusing to boot over something a
    re-export fixes would trade a real outage for a recoverable inconvenience.
    """
    from asclepius.constants import (
        path_is_ephemeral, path_under_declared_volume,
    )

    configured = (os.getenv("ASCLEPIUS_EXPORT_DIR") or "").strip()
    # NOT ``export_root()`` — see ``_export_root_path``. This is a read-only probe.
    path = str(_export_root_path())
    if not configured:
        return False, (
            f"ASCLEPIUS_EXPORT_DIR is not set, so built bundles land in {path} "
            "and are erased on every redeploy. Past exports stay listed in "
            "history but download as an empty archive — including for a buyer "
            "following the link we emailed them. Set it to a path on the volume "
            "(e.g. /data/asclepius-exports).")
    if path_under_declared_volume(path) is False:
        return False, (f"{path} is not under the volume this platform mounted; "
                       "built bundles are erased on every redeploy.")
    if path_is_ephemeral(path):
        return False, (f"{path} is on ephemeral storage; built bundles are "
                       "erased on every redeploy.")
    return True, f"export bundles durable ({path})"


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
    by_taxonomy_bucket: Dict[str, int] = {}
    by_case_type: Dict[str, int] = {}
    # Per-specialty breakdown (Specialty Hyper-Personalization PRD §2): count, mean
    # empirical difficulty, failure-mode histogram, and bucket histogram — so a buyer
    # sees exactly what each per-specialty slice contains and its measured hardness.
    spec_breakdown: Dict[str, Dict[str, Any]] = {}

    def _sb(sp: str) -> Dict[str, Any]:
        return spec_breakdown.setdefault(sp, {
            "count": 0, "failure_modes": {}, "taxonomy_buckets": {},
            "_difficulty_sum": 0.0, "_difficulty_n": 0, "measured_count": 0,
        })

    for r in records:
        by_type[r["type"]] = by_type.get(r["type"], 0) + 1
        sp = r.get("specialty") or "unknown"
        by_specialty[sp] = by_specialty.get(sp, 0) + 1
        # V1 (classic) vs V2 (assisted) breakdown (Asclepius V2). Legacy records
        # with no stamp are counted as v1 (they predate the assisted flow).
        payload = r.get("payload") or {}
        pv = payload.get("portal_version") or "v1"
        by_portal_version[pv] = by_portal_version.get(pv, 0) + 1
        # Text vs multimodal (structured-case) breakdown (Multimodal PRD §8).
        mod = _rec_modality(r)
        by_modality[mod] = by_modality.get(mod, 0) + 1
        cs = _rec_case_source(r)
        if cs:
            by_case_source[cs] = by_case_source.get(cs, 0) + 1
        # Specialty case-type metadata (PRD §2).
        bucket = payload.get("taxonomy_bucket")
        if bucket:
            by_taxonomy_bucket[bucket] = by_taxonomy_bucket.get(bucket, 0) + 1
        ctype = payload.get("case_type")
        if ctype:
            by_case_type[ctype] = by_case_type.get(ctype, 0) + 1
        sb = _sb(sp)
        sb["count"] += 1
        fm = payload.get("ai_failure_mode")
        if fm:
            sb["failure_modes"][fm] = sb["failure_modes"].get(fm, 0) + 1
        if bucket:
            sb["taxonomy_buckets"][bucket] = sb["taxonomy_buckets"].get(bucket, 0) + 1
        ed = payload.get("empirical_difficulty")
        if isinstance(ed, (int, float)):
            sb["_difficulty_sum"] += float(ed)
            sb["_difficulty_n"] += 1
        if payload.get("empirical_difficulty_measured"):
            sb["measured_count"] += 1

    # Finalize the per-specialty mean difficulty and drop the running sums.
    for sp, sb in spec_breakdown.items():
        n = sb.pop("_difficulty_n", 0)
        s = sb.pop("_difficulty_sum", 0.0)
        sb["mean_empirical_difficulty"] = round(s / n, 3) if n else None

    return {
        "by_type": by_type,
        "by_specialty": by_specialty,
        "by_portal_version": by_portal_version,
        "by_modality": by_modality,
        "by_case_source": by_case_source,
        "by_taxonomy_bucket": by_taxonomy_bucket,
        "by_case_type": by_case_type,
        "specialty_breakdown": spec_breakdown,
        "total": len(records),
    }


# ─── Filtering (opt §2) ───────────────────────────────────────────────────────
def _trajectory_sort_key(rec: Dict[str, Any]) -> tuple:
    """Order a walk's points by their position in it, and leave everything else
    where it was (Longitudinal E2E PRD §5.3).

    ``(0, "", 0)`` for a record with no trajectory, so every non-longitudinal
    record sorts equal and Python's stable sort preserves the order they were
    selected in. A walk sorts after them, grouped by ``trajectory_id`` and then by
    ``sequence_index`` — never by ``captured_at``, which is when the PHYSICIAN
    worked, not when the patient did.
    """
    payload = rec.get("payload") or {}
    block = payload.get("trajectory") or {}
    tid = block.get("trajectory_id") or payload.get("trajectory_id")
    if not tid:
        return (0, "", 0)
    idx = block.get("sequence_index")
    if idx is None:
        idx = payload.get("sequence_index")
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        # A point with no readable position sorts FIRST within its walk, where it
        # is conspicuous, rather than last where it reads as the terminal point.
        idx = -1
    return (1, str(tid), idx)


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
    case_id: Optional[str] = None,
    case_ids: Optional[set] = None,
) -> bool:
    payload = rec.get("payload") or {}
    # Single-task scoping (Exports rework): export exactly one submission's
    # records. The submission id is a top-level record column (and mirrored into
    # the payload at packaging time) — accept either.
    if submission_id and rec.get("submission_id") != submission_id and payload.get("submission_id") != submission_id:
        return False
    # Case scoping (PRD A Phase 5): a case IS a task — one case_id bundles every
    # submission + review on it. Accept the column or the payload mirror.
    #
    # ``case_ids`` WIDENS that to a set, and it exists for exactly one caller:
    # V5 scope, where selecting one point of a chart walk must export the WHOLE
    # walk (Longitudinal E2E PRD §5.3). A fragment of a trajectory is not a
    # cheaper trajectory — point 7 with no point 6 has no state to have been
    # reasoned from, and the buyer cannot reassemble what they paid for.
    if case_ids is not None:
        if (rec.get("task_id") not in case_ids
                and payload.get("task_id") not in case_ids):
            return False
    elif case_id and rec.get("task_id") != case_id and payload.get("task_id") != case_id:
        return False
    # Multi-case scoping (PRD §2.1): the Export tab's Case scope accepts a LIST,
    # because "the three cases I just approved" is one bundle and not three.
    # Kept beside ``case_id`` rather than replacing it — the single-id form is
    # ``export_by_case``'s frozen signature (Seam 2) and every caller of it.
    if case_ids is not None and (
            rec.get("task_id") not in case_ids and payload.get("task_id") not in case_ids):
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
    return f"""# Archangel Health Export: Data Dictionary

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
| `related_party` | true when the annotating physician holds an advisory relationship with Archangel Health, including equity. Their clinical credentials are unchanged and stated above; this flag exists so provenance is complete. Recorded as of authorship — a contributor appointed after writing a record carries `false` on that record, because they held no interest when they wrote it |
| `submission_id` / `task_id` | lineage |
| `context.case_source` | where the CHART came from: `real_deid` (a real encounter de-identified by the data partner before transfer, HIPAA Safe Harbor) · `synthetic` (authored against a clinician-curated archetype and PHI-scanned). Absent on a text record, which carries no case; the manifest buckets those under `unspecified` in `case_provenance`. Independent of `source` and of who authored the question |
| `source` | where the task originated: `lab_supplied` · `internal_prompt_bank` · `partner_ehr` (a real, de-identified case ingested from a data partner's secure upload). Independent of who authored the QUESTION — a `partner_ehr` record may still carry a model-authored question, and the manifest counts those separately under `model_generated_question_count` |
| `buyer_request_id` | the buyer request the record answers |
| `taxonomy_version` / `config_version` | versioning |
| `portal_version` | evaluator product flow that produced the record: `v1` (classic) · `v2` (assisted) · `v3` (seamless synthetic) · `v4` (real de-identified static charts) · `v5` (real longitudinal chart walks). Stage-1 prompt review and the record types are identical across all of them; `v2` adds quick-stance capture, model-assist provenance and structured reasons; `v4`/`v5` replace the synthetic case with a real de-identified chart carried under `context.case` |
| `license` / `ip_cleared` / `contains_phi` | rights attestation |
| `captured_at` | submission capture timestamp |

## Expert review annexes on every record (out of profile schema)

`review` and `supervision` are attached to every line in `{JSONL_NAME}` AFTER
profile mapping and schema validation, so they are deliberately **out of the
buyer profile's schema** — annexes, not mapped fields. A profile that declares
`additionalProperties: false` will not see them validated. They are documented
here because they ship, and an undocumented field in a delivered artifact is
indistinguishable from a leak.

| field | meaning |
| --- | --- |
| `review.reviewed` / `review.n_reviews` | whether a senior physician graded this labeler submission, and how many did |
| `review.reviews[].verdict` | `accept` · `accept_with_edits` · `reject` |
| `review.reviews[].dimensions` | per-dimension judgment: `agree` · `disagree` · `cannot_assess`. **`cannot_assess` is its own state** — the honest answer when a dimension is outside the reviewer's subspecialty — and is never counted as disagreement |
| `review.reviews[].corrections` | the reviewer's edits. `{{}}` when withheld — see `corrections_withheld` |
| `review.reviews[].corrections_withheld` | `true` when an insert-time Safe-Harbor scan found an identifier in the reviewer's free text, or when that text predates scanning. The verdict and dimensions still ship; only the prose is held back |
| `review.reviews[].reviewer_credential` | credential ATTRIBUTE only (e.g. board_certified_nephrology) — never a name, NPI or licence number |
| `review.reviews[].blinded` | `true` only when the payload served to that reviewer verifiably carried no labeler identity. **Derived from the served payload, not asserted.** An admin reviewer is always `false` (an admin can de-blind by other means) |
| `review.reviews[].step_divergence` | process-level supervision from the reviewer: `[{{index, judged}}]` — the reasoning-step positions where the two independent labels diverged, and which side (`A` / `B` / `neither`, in CANONICAL oldest-first terms, or `null` for undecided) the reviewer judged correct at each. **Absent — never `[]` — when the two labels were not comparable**, i.e. one of them carried no reasoning steps and nothing was measured; `[]` means both carried steps and they agreed at every one. Present only on paired adjudications |
| `review.accepted_without_edits` | true only when every review was a plain `accept` |
| `supervision.labeler_id_hashed` | stable hashed id of the labeler (no PII) |
| `supervision.independent_second_label` | `true` only for the double-labeled slice whose second observation was explicitly blinded **and not excluded from the κ pool** — the slice a real Cohen's κ is computed on |
| `supervision.kappa_excluded_reason` | why this case's agreement observation is outside the κ pool, when it is. `trajectory_sequential` — see the longitudinal annex below. `null` otherwise |

**Expert review is NOT inter-rater agreement.** The reviewer sees the labeler's
answer, so the two observations are not independent and κ does not apply. The
review acceptance rate and Cohen's κ are reported as two separately named
figures in `{QUALITY_NAME}`; κ covers only the independently double-labeled slice.

## Longitudinal decision points — the `trajectory` annex

A **longitudinal case** is a real chart truncated at one encounter. The physician
answers with the record sealed at that moment, and the chart's own next encounter
is what checks the answer. Where a single-shot case ships a preference, one of
these ships a **trajectory**: state → action → observed outcome.

`trajectory` is attached to any record that belongs to a chart walk or carries a
sealed prediction. Like `review` and `supervision` it is an **annex** — outside
the buyer profile's schema, and documented here because it ships.

| field | meaning |
| --- | --- |
| `trajectory.trajectory_id` | **the reassembly key.** Every decision point taken from one chart walk shares it. `records.jsonl` is one line per record, so without this a thirteen-point chart arrives as thirteen unrelated rows |
| `trajectory.sequence_index` | 0-based position in the walk. **Ordering is the point** — point *n*'s visible chart is the state before point *n*'s decision, and point *n+1*'s chart contains what happened after it. Sort on this, never on `captured_at` |
| `trajectory.expected_trajectory.expectations[]` | what the physician said should happen next if their assessment was right, each with an optional `horizon_days` |
| `trajectory.expected_trajectory.falsifiers[]` | **what would tell them they were wrong.** Specialist-authored, written before the next encounter was revealed, attached to a real chart. This is the falsifier corpus |
| `trajectory.expected_trajectory.falsifiable` | `true` when at least one falsifier was named. Filter on it rather than assuming — a physician who could not name one for a given decision is allowed to say so, and a fabricated falsifier is worth less than none |
| `trajectory.self_score.marks[]` | per expectation: `held` · `did_not_hold` · `not_assessable`. **`not_assessable` is its own state**, never folded into either of the others: the next encounter frequently does not contain the observation the prediction was about |
| `trajectory.self_score.falsifier_fired` | the physician's assertion that their own stated falsifier fired in the revealed encounter |
| `trajectory.outcome_verified` | `true` only when at least one expectation was actually assessable against the record. A point where everything was `not_assessable` produced **no** outcome verification |

**What the outcome check does and does not establish.** Marks are scored against
the trajectory the record actually contains, which reflects the treatment actually
given — not the plan this physician proposed. Where they proposed something
different, the outcome does not test their plan; it tests the one that was
followed. Nothing here scores counterfactual outcomes, and no figure in this
bundle should be read as doing so.

**Sicker patients get more aggressive treatment.** A model trained naively on
chart trajectories learns the treatment pattern, not the reasoning. The scored
object is the stated reasoning and expectation, never the plan's similarity to
what was done.

**Survivorship.** These charts continue because the patient continued. Encounters
ending in death or transfer are absent by construction — and that is exactly where
the interesting failures live.

**Yield per chart is not predictable.** One five-year chart yields thirteen
decision points; one twenty-year chart yields two. Count decision points, not
records and not charts.

**`study_findings_policy` varies within a single walk.** It is computed per
truncation: a window carrying no imaging is `visible`, a later one carrying a
study asset is `hidden`. The same patient therefore presents under two policies
across one trajectory. That is the policy reflecting what each window actually
contains, not an inconsistency.

**These points are excluded from Cohen's κ, deliberately.** Blinding means the
labeler did not see a co-labeler's identity; it says nothing about temporal
independence. A physician who labels encounter *k* and then *k+1* is blinded on
both, and what the two observations share is their own model of that patient,
formed at *k*. Aggregating them would measure within-physician consistency and
report it as between-physician agreement. They carry outcome verification instead
— reported in `{QUALITY_NAME}` under its own name, never folded into κ.

## `{CASES_NAME}` — the case-keyed companion

One JSON object per line, one line per case. Same content as `{JSONL_NAME}`,
reorganized around the case rather than the physician's worklist: a case with
two labelers and a review is one artifact, not three unrelated rows. Also an
annex — not covered by the profile schema.

| field | meaning |
| --- | --- |
| `case_id` | the case (task) identifier this bundle is keyed on |
| `specialty` / `difficulty` / `prompt` / `context` | the case itself; `context.case` holds out the answer key |
| `n_labelers` / `labels[]` | every labeler submission on this case |
| `labels[].records[]` | that labeler's mapped records, minus the `review`/`supervision` annexes, which are stated once at case level |
| `review` / `supervision` | as above, aggregated across every label on the case |
| `consensus.verdicts` | verdict histogram across labelers |
| `consensus.majority_verdict` | `null` on a tie — a tie is not a majority |
| `consensus.unanimous` | all labelers agreed. Read it together with `n_labelers`: unanimity across one label is not agreement |
| `consensus.agreement_observation` | the stored double-label observation (`verdict_a`, `verdict_b`, `verdict_agree`, `blinded`, `kappa_excluded_reason`), or `null` when the case was not double-labeled |
| `trajectory_id` / `sequence_index` | the chart walk this case belongs to and its position in it — the case-level reassembly key. `null` on a single-shot case |
| `labels[].trajectory` | that physician's sealed prediction and their own grading of it. Per LABEL, not per case: two physicians on one decision point write two different falsifiers |
"""


def _synthetic_records(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        r for r in records
        if (r.get("payload") or {}).get("source") == "internal_prompt_bank"
        or (r.get("payload") or {}).get("generation")
    ]


def _case_provenance(records: List[Dict[str, Any]]) -> Dict[str, int]:
    """Where the CASES in this batch came from, counted (PRD §2.3).

    Distinct from ``model_generated_question_count``, which is about where the
    QUESTION came from. The two are independent axes and conflating them is what
    made "synthetic_prompt_count" misread: a real de-identified chart with a
    model-authored question is a real case, and a buyer paying real-case prices
    is entitled to see that stated separately.

    Keys are the ``case_source`` vocabulary (``real_deid`` / ``synthetic``) plus
    ``unspecified`` for text records, which carry no case at all.
    """
    out: Dict[str, int] = {}
    for r in records:
        key = _rec_case_source(r) or "unspecified"
        out[key] = out.get(key, 0) + 1
    return out


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


def _sole_shipped_value(emitted: List[Dict[str, Any]], mapped_objs: List[Dict[str, Any]],
                        prof: Dict[str, Any], field: str) -> Optional[Any]:
    """The value EVERY shipped line carries for ``field``, or None.

    Read off ``mapped_objs`` — the objects ``records.jsonl`` is written from —
    never off the raw store record, which carries the value as CAPTURED. The
    license is the case that made this matter: it is re-stamped at emit, so the
    stored record and the shipped line legitimately disagree.

    Three ways to get None, all meaning "this bundle has no one answer, so the
    manifest asserts none":

    * no line carries the field;
    * some do and some do not — a bundle-wide claim contradicted by a shipped
      line is the defect this whole helper exists to prevent, and a partially
      stamped batch (legacy records with no ``portal_version``, mixed into a
      current cut) is a live shape, not a hypothetical;
    * lines disagree on the value.

    Mapped through the profile's field map, because a profile renames our
    canonical field to the buyer's (``TEMPLATE.json`` renames
    ``annotator_credential`` to ``expert``). Reading our name off a renamed line
    would report "no specialty" for a bundle whose every line carries one.
    """
    if not mapped_objs:
        return None
    seen = _shipped_values(emitted, mapped_objs, prof, field)
    # ``next(iter(...))`` rather than ``pop()``: the returned set is frozen, and
    # this function is called once per field alongside the plural derivation, so
    # memoising it is the obvious next change. A ``pop()`` on a cached set would
    # drain it and silently empty the plural key.
    return next(iter(seen)) if len(seen) == 1 and None not in seen else None


def _shipped_values(emitted: List[Dict[str, Any]], mapped_objs: List[Dict[str, Any]],
                    prof: Dict[str, Any], field: str) -> frozenset:
    """Every distinct value the shipped lines carry for ``field``, ``None`` included.

    The single derivation behind both the singular and the plural manifest keys.
    They were briefly computed from two different sources — the singular off the
    shipped payload through the profile's field map, the plural off ``counts``,
    which reads the store column and substitutes display sentinels — so the two
    keys could disagree with each other and with the lines they describe.

    ``_counts``' sentinels (``"unknown"`` for a missing specialty, ``"v1"`` for a
    missing portal version) must never reach a manifest key or an audit predicate.
    They exist to keep a report's tally total. Asserting one to a buyer claims
    content the bundle does not have, and satisfying a gate with one means the
    gate can no longer fail: ``build_export`` raises on an empty batch, so a
    ``counts`` bucket is non-empty for every bundle it produces.
    """
    seen = set()
    for rec, mapped in zip(emitted, mapped_objs):
        # ``map_record`` resolves its field map from ``payload["type"]``
        # (profiles.py), so resolve from the same place: reading the store column
        # instead would silently pick a different map if the two ever diverged.
        rtype = ((rec.get("payload") or {}).get("type")) or rec.get("type")
        fm = profiles.field_map_for(prof, rtype) or {}
        seen.add(mapped.get(fm.get(field, field)))
    return frozenset(seen)


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
    # The `source` values the records ACTUALLY carry. This section used to state
    # `source: internal_prompt_bank` flatly, which is wrong for every batch whose
    # records reached it by the other arm of ``_synthetic_records`` — a real
    # partner_ehr chart with a model-authored question. A datasheet that names a
    # field value the records do not carry is a contradiction a buyer finds with
    # one grep, so the value is read out of the records instead of typed here.
    # CHART provenance is ``case_source``, and ``_case_provenance`` already
    # computes it — the same count the manifest ships. ``source`` is TASK ORIGIN
    # (constants.TASK_SOURCES), a different axis, and naming it "chart" here is
    # what produced the contradiction this section exists to avoid. Both are
    # reported, each under its own name, each counted off the records.
    # Ties sort by name so two cuts of one set cannot render different text.
    def _axis(values: List[Optional[str]], field: str) -> str:
        """Render one provenance axis: the values the records carry, counted, plus
        a separate count of the records that carry none.

        An absence is never rendered as a value. ``unspecified`` is a bucket
        ``_case_provenance`` invents for text records (``packaging`` stamps
        ``case_source`` only for multimodal tasks) and is not in the
        ``case_source`` vocabulary; ``source`` can simply be missing. Either way a
        buyer who greps ``records.jsonl`` for the rendered string must find it, so
        an absence is stated as an absence, in its own clause, outside the value
        list. Ties sort by name: two cuts of one set render one text.
        """
        present: Dict[str, int] = {}
        absent = 0
        for v in values:
            # Absence is exactly falsiness, matching ``_case_provenance``'s
            # ``or "unspecified"`` so the two always agree on the same records.
            # A record that literally carries the string is NOT absent and is
            # rendered as the value it carries: the guard that used to special-case
            # it here reported "carries no source" about a record that carries one,
            # which is the defect class, inverted.
            if v:
                present[v] = present.get(v, 0) + 1
            else:
                absent += 1
        tally = " · ".join(
            f"**{n}/{len(records)}** `{k}`"
            for k, n in sorted(present.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        gap = f"**{absent}/{len(records)}** record(s) carry no `{field}`"
        return f"{tally}; {gap}" if tally and absent else (tally or gap)

    chart_values = [_rec_case_source(r) for r in records]
    chart_line = _axis(chart_values, "context.case_source")
    origin_line = _axis([(r.get("payload") or {}).get("source") for r in records],
                        "source")
    # Gloss only the values this batch uses, and say so loudly when a batch
    # carries a case_source outside the vocabulary — the same discipline
    # PORTAL_VERSION_LABELS applies, rather than shipping an unknown value with
    # no marker. The vocabulary's home is ``cases.CASE_SOURCES``; this dict is
    # checked against it by test, so it cannot drift into a second definition.
    present_sources = {c for c in chart_values if c}
    chart_gloss = ". ".join(
        CASE_SOURCE_GLOSS[v] for v in sorted(CASE_SOURCE_GLOSS) if v in present_sources
    )
    unknown = sorted(present_sources - set(CASE_SOURCE_GLOSS))
    if unknown:
        chart_gloss += ((". " if chart_gloss else "")
                        + "Not in the documented `case_source` vocabulary: "
                        + ", ".join(f"`{u}`" for u in unknown))
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
            "specialist sign-off; treat the prompt material as provisional. The expert "
            "training signal is the specialist's chosen/ideal answer and revision — not "
            "the synthetic prompt itself."
        )
    return f"""

## Question provenance (Seedmaker)
Where the CHART came from and where the QUESTION came from are two independent
axes. They are stated separately because collapsing them into one word is how a
real de-identified chart gets described as a synthetic dataset.
- **Chart** (`context.case_source`; a record with no case carries none, and is
  counted under `unspecified` in the manifest's `case_provenance`):
  {chart_line}.{(" " + chart_gloss + ".") if chart_gloss else ""}
- **Task origin** (`source`, where the task entered the portal — a different axis
  from both the chart and the question): {origin_line}. Values are defined in
  `data_dictionary.md`; no gloss is asserted here that the field does not carry.
- **Question:** **{len(synthetic)}/{len(records)}** records carry a model-authored
  question, counted in the manifest as `model_generated_question_count`. Questions
  were synthesized by the Archangel Health Seedmaker engine, grounded in a curated
  specialty seed corpus{(' (versions: ' + ', '.join(versions) + ')') if versions else ''},
  then novelty-/contamination-checked and passed an error-likelihood quality gate
  before any specialist evaluated them.
{ratify_line}
- The AI generated only the question and the two candidate answers (the material to
  be judged); all grounding/evidence anchors and the chosen/ideal answers are the
  credentialed specialist's work. Model-authored questions are never auto-marked
  grounded."""


def _longitudinal_scope_md(records: List[Dict[str, Any]]) -> str:
    """The Composition line naming what a longitudinal batch actually contains
    (Longitudinal E2E PRD §5.3): how many WALKS, and how many points across them.

    Two numbers because they are never the same number and the pair is what the
    artifact is priced on. "21 records" says nothing about whether that is two
    complete chart walks or twenty-one unrelated fragments, and those are
    different products at very different prices. Emitted only when the batch
    carries a trajectory, so a V1–V4 datasheet is byte-for-byte unchanged.
    """
    walks: Dict[str, int] = {}
    for rec in records:
        payload = rec.get("payload") or {}
        block = payload.get("trajectory") or {}
        tid = block.get("trajectory_id") or payload.get("trajectory_id")
        if tid:
            walks[str(tid)] = walks.get(str(tid), 0) + 1
    if not walks:
        return ""
    n_walks, n_points = len(walks), sum(walks.values())
    return (
        f"- Scope: **V5 longitudinal** · {n_walks} trajector"
        f"{'y' if n_walks == 1 else 'ies'} · {n_points} point"
        f"{'' if n_points == 1 else 's'}. Records are one line per POINT; the "
        "reassembly key is `trajectory.trajectory_id` and the order is "
        "`trajectory.sequence_index`. Never sort a walk on `captured_at` — that is "
        "when the physician worked, not when the patient did."
    )


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


def _composition_scope_line(scope: Optional[Dict[str, Any]]) -> str:
    """The one-line "how this bundle was cut" for the datasheet's Composition
    section (PRD §2.3), e.g.

        - Scope: **physician** · nephrology · 7 cases (annotator `3f9a…c1`)

    A lab that receives a bundle needs to know it is a slice and which slice —
    a per-physician cut and an everything cut have very different statistical
    properties, and a datasheet that does not say which is a datasheet that
    invites the wrong conclusion.

    **The physician's NAME never appears here, or anywhere else in a bundle.**
    Only ``annotator_id_hashed``, which is what the Further Credential Summary
    matches on under NDA. This function reads only fields that are hashes or
    counts; a caller that puts a name in ``scope`` is the bug, and
    ``test_export_approval_prd`` asserts no bundle carries one.
    """
    if not scope:
        return ""
    bits: List[str] = []
    stype = scope.get("type")
    if stype:
        bits.append(f"**{stype}**")
    for key in ("specialty", "portal_version"):
        if scope.get(key):
            bits.append(str(scope[key]))
    n = scope.get("case_count")
    if isinstance(n, int):
        bits.append(f"{n} case{'' if n == 1 else 's'}")
    if not bits:
        return ""
    line = "- Scope: " + " · ".join(bits)
    hashed = scope.get("annotator_id_hashed")
    if hashed:
        line += f" (annotator `{str(hashed)[:8]}…`)"
    return line


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
- `ref_range_unusable: true` on a lab result means the source supplied a
  reference range we had to DISCARD — OCR dropped a date into the range column.
  The measured value is unaffected and is kept; only the range is absent, and the
  flag says so rather than leaving the row silently rangeless. No flag (`L`/`H`)
  is ever derived for such a row: a flag computed against a repaired range would
  be a clinical claim built on OCR repair.
- Held-out answer key: the case's ground truth is **withheld** from the
  buyer-facing record by default; it ships (under `answer_key`) only for explicit
  benchmark exports.
"""


def _datasheet_md(*, export_id: str, profile_name: str, counts: Dict[str, Any],
                  records: List[Dict[str, Any]], contributors: List[Dict[str, Any]],
                  scope: Optional[Dict[str, Any]] = None,
                  eval_pack: Optional[Dict[str, Any]] = None) -> str:
    # No ``or "unspecified"`` fallback (Buyer Response PRD §6 E1): packaging now
    # fails closed when a credential cannot be resolved, so a None here would be a
    # bug, not a routine gap. Drop any stray None rather than manufacture a
    # confident-looking "unspecified" that contradicts the aggregate section.
    credentials = sorted({
        c for r in records
        if (c := (r.get("payload") or {}).get("annotator_credential"))
    })
    specialties = sorted(counts["by_specialty"].keys())
    type_lines = "\n".join(f"- `{k}`: {v}" for k, v in sorted(counts["by_type"].items()))
    contrib_lines = "\n".join(
        f"- {c.get('credential')} ({c.get('specialty') or 'n/a'}): "
        f"{c.get('submissions')} submissions, {c.get('total_hours')}h"
        for c in contributors
    ) or "- n/a"
    return f"""{SANDBOX_STAMP_MD if _realm.is_sandbox() else ""}# Datasheet: Archangel Health Expert Evaluation Export `{export_id}`

Generated: {datetime.utcnow().isoformat()}Z · Buyer profile: `{profile_name}`

## Motivation
Credentialed-specialist judgments comparing AI-generated answers to medical
prompts, packaged as hh-rlhf preference pairs, {{prompt, completion}} SFT
examples, and PRM800K-style step-level reasoning traces for frontier-lab training.

## Composition
- Total records: **{counts['total']}**
{type_lines}
- Specialties: {", ".join(specialties) or "n/a"}
- By product version: {", ".join(f"{k} — {v}" for k, v in sorted(counts.get('by_portal_version', {}).items())) or "n/a"} ({_portal_version_legend()})
- By modality: {", ".join(f"{k} — {v}" for k, v in sorted(counts.get('by_modality', {}).items())) or "n/a"} (text vs structured-multimodal case)
{_composition_scope_line(scope)}
{_longitudinal_scope_md(records)}
{_scope_section_md(scope)}
{_multimodal_section_md(records, counts)}
{_synthetic_provenance_md(records)}

## Collection process
Answers were evaluated in the Archangel Health portal. Each submission was
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
{_eval_pack_datasheet_md(eval_pack)}

## Limitations
- Evaluation artifacts, not medical advice; not for direct clinical use.
- Synthetic / de-identified prompts; no PHI (scanned defensively).
- Agreement reported as Cohen's κ on a double-labeled subset; single-labeled
  records carry no agreement score.

## Rights & privacy
- `contains_phi: false` (asserted + residual-identifier scanned).
- `ip_cleared: true`; `license: {_license_name()}` stamped on every record in
  this batch — commercial terms, stated on the artifact itself.
"""


def _license_name() -> str:
    from asclepius.constants import default_license
    return default_license()


def _outcome_verification_md(ov: Dict[str, Any]) -> str:
    """The longitudinal outcome-verification section, or "" when there is nothing
    to report (PRD 2 §3.4 signal 3).

    Empty rather than a row of zeros: a batch with no trajectory points has not
    measured anticipation badly, it has not measured it at all, and a table of
    zeros under a heading reads as the former.

    Placed immediately after κ and headed with what it is NOT, because these two
    numbers describe adjacent slices and the whole methodological claim rests on
    keeping them apart.
    """
    if not ov or not int(ov.get("n_points") or 0):
        return ""
    rate = ov.get("anticipation_rate")
    return f"""
## Outcome verification (longitudinal decision points — NOT κ)

The chart's own next encounter checking the physician's stated expectation. A
different statistic over a different pool: κ above is between-physician agreement;
this is one physician's anticipation checked against what the record recorded next.
Neither is ever reported under the other's name.

- Decision points in this corpus: **{ov.get('n_points')}**
- Points whose outcome was actually checkable: **{ov.get('n_points_verified')}**
- Points carrying a specialist-written falsifier: **{ov.get('n_points_with_falsifier')}**
- Expectations that held / did not hold: **{ov.get('n_expectations_held')} / {ov.get('n_expectations_did_not_hold')}**
- Expectations not assessable from the next encounter: {ov.get('n_expectations_not_assessable')}
- Physician's own falsifier fired: {ov.get('n_falsifiers_fired')}
- Anticipation rate (over assessable expectations only): **{rate if rate is not None else 'n/a — nothing was assessable'}**

Scored against the trajectory the record actually contains, which reflects the
treatment actually given rather than the plan the physician proposed. Where they
proposed something different, this does not test their plan. No figure here is a
counterfactual outcome.
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
        f"- {k} ({PORTAL_VERSION_LABELS.get(k, 'unrecognised portal version')}): {v}"
        for k, v in sorted(counts.get("by_portal_version", {}).items())
    ) or "- n/a"
    conf_lines = "\n".join(f"- {k}: {v}" for k, v in sorted(conf.items()))
    mm_section = _multimodal_quality_md(records, counts)
    qa = stats.get("qa_pass_rate") or {}
    # ``qa`` is STORE-WIDE — it comes from the whole store, not this cut — and it
    # used to print unqualified under a per-batch heading, so a 4-record batch
    # reported "37/37". Both numbers are worth having; what they are numbers ABOUT
    # is not optional.
    #
    # Do NOT reintroduce a batch-scoped PASS RATE here. Reaching ``export_ready``
    # is not evidence of QA: ``pipeline`` promotes ``submitted -> export_ready``
    # directly and logs a synthetic ``qa_checked`` event, and the human gate is
    # reached only when the critic flags, grounding flags, agreement is low, or
    # sampling fires. A batch-scoped rate would also have the batch as its own
    # denominator. The batch line below states a count of what shipped, nothing more.
    batch_subs = len({r.get("submission_id") for r in records if r.get("submission_id")})
    kappa = stats.get("kappa") or {}
    by_spec = kappa.get("by_specialty") or {}
    kappa_spec_lines = "\n".join(f"- {sp}: {v}" for sp, v in sorted(by_spec.items())) or "- n/a"
    outcome_section = _outcome_verification_md(stats.get("outcome_verification") or {})
    # Expert review vs κ: two statistics, two names (PRD A §0 / Phase 4). The
    # review rate is adjudication (reviewer SAW the labeler's answer); κ below is
    # the independent, blinded, double-labeled slice. They are never merged.
    ra = stats.get("review_acceptance") or {}

    def _ra_pct(x: Any) -> str:
        return f"{round(100 * x, 1)}%" if x is not None else "n/a"

    if ra.get("n"):
        ra_dim_lines = "\n".join(
            f"- {dim}: agree {v.get('agree', 0)} · disagree {v.get('disagree', 0)} · "
            f"cannot assess {v.get('cannot_assess', 0)}"
            for dim, v in sorted((ra.get("by_dimension") or {}).items())
        ) or "- n/a"
        review_section = f"""## Expert review (reviewer-adjudicated — NOT κ)
Senior reviewers graded these submissions with the labeler's answer visible, so
this is a review outcome, not inter-rater reliability. The independent Cohen's κ
is reported separately below.
- Expert review: accepted {_ra_pct(ra.get('accept_rate'))} · edited {_ra_pct(ra.get('edit_rate'))} · rejected {_ra_pct(ra.get('reject_rate'))} (n={ra.get('n')}, reviewer-adjudicated)
- Per-dimension verdicts ("cannot assess" reported as its own state, never folded):
{ra_dim_lines}
"""
    else:
        review_section = """## Expert review (reviewer-adjudicated — NOT κ)
- No expert reviews attached to this batch yet.
"""
    # Annotator pool, with the related-party count named (Advisor PRD §5.2).
    # Advisory physicians are NOT excluded from acceptance or from κ — their
    # labels are legitimate physician judgment and removing them would be a
    # different kind of dishonesty. They are counted, and the count is stated.
    #
    # Deliberately NOT a per-advisor acceptance rate: with n=1 advisor a
    # per-person quality score identifies him, and scoring an unpaid advisor
    # individually is a good way to stop having one.
    pool_ids = {(r.get("payload") or {}).get("annotator_id_hashed")
                for r in records} - {None, ""}
    advisory_ids = {(r.get("payload") or {}).get("annotator_id_hashed")
                    for r in records
                    if (r.get("payload") or {}).get("related_party")} - {None, ""}
    pool_line = f"Annotator pool     {len(pool_ids)} physician" \
                f"{'' if len(pool_ids) == 1 else 's'}"
    if advisory_ids:
        pool_line += f" · {len(advisory_ids)} advisory (related party)"
    flags = stats.get("flag_counts") or {}
    contributors = stats.get("contributors") or []
    contrib_lines = "\n".join(
        f"- {c.get('credential')} ({c.get('specialty') or 'n/a'}): "
        f"{c.get('submissions')} subs, {c.get('total_hours')}h, "
        f"premium {c.get('premium_submissions')} ({c.get('premium_hours')}h)"
        for c in contributors
    ) or "- n/a"
    return f"""# Quality Report: Archangel Health Export `{export_id}`

Generated: {datetime.utcnow().isoformat()}Z · Buyer profile: `{profile_name}`

## Totals by record type
- Total records: **{counts['total']}**
{type_lines}

## Annotator pool
```
{pool_line}
```
Advisory physicians hold an advisory relationship with Archangel Health,
including equity, and every record they authored carries `related_party: true`.
Their labels and reviews are counted in expert acceptance and in κ exactly like
any other physician's — their clinical judgment is not compromised by equity any
more than by an hourly rate. The count is stated so the pool is fully described.

## By product version
{portal_lines}
{mm_section}
## Grounded (evidence-anchored) premium tier
- Grounded records: **{grounded}/{counts['total']}** (**{grounded_pct}%**)

{review_section}
## Inter-annotator agreement (Cohen's κ — independently double-labeled)
- Aggregate κ (blinded, double-labeled subset, n={kappa.get('n')}): **{kappa.get('overall')}**{(' — ' + kappa['reason']) if kappa.get('overall') is None and kappa.get('reason') else ''}
- 95% CI (seeded bootstrap): {kappa.get('ci')}
- Observed agreement: {kappa.get('observed_agreement')}
- κ threshold for substantial agreement: {KAPPA_THRESHOLD}
- Minimum-n gate: {kappa.get('min_n')} · unblinded observations excluded: {kappa.get('excluded_unblinded')}
- Excluded as sequential (longitudinal decision points): {kappa.get('excluded_trajectory')}
- By specialty:
{kappa_spec_lines}
{outcome_section}

## Confidence distribution
{conf_lines}

## QA & integrity flags
- **This batch**: {batch_subs} submission(s), {counts['total']} records, all of
  which reached `export_ready`. That is a statement of what shipped, not a pass
  RATE: a submission that did not reach `export_ready` is not in this cut, so a
  per-batch rate would have the batch as its own denominator and read 1.0 always.
- Packaging outcome rate, platform-wide to date — context, NOT a statistic about
  this batch: **{qa.get('pass_rate')}** ({qa.get('passed')} of {qa.get('reviewed')}
  submissions that reached a terminal packaging outcome, i.e. `export_ready`,
  `exported` or `rejected`). Submissions still in flight are outside both numbers,
  and this figure does not assert how many submissions a human reviewed.
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
  - axes:   one or more of accuracy | completeness | safety | reasoning | grounding | \
communication (a criterion may score on several; `axis` is the deprecated first-value mirror)
  - tier:   critical | important | helpful — the criticality band (|points|: critical 8-10, \
important 4-7, helpful 1-3)

Rules:
- Judge ONLY against the listed criteria; do not invent criteria or use outside preferences.
- A positive criterion is met only if the answer clearly satisfies it. A negative criterion is \
triggered only if the answer clearly commits it.
- Be conservative and cite the exact span of the answer that satisfies/violates each criterion.
- CRITICAL-NEGATIVE HARD FAIL: a criterion with tier="critical" and negative points names a thing a \
correct answer must NEVER do. If the answer commits ANY critical negative, the answer HARD-FAILS: set \
"critical_failure" true, list the failed criteria, and report "normalized": 0.0 regardless of how many \
positive points it also earned. Still fill "score"/"max_points" with the raw weighted arithmetic for \
transparency.

Return ONLY JSON:
{
  "per_criterion": [ {"text": "<criterion text>", "points": <signed>, "tier": "<tier>", "met": true|false, "awarded": <points if met else 0>, "evidence": "<span or ''>"} ],
  "score": <sum of awarded>,
  "max_points": <sum of positive criterion points>,
  "critical_failure": true|false,
  "failed_critical_criteria": [ "<criterion text>", ... ],
  "normalized": <0.0 if critical_failure else score / max_points, 0..1>
}
"""


# ─── The CRITICAL-NEGATIVE HARD FAIL rule, as an importable function ──────────
#
# This rule had exactly one implementation and it lived INSIDE the `_SCORE_PY`
# string literal below — real source to a buyer unzipping an export bundle, and
# nothing at all to this process. `asclepius/grader_eval.py` imports
# `apply_critical_hard_fail` from this module inside a `try/except Exception`
# that returns `{"skipped": True}`, so the ImportError was swallowed on every
# call: every grader-validity probe (separation, stability, verbosity,
# hackability) silently skipped, `_rubric_is_validated` returned False for every
# rubric, and the eval pack reported `n_validated: 0` with nothing saying why.
#
# So the rule is defined here, at module level, and `_SCORE_PY` keeps its own
# textual copy — that copy has to stand alone inside a bundle we do not ship this
# module with. Two copies of ~20 lines is the price of the standalone scorer; a
# rule with no importable implementation was the price of not paying it.
# `test_grader_eval_import.py` asserts the two agree.
def _norm_criterion_text(s: Any) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _criterion_tier(c: Dict[str, Any]) -> str:
    """Criticality tier of a criterion by |points| (critical 8-10, important 4-7,
    helpful 1-3); trusts an explicit matching ``tier`` when present."""
    try:
        mag = abs(float(c.get("points") or 0.0))
    except (TypeError, ValueError):
        mag = 0.0
    derived = "critical" if mag >= 8 else "important" if mag >= 4 else "helpful"
    t = c.get("tier")
    return t if t in ("critical", "important", "helpful") and t == derived else derived


def apply_critical_hard_fail(
    result: Dict[str, Any], rubric: Dict[str, Any]
) -> Dict[str, Any]:
    """Deterministic backstop for the CRITICAL-NEGATIVE HARD FAIL rule: if the judge
    marked any critical-negative criterion as committed (met), floor normalized to 0
    and stamp critical_failure — regardless of what the model wrote for normalized.

    Mutates and returns ``result``."""
    crit_texts = {
        _norm_criterion_text(c.get("text")): c
        for c in (rubric.get("criteria") or [])
        if _points_of(c) < 0 and _criterion_tier(c) == "critical"
    }
    failed = []
    for pc in (result.get("per_criterion") or []):
        if not pc.get("met"):
            continue
        if _norm_criterion_text(pc.get("text")) in crit_texts:
            failed.append(pc.get("text"))
    if failed:
        result["critical_failure"] = True
        result["failed_critical_criteria"] = failed
        result["normalized"] = 0.0
    else:
        result.setdefault("critical_failure", False)
    return result


def _points_of(c: Dict[str, Any]) -> float:
    """The bundled copy uses a bare ``float(...)``, which raises on a malformed
    criterion. In-process this runs against LLM-authored rubrics on a live
    request, where one bad row must not take down the whole probe."""
    try:
        return float(c.get("points") or 0)
    except (TypeError, ValueError):
        return 0.0


_SCORE_PY = '''#!/usr/bin/env python3
"""Rubric-based LLM-as-judge scorer for an Archangel Health export.

Reads the rubric records from ``records.jsonl`` and scores a candidate answer
against each rubric\'s weighted criteria using an LLM judge with ``grader_prompt.txt``.

Usage:
    export ANTHROPIC_API_KEY=...           # or OPENAI_API_KEY with --provider openai
    python score.py --answer "the candidate answer text" [--task-id T] [--provider anthropic]
    python score.py --answers-file answers.jsonl   # {"task_id":..., "answer":...} per line

With no API key it prints the rubric(s) it WOULD score so the pipeline is inspectable offline.
This file is a runnable scaffold: adapt the model id / provider to your stack.
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


def _tier_of(c):
    """Criticality tier of a criterion by |points| (critical 8-10, important 4-7,
    helpful 1-3); trusts an explicit matching ``tier`` when present."""
    try:
        mag = abs(float(c.get("points") or 0.0))
    except (TypeError, ValueError):
        mag = 0.0
    derived = "critical" if mag >= 8 else "important" if mag >= 4 else "helpful"
    t = c.get("tier")
    return t if t in ("critical", "important", "helpful") and t == derived else derived


def apply_critical_hard_fail(result, rubric):
    """Deterministic backstop for the CRITICAL-NEGATIVE HARD FAIL rule: if the judge
    marked any critical-negative criterion as committed (met), floor normalized to 0
    and stamp critical_failure — regardless of what the model wrote for normalized."""
    crit_texts = {(_norm(c.get("text"))): c for c in (rubric.get("criteria") or [])
                  if float(c.get("points") or 0) < 0 and _tier_of(c) == "critical"}
    failed = []
    for pc in (result.get("per_criterion") or []):
        if not pc.get("met"):
            continue
        c = crit_texts.get(_norm(pc.get("text")))
        if c is not None:
            failed.append(pc.get("text"))
    if failed:
        result["critical_failure"] = True
        result["failed_critical_criteria"] = failed
        result["normalized"] = 0.0
    else:
        result.setdefault("critical_failure", False)
    return result


def _norm(s):
    import re
    return re.sub(r"\\s+", " ", (s or "").strip().lower())


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
    result = json.loads(text[start:end + 1]) if start != -1 else {"raw": text}
    return apply_critical_hard_fail(result, rubric) if isinstance(result, dict) and "per_criterion" in result else result


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


def _rubric_is_validated(rec: Dict[str, Any]) -> bool:
    """A rubric is VALIDATED when its package-time grader meta-eval PROVED it separates
    the chosen from the rejected answer (real separation, rejected critical-fails, not
    flagged needs_review). Mirrors value._rubric_validated exactly."""
    gv = rec.get("grader_validity") or {}
    return (bool(gv) and not gv.get("skipped") and not gv.get("needs_review")
            and bool(gv.get("rejected_critical_failed")))


def _eval_pack_summary(rubric_records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """FIX-5.2: roll the rubric records up into the eval-pack SKU descriptor — the
    validity report (how many graders were probed / validated / flagged) plus the
    recurring price (the sum of the quality-scaled rubric marginals, which is the
    reusable-grader value the buyer re-licenses per model version). Deterministic; a
    grader whose probes were skipped (no key at package time) counts as UNVALIDATED."""
    from asclepius.value import _rubric_marginal  # local: keep pricing in one place

    n = len(rubric_records)
    probed = [r for r in rubric_records if (r.get("grader_validity") or {}) and not (r.get("grader_validity") or {}).get("skipped")]
    n_validated = sum(1 for r in rubric_records if _rubric_is_validated(r))
    n_needs_review = sum(
        1 for r in rubric_records
        if r.get("needs_review") or (r.get("grader_validity") or {}).get("needs_review")
    )
    n_reliable = sum(
        1 for r in rubric_records
        if (r.get("grader_reliability") or {}) and not (r.get("grader_reliability") or {}).get("skipped")
        and not (r.get("grader_reliability") or {}).get("unreliable")
    )
    n_gameable = sum(1 for r in rubric_records if (r.get("hackability") or {}).get("gameable"))
    recurring_value = round(sum(_rubric_marginal(r) for r in rubric_records), 2)
    return {
        "sku": "asclepius_eval_pack",
        "title": "Archangel Health Rubric Eval Pack",
        "licensing": "re-licensable-per-model-version",
        "billing": "recurring",
        # The eval pack's discriminative validity is proven against ONE model version;
        # moving to a new frontier model requires re-running the validity probe, which
        # is what makes this a recurring (not one-time) line.
        "revalidation_trigger": "buyer_model_version_change",
        "recurring_value_usd": recurring_value,
        "n_rubrics": n,
        "n_probed": len(probed),
        "n_validated": n_validated,
        "n_needs_review": n_needs_review,
        "n_reliable": n_reliable,
        "n_gameable": n_gameable,
        "n_premium": sum(1 for r in rubric_records if r.get("premium")),
        "n_grounded": sum(1 for r in rubric_records if r.get("grounded")),
        "n_critical_negative": sum(1 for r in rubric_records if r.get("has_critical_negative")),
        "files": [JSONL_NAME, GRADER_PROMPT_NAME, SCORE_PY_NAME, VALIDITY_REPORT_NAME, EVAL_PACK_NAME],
    }


def _validity_report(rubric_records: List[Dict[str, Any]], summary: Dict[str, Any]) -> Dict[str, Any]:
    """The per-rubric validity/reliability/hackability meta-eval, packaged so a buyer
    can audit exactly which graders were proven to discriminate (and which need review
    before they rely on them). Only the verdict fields ship — never the raw answer key."""
    per_rubric = []
    for r in rubric_records:
        gv = r.get("grader_validity") or {}
        gr = r.get("grader_reliability") or {}
        hk = r.get("hackability") or {}
        per_rubric.append({
            "task_id": r.get("task_id") or r.get("prompt"),
            "validated": _rubric_is_validated(r),
            "needs_review": bool(r.get("needs_review") or gv.get("needs_review")),
            "grounded": bool(r.get("grounded")),
            "premium": bool(r.get("premium")),
            "has_critical_negative": bool(r.get("has_critical_negative")),
            "separation": gv.get("separation"),
            "rejected_critical_failed": gv.get("rejected_critical_failed"),
            "validity_skipped": bool(gv.get("skipped")),
            "reliability_variance": gr.get("variance"),
            "unreliable": bool(gr.get("unreliable")),
            "gameable": bool(hk.get("gameable")),
        })
    return {"summary": {k: v for k, v in summary.items() if k != "files"}, "per_rubric": per_rubric}


def _eval_pack_md(export_id: str, summary: Dict[str, Any]) -> str:
    """EVAL_PACK.md — the SKU sheet. States, in the buyer's terms, that this is a
    standalone reusable grader that re-licenses per model version (recurring), and how
    to run it. This file is what makes the eval pack legible as a separate line item."""
    files = "\n".join(f"- `{f}`" for f in summary["files"])
    return f"""# Archangel Health Rubric Eval Pack: `{export_id}`

**SKU:** `{summary['sku']}` · **Billing:** {summary['billing']} ·
**Licensing:** {summary['licensing']}

This pack is a **standalone, reusable scoring function** — physician-authored,
tiered, weighted rubrics plus a ready-to-run LLM-as-judge harness. It is sold and
reported **separately from the training data**: the data is a one-time deliverable,
the eval pack is a **recurring line**.

## Why it re-licenses per model version
Each rubric's discriminative validity was **proven at package time** against a
specific frontier model version (the grader separated the physician-chosen answer
from the rejected one, and the rejected answer critical-failed). That proof is
**version-specific** — when you move to a new model, the grader must be re-validated
against it. Re-validation is the recurring event ({summary['revalidation_trigger']}).

## What's inside
{files}

## Validity report (this batch)
- Rubrics: **{summary['n_rubrics']}** · probed at package time: **{summary['n_probed']}**
- **Validated** (proven to discriminate): **{summary['n_validated']}**
- Needs review before reliance: **{summary['n_needs_review']}**
- Reliable (low grader variance): **{summary['n_reliable']}**
- Gameable (verbose-wrong beats terse-right): **{summary['n_gameable']}**
- Premium graders: **{summary['n_premium']}** · grounded: **{summary['n_grounded']}** ·
  name a critical negative: **{summary['n_critical_negative']}**
- Recurring value (this batch): **${summary['recurring_value_usd']:.2f}**

See `{VALIDITY_REPORT_NAME}` for the per-rubric breakdown.

## Running the grader
```
export ANTHROPIC_API_KEY=...        # or OPENAI_API_KEY with --provider openai
export GRADER_MODEL=<your judge model id>
python {SCORE_PY_NAME} --answer "the candidate answer text"
```
With no key, `{SCORE_PY_NAME}` prints the rubrics it WOULD score, so the scoring
function is inspectable offline.
"""


def _eval_pack_datasheet_md(summary: Optional[Dict[str, Any]]) -> str:
    """Datasheet section reporting the eval pack as a SEPARATE recurring SKU (FIX-5.2).
    Empty when the batch carries no rubric records."""
    if not summary:
        return ""
    return f"""
## Eval pack (separate recurring SKU)
This batch includes a **rubric eval pack** (`{summary['sku']}`), reported and priced
**separately from the data**. It is a reusable scoring function (`{summary['n_rubrics']}`
rubrics; `{summary['n_validated']}` validated, `{summary['n_premium']}` premium,
`{summary['n_grounded']}` grounded) sold as a **{summary['billing']}** line and
**{summary['licensing']}** — its validity is proven per model version, so it
re-licenses whenever the buyer moves to a new frontier model. Recurring value this
batch: **${summary['recurring_value_usd']:.2f}**. See `{EVAL_PACK_NAME}`."""


# ─── Case-centric bundle (PRD A Phase 5) ─────────────────────────────────────
def _majority_verdict(tally: Dict[str, int]) -> Optional[str]:
    if not tally:
        return None
    best = max(tally.values())
    winners = [v for v, c in tally.items() if c == best]
    return winners[0] if len(winners) == 1 else None  # a tie has no majority


def _case_bundle(
    store: Any,
    emitted: List[Dict[str, Any]],
    mapped_records: List[Dict[str, Any]],
    reviews_by_sid: Dict[Any, List[Dict[str, Any]]],
    obs_by_tid: Dict[Any, Optional[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Fold the emitted (already mapped + leak-gated) records into case-keyed
    objects: one case carries the case content, every labeler submission, every
    review, and the derived consensus. Buyers asked for the case, not a
    physician's worklist — a case with two labelers plus a review is one
    artifact, not three unrelated rows (PRD A Phase 5)."""
    by_case: Dict[str, Dict[str, Any]] = {}
    tasks: Dict[str, Dict[str, Any]] = {}
    subs: Dict[str, Optional[Dict[str, Any]]] = {}
    for rec, mapped in zip(emitted, mapped_records):
        payload = rec.get("payload") or {}
        tid = rec.get("task_id") or payload.get("task_id")
        sid = rec.get("submission_id") or payload.get("submission_id")
        if tid is None or sid is None:
            continue
        if tid not in tasks:
            tasks[tid] = store.get_task(tid) or {}
        if sid not in subs:
            subs[sid] = store.get_submission(sid)
        group = by_case.setdefault(tid, {})
        sub = subs[sid] or {}
        label = group.setdefault(sid, {
            "submission_id": sid,
            "labeler_id_hashed": payload.get("annotator_id_hashed"),
            "verdict": sub.get("verdict"),
            "confidence": sub.get("confidence"),
            "portal_version": payload.get("portal_version") or sub.get("portal_version"),
            # NOTE: no ``review_status`` here. It is internal workflow state
            # ('in_review', 'not_routed', 'orphaned'), meaningless to a buyer and
            # faintly alarming in a delivered artifact (FIX A A-5.4). Whether the
            # case was reviewed is already stated by ``review.reviewed``.
            "submitted_at": sub.get("created_at"),
            # PRD 2 §4.2.3 — the falsifier is PER PHYSICIAN, so it sits on the
            # label rather than on the case: two physicians walking the same
            # decision point write two different predictions, and folding them to
            # case level would lose whose was whose.
            "trajectory": asc_packaging.trajectory_block(tasks[tid], sub),
            "records": [],
        })
        # The case-level ``review``/``supervision`` blocks are authoritative;
        # carrying them again on every embedded record repeated the same review
        # payload at three nesting levels per case (FIX A A-5.5).
        label["records"].append({k: v for k, v in mapped.items()
                                 if k not in ("review", "supervision", "trajectory")})

    cases: List[Dict[str, Any]] = []
    for tid in sorted(by_case):
        task = tasks.get(tid) or {}
        labels = sorted(by_case[tid].values(), key=lambda l: l.get("submitted_at") or "")
        case_reviews = [r for l in labels for r in reviews_by_sid.get(l["submission_id"], [])]
        tally: Dict[str, int] = {}
        for l in labels:
            if l.get("verdict"):
                tally[l["verdict"]] = tally.get(l["verdict"], 0) + 1
        obs = obs_by_tid.get(tid)
        # PRD 2 §4.2.4: a κ-excluded observation is not the independent-second-label
        # slice, blinded or not. Same rule as ``packaging.supervision_block`` —
        # written once here rather than re-derived, so the case bundle and the
        # per-record annex cannot disagree about the same observation.
        blinded_obs = (bool(obs) and (obs or {}).get("blinded") in (True, 1)
                       and not (obs or {}).get("kappa_excluded_reason"))
        cases.append({
            "case_id": tid,
            # PRD 2 §4.2.5 — the reassembly key at case level too, so a buyer can
            # group cases.jsonl by chart walk without joining through records.
            "trajectory_id": task.get("trajectory_id"),
            "sequence_index": task.get("sequence_index"),
            "specialty": task.get("specialty"),
            "difficulty": task.get("difficulty"),
            "prompt": task.get("prompt"),
            # Buyer-safe case content: public_case() inside _context holds out
            # the answer key; the leak gate re-scans the whole object below.
            "context": asc_packaging._context(task),
            "portal_versions": sorted({l.get("portal_version") for l in labels
                                       if l.get("portal_version")}),
            "n_labelers": len(labels),
            "labels": labels,
            # Same honest naming as the per-record block: adjudication under
            # ``review``, independence under ``supervision`` — never ``kappa``.
            "review": asc_packaging.review_block(case_reviews, store),
            "supervision": {"independent_second_label": blinded_obs},
            "consensus": {
                "n_labels": len(labels),
                "verdicts": tally,
                "majority_verdict": _majority_verdict(tally),
                "unanimous": len(tally) == 1 and bool(labels),
                # The stored double-label observation, when one exists — the κ
                # input for this case (verdicts only; identities stay hashed).
                "agreement_observation": {
                    "verdict_a": (obs or {}).get("verdict_a"),
                    "verdict_b": (obs or {}).get("verdict_b"),
                    "verdict_agree": bool((obs or {}).get("verdict_agree")),
                    "blinded": blinded_obs,
                    # PRD 2 §4.2.4 — why this observation is out of the κ pool,
                    # when it is. Shipped rather than silently dropped: a buyer
                    # comparing the κ denominator to the number of double-labelled
                    # cases is entitled to see which ones were excluded and why,
                    # instead of finding an unexplained gap.
                    "kappa_excluded_reason": (obs or {}).get("kappa_excluded_reason"),
                } if obs else None,
            },
        })
    return cases


def export_by_case(
    store: Any,
    *,
    created_by: Optional[str] = None,
    case_id: Optional[str] = None,
    case_ids: Optional[List[str]] = None,
    specialty: Optional[str] = None,
    portal_version: Optional[str] = None,
    include_exported: bool = True,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Bundle keyed by case_id. One case carries: the case content, every labeler
    submission, every review, and the derived consensus (PRD A Phase 5).

    SIGNATURE IS FROZEN (context pack Seam 2) — the admin export endpoint calls
    exactly this.

    ``include_exported`` defaults to **True**, unlike ``build_export`` (FIX A
    A-5.1). A case bundle that cannot see the case's earlier labels is not a
    case bundle: if labeler A's records shipped in a previous batch and labeler
    B's have not, a False default emits that case with ``n_labelers: 1`` and
    ``consensus.unanimous: true`` computed from a single label — a wrong number
    in a buyer's hands, produced silently and with no error anywhere.

    A thin, filterable entry point over ``build_export`` — one export pipeline,
    one Tier B leak gate. Emits ``records.jsonl`` (unchanged, for compatibility)
    AND ``cases.jsonl`` (case-keyed), plus the usual companions. Filters:
    ``case_id``, ``specialty``, ``portal_version`` (v3 / v4 / v5)."""
    return build_export(
        store,
        created_by=created_by,
        case_id=case_id,
        case_ids=case_ids,
        specialty=specialty,
        portal_version=portal_version,
        include_exported=include_exported,
        **kwargs,
    )


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
    case_id: Optional[str] = None,
    case_ids: Optional[List[str]] = None,
    licensed_to: Optional[str] = None,
    license_exclusivity: str = NON_EXCLUSIVE,
    license_label: Optional[str] = None,
    license_expires_at: Optional[str] = None,
    license_note: Optional[str] = None,
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
    training batch (internal demo tool).

    ``licensed_to`` names the buyer this batch is licensed to and, with
    ``license_exclusivity='exclusive'``, records an exclusive commitment over
    exactly the records that ship. Exclusivity is opt-in per deal: the default is
    non-exclusive, which is what every existing caller gets and which changes
    nothing. Whether or not this batch declares a licence, it is refused if it
    would re-ship records already committed exclusively to a DIFFERENT buyer
    (``ExclusiveLicenseConflict``)."""
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
    # ── V5 scope exports WHOLE trajectories (Longitudinal E2E PRD §5.3) ──────
    # Selecting V5 with a case id means "the walk this point belongs to", not
    # "this point". A thirteen-point chart sold as one artifact and delivered as
    # point 7 alone is not a smaller delivery of the same thing: the value is the
    # sequence, and a buyer cannot reassemble a walk they were given one link of.
    #
    # Scoped to the V5 selection deliberately. Widening every case-id export this
    # way would change what V3 and V4 mean, and there a case IS a task.
    #
    # ``case_ids`` is a PARAMETER (the Export tab's multi-case scope), so this
    # must not reset it — it may only WIDEN a single-case V5 selection into the
    # walk that case belongs to. It read ``case_ids = None`` before this merge,
    # which was harmless while the parameter did not exist and silently emptied
    # every multi-case bundle the moment it did.
    if portal_version == "v5" and case_id and not case_ids:
        anchor = store.get_task(case_id) if hasattr(store, "get_task") else None
        traj = (anchor or {}).get("trajectory_id")
        if traj and hasattr(store, "trajectory_points"):
            walk = store.trajectory_points(traj) or []
            case_ids = {p.get("task_id") for p in walk} - {None}
        # An anchor with no trajectory falls through to the ordinary single-case
        # path rather than erroring: a V5 filter on a static case id is an empty
        # slice, and "nothing matches these filters" is the honest answer.

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
            case_id=case_id,
            case_ids=set(case_ids) if case_ids else None,
        )
    ]
    if not records:
        raise ValueError("No export-ready records match the selected filters.")
    # ORDERED, and this is the product rather than tidiness. Point n's visible
    # chart is the state before point n's decision and point n+1's contains what
    # happened after it, so a walk delivered out of order reads as a contradictory
    # patient. Records with no trajectory keep their existing relative order — the
    # sort key is constant for them and Python's sort is stable.
    records.sort(key=lambda r: _trajectory_sort_key(r))

    export_id = _new_export_id()
    exported_at = datetime.utcnow().isoformat()
    # Resolved once per batch, not per record: one bundle ships under one license.
    from asclepius.constants import default_license as _default_license
    _license_terms = _default_license()

    # 1. Map + validate EVERY line before writing anything (fail loud, fail whole).
    lines: List[str] = []
    emitted: List[Dict[str, Any]] = []
    mapped_objs: List[Dict[str, Any]] = []  # parallel to ``emitted`` (Phase 5 case bundle)
    # Review + supervision blocks (PRD A Phase 3) are hydrated at emit time —
    # reviews land after packaging — and cached per submission/task so a batch
    # with many records per submission does one lookup each.
    _reviews_by_sid: Dict[Any, List[Dict[str, Any]]] = {}
    _obs_by_tid: Dict[Any, Optional[Dict[str, Any]]] = {}
    # Same caching discipline for the longitudinal annex (PRD 2 §4.2.5): the task
    # carries the walk identity, the submission carries the falsifier, and a
    # thirteen-point chart would otherwise re-read both on every record.
    _tasks_by_tid: Dict[Any, Optional[Dict[str, Any]]] = {}
    _subs_by_sid: Dict[Any, Optional[Dict[str, Any]]] = {}
    # Related-party disclosure on records packaged before the field existed
    # (audit H3). Packaging runs once, at submit, so the entire back catalogue
    # has no key — and the data dictionary now documents one. Filled HERE, at
    # emit, so no line in a shipped file is missing it; resolved from the
    # annotating physician's relationship rather than defaulted to False, which
    # would strip the qualifier off every historical record at once. Cached per
    # submission so a batch with many records per submission does one lookup.
    _rp_by_sid: Dict[Any, bool] = {}
    for rec in records:
        payload = dict(rec.get("payload") or {})
        payload.pop("record_id", None)
        payload["exported_at"] = exported_at
        # THE LICENSE IN FORCE AT SHIP TIME (PRD §2.3). Packaging stamps the
        # license when a record is captured, so the entire back catalogue carries
        # ``CC-BY-NC-4.0-clinical-eval`` — NON-commercial, on data sold to train
        # commercial models. Re-stamped here, at emit, for the same reason
        # ``exported_at`` is: it is a property of THIS shipment. Nothing in the
        # ``records`` table is rewritten (§0: no destructive migration), so a
        # record's captured payload stays exactly as it was captured, and every
        # line that leaves the building carries one consistent, correct license.
        payload["license"] = _license_terms
        if "related_party" not in payload:
            _sid = rec.get("submission_id") or payload.get("submission_id")
            if _sid not in _rp_by_sid:
                _rp_by_sid[_sid] = asc_packaging.backfill_related_party(
                    payload, rec, store)
            payload["related_party"] = _rp_by_sid[_sid]
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
        # Case-centric review metadata (PRD A Phase 3): the record carries the
        # original labeling AND every expert review of it, plus who supervised
        # it. Attached after schema validation (the same seam as answer_key)
        # but BEFORE the leak gate, so the gate scans these blocks for stray
        # Tier B keys too. Deliberately NOT under a kappa/agreement key: the
        # reviewer saw the labeler's answer, so this is adjudication, not
        # inter-rater agreement (PRD A §0).
        sid = rec.get("submission_id")
        if sid not in _reviews_by_sid:
            _reviews_by_sid[sid] = store.reviews_for_submission(sid) if sid else []
        tid = rec.get("task_id") or payload.get("task_id")
        if tid not in _obs_by_tid:
            _obs_by_tid[tid] = store.get_agreement_observation(tid) if tid else None
        mapped["review"] = asc_packaging.review_block(_reviews_by_sid[sid], store)
        mapped["supervision"] = asc_packaging.supervision_block(
            labeler_id_hashed=payload.get("annotator_id_hashed"),
            observation=_obs_by_tid[tid],
        )
        # Longitudinal annex (PRD 2 §4.2.5): the reassembly key + the falsifier.
        # The bundle is PER-RECORD, so without ``trajectory_id`` and
        # ``sequence_index`` on the line itself, thirteen decision points from one
        # chart arrive as thirteen disconnected rows — thirteen single-shot cases
        # sold at a trajectory price. Same seam as ``review``/``supervision``:
        # after schema validation, before the leak gate, so the gate scans it.
        if tid not in _tasks_by_tid:
            _t = store.get_task(tid) if tid else None
            # §8.7 provenance, resolved once per task rather than per record: a
            # point that changed hands mid-walk puts a substitution in the handoff
            # chain, which is a fact about the data a buyer is entitled to.
            if _t and _t.get("trajectory_id"):
                _t = dict(_t)
                _t["_reassigned"] = store.point_was_reassigned(tid)
            _tasks_by_tid[tid] = _t
        if sid not in _subs_by_sid:
            _subs_by_sid[sid] = store.get_submission(sid) if sid else None
        _traj = asc_packaging.trajectory_block(_tasks_by_tid[tid], _subs_by_sid[sid])
        if _traj:
            mapped["trajectory"] = _traj
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
        mapped_objs.append(mapped)

    if not emitted:
        raise ValueError(
            f"No records match the buyer profile {profile_name!r} record types."
        )

    # Exclusivity gate (audit U5). Placed here and not earlier because ``emitted``
    # is the first point at which we know precisely which records would leave the
    # building: ``records`` still contains lines the buyer profile drops. Placed
    # here and not later because nothing below this point is undoable. The next
    # statement creates the bundle directory, and after that the records are
    # marked exported and the submissions move on.
    licensed_key = (licensed_to or "").strip().lower() or None
    emitted_ids = [r["record_id"] for r in emitted]
    enforce_exclusivity(store, emitted_ids, licensed_to=licensed_key)

    out_dir = export_root() / export_id
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    # 2. JSONL
    jsonl_text = "".join(line + "\n" for line in lines)
    jsonl_path = out_dir / JSONL_NAME
    jsonl_path.write_text(jsonl_text, encoding="utf-8")

    # 2b. Case-keyed bundle (PRD A Phase 5): records.jsonl stays unchanged for
    # compatibility; cases.jsonl folds the same emitted records into one object
    # per case (every labeler + every review + consensus). Same CORE RULE: every
    # case line passes the Tier B leak gate or the whole batch is rejected.
    cases = _case_bundle(store, emitted, mapped_objs, _reviews_by_sid, _obs_by_tid)
    case_lines: List[str] = []
    for case_obj in cases:
        leak = asc_credentials.find_tier_b_leak(case_obj)
        if leak is not None:
            raise ExportValidationError(
                f"Tier B leak: case {case_obj.get('case_id')} contains the identifying "
                f"field {leak!r} in the case bundle. Batch rejected."
            )
        if verify_values:
            vleak = asc_credentials.find_tier_b_value_leak(case_obj, verify_values)
            if vleak is not None:
                raise ExportValidationError(
                    f"Tier B value leak: case {case_obj.get('case_id')} contains a "
                    f"private-vault value ({vleak!r}) in the case bundle. Batch rejected."
                )
        case_lines.append(json.dumps(case_obj, ensure_ascii=False, sort_keys=True))
    (out_dir / CASES_NAME).write_text(
        "".join(line + "\n" for line in case_lines), encoding="utf-8"
    )

    # 3. stats for the quality report
    contributors = store.contributor_stats()
    kappa = asc_agreement.aggregate_kappa(store.list_agreement_observations())
    # External adjudication (Buyer Response PRD §7 F3): agreement between the referring
    # institution's sealed adjudication and our physician's independent answer — a
    # cross-institution signal, reported separately and under the same 30-obs gate.
    try:
        ext_pairs = store.external_adjudication_pairs()
    except Exception:
        ext_pairs = []
    external_adjudication = asc_agreement.external_adjudication_agreement(ext_pairs)
    # Outcome verification (PRD 2 §3.4 signal 3). A THIRD separately named
    # statistic, for the same reason review acceptance is the second: it measures a
    # different thing over a different pool. κ is between-physician agreement on the
    # blinded double-labelled slice; this is one physician's anticipation checked
    # against what the chart actually recorded next. Trajectory points are absent
    # from the κ denominator by construction (§4.2.4) and present here; neither
    # number is ever reported under the other's label.
    try:
        outcome_verification = asc_trajectory.outcome_verification(
            store.trajectory_verification_points())
    except Exception:   # pragma: no cover - a store without the columns yet
        outcome_verification = asc_trajectory.outcome_verification([])
    # Two statistics, named correctly (PRD A Phase 4). ``kappa`` above IS the
    # independent κ (blinded double-labeled slice, min-n gated). Review
    # acceptance is a DIFFERENT statistic — expert adjudication over this
    # batch's reviews, where the reviewer saw the labeler's answer — and is
    # never reported under a κ label.
    _batch_reviews = [r for revs in _reviews_by_sid.values() for r in revs]
    review_acceptance = asc_agreement.review_acceptance(_batch_reviews)
    stats = {
        "status_counts": store.status_counts(),
        "qa_pass_rate": store.qa_pass_rate(),
        "average_agreement": store.average_agreement(),
        "kappa": kappa,
        "review_acceptance": review_acceptance,
        "external_adjudication_agreement": external_adjudication,
        "outcome_verification": outcome_verification,
        "flag_counts": _flag_counts(store),
        "contributors": contributors,
    }
    counts = _counts(emitted)

    # Eval-pack SKU descriptor (FIX-5.2): computed here so the datasheet can report it
    # as a separate recurring line. Empty when the batch carries no rubric records.
    _rubric_records = [r for r in emitted if r.get("type") == "rubric"]
    eval_pack_summary = _eval_pack_summary(_rubric_records) if _rubric_records else None

    # 4. companions
    (out_dir / DICTIONARY_NAME).write_text(_data_dictionary_md(profile_name), encoding="utf-8")
    (out_dir / DATASHEET_NAME).write_text(
        _datasheet_md(
            export_id=export_id, profile_name=profile_name, counts=counts,
            records=emitted, contributors=contributors, scope=scope,
            eval_pack=eval_pack_summary,
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
    companion_files.append(CASES_NAME)  # case-keyed bundle (PRD A Phase 5)
    if _rubric_records:
        (out_dir / GRADER_PROMPT_NAME).write_text(_GRADER_PROMPT, encoding="utf-8")
        (out_dir / SCORE_PY_NAME).write_text(_SCORE_PY, encoding="utf-8")
        companion_files += [GRADER_PROMPT_NAME, SCORE_PY_NAME]
        # Eval pack (FIX-5.2): the validity report + SKU sheet turn the grader files
        # into a standalone, re-licensable-per-model-version recurring SKU, reported
        # separately in the manifest (`eval_pack`) and datasheet.
        (out_dir / VALIDITY_REPORT_NAME).write_text(
            json.dumps(_validity_report(_rubric_records, eval_pack_summary), indent=2, ensure_ascii=False),
            encoding="utf-8")
        (out_dir / EVAL_PACK_NAME).write_text(
            _eval_pack_md(export_id, eval_pack_summary), encoding="utf-8")
        companion_files += [VALIDITY_REPORT_NAME, EVAL_PACK_NAME]

    # Model-Failure Taxonomy export (PRD §D): the targeted-eval artifact — a scored,
    # provider-attributed, physician-verified answer to "here is precisely how frontier
    # models fail on this class of case". V3/V4 only; emitted when the batch has any
    # physician failure observations. Small-N cells suppressed; κ label-agreement shipped.
    _taxonomy_meta: Optional[Dict[str, Any]] = None
    try:
        from asclepius import failure_taxonomy as _ft
        _bundle = _ft.build_failure_taxonomy(store, specialty=specialty)
        if _bundle["aggregate"]["n_observations"] > 0:
            _tax_doc = {
                "mode_definitions": _bundle["mode_definitions"],
                "label_agreement": _bundle["label_agreement"],
                "provenance": _bundle["provenance"],
                **_bundle["aggregate"],
            }
            (out_dir / "model_failure_taxonomy.json").write_text(
                json.dumps(_tax_doc, indent=2), encoding="utf-8")
            (out_dir / "TAXONOMY.md").write_text(_ft.taxonomy_markdown(_bundle), encoding="utf-8")
            _eval_dir = out_dir / "failure_eval"
            _eval_dir.mkdir(exist_ok=True)
            (_eval_dir / "holdout.json").write_text(json.dumps(_bundle["holdout"], indent=2), encoding="utf-8")
            (_eval_dir / "score_failuremode.py").write_text(_ft.SCORE_FAILUREMODE_PY, encoding="utf-8")
            companion_files += ["model_failure_taxonomy.json", "TAXONOMY.md",
                                "failure_eval/holdout.json", "failure_eval/score_failuremode.py"]
            _taxonomy_meta = {"n_observations": _bundle["aggregate"]["n_observations"],
                              "n_attributed": _bundle["aggregate"]["n_attributed"],
                              "label_agreement": _bundle["provenance"]["label_agreement"],
                              "n_physicians": _bundle["provenance"]["n_physicians"],
                              "min_cell_n": _bundle["aggregate"]["min_cell_n"],
                              "human_verified": True}
    except Exception:  # a taxonomy failure must never break the core export
        log.exception("asclepius: failure taxonomy export failed")

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
        # Widens the cut to records already shipped in an earlier batch. It
        # belongs here because the unscoped manifest scope says "the whole
        # eligible set under `filters`", and without this that sentence claims
        # more than `filters` can account for.
        "include_exported": include_exported,
        # A submission-scoped cut recorded its narrowing nowhere. Every caller
        # that passes it also passes an explicit ``scope``, so this cannot rescue
        # the unscoped label — it is here because a filter that selected the rows
        # belongs with the filters.
        "submission_id": submission_id,
        "mock_excluded": (not include_mock and bool(mock_ids)),
        "annotator_id_hashed": annotator_id_hashed,
        "annotator_ids": sorted(annotator_id_set) if annotator_id_set else None,
        "case_id": case_id,
        "case_ids": sorted(case_ids) if case_ids else None,
    }
    content_hashes = {JSONL_NAME: _sha256_text(jsonl_text)}
    for name in companion_files:
        if name in (JSONL_NAME, MANIFEST_NAME):
            continue
        content_hashes[name] = _sha256_text((out_dir / name).read_text(encoding="utf-8"))
    # Image assets (V4 Image Embedding PRD §8): bundle the CLEANED image bytes with the
    # record set so a buyer can use the reasoning trace about the image, referenced by
    # asset_id + sha256 (integrity). Blinding holds — stripped-metadata images only, no
    # provider/model/partner identity. Best-effort: a missing blob is skipped, never
    # fatal.
    image_assets = _collect_and_write_image_assets(emitted, out_dir)
    # What this bundle IS, stated plainly and derived from the emitted records.
    # These lived only indirectly before — specialty in ``scope``, portal version in
    # ``counts.by_portal_version``, and the license NOWHERE in the manifest unless
    # the cut was against a licensed key, even though every record carries one. A
    # consumer reading batch.json alone could not name the terms it shipped under,
    # and ``scripts/export_audit.py`` failed every unlicensed bundle on exactly that.
    manifest = {
        "export_id": export_id,
        "created_at": exported_at,
        "created_by": created_by,
        "profile": profile_name,
        # ``_license_terms`` is the license stamped onto every emitted line at
        # ship time; the manifest states that same value, never the captured one.
        "license": _license_terms,
        # Singular = the one value EVERY shipped line carries, else None. Plural =
        # what the bundle actually contains. A two-specialty cut is a normal thing
        # to sell: it has no single specialty, and saying so must not read as
        # "this bundle does not record its specialties", which is what the audit
        # gate concluded from the singular key alone.
        "specialty": _sole_shipped_value(emitted, mapped_objs, prof, "specialty"),
        # ``key=str`` keeps the sort total. These are raw payload values now, not
        # ``_counts``' coerced string keys, and a batch mixing types would raise
        # here — after ``records.jsonl`` is written and records are marked
        # exported, in the region the code above marks as not undoable.
        "specialties": sorted((v for v in _shipped_values(
            emitted, mapped_objs, prof, "specialty") if v is not None), key=str),
        "portal_version": _sole_shipped_value(emitted, mapped_objs, prof, "portal_version"),
        "portal_versions": sorted((v for v in _shipped_values(
            emitted, mapped_objs, prof, "portal_version") if v is not None), key=str),
        "preference_variant": prof.get("preference_variant", "flat"),
        "record_count": len(emitted),
        "submission_count": len({r["submission_id"] for r in emitted}),
        # One entry per case in cases.jsonl (PRD A Phase 5).
        "case_count": len(cases),
        "counts": counts,
        "grounded_count": sum(1 for r in emitted if (r.get("payload") or {}).get("grounded")),
        "multimodal_count": sum(1 for r in emitted if _rec_modality(r) == "multimodal"),
        # Image assets bundled with this export (V4 Image PRD §8): {asset_id, sha256,
        # modality, mime, license, provenance, path}. Empty for text-only batches.
        "image_assets": image_assets,
        "image_asset_count": len(image_assets),
        # Renamed from ``synthetic_prompt_count`` (PRD §2.3). "Synthetic" reads,
        # to a buyer, as "made-up case" — and it never meant that. It counts
        # records whose QUESTION was model-authored; the case underneath may be a
        # real de-identified chart. ``case_provenance`` sits beside it saying
        # which, so "real case, model-generated question" reads as what it is
        # instead of as a synthetic dataset.
        "model_generated_question_count": len(_synthetic_records(emitted)),
        "case_provenance": _case_provenance(emitted),
        # The old key, kept alongside for one release so a buyer's ingest script
        # written against it does not break on the rename. New consumers read
        # ``model_generated_question_count``.
        "synthetic_prompt_count": len(_synthetic_records(emitted)),
        # Tri-state: true (all synthetic prompts from a ratified corpus), false
        # (some unratified — see datasheet warning), or null (no synthetic prompts).
        "seed_corpus_ratified": _seed_corpus_ratified(emitted),
        "kappa": kappa,
        # Expert-review adjudication over this batch (PRD A Phase 4) — a
        # separate statistic from κ, under its own name, on purpose.
        "review_acceptance": review_acceptance,
        "filters": filters,
        "note": note,
        # An unscoped cut still has a scope: the whole eligible set under
        # ``filters``. Recorded as such rather than as null, because
        # ``scripts/export_audit.py`` fails a bundle whose scope is not written
        # down — "a bundle whose scope is not recorded cannot be reproduced or
        # corrected later" — and a plain cut is exactly as much in need of that as
        # a case-scoped one. Only the manifest key is defaulted: the datasheet's
        # scope section still renders from ``scope`` itself, so an unscoped export
        # gains no "Contributor scope" prose it did not have before.
        "scope": scope if scope is not None else {
            "type": "unscoped",
            "label": "the whole eligible set under `filters`",
            "record_count": len(emitted),
            "case_count": len(cases),
        },
        "tier_b_leak_gate": "passed",
        "files": companion_files,
        "content_hashes": content_hashes,
        "rubric_count": len(_rubric_records),
        # Eval pack (FIX-5.2): the reusable-grader SKU, reported SEPARATELY from the
        # one-time data sale — recurring, re-licensable per model version. Absent when
        # the batch carries no rubric records.
        "eval_pack": eval_pack_summary,
        # Model-Failure Taxonomy provenance (PRD §D-4): the trust certificate for the
        # targeted eval (physician count, κ label agreement, small-N floor). Absent when
        # the batch produced no failure observations.
        "model_failure_taxonomy": _taxonomy_meta,
        "dir_path": str(out_dir),
        "destination": "local_disk",  # future seam: a cloud writer pushes here.
    }
    # Licensing terms travel WITH the bundle, and only when there are terms. An
    # undeclared export writes the same manifest keys it always has, so nothing
    # already in flight sees a changed file; a licensed one carries the buyer and
    # the exclusivity on the copy that actually leaves the building, which is the
    # copy a dispute is argued over.
    license_id: Optional[str] = None
    if licensed_key:
        license_id = "lic-" + export_id[4:] if export_id.startswith("exp-") else "lic-" + export_id
        manifest["licensing"] = {
            "license_id": license_id,
            "licensed_to": license_label or licensed_key,
            "exclusivity": (EXCLUSIVE if license_exclusivity == EXCLUSIVE else NON_EXCLUSIVE),
            "expires_at": license_expires_at,
        }
    (out_dir / MANIFEST_NAME).write_text(
        json.dumps(_shippable_manifest(manifest), indent=2, ensure_ascii=False),
        encoding="utf-8",
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
    if licensed_key and license_id:
        store.create_export_license(
            license_id=license_id,
            export_id=export_id,
            buyer_key=licensed_key,
            buyer_label=license_label,
            exclusivity=(EXCLUSIVE if license_exclusivity == EXCLUSIVE else NON_EXCLUSIVE),
            record_ids=emitted_ids,
            case_ids=sorted({(r.get("task_id") or (r.get("payload") or {}).get("task_id"))
                             for r in emitted} - {None}),
            expires_at=license_expires_at,
            note=license_note,
            created_by=created_by,
        )
        store.log_event(
            entity_type="export", entity_id=export_id, event_type="export_licensed",
            actor=created_by,
            payload={"license_id": license_id, "buyer_key": licensed_key,
                     "exclusivity": (EXCLUSIVE if license_exclusivity == EXCLUSIVE
                                     else NON_EXCLUSIVE),
                     "record_count": len(emitted_ids),
                     "expires_at": license_expires_at},
        )
    store.log_event(
        entity_type="export", entity_id=export_id, event_type="export_built",
        actor=created_by, payload={"record_count": len(emitted), "filters": filters},
    )
    return manifest


# Manifest keys that are OURS, not the buyer's. `batch.json` is a file inside a
# bundle we hand to an outside lab, and it carried an internal admin user id
# (`created_by`) plus an absolute path on our own server (`dir_path`) — neither of
# which describes the data, and both of which describe us. The admin/advisor views
# already stripped exactly these (see routers/asclepius_advisor.py); the file
# itself did not, which is the copy that actually leaves the building.
#
# They stay on the returned dict and on the stored export row: that is the audit
# trail, and it is internal.
_INTERNAL_MANIFEST_KEYS = ("created_by", "dir_path", "destination")


def _shippable_manifest(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """The manifest as a BUYER sees it — everything about the data, nothing about
    the operator who built it or the machine it was built on."""
    return {k: v for k, v in manifest.items() if k not in _INTERNAL_MANIFEST_KEYS}


def _mime_ext(mime: str) -> str:
    return {"image/png": ".png", "image/jpeg": ".jpg", "application/pdf": ".pdf"}.get((mime or "").lower(), ".bin")


def _collect_and_write_image_assets(records: List[Dict[str, Any]], out_dir: "Path") -> List[Dict[str, Any]]:
    """Collect image-bearing studies from the emitted records, write each CLEANED blob
    to ``<out_dir>/assets/<sha256><ext>`` (deduped), and return the manifest entries
    (V4 Image PRD §8). Every asset carries provenance + the V4 real-record license.
    Best-effort — a missing/unreadable blob is skipped, never fatal."""
    try:
        from asclepius.cases import study_has_valid_asset
        from asclepius.assets import load_asset
        from asclepius.constants import default_license
    except Exception:  # pragma: no cover
        return []
    license_terms = None
    try:
        license_terms = default_license()
    except Exception:
        license_terms = None
    assets_dir = out_dir / "assets"
    seen: Dict[str, Dict[str, Any]] = {}
    for r in records:
        case = ((r.get("payload") or {}).get("context") or {}).get("case") or {}
        for s in (case.get("studies") or []):
            if not (isinstance(s, dict) and study_has_valid_asset(s)):
                continue
            a = s.get("asset") or {}
            sha = a.get("sha256")
            if not sha or sha in seen:
                continue
            fname = "assets/" + sha + _mime_ext(a.get("mime"))
            try:
                data, _mime = load_asset(a)
                assets_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / fname).write_bytes(data)
            except Exception:  # missing/corrupt blob — reference it but don't bundle
                fname = None
            seen[sha] = {
                "asset_id": a.get("asset_id"), "sha256": sha,
                "modality": str(s.get("modality") or "").lower(),
                "mime": a.get("mime"), "byte_size": a.get("byte_size"),
                "license": license_terms,
                "provenance": a.get("source") or "partner_deidentified",
                "path": fname,
            }
    return list(seen.values())


#: Sandbox PRD §1.4: a bundle built in the sandbox says so in its filename and
#: at the top of its datasheet, so a file that is ever moved out of the sandbox
#: cannot be mistaken for a deliverable.
SANDBOX_STAMP = "SANDBOX — not a deliverable"
SANDBOX_STAMP_MD = f"> **{SANDBOX_STAMP}.** This bundle was built in the sandbox realm " \
                   "from sandbox data. It must not be shipped to anyone.\n\n"


def bundle_filename(export_id: str) -> str:
    """The download name for an export archive, realm-stamped in the sandbox."""
    if _realm.is_sandbox():
        return f"SANDBOX-not-a-deliverable-{export_id}.zip"
    return f"{export_id}.zip"


def zip_export(export: Dict[str, Any]) -> bytes:
    """Zip an export directory into an in-memory archive for download."""
    dir_path = Path(export.get("dir_path") or "")
    manifest = export.get("manifest") or {}
    # Use the manifest's actual file list (may include the FEAT-2 grader files);
    # fall back to the base companions for older manifests.
    files = manifest.get("files") or _COMPANION_FILES
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if dir_path.is_dir():
            for name in files:
                fp = dir_path / name
                if fp.exists():
                    zf.write(fp, arcname=name)
            # Bundle the image assets (V4 Image PRD §8) — cleaned bytes only.
            for ia in (manifest.get("image_assets") or []):
                p = ia.get("path")
                if p and (dir_path / p).exists():
                    zf.write(dir_path / p, arcname=p)
        else:
            # The fallback branch: the export directory is gone (purged, or lost
            # to a redeploy on ephemeral storage) so the manifest is rebuilt from
            # the STORED row — which is the internal one, carrying created_by and
            # an absolute server path. This is the copy a buyer downloads, so it
            # gets the same strip as the on-disk file. Sanitizing only the
            # directory copy left the leak on the path that actually delivers.
            zf.writestr(MANIFEST_NAME,
                        json.dumps(_shippable_manifest(manifest), indent=2))
    buf.seek(0)
    return buf.read()
