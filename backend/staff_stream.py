"""Single-use opaque staff SSE tickets; session credentials never enter URLs."""
from dataclasses import dataclass
import hashlib
import secrets
import threading
import time
from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

import realm
from staff_context import StaffContext, get_staff_context_optional

TICKET_TTL_SECONDS = 60
MAX_PENDING_TICKETS = 4096


@dataclass(frozen=True)
class _Ticket:
    path: str
    realm: str
    authorization: str
    expires: float
    purpose: str = "staff_sse"


_tickets: dict[str, _Ticket] = {}
_lock = threading.Lock()


def mint_staff_stream_ticket(request: Request) -> JSONResponse:
    """Call only after the stream's normal staff and resource-scope checks.

    The credential stays server-side and is revalidated at redemption. A ticket
    authorizes one stream connection, never a REST endpoint or another path.
    Reconnects mint a new ticket over the authenticated header channel.
    """
    path = request.scope["path"]
    if not path.endswith("/stream-ticket"):
        raise ValueError("Stream tickets require a resource-specific ticket route")
    authorization = request.headers.get("authorization") or ""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    now = time.monotonic()
    ticket = secrets.token_urlsafe(32)
    digest = hashlib.sha256(ticket.encode()).hexdigest()
    with _lock:
        for key in [key for key, row in _tickets.items() if row.expires <= now]:
            _tickets.pop(key, None)
        if len(_tickets) >= MAX_PENDING_TICKETS:
            raise HTTPException(status_code=503, detail="Stream connection busy; retry shortly.")
        _tickets[digest] = _Ticket(path.removesuffix("-ticket"), realm.current(),
                                   authorization, now + TICKET_TTL_SECONDS)
    return JSONResponse({"ticket": ticket, "expires_in": TICKET_TTL_SECONDS,
                         "realm": realm.current()}, headers={"Cache-Control": "no-store"})


async def resolve_staff_stream(request: Request) -> Optional[StaffContext]:
    """Accept header authentication or one valid ticket, never a query JWT."""
    authorization = request.headers.get("authorization")
    if authorization:
        return await get_staff_context_optional(authorization=authorization)
    ticket = request.query_params.get("ticket") or ""
    if not ticket or len(ticket) > 128:
        return None
    digest = hashlib.sha256(ticket.encode()).hexdigest()
    with _lock:
        row = _tickets.get(digest)
        if not row:
            return None
        if row.expires <= time.monotonic():
            _tickets.pop(digest, None)
            return None
        if (row.purpose != "staff_sse" or row.path != request.scope["path"]
                or row.realm != realm.current()):
            return None
        _tickets.pop(digest)  # Exactly one connection wins concurrent redemption.
    return await get_staff_context_optional(authorization=row.authorization)
