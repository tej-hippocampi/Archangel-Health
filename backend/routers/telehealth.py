"""Telehealth encounters, video sessions, and TEAM claim drafting."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from integrations.twilio_client import TwilioClient
from integrations.video.daily import get_video_provider
from staff_context import StaffContext, get_staff_context_optional
from telehealth.gcodes import (
    DEMO_CODE,
    REVENUE_CODE,
    RIDE_ALONE,
    TYPE_OF_BILL,
    RideAloneViolation,
    enforce_ride_alone,
    map_gcode,
    next_threshold,
    pos_from_location,
    requires_l45_gate,
)
from tenant_jwt import create_telehealth_join_token, decode_telehealth_join_token

router = APIRouter(tags=["telehealth"])

HEARTBEAT_MAX_GAP_SEC = 20


def _parse_iso_datetime(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    text = str(raw).strip()
    if text.endswith("Z"):
        text = text[:-1]
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _resolve_eligibility_verdict(patient: Dict[str, Any]) -> Optional[str]:
    """Prefer stored eligibility-check verdict; fall back to patient dict field."""
    from eligibility import store as elig_store  # noqa: PLC0415

    check_id = patient.get("eligibility_check_id")
    if check_id:
        rec = elig_store.get_check(check_id)
        if rec:
            overall = (rec.get("overall_verdict") or "").strip().upper()
            if overall:
                return overall

    pid = patient.get("id")
    if pid:
        latest: Optional[str] = None
        latest_at = ""
        for rec in elig_store.ELIGIBILITY_CHECKS.values():
            if rec.get("patient_id") != pid:
                continue
            overall = (rec.get("overall_verdict") or "").strip().upper()
            if not overall:
                continue
            ts = str(rec.get("finished_at") or rec.get("updated_at") or "")
            if ts >= latest_at:
                latest_at = ts
                latest = overall
        if latest:
            return latest

    status = (patient.get("eligibility_status") or patient.get("eligibilityStatus") or "").strip().upper()
    return status or None


def _check_team_eligibility(patient: Dict[str, Any]) -> None:
    verdict = _resolve_eligibility_verdict(patient)
    if verdict == "INELIGIBLE":
        raise HTTPException(
            status_code=409,
            detail="This patient's TEAM eligibility is INELIGIBLE — telehealth visits cannot be started.",
        )


def _accrue_connected_seconds(enc: Dict[str, Any], *, cap_gap: int = HEARTBEAT_MAX_GAP_SEC) -> int:
    """Add elapsed time since last heartbeat/start, capped to tolerate drop/rejoin gaps."""
    now = datetime.utcnow()
    connected = int(enc.get("connected_seconds") or 0)
    last_raw = enc.get("last_heartbeat_at") or enc.get("started_at")
    last_dt = _parse_iso_datetime(last_raw)
    if last_dt:
        gap = int((now - last_dt).total_seconds())
        if gap > 0:
            connected += min(gap, cap_gap)
    return max(0, connected)


def _compute_end_duration_seconds(enc: Dict[str, Any], client_fallback: int) -> int:
    connected = _accrue_connected_seconds(enc)
    if connected > 0:
        return connected
    started_dt = _parse_iso_datetime(enc.get("started_at"))
    if started_dt:
        return max(0, int((datetime.utcnow() - started_dt).total_seconds()))
    return max(0, int(client_fallback))


def _serialize_ladder_next(patient_type: str, duration_minutes: int) -> Optional[Dict[str, Any]]:
    nxt = next_threshold(patient_type, duration_minutes)
    if not nxt:
        return None
    hcpcs, minutes = nxt
    return {"hcpcs": hcpcs, "minutes": minutes}


def _normalize_tier(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        return value if value in (1, 2, 3) else None
    raw = str(value).strip().upper()
    if raw in ("1", "2", "3"):
        return int(raw)
    if raw.startswith("TIER_"):
        try:
            tier = int(raw.split("_", 1)[1])
            return tier if tier in (1, 2, 3) else None
        except (IndexError, ValueError):
            return None
    return None


def _patient_summary(request: Request, patient_id: str) -> Dict[str, Any]:
    patient = _patients(request).get(patient_id) or {}
    tier = (
        _normalize_tier(patient.get("current_tier"))
        or _normalize_tier(patient.get("initial_tier"))
    )
    signals: list[str] = []
    ts = _ts(request)
    for rec in ts.list_postop_retier_events(patient_id, limit=12):
        for reason in rec.get("reasons") or []:
            label = (reason.get("label") or reason.get("code") or "").strip()
            if label and label not in signals:
                signals.append(label)
            if len(signals) >= 3:
                break
        if len(signals) >= 3:
            break
    return {
        "name": patient.get("name") or patient_id,
        "tier": tier,
        "tier_label": f"Tier {tier}" if tier else "—",
        "signals": signals[:3],
        "procedure": (patient.get("structured_data") or {}).get("procedure_name") or "",
    }


def _ts(request: Request):
    return request.app.state.team_store


def _patients(request: Request) -> dict:
    return request.app.state.patient_store


def _assert_patient_access(patient_id: str, staff: Optional[StaffContext], request: Request) -> None:
    from main import _assert_clinical_staff_can_access_patient  # noqa: PLC0415

    _assert_clinical_staff_can_access_patient(patient_id, staff)


def _require_staff(staff: Optional[StaffContext]) -> StaffContext:
    from main import _require_clinical_staff  # noqa: PLC0415

    return _require_clinical_staff(staff)


class CreateEncounterBody(BaseModel):
    patient_id: str
    escalation_id: Optional[int] = None
    scheduled_for: Optional[str] = None


class PatientTypeBody(BaseModel):
    patient_type: str = Field(..., pattern="^(NEW|ESTABLISHED)$")


class LocationBody(BaseModel):
    location: str = Field(..., pattern="^(HOME|FACILITY_OTHER)$")


class EndEncounterBody(BaseModel):
    duration_seconds: int = 0
    outcome: str = "COMPLETED"


class AttestBody(BaseModel):
    type: str
    note: str = ""


class DocumentationBody(BaseModel):
    documentation: str = ""


def _frontend_path(name: str) -> str:
    return os.path.join(os.path.dirname(__file__), "..", "..", "frontend", name)


def _read_frontend(name: str) -> str:
    path = _frontend_path(name)
    with open(path, encoding="utf-8") as f:
        return f.read()


@router.post("/api/telehealth/encounters")
async def create_encounter(
    body: CreateEncounterBody,
    request: Request,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    if body.patient_id not in _patients(request):
        raise HTTPException(status_code=404, detail="Patient not found")
    _assert_patient_access(body.patient_id, staff, request)
    staff = _require_staff(staff)
    patient = _patients(request)[body.patient_id]
    _check_team_eligibility(patient)
    ts = _ts(request)
    enc_id = str(uuid.uuid4())
    enc = ts.create_telehealth_encounter(
        encounter_id=enc_id,
        patient_id=body.patient_id,
        health_system_id=patient.get("health_system_id"),
        escalation_id=body.escalation_id,
        scheduled_clinician=staff.email,
        clinician_role=staff.role,
        scheduled_for=body.scheduled_for,
    )
    ts.log_event(
        patient_id=body.patient_id,
        event_type="telehealth_encounter_created",
        payload={"encounter_id": enc_id, "clinician": staff.email},
    )
    return {"encounter_id": enc_id, "encounter": enc}


@router.post("/api/telehealth/encounters/{encounter_id}/session")
async def create_session(
    encounter_id: str,
    request: Request,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    ts = _ts(request)
    enc = ts.get_telehealth_encounter(encounter_id)
    if not enc:
        raise HTTPException(status_code=404, detail="Encounter not found")
    _assert_patient_access(enc["patient_id"], staff, request)
    staff = _require_staff(staff)
    provider = get_video_provider()
    session = await provider.create_session(encounter_id=encounter_id)
    token = await provider.issue_token(
        session=session,
        display_name=staff.name or staff.email or "Clinician",
        is_owner=True,
    )
    ts.update_telehealth_encounter(
        encounter_id,
        vendor_session_id=session.session_id,
        room_url=session.room_url,
        provider=session.provider,
    )
    return {"join_url": token.join_url, "room_url": session.room_url}


@router.post("/api/telehealth/encounters/{encounter_id}/patient-type")
async def set_patient_type(
    encounter_id: str,
    body: PatientTypeBody,
    request: Request,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    ts = _ts(request)
    enc = ts.get_telehealth_encounter(encounter_id)
    if not enc:
        raise HTTPException(status_code=404, detail="Encounter not found")
    _assert_patient_access(enc["patient_id"], staff, request)
    _require_staff(staff)
    ts.update_telehealth_encounter(encounter_id, patient_type=body.patient_type)
    return {"ok": True, "patient_type": body.patient_type}


@router.post("/api/telehealth/encounters/{encounter_id}/invite")
async def invite_patient(
    encounter_id: str,
    request: Request,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    from email_utils import is_email_transport_configured  # noqa: PLC0415
    from main import _send_html_email_impl  # noqa: PLC0415

    ts = _ts(request)
    enc = ts.get_telehealth_encounter(encounter_id)
    if not enc:
        raise HTTPException(status_code=404, detail="Encounter not found")
    _assert_patient_access(enc["patient_id"], staff, request)
    _require_staff(staff)
    patient = _patients(request).get(enc["patient_id"]) or {}
    join_token = create_telehealth_join_token(encounter_id)
    base = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
    join_url = f"{base}/telehealth/join/{encounter_id}?t={join_token}"
    phone = (patient.get("phone") or "").strip()
    email = (patient.get("email") or "").strip()
    first = (patient.get("name") or "Patient").split()[0]
    sms_sid = None
    if phone:
        sms_sid = TwilioClient().send(
            phone,
            f"Hi {first}, your care team invited you to a video visit. Join here: {join_url}",
        )
    emailed = False
    if email and is_email_transport_configured():
        html = f"<p>Hi {first},</p><p>Your care team invited you to a secure video visit.</p><p><a href='{join_url}'>Join video visit</a></p>"
        emailed = await _send_html_email_impl(email, "Your care team video visit invitation", html)
    ts.log_event(
        patient_id=enc["patient_id"],
        event_type="telehealth_invite_sent",
        payload={"encounter_id": encounter_id, "sms": bool(sms_sid), "emailed": emailed},
    )
    return {"ok": True, "join_url": join_url, "sms": bool(sms_sid), "emailed": emailed}


@router.post("/api/telehealth/encounters/{encounter_id}/start")
async def start_encounter(
    encounter_id: str,
    request: Request,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    ts = _ts(request)
    enc = ts.get_telehealth_encounter(encounter_id)
    if not enc:
        raise HTTPException(status_code=404, detail="Encounter not found")
    _assert_patient_access(enc["patient_id"], staff, request)
    _require_staff(staff)
    now = datetime.utcnow().replace(microsecond=0).isoformat()
    ts.update_telehealth_encounter(
        encounter_id,
        status="IN_PROGRESS",
        started_at=now,
        connected_seconds=0,
        last_heartbeat_at=now,
    )
    ts.log_event(patient_id=enc["patient_id"], event_type="telehealth_started", payload={"encounter_id": encounter_id})
    return {"ok": True, "started_at": now}


@router.post("/api/telehealth/encounters/{encounter_id}/heartbeat")
async def heartbeat_encounter(
    encounter_id: str,
    request: Request,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    ts = _ts(request)
    enc = ts.get_telehealth_encounter(encounter_id)
    if not enc:
        raise HTTPException(status_code=404, detail="Encounter not found")
    _assert_patient_access(enc["patient_id"], staff, request)
    _require_staff(staff)
    if enc.get("status") not in ("IN_PROGRESS",):
        raise HTTPException(status_code=409, detail="Encounter is not in progress.")
    connected = _accrue_connected_seconds(enc)
    now = datetime.utcnow().replace(microsecond=0).isoformat()
    ts.update_telehealth_encounter(
        encounter_id,
        connected_seconds=connected,
        last_heartbeat_at=now,
    )
    return {"ok": True, "connected_seconds": connected}


@router.post("/api/telehealth/encounters/{encounter_id}/location")
async def set_location(
    encounter_id: str,
    body: LocationBody,
    request: Request,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    ts = _ts(request)
    enc = ts.get_telehealth_encounter(encounter_id)
    if not enc:
        raise HTTPException(status_code=404, detail="Encounter not found")
    _assert_patient_access(enc["patient_id"], staff, request)
    _require_staff(staff)
    ts.update_telehealth_encounter(encounter_id, patient_location=body.location)
    return {"ok": True, "pos": pos_from_location(body.location)}


@router.post("/api/telehealth/encounters/{encounter_id}/documentation")
async def save_documentation(
    encounter_id: str,
    body: DocumentationBody,
    request: Request,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    ts = _ts(request)
    enc = ts.get_telehealth_encounter(encounter_id)
    if not enc:
        raise HTTPException(status_code=404, detail="Encounter not found")
    _assert_patient_access(enc["patient_id"], staff, request)
    _require_staff(staff)
    ts.update_telehealth_encounter(
        encounter_id,
        documentation={"note": body.documentation, "updated_at": datetime.utcnow().isoformat()},
    )
    return {"ok": True}


@router.post("/api/telehealth/encounters/{encounter_id}/end")
async def end_encounter(
    encounter_id: str,
    body: EndEncounterBody,
    request: Request,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    ts = _ts(request)
    enc = ts.get_telehealth_encounter(encounter_id)
    if not enc:
        raise HTTPException(status_code=404, detail="Encounter not found")
    _assert_patient_access(enc["patient_id"], staff, request)
    _require_staff(staff)
    outcome = (body.outcome or "COMPLETED").upper()
    now = datetime.utcnow().replace(microsecond=0).isoformat()
    duration = _compute_end_duration_seconds(enc, body.duration_seconds)
    connected = duration if duration > 0 else int(enc.get("connected_seconds") or 0)
    ts.update_telehealth_encounter(
        encounter_id,
        status=outcome,
        ended_at=now,
        duration_seconds=duration,
        connected_seconds=connected,
        last_heartbeat_at=now,
    )
    provider = get_video_provider()
    if enc.get("vendor_session_id"):
        await provider.end_session(session_id=enc["vendor_session_id"])
    ts.log_event(
        patient_id=enc["patient_id"],
        event_type="telehealth_ended",
        payload={"encounter_id": encounter_id, "outcome": outcome, "duration_seconds": duration},
    )
    minutes = duration // 60
    gcode = None
    if outcome == "COMPLETED" and enc.get("patient_type"):
        gcode = map_gcode(enc["patient_type"], minutes)
    return {
        "ok": True,
        "duration_minutes": minutes,
        "hcpcs_code": gcode,
        "requires_l45": requires_l45_gate(gcode) if gcode else False,
    }


@router.post("/api/telehealth/encounters/{encounter_id}/attest")
async def post_attestation(
    encounter_id: str,
    body: AttestBody,
    request: Request,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    ts = _ts(request)
    enc = ts.get_telehealth_encounter(encounter_id)
    if not enc:
        raise HTTPException(status_code=404, detail="Encounter not found")
    _assert_patient_access(enc["patient_id"], staff, request)
    staff = _require_staff(staff)
    att = {"type": body.type, "note": body.note, "by": staff.email, "at": datetime.utcnow().isoformat()}
    ts.update_telehealth_encounter(encounter_id, l45_attestation=att)
    ts.log_event(patient_id=enc["patient_id"], event_type="telehealth_l45_attestation", payload={"encounter_id": encounter_id, **att})
    return {"ok": True}


@router.post("/api/telehealth/encounters/{encounter_id}/build-claim")
async def build_claim(
    encounter_id: str,
    request: Request,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    ts = _ts(request)
    enc = ts.get_telehealth_encounter(encounter_id)
    if not enc:
        raise HTTPException(status_code=404, detail="Encounter not found")
    _assert_patient_access(enc["patient_id"], staff, request)
    staff = _require_staff(staff)
    if enc.get("status") not in ("COMPLETED",):
        raise HTTPException(status_code=400, detail="Encounter must be completed to build a claim.")
    if enc.get("status") in ("NO_SHOW", "REDIRECTED_TO_ED"):
        raise HTTPException(status_code=400, detail="No claim for this encounter outcome.")
    duration = int(enc.get("duration_seconds") or 0)
    minutes = max(1, duration // 60) if duration else 0
    patient_type = enc.get("patient_type") or "ESTABLISHED"
    hcpcs = map_gcode(patient_type, minutes)
    if not hcpcs:
        raise HTTPException(status_code=400, detail="Duration too short for a billable G-code.")
    if requires_l45_gate(hcpcs) and not (enc.get("l45_attestation") or enc.get("l45_attestation_json")):
        raise HTTPException(status_code=400, detail="L4/L5 attestation required before claim build.")
    try:
        enforce_ride_alone(1)
    except RideAloneViolation as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    pos = pos_from_location(enc.get("patient_location") or "HOME")
    claim_id = str(uuid.uuid4())
    audit = [{"action": "build_claim", "by": staff.email, "at": datetime.utcnow().isoformat()}]
    claim = ts.create_telehealth_claim(
        claim_id=claim_id,
        encounter_id=encounter_id,
        patient_id=enc["patient_id"],
        hcpcs_code=hcpcs,
        pos=pos,
        type_of_bill=TYPE_OF_BILL,
        revenue_code=REVENUE_CODE,
        demo_code=DEMO_CODE,
        ride_alone=RIDE_ALONE,
        duration_minutes=minutes,
        health_system_id=enc.get("health_system_id"),
        audit_trail=audit,
    )
    ts.update_telehealth_encounter(encounter_id, claim_id=claim_id, hcpcs_code=hcpcs)
    ts.log_event(patient_id=enc["patient_id"], event_type="telehealth_claim_built", payload={"encounter_id": encounter_id, "claim_id": claim_id})
    return {"claim_id": claim_id, "hcpcs_code": hcpcs, "claim": claim}


@router.get("/api/telehealth/encounters/{encounter_id}")
async def get_encounter_api(
    encounter_id: str,
    request: Request,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    ts = _ts(request)
    enc = ts.get_telehealth_encounter(encounter_id)
    if not enc:
        raise HTTPException(status_code=404, detail="Encounter not found")
    _assert_patient_access(enc["patient_id"], staff, request)
    _require_staff(staff)
    patient_type = enc.get("patient_type") or "ESTABLISHED"
    minutes = int(enc.get("connected_seconds") or enc.get("duration_seconds") or 0) // 60
    ladder = _serialize_ladder_next(patient_type, minutes)
    return {
        "encounter": enc,
        "ladder_next": ladder,
        "connected_seconds": int(enc.get("connected_seconds") or 0),
        "patient_summary": _patient_summary(request, enc["patient_id"]),
    }


@router.get("/api/telehealth/encounters/{encounter_id}/patient-join-token")
async def patient_join_token(encounter_id: str, t: str, request: Request):
    payload = decode_telehealth_join_token(t)
    if not payload or payload.get("enc") != encounter_id:
        raise HTTPException(status_code=401, detail="Invalid or expired join link.")
    ts = _ts(request)
    enc = ts.get_telehealth_encounter(encounter_id)
    if not enc:
        raise HTTPException(status_code=404, detail="Encounter not found")
    patient = _patients(request).get(enc["patient_id"]) or {}
    first = (patient.get("name") or "Patient").split()[0]
    provider = get_video_provider()
    if not enc.get("vendor_session_id"):
        session = await provider.create_session(encounter_id=encounter_id)
        ts.update_telehealth_encounter(
            encounter_id,
            vendor_session_id=session.session_id,
            room_url=session.room_url,
            provider=session.provider,
        )
        enc = ts.get_telehealth_encounter(encounter_id) or enc
    session_obj = type("S", (), {
        "provider": enc.get("provider") or "daily",
        "session_id": enc.get("vendor_session_id") or f"stub-{encounter_id}",
        "room_url": enc.get("room_url") or f"/telehealth/unavailable?encounter={encounter_id}",
    })()
    token = await provider.issue_token(session=session_obj, display_name=first, is_owner=False)
    return {"join_url": token.join_url}


@router.get("/api/telehealth/claims/{claim_id}/download")
async def download_claim(
    claim_id: str,
    request: Request,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    ts = _ts(request)
    claim = ts.get_telehealth_claim(claim_id)
    if not claim:
        raise HTTPException(status_code=404, detail="Claim not found")
    enc = ts.get_telehealth_encounter(claim["encounter_id"])
    if enc:
        _assert_patient_access(enc["patient_id"], staff, request)
    _require_staff(staff)
    patient = _patients(request).get(claim["patient_id"]) or {}
    last_name = (patient.get("name") or "patient").split()[-1]
    service_date = (enc or {}).get("ended_at") or claim.get("created_at") or ""
    service_date = service_date[:10] if service_date else "unknown"
    filename = f"claim_{last_name}_{service_date}_{claim.get('hcpcs_code')}.txt"
    body_lines = [
        "TEAM TELEHEALTH DRAFT CLAIM",
        "===========================",
        f"Patient: {patient.get('name') or claim['patient_id']}",
        f"Patient ID: {claim['patient_id']}",
        f"Service date: {service_date}",
        f"HCPCS: {claim.get('hcpcs_code')}",
        f"POS: {claim.get('pos')}",
        f"Type of bill: {claim.get('type_of_bill')}",
        f"Revenue code: {claim.get('revenue_code')}",
        f"Demo code: {claim.get('demo_code')}",
        f"Ride alone: {claim.get('ride_alone')}",
        f"Duration (minutes): {claim.get('duration_minutes')}",
        f"Status: {claim.get('status')}",
        "",
        "L4/L5 Attestation:",
        json.dumps((enc or {}).get("l45_attestation") or {}, indent=2),
        "",
        "Audit trail:",
        json.dumps(claim.get("audit_trail") or [], indent=2),
    ]
    content = "\n".join(body_lines)
    if enc:
        ts.log_event(patient_id=enc["patient_id"], event_type="claim_downloaded", payload={"claim_id": claim_id})
    return PlainTextResponse(
        content,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/telehealth/setup/{encounter_id}", response_class=HTMLResponse)
async def telehealth_setup_page(encounter_id: str, request: Request):
    ts = _ts(request)
    if not ts.get_telehealth_encounter(encounter_id):
        raise HTTPException(status_code=404, detail="Encounter not found")
    html = _read_frontend("telehealth-setup.html").replace("__ENCOUNTER_ID__", encounter_id)
    return HTMLResponse(content=html)


@router.get("/telehealth/room/{encounter_id}", response_class=HTMLResponse)
async def telehealth_room_page(encounter_id: str, request: Request):
    ts = _ts(request)
    if not ts.get_telehealth_encounter(encounter_id):
        raise HTTPException(status_code=404, detail="Encounter not found")
    html = _read_frontend("telehealth-room.html").replace("__ENCOUNTER_ID__", encounter_id)
    return HTMLResponse(content=html)


@router.get("/telehealth/join/{encounter_id}", response_class=HTMLResponse)
async def telehealth_join_page(encounter_id: str, t: str, request: Request):
    payload = decode_telehealth_join_token(t)
    if not payload or payload.get("enc") != encounter_id:
        raise HTTPException(status_code=401, detail="Invalid or expired join link.")
    ts = _ts(request)
    if not ts.get_telehealth_encounter(encounter_id):
        raise HTTPException(status_code=404, detail="Encounter not found")
    html = (
        _read_frontend("telehealth-join.html")
        .replace("__ENCOUNTER_ID__", encounter_id)
        .replace("__JOIN_TOKEN__", t)
    )
    return HTMLResponse(content=html)


@router.get("/telehealth/unavailable", response_class=HTMLResponse)
async def telehealth_unavailable():
    return HTMLResponse(
        "<html><body style='font-family:sans-serif;padding:40px;text-align:center;'>"
        "<h1>Video visit unavailable</h1>"
        "<p>Configure <code>DAILY_API_KEY</code> to enable telehealth video.</p>"
        "</body></html>"
    )
