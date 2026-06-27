"""Deepgram STT provider (with speaker diarization).

Requires ``DEEPGRAM_API_KEY``. When unconfigured, falls back to the offline
stub. Uses ``diarize=true`` to build rough doctor/patient speaker turns.

PHI note: audio is PHI — only send to Deepgram once a BAA is on file (gated via
``compliance.subprocessors``).
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List

import httpx

from compliance.subprocessors import assert_phi_allowed
from integrations.stt.base import Transcript
from integrations.stt.stub import StubSTTProvider

_API_URL = "https://api.deepgram.com/v1/listen"


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


class DeepgramSTTProvider:
    name = "deepgram"

    def __init__(self) -> None:
        self.api_key = os.getenv("DEEPGRAM_API_KEY")
        self.model = (os.getenv("DEEPGRAM_MODEL") or "nova-2-medical").strip()

    def _configured(self) -> bool:
        return bool(self.api_key)

    async def transcribe(self, *, audio_path: str, mime_type: str) -> Transcript:
        if not self._configured():
            print("[stt] DEEPGRAM_API_KEY not set — returning stub transcript.")
            return await StubSTTProvider().transcribe(audio_path=audio_path, mime_type=mime_type)

        # PHI gate (A2): raw audio is PHI — never transmit without a signed BAA.
        assert_phi_allowed("deepgram")

        content = await asyncio.to_thread(_read_bytes, audio_path)
        params = {"model": self.model, "diarize": "true", "punctuate": "true", "smart_format": "true"}
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                _API_URL,
                headers={
                    "Authorization": f"Token {self.api_key}",
                    "Content-Type": mime_type or "audio/webm",
                },
                params=params,
                content=content,
            )
            resp.raise_for_status()
            payload = resp.json()

        return _parse(payload)


def _parse(payload: Dict[str, Any]) -> Transcript:
    results = payload.get("results") or {}
    channels = results.get("channels") or []
    alt = (channels[0].get("alternatives") if channels else None) or [{}]
    first = alt[0] if alt else {}
    flat_text = (first.get("transcript") or "").strip()

    turns: List[dict] = []
    words = first.get("words") or []
    cur_speaker, cur_words = None, []
    for w in words:
        spk = w.get("speaker")
        token = w.get("punctuated_word") or w.get("word") or ""
        if spk != cur_speaker and cur_words:
            turns.append({"speaker": f"SPEAKER_{cur_speaker}", "text": " ".join(cur_words)})
            cur_words = []
        cur_speaker = spk
        cur_words.append(token)
    if cur_words:
        turns.append({"speaker": f"SPEAKER_{cur_speaker}", "text": " ".join(cur_words)})

    duration = (payload.get("metadata") or {}).get("duration")
    return Transcript(
        text=flat_text,
        provider="deepgram",
        turns=turns,
        duration_sec=float(duration) if duration is not None else None,
        languages=["en"],
    )
