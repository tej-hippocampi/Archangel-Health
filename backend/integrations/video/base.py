"""Video provider abstraction for telehealth visits."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class VideoSession:
    provider: str
    session_id: str
    room_url: str


@dataclass
class JoinToken:
    token: str
    join_url: str


class VideoProvider(Protocol):
    name: str

    async def create_session(
        self,
        *,
        encounter_id: str,
        max_minutes: int = 60,
        record: bool = False,
    ) -> VideoSession: ...

    async def issue_token(
        self,
        *,
        session: VideoSession,
        display_name: str,
        is_owner: bool,
        minutes_valid: int = 120,
    ) -> JoinToken: ...

    async def end_session(self, *, session_id: str) -> None: ...
