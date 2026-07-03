"""Provider-abstracted speech-to-text for in-app dictation (Speed Optimization §4).

Contributors run the Wispr Flow desktop app today (system-wide dictation into
any plain textarea — which every Asclepius field already is); this module
scaffolds the in-app mic behind a provider interface so the Wispr Flow API can
drop in later with no rework.

Providers (``ASCLEPIUS_STT_PROVIDER``):
  * ``standard`` (default) — Deepgram (``DEEPGRAM_API_KEY``) or OpenAI Whisper
    (``OPENAI_API_KEY``), whichever key is configured (Deepgram preferred).
  * ``wispr``    — stub for the Wispr Flow API. Their API wants base64 16 kHz
    PCM WAV, so :func:`webm_to_wav_base64` is provided; the HTTP call is wired
    in when API access lands (``WISPR_API_KEY``).

Compliance: prompts are synthetic (no PHI) and audio is EPHEMERAL — it lives
only in this request's memory, is forwarded over TLS, and is never written to
disk or the store. Optional LLM cleanup pass behind ``ASCLEPIUS_STT_CLEANUP=1``
(BAA-covered Anthropic via ``ai.llm_client``).

Everything degrades gracefully: with no provider key configured,
:func:`transcribe` returns ``{skipped: True}`` and the doctor simply types (or
uses the Wispr desktop app) — dictation is an accelerator, never a gate.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import shutil
import subprocess
import wave
from typing import Any, Dict, Optional

log = logging.getLogger("asclepius.stt")

_HTTP_TIMEOUT = 60.0


def stt_provider() -> str:
    p = (os.getenv("ASCLEPIUS_STT_PROVIDER") or "standard").strip().lower()
    return p if p in ("standard", "wispr") else "standard"


def cleanup_enabled() -> bool:
    return (os.getenv("ASCLEPIUS_STT_CLEANUP") or "").strip().lower() in ("1", "true", "yes", "on")


# ─── Audio conversion util (Wispr Flow wants base64 16 kHz PCM WAV) ───────────
def webm_to_wav_base64(data: bytes, *, sample_rate: int = 16000) -> Optional[str]:
    """Convert browser MediaRecorder audio (webm/ogg opus) to base64-encoded
    16 kHz mono PCM WAV — the shape the Wispr Flow API expects. Uses ffmpeg via
    stdin/stdout pipes so the audio NEVER touches disk (ephemeral requirement).
    Returns None when ffmpeg is unavailable or conversion fails."""
    if not data:
        return None
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        log.info("asclepius stt: ffmpeg not available for wav conversion")
        return None
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error",
             "-i", "pipe:0",
             "-ac", "1", "-ar", str(sample_rate), "-f", "wav", "pipe:1"],
            input=data, capture_output=True, timeout=60, check=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        log.info("asclepius stt: wav conversion failed: %s", exc)
        return None
    wav_bytes = proc.stdout
    if not wav_bytes:
        return None
    try:  # sanity: parseable WAV at the requested rate
        with wave.open(io.BytesIO(wav_bytes)) as w:
            if w.getframerate() != sample_rate:
                log.info("asclepius stt: unexpected wav rate %s", w.getframerate())
    except wave.Error:
        return None
    return base64.b64encode(wav_bytes).decode("ascii")


# ─── Providers ────────────────────────────────────────────────────────────────
async def _transcribe_deepgram(data: bytes, mime: str) -> Optional[str]:
    key = (os.getenv("DEEPGRAM_API_KEY") or "").strip()
    if not key:
        return None
    import httpx

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.post(
            "https://api.deepgram.com/v1/listen",
            params={"model": "nova-2-medical", "smart_format": "true"},
            headers={"Authorization": f"Token {key}", "Content-Type": mime or "audio/webm"},
            content=data,
        )
        resp.raise_for_status()
        body = resp.json()
    try:
        return body["results"]["channels"][0]["alternatives"][0]["transcript"]
    except (KeyError, IndexError, TypeError):
        return None


async def _transcribe_whisper(data: bytes, mime: str) -> Optional[str]:
    key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not key:
        return None
    import httpx

    ext = "webm"
    if mime and "/" in mime:
        ext = mime.split("/", 1)[1].split(";")[0] or "webm"
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {key}"},
            data={"model": "whisper-1"},
            files={"file": (f"dictation.{ext}", data, mime or "audio/webm")},
        )
        resp.raise_for_status()
        body = resp.json()
    return body.get("text")


async def _transcribe_wispr(data: bytes, mime: str) -> Optional[str]:
    """Wispr Flow API stub. The conversion util is ready (base64 16 kHz PCM WAV);
    the HTTP call lands once API access is granted. Until then this provider
    degrades to skipped so the desktop Wispr app remains the dictation path."""
    key = (os.getenv("WISPR_API_KEY") or "").strip()
    if not key:
        return None
    wav_b64 = webm_to_wav_base64(data)
    if not wav_b64:
        return None
    # TODO(wispr): POST {audio: wav_b64} to the Flow API once access is granted.
    log.info("asclepius stt: wispr provider configured but the Flow API is not integrated yet")
    return None


async def _cleanup_text(raw: str) -> str:
    """Optional LLM tidy pass on the raw transcript (``ASCLEPIUS_STT_CLEANUP=1``).
    Falls back to the raw transcript on any failure — cleanup is best-effort."""
    if not cleanup_enabled() or not raw.strip():
        return raw
    try:
        from ai.llm_client import call_llm, first_text
        from asclepius.prompts import ASCLEPIUS_STT_CLEANUP_SYSTEM

        resp, _rec = await call_llm(
            role="asclepius_stt_cleanup",
            system=ASCLEPIUS_STT_CLEANUP_SYSTEM,
            messages=[{"role": "user", "content": raw}],
            prompt_id="asclepius_stt_cleanup",
            purpose="asclepius_dictation_cleanup",
        )
        cleaned = (first_text(resp) or "").strip()
        return cleaned or raw
    except Exception as exc:
        log.info("asclepius stt cleanup skipped: %s", exc)
        return raw


async def transcribe(data: bytes, mime: str = "audio/webm") -> Dict[str, Any]:
    """Transcribe one dictation clip. Returns
    ``{text, provider, skipped, error?}``; ``skipped=True`` (never an exception)
    when no provider is configured or the provider call fails."""
    if not data:
        return {"text": "", "provider": None, "skipped": True, "error": "empty_audio"}
    provider = stt_provider()
    try:
        if provider == "wispr":
            text = await _transcribe_wispr(data, mime)
            used = "wispr"
        else:
            text = await _transcribe_deepgram(data, mime)
            used = "deepgram"
            if text is None:
                text = await _transcribe_whisper(data, mime)
                used = "whisper"
    except Exception as exc:
        log.info("asclepius stt: transcription failed (%s): %s", provider, exc)
        return {"text": "", "provider": provider, "skipped": True, "error": "provider_error"}
    if text is None:
        return {"text": "", "provider": provider, "skipped": True, "error": "no_stt_provider_configured"}
    return {"text": await _cleanup_text(text or ""), "provider": used, "skipped": False}
