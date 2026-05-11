"""
Extract PAM-13 proxy responses from intake form data.

Pure function over a `form_data` dict — accepts several shapes the
upstream intake parser may emit:

  1. Flat keys named `pam_1`..`pam_13` whose values are integers 1..4
     (or the string "N_A" for "does not apply").
  2. A nested `pam` block: `{ "pam": { "1": 4, "2": 3, ... } }` or a
     `{ "pam": { "responses": [ {"item_index": 1, "value": 4}, ...] } }`
     pre-shaped block.
  3. A nested `section_3_5` (intake interview section 3.5) block holding
     the same flat or nested layout.
  4. Triage Suite Pass 3 — `section10_dayOfSurgeryReadiness.pam_<i>`
     fields whose values are `{value: "4", source: "interview"}` dicts
     (the canonical schema shape produced by the intake AI-patch path).

Unknown / out-of-range / unparsable values are silently dropped so
partial submissions still produce the best-effort `PamResult` the
algorithm expects (PRD §4.2: items_scored < 10 → is_complete=False).
"""

from __future__ import annotations

from typing import Any

from triage.preop_retier.types import PamResponse, PamValue


_VALID_INTS: set[int] = {1, 2, 3, 4}


def _coerce_value(raw: Any) -> PamValue | None:
    """Coerce a raw form value into a PamValue (1..4 or 'N_A').

    Also unwraps the canonical intake-schema `{value: ..., source: ...}`
    shape — Triage Suite Pass 3 §2.3.
    """
    if isinstance(raw, dict) and "value" in raw:
        raw = raw.get("value")
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        if s.upper() in {"N_A", "NA", "N/A"}:
            return "N_A"
        try:
            raw = int(s)
        except (TypeError, ValueError):
            try:
                raw = int(float(s))
            except (TypeError, ValueError):
                return None
    if isinstance(raw, bool):  # bool is int subclass — reject explicitly
        return None
    if isinstance(raw, int) and raw in _VALID_INTS:
        return raw  # type: ignore[return-value]
    return None


def _extract_from_flat(d: dict[str, Any]) -> list[PamResponse]:
    out: list[PamResponse] = []
    for i in range(1, 14):
        for key in (f"pam_{i}", f"pam{i}", f"PAM_{i}", f"section_3_5_pam_{i}"):
            if key in d:
                v = _coerce_value(d[key])
                if v is not None:
                    out.append(PamResponse(item_index=i, value=v))
                break
    return out


def _extract_from_nested(block: Any) -> list[PamResponse]:
    """Accept a block that's a dict-of-1..13-keys or has a `responses` list."""
    if not isinstance(block, dict):
        return []

    # Pre-shaped: { responses: [{ item_index, value }, ...] }
    if isinstance(block.get("responses"), list):
        out: list[PamResponse] = []
        for r in block["responses"]:
            if not isinstance(r, dict):
                continue
            try:
                idx = int(r.get("item_index"))
            except (TypeError, ValueError):
                continue
            if idx < 1 or idx > 13:
                continue
            v = _coerce_value(r.get("value"))
            if v is not None:
                out.append(PamResponse(item_index=idx, value=v))
        return out

    # Plain dict of integer-keyed (or string-integer-keyed) items.
    out2: list[PamResponse] = []
    for k, raw in block.items():
        try:
            idx = int(k)
        except (TypeError, ValueError):
            continue
        if idx < 1 or idx > 13:
            continue
        v = _coerce_value(raw)
        if v is not None:
            out2.append(PamResponse(item_index=idx, value=v))
    return out2


def extract_pam_responses(form_data: dict[str, Any]) -> list[PamResponse]:
    """Return up to 13 unique PAM responses keyed by item_index.

    Resolution order (first-hit wins on collisions):
      1. Pre-shaped nested `pam` / `section_3_5_pam` blocks.
      2. Section 10 (`section10_dayOfSurgeryReadiness.pam_*`) — Triage
         Suite Pass 3.
      3. Top-level flat keys (`pam_1` .. `pam_13`) — legacy / direct
         callers.
    """
    if not isinstance(form_data, dict):
        return []

    pam_block = (
        form_data.get("pam")
        or (form_data.get("section_3_5") or {}).get("pam") if isinstance(form_data.get("section_3_5"), dict) else None
        or form_data.get("section_3_5_pam")
    )
    nested = _extract_from_nested(pam_block) if pam_block is not None else []

    # Pass 3 — pull from canonical Section 10 schema shape if present.
    section10 = form_data.get("section10_dayOfSurgeryReadiness")
    section10_flat = _extract_from_flat(section10) if isinstance(section10, dict) else []

    flat = _extract_from_flat(form_data)

    by_idx: dict[int, PamResponse] = {r.item_index: r for r in flat}
    for r in section10_flat:
        by_idx[r.item_index] = r
    for r in nested:
        by_idx[r.item_index] = r
    return sorted(by_idx.values(), key=lambda r: r.item_index)
