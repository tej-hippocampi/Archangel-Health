"""OpenAI Whisper STT provider.

Sends audio to the Whisper transcription API (``whisper-1`` by default).
Requires ``WHISPER_API_KEY`` (or ``OPENAI_API_KEY``). When unconfigured, falls
back to the offline stub so the pipeline still completes in local demos.

PHI note: audio is PHI. Only send it to Whisper once a BAA is on file — the
``gold`` pipeline gates this via ``compliance.subprocessors``.
"""

from __future__ import annotations

import asyncio
import os

import httpx

from compliance.subprocessors import assert_phi_allowed
from integrations.stt.base import Transcript
from integrations.stt.stub import StubSTTProvider

_API_URL = "https://api.openai.com/v1/audio/transcriptions"


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


class WhisperSTTProvider:
    name = "whisper"

    def __init__(self) -> None:
        self.api_key = os.getenv("WHISPER_API_KEY") or os.getenv("OPENAI_API_KEY")
        self.model = (os.getenv("WHISPER_MODEL") or "whisper-1").strip()

    def _configured(self) -> bool:
        return bool(self.api_key)

    async def transcribe(self, *, audio_path: str, mime_type: str) -> Transcript:
        if not self._configured():
            print("[stt] WHISPER_API_KEY not set — returning stub transcript.")
            return await StubSTTProvider().transcribe(audio_path=audio_path, mime_type=mime_type)

        # PHI gate (A2): raw audio is PHI — never transmit without a signed BAA.
        # Raises SubprocessorPHIError, which the pipeline turns into status=ERROR.
        assert_phi_allowed("openai_whisper")

        filename = os.path.basename(audio_path) or "audio.webm"
        content = await asyncio.to_thread(_read_bytes, audio_path)
        files = {"file": (filename, content, mime_type or "audio/webm")}
        data = {"model": self.model, "response_format": "verbose_json"}
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                _API_URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                files=files,
                data=data,
            )
            resp.raise_for_status()
            payload = resp.json()

        text = (payload.get("text") or "").strip()
        duration = payload.get("duration")
        language = payload.get("language")
        # Whisper does not diarize — keep turns empty; reviewers see flat text.
        return Transcript(
            text=text,
            provider="whisper",
            turns=[],
            duration_sec=float(duration) if duration is not None else None,
            languages=[language] if language else [],
        )
