"""Shared validation for versioned, private chart-walk outcome windows."""
from typing import Any, Dict, Optional
from asclepius import timeline as _timeline

_TIMED_COLLECTIONS = ("lab_panels", "notes", "studies", "medications", "problem_list")


def validate_sealed_outcome(sealed: Any, *, index_offset: Any) -> Optional[Dict[str, Any]]:
    """Fail closed on malformed stored seals; never reconstruct over a bad seal."""
    import json
    error = "the stored outcome seal is invalid; regenerate this point from its source chart"
    if not isinstance(sealed, dict) or set(sealed) != {"version", "index_event_offset", "until_offset", "outcome_encounter_index", "outcome"} \
            or type(sealed.get("version")) is not int or sealed["version"] != 1 \
            or type(index_offset) is not int or type(sealed.get("index_event_offset")) is not int \
            or sealed["index_event_offset"] != index_offset:
        raise ValueError(error)
    until = sealed.get("until_offset")
    delta = sealed.get("outcome")
    if delta is None:
        if "outcome" not in sealed or until is not None or sealed.get("outcome_encounter_index") is not None:
            raise ValueError(error)
        return None
    if type(until) is not int or until <= index_offset or not isinstance(delta, dict) \
            or type(sealed.get("outcome_encounter_index")) is not int or sealed["outcome_encounter_index"] < 0:
        raise ValueError(error)
    expected_keys = {*_TIMED_COLLECTIONS, "vitals", "study_findings_policy", "days_after_decision", "n_events"}
    if set(delta) != expected_keys or type(delta.get("days_after_decision")) is not int \
            or delta["days_after_decision"] != until - index_offset:
        raise ValueError(error)
    items = []
    for key in _TIMED_COLLECTIONS:
        if not isinstance(delta[key], list):
            raise ValueError(error)
        items.extend(delta[key])
    if not isinstance(delta["vitals"], dict):
        raise ValueError(error)
    if type(delta["n_events"]) is not int or delta["n_events"] != len(items):
        raise ValueError(error)
    if delta["vitals"]:
        items.append(delta["vitals"])
    for item in items:
        if not isinstance(item, dict) or type(item.get("collected_offset_days")) is not int \
                or not 0 < item["collected_offset_days"] <= until - index_offset:
            raise ValueError(error)
    if _timeline.datelike_leftovers_in_text(json.dumps(delta)):
        raise ValueError("calendar date in the stored outcome seal; regenerate from a de-identified chart")
    return delta

