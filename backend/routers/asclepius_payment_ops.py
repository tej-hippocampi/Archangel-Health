"""Admin-only payment operations. No approval or filing is inferred from a GET."""
from datetime import datetime, timezone
from typing import List, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from asclepius import auth, payment_ops as ops, stripe_rail as rail
from asclepius.store import get_store
import realm

router = APIRouter(prefix='/api/asclepius/admin/payment-ops', dependencies=[Depends(auth.require_admin)])


def call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (ops.OpsError, rail.RailUnavailable) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def writable():
    if realm.is_sandbox():
        raise HTTPException(status_code=403, detail='Payment operations are read-only in the sandbox realm.')


class BatchDraft(BaseModel):
    earning_ids: List[str] = Field(min_length=1, max_length=200)


class Approval(BaseModel):
    fingerprint: str = Field(min_length=64, max_length=64)


class TaxReview(BaseModel):
    tax_year: int = Field(ge=2020, le=2100)
    form_type: Literal['1099-NEC'] = '1099-NEC'
    calculation_method: Literal['payments_including_fees', 'payments_excluding_fees', 'payouts_only']
    platform_and_funding_checked: bool
    tax_identity_and_w9_checked: bool
    delivery_and_consent_checked: bool
    state_filing_checked: bool


class Reconciliation(BaseModel):
    tax_year: int = Field(ge=2020, le=2100)
    totals_csv: str = Field(min_length=1, max_length=1_000_000)


@router.get('')
def dashboard():
    return ops.dashboard(get_store())


@router.get('/readiness')
def readiness():
    return ops.readiness(get_store(), datetime.now(timezone.utc).year)


@router.post('/batches')
def create_batch(body: BatchDraft, admin=Depends(auth.require_admin)):
    writable()
    return call(ops.create_batch, get_store(), body.earning_ids, admin['id'])


@router.post('/batches/{batch_id}/approve')
def approve(batch_id: str, body: Approval, admin=Depends(auth.require_admin)):
    writable()
    return call(ops.approve_batch, get_store(), batch_id, body.fingerprint, admin['id'])


@router.post('/batches/{batch_id}/cancel')
def cancel(batch_id: str):
    writable()
    return call(ops.cancel_batch, get_store(), batch_id)


@router.post('/batches/{batch_id}/retry')
def retry(batch_id: str, body: Approval, admin=Depends(auth.require_admin)):
    writable()
    result = call(ops.retry_batch, get_store(), batch_id, body.fingerprint)
    get_store().log_event(entity_type='payment_batch', entity_id=batch_id,
        event_type='approved_batch_retry_requested', actor=admin['id'], payload={})
    return result


@router.post('/tax-review')
def tax_review(body: TaxReview, admin=Depends(auth.require_admin)):
    writable()
    call(ops.save_tax_review, get_store(), body.tax_year, admin['id'], body.model_dump())
    return {'ok': True, 'verification': 'administrator_attestation', 'filed': False}


@router.post('/physicians/{user_id}/tax-collection')
def tax_collection(user_id: str, admin=Depends(auth.require_admin)):
    writable()
    user = get_store().get_user_by_id(user_id) or {}
    account_id = user.get('stripe_account_id')
    if not account_id:
        raise HTTPException(status_code=409, detail='Physician has no connected Stripe account.')
    call(rail.enable_us_tax_collection, account_id)
    get_store().log_event(entity_type='user', entity_id=user_id,
        event_type='stripe_tax_collection_requested', actor=admin['id'], payload={'account_id': account_id})
    return {'ok': True, 'filing_enabled': False}


@router.post('/reconciliation')
def reconciliation(body: Reconciliation, admin=Depends(auth.require_admin)):
    writable()
    totals = call(ops.parse_totals_csv, body.totals_csv)
    return call(ops.reconcile, get_store(), body.tax_year, totals, admin['id'])


@router.get('/reconciliation/{report_id}')
def read_reconciliation(report_id: str):
    import json
    with get_store()._conn() as conn:
        row = conn.execute('SELECT report_json FROM payment_reconciliations WHERE report_id=?', (report_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail='No such reconciliation.')
    return json.loads(row[0])
