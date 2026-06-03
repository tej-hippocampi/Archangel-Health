from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Optional

from ai.model_config import resolve
from pipeline.extract import EXTRACTION_SYSTEM, ExtractionLayer
from pipeline.gated_synthesis import synthesize_script
from pipeline.generate import GenerationLayer
from pipeline.grounding_gate import apply_grounding_to_patient
from prompts.registry import prompt_meta

try:
    from pipeline.grounding_check import GROUNDING_PROMPT_V, build_required_items, compute_accuracy

    _GROUNDING_READY = True
except Exception:
    _GROUNDING_READY = False
    GROUNDING_PROMPT_V = "unavailable"


@dataclass
class StreamingPipelineContext:
    patient_store: dict[str, Any]
    team_store: Any
    persist_demo: Callable[[], None]
    base_url: str


def _ev(stage: str, status: str = "ok", **data: Any) -> dict[str, Any]:
    return {"stage": stage, "status": status, "ts": round(time.time(), 3), **data}


def _words(text: str) -> int:
    return len((text or "").split())


def _safe_summary(structured_data: dict[str, Any]) -> dict[str, Any]:
    meds = structured_data.get("medications") or []
    red_flags = structured_data.get("red_flags") or []
    changed = 0
    for med in meds:
        status = (med.get("status") or "").lower()
        if status in ("new", "changed", "hold", "stop"):
            changed += 1
    return {
        "medications": len(meds),
        "red_flags": len(red_flags),
        "has_follow_up": bool((structured_data.get("follow_up") or {}).get("date")),
        "new_or_changed_meds": changed,
        "missing_critical_data_count": len(structured_data.get("missing_critical_data") or []),
    }


def _prompt_chip(prompt_id: str) -> dict[str, Any]:
    pm = prompt_meta(prompt_id)
    return {
        "prompt_id": pm["prompt_id"],
        "version": pm["version"],
        "sha": pm["sha"],
    }


async def run_postop_stream(
    input_data: Any,
    *,
    patient_id: str,
    clinic_code: Optional[str],
    resource_code: Optional[str],
    office_phone: Optional[str],
    health_system_id: Optional[str],
    ctx: StreamingPipelineContext,
) -> AsyncIterator[dict[str, Any]]:
    yield _ev(
        "pipeline.start",
        track="post_op",
        patient_id=patient_id,
        patient_name=input_data.patient_name,
        tracks=["post_op_diagnosis", "post_op_treatment"],
    )
    raw_package = {
        "metadata": {
            "patient_id": patient_id,
            "patient_name": input_data.patient_name,
            "phone_number": input_data.phone_number or "",
        },
        "clinical_data": {
            "clinical_notes": input_data.discharge_notes,
            "after_visit_summary": input_data.discharge_notes,
            "pmh": "",
            "procedure_context": "",
            "medication_list": "",
            "allergies": "",
            "problem_list": "",
        },
    }

    yield _ev("extract.start", model=resolve("extraction")["model"], prompt=_prompt_chip("ehr_extract"))
    structured_data = await ExtractionLayer().extract(raw_package)
    yield _ev(
        "extract.done",
        summary=_safe_summary(structured_data),
        missing_critical_data=structured_data.get("missing_critical_data", []),
    )

    generator = GenerationLayer()
    yield _ev(
        "generate.start",
        track="post_op_diagnosis",
        model=resolve("generation")["model"],
        prompts=[_prompt_chip("diagnosis_voice"), _prompt_chip("diagnosis_battlecard")],
    )
    yield _ev(
        "generate.start",
        track="post_op_treatment",
        model=resolve("generation")["model"],
        prompts=[_prompt_chip("treatment_voice"), _prompt_chip("treatment_battlecard")],
    )
    resources = await generator.generate_two_resources(structured_data)
    yield _ev("generate.done", track="post_op_diagnosis", word_count=_words(resources["diagnosis"]["voice_script"]))
    yield _ev("generate.done", track="post_op_treatment", word_count=_words(resources["treatment"]["voice_script"]))

    async def _regen_diagnosis() -> str:
        regen = await generator.generate_two_resources(structured_data)
        resources["diagnosis"] = regen["diagnosis"]
        return regen["diagnosis"]["voice_script"]

    async def _regen_treatment() -> str:
        regen = await generator.generate_two_resources(structured_data)
        resources["treatment"] = regen["treatment"]
        return regen["treatment"]["voice_script"]

    diag_required = build_required_items(structured_data, "post_op_diagnosis") if _GROUNDING_READY else []
    treat_required = build_required_items(structured_data, "post_op_treatment") if _GROUNDING_READY else []
    if _GROUNDING_READY:
        yield _ev(
            "grounding.start",
            track="post_op_diagnosis",
            judge_model=resolve("grounding_judge")["model"],
            prompt_version=GROUNDING_PROMPT_V,
            required_items=diag_required,
        )
        yield _ev("grounding.checking", track="post_op_diagnosis", phase="coverage")
    diag_gate, diag_audio = await synthesize_script(
        patient_id=patient_id,
        structured_data=structured_data,
        script=resources["diagnosis"]["voice_script"],
        track="post_op_diagnosis",
        team_store=ctx.team_store,
        audio_id=f"{patient_id}_diagnosis",
        regenerate_fn=_regen_diagnosis,
    )
    resources["diagnosis"]["voice_script"] = diag_gate.script
    if _GROUNDING_READY:
        if diag_gate.regenerated:
            yield _ev("grounding.regenerated", track="post_op_diagnosis", reason="first draft blocked; redrafted")
        diag_acc = compute_accuracy(diag_gate.report)
        yield _ev(
            "grounding.result",
            track="post_op_diagnosis",
            verdict=diag_gate.report.verdict,
            coverage_pct=diag_acc["coverage_pct"],
            faithfulness_pct=diag_acc["faithfulness_pct"],
            coverage=diag_gate.report.coverage,
            faithfulness=diag_gate.report.faithfulness,
            critical_failures=diag_gate.report.critical_failures,
            summary=diag_gate.report.summary,
        )
    if diag_audio:
        yield _ev("synthesize.done", track="post_op_diagnosis", audio_url=diag_audio)
    else:
        yield _ev("synthesize.skipped", track="post_op_diagnosis", reason=diag_gate.report.verdict)

    if _GROUNDING_READY:
        yield _ev(
            "grounding.start",
            track="post_op_treatment",
            judge_model=resolve("grounding_judge")["model"],
            prompt_version=GROUNDING_PROMPT_V,
            required_items=treat_required,
        )
        yield _ev("grounding.checking", track="post_op_treatment", phase="coverage")
    treat_gate, treat_audio = await synthesize_script(
        patient_id=patient_id,
        structured_data=structured_data,
        script=resources["treatment"]["voice_script"],
        track="post_op_treatment",
        team_store=ctx.team_store,
        audio_id=f"{patient_id}_treatment",
        regenerate_fn=_regen_treatment,
    )
    resources["treatment"]["voice_script"] = treat_gate.script
    if _GROUNDING_READY:
        if treat_gate.regenerated:
            yield _ev("grounding.regenerated", track="post_op_treatment", reason="first draft blocked; redrafted")
        treat_acc = compute_accuracy(treat_gate.report)
        yield _ev(
            "grounding.result",
            track="post_op_treatment",
            verdict=treat_gate.report.verdict,
            coverage_pct=treat_acc["coverage_pct"],
            faithfulness_pct=treat_acc["faithfulness_pct"],
            coverage=treat_gate.report.coverage,
            faithfulness=treat_gate.report.faithfulness,
            critical_failures=treat_gate.report.critical_failures,
            summary=treat_gate.report.summary,
        )
    if treat_audio:
        yield _ev("synthesize.done", track="post_op_treatment", audio_url=treat_audio)
    else:
        yield _ev("synthesize.skipped", track="post_op_treatment", reason=treat_gate.report.verdict)

    dashboard_url = f"{ctx.base_url}/patient/{patient_id}"
    # MERGE generated material onto any existing patient record instead of
    # replacing it wholesale. Replacing wiped seeded/triage fields (current_tier,
    # initial_tier, phase, windows, etc.) when regenerating for an existing patient.
    blob = ctx.patient_store.get(patient_id)
    if not isinstance(blob, dict):
        blob = {}
        ctx.patient_store[patient_id] = blob
    blob.update({
        "name": input_data.patient_name,
        "health_system_id": health_system_id,
        "phone": input_data.phone_number or "",
        "email": input_data.email or "",
        "pipeline_type": "post_op",
        "voice_audio_url": diag_audio,
        "battlecard_html": resources["diagnosis"]["battlecard_html"],
        "avatar_url": None,
        "voice_script": resources["diagnosis"]["voice_script"],
        "structured_data": structured_data,
        "clinic_code": clinic_code,
        "resource_code": resource_code,
        "office_phone": office_phone,
        "resources": {
            "diagnosis": {
                "voice_script": resources["diagnosis"]["voice_script"],
                "battlecard_html": resources["diagnosis"]["battlecard_html"],
                "voice_audio_url": diag_audio,
            },
            "treatment": {
                "voice_script": resources["treatment"]["voice_script"],
                "battlecard_html": resources["treatment"]["battlecard_html"],
                "voice_audio_url": treat_audio,
            },
        },
    })
    apply_grounding_to_patient(ctx.patient_store[patient_id], "post_op_diagnosis", diag_gate)
    apply_grounding_to_patient(ctx.patient_store[patient_id], "post_op_treatment", treat_gate)
    ctx.team_store.ensure_episode(
        patient_id=patient_id,
        procedure_type=structured_data.get("procedure_name", ""),
        clinic_code=clinic_code or "",
        resource_code=resource_code or "",
        health_system_id=health_system_id,
    )
    ctx.persist_demo()
    out = {
        "patient_id": patient_id,
        "dashboard_url": dashboard_url,
        "clinic_code": clinic_code,
        "resource_code": resource_code,
        "diagnosis": {
            "voice_script": resources["diagnosis"]["voice_script"],
            "battlecard_html": resources["diagnosis"]["battlecard_html"],
            "voice_audio_url": diag_audio,
        },
        "treatment": {
            "voice_script": resources["treatment"]["voice_script"],
            "battlecard_html": resources["treatment"]["battlecard_html"],
            "voice_audio_url": treat_audio,
        },
        "structured_data": structured_data,
    }
    yield _ev("complete", payload=out)


async def run_preop_stream(
    input_data: Any,
    *,
    patient_id: str,
    clinic_code: Optional[str],
    resource_code: Optional[str],
    office_phone: Optional[str],
    health_system_id: Optional[str],
    specialty_from_procedure: Callable[[str], str],
    ctx: StreamingPipelineContext,
) -> AsyncIterator[dict[str, Any]]:
    yield _ev(
        "pipeline.start",
        track="pre_op",
        patient_id=patient_id,
        patient_name=input_data.patient_name,
        tracks=["pre_op"],
    )
    raw_package = {
        "metadata": {
            "patient_id": patient_id,
            "patient_name": input_data.patient_name,
            "phone_number": input_data.phone_number or "",
        },
        "clinical_data": {
            "clinical_notes": input_data.preparation_notes,
            "after_visit_summary": input_data.preparation_notes,
            "pmh": "",
            "procedure_context": input_data.procedure_type or "",
            "medication_list": "",
            "allergies": "",
            "problem_list": "",
        },
    }
    yield _ev("extract.start", model=resolve("extraction")["model"], prompt=_prompt_chip("ehr_extract"))
    structured_data = await ExtractionLayer().extract(raw_package)
    if input_data.procedure_type and not structured_data.get("procedure_name"):
        structured_data["procedure_name"] = input_data.procedure_type
    if input_data.scheduled_surgery_date:
        structured_data["procedure_date"] = input_data.scheduled_surgery_date
        structured_data["procedure_status"] = "scheduled"
    structured_data["pre_op_instructions"] = structured_data.get("pre_op_instructions") or input_data.preparation_notes
    yield _ev(
        "extract.done",
        summary=_safe_summary(structured_data),
        missing_critical_data=structured_data.get("missing_critical_data", []),
    )

    generator = GenerationLayer()
    yield _ev(
        "generate.start",
        track="pre_op",
        model=resolve("generation")["model"],
        prompts=[_prompt_chip("preop_voice"), _prompt_chip("preop_battlecard")],
    )
    preop_voice, preop_battlecard = await generator.generate(structured_data, "pre_op")
    yield _ev("generate.done", track="pre_op", word_count=_words(preop_voice))

    async def _regen_preop() -> str:
        nonlocal preop_battlecard
        v, b = await generator.generate(structured_data, "pre_op")
        preop_battlecard = b
        return v

    required = build_required_items(structured_data, "pre_op") if _GROUNDING_READY else []
    if _GROUNDING_READY:
        yield _ev(
            "grounding.start",
            track="pre_op",
            judge_model=resolve("grounding_judge")["model"],
            prompt_version=GROUNDING_PROMPT_V,
            required_items=required,
        )
        yield _ev("grounding.checking", track="pre_op", phase="coverage")
    preop_gate, preop_audio = await synthesize_script(
        patient_id=patient_id,
        structured_data=structured_data,
        script=preop_voice,
        track="pre_op",
        team_store=ctx.team_store,
        audio_id=f"{patient_id}_preop",
        regenerate_fn=_regen_preop,
    )
    preop_voice = preop_gate.script
    if _GROUNDING_READY:
        if preop_gate.regenerated:
            yield _ev("grounding.regenerated", track="pre_op", reason="first draft blocked; redrafted")
        acc = compute_accuracy(preop_gate.report)
        yield _ev(
            "grounding.result",
            track="pre_op",
            verdict=preop_gate.report.verdict,
            coverage_pct=acc["coverage_pct"],
            faithfulness_pct=acc["faithfulness_pct"],
            coverage=preop_gate.report.coverage,
            faithfulness=preop_gate.report.faithfulness,
            critical_failures=preop_gate.report.critical_failures,
            summary=preop_gate.report.summary,
        )
    if preop_audio:
        yield _ev("synthesize.done", track="pre_op", audio_url=preop_audio)
    else:
        yield _ev("synthesize.skipped", track="pre_op", reason=preop_gate.report.verdict)

    dashboard_url = f"{ctx.base_url}/patient/{patient_id}/pre-op"
    specialty = specialty_from_procedure(structured_data.get("procedure_name", ""))
    # MERGE generated material onto any existing patient record instead of
    # replacing it wholesale (preserves seeded/triage fields on regeneration).
    blob = ctx.patient_store.get(patient_id)
    if not isinstance(blob, dict):
        blob = {}
        ctx.patient_store[patient_id] = blob
    blob.update({
        "name": input_data.patient_name,
        "health_system_id": health_system_id,
        "phone": input_data.phone_number or "",
        "email": input_data.email or "",
        "pipeline_type": "pre_op",
        "voice_audio_url": preop_audio,
        "battlecard_html": preop_battlecard,
        "avatar_url": None,
        "voice_script": preop_voice,
        "structured_data": structured_data,
        "clinic_code": clinic_code,
        "resource_code": resource_code,
        "office_phone": office_phone,
        "specialty": specialty,
        "scheduled_surgery_date": structured_data.get("procedure_date", ""),
        "resources": {
            "preop": {
                "voice_script": preop_voice,
                "battlecard_html": preop_battlecard,
                "voice_audio_url": preop_audio,
            }
        },
    })
    apply_grounding_to_patient(ctx.patient_store[patient_id], "pre_op", preop_gate)
    ctx.team_store.ensure_episode(
        patient_id=patient_id,
        procedure_type=structured_data.get("procedure_name", ""),
        clinic_code=clinic_code or "",
        resource_code=resource_code or "",
        health_system_id=health_system_id,
    )
    ctx.persist_demo()
    out = {
        "patient_id": patient_id,
        "dashboard_url": dashboard_url,
        "clinic_code": clinic_code,
        "resource_code": resource_code,
        "preop": {
            "voice_script": preop_voice,
            "battlecard_html": preop_battlecard,
            "voice_audio_url": preop_audio,
        },
        "structured_data": structured_data,
        "specialty": specialty,
    }
    yield _ev("complete", payload=out)
