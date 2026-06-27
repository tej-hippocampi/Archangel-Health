"""Speech-to-text provider abstraction for Gold Standard capture.

Swappable behind ``STT_PROVIDER`` (``whisper`` | ``deepgram`` | ``stub``),
mirroring ``integrations/video``. Keys come from env, server-side only. When the
selected provider has no API key configured, ``transcribe`` returns a clearly
labelled stub transcript so the capture → review pipeline still runs end-to-end
in local demos without leaking that a real call was made.
"""

from __future__ import annotations

import os

from integrations.stt.base import STTProvider, Transcript
from integrations.stt.stub import StubSTTProvider


def get_stt_provider() -> STTProvider:
    provider = (os.getenv("STT_PROVIDER") or "whisper").strip().lower()
    if provider == "deepgram":
        from integrations.stt.deepgram import DeepgramSTTProvider

        return DeepgramSTTProvider()
    if provider == "stub":
        return StubSTTProvider()
    from integrations.stt.whisper import WhisperSTTProvider

    return WhisperSTTProvider()


__all__ = ["STTProvider", "Transcript", "get_stt_provider", "StubSTTProvider"]
