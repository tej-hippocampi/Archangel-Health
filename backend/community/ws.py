"""Community WebSocket hub (PRD §4, §6): connect, presence, typing, broadcast.

One process-wide :class:`Hub`. Every gated member connection receives every
community event (three fixed channels, small membership — fan-out filtering
would be premature). REST handlers call :func:`broadcast` after a successful
write; the client's polling fallback covers a dropped socket (PRD §4).

Presence is connection-derived: a user is "online" while they hold ≥1 open
socket. Typing indicators are ephemeral relays — never persisted.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Set

from fastapi import WebSocket

log = logging.getLogger("community.ws")


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
        connection (a presence transition), else None."""
        async with self._lock:
            user_id = self._sockets.pop(ws, None)
            if user_id is None:
                return None
            still_online = user_id in self._sockets.values()
            return None if still_online else user_id

    async def online_user_ids(self) -> List[str]:
        async with self._lock:
            return sorted(set(self._sockets.values()))

    async def broadcast(self, event: Dict[str, Any], *, exclude: Optional[WebSocket] = None) -> None:
        """Send one event to every connected socket. A failed send drops that
        socket (its reader loop will observe the disconnect and clean up)."""
        async with self._lock:
            targets = [s for s in self._sockets.keys() if s is not exclude]
        dead: Set[WebSocket] = set()
        for sock in targets:
            try:
                await sock.send_json(event)
            except Exception:
                dead.add(sock)
        for sock in dead:
            try:
                await sock.close()
            except Exception:
                pass
            async with self._lock:
                self._sockets.pop(sock, None)


hub = Hub()
