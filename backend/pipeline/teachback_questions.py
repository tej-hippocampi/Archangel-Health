"""
Teach-back question authoring for patient comprehension checks.

Builds patient-specific open-ended questions from:
  - structured_data (source-of-truth clinical facts)
  - generated voice_script
  - generated battlecard_html
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Literal, Optional, Tuple

from anthropic import AsyncAnthropic
from pydantic import BaseModel, ValidationError

from pipeline.grounding_check import VALID_TRACKS, build_required_items

TEACHBACK_QUESTIONS_PROMPT_V = "2026-06-01.1"
TEACHBACK_AUTHOR_MODEL = "claude" + "-sonnet" + "-4-6"

_MAX_QUESTIONS_DEFAULT = 3
_ANCHOR_PREFIX = "tb-anchor"

TEACHBACK_QUESTIONS_PROMPT = """You author teach-back questions for post-education clinical comprehension.

You are given:
- TRACK: one of pre_op | post_op_diagnosis | post_op_treatment
- STRUCTURED_DATA: patient-specific clinical source of truth
- REQUIRED_ITEMS: required critical/major topics derived from source
- VOICE_SCRIPT: generated patient script
- BATTLECARD_HTML: generated battlecard HTML
- MAX_QUESTIONS: number of questions to return

Rules:
1) Ground only in STRUCTURED_DATA + VOICE_SCRIPT + BATTLECARD_HTML.
2) Never invent medications, doses, instructions, names, timelines, or thresholds.
3) Prioritize high-consequence domains by track:
   - pre_op: FASTING, MED_HOLD, then FOLLOWUP/MAIN_PROBLEM
   - post_op_diagnosis: MED, MAIN_PROBLEM, FOLLOWUP
   - post_op_treatment: RED_FLAG, WOUND_CARE, ACTIVITY, MED
4) Use open-ended or scenario style prompts in plain language.
5) Every question must include:
   - id (stable slug)
   - severity (CRITICAL|MAJOR)
   - domain
   - form (OPEN_ENDED|SCENARIO|WHY)
   - question (patient-facing text)
   - expected (patient-specific correct answer)
   - source_quote (verbatim supporting quote from VOICE_SCRIPT)
   - battlecard_anchor (id string, starts with "tb-anchor-")
6) Return strict JSON only with shape:
{
  "questions": [ ... ]
}
"""


TeachBackSeverity = Literal["CRITICAL", "MAJOR"]
TeachBackDomain = Literal[
    "RED_FLAG",
    "MED",
    "FASTING",
    "MED_HOLD",
    "WOUND_CARE",
    "ACTIVITY",
    "FOLLOWUP",
    "MAIN_PROBLEM",
]
TeachBackForm = Literal["OPEN_ENDED", "SCENARIO", "WHY"]


class TeachBackQuestion(BaseModel):
    id: str
    track: str
    severity: TeachBackSeverity
    domain: TeachBackDomain
    form: TeachBackForm
    question: str
    expected: str
    source_quote: str
    battlecard_anchor: str


class TeachBackQuestionSet(BaseModel):
    questions: list[TeachBackQuestion]


def _slug(text: str, limit: int = 48) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:limit] or "item"


def _strip_json_fences(text: str) -> str:
    out = (text or "").strip()
    if out.startswith("```"):
        first_nl = out.find("\n")
        out = out[first_nl + 1 :] if first_nl != -1 else out[3:]
        if out.endswith("```"):
            out = out[:-3].strip()
    return out


def _domain_and_form(track: str, item: Dict[str, Any]) -> tuple[str, str]:
    category = str(item.get("category") or "").lower()
    text = str(item.get("text") or "").lower()

    if category == "red_flag":
        return "RED_FLAG", "SCENARIO"
    if category == "wound_care":
        return "WOUND_CARE", "OPEN_ENDED"
    if category == "activity":
        return "ACTIVITY", "SCENARIO"
    if category == "follow_up":
        return "FOLLOWUP", "OPEN_ENDED"
    if category == "diagnosis":
        return "MAIN_PROBLEM", "OPEN_ENDED"
    if category == "plan":
        return "MAIN_PROBLEM", "OPEN_ENDED"
    if category == "diet" and track == "pre_op":
        return "FASTING", "OPEN_ENDED"
    if category == "medication":
        if track == "pre_op" and ("hold" in text or "stop" in text):
            return "MED_HOLD", "WHY"
        return "MED", "WHY"
    return "MAIN_PROBLEM", "OPEN_ENDED"


def _domain_priority(track: str) -> dict[str, int]:
    if track == "post_op_treatment":
        ordered = ["RED_FLAG", "WOUND_CARE", "ACTIVITY", "MED", "FOLLOWUP", "MAIN_PROBLEM"]
    elif track == "post_op_diagnosis":
        ordered = ["MED", "MAIN_PROBLEM", "FOLLOWUP", "ACTIVITY", "RED_FLAG", "WOUND_CARE"]
    else:
        ordered = ["FASTING", "MED_HOLD", "MED", "FOLLOWUP", "MAIN_PROBLEM", "ACTIVITY", "RED_FLAG", "WOUND_CARE"]
    return {name: idx for idx, name in enumerate(ordered)}


def _severity_rank(sev: str) -> int:
    return 0 if sev == "CRITICAL" else 1


def _pick_source_quote(voice_script: str, item_text: str) -> str:
    lines = [ln.strip() for ln in (voice_script or "").splitlines() if ln.strip()]
    tokens = [t for t in re.split(r"[^a-z0-9]+", item_text.lower()) if len(t) >= 5][:3]
    if tokens:
        for ln in lines:
            ll = ln.lower()
            if any(tok in ll for tok in tokens):
                return ln[:280]
    return (lines[0][:280] if lines else item_text[:280]) or "Refer to your recovery instructions."


def _inject_anchors(battlecard_html: str, anchors: List[str]) -> str:
    html = battlecard_html or ""
    if not html.strip():
        return html
    used = set()
    scan_pos = 0
    for anchor in anchors:
        if not anchor or anchor in used or f'id="{anchor}"' in html or f"id='{anchor}'" in html:
            continue
        used.add(anchor)
        # Add anchor ids to visible semantic nodes in order.
        m = re.search(r"<(h[1-6]|p|li|div)(\s[^>]*)?>", html[scan_pos:], flags=re.IGNORECASE)
        if not m:
            continue
        start = scan_pos + m.start()
        end = scan_pos + m.end()
        tag_text = html[start:end]
        if re.search(r"\sid\s*=", tag_text, flags=re.IGNORECASE):
            scan_pos = end
            continue
        replaced = tag_text[:-1] + f' id="{anchor}">'
        html = html[:start] + replaced + html[end:]
        scan_pos = start + len(replaced)
    return html


def _build_fallback_questions(
    *,
    track: str,
    required_items: List[Dict[str, Any]],
    voice_script: str,
    max_questions: int,
) -> list[TeachBackQuestion]:
    candidates: list[tuple[int, int, TeachBackQuestion]] = []
    prio = _domain_priority(track)
    for i, item in enumerate(required_items):
        severity = str(item.get("severity") or "MAJOR")
        if severity not in ("CRITICAL", "MAJOR"):
            continue
        domain, form = _domain_and_form(track, item)
        item_id = _slug(str(item.get("id") or f"req-{i}"))
        anchor = f"{_ANCHOR_PREFIX}-{item_id}"
        text = str(item.get("text") or "").strip()
        if form == "SCENARIO":
            q = f"If this happens at home, what would you do: {text}?"
        elif form == "WHY":
            q = f"In your own words, what is your medication plan here, and why is it important: {text}?"
        else:
            q = f"In your own words, can you explain this part of your plan: {text}?"
        question = TeachBackQuestion(
            id=f"tb-{item_id}",
            track=track,
            severity=severity,  # type: ignore[arg-type]
            domain=domain,  # type: ignore[arg-type]
            form=form,  # type: ignore[arg-type]
            question=q,
            expected=text,
            source_quote=_pick_source_quote(voice_script, text),
            battlecard_anchor=anchor,
        )
        candidates.append((prio.get(domain, 99), _severity_rank(severity), question))
    candidates.sort(key=lambda x: (x[1], x[0]))
    return [c[2] for c in candidates[:max_questions]]


def _validate_and_normalize_questions(raw: str, track: str, max_questions: int) -> list[TeachBackQuestion]:
    cleaned = _strip_json_fences(raw)
    data = json.loads(cleaned)
    parsed = TeachBackQuestionSet.model_validate(data)
    out: list[TeachBackQuestion] = []
    for q in parsed.questions[:max_questions]:
        if q.track != track:
            q.track = track
        if not q.battlecard_anchor.startswith(f"{_ANCHOR_PREFIX}-"):
            q.battlecard_anchor = f"{_ANCHOR_PREFIX}-{_slug(q.battlecard_anchor)}"
        out.append(q)
    return out


def _required_items_for_teachback(structured_data: Dict[str, Any], track: str) -> list[dict]:
    base = build_required_items(structured_data or {}, track)
    return [item for item in base if str(item.get("severity")) in ("CRITICAL", "MAJOR")]


async def generate_teachback_questions(
    *,
    structured_data: Dict[str, Any],
    voice_script: str,
    battlecard_html: str,
    track: str,
    patient_id: Optional[str] = None,
    max_questions: int = _MAX_QUESTIONS_DEFAULT,
    client: Optional[AsyncAnthropic] = None,
) -> tuple[list[dict], str]:
    """
    Return (questions, battlecard_html_with_anchors).

    Falls back deterministically when model access is unavailable.
    """
    if track not in VALID_TRACKS:
        raise ValueError(f"unknown track: {track}")
    max_questions = max(1, min(int(max_questions or _MAX_QUESTIONS_DEFAULT), 5))
    required_items = _required_items_for_teachback(structured_data, track)
    if not required_items:
        return [], battlecard_html or ""

    user_payload = {
        "track": track,
        "patient_id": patient_id,
        "max_questions": max_questions,
        "structured_data": structured_data or {},
        "required_items": required_items,
        "voice_script": voice_script or "",
        "battlecard_html": battlecard_html or "",
    }
    user_msg = json.dumps(user_payload, indent=2)

    try:
        api_client = client
        if api_client is None:
            key = os.getenv("ANTHROPIC_API_KEY")
            if not key:
                raise RuntimeError("ANTHROPIC_API_KEY not configured")
            api_client = AsyncAnthropic(api_key=key)
        response = await api_client.messages.create(
            model=TEACHBACK_AUTHOR_MODEL,
            max_tokens=1800,
            temperature=0,
            system=TEACHBACK_QUESTIONS_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = str((response.content or [])[0].text).strip()  # type: ignore[index]
        questions = _validate_and_normalize_questions(text, track, max_questions)
    except (RuntimeError, ValidationError, json.JSONDecodeError, IndexError, AttributeError, TypeError):
        questions = _build_fallback_questions(
            track=track,
            required_items=required_items,
            voice_script=voice_script,
            max_questions=max_questions,
        )

    anchors = [q.battlecard_anchor for q in questions]
    anchored_html = _inject_anchors(battlecard_html or "", anchors)
    return [q.model_dump() for q in questions], anchored_html
