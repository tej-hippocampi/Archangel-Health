"""Community WebSocket hub (PRD §4, §6): connect, presence, typing, broadcast.

One process-wide :class:`Hub` with realm-scoped connections and delivery.
REST handlers broadcast public channel events within their current realm and
target private events to its participants. Every delivery rechecks the original
session and current community eligibility; the client's polling fallback covers
a dropped socket (PRD §4).

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
from typing import Any, Callable, Dict, List, Optional, Set

from fastapi import WebSocket

import realm

log = logging.getLogger("community.ws")

SEND_TIMEOUT_SEC = 5.0


class Hub:
    def __init__(self) -> None:
        self._sockets: Dict[WebSocket, str] = {}  # socket -> user_id
        self._realms: Dict[WebSocket, str] = {}
        self._authorizers: Dict[WebSocket, Callable[[], bool]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket, user_id: str, *,
                      authorize: Optional[Callable[[], bool]] = None) -> bool:
        """Register an accepted socket. Returns True when this is the user's
        first live connection (a presence transition)."""
        async with self._lock:
            connection_realm = realm.current()
            was_online = any(
                uid == user_id and self._socket_realm(sock) == connection_realm
                for sock, uid in self._sockets.items()
            )
            self._sockets[ws] = user_id
            self._realms[ws] = connection_realm
            if authorize is not None:
                self._authorizers[ws] = authorize
            return not was_online

    async def disconnect(self, ws: WebSocket) -> Optional[str]:
        """Unregister a socket. Returns the user_id if this was their LAST
        connection (a presence transition), else None. Safe to call for a
        socket ``broadcast`` already reaped (returns None — the reaper owned
        the presence transition)."""
        async with self._lock:
            user_id = self._sockets.pop(ws, None)
            connection_realm = self._realms.pop(ws, realm.LIVE)
            self._authorizers.pop(ws, None)
            if user_id is None:
                return None
            still_online = any(
                uid == user_id and self._socket_realm(sock) == connection_realm
                for sock, uid in self._sockets.items()
            )
            return None if still_online else user_id

    def _socket_realm(self, sock: WebSocket) -> str:
        # Connections made through connect() always have an explicit realm.
        # The live default preserves older in-process callers/test fixtures.
        return self._realms.get(sock, realm.LIVE)

    def _authorized(self, sock: WebSocket) -> bool:
        authorize = self._authorizers.get(sock)
        try:
            return authorize is None or bool(authorize())
        except Exception:
            # A failed account/store check must stop delivery, not bypass it.
            return False

    async def online_user_ids(self) -> List[str]:
        async with self._lock:
            return sorted({
                uid for sock, uid in self._sockets.items()
                if self._socket_realm(sock) == realm.current() and self._authorized(sock)
            })

    async def _send_one(self, sock: WebSocket, event: Dict[str, Any]) -> bool:
        try:
            if self._socket_realm(sock) != realm.current() or not self._authorized(sock):
                return False
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
        offline_realms: Set[str] = set()
        async with self._lock:
            for sock in dead:
                user_id = self._sockets.pop(sock, None)
                connection_realm = self._realms.pop(sock, realm.LIVE)
                self._authorizers.pop(sock, None)
                still_online = any(
                    uid == user_id and self._socket_realm(other) == connection_realm
                    for other, uid in self._sockets.items()
                )
                if user_id is not None and not still_online:
                    offline_realms.add(connection_realm)
        # Close the reaped sockets CONCURRENTLY with a tight bound — a batch of
        # dead sockets must never stack serial 5s timeouts inside the awaiting
        # write request (this fan-out sits on the message-post path).
        async def _close_quiet(sock: WebSocket) -> None:
            try:
                await asyncio.wait_for(sock.close(), timeout=1.0)
            except Exception:
                pass
        await asyncio.gather(*(_close_quiet(s) for s in dead))
        for connection_realm in offline_realms:
            # Recursion is bounded: each level strictly shrinks the socket set.
            with realm.scoped(connection_realm):
                await self.broadcast({"type": "presence", "online": await self.online_user_ids()})

    async def broadcast(self, event: Dict[str, Any], *, exclude: Optional[WebSocket] = None) -> None:
        """Send one event to connected sockets in the originating realm."""
        async with self._lock:
            targets = [s for s in self._sockets.keys()
                       if s is not exclude and self._socket_realm(s) == realm.current()]
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
                if uid in want and s is not exclude and self._socket_realm(s) == realm.current()
            ]
        await self._deliver(targets, event)


hub = Hub()
