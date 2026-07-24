"""ClinicalEnv — the runnable Gymnasium-standard interface (PRD §4.5).

This is what makes V5 a *gym*, not a dataset: a lab plugs their own agent into
``reset``/``step``/``verify`` and trains against it live. Our ``rollout.py`` is
just one consumer of the same interface.

Gymnasium contract (PRD §4.5, acceptance §1b):
  * ``reset(seed=None) -> (observation, info)`` — reproducible on a seed; fully
    clears mutable episode state (the most common env bug).
  * ``step(action) -> (observation, reward, terminated, truncated, info)``.
  * ``verify() -> reward`` — trajectory-level reward, post-episode.
  * action/observation spaces are declared (the §4 tool schemas ARE the action
    space) and validated on load.
  * vector/parallel rollouts via ``VectorClinicalEnv`` (PRD §4.5).

Pure-Python, no gymnasium dependency required (we mirror the API so it drops into
a lab's loop; ``as_gym()`` wraps it in a real ``gymnasium.Env`` if installed).
"""

from __future__ import annotations

import random
from typing import Any, Dict, List, Optional, Tuple

from ..constants import env_max_steps
from .state import EHRState
from .tools import ToolRegistry, is_terminal_tool


class ClinicalEnv:
    """One episode over one compiled environment (PRD §8.4 output).

    ``action`` shapes accepted by ``step`` (all provider-neutral):
      * ``{"type": "thought", "content": "..."}`` — records a reasoning step.
      * ``{"type": "tool_call", "tool": name, "input": {...}}`` — a read/act tool.
      * ``{"tool": name, "input": {...}}`` — shorthand tool_call.
    """

    def __init__(self, compiled: Dict[str, Any], *, max_steps: Optional[int] = None):
        self.compiled = compiled
        self.max_steps = int(max_steps) if max_steps else env_max_steps()
        self.task_type = compiled.get("task_template") or "diagnostic_workup"
        self.specialty = compiled.get("specialty") or "general"
        self.allowed_tools: List[str] = list(compiled.get("allowed_tools") or [])
        # Episode state (populated by reset()).
        self._state: Optional[EHRState] = None
        self._registry: Optional[ToolRegistry] = None
        self._seed: Optional[int] = None
        self._rng = random.Random()
        self.trajectory: List[Dict[str, Any]] = []
        self.step_rewards: List[float] = []
        self.emitted: List[Dict[str, Any]] = []  # {tool, input, fhir, valid} per action/final
        self._step_no = 0
        self._terminated = False
        self._truncated = False
        self._final_action: Optional[Dict[str, Any]] = None
        # Validate the action space on load (Gymnasium best practice, PRD §4.5).
        self._validate_action_space()

    # ─── Space declarations (PRD §4.5) ────────────────────────────────────────
    def action_space(self) -> List[Dict[str, Any]]:
        """The declared action space = the task's tool schemas (PRD §4/§4.5)."""
        from .tools import tool_schemas

        return tool_schemas(self.allowed_tools)

    def observation_space(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "description": "A chart slice / tool observation. Free-form JSON keyed by field.",
        }

    def _validate_action_space(self) -> None:
        from .tools import all_tool_names

        known = set(all_tool_names())
        bad = [t for t in self.allowed_tools if t not in known]
        if bad:
            raise ValueError(f"compiled env declares unknown tools: {bad}")

    # ─── reset (PRD §4.5) ──────────────────────────────────────────────────────
    def reset(self, seed: Optional[int] = None) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Load the task+case, hide withheld fields, return the opening
        observation. Fully clears mutable episode state so a seeded replay is
        deterministic (PRD §4.5 — 'reset() must fully clear mutable episode state')."""
        self._seed = seed
        if seed is not None:
            self._rng.seed(seed)
        dp = (self.compiled.get("decision_point") or {}).get("offset_days")
        observable = (self.compiled.get("observable_state") or {}).get("panels")
        self._state = EHRState(
            self.compiled.get("case") or {},
            decision_offset_days=dp,
            observable_panels=observable,
            deid_recheck=bool(self.compiled.get("deid_recheck_required")),
        )
        self._registry = ToolRegistry(self.allowed_tools, self._state)
        # Reset ALL mutable episode state.
        self.trajectory = []
        self.step_rewards = []
        self.emitted = []
        self._step_no = 0
        self._terminated = False
        self._truncated = False
        self._final_action = None

        opening = self._state.observation_at_reset()
        observation = {"prompt": self.prompt(), "chart": opening}
        info = {
            "task_type": self.task_type,
            "specialty": self.specialty,
            "allowed_tools": self.allowed_tools,
            "action_space": self.action_space(),
            "seed": seed,
            "max_steps": self.max_steps,
        }
        return observation, info

    def prompt(self) -> str:
        from . import catalog

        return catalog.build_prompt(
            self.compiled.get("case") or {},
            self.compiled.get("question") or "",
            self.task_type,
        )

    # ─── step (PRD §4.5) ───────────────────────────────────────────────────────
    def step(self, action: Dict[str, Any]) -> Tuple[Dict[str, Any], float, bool, bool, Dict[str, Any]]:
        if self._state is None:
            raise RuntimeError("step() called before reset()")
        if self._terminated or self._truncated:
            # Episode already over — return a no-op per Gymnasium convention.
            return {}, 0.0, self._terminated, self._truncated, {"note": "episode already ended"}

        action = dict(action or {})
        atype = action.get("type") or ("tool_call" if action.get("tool") else "thought")
        reward = 0.0
        info: Dict[str, Any] = {}

        if atype == "thought":
            self._step_no += 1
            self.trajectory.append({"step": self._step_no, "type": "thought",
                                    "content": str(action.get("content") or "")})
            observation: Dict[str, Any] = {}
        else:
            tool = action.get("tool")
            tool_input = action.get("input") or {}
            # Log the tool_call step.
            self._step_no += 1
            self.trajectory.append({"step": self._step_no, "type": "tool_call",
                                    "tool": tool, "input": tool_input})
            result = self._registry.execute(tool, tool_input)
            info = {"kind": result.get("kind")}
            if result.get("kind") in ("action", "final"):
                info["fhir"] = result.get("fhir")
                info["fhir_valid"] = result.get("valid")
                info["echo"] = result.get("echo")
                self.emitted.append({"tool": tool, "input": tool_input,
                                     "fhir": result.get("fhir"), "valid": bool(result.get("valid"))})
                # small dense shaping: valid FHIR action → +, invalid → −
                reward = 0.05 if result.get("valid") else -0.05
            # Log the observation step.
            self._step_no += 1
            obs_payload = result.get("observation")
            self.trajectory.append({"step": self._step_no, "type": "observation",
                                    "content": _as_obs_text(obs_payload)})
            observation = {"observation": obs_payload}

            if is_terminal_tool(tool):
                # Final decision → log a final_output step + terminate.
                self._step_no += 1
                self._final_action = {"tool": tool, "input": tool_input, "fhir": result.get("fhir")}
                self.trajectory.append({"step": self._step_no, "type": "final_output",
                                        "content": _as_obs_text(result.get("echo") or tool_input)})
                self._terminated = True

        # Max-step cap → truncate (PRD §4.5).
        if not self._terminated and self._step_no >= self.max_steps:
            self._truncated = True

        self.step_rewards.append(reward)
        info.update({"step_no": self._step_no, "terminated": self._terminated, "truncated": self._truncated})
        return observation, reward, self._terminated, self._truncated, info

    # ─── verify (PRD §4.5 / §5) ────────────────────────────────────────────────
    def verify(self, *, rubric: bool = False) -> Dict[str, Any]:
        """Post-episode trajectory-level reward (PRD §5). Returns the full
        ``verification`` block; ``verification['reward']`` is the sparse scalar.
        Set ``rubric=True`` to run the async rubric layer (see ``verify.score``)."""
        from .verify import score

        return score(self, run_rubric=rubric)

    # ─── Accessors used by verify / rollout ────────────────────────────────────
    @property
    def state(self) -> Optional[EHRState]:
        return self._state

    def final_action(self) -> Optional[Dict[str, Any]]:
        return self._final_action

    def ground_truth(self) -> Dict[str, Any]:
        return self.compiled.get("ground_truth") or {}

    def checks(self) -> List[Dict[str, Any]]:
        return [dict(c) for c in (self.compiled.get("checks") or [])]

    def as_gym(self):
        """Wrap in a real ``gymnasium.Env`` if gymnasium is installed (optional —
        we mirror the API so this is not required to run)."""
        try:
            import gymnasium as gym
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("gymnasium is not installed; ClinicalEnv already mirrors the API") from exc

        env_self = self

        class _GymWrap(gym.Env):
            def reset(self, *, seed=None, options=None):
                return env_self.reset(seed=seed)

            def step(self, action):
                return env_self.step(action)

        return _GymWrap()


def _as_obs_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    try:
        import json

        return json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        return str(payload)


class VectorClinicalEnv:
    """Run many copies for throughput (PRD §4.5 vector/parallel rollouts). A thin
    synchronous vectorizer — a lab wanting thousands of episodes maps over these."""

    def __init__(self, compiled_list: List[Dict[str, Any]], *, max_steps: Optional[int] = None):
        self.envs = [ClinicalEnv(c, max_steps=max_steps) for c in compiled_list]

    def reset(self, seeds: Optional[List[int]] = None):
        seeds = seeds or [None] * len(self.envs)
        return [e.reset(seed=s) for e, s in zip(self.envs, seeds)]

    def step(self, actions: List[Dict[str, Any]]):
        return [e.step(a) for e, a in zip(self.envs, actions)]

    def verify(self):
        return [e.verify() for e in self.envs]
