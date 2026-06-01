"""
Teach-back router: patient session flow + admin observability.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from auth_roles import require_patient_session
from pipeline.teachback_grade import (
    TEACHBACK_GRADE_PROMPT_V,
    TEACHBACK_JUDGE_MODEL,
    grade_answer,
)
from pipeline.teachback_questions import (
    TEACHBACK_AUTHOR_MODEL,
    TEACHBACK_QUESTIONS_PROMPT_V,
    generate_teachback_questions,
)
from routers.admin import _verify_token as _verify_admin_bearer
from staff_context import get_staff_context_optional
from triage.postop.apply import apply_postop_retier
from triage.postop.locks import with_patient_lock
from triage.postop.scoring.video_engagement import determine_video_flags
from triage.preop_retier.apply import apply_preop_retier
from triage.preop_retier.locks import with_episode_lock

router = APIRouter(tags=["teachback"])

_VALID_TRACKS = {"pre_op", "post_op_diagnosis", "post_op_treatment"}
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


class TeachBackStartResponse(BaseModel):
    ok: bool
    session_id: int
    track: str
    questions: list[dict]
    battlecard_html: str


class TeachBackAnswerBody(BaseModel):
    session_id: Optional[int] = None
    question_id: str
    answer: str = Field(default="")


def _resolve_patient(request: Request, patient_id: str) -> dict:
    store = request.app.state.patient_store
    patient = store.get(patient_id)
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    return patient


def _verify_admin_access(authorization: Optional[str], x_admin_token: Optional[str]) -> None:
    if authorization:
        _verify_admin_bearer(authorization)
        return
    expected = os.getenv("ADMIN_AUTH_TOKEN") or os.getenv("ADMIN_TOKEN")
    if not expected or x_admin_token != expected:
        raise HTTPException(status_code=401, detail="Admin token required")


def _days_since_discharge(patient: dict) -> int:
    raw = patient.get("discharge_at")
    if not raw:
        return 0
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return 0
    return max((datetime.utcnow() - ts).days, 0)


def _resource_for_track(patient: dict, track: str) -> dict:
    resources = patient.get("resources") or {}
    if track == "pre_op":
        out = (resources.get("preop") or {}) if isinstance(resources, dict) else {}
    elif track == "post_op_diagnosis":
        out = (resources.get("diagnosis") or {}) if isinstance(resources, dict) else {}
    else:
        out = (resources.get("treatment") or {}) if isinstance(resources, dict) else {}
    if not isinstance(out, dict):
        out = {}
    return out


def _set_resource_battlecard(patient: dict, track: str, battlecard_html: str) -> None:
    resources = patient.get("resources") or {}
    if not isinstance(resources, dict):
        return
    key = "preop" if track == "pre_op" else "diagnosis" if track == "post_op_diagnosis" else "treatment"
    cur = resources.get(key) or {}
    if not isinstance(cur, dict):
        cur = {}
    cur["battlecard_html"] = battlecard_html
    resources[key] = cur
    patient["resources"] = resources


def _is_track_unlocked(team_store, patient: dict, patient_id: str, track: str) -> bool:
    if track == "pre_op":
        events = team_store.get_events(patient_id) or []
        return any(e.get("event_type") in {"preop_video_watched", "PREOP_VIDEO_PLAYED"} for e in events)

    events = team_store.list_postop_video_events(patient_id)
    flags = determine_video_flags(
        events,
        discharge_at_iso=patient.get("discharge_at"),
        days_since_discharge=_days_since_discharge(patient),
    )
    if track == "post_op_diagnosis":
        return bool(flags.get("diag_treat_video_viewed_by_d5"))
    return bool(flags.get("red_flag_video_viewed_by_d5"))


def _derive_final_status(items: list[dict]) -> str:
    statuses = [str(((it.get("final_grade") or {}).get("status") or "PARTIAL")).upper() for it in items]
    if any(s == "FAIL" for s in statuses):
        return "FAIL"
    if any(s == "PARTIAL" for s in statuses):
        return "PARTIAL"
    return "PASS"


def _update_patient_teachback_flags(patient: dict, track: str, results: dict) -> None:
    tb = patient.get("teachback") or {}
    if not isinstance(tb, dict):
        tb = {}
    aggregate = (results or {}).get("aggregate") or {}
    flags = {
        "track": track,
        "started": True,
        "completed": bool(results.get("completed")),
        "final_status": aggregate.get("final_status"),
        "failed_red_flag": bool(aggregate.get("failed_red_flag")),
        "failed_med": bool(aggregate.get("failed_med")),
        "failed_med_hold": bool(aggregate.get("failed_med_hold")),
        "failed_fasting": bool(aggregate.get("failed_fasting")),
        "failed_critical": bool(aggregate.get("failed_critical")),
        "completed_at": datetime.utcnow().replace(microsecond=0).isoformat(),
    }
    tb[track] = flags
    patient["teachback"] = tb


async def _trigger_retier_after_completion(
    *,
    request: Request,
    patient_id: str,
    track: str,
) -> None:
    patient_store = request.app.state.patient_store
    team_store = request.app.state.team_store
    if track == "pre_op":
        async with with_episode_lock(patient_id):
            apply_preop_retier(
                patient_id=patient_id,
                patient_store=patient_store,
                team_store=team_store,
                triggered_by="SIGNAL:TEACHBACK_RESULT",
            )
        return
    async with with_patient_lock(patient_id):
        apply_postop_retier(
            patient_id=patient_id,
            patient_store=patient_store,
            team_store=team_store,
            triggered_by="SIGNAL:TEACHBACK_RESULT",
        )


@router.post("/api/episodes/{patient_id}/teachback/{track}/start", response_model=TeachBackStartResponse)
async def start_teachback(
    patient_id: str,
    track: str,
    request: Request,
    authorization: Optional[str] = Header(None),
):
    staff = await get_staff_context_optional(authorization)
    require_patient_session(staff)
    if track not in _VALID_TRACKS:
        raise HTTPException(status_code=400, detail="Invalid track")
    team_store = request.app.state.team_store
    patient = _resolve_patient(request, patient_id)
    if not _is_track_unlocked(team_store, patient, patient_id, track):
        raise HTTPException(status_code=409, detail="Teach-back unlocks after track video completion")

    resource = _resource_for_track(patient, track)
    voice_script = str(resource.get("voice_script") or "")
    battlecard_html = str(resource.get("battlecard_html") or "")
    structured_data = dict(patient.get("structured_data") or {})
    if not voice_script or not battlecard_html:
        raise HTTPException(status_code=422, detail="Missing generated materials for teach-back")

    questions, anchored_html = await generate_teachback_questions(
        structured_data=structured_data,
        voice_script=voice_script,
        battlecard_html=battlecard_html,
        track=track,
        patient_id=patient_id,
    )
    if not questions:
        raise HTTPException(status_code=422, detail="No teach-back questions available for this track")

    session_items = []
    for q in questions:
        session_items.append(
            {
                "question": q,
                "attempts": [],
                "final_grade": None,
                "finalized": False,
                "retry_used": False,
            }
        )
    results = {
        "current_index": 0,
        "items": session_items,
        "completed": False,
        "aggregate": {},
    }
    session_id = team_store.save_teachback_session(
        patient_id=patient_id,
        track=track,
        questions=questions,
        results=results,
        completed=False,
        prompt_version=TEACHBACK_QUESTIONS_PROMPT_V,
        model=TEACHBACK_AUTHOR_MODEL,
    )
    tb = patient.get("teachback") or {}
    if not isinstance(tb, dict):
        tb = {}
    cur = tb.get(track) if isinstance(tb.get(track), dict) else {}
    cur = dict(cur or {})
    cur["track"] = track
    cur["started"] = True
    cur["completed"] = bool(cur.get("completed"))
    cur["started_at"] = datetime.utcnow().replace(microsecond=0).isoformat()
    tb[track] = cur
    patient["teachback"] = tb
    _set_resource_battlecard(patient, track, anchored_html)
    return TeachBackStartResponse(
        ok=True,
        session_id=session_id,
        track=track,
        questions=questions,
        battlecard_html=anchored_html,
    )


@router.post("/api/episodes/{patient_id}/teachback/{track}/answer")
async def answer_teachback(
    patient_id: str,
    track: str,
    body: TeachBackAnswerBody,
    request: Request,
    authorization: Optional[str] = Header(None),
):
    staff = await get_staff_context_optional(authorization)
    require_patient_session(staff)
    if track not in _VALID_TRACKS:
        raise HTTPException(status_code=400, detail="Invalid track")
    team_store = request.app.state.team_store
    patient = _resolve_patient(request, patient_id)
    session = (
        team_store.get_teachback_session(int(body.session_id))
        if body.session_id is not None
        else team_store.get_latest_teachback_session(patient_id=patient_id, track=track)
    )
    if not session or str(session.get("track")) != track:
        raise HTTPException(status_code=404, detail="Teach-back session not found")
    if bool(session.get("completed")):
        return {"ok": True, "completed": True, "results": session.get("results") or {}}

    results = dict(session.get("results") or {})
    items = list(results.get("items") or [])
    current_idx = int(results.get("current_index") or 0)
    if current_idx < 0 or current_idx >= len(items):
        raise HTTPException(status_code=409, detail="Teach-back session state is out of sync")
    item = dict(items[current_idx] or {})
    question = dict(item.get("question") or {})
    if str(question.get("id")) != str(body.question_id):
        raise HTTPException(status_code=400, detail="Answer does not match the active question")

    answer = (body.answer or "").strip()
    is_non_answer = answer.lower() in _NON_ANSWER_MARKERS
    grade = await grade_answer(
        question,
        answer,
        dict(patient.get("structured_data") or {}),
        patient_id=patient_id,
    )
    attempts = list(item.get("attempts") or [])
    attempts.append(
        {
            "answer": answer,
            "is_non_answer": is_non_answer,
            "grade": grade.model_dump(),
            "attempt_index": len(attempts) + 1,
            "submitted_at": datetime.utcnow().replace(microsecond=0).isoformat(),
        }
    )
    item["attempts"] = attempts

    first_attempt = len(attempts) == 1
    status = grade.status
    should_retry = first_attempt and status != "PASS"

    if should_retry:
        item["retry_used"] = True
        items[current_idx] = item
        results["items"] = items
        results["current_index"] = current_idx
        team_store.update_teachback_session(session_id=int(session["id"]), results=results, completed=False)
        locate = {
            "battlecard_anchor": question.get("battlecard_anchor"),
            "transcript_quote": question.get("source_quote"),
            "audio_seek_sec": None,
        }
        return {"ok": True, "completed": False, "status": status, "retry": True, "locate": locate}

    item["final_grade"] = grade.model_dump()
    item["finalized"] = True
    items[current_idx] = item

    next_idx = current_idx + 1
    finished = next_idx >= len(items)
    results["items"] = items
    results["current_index"] = current_idx if finished else next_idx
    results["completed"] = finished

    if finished:
        final_status = _derive_final_status(items)
        failed_red_flag = False
        failed_med = False
        failed_med_hold = False
        failed_fasting = False
        failed_critical = False
        by_status = {"PASS": 0, "PARTIAL": 0, "FAIL": 0}
        for it in items:
            q = it.get("question") or {}
            fg = it.get("final_grade") or {}
            st = str(fg.get("status") or "PARTIAL").upper()
            if st in by_status:
                by_status[st] += 1
            if st != "PASS":
                if str(q.get("domain")) == "RED_FLAG":
                    failed_red_flag = True
                if str(q.get("domain")) in {"MED", "MED_HOLD"}:
                    failed_med = True
                if str(q.get("domain")) == "MED_HOLD":
                    failed_med_hold = True
                if str(q.get("domain")) == "FASTING":
                    failed_fasting = True
                if str(q.get("severity")) == "CRITICAL" and str(q.get("domain")) not in {"RED_FLAG", "MED", "MED_HOLD"}:
                    failed_critical = True
        results["aggregate"] = {
            "final_status": final_status,
            "by_status": by_status,
            "failed_red_flag": failed_red_flag,
            "failed_med": failed_med,
            "failed_med_hold": failed_med_hold,
            "failed_fasting": failed_fasting,
            "failed_critical": failed_critical,
        }

    team_store.update_teachback_session(session_id=int(session["id"]), results=results, completed=finished)

    if finished:
        aggregate = results.get("aggregate") or {}
        team_store.log_event(
            patient_id=patient_id,
            event_type="teachback_result",
            payload={
                "session_id": int(session["id"]),
                "track": track,
                "final_status": aggregate.get("final_status"),
                "by_status": aggregate.get("by_status") or {},
                "failed_red_flag": bool(aggregate.get("failed_red_flag")),
                "failed_med": bool(aggregate.get("failed_med")),
                "failed_med_hold": bool(aggregate.get("failed_med_hold")),
                "failed_fasting": bool(aggregate.get("failed_fasting")),
                "failed_critical": bool(aggregate.get("failed_critical")),
                "prompt_version": TEACHBACK_GRADE_PROMPT_V,
                "model": TEACHBACK_JUDGE_MODEL,
            },
        )
        _update_patient_teachback_flags(patient, track, results)
        await _trigger_retier_after_completion(request=request, patient_id=patient_id, track=track)

    return {
        "ok": True,
        "completed": finished,
        "status": status,
        "retry": False,
        "next_question_id": None if finished else (items[next_idx].get("question") or {}).get("id"),
        "results": results if finished else None,
    }


@router.get("/api/episodes/{patient_id}/teachback/{track}")
async def get_teachback_session_state(
    patient_id: str,
    track: str,
    request: Request,
    authorization: Optional[str] = Header(None),
):
    staff = await get_staff_context_optional(authorization)
    require_patient_session(staff)
    if track not in _VALID_TRACKS:
        raise HTTPException(status_code=400, detail="Invalid track")
    _resolve_patient(request, patient_id)
    team_store = request.app.state.team_store
    session = team_store.get_latest_teachback_session(patient_id=patient_id, track=track)
    if not session:
        return {"available": False}
    return {"available": True, "session": session}


@router.get("/admin/teachback/stats")
async def admin_teachback_stats(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_admin_token: Optional[str] = Header(None, alias="X-Admin-Token"),
    window_days: int = 30,
):
    _verify_admin_access(authorization, x_admin_token)
    team_store = request.app.state.team_store
    return team_store.teachback_summary_stats(window_days=window_days)


@router.get("/admin/teachback/grader-recall")
async def admin_teachback_recall(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_admin_token: Optional[str] = Header(None, alias="X-Admin-Token"),
):
    _verify_admin_access(authorization, x_admin_token)
    team_store = request.app.state.team_store
    snap = team_store.get_latest_teachback_recall()
    if not snap:
        return {"available": False, "message": "No teach-back grader recall snapshot yet"}
    return {"available": True, **snap}
