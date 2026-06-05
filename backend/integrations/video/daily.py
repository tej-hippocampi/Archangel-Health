"""Daily.co video provider for telehealth."""

from __future__ import annotations

import os
import time
from typing import Optional

import httpx

from integrations.video.base import JoinToken, VideoSession


class DailyVideoProvider:
    name = "daily"

    def __init__(self) -> None:
        self.api_key = os.getenv("DAILY_API_KEY")
        self.domain = (os.getenv("DAILY_DOMAIN") or "demo.daily.co").strip().rstrip("/")
        self.base_url = "https://api.daily.co/v1"

    def _configured(self) -> bool:
        return bool(self.api_key)

    async def create_session(
        self,
        *,
        encounter_id: str,
        max_minutes: int = 60,
        record: bool = False,
    ) -> VideoSession:
        if not self._configured():
            print("[video] DAILY_API_KEY not set — returning stub session.")
            stub_url = f"/telehealth/unavailable?encounter={encounter_id}"
            return VideoSession(provider="daily-stub", session_id=f"stub-{encounter_id}", room_url=stub_url)

        exp = int(time.time()) + max_minutes * 60 + 300
        room_name = f"archangel-{encounter_id[:12]}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{self.base_url}/rooms",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "name": room_name,
                    "privacy": "private",
                    "properties": {
                        "exp": exp,
                        "eject_at_room_exp": True,
                        "enable_recording": "cloud" if record else "",
                    },
                },
            )
            resp.raise_for_status()
            data = resp.json()
            room_url = data.get("url") or f"https://{self.domain}/{room_name}"
            return VideoSession(provider="daily", session_id=room_name, room_url=room_url)

    async def issue_token(
        self,
        *,
        session: VideoSession,
        display_name: str,
        is_owner: bool,
        minutes_valid: int = 120,
    ) -> JoinToken:
        if session.provider == "daily-stub":
            return JoinToken(token="", join_url=session.room_url)

        exp = int(time.time()) + minutes_valid * 60
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{self.base_url}/meeting-tokens",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "properties": {
                        "room_name": session.session_id,
                        "user_name": display_name,
                        "is_owner": is_owner,
                        "exp": exp,
                    }
                },
            )
            resp.raise_for_status()
            token = resp.json().get("token") or ""
            join_url = f"{session.room_url}?t={token}"
            return JoinToken(token=token, join_url=join_url)

    async def end_session(self, *, session_id: str) -> None:
        if not self._configured() or session_id.startswith("stub-"):
            return
        async with httpx.AsyncClient(timeout=30.0) as client:
            await client.delete(
                f"{self.base_url}/rooms/{session_id}",
                headers={"Authorization": f"Bearer {self.api_key}"},
            )


def get_video_provider() -> DailyVideoProvider:
    provider = (os.getenv("VIDEO_PROVIDER") or "daily").strip().lower()
    if provider == "daily":
        return DailyVideoProvider()
    return DailyVideoProvider()
