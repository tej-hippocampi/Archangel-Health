from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai import llm_client  # noqa: E402
from ai.model_config import resolve  # noqa: E402
from prompts.registry import PROMPT_REGISTRY, prompt_sha  # noqa: E402


class _StoreRecorder:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def log_event(self, *, patient_id=None, event_type: str, payload=None, occurred_at=None):  # noqa: ANN001
        self.events.append(
            {
                "patient_id": patient_id,
                "event_type": event_type,
                "payload": payload or {},
                "occurred_at": occurred_at,
            }
        )


def _text_response(text: str = "ok") -> object:
    return types.SimpleNamespace(
        content=[types.SimpleNamespace(type="text", text=text)],
        usage=types.SimpleNamespace(input_tokens=10, output_tokens=5),
        _request_id="req_123",
    )


def _tool_response() -> object:
    return types.SimpleNamespace(
        content=[types.SimpleNamespace(type="tool_use", name="demo_tool", input={"ok": True})],
        usage=types.SimpleNamespace(input_tokens=1, output_tokens=1),
        _request_id="req_tool",
    )


def _run(coro):
    return asyncio.run(coro)


def test_no_claude_literals_outside_model_config():
    backend_dir = Path(__file__).resolve().parent.parent
    offenders: list[str] = []
    for py_file in backend_dir.rglob("*.py"):
        if "tests" in py_file.parts:
            continue
        rel = py_file.relative_to(backend_dir.parent).as_posix()
        if rel == "backend/ai/model_config.py":
            continue
        text = py_file.read_text(encoding="utf-8")
        if any(x in text for x in ("claude-sonnet-", "claude-opus-", "claude-haiku-")):
            offenders.append(rel)
    assert offenders == [], offenders


def test_prompt_registry_entries_include_versions():
    assert PROMPT_REGISTRY
    for prompt_id, entry in PROMPT_REGISTRY.items():
        assert entry.get("version"), f"missing version for {prompt_id}"


def test_prompt_sha_changes_when_content_changes():
    before = prompt_sha("abc")
    after = prompt_sha("abcd")
    assert before != after


def test_call_llm_sync_logs_prompt_metadata_and_hides_raw_by_default(monkeypatch: pytest.MonkeyPatch):
    store = _StoreRecorder()
    sync_client = MagicMock()
    sync_client.messages.create = MagicMock(return_value=_text_response("hello"))
    monkeypatch.setattr(llm_client, "_sclient", lambda: sync_client)
    monkeypatch.setattr(llm_client, "_store", lambda: store)
    monkeypatch.delenv("LLM_LOG_RAW", raising=False)

    resp, rec = llm_client.call_llm_sync(
        role="avatar_chat",
        system="s",
        messages=[{"role": "user", "content": "hi"}],
        prompt_id="avatar_chat",
        patient_id=None,
    )
    assert llm_client.first_text(resp) == "hello"
    assert rec["model"]

    assert len(store.events) == 1
    payload = store.events[0]["payload"]
    assert store.events[0]["event_type"] == "llm_call"
    assert payload["model"]
    assert payload["ai_config_version"]
    assert payload["prompt"]["version"]
    assert payload["prompt"]["sha"]
    assert "raw_input" not in payload
    assert "raw_output" not in payload


def test_call_llm_logs_raw_only_with_env(monkeypatch: pytest.MonkeyPatch):
    store = _StoreRecorder()
    async_client = MagicMock()
    async_client.messages.create = AsyncMock(return_value=_text_response("raw-body"))
    monkeypatch.setattr(llm_client, "_aclient", lambda: async_client)
    monkeypatch.setattr(llm_client, "_store", lambda: store)
    monkeypatch.setenv("LLM_LOG_RAW", "1")

    _run(
        llm_client.call_llm(
            role="generation",
            system="sys",
            messages=[{"role": "user", "content": "msg"}],
            prompt_id="diagnosis_voice",
        )
    )
    payload = store.events[0]["payload"]
    assert payload["raw_input"]["system"] == "sys"
    assert payload["raw_output"] == "raw-body"


def test_resolve_honors_role_and_legacy_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MODEL_GENERATION", "claude-custom-a")
    monkeypatch.setenv("INTRAOP_EXTRACTOR_MODEL", "claude-custom-b")
    assert resolve("generation")["model"] == "claude-custom-a"
    assert resolve("intraop_extract")["model"] == "claude-custom-b"


def test_temperature_none_is_omitted_and_zero_is_preserved(monkeypatch: pytest.MonkeyPatch):
    sync_client = MagicMock()
    sync_client.messages.create = MagicMock(return_value=_text_response())
    monkeypatch.setattr(llm_client, "_sclient", lambda: sync_client)
    monkeypatch.setattr(llm_client, "_store", lambda: _StoreRecorder())

    llm_client.call_llm_sync(
        role="generation",
        system="s",
        messages=[{"role": "user", "content": "x"}],
    )
    _, kwargs = sync_client.messages.create.call_args
    assert "temperature" not in kwargs

    llm_client.call_llm_sync(
        role="eligibility_extract",
        system="s",
        messages=[{"role": "user", "content": "x"}],
    )
    _, kwargs = sync_client.messages.create.call_args
    assert kwargs["temperature"] == 0.0


def test_tool_use_passthrough_returns_raw_response(monkeypatch: pytest.MonkeyPatch):
    async_client = MagicMock()
    async_client.messages.create = AsyncMock(return_value=_tool_response())
    monkeypatch.setattr(llm_client, "_aclient", lambda: async_client)
    monkeypatch.setattr(llm_client, "_store", lambda: _StoreRecorder())

    resp, _ = _run(
        llm_client.call_llm(
            role="intraop_extract",
            system="tool-system",
            messages=[{"role": "user", "content": "x"}],
            tools=[{"name": "demo_tool"}],
            tool_choice={"type": "tool", "name": "demo_tool"},
        )
    )
    _, kwargs = async_client.messages.create.await_args
    assert kwargs["tools"] == [{"name": "demo_tool"}]
    assert kwargs["tool_choice"] == {"type": "tool", "name": "demo_tool"}
    assert getattr(resp.content[0], "type", None) == "tool_use"
