"""
Clinical safety inspector for generated voice scripts.

Runs coverage (omission) and faithfulness (fabrication) checks via an
independent LLM-as-judge call before ElevenLabs synthesis.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ValidationError

from ai.llm_client import call_llm, first_text
from ai.model_config import resolve

GROUNDING_PROMPT_V = "2026-05-31.1"
GROUNDING_JUDGE_MODEL = resolve("grounding_judge")["model"]

VALID_TRACKS = frozenset({"pre_op", "post_op_diagnosis", "post_op_treatment"})

GROUNDING_JUDGE_PROMPT = """You are a clinical safety reviewer. You audit a patient-education VOICE SCRIPT
against the SOURCE clinical data it was generated from. You do not rewrite the
script. You produce a structured audit only.

You are given:
- TRACK: pre_op | post_op_diagnosis | post_op_treatment
- SOURCE: the structured clinical facts (the ONLY source of truth)
- REQUIRED_ITEMS: a pre-computed list of items that MUST appear in the script,
  each with an id, category, text, and severity
- SCRIPT: the generated voice script to audit

Do TWO independent jobs:

1. COVERAGE. For each entry in REQUIRED_ITEMS, decide whether the SCRIPT conveys
   it. Match on MEANING, not exact words (the script is plain-language and spells
   out numbers, e.g. "five hundred milligrams" for 500 mg). Mark each:
   - COVERED  : clearly conveyed, including any required specific (dose, action)
   - PARTIAL  : mentioned but missing a critical specific (drug named but dose
                omitted; a red flag stated without the action to take; a
                stop/continue instruction stated without its timing)
   - MISSING  : absent
   For COVERED/PARTIAL, include a short verbatim quote from the SCRIPT as evidence.

2. FAITHFULNESS. Scan the SCRIPT for every clinical SPECIFIC it asserts:
   medication names, doses, frequencies, routes, provider names, follow-up dates,
   numeric thresholds (temperatures, time windows, weight limits), activity
   restrictions, and diagnoses. For each, decide:
   - SUPPORTED   : the specific is present in SOURCE and matches it exactly
   - UNSUPPORTED : the specific is NOT in SOURCE, or CONTRADICTS / DRIFTS from it
                   (e.g. SOURCE says stop a drug but SCRIPT says continue; SOURCE
                   fever cutoff 100.4F but SCRIPT says 101.4F; SOURCE 500 mg but
                   SCRIPT 5000 mg; a provider/date/dose absent from SOURCE)
   For SUPPORTED, cite the matching SOURCE field. UNSUPPORTED specifics are
   hallucinations or dangerous drifts.

Pay special attention to NEAR-MISSES that look almost right: a single digit or
decimal changed in a dose; a temperature cutoff shifted across the call-the-
doctor line; a "stop" flipped to "continue" (or vice-versa) for an anticoagulant
or diabetes drug; a follow-up moved from days to weeks; a provider name that
sounds plausible but is wrong; a sound-alike drug name. These are the dangerous
failures. Do not let plain-language paraphrase hide them — compare the underlying
clinical fact.

HARD CRITICAL RULES (any of these -> critical failure):
- A provider name in the SCRIPT that is not in SOURCE.
- A medication, dose, frequency, or route in the SCRIPT that is not in SOURCE or
  differs from it.
- A follow-up date in the SCRIPT that is not in SOURCE or differs from it.
- A numeric clinical threshold in the SCRIPT (temperature, time window, weight
  limit, max daily dose) that differs from SOURCE.
- A stop/continue/hold medication direction in the SCRIPT that reverses SOURCE.
- A recommendation that contradicts a SOURCE allergy or contraindication.
- Any REQUIRED_ITEM with severity CRITICAL that is MISSING or PARTIAL.

VERDICT:
- BLOCK  : one or more critical failures. Not safe to ship.
- REVIEW : no critical failures, but one or more MAJOR coverage gaps or
           UNSUPPORTED non-critical specifics. Needs human sign-off.
- PASS   : full critical + major coverage, no unsupported specifics. Minor gaps ok.

Return ONLY this JSON, no prose, no markdown fences:
{
  "track": "<track>",
  "coverage": [
    {"id":"...","category":"...","status":"COVERED|PARTIAL|MISSING",
     "severity":"CRITICAL|MAJOR|MINOR","evidence":"<script quote or null>"}
  ],
  "faithfulness": [
    {"claim":"...","claim_type":"medication|dose|frequency|route|doctor_name|date|threshold|restriction|diagnosis|allergy|other",
     "status":"SUPPORTED|UNSUPPORTED","source_evidence":"<source field or null>",
     "severity":"CRITICAL|MAJOR|MINOR"}
  ],
  "critical_failures": ["short description", "..."],
  "verdict": "PASS|REVIEW|BLOCK",
  "summary": "one sentence"
}"""


class GroundingReport(BaseModel):
    track: str
    coverage: list[dict]
    faithfulness: list[dict]
    critical_failures: list[str]
    verdict: Literal["PASS", "REVIEW", "BLOCK"]
    summary: str
    required_items: list[dict] = []
    model: str = GROUNDING_JUDGE_MODEL
    prompt_version: str = GROUNDING_PROMPT_V


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")[:48]


def _med_status_direction(status: str) -> Optional[str]:
    s = (status or "").lower().strip()
    if s in ("stop", "hold"):
        return "stop"
    if s == "continue":
        return "continue"
    return None


def _has_npo_or_fasting(d: Dict[str, Any]) -> bool:
    combined = " ".join(
        str(d.get(k) or "")
        for k in ("diet_instructions", "pre_op_instructions")
    ).lower()
    return any(
        kw in combined
        for kw in ("npo", "nothing to eat", "no solid", "fasting", "after midnight", "clear liquid")
    )


def build_required_items(structured_data: Dict[str, Any], track: str) -> List[Dict[str, Any]]:
    """Derive mandatory checklist items from structured_data only."""
    if track not in VALID_TRACKS:
        raise ValueError(f"unknown track: {track}")

    d = structured_data or {}
    items: List[Dict[str, Any]] = []

    def add(item_id: str, category: str, text: str, severity: str) -> None:
        items.append({"id": item_id, "category": category, "text": text, "severity": severity})

    if track == "pre_op":
        for i, m in enumerate(d.get("medications") or []):
            direction = _med_status_direction(m.get("status", ""))
            if direction:
                name = m.get("name") or "medication"
                notes = m.get("notes") or ""
                timing = f" ({notes})" if notes else ""
                add(
                    f"preop_med_{i}_{_slug(name)}",
                    "medication",
                    f"medication {name}: {direction} instruction must be stated{timing}",
                    "CRITICAL",
                )

        if _has_npo_or_fasting(d):
            diet = d.get("diet_instructions") or ""
            pre = d.get("pre_op_instructions") or ""
            window = diet or pre
            add(
                "preop_npo",
                "diet",
                f"fasting / NPO instruction must be stated ({window[:120]})",
                "CRITICAL",
            )

        act = d.get("activity_restrictions")
        if isinstance(act, str) and act.strip():
            add("preop_activity_text", "activity", act.strip(), "MAJOR")
        elif isinstance(act, list):
            for i, restriction in enumerate(act):
                if isinstance(restriction, str) and restriction.strip():
                    add(f"preop_activity_{i}", "activity", restriction.strip(), "MAJOR")
                elif isinstance(restriction, dict) and restriction.get("text"):
                    add(f"preop_activity_{i}", "activity", str(restriction["text"]), "MAJOR")

        for i, flag in enumerate(d.get("red_flags") or []):
            if flag:
                add(f"preop_red_flag_{i}", "red_flag", f"pre-op warning sign: {flag}", "CRITICAL")

        proc_date = d.get("procedure_date")
        pre_inst = d.get("pre_op_instructions") or ""
        if proc_date or pre_inst:
            add(
                "preop_logistics",
                "logistics",
                f"arrival/logistics and procedure date ({proc_date or 'see instructions'}) must be stated",
                "MAJOR",
            )

        for i, allergy in enumerate(d.get("allergies") or []):
            if allergy:
                add(f"preop_allergy_{i}", "allergy", f"allergy: {allergy}", "CRITICAL")

    elif track == "post_op_treatment":
        for i, m in enumerate(d.get("medications") or []):
            status = (m.get("status") or "").lower()
            name = m.get("name") or "medication"
            if status in ("new", "changed"):
                dose = m.get("dose") or ""
                freq = m.get("frequency") or ""
                add(
                    f"tx_med_{i}_{_slug(name)}",
                    "medication",
                    f"new/changed medication {name} with name + dose ({dose}) + frequency ({freq})",
                    "CRITICAL",
                )
            notes = (m.get("notes") or "").strip()
            if notes and any(kw in notes.lower() for kw in ("critical", "do not", "warning", "never", "important")):
                add(f"tx_med_notes_{i}", "medication", f"medication warning for {name}: {notes}", "CRITICAL")

        act = d.get("activity_restrictions")
        if isinstance(act, str) and act.strip():
            add("tx_activity", "activity", act.strip(), "MAJOR")
        elif isinstance(act, list):
            for i, restriction in enumerate(act):
                if restriction:
                    add(f"tx_activity_{i}", "activity", str(restriction), "MAJOR")

        wound = d.get("wound_care")
        if wound:
            add("tx_wound_care", "wound_care", str(wound), "MAJOR")

        diet = d.get("diet_instructions")
        if diet:
            add("tx_diet", "diet", str(diet), "MAJOR")

        for i, flag in enumerate(d.get("red_flags") or []):
            if flag:
                add(
                    f"tx_red_flag_{i}",
                    "red_flag",
                    f"red flag with symptom and action to take: {flag}",
                    "CRITICAL",
                )

        fu = d.get("follow_up") or {}
        if fu.get("date"):
            add("tx_followup_date", "follow_up", f"follow-up date: {fu['date']}", "MAJOR")
        if fu.get("provider"):
            add("tx_followup_provider", "follow_up", f"follow-up provider: {fu['provider']}", "MAJOR")

    elif track == "post_op_diagnosis":
        for i, dx in enumerate(d.get("key_diagnoses") or []):
            if dx:
                add(f"dx_{i}_{_slug(str(dx))}", "diagnosis", f"diagnosis must be named/explained: {dx}", "CRITICAL")

        post = d.get("post_op_instructions") or ""
        fu = d.get("follow_up") or {}
        if post or fu.get("date") or fu.get("provider"):
            add(
                "dx_next_steps",
                "plan",
                "what comes next (post-op instructions and/or follow-up) must be stated",
                "MAJOR",
            )

    return items


def compute_accuracy(report: GroundingReport) -> dict:
    """Derived display metrics for the admin UI."""
    cov = report.coverage
    covered = sum(1 for c in cov if c.get("status") == "COVERED")
    coverage_pct = round(100 * covered / len(cov), 1) if cov else 100.0
    faith = report.faithfulness
    unsupported = sum(1 for f in faith if f.get("status") == "UNSUPPORTED")
    faithfulness_pct = round(100 * (len(faith) - unsupported) / len(faith), 1) if faith else 100.0
    return {
        "coverage_pct": coverage_pct,
        "faithfulness_pct": faithfulness_pct,
        "items_required": len(cov),
        "items_covered": covered,
        "items_partial": sum(1 for c in cov if c.get("status") == "PARTIAL"),
        "items_missing": sum(1 for c in cov if c.get("status") == "MISSING"),
        "unsupported_claims": unsupported,
        "critical_failures": len(report.critical_failures),
    }


def assert_script_is_grounded(report: GroundingReport) -> None:
    """Raise ValueError if verdict is BLOCK."""
    if report.verdict == "BLOCK":
        msg = "; ".join(report.critical_failures) or report.summary
        raise ValueError(f"Script blocked by grounding check: {msg}")


def _fail_safe_report(track: str, required_items: List[dict], reason: str) -> GroundingReport:
    return GroundingReport(
        track=track,
        coverage=[
            {
                "id": ri["id"],
                "category": ri.get("category", ""),
                "status": "MISSING",
                "severity": ri.get("severity", "CRITICAL"),
                "evidence": None,
            }
            for ri in required_items
        ],
        faithfulness=[],
        critical_failures=[f"inspector_unavailable: {reason}"],
        verdict="BLOCK",
        summary="Inspector could not verify script; treated as unsafe.",
        required_items=required_items,
    )


def _strip_json_fences(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1 :]
        else:
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3].strip()
    return text


def parse_grounding_response(raw: str, track: str, required_items: List[dict]) -> GroundingReport:
    """Parse judge JSON into GroundingReport."""
    cleaned = _strip_json_fences(raw)
    data = json.loads(cleaned)
    report = GroundingReport.model_validate(data)
    report.required_items = required_items
    report.model = GROUNDING_JUDGE_MODEL
    report.prompt_version = GROUNDING_PROMPT_V
    if not report.track:
        report.track = track
    return report


async def check_grounding(
    structured_data: Dict[str, Any],
    script: str,
    track: str,
    *,
    patient_id: Optional[str] = None,
    client: Optional[Any] = None,
) -> GroundingReport:
    """Run coverage + faithfulness audit on a voice script."""
    required = build_required_items(structured_data, track)

    if client is None and not os.getenv("ANTHROPIC_API_KEY"):
        return _fail_safe_report(track, required, "ANTHROPIC_API_KEY not configured")
    user_msg = (
        f"TRACK: {track}\n\n"
        f"SOURCE:\n{json.dumps(structured_data, indent=2)}\n\n"
        f"REQUIRED_ITEMS:\n{json.dumps(required, indent=2)}\n\n"
        f"SCRIPT:\n{script}"
    )

    try:
        if client is None:
            response, _ = await call_llm(
                role="grounding_judge",
                prompt_id="grounding_judge",
                patient_id=patient_id,
                purpose="grounding_judge",
                system=GROUNDING_JUDGE_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
        else:
            # Test seam only: allow injected mocked SDK-like clients.
            create_fn = getattr(getattr(client, "messages"), "create")
            response = await create_fn(
                model=resolve("grounding_judge")["model"],
                max_tokens=1500,
                temperature=0,
                system=GROUNDING_JUDGE_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
        raw = first_text(response).strip()
        return parse_grounding_response(raw, track, required)
    except (json.JSONDecodeError, ValidationError, IndexError, AttributeError) as exc:
        return _fail_safe_report(track, required, f"could not verify script ({exc})")
    except Exception as exc:
        return _fail_safe_report(track, required, f"could not verify script ({type(exc).__name__})")
