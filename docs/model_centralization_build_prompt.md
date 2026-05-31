# Centralize Models + Version & Log Every Prompt — Cursor Build Prompt

> Paste this whole file into Cursor as one build task. It is written against the
> **verified** state of this repo (every call site, client type, and line below was
> checked against the actual code). Build it top to bottom. The goal: every Claude
> call resolves its model from ONE place, every prompt carries a version + content
> hash, and every call logs which model + prompt fingerprint produced the output —
> reproducibility, an audit trail, and the seed of an FDA Predetermined Change
> Control Plan.

---

## 0. Verified ground truth (do not re-derive — this was checked against the code)

`"claude-sonnet-4-6"` is hardcoded across the backend and every module builds its
own Anthropic client. Here is the **exact, verified** inventory. Note the
`client` column — it is the thing the PRD got partly wrong, and it determines
which wrapper (`call_llm` vs `call_llm_sync`) each site uses.

| # | File:line (create call) | Client | Enclosing fn | Uses tools? | Current temp | Role |
|---|---|---|---|---|---|---|
| 1 | `pipeline/generate.py:125` | `AsyncAnthropic` (`self.client`, init L24) | `_call_claude` (async) | no | **none → API default** | `generation` |
| 2 | `pipeline/extract.py:76` | `AsyncAnthropic` (`self.client`, init L67) | `extract` (async) | no | **none → default** | `extraction` |
| 3 | `eligibility/extract.py:211` (const `MODEL` L24) | `AsyncAnthropic` (`_client()` L172) | `_call_with_retry` (async) | **YES** (`tool_choice`) | `0.0` | `eligibility_extract` |
| 4 | `triage/intraop/extractor_llm.py:217` (const `_MODEL` L34) | `AsyncAnthropic` (`_client()` L176) | `extract` (async) | **YES** | `0.0` | `intraop_extract` |
| 5 | `intake_section_chat.py:329` | **sync `Anthropic`** (`_anthropic_client()` L306) | `_repair_parsed` (**sync**) | no | `0.0` | `intake_chat` |
| 6 | `intake_section_chat.py:420` | **sync `Anthropic`** | `run_intake_section_turn` (**sync**) | **YES** | `0.2` | `intake_chat` |
| 7 | `intake_section_chat.py:445` | **sync `Anthropic`** | `run_intake_section_turn` (**sync**) | no | `0.2` | `intake_chat` |
| 8 | `main.py:1084` | **sync `Anthropic`** (built inline L1071) | `_evaluate_semantic_escalation_llm` (**async**) | no | **none → default** | `escalation_classifier` |
| 9 | `main.py:3229` | **sync `Anthropic`** (built inline L3210) | `digital_care_companion_chat` (**async**) | no | **none → default** | `care_companion_chat` |
| 10 | `routers/internal.py:238` | `AsyncAnthropic` (built L227) | `run_prompt` (async) | no | **none → default** | `avatar_chat` (prompt-lab) |
| 11 | `routers/internal.py:271` | `AsyncAnthropic` | `run_prompt` (async) | no | **none → default** | `generation` (prompt-lab) |

Other verified facts:
- API key: `os.getenv("ANTHROPIC_API_KEY")` everywhere. No shared client factory.
- `prompts/registry.py::PROMPT_REGISTRY` exists; entry shape is
  `{label, content, file, variable, type, (paired_voice|paired_battlecard)?}`.
  **No `version` field yet.** Existing ids: `avatar_chat`, `diagnosis_voice`,
  `diagnosis_battlecard`, `treatment_voice`, `treatment_battlecard`, `preop_voice`,
  `preop_battlecard`, `postop_voice`, `postop_battlecard`.
- `team_store.py::log_event(*, patient_id: str, event_type: str,
  occurred_at=None, payload=None)` — **`patient_id` is REQUIRED** today. It calls
  `get_episode(patient_id)`. Several LLM calls have NO patient_id.
- `eligibility/store.py::append_audit(*, action, actor, patient_id=None, ...)` is a
  separate in-memory audit ring buffer (patient_id already optional).
- `triage/intraop/extractor_llm.py:34` already honors env var
  `INTRAOP_EXTRACTOR_MODEL` — preserve that override.

> ⚠️ **Behavior-preservation rule (read before touching temperatures).** Sites 1,
> 2, 8, 9, 10, 11 set **no** `temperature` today, so they run at the Anthropic API
> default. If you pin them to the PRD's proposed values you will **change model
> outputs** during what should be a pure refactor. **Default: preserve current
> behavior** — see §1 for how the registry encodes "don't send a temperature."
> Pinning temperatures is a separate, deliberate change; do not bundle it here.

---

## 1. Central model config — `backend/ai/model_config.py` (new)

```python
import os

APP_AI_CONFIG_VERSION = "2026-05-31.1"   # bump on ANY model/role/temperature change

# temperature: None  -> do NOT send a temperature (use API default; preserves
#                       the current behavior of sites that never set one)
#              float -> send exactly this value
MODEL_REGISTRY: dict[str, dict] = {
    # role                     model                temperature  max_tokens
    "generation":            {"model": "claude-sonnet-4-6", "temperature": None, "max_tokens": 2000},
    "extraction":            {"model": "claude-sonnet-4-6", "temperature": None, "max_tokens": 2500},
    "eligibility_extract":   {"model": "claude-sonnet-4-6", "temperature": 0.0,  "max_tokens": 4000},
    "intraop_extract":       {"model": "claude-sonnet-4-6", "temperature": 0.0,  "max_tokens": 4000},
    "intake_chat":           {"model": "claude-sonnet-4-6", "temperature": 0.2,  "max_tokens": 3000},
    "escalation_classifier": {"model": "claude-sonnet-4-6", "temperature": None, "max_tokens": 120},
    "care_companion_chat":   {"model": "claude-sonnet-4-6", "temperature": None, "max_tokens": 350},
    "avatar_chat":           {"model": "claude-sonnet-4-6", "temperature": None, "max_tokens": 150},
    "grounding_judge":       {"model": "claude-sonnet-4-6", "temperature": 0.0,  "max_tokens": 1500},
}

# Back-compat env overrides that already exist in the codebase, honored per role.
_LEGACY_ENV = {"intraop_extract": "INTRAOP_EXTRACTOR_MODEL"}

def resolve(role: str) -> dict:
    cfg = dict(MODEL_REGISTRY[role])
    # 1) generic per-role override, e.g. MODEL_GENERATION=claude-x
    env_model = os.getenv(f"MODEL_{role.upper()}")
    # 2) preserve any legacy env var (e.g. INTRAOP_EXTRACTOR_MODEL)
    if not env_model and role in _LEGACY_ENV:
        env_model = os.getenv(_LEGACY_ENV[role])
    if env_model:
        cfg["model"] = env_model
    return cfg
```

> Note on `intake_chat`: three sites currently use temps `0.0`, `0.2`, `0.2`. To
> preserve the `_repair_parsed` (0.0) site exactly, pass `temperature=0.0` as a
> per-call override there (see §3); the role default `0.2` matches the two turn
> calls. This keeps every output identical to today.

---

## 2. Version-fingerprint every prompt — extend `backend/prompts/registry.py`

1. Add a `"version"` string (start every entry at `"1.0.0"`) to each existing
   `PROMPT_REGISTRY` entry. Do **not** change the existing fields or ids.
2. Add helpers:

```python
import hashlib

def prompt_sha(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]

def prompt_meta(prompt_id: str) -> dict:
    e = PROMPT_REGISTRY[prompt_id]
    return {
        "prompt_id": prompt_id,
        "version": e.get("version", "0.0.0"),
        "sha": prompt_sha(e["content"]),
    }
```

3. **Register the prompts that aren't in the registry yet**, so EVERY Claude call
   can pass a `prompt_id`. Add entries (type can be a new `"system"` value) for:
   - `ehr_extract` → `EXTRACTION_SYSTEM` / `EXTRACTION_PROMPT` in `pipeline/extract.py`.
   - `eligibility_extract` → the system prompt in `eligibility/extract.py`.
   - `intraop_extract` → `_system_prompt(...)` template in `triage/intraop/extractor_llm.py`
     (register the template; the `sha` will cover the static skeleton).
   - `semantic_escalation` → `eval_prompt` built around `main.py:1075`.
   - `care_companion_chat` → the care-companion system prompt / `AVATAR_BEHAVIOR_TEMPLATE`
     used at `main.py:3210`+ (register the template string).
   - `intake_repair`, `intake_turn`, `intake_turn_json` → the three systems in
     `intake_section_chat.py`.

   For dynamically-assembled prompts, register the **stable template/skeleton** as
   `content`; the `sha` then proves which template version was live even though
   the runtime fill-ins (PHI) are never stored. The `sha` is the safety net: edit
   the text, the fingerprint changes, the logs prove which text ran — even if
   someone forgets to bump `version`.

---

## 3. One logging wrapper — `backend/ai/llm_client.py` (new)

This is the single entry point for all Claude calls. **It must support tool-use
calls** (sites 3, 4, 6 pass `tools`/`tool_choice` and read `tool_use` blocks), so
it returns the **raw Anthropic response** plus the audit record — never just
`.content[0].text`. Provide convenience text extraction separately, and provide
both an async and a sync variant.

```python
import json, os, time
from anthropic import Anthropic, AsyncAnthropic
from backend.ai.model_config import resolve, APP_AI_CONFIG_VERSION
from backend.prompts.registry import prompt_meta, prompt_sha
# import the existing team store singleton used elsewhere (e.g. main.py: _team_store)
from backend.team_store import team_store  # adjust to the actual singleton accessor

_async_client = None
_sync_client = None
def _aclient():
    global _async_client
    if _async_client is None:
        _async_client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _async_client
def _sclient():
    global _sync_client
    if _sync_client is None:
        _sync_client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _sync_client

def first_text(resp) -> str:
    """Convenience for non-tool callers (matches old `resp.content[0].text`)."""
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            return block.text
    return ""

def _build_kwargs(role: str, system: str, messages: list[dict], overrides: dict) -> dict:
    cfg = resolve(role)
    kwargs = {
        "model": cfg["model"],
        "max_tokens": overrides.pop("max_tokens", cfg["max_tokens"]),
        "system": system,
        "messages": messages,
    }
    temp = overrides.pop("temperature", cfg["temperature"])
    if temp is not None:                       # None => omit, use API default
        kwargs["temperature"] = temp
    kwargs.update(overrides)                    # tools, tool_choice, etc. pass through
    return kwargs, cfg

def _record(role, cfg, prompt_id, purpose, system, messages, resp, t0) -> dict:
    return {
        "role": role,
        "model": cfg["model"],
        "ai_config_version": APP_AI_CONFIG_VERSION,
        "prompt": prompt_meta(prompt_id) if prompt_id else None,
        "purpose": purpose,
        "latency_ms": int((time.monotonic() - t0) * 1000),
        "usage": {
            "input": getattr(resp.usage, "input_tokens", None),
            "output": getattr(resp.usage, "output_tokens", None),
        },
        "anthropic_request_id": getattr(resp, "_request_id", None),
        "input_sha": prompt_sha(system + json.dumps(messages, default=str)),
    }

def _log(record: dict, patient_id, system, messages, resp):
    if os.getenv("LLM_LOG_RAW") == "1":         # PHI gate (see note below)
        record = {**record, "raw_input": {"system": system, "messages": messages},
                  "raw_output": first_text(resp)}
    team_store.log_event(patient_id=patient_id, event_type="llm_call", payload=record)

async def call_llm(*, role: str, system: str, messages: list[dict],
                   prompt_id: str | None = None, patient_id: str | None = None,
                   purpose: str = "", **overrides):
    kwargs, cfg = _build_kwargs(role, system, messages, overrides)
    t0 = time.monotonic()
    resp = await _aclient().messages.create(**kwargs)
    rec = _record(role, cfg, prompt_id, purpose, system, messages, resp, t0)
    _log(rec, patient_id, system, messages, resp)
    return resp, rec

def call_llm_sync(*, role: str, system: str, messages: list[dict],
                  prompt_id: str | None = None, patient_id: str | None = None,
                  purpose: str = "", **overrides):
    kwargs, cfg = _build_kwargs(role, system, messages, overrides)
    t0 = time.monotonic()
    resp = _sclient().messages.create(**kwargs)
    rec = _record(role, cfg, prompt_id, purpose, system, messages, resp, t0)
    _log(rec, patient_id, system, messages, resp)
    return resp, rec
```

**PHI rule (healthcare — required):** by default log only **identifiers, hashes,
token counts, versions, latency** — NOT raw prompt text or model output (they
contain PHI). Raw capture is gated behind `LLM_LOG_RAW=1` and must land only in
the same access-controlled store as the existing audit logs. `input_sha` proves
reproducibility without storing PHI.

### 3a. Make `log_event` accept a null patient (prerequisite)

In `backend/team_store.py`, relax `log_event` so non-patient calls can be logged
through the **existing** sink (do not invent a new one):

```python
def log_event(self, *, patient_id: Optional[str] = None, event_type: str,
              occurred_at: Optional[str] = None,
              payload: Optional[Dict[str, Any]] = None) -> None:
    episode = self.get_episode(patient_id) if patient_id else None
    # ... existing INSERT; episode_open_date = episode.get("open_date") if episode else None
```

Confirm `get_episode(None)` is never called (the guard above prevents it). All
existing callers pass `patient_id=` by keyword, so this is backward compatible.

---

## 4. Refactor every call site to the wrapper

Replace each `messages.create(...)` with the matching wrapper call. **Async sites
use `await call_llm(...)`; sync sites use `call_llm_sync(...)`.** Tool-use sites
pass `tools=`/`tool_choice=` straight through as overrides and read the returned
raw `resp`.

| # | File:line | Wrapper | role | prompt_id | Notes |
|---|---|---|---|---|---|
| 1 | `pipeline/generate.py:125` | `await call_llm` | `generation` | the matching `*_voice`/`*_battlecard` id | replace `first_text(resp)` for the returned text; pass `patient_id` if available |
| 2 | `pipeline/extract.py:76` | `await call_llm` | `extraction` | `ehr_extract` | |
| 3 | `eligibility/extract.py:211` | `await call_llm` | `eligibility_extract` | `eligibility_extract` | **pass `tools=[tool], tool_choice={...}`**; read tool_use from `resp` as before |
| 4 | `triage/intraop/extractor_llm.py:217` | `await call_llm` | `intraop_extract` | `intraop_extract` | **tool-use**; keep `INTRAOP_EXTRACTOR_MODEL` override via `resolve()`; keep the `asyncio.wait_for(..., timeout=...)` wrapper around the call |
| 5 | `intake_section_chat.py:329` | `call_llm_sync` | `intake_chat` | `intake_repair` | pass `temperature=0.0` override to match today |
| 6 | `intake_section_chat.py:420` | `call_llm_sync` | `intake_chat` | `intake_turn` | **tool-use** (`INTAKE_TURN_TOOL`); temp 0.2 = role default |
| 7 | `intake_section_chat.py:445` | `call_llm_sync` | `intake_chat` | `intake_turn_json` | temp 0.2 = role default |
| 8 | `main.py:1084` | `call_llm_sync` | `escalation_classifier` | `semantic_escalation` | currently sync client inside async fn — keep sync via `call_llm_sync` to preserve behavior; pass `patient_id` |
| 9 | `main.py:3229` | `call_llm_sync` | `care_companion_chat` | `care_companion_chat` | pass `patient_id` |
| 10 | `routers/internal.py:238` | `await call_llm` | `avatar_chat` | `avatar_chat` | prompt-lab; no patient_id |
| 11 | `routers/internal.py:271` | `await call_llm` | `generation` | the selected prompt id from the request | prompt-lab; no patient_id |

Then:
- Delete the now-dead `MODEL` (`eligibility/extract.py:24`) and `_MODEL`
  (`triage/intraop/extractor_llm.py:34`) constants and the ad-hoc client
  constructors they fed (or have them delegate to the wrapper's clients).
- Each refactored non-tool call: `resp, _ = await call_llm(...)` then
  `text = first_text(resp)` where the old code used `resp.content[0].text`.

---

## 5. Tests — `backend/tests/test_llm_versioning.py`

Use the existing pytest style; mock the Anthropic client so tests are offline.

- **Grep guard (regression lock):** assert no `claude-` model literal exists
  anywhere under `backend/` **except** `backend/ai/model_config.py`. Concretely:
  ```python
  import subprocess
  out = subprocess.run(
      ["grep", "-rEn", "claude-(sonnet|opus|haiku)", "backend/",
       "--include=*.py", "--exclude-dir=tests"],
      capture_output=True, text=True).stdout
  offenders = [l for l in out.splitlines() if "ai/model_config.py" not in l]
  assert offenders == [], offenders
  ```
- Assert every `PROMPT_REGISTRY` entry has a non-empty `version`.
- Assert `prompt_sha` changes when content changes (hash a string, mutate, compare).
- Mock the client; assert `call_llm` and `call_llm_sync` each emit an `llm_call`
  event whose payload contains `model`, `ai_config_version`, and
  `prompt.version` + `prompt.sha`.
- Assert raw text is **absent** from the logged payload by default and **present**
  only when `LLM_LOG_RAW=1`.
- Assert `resolve("generation")` honors `MODEL_GENERATION` env, and
  `resolve("intraop_extract")` still honors legacy `INTRAOP_EXTRACTOR_MODEL`.
- Assert `temperature=None` roles do **not** pass a `temperature` kwarg to
  `messages.create` (behavior preservation), while `0.0` roles do.
- Assert a tool-use call routed through the wrapper still receives `tools` /
  `tool_choice` and returns the raw response (so sites 3/4/6 keep working).

---

## 6. PR description (paste this in)

- **Reproducibility:** any past output traces to its exact model + prompt
  fingerprint (`prompt.version` + `prompt.sha` + `ai_config_version`).
- **Change control:** the content `sha` makes silent prompt edits impossible to
  hide; `APP_AI_CONFIG_VERSION` versions the whole model layer. This is the
  artifact an FDA PCCP / hospital security review expects.
- **Single switch:** changing or A/B-testing a model is now one edit (or one env
  var) in `model_config.py`, logged automatically, instead of a hunt across ~11
  call sites.
- **Behavior preserved:** this PR is a pure refactor — temperatures and tool-use
  behavior are unchanged; sites that never set a temperature still don't. Pinning
  temperatures is intentionally deferred to a separate change.

---

## Build order

1. `backend/ai/model_config.py`.
2. `backend/prompts/registry.py`: add `version` to all entries + `prompt_sha` /
   `prompt_meta`; register the unregistered prompts.
3. `backend/team_store.py`: make `log_event` `patient_id` optional.
4. `backend/ai/llm_client.py`: `call_llm` + `call_llm_sync` (+ `first_text`).
5. Refactor the 11 call sites per the §4 table; delete dead `MODEL`/`_MODEL`.
6. `backend/tests/test_llm_versioning.py`; run `pytest backend/tests/test_llm_versioning.py`
   and the existing suite to confirm no behavior change.
```
