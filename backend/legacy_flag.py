"""The kill switch for the retiring peri-op surface (PRD §5).

One flag, read once at import, shared by every module that registers a legacy
route — ``main.py`` for the inline peri-op endpoints and ``routers/admin.py`` for
the triage-config and prompt-registry endpoints. It lives in its own module so
those two cannot drift apart: a flag defined twice is a flag that gets flipped
once.

Default ON. Turning it on is a no-op; turning it off is the whole point.

The rollout it exists for:

1. Ship with the flag ON. Production serves exactly the same route table, in the
   same order, and nothing changes.
2. Set ``ARCHANGEL_LEGACY_PERIOP=0`` in staging, run the audit, confirm only the
   intended paths disappear.
3. Set it to 0 in production and watch the access logs for a week. A 404 on a
   gated path is a live consumer nobody knew about: turn the flag back on and
   find out who it was.
4. Only once that week is clean does the code get deleted.

Step 3 is the part that cannot be shortened. The gated set was built by tracing
callers through the frontends, and tracing finds every consumer that is written
down — not the cron job on someone's laptop, the partner integration, or the
bookmark. The logs see those; a grep never will.
"""

from __future__ import annotations

import os
from typing import Callable, TypeVar

__all__ = ["LEGACY_PERIOP", "legacy_route"]

LEGACY_PERIOP: bool = os.getenv("ARCHANGEL_LEGACY_PERIOP", "1") == "1"

_F = TypeVar("_F", bound=Callable)


def legacy_route(route_decorator: Callable[[_F], _F]) -> Callable[[_F], _F]:
    """Register a route only while the legacy peri-op surface is enabled.

    Used as ``@legacy_route(app.get("/api/patients"))`` or
    ``@legacy_route(router.get("/triage/intraop/config"))``.

    This wraps REGISTRATION, not the handler body. With the flag on, the
    underlying decorator is applied at exactly the point in module execution it
    always was, so the route keeps its position in ``app.routes`` — and FastAPI
    matches on registration order, which a set-based route diff cannot check.
    Moving these handlers onto a separate ``APIRouter`` would have been the more
    obvious refactor and would have silently reordered every one of them behind
    its ``include_router`` call.

    With the flag off the decorator is never applied, so the path is never
    registered and a request 404s exactly as it will once the code is deleted —
    which is what makes the dark week a real rehearsal rather than a guess.
    """
    if LEGACY_PERIOP:
        return route_decorator

    def _unregistered(fn: _F) -> _F:
        # The function object is returned untouched, so anything that calls it
        # directly (a background job, a test, another handler) still works. Only
        # the HTTP route is withheld.
        return fn

    return _unregistered
