"""Offline stub STT — used when no real provider key is configured.

Returns a deterministic surgical post-op follow-up transcript with speaker
turns so the Gold Standard capture → draft → review pipeline runs end-to-end in
local demos. The result is flagged ``is_stub=True`` so the UI/audit can make the
"no real transcription happened" state honest.
"""

from __future__ import annotations

import os
import wave
from typing import Optional

from integrations.stt.base import Transcript

_STUB_TURNS = [
    {"speaker": "DOCTOR", "text": "Good morning. How has the incision site been healing since your surgery two weeks ago?"},
    {"speaker": "PATIENT", "text": "Mostly okay. There's some redness around the lower edge and it's been a little warm, but no real drainage."},
    {"speaker": "DOCTOR", "text": "Any fevers, chills, or increasing pain?"},
    {"speaker": "PATIENT", "text": "I had a low grade fever yesterday, about 100.4. The pain is about the same, maybe a four out of ten."},
    {"speaker": "DOCTOR", "text": "Are you still taking the oxycodone, and how is your bowel function on it?"},
    {"speaker": "PATIENT", "text": "I stopped the oxycodone three days ago, I'm just on Tylenol now. Bowels are back to normal."},
    {"speaker": "DOCTOR", "text": "Good. Given the warmth and that low grade fever, I want to start you on a course of cephalexin and have you watch for spreading redness."},
    {"speaker": "PATIENT", "text": "Okay. Should I keep the dressing on?"},
    {"speaker": "DOCTOR", "text": "Keep it clean and dry, change it daily. Let's see you back in one week, sooner if the redness spreads or the fever climbs above 101."},
]


def _flat_text() -> str:
    return "\n".join(f"[{t['speaker']}] {t['text']}" for t in _STUB_TURNS)


def _wav_duration(audio_path: str) -> Optional[float]:
    try:
        with wave.open(audio_path, "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate() or 1
            return round(frames / float(rate), 1)
    except Exception:
        try:
            size = os.path.getsize(audio_path)
            # webm/opus ~ very rough: assume 24 kbps mono
            return round(size / (24_000 / 8), 1)
        except OSError:
            return None


class StubSTTProvider:
    name = "stub"

    async def transcribe(self, *, audio_path: str, mime_type: str) -> Transcript:
        return Transcript(
            text=_flat_text(),
            provider="stub",
            turns=list(_STUB_TURNS),
            duration_sec=_wav_duration(audio_path),
            languages=["en"],
            is_stub=True,
        )
