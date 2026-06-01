import json
import os
import time
import hashlib
from typing import Any, Optional

from anthropic import Anthropic, AsyncAnthropic

from ai.model_config import APP_AI_CONFIG_VERSION, resolve
from team_store import TeamStore

_async_client: Optional[AsyncAnthropic] = None
_sync_client: Optional[Anthropic] = None
_event_store: Optional[TeamStore] = None


def _aclient() -> AsyncAnthropic:
    global _async_client
    if _async_client is None:
        _async_client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _async_client


def _sclient() -> Anthropic:
    global _sync_client
    if _sync_client is None:
        _sync_client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _sync_client


def _store() -> TeamStore:
    global _event_store
    if _event_store is None:
        _event_store = TeamStore()
    return _event_store


def first_text(resp: Any) -> str:
    blocks = list(getattr(resp, "content", []) or [])
    for block in blocks:
        if getattr(block, "type", None) == "text":
            return getattr(block, "text", "") or ""
    # Back-compat for tests and older mocked responses without a `type` attribute.
    if blocks:
        return getattr(blocks[0], "text", "") or ""
    return ""


def _prompt_sha(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]


def _build_kwargs(role: str, system: str, messages: list[dict[str, Any]], overrides: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    cfg = resolve(role)
    kwargs: dict[str, Any] = {
        "model": cfg["model"],
        "max_tokens": overrides.pop("max_tokens", cfg["max_tokens"]),
        "system": system,
        "messages": messages,
    }
    temp = overrides.pop("temperature", cfg["temperature"])
    if temp is not None:
        kwargs["temperature"] = temp
    kwargs.update(overrides)
    return kwargs, cfg


def _record(
    role: str,
    cfg: dict[str, Any],
    prompt_id: Optional[str],
    purpose: str,
    system: str,
    messages: list[dict[str, Any]],
    resp: Any,
    t0: float,
) -> dict[str, Any]:
    prompt = None
    if prompt_id:
        from prompts.registry import prompt_meta

        prompt = prompt_meta(prompt_id)
    return {
        "role": role,
        "model": cfg["model"],
        "ai_config_version": APP_AI_CONFIG_VERSION,
        "prompt": prompt,
        "purpose": purpose,
        "latency_ms": int((time.monotonic() - t0) * 1000),
        "usage": {
            "input": getattr(getattr(resp, "usage", None), "input_tokens", None),
            "output": getattr(getattr(resp, "usage", None), "output_tokens", None),
        },
        "anthropic_request_id": getattr(resp, "_request_id", None),
        "input_sha": _prompt_sha(system + json.dumps(messages, default=str)),
    }


def _log(record: dict[str, Any], patient_id: Optional[str], system: str, messages: list[dict[str, Any]], resp: Any) -> None:
    payload = dict(record)
    if os.getenv("LLM_LOG_RAW") == "1":
        payload["raw_input"] = {"system": system, "messages": messages}
        payload["raw_output"] = first_text(resp)
    _store().log_event(patient_id=patient_id, event_type="llm_call", payload=payload)


async def call_llm(
    *,
    role: str,
    system: str,
    messages: list[dict[str, Any]],
    prompt_id: Optional[str] = None,
    patient_id: Optional[str] = None,
    purpose: str = "",
    **overrides: Any,
) -> tuple[Any, dict[str, Any]]:
    kwargs, cfg = _build_kwargs(role, system, messages, overrides)
    t0 = time.monotonic()
    resp = await _aclient().messages.create(**kwargs)
    try:
        rec = _record(role, cfg, prompt_id, purpose, system, messages, resp, t0)
        _log(rec, patient_id, system, messages, resp)
    except Exception as exc:
        import sys

        print(f"[llm_client] audit log failed: {exc!r}", file=sys.stderr)
        rec = {"role": role, "model": cfg["model"], "audit_error": repr(exc)}
    return resp, rec


def call_llm_sync(
    *,
    role: str,
    system: str,
    messages: list[dict[str, Any]],
    prompt_id: Optional[str] = None,
    patient_id: Optional[str] = None,
    purpose: str = "",
    **overrides: Any,
) -> tuple[Any, dict[str, Any]]:
    kwargs, cfg = _build_kwargs(role, system, messages, overrides)
    t0 = time.monotonic()
    resp = _sclient().messages.create(**kwargs)
    try:
        rec = _record(role, cfg, prompt_id, purpose, system, messages, resp, t0)
        _log(rec, patient_id, system, messages, resp)
    except Exception as exc:
        import sys

        print(f"[llm_client] audit log failed: {exc!r}", file=sys.stderr)
        rec = {"role": role, "model": cfg["model"], "audit_error": repr(exc)}
    return resp, rec
