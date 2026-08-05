"""Community WebSocket hub (PRD §4, §6): connect, presence, typing, broadcast.

One process-wide :class:`Hub`. Every gated member connection receives every
community event (three fixed channels, small membership — fan-out filtering
would be premature). REST handlers call :func:`broadcast` after a successful
write; the client's polling fallback covers a dropped socket (PRD §4).

Hardening (audit findings):
  * Sends fan out CONCURRENTLY and each is bounded by ``SEND_TIMEOUT_SEC`` —
    a stalled client (half-open TCP, full kernel buffer) can no longer wedge
    every write endpoint behind one sequential ``await send_json``.
  * When ``broadcast`` reaps a failed socket, the presence bookkeeping runs
    through the same last-connection logic as a normal disconnect and a
    presence event is emitted — a died-mid-broadcast user no longer stays
    "online" forever.

Presence is connection-derived: a user is "online" while they hold ≥1 open
socket. Typing indicators are ephemeral relays — never persisted.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Set

from fastapi import WebSocket

log = logging.getLogger("community.ws")

SEND_TIMEOUT_SEC = 5.0


class Hub:
    def __init__(self) -> None:
        self._sockets: Dict[WebSocket, str] = {}  # socket -> user_id
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket, user_id: str) -> bool:
        """Register an accepted socket. Returns True when this is the user's
        first live connection (a presence transition)."""
        async with self._lock:
            was_online = user_id in self._sockets.values()
            self._sockets[ws] = user_id
            return not was_online

    async def disconnect(self, ws: WebSocket) -> Optional[str]:
        """Unregister a socket. Returns the user_id if this was their LAST
        connection (a presence transition), else None. Safe to call for a
        socket ``broadcast`` already reaped (returns None — the reaper owned
        the presence transition)."""
        async with self._lock:
            user_id = self._sockets.pop(ws, None)
            if user_id is None:
                return None
            still_online = user_id in self._sockets.values()
            return None if still_online else user_id

    async def online_user_ids(self) -> List[str]:
        async with self._lock:
            return sorted(set(self._sockets.values()))

    async def _send_one(self, sock: WebSocket, event: Dict[str, Any]) -> bool:
        try:
            await asyncio.wait_for(sock.send_json(event), timeout=SEND_TIMEOUT_SEC)
            return True
        except Exception:
            return False

    async def _deliver(self, targets: List[WebSocket], event: Dict[str, Any]) -> None:
        """Concurrent, timeout-bounded fan-out to a target list, with dead
        sockets reaped and presence transitions emitted."""
        if not targets:
            return
        results = await asyncio.gather(
            *(self._send_one(s, event) for s in targets), return_exceptions=False
        )
        dead = [s for s, ok in zip(targets, results) if not ok]
        if not dead:
            return
        went_offline: Set[str] = set()
        async with self._lock:
            for sock in dead:
                user_id = self._sockets.pop(sock, None)
                if user_id is not None and user_id not in self._sockets.values():
                    went_offline.add(user_id)
        # Close the reaped sockets CONCURRENTLY with a tight bound — a batch of
        # dead sockets must never stack serial 5s timeouts inside the awaiting
        # write request (this fan-out sits on the message-post path).
        async def _close_quiet(sock: WebSocket) -> None:
            try:
                await asyncio.wait_for(sock.close(), timeout=1.0)
            except Exception:
                pass
        await asyncio.gather(*(_close_quiet(s) for s in dead))
        if went_offline:
            # Recursion is bounded: each level strictly shrinks the socket set.
            await self.broadcast({"type": "presence", "online": await self.online_user_ids()})

    async def broadcast(self, event: Dict[str, Any], *, exclude: Optional[WebSocket] = None) -> None:
        """Send one event to every connected socket."""
        async with self._lock:
            targets = [s for s in self._sockets.keys() if s is not exclude]
        await self._deliver(targets, event)

    async def send_to_users(
        self,
        user_ids: List[str],
        event: Dict[str, Any],
        *,
        exclude: Optional[WebSocket] = None,
    ) -> None:
        """Send one event ONLY to the given users' sockets — the delivery
        primitive for direct messages: a DM event never rides the broadcast."""
        want = set(user_ids or [])
        async with self._lock:
            targets = [
                s for s, uid in self._sockets.items()
                if uid in want and s is not exclude
            ]
        await self._deliver(targets, event)


hub = Hub()
