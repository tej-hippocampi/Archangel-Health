"""The rollout (PRD §6) — drive ``ClinicalEnv`` with an agent, log the trajectory.

Our ``rollout.py`` is just ONE consumer of the ``ClinicalEnv`` interface (PRD
§4.5); a lab is another. The agent loop is a provider-neutral TEXT protocol (the
model returns a JSON action each turn), so the two-frontier run compares OpenAI
and Anthropic through the *identical* environment and protocol — a fair
comparison, and it sidesteps the fact that ``llm_client`` only wires native
tool-use for Anthropic (both providers do plain text generation here).

Loop (PRD §6): present prompt → model emits thought+action → execute against
EHRState → append observation → repeat → stop on final_output or max-step cap →
verify → build the §1 record.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from .env import ClinicalEnv
from .schema import Provenance, TrajectoryRecord, Verification

_SYSTEM = """You are a clinician-agent working a real case inside a virtual EHR. \
You do NOT have the full chart — you must EARN information by calling tools.

On EACH turn respond with a SINGLE JSON object and nothing else:
  * to think then act:   {{"thought": "...", "tool": "<tool_name>", "input": {{...}}}}
  * to just think:       {{"thought": "..."}}
  * to submit and end:   {{"thought": "...", "final": {{"tool": "<submit_diagnosis|submit_plan|escalate>", "input": {{...}}}}}}

Available tools (name → JSON input schema):
{tool_docs}

Rules:
  * Read tools reveal withheld chart data; act tools record your decisions.
  * Order the decisive test BEFORE you conclude. Never take an unsafe action.
  * When you are ready, use a final submit tool to end the episode.
Reply with ONLY the JSON object."""


def _tool_docs(env: ClinicalEnv) -> str:
    lines = []
    for s in env.action_space():
        req = s.get("input_schema", {}).get("required") or []
        props = list((s.get("input_schema", {}).get("properties") or {}).keys())
        lines.append(f"  - {s['name']}({', '.join(props)}) — {s.get('description','')}"
                     + (f" [required: {', '.join(req)}]" if req else ""))
    return "\n".join(lines)


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    # Prefer a fenced or first balanced {...} block.
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    blob = m.group(0)
    for candidate in (blob, blob[: blob.rfind("}") + 1]):
        try:
            return json.loads(candidate)
        except Exception:
            continue
    return None


async def rollout(
    env: ClinicalEnv,
    *,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    role: str = "asclepius_baseline",
    seed: Optional[int] = None,
    run_rubric: bool = True,
) -> Dict[str, Any]:
    """Drive one agent through one episode; return the §1 trajectory record dict
    plus internal fields (``_env``, ``_provider``). ``model`` overrides the role's
    model (how two-frontier selects a provider — mirrors ``baselines.run_baselines``)."""
    observation, info = env.reset(seed=seed)
    system = _SYSTEM.format(tool_docs=_tool_docs(env))
    prompt = observation["prompt"]
    chart = observation["chart"]
    user0 = f"CASE PROMPT:\n{prompt}\n\nOPENING CHART (everything else must be earned via tools):\n{json.dumps(chart, ensure_ascii=False, default=str)}"
    messages: List[Dict[str, Any]] = [{"role": "user", "content": user0}]

    terminated = truncated = False
    turns = 0
    max_turns = env.max_steps + 4
    prov = provider

    while not (terminated or truncated) and turns < max_turns:
        turns += 1
        text = await _agent_turn(role, system, messages, model)
        if text is None:
            break
        if prov is None:
            prov = _provider_of(model)
        action = _extract_json(text)
        if not action:
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": "Respond with ONLY a single JSON object per the protocol."})
            continue
        messages.append({"role": "assistant", "content": json.dumps(action)})

        thought = action.get("thought")
        if thought:
            _o, _r, terminated, truncated, _i = env.step({"type": "thought", "content": str(thought)})

        final = action.get("final")
        if final and not (terminated or truncated):
            obs, _r, terminated, truncated, _i = env.step(
                {"tool": final.get("tool"), "input": final.get("input") or {}})
            break
        tool = action.get("tool")
        if tool and not (terminated or truncated):
            obs, _r, terminated, truncated, _i = env.step({"tool": tool, "input": action.get("input") or {}})
            messages.append({"role": "user",
                             "content": f"OBSERVATION: {json.dumps(obs.get('observation'), ensure_ascii=False, default=str)}"})

    # Post-episode reward (PRD §6.4).
    if run_rubric:
        from .verify import score_async

        verification = await score_async(env, prompt=prompt)
    else:
        verification = env.verify()

    record = _build_record(env, verification, provider=prov)
    record["_env"] = env
    record["_provider"] = prov
    return record


async def _agent_turn(role: str, system: str, messages: List[Dict[str, Any]],
                      model: Optional[str]) -> Optional[str]:
    try:
        from ai.llm_client import call_llm, first_text

        overrides: Dict[str, Any] = {"max_tokens": 900}
        if model:
            overrides["model"] = model
        resp, _rec = await call_llm(role=role, system=system, messages=messages,
                                    prompt_id="asclepius_env_rollout",
                                    purpose="asclepius_env_rollout", **overrides)
        return first_text(resp) or ""
    except Exception:
        return None


def _provider_of(model: Optional[str]) -> Optional[str]:
    if not model:
        return None
    try:
        from ai.model_config import resolve_provider

        return resolve_provider(model)
    except Exception:
        m = (model or "").lower()
        if m.startswith(("gpt", "o1", "o3", "o4", "chatgpt", "openai")):
            return "openai"
        if m.startswith(("claude", "anthropic")):
            return "anthropic"
        return None


def _build_record(env: ClinicalEnv, verification: Dict[str, Any], *,
                  provider: Optional[str] = None, ab_source: Optional[str] = None) -> Dict[str, Any]:
    compiled = env.compiled
    gt = env.ground_truth()
    prov = Provenance(
        case_source=_prov_source(compiled.get("case_source")),
        provider=provider,
        contributor_verified=False,
        ab_source=ab_source,
        case_id=compiled.get("case_ref"),
    )
    rec = TrajectoryRecord(
        task_id=_task_id(compiled),
        specialty=env.specialty,
        task_type=env.task_type,
        prompt=env.prompt(),
        trajectory=[{**s} for s in env.trajectory],
        verification=Verification(**verification) if verification else None,
        provenance=prov,
    )
    out = rec.model_dump(exclude_none=True)
    out["provenance"]["ground_truth_source"] = gt.get("source")
    return out


def _prov_source(case_source: Optional[str]) -> str:
    cs = case_source or "synthetic"
    if cs == "real_deid":
        return "real_deid"
    if cs == "gold" or cs == "gold_seed":
        return "gold"
    return "synthetic"


def _task_id(compiled: Dict[str, Any]) -> str:
    ref = compiled.get("case_ref") or "case"
    return f"{env_specialty_short(compiled)}-{compiled.get('task_template','task')}-{ref}"


def env_specialty_short(compiled: Dict[str, Any]) -> str:
    return (compiled.get("specialty") or "gen")[:5]


# ─── Two-frontier (PRD §6) ────────────────────────────────────────────────────
async def two_frontier_rollout(
    compiled: Dict[str, Any], *, models: Optional[List[str]] = None,
    seed: Optional[int] = None, run_rubric: bool = True,
) -> Dict[str, Any]:
    """Run the identical environment with both frontier providers (PRD §6). Two
    trajectories per case; the divergence is itself signal a physician can
    adjudicate (feeds the preference/PRM products). Stamps provider + ab_source."""
    from ..constants import baseline_models

    models = models or baseline_models()
    records: List[Dict[str, Any]] = []
    for m in models:
        env = ClinicalEnv(compiled)
        rec = await rollout(env, model=m, seed=seed, run_rubric=run_rubric)
        rec["provenance"]["ab_source"] = "two_frontier"
        records.append(rec)

    # Blind A/B assignment (reuse the two-frontier idea; deterministic here so a
    # buyer can replay). Divergence = do the two agents reach the same reward tier?
    rewards = [(r.get("verification") or {}).get("reward", 0.0) for r in records]
    divergence = round(abs((rewards[0] if rewards else 0) - (rewards[-1] if rewards else 0)), 3)
    return {"records": records, "reward_divergence": divergence,
            "models": models, "providers": [r.get("_provider") for r in records]}


# ─── Difficulty gate (PRD §6) ─────────────────────────────────────────────────
async def measure_difficulty(compiled: Dict[str, Any]) -> Dict[str, Any]:
    """Only ship environments where frontier agents actually fail (PRD §6). Reuse
    ``empirical_difficulty.measure_empirical_difficulty`` on the underlying case."""
    try:
        from ..empirical_difficulty import measure_empirical_difficulty

        case = compiled.get("case") or {}
        question = compiled.get("question") or env_prompt_stub(compiled)
        return await measure_empirical_difficulty(case, question)
    except Exception as exc:
        return {"value": None, "measured": False, "passes_gate": None, "error": str(exc)}


def env_prompt_stub(compiled: Dict[str, Any]) -> str:
    gt = compiled.get("ground_truth") or {}
    return gt.get("answer") or "clinical decision"
