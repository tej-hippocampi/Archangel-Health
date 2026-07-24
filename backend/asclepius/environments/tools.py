"""The tool layer (PRD §4) — a fixed, FHIR-shaped tool registry.

READ tools return withheld ``ClinicalCase`` fields on demand (the "information
must be earned" mechanic). ACTION tools construct valid FHIR R4 resources
(``ServiceRequest`` / ``MedicationRequest``) recorded in the trajectory so the
verifier can check payload validity + FHIR compliance (MedAgentBench's
action-grading method, PRD §4).

``fhir_r4.py`` is import-only (no builders), so the minimal FHIR resource
builders live here. They emit well-formed R4 resource dicts — enough for the
verifier's validity + coding checks, not a full server payload.

The registry is config-driven per task template (PRD §4): a task exposes only its
relevant tools, so adding a task type is config (``catalog.py``), not new code.
Tool schemas double as the Gymnasium *action space* declaration (PRD §4.5).
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from .state import EHRState

# ─── Minimal FHIR R4 resource builders (action tools) ─────────────────────────


def build_service_request(*, code: str, category: str = "procedure",
                          display: Optional[str] = None) -> Dict[str, Any]:
    """A well-formed R4 ``ServiceRequest`` (an order for a test / referral)."""
    return {
        "resourceType": "ServiceRequest",
        "status": "active",
        "intent": "order",
        "category": [{"text": category}],
        "code": {"text": display or code, "coding": [{"code": str(code), "display": display or str(code)}]},
        "subject": {"reference": "Patient/env"},
    }


def build_medication_request(*, drug: str, dose: Optional[str] = None,
                            route: Optional[str] = None, freq: Optional[str] = None) -> Dict[str, Any]:
    """A well-formed R4 ``MedicationRequest``."""
    dosage: Dict[str, Any] = {}
    if dose:
        dosage["text"] = " ".join(x for x in [dose, route, freq] if x)
    if route:
        dosage["route"] = {"text": route}
    return {
        "resourceType": "MedicationRequest",
        "status": "active",
        "intent": "order",
        "medicationCodeableConcept": {"text": drug, "coding": [{"display": drug}]},
        "subject": {"reference": "Patient/env"},
        "dosageInstruction": [dosage] if dosage else [],
    }


def fhir_resource_valid(resource: Dict[str, Any]) -> bool:
    """Deterministic FHIR validity check the verifier uses (PRD §5.1 action
    validity). Well-formed = has a resourceType and the R4-required fields for
    the two resource types we emit."""
    if not isinstance(resource, dict):
        return False
    rt = resource.get("resourceType")
    if rt == "ServiceRequest":
        return bool(resource.get("status") and resource.get("intent") and resource.get("code"))
    if rt == "MedicationRequest":
        return bool(resource.get("status") and resource.get("intent")
                    and resource.get("medicationCodeableConcept"))
    return False


# ─── Tool definitions ─────────────────────────────────────────────────────────
# Each tool: name → (kind, json_schema, handler). ``kind`` ∈ {read, action}.
# Read handlers take (state, **input) → observation payload.
# Action handlers take (**input) → (fhir_resource_or_payload, echo) — recorded
# in the trajectory ``info``; a final_output action also terminates the episode.

READ = "read"
ACTION = "action"
FINAL = "final"  # an action that submits the final decision (terminates)


def _schema(props: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or []}


# Read-tool handlers ----------------------------------------------------------
def _get_problem_list(state: EHRState, **_):
    return state.get_problem_list()


def _get_labs(state: EHRState, panel: Optional[str] = None, **_):
    return state.get_labs(panel)


def _get_vitals(state: EHRState, **_):
    return state.get_vitals()


def _get_medications(state: EHRState, **_):
    return state.get_medications()


def _get_notes(state: EHRState, type: Optional[str] = None, note_type: Optional[str] = None, **_):
    return state.get_notes(type or note_type)


def _get_studies(state: EHRState, modality: Optional[str] = None, **_):
    return state.get_studies(modality)


def _get_timeline(state: EHRState, window: Optional[int] = None, **_):
    return state.get_timeline(window)


# Action-tool handlers (construct FHIR resources / submit decisions) ----------
def _order_test(code: str = "", **_):
    res = build_service_request(code=code, category="laboratory")
    return res, {"tool": "order_test", "code": code}


def _order_medication(drug: str = "", dose: Optional[str] = None, route: Optional[str] = None,
                      freq: Optional[str] = None, **_):
    res = build_medication_request(drug=drug, dose=dose, route=route, freq=freq)
    return res, {"tool": "order_medication", "drug": drug, "dose": dose, "route": route, "freq": freq}


def _place_referral(specialty: str = "", **_):
    res = build_service_request(code=f"referral-{specialty}", category="referral",
                                display=f"Referral to {specialty}")
    return res, {"tool": "place_referral", "specialty": specialty}


def _submit_diagnosis(text: str = "", icd: Optional[str] = None, **_):
    return {"resourceType": "Condition", "code": {"text": text, "coding": ([{"code": icd}] if icd else [])},
            "verificationStatus": {"text": "provisional"}}, {"tool": "submit_diagnosis", "text": text, "icd": icd}


def _submit_plan(text: str = "", **_):
    return {"resourceType": "CarePlan", "status": "active", "intent": "plan",
            "description": text}, {"tool": "submit_plan", "text": text}


def _escalate(reason: str = "", **_):
    return {"resourceType": "Flag", "status": "active", "code": {"text": "escalation"},
            "period": {}, "author": {"display": "agent"}, "text": reason}, {"tool": "escalate", "reason": reason}


# name → (kind, schema, handler)
_TOOL_TABLE: Dict[str, Tuple[str, Dict[str, Any], Callable]] = {
    # READ (earn context)
    "get_problem_list": (READ, _schema({}), _get_problem_list),
    "get_labs": (READ, _schema({"panel": {"type": "string", "description": "panel name, e.g. renal / BMP"}}), _get_labs),
    "get_vitals": (READ, _schema({}), _get_vitals),
    "get_medications": (READ, _schema({}), _get_medications),
    "get_notes": (READ, _schema({"type": {"type": "string", "description": "note type, e.g. Consult / Progress"}}), _get_notes),
    "get_studies": (READ, _schema({"modality": {"type": "string", "description": "ecg|echo|imaging|path|molecular"}}), _get_studies),
    "get_timeline": (READ, _schema({"window": {"type": "integer", "description": "days before the decision point"}}), _get_timeline),
    # ACTION (emit a FHIR resource)
    "order_test": (ACTION, _schema({"code": {"type": "string"}}, ["code"]), _order_test),
    "order_medication": (ACTION, _schema({"drug": {"type": "string"}, "dose": {"type": "string"},
                                          "route": {"type": "string"}, "freq": {"type": "string"}}, ["drug"]), _order_medication),
    "place_referral": (ACTION, _schema({"specialty": {"type": "string"}}, ["specialty"]), _place_referral),
    # FINAL (submit the decision — terminates the episode)
    "submit_diagnosis": (FINAL, _schema({"text": {"type": "string"}, "icd": {"type": "string"}}, ["text"]), _submit_diagnosis),
    "submit_plan": (FINAL, _schema({"text": {"type": "string"}}, ["text"]), _submit_plan),
    "escalate": (FINAL, _schema({"reason": {"type": "string"}}, ["reason"]), _escalate),
}

# Tool description strings (surfaced to the model — PRD §4).
_TOOL_DESC: Dict[str, str] = {
    "get_problem_list": "Return the patient's problem list.",
    "get_labs": "Return a lab panel by name (omit 'panel' to list available panels).",
    "get_vitals": "Return the current vital signs.",
    "get_medications": "Return the active medication list.",
    "get_notes": "Return clinical notes (optionally filtered by type).",
    "get_studies": "Return studies (ECG/echo/imaging/path) by modality.",
    "get_timeline": "Return prior labs bucketed by day-offset (longitudinal).",
    "order_test": "Order a diagnostic test — emits a FHIR ServiceRequest.",
    "order_medication": "Order/adjust a medication — emits a FHIR MedicationRequest.",
    "place_referral": "Place a specialty referral — emits a FHIR ServiceRequest.",
    "submit_diagnosis": "Submit the final diagnosis (ends the episode).",
    "submit_plan": "Submit the management plan (ends the episode).",
    "escalate": "Escalate / act on a danger, or refuse an unsafe action (ends the episode).",
}


def tool_kind(name: str) -> Optional[str]:
    entry = _TOOL_TABLE.get(name)
    return entry[0] if entry else None


def is_terminal_tool(name: str) -> bool:
    return tool_kind(name) == FINAL


def all_tool_names() -> List[str]:
    return list(_TOOL_TABLE.keys())


def tool_schemas(names: List[str]) -> List[Dict[str, Any]]:
    """Function/tool schemas (the Gymnasium *action space*, PRD §4.5). Anthropic
    tool-use shape (``input_schema``); trivially mapped to OpenAI's ``parameters``."""
    out = []
    for n in names:
        entry = _TOOL_TABLE.get(n)
        if not entry:
            continue
        out.append({"name": n, "description": _TOOL_DESC.get(n, ""), "input_schema": entry[1]})
    return out


class ToolRegistry:
    """The per-task tool registry (config-driven, PRD §4). Exposes only the
    template's allowed tools; executes read tools against ``EHRState`` and action
    tools into FHIR resources."""

    def __init__(self, allowed: List[str], state: EHRState):
        self.allowed = [t for t in allowed if t in _TOOL_TABLE]
        self.state = state

    def schemas(self) -> List[Dict[str, Any]]:
        return tool_schemas(self.allowed)

    def has(self, name: str) -> bool:
        return name in self.allowed

    def execute(self, name: str, tool_input: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Execute one tool. Returns a dict:
          read → {"kind":"read","observation": <payload>}
          action/final → {"kind": kind, "fhir": <resource>, "valid": bool,
                          "echo": {...}, "observation": <str>}
        Never raises on bad input — an unknown/unavailable tool returns an error
        observation the agent must handle."""
        tool_input = dict(tool_input or {})
        if name not in self.allowed:
            return {"kind": "error", "observation": f"tool '{name}' is not available for this task"}
        kind, _schema_def, handler = _TOOL_TABLE[name]
        try:
            if kind == READ:
                payload = handler(self.state, **tool_input)
                return {"kind": READ, "observation": payload}
            resource, echo = handler(**tool_input)
            valid = fhir_resource_valid(resource) if resource.get("resourceType") in (
                "ServiceRequest", "MedicationRequest") else True
            return {"kind": kind, "fhir": resource, "valid": valid, "echo": echo,
                    "observation": f"recorded {name}: {echo}"}
        except TypeError as exc:
            return {"kind": "error", "observation": f"bad input to '{name}': {exc}"}
