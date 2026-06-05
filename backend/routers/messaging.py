"""Care-team ↔ patient threaded messaging."""

from __future__ import annotations

import html as html_lib
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from staff_context import StaffContext, get_staff_context_optional

router = APIRouter(tags=["messaging"])


def _ts(request: Request):
    return request.app.state.team_store


def _patients(request: Request) -> dict:
    return request.app.state.patient_store


def _provider_role_display(role: Optional[str]) -> str:
    from main import _provider_role_display as _prd  # noqa: PLC0415

    return _prd(role)


def _provider_email_signature(staff: Optional[StaffContext]) -> str:
    from main import _provider_email_signature as _pes  # noqa: PLC0415

    return _pes(staff)


def _assert_clinical_staff_can_access_patient(patient_id: str, staff: Optional[StaffContext], request: Request) -> None:
    from main import _assert_clinical_staff_can_access_patient as _assert  # noqa: PLC0415

    _assert(patient_id, staff)


def _require_clinical_staff(staff: Optional[StaffContext]) -> StaffContext:
    from main import _require_clinical_staff as _req  # noqa: PLC0415

    return _req(staff)


def _care_team_sender_label(msg: Dict[str, Any]) -> str:
    name = (msg.get("sender_name") or "").strip()
    role = _provider_role_display(msg.get("sender_role"))
    if name and role:
        return f"{name}, {role}"
    return name or role or "Care Team"


def _build_care_team_notification_email_html(
    *,
    patient_name: str,
    sender_signature: str,
    clinic_code: str,
    resource_code: str,
    entry_url: str,
) -> str:
    first_name = html_lib.escape((patient_name or "Patient").split()[0])
    sender_safe = html_lib.escape(sender_signature)
    clinic_safe = html_lib.escape(clinic_code or "N/A")
    resource_safe = html_lib.escape(resource_code or "N/A")
    url_safe = html_lib.escape(entry_url or "#", quote=True)
    return f"""
    <html><body style="margin:0;padding:20px;background:#f8fafc;
    font-family:Inter,-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;color:#0f172a;">
    <div style="max-width:680px;margin:0 auto;background:#ffffff;border:1px solid #e2e8f0;
    border-radius:12px;padding:22px;">
      <div style="font-size:12px;font-weight:700;letter-spacing:0.08em;color:#1d4ed8;
      text-transform:uppercase;margin-bottom:10px;">Archangel Health</div>
      <h2 style="font-size:22px;line-height:1.25;margin:0 0 8px;">Hi {first_name},</h2>
      <p style="margin:0 0 16px;font-size:15px;line-height:1.65;color:#334155;">
        <strong>{sender_safe}</strong> has sent you a secure message through Archangel Health.
        Sign in with your access codes to read it in your recovery dashboard.
      </p>
      <div style="font-size:11px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;
      color:#64748b;text-align:center;margin:18px 0 8px;">Health System Code</div>
      <div style="background:#f8fafc;border:1px solid #cbd5e1;border-radius:12px;padding:16px;text-align:center;margin-bottom:12px;">
        <div style="font-family:monospace;font-size:28px;font-weight:800;letter-spacing:0.12em;">{clinic_safe}</div>
      </div>
      <div style="font-size:11px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;
      color:#64748b;text-align:center;margin:8px 0;">Resource Code</div>
      <div style="background:#f8fafc;border:1px solid #cbd5e1;border-radius:12px;padding:16px;text-align:center;margin-bottom:18px;">
        <div style="font-family:monospace;font-size:28px;font-weight:800;letter-spacing:0.12em;">{resource_safe}</div>
      </div>
      <a href="{url_safe}" style="display:block;text-decoration:none;text-align:center;background:#0891b2;
      color:#ffffff;border-radius:12px;padding:14px 16px;font-size:16px;font-weight:700;">
        Open your recovery dashboard
      </a>
      <p style="margin:16px 0 0;font-size:12px;line-height:1.55;color:#64748b;">
        This message may contain confidential medical information intended only for the recipient.
        If you received this in error, please contact your care team directly.
      </p>
    </div></body></html>
    """


async def persist_and_notify_care_team_message(
    request: Request,
    *,
    patient_id: str,
    message: str,
    staff: StaffContext,
    escalation_id: Optional[int] = None,
    urgent: bool = False,
) -> Dict[str, Any]:
    """Persist CARE_TEAM message and send notification-only email when possible."""
    from email_utils import is_email_transport_configured  # noqa: PLC0415
    from main import _send_html_email_with_reason_impl  # noqa: PLC0415

    ts = _ts(request)
    patients = _patients(request)
    patient = patients.get(patient_id) or {}
    health_system_id = patient.get("health_system_id")

    message_id = ts.create_care_team_message(
        patient_id=patient_id,
        sender_type="CARE_TEAM",
        body=message,
        escalation_id=escalation_id,
        health_system_id=health_system_id,
        sender_role=staff.role,
        sender_name=staff.name,
        sender_email=staff.email,
    )

    patient_email = (patient.get("email") or "").strip()
    emailed = False
    email_reason: Optional[str] = None

    if not is_email_transport_configured():
        email_reason = "email_transport_not_configured"
    elif not patient_email:
        email_reason = "no_patient_email"
    else:
        base_url = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
        landing_url = (os.getenv("LANDING_URL") or "").strip().rstrip("/")
        entry_url = f"{landing_url}/#recovery-plan" if landing_url else f"{base_url}/patient/{patient_id}"
        clinic_code = (patient.get("clinic_code") or "").strip()
        resource_code = (patient.get("resource_code") or "").strip()
        signature = _provider_email_signature(staff)
        subject_suffix = "URGENT CARE MESSAGE" if urgent else "New secure message"
        subject = f"{signature} — {subject_suffix}"
        html_body = _build_care_team_notification_email_html(
            patient_name=str(patient.get("name") or "Patient"),
            sender_signature=signature,
            clinic_code=clinic_code,
            resource_code=resource_code,
            entry_url=entry_url,
        )
        ok, fail_reason = await _send_html_email_with_reason_impl(
            patient_email,
            subject,
            html_body,
            importance_headers=True,
        )
        if ok:
            emailed = True
        else:
            email_reason = fail_reason or "send_failed"

    ts.log_event(
        patient_id=patient_id,
        event_type="care_team_message_sent",
        payload={
            "message_id": message_id,
            "sender_role": staff.role,
            "sender_email": staff.email,
            "escalation_id": escalation_id,
            "emailed": emailed,
            "email_reason": email_reason,
        },
    )
    return {
        "ok": True,
        "message_id": message_id,
        "emailed": emailed,
        "reason": email_reason,
    }


class CareTeamMessageSendBody(BaseModel):
    message: str
    escalation_id: Optional[int] = None


class CareTeamPatientReplyBody(BaseModel):
    message: str
    recipient_email: str
    recipient_role: Optional[str] = None
    in_reply_to: Optional[int] = None


def _serialize_clinician_messages(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for msg in rows:
        out.append(
            {
                "id": msg["id"],
                "sender_type": msg["sender_type"],
                "sender_role": msg.get("sender_role"),
                "sender_name": msg.get("sender_name"),
                "sender_email": msg.get("sender_email"),
                "sender_label": _care_team_sender_label(msg) if msg["sender_type"] == "CARE_TEAM" else "Patient",
                "recipient_role": msg.get("recipient_role"),
                "recipient_email": msg.get("recipient_email"),
                "body": msg.get("body"),
                "escalation_id": msg.get("escalation_id"),
                "created_at": msg.get("created_at"),
                "read_by_patient": msg.get("read_by_patient"),
                "read_by_care_team": msg.get("read_by_care_team"),
            }
        )
    return out


def _serialize_patient_messages(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for msg in rows:
        if msg["sender_type"] == "CARE_TEAM":
            direction = "incoming"
            sender_label = _care_team_sender_label(msg)
            read = bool(msg.get("read_by_patient"))
        else:
            direction = "outgoing"
            sender_label = "You"
            read = True
        out.append(
            {
                "id": msg["id"],
                "direction": direction,
                "sender_label": sender_label,
                "sender_role": msg.get("sender_role"),
                "sender_email": msg.get("sender_email"),
                "recipient_email": msg.get("recipient_email"),
                "recipient_role": msg.get("recipient_role"),
                "body": msg.get("body"),
                "created_at": msg.get("created_at"),
                "read": read,
            }
        )
    return out


@router.post("/api/patients/{patient_id}/care-team-messages")
async def clinician_send_care_team_message(
    patient_id: str,
    body: CareTeamMessageSendBody,
    request: Request,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    if patient_id not in _patients(request):
        raise HTTPException(status_code=404, detail="Patient not found")
    _assert_clinical_staff_can_access_patient(patient_id, staff, request)
    staff = _require_clinical_staff(staff)
    message = (body.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message is required.")
    urgent = False
    if body.escalation_id:
        esc = _ts(request).get_escalation(body.escalation_id)
        if esc and int(esc.get("tier") or 0) >= 3:
            urgent = True
    result = await persist_and_notify_care_team_message(
        request,
        patient_id=patient_id,
        message=message,
        staff=staff,
        escalation_id=body.escalation_id,
        urgent=urgent,
    )
    return result


@router.get("/api/patients/{patient_id}/care-team-messages")
async def clinician_list_care_team_messages(
    patient_id: str,
    request: Request,
    staff: Optional[StaffContext] = Depends(get_staff_context_optional),
):
    if patient_id not in _patients(request):
        raise HTTPException(status_code=404, detail="Patient not found")
    _assert_clinical_staff_can_access_patient(patient_id, staff, request)
    _require_clinical_staff(staff)
    ts = _ts(request)
    rows = ts.list_care_team_messages(patient_id)
    ts.mark_care_team_thread_read(patient_id, by="care_team")
    return {
        "messages": _serialize_clinician_messages(rows),
        "unread_from_patient": ts.count_unread_for_care_team(patient_id),
        "clinicians_in_thread": ts.list_care_team_clinicians_in_thread(patient_id),
    }


@router.get("/api/patient/{patient_id}/care-team-messages")
async def patient_list_care_team_messages(patient_id: str, request: Request):
    if patient_id not in _patients(request):
        raise HTTPException(status_code=404, detail="Patient not found")
    ts = _ts(request)
    rows = ts.list_care_team_messages(patient_id)
    ts.mark_care_team_thread_read(patient_id, by="patient")
    ts.log_event(
        patient_id=patient_id,
        event_type="care_team_message_viewed",
        payload={"count": len(rows)},
    )
    clinicians = ts.list_care_team_clinicians_in_thread(patient_id)
    return {
        "messages": _serialize_patient_messages(rows),
        "unread_count": ts.count_unread_for_patient(patient_id),
        "clinicians_in_thread": [
            {
                "email": c.get("sender_email"),
                "name": c.get("sender_name"),
                "role": c.get("sender_role"),
                "label": _care_team_sender_label(c),
            }
            for c in clinicians
        ],
    }


@router.post("/api/patient/{patient_id}/care-team-messages/reply")
async def patient_reply_care_team_message(
    patient_id: str,
    body: CareTeamPatientReplyBody,
    request: Request,
):
    if patient_id not in _patients(request):
        raise HTTPException(status_code=404, detail="Patient not found")
    message = (body.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message is required.")
    recipient_email = (body.recipient_email or "").strip()
    if not recipient_email:
        raise HTTPException(status_code=400, detail="Recipient is required.")
    ts = _ts(request)
    patient = _patients(request).get(patient_id) or {}
    clinicians = ts.list_care_team_clinicians_in_thread(patient_id)
    valid_emails = {c.get("sender_email") for c in clinicians if c.get("sender_email")}
    if recipient_email not in valid_emails:
        raise HTTPException(status_code=400, detail="Invalid recipient.")
    recipient_role = body.recipient_role
    if not recipient_role:
        for c in clinicians:
            if c.get("sender_email") == recipient_email:
                recipient_role = c.get("sender_role")
                break
    message_id = ts.create_care_team_message(
        patient_id=patient_id,
        sender_type="PATIENT",
        body=message,
        health_system_id=patient.get("health_system_id"),
        recipient_email=recipient_email,
        recipient_role=recipient_role,
    )
    ts.log_event(
        patient_id=patient_id,
        event_type="patient_care_team_reply",
        payload={
            "message_id": message_id,
            "recipient_role": recipient_role,
            "recipient_email": recipient_email,
            "in_reply_to": body.in_reply_to,
        },
    )
    return {"ok": True, "message_id": message_id}
