"""
Teach-back grading for patient free-text answers.
"""

from __future__ import annotations

import json
import os
from typing import Any, Literal, Optional

from anthropic import AsyncAnthropic
from pydantic import BaseModel, ValidationError

TEACHBACK_GRADE_PROMPT_V = "2026-06-01.1"
TEACHBACK_JUDGE_MODEL = "claude" + "-sonnet" + "-4-6"

_NON_ANSWER_MARKERS = {
    "",
    "i'm not sure",
    "im not sure",
    "not sure",
    "i dont know",
    "i don't know",
    "dont know",
    "don't know",
}

TEACHBACK_JUDGE_PROMPT = """You are a clinical teach-back answer grader.

Grade a patient answer against the PATIENT-SPECIFIC EXPECTED answer.
This is open-book teach-back: verbatim reading from the battlecard can PASS.

Rules:
1) Grade on meaning, not exact wording.
2) PASS: includes the right action and critical specific(s) needed for safety.
3) PARTIAL: right topic but missing critical specific(s).
4) FAIL: clearly wrong action/instruction.
5) Empty/"I'm not sure" is a non-answer and must return PARTIAL with missing=["NON_ANSWER"].
6) Never invent facts beyond QUESTION + EXPECTED + STRUCTURED_DATA context.
7) Return strict JSON only:
{
  "question_id": "...",
  "status": "PASS|PARTIAL|FAIL",
  "missing": ["..."],
  "evidence": "...",
  "severity": "CRITICAL|MAJOR",
  "domain": "RED_FLAG|MED|FASTING|MED_HOLD|WOUND_CARE|ACTIVITY|FOLLOWUP|MAIN_PROBLEM"
}
"""


class TeachBackGrade(BaseModel):
    question_id: str
    status: Literal["PASS", "PARTIAL", "FAIL"]
    missing: list[str]
    evidence: str
    severity: str
    domain: str
    model: str = TEACHBACK_JUDGE_MODEL
    prompt_version: str = TEACHBACK_GRADE_PROMPT_V


def _strip_json_fences(text: str) -> str:
    out = (text or "").strip()
    if out.startswith("```"):
        first_nl = out.find("\n")
        out = out[first_nl + 1 :] if first_nl != -1 else out[3:]
        if out.endswith("```"):
            out = out[:-3].strip()
    return out


def _fail_safe_grade(question: dict, reason: str) -> TeachBackGrade:
    return TeachBackGrade(
        question_id=str(question.get("id") or "unknown"),
        status="PARTIAL",
        missing=[f"GRADER_UNAVAILABLE:{reason}"],
        evidence="Could not verify answer safely; route to re-teach.",
        severity=str(question.get("severity") or "CRITICAL"),
        domain=str(question.get("domain") or "MAIN_PROBLEM"),
    )


def _non_answer_grade(question: dict) -> TeachBackGrade:
    return TeachBackGrade(
        question_id=str(question.get("id") or "unknown"),
        status="PARTIAL",
        missing=["NON_ANSWER"],
        evidence="Patient did not provide an answer.",
        severity=str(question.get("severity") or "CRITICAL"),
        domain=str(question.get("domain") or "MAIN_PROBLEM"),
    )


def _parse_grade(raw: str) -> TeachBackGrade:
    data = json.loads(_strip_json_fences(raw))
    grade = TeachBackGrade.model_validate(data)
    grade.model = TEACHBACK_JUDGE_MODEL
    grade.prompt_version = TEACHBACK_GRADE_PROMPT_V
    return grade


async def grade_answer(
    question: dict,
    patient_answer: str,
    structured_data: dict,
    *,
    patient_id: str | None = None,
    client: AsyncAnthropic | None = None,
) -> TeachBackGrade:
    answer = (patient_answer or "").strip()
    if answer.lower() in _NON_ANSWER_MARKERS:
        return _non_answer_grade(question)

    user_msg = json.dumps(
        {
            "patient_id": patient_id,
            "question": question or {},
            "patient_answer": answer,
            "structured_data": structured_data or {},
        },
        indent=2,
    )

    try:
        api_client = client
        if api_client is None:
            key = os.getenv("ANTHROPIC_API_KEY")
            if not key:
                return _fail_safe_grade(question, "ANTHROPIC_API_KEY not configured")
            api_client = AsyncAnthropic(api_key=key)
        resp = await api_client.messages.create(
            model=TEACHBACK_JUDGE_MODEL,
            max_tokens=800,
            temperature=0,
            system=TEACHBACK_JUDGE_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = str((resp.content or [])[0].text).strip()  # type: ignore[index]
        grade = _parse_grade(text)
        if not grade.question_id:
            grade.question_id = str(question.get("id") or "unknown")
        return grade
    except (json.JSONDecodeError, ValidationError, IndexError, AttributeError, TypeError) as exc:
        return _fail_safe_grade(question, f"parse_error:{type(exc).__name__}")
    except Exception as exc:  # noqa: BLE001
        return _fail_safe_grade(question, f"runtime_error:{type(exc).__name__}")
