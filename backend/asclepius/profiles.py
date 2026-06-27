"""Buyer export profiles + field-mapping + a minimal JSON-Schema validator
(opt §2).

The first buyer's eval format *is* the spec, so export is a field-mapping layer,
never a hardcoded writer. A profile (``buyer_profiles/<name>.json``) declares:
  * ``preference_variant``  flat | chat (hh-rlhf)
  * ``record_types``        which canonical types to emit
  * ``field_maps``          per-type {our_canonical_field: their_field}
  * ``schemas``             per-type JSON Schema validated BEFORE writing

Adding a buyer is a ~10-line JSON file, not a code change. The JSON-Schema
validator is a dependency-free subset (type / required / properties / enum /
items) sufficient to guarantee every emitted line matches the target shape; any
invalid line fails the whole batch loudly (no partial silent exports).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

PROFILES_DIR = Path(__file__).resolve().parent / "buyer_profiles"

_CACHE: Dict[str, Dict[str, Any]] = {}


class ProfileError(ValueError):
    """Raised when a profile is missing or malformed."""


def profiles_dir() -> Path:
    override = os.getenv("ASCLEPIUS_PROFILES_DIR")
    return Path(override).resolve() if override else PROFILES_DIR


def list_profiles() -> List[str]:
    d = profiles_dir()
    if not d.is_dir():
        return []
    return sorted(
        p.stem for p in d.glob("*.json") if p.stem.lower() != "template"
    )


def load_profile(name: str) -> Dict[str, Any]:
    name = (name or "default").strip() or "default"
    if name in _CACHE:
        return _CACHE[name]
    path = profiles_dir() / f"{name}.json"
    if not path.exists():
        raise ProfileError(f"Unknown buyer profile: {name!r}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProfileError(f"Profile {name!r} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or "field_maps" not in data:
        raise ProfileError(f"Profile {name!r} is missing 'field_maps'")
    _CACHE[name] = data
    return data


def clear_cache() -> None:  # test helper
    _CACHE.clear()


# ─── Field mapping ────────────────────────────────────────────────────────────
def _variant(profile: Dict[str, Any]) -> str:
    v = (profile.get("preference_variant") or "flat").strip()
    return v if v in ("flat", "chat") else "flat"


def field_map_for(profile: Dict[str, Any], rtype: str) -> Optional[Dict[str, str]]:
    fm = (profile.get("field_maps") or {}).get(rtype)
    if fm is None:
        return None
    if rtype == "preference":
        return fm.get(_variant(profile))
    return fm


def schema_for(profile: Dict[str, Any], rtype: str) -> Optional[Dict[str, Any]]:
    sc = (profile.get("schemas") or {}).get(rtype)
    if sc is None:
        return None
    if rtype == "preference":
        return sc.get(_variant(profile))
    return sc


def map_record(profile: Dict[str, Any], record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Map a canonical record payload to the buyer's field names, or None when
    this record type is not emitted by the profile."""
    rtype = record.get("type")
    if rtype not in (profile.get("record_types") or []):
        return None
    fmap = field_map_for(profile, rtype)
    if not fmap:
        return None
    out: Dict[str, Any] = {}
    for our_field, their_field in fmap.items():
        if our_field in record:
            out[their_field] = record[our_field]
    return out


# ─── Minimal JSON-Schema validator (dependency-free subset) ───────────────────
_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def _check_type(value: Any, type_spec: Any) -> bool:
    if isinstance(type_spec, list):
        return any(_check_type(value, t) for t in type_spec)
    check = _TYPE_CHECKS.get(type_spec)
    return check(value) if check else True


def validate_against_schema(obj: Any, schema: Dict[str, Any], path: str = "$") -> List[str]:
    """Return a list of human-readable validation errors (empty == valid)."""
    errors: List[str] = []
    if not schema:
        return errors

    if "type" in schema and not _check_type(obj, schema["type"]):
        errors.append(f"{path}: expected type {schema['type']}, got {type(obj).__name__}")
        return errors  # type mismatch — deeper checks would be noise

    if "enum" in schema and obj not in schema["enum"]:
        errors.append(f"{path}: {obj!r} not in enum {schema['enum']}")

    if isinstance(obj, dict):
        for req in schema.get("required", []) or []:
            if req not in obj:
                errors.append(f"{path}: missing required field '{req}'")
        props = schema.get("properties") or {}
        for key, subschema in props.items():
            if key in obj:
                errors.extend(validate_against_schema(obj[key], subschema, f"{path}.{key}"))
        if schema.get("additionalProperties") is False:
            extra = set(obj) - set(props)
            if extra:
                errors.append(f"{path}: unexpected fields {sorted(extra)}")

    if isinstance(obj, list) and schema.get("items"):
        for i, item in enumerate(obj):
            errors.extend(validate_against_schema(item, schema["items"], f"{path}[{i}]"))

    return errors
