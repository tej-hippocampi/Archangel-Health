"""
Intra-Op operative-note extractor (PRD §6).

The interface is a `typing.Protocol` so any extractor (real LLM, stub,
or future fine-tuned model) can be plugged in. Two implementations
ship with the codebase:

  - `MockIntraopExtractor`  — deterministic per-family payload for dev / CI.
  - `LlmIntraopExtractor`   — production extractor using Anthropic Claude.

`extract()` is async and returns an `ExtractionPayload` containing the
extracted partial form, per-field confidence, raw OCR'd text, model and
prompt versions, and any warnings. Confidence is an integer rating
collapsed to 0.95 / 0.75 / 0.50 per `EXTRACTION.confidence_map`; a
field that wasn't found returns `None` with confidence `0.0`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from triage.intraop.tuning import EXTRACTION
from triage.types import ProcedureFamily


# ─── Public payload type ─────────────────────────────────────────────────────

@dataclass
class ExtractionPayload:
    """Output of any `IntraopExtractor.extract()` implementation."""
    fields: dict[str, Any]
    field_confidences: dict[str, float]
    raw_text: str
    model_version: str
    prompt_version: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class ExtractionContext:
    """Episode-level context passed into the extractor — shapes the prompt
    so the LLM knows which procedure-family-specific fields to look for."""
    patient_id: str
    procedure_family: Optional[ProcedureFamily]
    procedure_name: Optional[str] = None


class IntraopExtractor(Protocol):
    async def extract(
        self,
        *,
        pdf_bytes: bytes,
        context: ExtractionContext,
    ) -> ExtractionPayload:
        ...


# ─── Confidence binning helpers ─────────────────────────────────────────────

def confidence_bin(value: float) -> str:
    """Return 'HIGH' / 'MED' / 'LOW' for a confidence in [0, 1]
    using the thresholds in `EXTRACTION` (PRD §3.2 / §6.4)."""
    if value >= EXTRACTION["mid_confidence_threshold"]:
        return "HIGH"
    if value >= EXTRACTION["low_confidence_threshold"]:
        return "MED"
    return "LOW"


def confidence_for(rating: str) -> float:
    """Map a self-rated 'HIGH' / 'MED' / 'LOW' onto the canonical numeric
    value (PRD §6.2). Unknown ratings return 0.0 (treated as not found)."""
    return EXTRACTION["confidence_map"].get(rating.upper(), 0.0)


# ─── Mock extractor (PRD §6.3) ───────────────────────────────────────────────

class MockIntraopExtractor:
    """Deterministic per-family stub used in dev and CI.

    Each family gets a stable partial payload that exercises a couple of
    LOW / MED / HIGH cases so the UI's confidence-pill rendering can be
    smoke-tested without a real LLM round-trip.
    """

    MODEL_VERSION = "intraop-extractor-mock@1.0.0"
    PROMPT_VERSION = "v1"

    def __init__(self, *, simulate_failure: bool = False):
        self._simulate_failure = simulate_failure

    async def extract(
        self,
        *,
        pdf_bytes: bytes,
        context: ExtractionContext,
    ) -> ExtractionPayload:
        if self._simulate_failure:
            raise RuntimeError("simulated extractor failure")

        family = context.procedure_family
        fields, confidences = self._payload_for(family or "_UNIVERSAL_ONLY_")
        warnings: list[str] = []
        if "ebl" not in fields:
            warnings.append("EBL not found in operative note")
        return ExtractionPayload(
            fields=fields,
            field_confidences=confidences,
            raw_text=f"Simulated operative note for {family} (bytes={len(pdf_bytes)}).",
            model_version=self.MODEL_VERSION,
            prompt_version=self.PROMPT_VERSION,
            warnings=warnings,
        )

    @staticmethod
    def _payload_for(family: str) -> tuple[dict[str, Any], dict[str, float]]:
        """Per-family canned payloads. Tests assert on these stable keys."""
        common = {
            "documented_complication": False,
            "ebl": 250,
            "transfusion_total_units": 0,
            "conversion": "NO",
            "sustained_hypotension": False,
            "vasopressor_requirement": "NONE",
            "significant_arrhythmia": False,
            "or_duration_minutes": 90,
            "difficult_airway": False,
            "net_fluid_balance": 0,
            "anesthesia_type": "GENERAL",
        }
        common_conf = {
            "documented_complication": confidence_for("HIGH"),
            "ebl": confidence_for("HIGH"),
            "transfusion_total_units": confidence_for("HIGH"),
            "conversion": confidence_for("HIGH"),
            "sustained_hypotension": confidence_for("MED"),
            "vasopressor_requirement": confidence_for("MED"),
            "significant_arrhythmia": confidence_for("HIGH"),
            "or_duration_minutes": confidence_for("LOW"),
            "difficult_airway": confidence_for("MED"),
            "net_fluid_balance": confidence_for("LOW"),
            "anesthesia_type": confidence_for("HIGH"),
        }

        if family == "LEJR":
            extra = {
                "lejr_joint": "KNEE", "lejr_side": "LEFT",
                "lejr_fixation_type": "CEMENTLESS",
                "intraoperative_fracture": False,
            }
            extra_conf = {
                "lejr_joint": confidence_for("HIGH"),
                "lejr_side": confidence_for("HIGH"),
                "lejr_fixation_type": confidence_for("MED"),
                "intraoperative_fracture": confidence_for("HIGH"),
            }
        elif family == "CABG":
            extra = {
                "number_of_grafts": 3, "pump_strategy": "ON_PUMP",
                "aortic_cross_clamp_minutes": 75, "cpb_time_minutes": 110,
                "weaning_from_bypass": "YES",
            }
            extra_conf = {
                "number_of_grafts": confidence_for("HIGH"),
                "pump_strategy": confidence_for("HIGH"),
                "aortic_cross_clamp_minutes": confidence_for("MED"),
                "cpb_time_minutes": confidence_for("MED"),
                "weaning_from_bypass": confidence_for("HIGH"),
            }
        elif family == "SPINAL_FUSION":
            extra = {
                "spinal_approach": "POSTERIOR", "number_of_levels_fused": 2,
                "spinal_levels": ["L4-L5", "L5-S1"],
                "spinal_instrumentation": True, "dural_tear": False,
                "neuromonitoring_used": True, "neuromonitoring_changes": False,
            }
            extra_conf = {
                "spinal_approach": confidence_for("HIGH"),
                "number_of_levels_fused": confidence_for("HIGH"),
                "spinal_levels": confidence_for("MED"),
                "spinal_instrumentation": confidence_for("HIGH"),
                "dural_tear": confidence_for("HIGH"),
                "neuromonitoring_used": confidence_for("HIGH"),
                "neuromonitoring_changes": confidence_for("MED"),
            }
        elif family == "HIP_FEMUR_FRACTURE":
            extra = {
                "hip_fracture_pattern": "INTERTROCHANTERIC",
                "hip_fixation_method": "INTRAMEDULLARY_NAIL",
                "time_to_or_hours": 24.0,
                "weight_bearing_status": "PARTIAL",
            }
            extra_conf = {
                "hip_fracture_pattern": confidence_for("HIGH"),
                "hip_fixation_method": confidence_for("HIGH"),
                "time_to_or_hours": confidence_for("LOW"),
                "weight_bearing_status": confidence_for("MED"),
            }
        elif family == "MAJOR_BOWEL":
            extra = {
                "bowel_procedure_type": "PARTIAL_COLECTOMY",
                "bowel_approach": "LAPAROSCOPIC",
                "anastomosis_performed": True,
                "ostomy_created": False,
                "contamination_class": 2,
            }
            extra_conf = {
                "bowel_procedure_type": confidence_for("HIGH"),
                "bowel_approach": confidence_for("HIGH"),
                "anastomosis_performed": confidence_for("HIGH"),
                "ostomy_created": confidence_for("HIGH"),
                "contamination_class": confidence_for("LOW"),
            }
        else:
            extra = {}
            extra_conf = {}

        return {**common, **extra}, {**common_conf, **extra_conf}
