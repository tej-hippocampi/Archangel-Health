"""
ElevenLabs Integration — Text-to-Speech synthesis
Step 5: Voice script → .mp3 audio → CDN URL

Docs: https://elevenlabs.io/docs/api-reference/text-to-speech
"""

import os
import re
from pathlib import Path
from typing import Optional

import httpx


class ElevenLabsClient:
    BASE_URL = "https://api.elevenlabs.io/v1"

    DEFAULT_VOICE_ID = "l4Coq6695JDX9xtLqXDE"

    def __init__(self) -> None:
        self.api_key  = os.getenv("ELEVENLABS_API_KEY")
        self.voice_id = os.getenv("ELEVENLABS_VOICE_ID", self.DEFAULT_VOICE_ID)

    async def synthesize(
        self,
        script: str,
        patient_id: str,
        voice_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        Converts voice script to .mp3 audio.

        Returns CDN URL of audio file, or None if ElevenLabs is not configured.
        """
        if not self.api_key:
            print("[ElevenLabs] ELEVENLABS_API_KEY not set — skipping synthesis.")
            return None

        clean = self._clean_for_tts(script)
        vid   = voice_id or self.voice_id

        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(
                f"{self.BASE_URL}/text-to-speech/{vid}",
                headers={
                    "xi-api-key":     self.api_key,
                    "Content-Type":   "application/json",
                    "Accept":         "audio/mpeg",
                },
                json={
                    "text":     clean,
                    "model_id": "eleven_turbo_v2_5",
                    "voice_settings": {
                        "stability":          0.75,
                        "similarity_boost":   0.75,
                        "style":              0.30,
                        "use_speaker_boost":  True,
                    },
                },
            )
            resp.raise_for_status()

        return await self._save_audio(resp.content, patient_id)

    # ── Private ──────────────────────────────────────────────

    def _clean_for_tts(self, script: str) -> str:
        """
        Strip tone markers ([calm], [firm], [reassuring], etc.) before TTS.
        ElevenLabs ignores them as SSML anyway; removing them prevents
        the voice from reading them aloud.
        """
        cleaned = re.sub(r'\[[^\]]+\]', '', script)
        # Normalize whitespace while preserving paragraph breaks
        cleaned = re.sub(r'[ \t]+', ' ', cleaned)
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
        return cleaned.strip()

    async def _save_audio(self, audio_bytes: bytes, patient_id: str) -> str:
        """
        Save audio to disk and return a URL.

        Production: upload to S3/GCS instead and return a signed CDN URL.
        Development: serve from local /tmp via FastAPI static endpoint.
        """
        filename = f"audio_{patient_id}.mp3"
        filepath = Path("/tmp") / filename

        filepath.write_bytes(audio_bytes)

        return f"/audio/{filename}"
