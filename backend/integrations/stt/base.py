"""STT provider protocol + result type."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Protocol


@dataclass
class Transcript:
    text: str
    provider: str
    # Rough speaker turns when the provider supports diarization:
    # [{"speaker": "DOCTOR"|"PATIENT"|"SPEAKER_0", "text": "..."}]
    turns: List[dict] = field(default_factory=list)
    duration_sec: Optional[float] = None
    languages: List[str] = field(default_factory=list)
    is_stub: bool = False


class STTProvider(Protocol):
    name: str

    async def transcribe(self, *, audio_path: str, mime_type: str) -> Transcript: ...
