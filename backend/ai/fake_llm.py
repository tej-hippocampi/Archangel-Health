"""Deterministic, schema-valid fake LLM transport (Fake LLM Provider PRD).

Switched on with ``ASCLEPIUS_LLM_PROVIDER=fake``. Every ``call_llm`` /
``call_llm_sync`` returns a canned response instead of reaching a real API, so the
app, the suite and the generation jobs all run in a sandbox that holds no key.

Three properties make this useful rather than merely quiet:

* **Real result shape.** The fake returns the same ``_LLMResult`` the Anthropic and
  OpenAI legs return, so ``first_text``, ``_record``, usage and the prompt-sha are
  byte-identical in structure. A caller cannot tell it is talking to the fake, and
  the AI-calls admin view shows the traffic honestly with ``provider="fake"``.
* **Valid payloads.** Responses are keyed by the call's ``purpose`` (or, where a
  call site declares none, its ``role``) and shaped to pass the SAME parsers and
  validators real output must pass — case generation returns a real gold case, the
  judges return verdicts above the live gate floors, and any tool-use call is
  answered with an object synthesized from that tool's own ``input_schema``.
* **Determinism.** Identical inputs produce byte-identical output, seeded from a
  digest of the call, so tests are stable and diffs are reviewable.

Environment:
  ``ASCLEPIUS_LLM_PROVIDER=fake``  turn the fake transport on
  ``FAKE_LLM_VERDICT=fail``       flip every judge/checker to a REJECT verdict, to
                                  exercise the rejection paths
  ``FAKE_LLM_LATENCY_MS=<int>``   sleep this long per call (default 0), for tests
                                  that exercise timeouts
"""

from __future__ import annotations

import hashlib
import json
import os
import random
from typing import Any, Callable, Optional


class UnknownFakePurpose(KeyError):
    """No fixture is registered for this call's ``purpose``/``role``.

    Raised loudly on purpose (PRD §1.1): an unregistered key means a call site
    reached the LLM without declaring itself, or a new call site landed without a
    fixture. Returning a generic string instead would let that call site silently
    "work" in the sandbox and fail only against a real model.
    """


# ─── Knobs ───────────────────────────────────────────────────────────────────

def verdict_mode() -> str:
    """``"fail"`` flips every judge/checker to reject; anything else passes."""
    return (os.getenv("FAKE_LLM_VERDICT", "") or "").strip().lower()


def _failing() -> bool:
    return verdict_mode() == "fail"


def latency_ms() -> int:
    try:
        return max(0, int(os.getenv("FAKE_LLM_LATENCY_MS", "0") or 0))
    except (TypeError, ValueError):
        return 0


# ─── Determinism ─────────────────────────────────────────────────────────────

def call_digest(role: str, purpose: str, system: str, messages: list) -> str:
    """A stable digest of everything that identifies this call. Same inputs → same
    digest → same response. Mirrors ``llm_client._prompt_sha`` (sha256, 12 hex)."""
    blob = json.dumps(
        {"role": role, "purpose": purpose, "system": system, "messages": messages},
        sort_keys=True, default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def _rng(digest: str) -> random.Random:
    return random.Random(int(digest, 16))


# ─── Gold-case fixtures ──────────────────────────────────────────────────────
# Case/candidate fixtures are built from the committed gold cases rather than
# hand-authored, so they pass the same validators real generation must pass
# (assert_multimodal_content's lab/note/problem/medication floors, the specialty
# study requirement, the case schema). Hand-rolled fixtures drift from those
# floors the moment a floor moves; a gold case cannot.

def _gold_case(digest: str) -> dict:
    """A deterministically chosen, already-validated gold case."""
    from asclepius.gold_cases import all_gold_cases

    cases = all_gold_cases()
    if not cases:  # pragma: no cover — the seed corpus is committed
        return {}
    return cases[int(digest, 16) % len(cases)]


# ─── JSON-Schema-driven tool input synthesis ─────────────────────────────────
# Every tool-use call site (eligibility extraction, the intra-op extractor, HS
# referral enrichment, the community digest, intake chat) ships its own
# ``input_schema``. Synthesizing the answer FROM that schema means the fake stays
# correct as those schemas evolve, and one implementation covers all of them —
# no per-tool fixture to forget to update.

def _synth_from_schema(schema: Any, rng: random.Random, depth: int = 0) -> Any:
    if not isinstance(schema, dict) or depth > 6:
        return None

    if "enum" in schema and isinstance(schema["enum"], list) and schema["enum"]:
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]

    t = schema.get("type")
    if isinstance(t, list):  # ["string", "null"] — take the first non-null
        t = next((x for x in t if x != "null"), "string")

    if t == "object" or ("properties" in schema and t is None):
        props = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        out: dict[str, Any] = {}
        for name, sub in props.items():
            # Fill required fields always; fill optional ones too so downstream
            # code that reads an optional key still sees a well-typed value.
            out[name] = _synth_from_schema(sub, rng, depth + 1)
        for name in required:
            if name not in out:
                out[name] = "FAKE"
        return out

    if t == "array":
        items = schema.get("items")
        n = max(int(schema.get("minItems", 1) or 1), 1)
        if items is None:
            return []
        return [_synth_from_schema(items, rng, depth + 1) for _ in range(min(n, 2))]

    if t == "integer":
        return int(schema.get("minimum", 1) or 1)
    if t == "number":
        return float(schema.get("minimum", 1) or 1)
    if t == "boolean":
        # A checker/judge tool answers "no problems found" unless flipped.
        return not _failing()
    if t == "null":
        return None

    # string (and unknown types)
    fmt = (schema.get("format") or "").lower()
    if fmt == "date":
        return "2026-01-01"
    if fmt == "date-time":
        return "2026-01-01T00:00:00Z"
    return "FAKE"


def _tool_payload(kwargs: dict, rng: random.Random) -> Optional[tuple[str, dict]]:
    """``(tool_name, input)`` when this call forces a tool, else None."""
    tools = kwargs.get("tools") or []
    if not tools:
        return None
    choice = kwargs.get("tool_choice") or {}
    wanted = choice.get("name") if isinstance(choice, dict) else None
    tool = None
    for t in tools:
        if not isinstance(t, dict):
            continue
        if wanted is None or t.get("name") == wanted:
            tool = t
            break
    if tool is None:
        tool = tools[0] if isinstance(tools[0], dict) else None
    if tool is None:
        return None
    schema = tool.get("input_schema") or tool.get("parameters") or {}
    return str(tool.get("name") or wanted or "fake_tool"), (_synth_from_schema(schema, rng) or {})


# ─── Purpose/role fixtures ───────────────────────────────────────────────────
# Each builder takes (ctx) and returns the RAW TEXT the model would have emitted.
# JSON-shaped callers get a JSON string; prose callers get prose.


class _Ctx:
    __slots__ = ("role", "purpose", "system", "messages", "kwargs", "digest", "rng")

    def __init__(self, role, purpose, system, messages, kwargs, digest):
        self.role = role
        self.purpose = purpose
        self.system = system
        self.messages = messages
        self.kwargs = kwargs
        self.digest = digest
        self.rng = _rng(digest)

    def user_text(self) -> str:
        parts = []
        for m in self.messages or []:
            c = m.get("content")
            if isinstance(c, list):
                c = " ".join(b.get("text", "") for b in c if isinstance(b, dict))
            if c:
                parts.append(str(c))
        return "\n".join(parts)


def _j(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, default=str)


# -- generation -------------------------------------------------------------

def _f_case_generation(ctx: _Ctx) -> str:
    gold = _gold_case(ctx.digest)
    return _j({"question": gold.get("question") or "What is the next best step?",
               "case": gold.get("case") or {}})


def _f_candidate_generation(ctx: _Ctx) -> str:
    gold = _gold_case(ctx.digest)
    cands = gold.get("candidate_answers") or []
    if len(cands) < 2:  # pragma: no cover — gold cases always ship a pair
        cands = [{"id": "A", "text": "Fake answer A."}, {"id": "B", "text": "Fake answer B."}]
    return _j({"candidate_answers": [dict(c) for c in cands[:2]],
               "intended_flawed_id": gold.get("intended_flawed_id") or "B"})


def _f_prompt_generation(ctx: _Ctx) -> str:
    gold = _gold_case(ctx.digest)
    q = gold.get("question") or "Describe the next best step in management."
    return _j({"prompts": [q, f"{q} Justify the choice with the labs given."]})


def _f_real_case_question(ctx: _Ctx) -> str:
    # Plain prose, >= 40 chars, and deliberately generic so it can never leak the
    # held-out answer (assert_question_has_no_leakage).
    return ("Based on the chart provided, what is the most likely diagnosis and "
            "what is the next best step in management?")


# -- judges and checkers ----------------------------------------------------
# Passing values sit clear of the live floors: error_likelihood/revision_value
# >= 0.5, hardness >= 0.75, multimodal necessity >= 0.8.

def _f_case_judge(ctx: _Ctx) -> str:
    lo, hi = (0.10, 0.15) if _failing() else (0.90, 0.95)
    return _j({"coherence": hi, "ground_truth_determinable": hi,
               "multimodal_necessity": hi, "reasoning_divergence_potential": lo if _failing() else 0.88,
               "explanation": f"fake case-judge verdict ({verdict_mode() or 'pass'})"})


def _f_hardness_judge(ctx: _Ctx) -> str:
    return _j({"hardness_score": 0.20 if _failing() else 0.85,
               "hardness_axes": ["differential_breadth", "data_integration"],
               "explanation": f"fake hardness verdict ({verdict_mode() or 'pass'})"})


def _f_prompt_judge(ctx: _Ctx) -> str:
    v = 0.10 if _failing() else 0.80
    return _j({"error_likelihood": v, "revision_value": v,
               "on_specialty": not _failing(), "safety_ok": not _failing(),
               "explanation": f"fake prompt-judge verdict ({verdict_mode() or 'pass'})"})


def _f_consistency_check(ctx: _Ctx) -> str:
    return _j({"consistent": not _failing(),
               "issues": ["fake inconsistency"] if _failing() else [],
               "explanation": f"fake consistency verdict ({verdict_mode() or 'pass'})"})


def _f_grounding_check(ctx: _Ctx) -> str:
    return _j({"grounding_ok": not _failing(),
               "issues": ["fake ungrounded claim"] if _failing() else [],
               "explanation": f"fake grounding verdict ({verdict_mode() or 'pass'})"})


def _f_grounding_judge(ctx: _Ctx) -> str:
    # Shape must satisfy pipeline.grounding_check.GroundingReport.
    return _j({"track": "", "coverage": [], "faithfulness": [],
               "critical_failures": ["fake critical failure"] if _failing() else [],
               "verdict": "BLOCK" if _failing() else "PASS",
               "summary": f"fake grounding-judge verdict ({verdict_mode() or 'pass'})"})


def _f_empirical_difficulty_judge(ctx: _Ctx) -> str:
    ok = not _failing()
    # No evidence_span: a span the judge cannot quote verbatim from the graded
    # answer is EXCLUDED by verify_span, which would silently drop the vote.
    return _j({"answer_correct": ok, "reasoning_sound": ok,
               "explanation": f"fake difficulty verdict ({verdict_mode() or 'pass'})"})


def _f_prelabel_suggestion(ctx: _Ctx) -> str:
    # confidence is parsed as a FLOAT in [0,1] (critic.run_prelabel), not the
    # string "low" the PRD sketches — a low-but-valid float is the honest read of
    # "low confidence" here. error_spans stays empty: only spans occurring
    # verbatim in the weaker answer survive filtering, and the fake cannot know
    # that text.
    return _j({"suggested_weaker": "B", "suggested_error_tags": [],
               "suggested_rationale": "Fake pre-label suggestion — verify before use.",
               "error_spans": [], "confidence": 0.2})


# -- reasoning --------------------------------------------------------------

def _f_reasoning_split(ctx: _Ctx) -> str:
    return _j({"steps": ["Fake reasoning step one.",
                         "Fake reasoning step two.",
                         "Fake reasoning step three."]})


def _f_reasoning_pregrade(ctx: _Ctx) -> str:
    label = "bad" if _failing() else "good"
    steps = [{"text": f"Fake reasoning step {i}.", "label": label,
              "critique": "fake critique" if label == "bad" else None}
             for i in (1, 2, 3)]
    return _j({"steps": steps})


# -- baselines / rollout / misc --------------------------------------------

def _f_baseline_answer(ctx: _Ctx) -> str:
    # Keyed off the model so a two-frontier A/B gets two DIFFERENT answers (an
    # identical pair would make every comparison a tie and hide real bugs).
    model = str(ctx.kwargs.get("model") or "unknown-model")
    return (f"Fake baseline answer from {model}. The most likely diagnosis follows "
            f"from the presented labs and history; the next best step is to confirm "
            f"with the indicated test before initiating treatment.")


def _f_citation_rank(ctx: _Ctx) -> str:
    return "[0, 1, 2]"


def _f_dictation_cleanup(ctx: _Ctx) -> str:
    return ctx.user_text().strip() or "Fake cleaned dictation."


def _f_grader_eval(ctx: _Ctx) -> str:
    return _j({"per_criterion": [], "overall": 0.1 if _failing() else 0.9,
               "explanation": f"fake grader eval ({verdict_mode() or 'pass'})"})


def _f_gold_deid(ctx: _Ctx) -> str:
    return _j({"deidentified_text": "Fake de-identified text with [NAME] and [DATE].",
               "placeholders": ["[NAME]", "[DATE]"]})


def _f_prose(ctx: _Ctx) -> str:
    return ("Fake model reply. This text comes from the fake LLM transport "
            "(ASCLEPIUS_LLM_PROVIDER=fake) and is not clinical advice.")


def _f_escalation_classifier(ctx: _Ctx) -> str:
    return _j({"tier": 3 if _failing() else 1, "reason": "fake escalation classification"})


def _f_extraction(ctx: _Ctx) -> str:
    return _j({"chief_complaint": "Fake chief complaint",
               "history_of_present_illness": "Fake HPI for sandbox use.",
               "medications": [], "allergies": [], "problems": [], "vitals": {},
               "labs": [], "assessment": "Fake assessment", "plan": "Fake plan"})


def _f_generation(ctx: _Ctx) -> str:
    return _j({"script": "Fake generated script for sandbox use.",
               "sections": [], "summary": "Fake summary."})


def _f_community_items(ctx: _Ctx) -> str:
    return _j({"items": [], "selected": [], "scores": []})


# Keyed by ``purpose`` first, then by ``role`` for the call sites that declare no
# purpose. Both spaces are closed and enumerable, and both are asserted against
# the live code by test_fake_llm_provider.py's AST scan.
_FIXTURES: dict[str, Callable[[_Ctx], str]] = {
    # ── purposes ──
    "asclepius_baseline_capture": _f_baseline_answer,
    "asclepius_candidate_generation": _f_candidate_generation,
    "asclepius_case_generation": _f_case_generation,
    "asclepius_case_judge": _f_case_judge,
    "asclepius_citation_rank": _f_citation_rank,
    "asclepius_consistency_check": _f_consistency_check,
    "asclepius_dictation_cleanup": _f_dictation_cleanup,
    "asclepius_empirical_difficulty": _f_baseline_answer,
    "asclepius_empirical_difficulty_judge": _f_empirical_difficulty_judge,
    "asclepius_env_rollout": _f_baseline_answer,
    "asclepius_grounding_check": _f_grounding_check,
    "asclepius_hardness_judge": _f_hardness_judge,
    "asclepius_prelabel_suggestion": _f_prelabel_suggestion,
    "asclepius_prompt_generation": _f_prompt_generation,
    "asclepius_prompt_judge": _f_prompt_judge,
    "asclepius_real_case_question": _f_real_case_question,
    "asclepius_reasoning_pregrade": _f_reasoning_pregrade,
    "asclepius_reasoning_split": _f_reasoning_split,
    "community morning content": _f_prose,
    "community morning content (grounded)": _f_prose,
    # THESE KEYS ARE IDENTIFIERS, NOT COPY, and they must match the `purpose=`
    # at the call site (community/digest.py) character for character.
    #
    # They said "digest — compose post" until a sweep that removes em dashes
    # from user-facing writing reached digest.py, changed the purpose there, and
    # could not see that the same string is a dictionary key over here. The call
    # site then looked up a key that no longer existed. Nothing user-visible was
    # wrong; the fixture simply stopped resolving, and test_no_fixture_key_is_dead
    # is what caught it.
    "community news digest: compose post": _f_prose,
    "community news digest: select/score items": _f_community_items,
    "gold_deid": _f_gold_deid,
    "gold_draft": _f_prose,
    "grounding_judge": _f_grounding_judge,
    "hs_referral_enrichment": _f_prose,          # tool-use path answers via schema
    "rubric_grader_eval": _f_grader_eval,
    # ── roles (call sites that declare no purpose) ──
    "avatar_chat": _f_prose,
    "care_companion_chat": _f_prose,
    "community_digest": _f_community_items,
    "eligibility_extract": _f_prose,             # tool-use path answers via schema
    "escalation_classifier": _f_escalation_classifier,
    "extraction": _f_extraction,
    "generation": _f_generation,
    "gold_draft_note": _f_prose,
    "intake_chat": _f_prose,
    "intraop_extract": _f_prose,                 # tool-use path answers via schema
}


def fixture_keys() -> frozenset[str]:
    """Every purpose/role the fake can answer (used by the coverage test)."""
    return frozenset(_FIXTURES)


def _lookup(role: str, purpose: str) -> Callable[[_Ctx], str]:
    for key in ((purpose or "").strip(), (role or "").strip()):
        if key and key in _FIXTURES:
            return _FIXTURES[key]
    raise UnknownFakePurpose(
        f"fake LLM has no fixture for purpose={purpose!r} / role={role!r}. "
        f"A call site reached the LLM without a registered key — add one to "
        f"ai/fake_llm._FIXTURES (and give the call site an explicit purpose=). "
        f"Known keys: {sorted(_FIXTURES)}"
    )


# ─── Public entry point ──────────────────────────────────────────────────────

def build_response(*, role: str, purpose: str, system: str,
                   messages: list, kwargs: dict) -> Any:
    """The fake transport. Returns an Anthropic-shaped ``_LLMResult`` — identical in
    structure to what the real legs return — so telemetry and every caller-side
    accessor behave the same.

    Raises :class:`UnknownFakePurpose` for an unregistered purpose/role."""
    # Imported lazily: llm_client imports this module inside its provider branch,
    # so a module-level import here would be circular.
    from ai.llm_client import _LLMResult

    digest = call_digest(role, purpose, system, messages)
    ctx = _Ctx(role, purpose, system, messages, kwargs, digest)

    tool = _tool_payload(kwargs, ctx.rng)
    if tool is not None:
        name, payload = tool
        text = ""
    else:
        # Resolve the fixture only for non-tool calls: a tool-use call is answered
        # from the tool's own schema and needs no per-purpose fixture.
        text = _lookup(role, purpose)(ctx)
        name, payload = None, None

    # Token counts are honest fakes: proportional to real payload sizes, stable
    # for identical inputs, so usage-based assertions and cost math stay sane.
    in_tokens = max(1, (len(system) + len(json.dumps(messages, default=str))) // 4)
    out_tokens = max(1, len(text if text else json.dumps(payload, default=str)) // 4)
    req_id = f"fake_{digest}"

    result = _LLMResult(text, input_tokens=in_tokens, output_tokens=out_tokens,
                        request_id=req_id)
    if tool is not None:
        result.content = [_FakeToolUseBlock(name, payload)]
    # Callers read these off the raw response (critic checks stop_reason for
    # truncation; eligibility reads resp.id).
    result.stop_reason = "tool_use" if tool is not None else "end_turn"
    result.id = req_id
    return result


class _FakeToolUseBlock:
    """Anthropic ``tool_use`` content block: ``.type``/``.name``/``.input`` — the
    exact three attributes ``_find_tool_use`` reads."""

    type = "tool_use"

    def __init__(self, name: str, payload: dict):
        self.name = name
        self.input = payload or {}
        self.id = "fake_tool_use"
