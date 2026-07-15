"""Buyer data workspace — admin send-to-buyer + the locked-down buyer portal.

Two strictly role-separated surfaces (mirrors the Data Provider Portal):
  * ``/admin/buyer-deliveries*`` — admin builds an export scoped to one or more
    organizations (+ optional time window), provisions/rotates a ``buyer``
    account, records the delivery, and emails the buyer their credentials + a
    link to their secure workspace.
  * ``/buyer/*`` — the locked-down ``buyer`` portal: sign in, forced first-login
    password reset, and download every dataset delivered to this email. Data
    sent to a buyer ALWAYS appears here because each delivery is stored per
    account (``buyer_deliveries``) and looked up on sign-in.

Reuses the built export + zip machinery in ``asclepius/export.py`` unchanged —
this router only adds the buyer front door and the delivery association.
"""

from __future__ import annotations

import io
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from ratelimit import rate_limiter

from asclepius import auth as asc_auth
from asclepius import export as asc_export
from asclepius import profiles as asc_profiles
from asclepius.schemas import BuyerDeliveryRequest, ProviderPasswordRequest
from asclepius.store import get_store, verify_password
from email_utils import is_email_transport_configured, send_html_email
from onboarding_emails import build_buyer_delivery_email
from tenant_utils import generate_secure_password

# The private identity-value collector already used by the admin scoped exports —
# reused so a buyer batch gets the same defense-in-depth value leak scan.
from routers.asclepius import _identifying_values

log = logging.getLogger("asclepius.buyer")

router = APIRouter(prefix="/api/asclepius", tags=["asclepius-buyer"])


def _store():
    return get_store()


def _email_configured() -> bool:
    return is_email_transport_configured()


def _app_base() -> str:
    return (os.getenv("BASE_URL") or "http://localhost:8000").strip().rstrip("/")


def _workspace_base() -> str:
    return (os.getenv("ASCLEPIUS_PORTAL_URL") or _app_base()).strip().rstrip("/")


def _workspace_url() -> str:
    return _workspace_base() + "/workspace"


def _invite_ttl_days() -> int:
    try:
        return max(1, int(os.getenv("ASCLEPIUS_BUYER_INVITE_TTL_DAYS", "14")))
    except ValueError:
        return 14


def _invite_expiry_iso() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=_invite_ttl_days())).isoformat()


# ════════════════════════════════════════════════════════════════════════════
#  Admin — send a dataset to a buyer
# ════════════════════════════════════════════════════════════════════════════
@router.post("/admin/buyer-deliveries")
async def send_buyer_delivery(
    body: BuyerDeliveryRequest,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Package the selected organizations' data (optionally within a time window)
    and deliver it to a buyer's secure workspace. Builds the export (Tier A only,
    leak-gated), provisions/rotates the buyer account, records the delivery, and
    emails the buyer credentials + the workspace link. 503 if email isn't
    configured — we never deliver without being able to tell the buyer."""
    if not _email_configured():
        raise HTTPException(status_code=503, detail="Email is not configured (SendGrid or SMTP).")
    store = _store()

    orgs = [o for o in (body.organizations or []) if (o or "").strip()]
    if not orgs:
        raise HTTPException(status_code=400, detail="Select at least one organization to send.")

    # Union the hashed annotator ids across every selected organization, and
    # gather their private-vault values for the defense-in-depth leak scan.
    hashed_ids: List[str] = []
    verify_values: List[str] = []
    for org in orgs:
        ids = store.hashed_ids_for_organization(org)
        for h in ids:
            if h not in hashed_ids:
                hashed_ids.append(h)
                verify_values += _identifying_values(store, h)
    if not hashed_ids:
        raise HTTPException(status_code=404, detail="No contributors found for the selected organization(s).")

    label = ", ".join(orgs)
    scope = {"type": "buyer_delivery", "organizations": orgs, "buyer_email": body.buyer_email}
    try:
        manifest = asc_export.build_export(
            store,
            created_by=admin["id"],
            profile=body.profile,
            note=body.note,
            include_exported=body.include_exported,
            annotator_ids=hashed_ids,
            verify_values=sorted(set(verify_values)),
            since=body.since,
            until=body.until,
            scope=scope,
        )
    except asc_export.ExportValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except asc_profiles.ProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Provision (or rotate) the buyer account. First delivery mints credentials;
    # a returning buyer keeps their password (their workspace already exists).
    existing = store.get_buyer_account_by_email(body.buyer_email)
    first_delivery = existing is None
    temp_password = generate_secure_password()
    if first_delivery:
        buyer = store.provision_buyer(
            email=body.buyer_email, password=temp_password, buyer_name=body.buyer_name,
            note=body.note, invited_by=admin["id"], invite_expires_at=_invite_expiry_iso(),
        )
    else:
        buyer = existing

    delivery = store.record_buyer_delivery(
        buyer_account_id=buyer["buyer_account_id"], buyer_email=body.buyer_email,
        export_id=manifest["export_id"], label=label,
        data_format=body.data_format or body.profile,
        record_count=manifest.get("record_count") or 0, note=body.note, sent_by=admin["id"],
    )

    html_body = build_buyer_delivery_email(
        workspace_url=_workspace_url(), email=body.buyer_email,
        temporary_password=temp_password, buyer_name=body.buyer_name,
        datasets_label=label, data_format=(body.data_format or body.profile),
        record_count=manifest.get("record_count") or 0, note=body.note or "",
        invite_ttl_days=_invite_ttl_days(), first_delivery=first_delivery,
    )
    email_ok = await send_html_email(
        body.buyer_email, "Your Archangel Health dataset is ready", html_body,
        importance_headers=True,
    )
    store.log_event(entity_type="buyer_delivery", entity_id=delivery["delivery_id"],
                    event_type="delivery_sent", actor=admin["id"],
                    payload={"buyer_email": body.buyer_email, "export_id": manifest["export_id"],
                             "organizations": orgs, "record_count": manifest.get("record_count"),
                             "email_ok": email_ok, "first_delivery": first_delivery})
    return {
        "delivery_id": delivery["delivery_id"],
        "export_id": manifest["export_id"],
        "record_count": manifest.get("record_count") or 0,
        "buyer_email": body.buyer_email,
        "workspace_url": _workspace_url(),
        "first_delivery": first_delivery,
        "email_sent": email_ok,
        "message": (f"Delivered {manifest.get('record_count') or 0} record(s) to "
                    f"{body.buyer_email} — {'account created and ' if first_delivery else ''}"
                    f"credentials emailed." if email_ok
                    else "Delivery recorded, but the notification email could not be sent."),
    }


@router.get("/admin/buyer-deliveries")
async def list_buyer_deliveries_admin(_admin: Dict[str, Any] = Depends(asc_auth.require_admin)):
    """Delivery history for the admin: who received what, when, and how many
    records (with the product-version mix from the underlying export)."""
    store = _store()
    out = []
    for d in store.list_buyer_deliveries():
        exp = store.get_export(d["export_id"]) or {}
        manifest = exp.get("manifest") or {}
        out.append({
            **d,
            "by_portal_version": (manifest.get("counts") or {}).get("by_portal_version") or {},
        })
    return {"deliveries": out, "buyers": store.list_buyer_accounts()}


# ════════════════════════════════════════════════════════════════════════════
#  Buyer portal — the locked-down buyer surface
# ════════════════════════════════════════════════════════════════════════════
@router.get("/buyer/me")
async def buyer_me(buyer_user: Dict[str, Any] = Depends(asc_auth.require_buyer)):
    store = _store()
    b = store.get_buyer_account(buyer_user["id"]) or {}
    deliveries = store.list_buyer_deliveries(buyer_account_id=buyer_user["id"])
    return {
        "email": buyer_user.get("email"),
        "buyer_name": b.get("buyer_name"),
        "status": b.get("status") or "active",
        "must_reset_password": bool(b.get("must_reset_password")),
        "delivery_count": len(deliveries),
    }


@router.post("/buyer/password",
             dependencies=[Depends(rate_limiter("buyer_password", 10, 60))])
async def buyer_password(
    body: ProviderPasswordRequest,
    buyer_user: Dict[str, Any] = Depends(asc_auth.require_buyer),
):
    """Forced first-login reset (and normal change), mirroring the provider flow."""
    store = _store()
    b = store.get_buyer_account(buyer_user["id"]) or {}
    if len((body.new_password or "").strip()) < 12:
        raise HTTPException(status_code=400, detail="New password must be at least 12 characters.")
    if not b.get("must_reset_password"):
        full = store.get_user_by_id(buyer_user["id"]) or {}
        if not verify_password(body.current_password or "", full.get("password_hash") or ""):
            raise HTTPException(status_code=400, detail="Current password is incorrect.")
    store.set_user_password(buyer_user["id"], body.new_password)
    store.clear_buyer_password_reset(buyer_user["id"])
    store.log_event(entity_type="buyer_account", entity_id=buyer_user["id"],
                    event_type="password_reset", actor=buyer_user["id"])
    return {"ok": True}


@router.get("/buyer/deliveries")
async def buyer_deliveries(buyer_user: Dict[str, Any] = Depends(asc_auth.require_buyer)):
    """Every dataset delivered to this buyer — scoped strictly to their account."""
    store = _store()
    out = []
    for d in store.list_buyer_deliveries(buyer_account_id=buyer_user["id"]):
        out.append({
            "delivery_id": d["delivery_id"],
            "export_id": d["export_id"],
            "label": d.get("label"),
            "data_format": d.get("data_format"),
            "record_count": d.get("record_count") or 0,
            "note": d.get("note"),
            "sent_at": d.get("sent_at"),
        })
    return {"deliveries": out}


@router.get("/buyer/deliveries/{export_id}/download")
async def buyer_download_delivery(
    export_id: str, buyer_user: Dict[str, Any] = Depends(asc_auth.require_buyer)
):
    """Stream a delivered dataset — but ONLY if it was delivered to THIS buyer."""
    store = _store()
    mine = store.list_buyer_deliveries(buyer_account_id=buyer_user["id"], export_id=export_id)
    if not mine:
        raise HTTPException(status_code=404, detail="Dataset not found in your workspace.")
    export = store.get_export(export_id)
    if not export:
        raise HTTPException(status_code=404, detail="Dataset is no longer available.")
    data = asc_export.zip_export(export)
    store.log_event(entity_type="buyer_delivery", entity_id=export_id,
                    event_type="delivery_downloaded", actor=buyer_user["id"])
    headers = {"Content-Disposition": f'attachment; filename="{export_id}.zip"'}
    return StreamingResponse(io.BytesIO(data), media_type="application/zip", headers=headers)
