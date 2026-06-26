"""Buyer export profiles — the format each lab needs.

A profile maps our canonical record fields (packaging.py) onto a buyer's
expected schema. The first buyer's eval format defines "optimal"; adding a new
buyer is a small dict here (or a stored profile in the DB), never a code change
to the writer.

A profile:
  {
    "name": "anthropic_hh",
    "description": "...",
    "record_types": ["preference"],         # which canonical types to include
    "field_map": {                            # per type: out_field -> canonical_field
        "preference": {"chosen": "chosen", "rejected": "rejected"}
    }
  }

If field_map has no entry for an included type, that type is emitted verbatim
(canonical passthrough).
"""

from __future__ import annotations

from typing import Any

BUILTIN_PROFILES: dict[str, dict[str, Any]] = {
    "default": {
        "name": "default",
        "description": "Canonical Asclepius records, all types, all fields (our schema).",
        "record_types": ["preference", "ideal_answer", "reasoning_trace"],
        "field_map": {},  # empty => passthrough
    },
    "anthropic_hh": {
        "name": "anthropic_hh",
        "description": "Anthropic hh-rlhf style flat preference pairs: {chosen, rejected}.",
        "record_types": ["preference"],
        "field_map": {
            "preference": {
                "prompt": "prompt",
                "chosen": "chosen",
                "rejected": "rejected",
            }
        },
    },
    "openai_preference": {
        "name": "openai_preference",
        "description": "OpenAI-style preference: {input, preferred_output, non_preferred_output, metadata}.",
        "record_types": ["preference"],
        "field_map": {
            "preference": {
                "input": "prompt",
                "preferred_output": "chosen",
                "non_preferred_output": "rejected",
                "metadata.specialty": "context.specialty",
                "metadata.difficulty": "context.difficulty",
                "metadata.credential": "annotator_credential",
            }
        },
    },
    "sft_jsonl": {
        "name": "sft_jsonl",
        "description": "Instruction-tuning SFT pairs from ideal answers: {prompt, completion}.",
        "record_types": ["ideal_answer"],
        "field_map": {
            "ideal_answer": {
                "prompt": "prompt",
                "completion": "ideal_answer",
            }
        },
    },
    "reasoning_prm": {
        "name": "reasoning_prm",
        "description": "Process-reward step traces: {prompt, steps, final_answer}.",
        "record_types": ["reasoning_trace"],
        "field_map": {
            "reasoning_trace": {
                "prompt": "prompt",
                "steps": "steps",
                "final_answer": "final_answer",
            }
        },
    },
}


def list_profiles() -> list[dict[str, Any]]:
    return list(BUILTIN_PROFILES.values())


def get_profile(name: str) -> dict[str, Any]:
    return BUILTIN_PROFILES.get(name) or BUILTIN_PROFILES["default"]


def _dig(obj: dict[str, Any], dotted: str) -> Any:
    cur: Any = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _set(obj: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur = obj
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def apply_profile(record: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    """Map one canonical record into the buyer's shape per the profile."""
    rtype = record.get("type")
    field_map = (profile.get("field_map") or {}).get(rtype)
    if not field_map:
        return record  # passthrough
    out: dict[str, Any] = {}
    for out_field, src_field in field_map.items():
        _set(out, out_field, _dig(record, src_field))
    return out
