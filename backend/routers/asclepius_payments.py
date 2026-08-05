"""Earnings + billable session router (PRD-P §6).

Every doctor-facing endpoint here scopes from the SESSION. No doctor-facing route
takes a ``user_id`` in its path, query or body — a physician can read their own
ledger and nobody else's, and that is a property of the routes rather than of a
check somebody remembered to write.

``user_id`` appears on exactly two routes, both under ``/admin/`` and both gated
on ``require_admin``: the ledger view and the disbursement record. That split is
the design — naming another physician is an administrative act, so it happens only
where an administrator is proven to be doing it.

Policy lives in ``asclepius.payments``; persistence in the PRD-P sentinel block of
``asclepius.store``. This router never touches ``routers/asclepius.py``, and
imports nothing from ``review.py`` or ``routing.py``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from asclepius import auth as asc_auth
from asclepius import payments as asc_payments
from asclepius.store import get_store
from ratelimit import rate_limiter

log = logging.getLogger("asclepius.payments.router")

router = APIRouter(tags=["asclepius-payments"])


def _store():
    return get_store()


# ─── Earnings ─────────────────────────────────────────────────────────────────
@router.get("/api/asclepius/earnings")
async def my_earnings(user: Dict[str, Any] = Depends(asc_auth.get_current_user)):
    """Summary + recent ledger for the signed-in physician. Own rows only —
    the user id comes from the token and from nowhere else."""
    return asc_payments.earnings_summary(_store(), user_id=user["id"])


# ─── Sessions ─────────────────────────────────────────────────────────────────
class OpenSessionBody(BaseModel):
    kind: str = Field(default=asc_payments.SESSION_KIND_REVIEW)


@router.post(
    "/api/asclepius/sessions",
    dependencies=[Depends(rate_limiter("asclepius_session_open", 60, 600))],
)
async def open_session(
    body: OpenSessionBody,
    user: Dict[str, Any] = Depends(asc_auth.get_current_user),
):
    """Open (or resume) a billable session. Idempotent — a client that opens twice
    gets the same session back, with the nonce it must beat with."""
    kind = (body.kind or "").strip().lower()
    if kind != asc_payments.SESSION_KIND_REVIEW:
        # One kind exists today. Rejecting anything else keeps a typo from
        # minting a whole parallel class of billable session nobody priced.
        raise HTTPException(status_code=422, detail="Unsupported session kind")
    store = _store()
    try:
        return asc_payments.open_session(store, user_id=user["id"], kind=kind)
    except asc_payments.PaymentsDenied as denied:
        # Recorded, not just refused. If a labeler's client is calling this it is
        # a bug in someone's code and we want to see it; if it is not a client at
        # all, that is worth knowing about long before a payout run.
        log.warning("asclepius.payments: session open denied for %s (%s)",
                    user["id"], denied.reason)
        store.log_event(
            entity_type="work_session", entity_id=None,
            event_type="session_open_denied", actor=user["id"],
            payload={"reason": denied.reason, "kind": kind,
                     "role": user.get("role")},
        )
        raise HTTPException(status_code=403, detail=denied.detail)


class HeartbeatBody(BaseModel):
    nonce: str
    seq: Optional[int] = None
    active: bool = True
    progress_key: Optional[str] = None
    # Recorded as a FRAUD SIGNAL and never used in any calculation (PRD-P §1.3).
    client_ts: Optional[str] = None


# Beating is legitimate high-frequency traffic — a 15 s beat is 4 requests a
# minute — and is rate-limited generously enough that several tabs and a retry
# storm on a shared hospital NAT stay well inside it.
#
# Resuming is the opposite: it hands out a beating credential, so it gets a
# budget that a page reload fits inside and a script does not. The audit's bot
# needed ~1.3 credentials a minute; this allows 0.4. Named rather than inlined so
# ``test_payments_nonce.py`` can assert the RELATIONSHIP between the two rather
# than two magic numbers that could drift apart unnoticed.
HEARTBEAT_RATE_LIMIT = (600, 60)
RESUME_RATE_LIMIT = (4, 600)


@router.post(
    "/api/asclepius/sessions/{session_id}/heartbeat",
    dependencies=[Depends(rate_limiter("asclepius_session_beat", *HEARTBEAT_RATE_LIMIT))],
)
async def session_heartbeat(
    session_id: str,
    body: HeartbeatBody,
    user: Dict[str, Any] = Depends(asc_auth.get_current_user),
):
    result = asc_payments.heartbeat(
        _store(), session_id=_owned(session_id, user), nonce=body.nonce,
        active=body.active, progress_key=body.progress_key, seq=body.seq,
        client_ts=body.client_ts,
    )
    if not result.get("ok"):
        code = result.get("error")
        if code == "not_found":
            raise HTTPException(status_code=404, detail="Session not found")
        if code in asc_payments._BEAT_MALFORMED:
            # The client sent something incomplete — a beat with no progress key,
            # or no sequence number on a session that already has beats. Retrying
            # it unchanged will never work, so this is a 422 and not a 409.
            raise HTTPException(
                status_code=422, detail=result.get("message") or "Heartbeat rejected")
        # A stale nonce or a replayed seq is a CONFLICT, not a bad request: the
        # beat was well-formed and the client simply lost the race or replayed.
        # The client re-opens rather than retrying the same beat forever.
        raise HTTPException(status_code=409, detail=result.get("message") or "Heartbeat rejected")
    return result


class CloseSessionBody(BaseModel):
    reason: str = Field(default=asc_payments.END_CLOSED)


@router.post("/api/asclepius/sessions/{session_id}/close")
async def close_session(
    session_id: str,
    body: CloseSessionBody,
    user: Dict[str, Any] = Depends(asc_auth.get_current_user),
):
    """Close a session and settle it. Safe to call repeatedly — and it will be,
    because the client closes on both ``visibilitychange`` and ``pagehide``."""
    result = asc_payments.close_session(
        _store(), session_id=_owned(session_id, user), reason=body.reason)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail="Session not found")
    return result


@router.post(
    "/api/asclepius/sessions/{session_id}/resume",
    dependencies=[Depends(rate_limiter("asclepius_session_resume", *RESUME_RATE_LIMIT))],
)
async def resume_session(
    session_id: str,
    user: Dict[str, Any] = Depends(asc_auth.get_current_user),
):
    """Re-issue a beating credential to a client that legitimately lost one — a
    physician who reloaded the page mid-session.

    This is the ONLY route besides creating a session and beating on one that
    returns a nonce, and it is deliberately expensive: hard rate limit, ownership
    check, and a counter on the session row. ``open_session`` used to do this job
    for free and unlimited, which turned its idempotence into a nonce dispenser."""
    try:
        return asc_payments.resume_session(
            _store(), session_id=session_id, user_id=user["id"])
    except asc_payments.PaymentsDenied as denied:
        # A session that is not this user's and a session that does not exist get
        # the same 404, so the route cannot enumerate anyone else's sessions.
        if denied.reason == "ended":
            raise HTTPException(status_code=409, detail=denied.detail)
        raise HTTPException(status_code=404, detail="Session not found")


@router.get("/api/asclepius/sessions/{session_id}")
async def session_state(
    session_id: str,
    user: Dict[str, Any] = Depends(asc_auth.get_current_user),
):
    """Server-authoritative state, for a client that reloaded mid-session and
    needs the truth back — including a fresh nonce to resume beating with."""
    state = asc_payments.session_state(_store(), session_id=session_id, user_id=user["id"])
    if state is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return state


def _owned(session_id: str, user: Dict[str, Any]) -> str:
    """Ownership check on every session route.

    A session id is a guessable-shaped opaque string, and without this a physician
    could beat on — and close, and be credited for — a session belonging to
    somebody else. A session that exists but belongs to another user returns the
    same 404 as one that does not exist, so the endpoint cannot be used to
    enumerate other people's sessions."""
    row = _store().get_work_session(session_id)
    if row is None or row.get("user_id") != user["id"]:
        raise HTTPException(status_code=404, detail="Session not found")
    return session_id


# ─── Admin ────────────────────────────────────────────────────────────────────
@router.get("/api/asclepius/admin/earnings")
async def admin_earnings(
    user_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    payout_batch_id: Optional[str] = Query(None),
    limit: int = Query(500, ge=1, le=2000),
    _admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """The whole ledger, filterable. Reconciles first so the admin view and the
    doctor's view are never two different answers to the same question."""
    store = _store()
    try:
        asc_payments.reconcile_task_accruals(store)
    except Exception:
        log.exception("asclepius.payments: admin reconciliation failed; serving the ledger as-is")
    if status is not None and status not in asc_payments.LEDGER_STATES:
        raise HTTPException(status_code=422, detail="Unknown status filter")
    return {
        "rows": store.list_earnings(user_id=user_id, status=status,
                                    payout_batch_id=payout_batch_id, limit=limit),
        "totals": store.earnings_by_status(),
        "rates": {
            "tl_rate_cents": asc_payments.tl_rate_cents(),
            "tr_session_cents": asc_payments.tr_session_cents(),
            "tr_min_seconds": asc_payments.tr_min_seconds(),
            "tl_auto_approve_days": asc_payments.tl_auto_approve_days(),
        },
    }


class MarkPaidBody(BaseModel):
    # The idempotency key, not a label. Replaying a batch is a no-op, which is
    # what makes a retried disbursement job safe.
    payout_batch_id: str = Field(default="")
    earning_ids: Optional[List[str]] = None
    user_id: Optional[str] = None


@router.post(
    "/api/asclepius/admin/earnings/mark-paid",
    dependencies=[Depends(rate_limiter("asclepius_mark_paid", 30, 600))],
)
async def mark_paid(
    body: MarkPaidBody,
    admin: Dict[str, Any] = Depends(asc_auth.require_admin),
):
    """Record that a batch of approved earnings has actually been disbursed.

    This is the ledger's record that money moved; it does not move money. The
    transfer is a treasury operation, and ``payout_batch_id`` is how the two are
    reconciled — ``GET /admin/earnings?payout_batch_id=...`` returns exactly the
    rows a given disbursement covered.

    ``user_id`` appears here and nowhere else on this router. That is the point of
    the split: every doctor-facing route scopes from the session and cannot name
    another physician, and the one route that must name one is admin-gated."""
    try:
        return asc_payments.mark_paid(
            _store(), payout_batch_id=body.payout_batch_id, actor_id=admin["id"],
            earning_ids=body.earning_ids, user_id=body.user_id)
    except asc_payments.PaymentsDenied as denied:
        raise HTTPException(status_code=422, detail=denied.detail)
