import asyncio
import json
import os
import time
import hashlib
from typing import Any, Optional

from anthropic import Anthropic, AsyncAnthropic

from ai.model_config import APP_AI_CONFIG_VERSION, resolve, resolve_provider, api_model_id, UnknownProvider
from team_store import TeamStore

_async_client: Optional[AsyncAnthropic] = None
_sync_client: Optional[Anthropic] = None
_async_openai = None
_sync_openai = None
_event_store: Optional[TeamStore] = None


def _llm_timeout_sec() -> float:
    # 180s default: the heaviest legitimate call (opus case-gen, 6000 tokens) can
    # exceed 90s; a too-tight timeout would abort a call that was about to succeed and
    # (before the retry fix below) re-run it at 2× cost. Env-overridable.
    try:
        return float(os.getenv("ASCLEPIUS_LLM_TIMEOUT_SEC", "180"))
    except (TypeError, ValueError):
        return 180.0


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


def _aopenai():
    """Lazily construct the async OpenAI client (mirrors the Anthropic one). Import is
    lazy so the package is only required when an OpenAI model is actually routed to."""
    global _async_openai
    if _async_openai is None:
        from openai import AsyncOpenAI  # local import: optional dependency
        _async_openai = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _async_openai


def _sopenai():
    global _sync_openai
    if _sync_openai is None:
        from openai import OpenAI
        _sync_openai = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _sync_openai


def _store() -> TeamStore:
    global _event_store
    if _event_store is None:
        _event_store = TeamStore()
    return _event_store


# ── Provider-agnostic response normalization ─────────────────────────────────
# OpenAI and Anthropic return different response shapes. We normalize an OpenAI
# response into an Anthropic-shaped lightweight object so EVERY caller —
# first_text(), _record(), and any downstream reader — stays provider-agnostic and
# unchanged. The Anthropic path returns the raw SDK object as before.
class _TextBlock:
    type = "text"

    def __init__(self, text: str):
        self.text = text or ""


class _Usage:
    def __init__(self, input_tokens: Optional[int], output_tokens: Optional[int]):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _LLMResult:
    """Anthropic-shaped: `.content[0].text`, `.usage.input_tokens/output_tokens`,
    `._request_id` — so first_text/_record read it exactly like an Anthropic resp."""

    def __init__(self, text: str, input_tokens=None, output_tokens=None, request_id=None):
        self.content = [_TextBlock(text)]
        self.usage = _Usage(input_tokens, output_tokens)
        self._request_id = request_id


def first_text(resp: Any) -> str:
    # Anthropic-shaped (raw SDK, mocked test resps, or our normalized _LLMResult).
    blocks = list(getattr(resp, "content", []) or [])
    for block in blocks:
        if getattr(block, "type", None) == "text":
            return getattr(block, "text", "") or ""
    if blocks:
        return getattr(blocks[0], "text", "") or ""
    # OpenAI-shaped fallbacks (should not normally reach here — we normalize inside
    # the client — but keep callers safe if a raw OpenAI resp is ever passed).
    ot = getattr(resp, "output_text", None)
    if isinstance(ot, str):
        return ot
    choices = getattr(resp, "choices", None)
    if choices:
        msg = getattr(choices[0], "message", None)
        if msg is not None:
            return getattr(msg, "content", "") or ""
    return ""


def _prompt_sha(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]


def _user_text(messages: list[dict[str, Any]]) -> str:
    """Flatten the user/assistant messages into a single input string for OpenAI's
    single-`input` shape (Anthropic keeps the structured messages list)."""
    parts: list[str] = []
    for m in messages or []:
        content = m.get("content")
        if isinstance(content, list):  # anthropic block form
            content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
        if content:
            parts.append(str(content))
    return "\n\n".join(parts)


def _is_openai_reasoning(model: str) -> bool:
    m = (model or "").lower().replace("openai:", "")
    return m.startswith(("o1", "o3", "o4", "gpt-5", "gpt5"))


def _openai_output_cap(max_tokens: Optional[int], reasoning: bool) -> int:
    """Effective ``max_output_tokens`` for OpenAI.

    For REASONING models (o1/o3/o4/gpt-5) the hidden reasoning tokens are drawn from
    the SAME ``max_output_tokens`` budget as the visible answer. A small cap (e.g. the
    2000-token baseline role) is routinely consumed ENTIRELY by reasoning on a hard
    multi-panel clinical case, so the API returns ``status="incomplete"`` with an
    EMPTY ``output_text`` — which would silently make every two-frontier pair fail and
    mark every task ``needs_baseline`` (the whole feature yields no data). So for
    reasoning models we add a generous reasoning reserve on top of the requested
    answer budget. Env-overridable via ``LLM_OPENAI_REASONING_RESERVE`` (default
    12000). Non-reasoning models are unchanged."""
    base = int(max_tokens or 0) or 2000
    if not reasoning:
        return base
    try:
        reserve = int(os.getenv("LLM_OPENAI_REASONING_RESERVE", "12000"))
    except (TypeError, ValueError):
        reserve = 12000
    return base + max(0, reserve)


def _build_kwargs(role: str, system: str, messages: list[dict[str, Any]], overrides: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    cfg = resolve(role)
    if "model" in overrides:  # explicit per-call model (e.g. a baseline id) wins
        cfg = {**cfg, "model": overrides.pop("model")}
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
    provider: str = "anthropic",
) -> dict[str, Any]:
    prompt = None
    if prompt_id:
        from prompts.registry import prompt_meta

        # An unregistered prompt_id must not sink the whole audit record (which now
        # carries provider/request_id/usage for the two-frontier trail). Degrade the
        # prompt-meta field alone rather than losing every other field to the
        # call_llm fallback rec.
        try:
            prompt = prompt_meta(prompt_id)
        except Exception:  # noqa: BLE001 — telemetry must never break a call
            prompt = {"prompt_id": prompt_id}
    req_id = getattr(resp, "_request_id", None) or getattr(resp, "id", None)
    return {
        "role": role,
        "provider": provider,
        "model": cfg["model"],
        "ai_config_version": APP_AI_CONFIG_VERSION,
        "prompt": prompt,
        "purpose": purpose,
        "latency_ms": int((time.monotonic() - t0) * 1000),
        "usage": {
            "input_tokens": getattr(getattr(resp, "usage", None), "input_tokens", None),
            "output_tokens": getattr(getattr(resp, "usage", None), "output_tokens", None),
            # legacy keys kept for existing readers
            "input": getattr(getattr(resp, "usage", None), "input_tokens", None),
            "output": getattr(getattr(resp, "usage", None), "output_tokens", None),
        },
        "request_id": req_id,
        "anthropic_request_id": req_id if provider == "anthropic" else None,
        "input_sha": _prompt_sha(system + json.dumps(messages, default=str)),
    }


def _log(record: dict[str, Any], patient_id: Optional[str], system: str, messages: list[dict[str, Any]], resp: Any) -> None:
    payload = dict(record)
    if os.getenv("LLM_LOG_RAW") == "1":
        payload["raw_input"] = {"system": system, "messages": messages}
        payload["raw_output"] = first_text(resp)
    _store().log_event(patient_id=patient_id, event_type="llm_call", payload=payload)


# ── OpenAI call (async) — normalized to an Anthropic-shaped result ───────────
async def _openai_create_async(model: str, system: str, messages: list[dict[str, Any]], max_tokens: int, temperature) -> _LLMResult:
    client = _aopenai()
    model = api_model_id(model)
    user_text = _user_text(messages)
    reasoning = _is_openai_reasoning(model)
    out_cap = _openai_output_cap(max_tokens, reasoning)
    # Prefer the Responses API (uniform across reasoning + non-reasoning models);
    # fall back to chat.completions if the installed SDK lacks it.
    try:
        params: dict[str, Any] = {"model": model, "instructions": system, "input": user_text,
                                  "max_output_tokens": out_cap}
        if temperature is not None and not reasoning:
            params["temperature"] = temperature
        resp = await client.responses.create(**params)
        text = getattr(resp, "output_text", "") or ""
        usage = getattr(resp, "usage", None)
        return _LLMResult(text,
                          getattr(usage, "input_tokens", None),
                          getattr(usage, "output_tokens", None),
                          getattr(resp, "id", None))
    except (AttributeError, TypeError):
        # Older SDK / shape mismatch → chat.completions with reasoning-safe params.
        params = {"model": model,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user_text}]}
        if reasoning:
            params["max_completion_tokens"] = out_cap
        else:
            params["max_tokens"] = out_cap
            if temperature is not None:
                params["temperature"] = temperature
        resp = await client.chat.completions.create(**params)
        choice = resp.choices[0]
        text = getattr(getattr(choice, "message", None), "content", "") or ""
        usage = getattr(resp, "usage", None)
        return _LLMResult(text,
                          getattr(usage, "prompt_tokens", None),
                          getattr(usage, "completion_tokens", None),
                          getattr(resp, "id", None))


async def _anthropic_create_async(kwargs: dict[str, Any]) -> Any:
    kwargs = {**kwargs, "model": api_model_id(kwargs.get("model", ""))}
    return await _aclient().messages.create(**kwargs)


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
    provider = resolve_provider(cfg["model"])  # raises UnknownProvider on garbage ids
    t0 = time.monotonic()
    timeout = _llm_timeout_sec()

    async def _do():
        if provider == "openai":
            return await _openai_create_async(cfg["model"], system, messages,
                                              kwargs.get("max_tokens"), kwargs.get("temperature"))
        return await _anthropic_create_async(kwargs)

    for attempt in range(2):  # one retry on TRANSIENT errors only
        try:
            resp = await asyncio.wait_for(_do(), timeout=timeout)
            break
        except asyncio.TimeoutError:
            # A timeout means the call was genuinely slow (or hung). Re-running an
            # expensive generation is costly and unlikely to be faster, and would
            # double the worst-case latency of in-request callers — so do NOT retry a
            # timeout; fail fast and let the caller degrade.
            import sys
            print(f"[llm_client] {provider} call timed out after {timeout}s ({cfg['model']})", file=sys.stderr)
            raise
        except Exception as exc:  # noqa: BLE001 — graceful-degrade, never crash the pipeline
            # Do NOT retry PERMANENT client errors — a bad/again-bad API key (401),
            # forbidden (403), an unknown/no-access model (404), or a malformed request
            # (400) will fail identically on a retry, so a retry only wastes a round-trip
            # and doubles the failure latency. Retry only transient classes (429 rate
            # limit, 5xx, network) once. Both the OpenAI and Anthropic SDK errors expose
            # ``status_code``; absence (a raw network error) is treated as transient.
            status = getattr(exc, "status_code", None)
            permanent = status in (400, 401, 403, 404)
            if attempt == 0 and not permanent:
                await asyncio.sleep(0.6)
                continue
            import sys
            print(f"[llm_client] {provider} call failed ({cfg['model']}): {exc!r}", file=sys.stderr)
            raise
    try:
        rec = _record(role, cfg, prompt_id, purpose, system, messages, resp, t0, provider=provider)
        _log(rec, patient_id, system, messages, resp)
    except Exception as exc:
        import sys

        print(f"[llm_client] audit log failed: {exc!r}", file=sys.stderr)
        rec = {"role": role, "provider": provider, "model": cfg["model"], "audit_error": repr(exc)}
    return resp, rec


def _openai_create_sync(model: str, system: str, messages: list[dict[str, Any]], max_tokens: int, temperature) -> _LLMResult:
    client = _sopenai()
    model = api_model_id(model)
    user_text = _user_text(messages)
    reasoning = _is_openai_reasoning(model)
    out_cap = _openai_output_cap(max_tokens, reasoning)
    try:
        params: dict[str, Any] = {"model": model, "instructions": system, "input": user_text,
                                  "max_output_tokens": out_cap}
        if temperature is not None and not reasoning:
            params["temperature"] = temperature
        resp = client.responses.create(**params)
        usage = getattr(resp, "usage", None)
        return _LLMResult(getattr(resp, "output_text", "") or "",
                          getattr(usage, "input_tokens", None),
                          getattr(usage, "output_tokens", None),
                          getattr(resp, "id", None))
    except (AttributeError, TypeError):
        params = {"model": model,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user_text}]}
        if reasoning:
            params["max_completion_tokens"] = out_cap
        else:
            params["max_tokens"] = out_cap
            if temperature is not None:
                params["temperature"] = temperature
        resp = client.chat.completions.create(**params)
        choice = resp.choices[0]
        usage = getattr(resp, "usage", None)
        return _LLMResult(getattr(getattr(choice, "message", None), "content", "") or "",
                          getattr(usage, "prompt_tokens", None),
                          getattr(usage, "completion_tokens", None),
                          getattr(resp, "id", None))


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
    provider = resolve_provider(cfg["model"])
    t0 = time.monotonic()
    if provider == "openai":
        resp = _openai_create_sync(cfg["model"], system, messages,
                                   kwargs.get("max_tokens"), kwargs.get("temperature"))
    else:
        resp = _sclient().messages.create(**{**kwargs, "model": api_model_id(kwargs.get("model", ""))})
    try:
        rec = _record(role, cfg, prompt_id, purpose, system, messages, resp, t0, provider=provider)
        _log(rec, patient_id, system, messages, resp)
    except Exception as exc:
        import sys

        print(f"[llm_client] audit log failed: {exc!r}", file=sys.stderr)
        rec = {"role": role, "provider": provider, "model": cfg["model"], "audit_error": repr(exc)}
    return resp, rec
