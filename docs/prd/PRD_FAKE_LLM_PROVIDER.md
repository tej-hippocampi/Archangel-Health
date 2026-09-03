# PRD — `ASCLEPIUS_LLM_PROVIDER=fake`: run every LLM code path without a key

**Goal:** Claude Code (and any developer) can boot the app, run the suite, run
generation jobs, and exercise every LLM-backed endpoint in a sandbox that holds no
API key — deterministically. Real-model behaviour is tested in CI with a GitHub
secret, never by pasting keys into a sandbox.

Verified against `Archangel-Health-main (32)`.

---

## §0 Why this is needed

- `ai/llm_client.py` builds real clients on first use (`_aclient` :34, `_sclient`
  :41, OpenAI :48/:58) from `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`. No fake path.
- Tests cope by monkeypatching `call_llm` per file (e.g.
  `test_asclepius_baselines.py:40 _stub_llm`) — 14 modules call the client, 27
  distinct `purpose=` strings, and every new test re-invents the stub.
- Anything outside pytest (smoke scripts, admin generation, the app itself) needs a
  real key → the paste-and-delete loop. The Claude Code cloud env forbids secrets.

## §1 Design — one switch, one seam, purpose-keyed responses

**The seam is `call_llm` / `call_llm_sync`** (`llm_client.py:419`, `:514`). Both
already route on `resolve_provider(cfg["model"])`. Add a third provider:

```python
# model_config.py
def resolve_provider(model_id):            # existing
    if os.getenv("ASCLEPIUS_LLM_PROVIDER", "").lower() == "fake":
        return "fake"
    ...
```

Everything downstream of the provider branch — `_record()` telemetry, `first_text`,
`_LLMResult` shape, prompt-sha, usage — stays identical, so callers cannot tell.
**The fake returns a real `_LLMResult`**, never a bare string.

### 1.1 Purpose-keyed fixtures

New module `ai/fake_llm.py`. Responses are looked up by the `purpose` argument that
every call already passes (27 values in the repo — enumerate them in the module and
**fail loudly on an unknown purpose** rather than returning a generic string; an
unknown purpose is a call site that forgot to declare itself, and the fake should
make that visible):

| purpose | fake returns |
|---|---|
| `asclepius_case_generation`, `asclepius_prompt_generation` | a valid case / prompt JSON matching `schemas` (build from `gold_cases` seeds so it passes the same validators) |
| `asclepius_candidate_generation` | two distinct, well-formed A/B answers |
| `asclepius_case_judge`, `asclepius_hardness_judge`, `asclepius_prompt_judge` | JSON verdicts that **pass** the gates (so pipelines proceed); a `FAKE_LLM_VERDICT=fail` env override makes them fail, for testing the rejection paths |
| `asclepius_grounding_check`, `asclepius_consistency_check` | pass verdicts |
| `asclepius_prelabel_suggestion` | a suggestion object with `confidence: "low"` |
| `asclepius_reasoning_split`, `asclepius_reasoning_pregrade` | 3 steps, all `good` |
| baselines / empirical difficulty | a fixed answer string per model role |
| citations, hs_enrich, stt, community digest/websearch | minimal valid payloads |

Determinism: same inputs → same output (seed from `_prompt_sha`), so tests are
stable and diffs are reviewable.

### 1.2 Vision + thinking

`image_block` messages are accepted and ignored; `emits_thinking` models return an
empty thinking block. Model-constraint tests (`test_llm_model_constraints.py`) still
run against the real config table — the fake only replaces the *transport*.

### 1.3 Latency + telemetry

Zero sleep by default; `FAKE_LLM_LATENCY_MS` for tests that exercise timeouts.
`_record()` writes the call to the AI-calls log with `provider="fake"` so the admin
AI-calls view shows sandbox activity honestly.

## §2 Wire it in

- `conftest.py`: `os.environ.setdefault("ASCLEPIUS_LLM_PROVIDER", "fake")`. Then
  **delete the per-file stubs** where the fake's fixture satisfies the assertion;
  keep a monkeypatch only where a test needs a *specific* answer. Expected: most of
  the 40-odd stubs go.
- Claude Code cloud env variables: add `ASCLEPIUS_LLM_PROVIDER=fake`.
- `AGENTS.md`: one line — "No keys in the sandbox by design; `ASCLEPIUS_LLM_PROVIDER
  =fake` is set, every LLM path runs. Real-model checks: trigger the `llm-smoke`
  workflow."
- Guard: if `ENV=production` and provider is `fake`, refuse to boot with a clear
  error. A fake in production is a silent data-corruption machine.

## §3 Real-model testing moves to CI

New workflow `.github/workflows/llm-smoke.yml`, `workflow_dispatch` only:
`ANTHROPIC_API_KEY` from GitHub Secrets → `python scripts/smoke_multimodal.py
--n 2` → uploads the report as an artifact. Claude Code pushes a branch; a human
clicks Run; results come back in the log. The key never leaves GitHub.

## §4 Tests

```
- resolve_provider returns "fake" iff env set; "anthropic"/"openai" otherwise
- every purpose in the repo has a fixture (test enumerates purpose= strings via
  grep-equivalent AST scan and asserts coverage) — unknown purpose raises
- fake output passes the same schema validators real output must pass
- FAKE_LLM_VERDICT=fail flips every judge to reject
- determinism: two calls with identical inputs are byte-identical
- production guard: ENV=production + fake → boot refuses
- full suite green with no ANTHROPIC_API_KEY / OPENAI_API_KEY in the environment
  (CI job runs with both unset to prove it)
```

## §5 Do not touch

Model config table, sampling/thinking constraints, `_record` schema, prompt
registry, any prompt text.
