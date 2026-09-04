"""Data-request broadcasts: tell every partner who may upload what we need.

Modeled line for line on ``task_notify.py``, and for the same two reasons. One
broadcast is up to (partners x members) letters, so delivery is decoupled from
the admin's request via a persistent outbox (``hs_request_outbox``); and a worker
that dies mid-send must lose nothing, so unsent rows just stay ``pending`` until
the next drain rather than living in a ``BackgroundTasks`` handle that died with
the process.

WHO HEARS A BROADCAST is the load-bearing decision here, and it is not "every
health system". It is every organization that ``hs_states.can_upload`` accepts:
ACTIVE, including the legacy NULL collapse. A partner sitting in ``intake``,
``submitted`` or ``approved_awaiting_dla`` has not signed the data licensing
agreement, and asking an unsigned partner for patient data is asking them to do
something the paperwork does not yet permit. ``declined`` hears nothing for the
obvious reason. The same gate answers ``GET /hs/requests``, so a suppressed
organization is neither mailed nor shown a list it could act on.
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from asclepius import hs_states
from asclepius.ingest_notify import _run_coro

log = logging.getLogger("asclepius.hs_request_notify")


def _app_base() -> str:
    return (os.getenv("BASE_URL") or "http://localhost:8000").strip().rstrip("/")


def _portal_url() -> str:
    """Where the letter sends them. ``ASCLEPIUS_PORTAL_URL`` first, mirroring the
    rest of the health-system mail, so a deployment that serves the portal from
    its own host does not mail partners a link into the API host."""
    base = (os.getenv("ASCLEPIUS_PORTAL_URL") or "").strip().rstrip("/")
    return base or f"{_app_base()}/provider"


def _specialty_label(specialty: str) -> str:
    return (specialty or "").strip().replace("_", " ").title() or "general"


def _idempotency_key(*, request_id: str, hs_id: str, recipient_email: str) -> str:
    """(request, organization, recipient). The organization is in the key even
    though the email address would nearly always be enough: one person may hold
    accounts at two partner organizations, and each of those organizations is
    separately being asked."""
    raw = f"{request_id}|{hs_id}|{recipient_email.lower().strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def eligible_health_systems(store: Any) -> List[Dict[str, Any]]:
    """The organizations a broadcast may reach: active row, ACTIVE state.

    ``health_systems.active`` is the operator's revocation switch and
    ``onboarding_state`` is the paperwork; both have to hold, and neither
    substitutes for the other.
    """
    return [hs for hs in store.list_health_systems()
            if hs.get("active", 1) and hs_states.can_upload(hs)]


def enqueue_for_request(store: Any, *, request_id: str,
                        recipient_hs_ids: Optional[List[str]] = None) -> int:
    """Enqueue one outbox row per active portal member of every eligible
    organization — or, with ``recipient_hs_ids``, of those organizations only
    (Case Generation Fix PRD §B4: the operator picks recipients; "every active
    partner" is the select-all case). An id that is not eligible is ignored
    here, because the router has already refused it. Returns how many rows were
    newly created.

    Re-broadcasting an already-broadcast request returns 0: every key it would
    write already exists. That is deliberate rather than incidental, because the
    admin console's obvious failure mode is a second click on a button whose
    first click took a second to answer.

    Never raises. A notify problem must not fail the request the operator just
    wrote down, because the request row is the record and the letters are
    recoverable from it.
    """
    try:
        enqueued = 0
        wanted = ({str(x) for x in recipient_hs_ids}
                  if recipient_hs_ids is not None else None)
        for hs in eligible_health_systems(store):
            hs_id = hs["hs_id"]
            if wanted is not None and hs_id not in wanted:
                continue
            for member in store.list_hs_portal_users(hs_id):
                if not member.get("active"):
                    continue
                email = (member.get("email") or "").strip()
                if not email:
                    continue
                key = _idempotency_key(request_id=request_id, hs_id=hs_id,
                                       recipient_email=email)
                row_id = store.enqueue_hs_request_notification(
                    idempotency_key=key, request_id=request_id, hs_id=hs_id,
                    recipient_email=email,
                )
                if row_id is not None:
                    enqueued += 1
        return enqueued
    except Exception as exc:  # pragma: no cover - defensive; never break the request
        log.warning("hs_request_notify: enqueue_for_request(%s) failed: %s",
                    request_id, exc)
        return 0


#: Rows claimed per round-trip, for the reason spelled out in
#: ``task_notify._CLAIM_CHUNK``: the claim carries a lease, and a chunk has to
#: finish well inside it.
_CLAIM_CHUNK = 50


def drain_outbox(store: Any, *, limit: int = 500) -> Tuple[int, int]:
    """Send every ``pending`` outbox row (up to ``limit``). Returns
    ``(sent_count, failed_count)``. Never raises.

    Per-row defensiveness is the point: one address that bounces marks its own
    row failed and leaves the rest of the batch alone. A broadcast that stopped
    at the first bad address would silently under-deliver a request the operator
    believes went out.

    Rows are CLAIMED rather than listed, same as ``task_notify.drain_outbox``
    and for the same reason: this outbox has four paths into it (the 60 s loop,
    the create-request path, the admin re-drain, and the failed-row retry), and
    a plain pending SELECT let two of them mail one partner the same broadcast
    twice.
    """
    sent = 0
    failed = 0
    try:
        from email_utils import send_html_email_with_reason
        from onboarding_emails import build_hs_data_request_email

        portal_url = _portal_url()
        # One lookup per request rather than per row: a broadcast is hundreds of
        # rows against a handful of requests.
        requests: Dict[str, Any] = {}
        remaining = max(0, int(limit))
        while remaining > 0:
            batch = store.claim_hs_request_notifications(
                limit=min(_CLAIM_CHUNK, remaining))
            if not batch:
                break
            remaining -= len(batch)
            for row in batch:
                row_id = row["id"]
                email = row["recipient_email"]
                try:
                    request_id = row["request_id"]
                    if request_id not in requests:
                        requests[request_id] = store.get_hs_data_request(request_id)
                    req = requests[request_id]
                    if not req:
                        # The request was deleted out from under a pending row. There
                        # is no letter to write, and leaving it pending would retry
                        # forever on every tick.
                        store.mark_hs_request_notification_failed(
                            row_id, "data request no longer exists")
                        failed += 1
                        continue
                    html_body = build_hs_data_request_email(
                        title=req["title"],
                        specialty_label=_specialty_label(req["specialty"]),
                        case_count=int(req["case_count"]),
                        due_date=req.get("due_date") or "",
                        details=req.get("details") or "",
                        portal_url=portal_url,
                    )
                    subject = f"Data request: {req['title']}"
                    ok, reason = _run_coro(
                        send_html_email_with_reason(email, subject, html_body)
                    )
                    if ok:
                        store.mark_hs_request_notification_sent(row_id)
                        store.log_event(
                            entity_type="hs_data_request", entity_id=str(request_id),
                            event_type="hs_data_request_sent",
                            payload={
                                "hs_id": row["hs_id"],
                                "recipient_domain": email.split("@")[-1] if "@" in email else None,
                            },
                        )
                        sent += 1
                    else:
                        store.mark_hs_request_notification_failed(
                            row_id, reason or "email transport failed")
                        store.log_event(
                            entity_type="hs_data_request", entity_id=str(request_id),
                            event_type="hs_data_request_failed",
                            payload={"hs_id": row["hs_id"], "reason": reason},
                        )
                        failed += 1
                except Exception as exc:  # pragma: no cover - defensive per-row
                    log.warning("hs_request_notify: drain row %s failed: %s", row_id, exc)
                    try:
                        store.mark_hs_request_notification_failed(row_id, str(exc))
                    except Exception:
                        # Swallowing this is what turns one bad send into a
                        # repeated one: the row keeps its 'pending' status and a
                        # later drain re-sends it once the claim lease expires.
                        log.warning(
                            "hs_request_notify: could not mark row %s failed; it "
                            "stays pending and will be retried after the claim "
                            "lease", row_id, exc_info=True,
                        )
                    failed += 1
        return sent, failed
    except Exception as exc:  # pragma: no cover - defensive; never break the caller
        log.warning("hs_request_notify: drain_outbox failed: %s", exc)
        return sent, failed
