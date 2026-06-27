"""
Prompt Registry — maps prompt_id → metadata for the internal Prompt Lab.
"""

import hashlib

from .diagnosis import DIAGNOSIS_VOICE_PROMPT, DIAGNOSIS_BATTLECARD_PROMPT
from .treatment import TREATMENT_VOICE_PROMPT, TREATMENT_BATTLECARD_PROMPT
from .preop import PREOP_VOICE_PROMPT, PREOP_BATTLECARD_PROMPT
from .postop import POSTOP_VOICE_PROMPT, POSTOP_BATTLECARD_PROMPT
from .avatar import AVATAR_BEHAVIOR_TEMPLATE
from .eligibility import ELIGIBILITY_SYSTEM_PROMPT
from asclepius.prompts import (
    ASCLEPIUS_CRITIC_SYSTEM,
    ASCLEPIUS_CANDIDATE_GEN_SYSTEM,
    ASCLEPIUS_GROUNDING_SYSTEM,
    ASCLEPIUS_PROMPT_GEN_SYSTEM,
    ASCLEPIUS_PROMPT_JUDGE_SYSTEM,
)
from .system import SEMANTIC_ESCALATION_PROMPT
from pipeline.grounding_check import GROUNDING_JUDGE_PROMPT
from pipeline.teachback_grade import TEACHBACK_JUDGE_PROMPT
from pipeline.teachback_questions import TEACHBACK_QUESTIONS_PROMPT
from pipeline.extract import EXTRACTION_PROMPT, EXTRACTION_SYSTEM
from triage.intraop.extractor_llm import _system_prompt as intraop_system_prompt
from intake_section_chat import (
    INTAKE_REPAIR_SYSTEM_PROMPT,
    INTAKE_SYSTEM_TEMPLATE,
    INTAKE_TURN_JSON_CRITICAL,
    INTAKE_TURN_TOOL_CRITICAL,
)

_INTAKE_SKELETON_ARGS = {
    "patient_name": "{patient_name}",
    "critical": "{critical}",
    "spec": "{spec}",
    "current_form_section": "{current_form_section}",
    "skeleton": "{skeleton}",
    "ref": "{ref}",
    "patient_context": "{patient_context}",
    "prior_sections_text": "{prior_sections_text}",
}


def _safe_content(builder, fallback: str = "") -> str:
    try:
        return builder()
    except Exception:
        return fallback

PROMPT_REGISTRY: dict = {
    "avatar_chat": {
        "label": "Digital Care Companion — Chat Persona",
        "content": AVATAR_BEHAVIOR_TEMPLATE,
        "file": "backend/prompts/avatar.py",
        "variable": "AVATAR_BEHAVIOR_TEMPLATE",
        "type": "avatar",
        "version": "1.1.0",
    },
    "diagnosis_voice": {
        "label": "Diagnosis — Voice Script",
        "content": DIAGNOSIS_VOICE_PROMPT,
        "file": "backend/prompts/diagnosis.py",
        "variable": "DIAGNOSIS_VOICE_PROMPT",
        "type": "voice",
        "paired_battlecard": "diagnosis_battlecard",
        "version": "1.0.0",
    },
    "diagnosis_battlecard": {
        "label": "Diagnosis — Battlecard",
        "content": DIAGNOSIS_BATTLECARD_PROMPT,
        "file": "backend/prompts/diagnosis.py",
        "variable": "DIAGNOSIS_BATTLECARD_PROMPT",
        "type": "battlecard",
        "paired_voice": "diagnosis_voice",
        "version": "1.0.0",
    },
    "treatment_voice": {
        "label": "Treatment — Voice Script",
        "content": TREATMENT_VOICE_PROMPT,
        "file": "backend/prompts/treatment.py",
        "variable": "TREATMENT_VOICE_PROMPT",
        "type": "voice",
        "paired_battlecard": "treatment_battlecard",
        "version": "1.0.0",
    },
    "treatment_battlecard": {
        "label": "Treatment — Battlecard",
        "content": TREATMENT_BATTLECARD_PROMPT,
        "file": "backend/prompts/treatment.py",
        "variable": "TREATMENT_BATTLECARD_PROMPT",
        "type": "battlecard",
        "paired_voice": "treatment_voice",
        "version": "1.0.0",
    },
    "preop_voice": {
        "label": "Pre-Op — Voice Script",
        "content": PREOP_VOICE_PROMPT,
        "file": "backend/prompts/preop.py",
        "variable": "PREOP_VOICE_PROMPT",
        "type": "voice",
        "paired_battlecard": "preop_battlecard",
        "version": "1.0.0",
    },
    "preop_battlecard": {
        "label": "Pre-Op — Battlecard",
        "content": PREOP_BATTLECARD_PROMPT,
        "file": "backend/prompts/preop.py",
        "variable": "PREOP_BATTLECARD_PROMPT",
        "type": "battlecard",
        "paired_voice": "preop_voice",
        "version": "1.0.0",
    },
    "postop_voice": {
        "label": "Post-Op — Voice Script (Legacy)",
        "content": POSTOP_VOICE_PROMPT,
        "file": "backend/prompts/postop.py",
        "variable": "POSTOP_VOICE_PROMPT",
        "type": "voice",
        "paired_battlecard": "postop_battlecard",
        "version": "1.0.0",
    },
    "postop_battlecard": {
        "label": "Post-Op — Battlecard (Legacy)",
        "content": POSTOP_BATTLECARD_PROMPT,
        "file": "backend/prompts/postop.py",
        "variable": "POSTOP_BATTLECARD_PROMPT",
        "type": "battlecard",
        "paired_voice": "postop_voice",
        "version": "1.0.0",
    },
    "ehr_extract": {
        "label": "EHR Structured Extraction",
        "content": _safe_content(lambda: f"{EXTRACTION_SYSTEM}\n\n{EXTRACTION_PROMPT}"),
        "file": "backend/pipeline/extract.py",
        "variable": "EXTRACTION_SYSTEM|EXTRACTION_PROMPT",
        "type": "system",
        "version": "1.0.0",
    },
    "eligibility_extract": {
        "label": "TEAM Eligibility Extraction",
        "content": ELIGIBILITY_SYSTEM_PROMPT,
        "file": "backend/prompts/eligibility.py",
        "variable": "ELIGIBILITY_SYSTEM_PROMPT",
        "type": "system",
        "version": "1.0.0",
    },
    "intraop_extract": {
        "label": "Intra-Op Tool Extraction",
        "content": _safe_content(lambda: intraop_system_prompt(None)),
        "file": "backend/triage/intraop/extractor_llm.py",
        "variable": "_system_prompt",
        "type": "system",
        "version": "1.0.0",
    },
    "semantic_escalation": {
        "label": "Semantic Escalation Classifier",
        "content": SEMANTIC_ESCALATION_PROMPT,
        "file": "backend/prompts/system.py",
        "variable": "SEMANTIC_ESCALATION_PROMPT",
        "type": "system",
        "version": "1.0.0",
    },
    "care_companion_chat": {
        "label": "Care Companion Chat System",
        "content": AVATAR_BEHAVIOR_TEMPLATE,
        "file": "backend/prompts/avatar.py",
        "variable": "AVATAR_BEHAVIOR_TEMPLATE",
        "type": "system",
        "version": "1.1.0",
    },
    "grounding_judge": {
        "label": "Grounding Judge Audit Prompt",
        "content": _safe_content(lambda: GROUNDING_JUDGE_PROMPT),
        "file": "backend/pipeline/grounding_check.py",
        "variable": "GROUNDING_JUDGE_PROMPT",
        "type": "system",
        "version": "1.0.0",
    },
    "teachback_author": {
        "label": "Teach-Back — Question Author",
        "content": _safe_content(lambda: TEACHBACK_QUESTIONS_PROMPT),
        "file": "backend/pipeline/teachback_questions.py",
        "variable": "TEACHBACK_QUESTIONS_PROMPT",
        "type": "system",
        "version": "1.0.0",
    },
    "teachback_judge": {
        "label": "Teach-Back — Answer Judge",
        "content": _safe_content(lambda: TEACHBACK_JUDGE_PROMPT),
        "file": "backend/pipeline/teachback_grade.py",
        "variable": "TEACHBACK_JUDGE_PROMPT",
        "type": "system",
        "version": "1.0.0",
    },
    "intake_repair": {
        "label": "Intake JSON Repair System",
        "content": INTAKE_REPAIR_SYSTEM_PROMPT,
        "file": "backend/intake_section_chat.py",
        "variable": "INTAKE_REPAIR_SYSTEM_PROMPT",
        "type": "system",
        "version": "1.0.0",
    },
    "intake_turn": {
        "label": "Intake Turn Tool System",
        "content": _safe_content(
            lambda: INTAKE_SYSTEM_TEMPLATE.format(
                **{**_INTAKE_SKELETON_ARGS, "critical": INTAKE_TURN_TOOL_CRITICAL}
            )
        ),
        "file": "backend/intake_section_chat.py",
        "variable": "INTAKE_SYSTEM_TEMPLATE",
        "type": "system",
        "version": "1.0.0",
    },
    "intake_turn_json": {
        "label": "Intake Turn JSON Fallback System",
        "content": _safe_content(
            lambda: INTAKE_SYSTEM_TEMPLATE.format(
                **{**_INTAKE_SKELETON_ARGS, "critical": INTAKE_TURN_JSON_CRITICAL}
            )
        ),
        "file": "backend/intake_section_chat.py",
        "variable": "INTAKE_SYSTEM_TEMPLATE",
        "type": "system",
        "version": "1.0.0",
    },
    "asclepius_critic": {
        "label": "Asclepius — Evaluation Consistency Critic",
        "content": ASCLEPIUS_CRITIC_SYSTEM,
        "file": "backend/asclepius/prompts.py",
        "variable": "ASCLEPIUS_CRITIC_SYSTEM",
        "type": "system",
        "version": "1.0.0",
    },
    "asclepius_candidate_gen": {
        "label": "Asclepius — Candidate Answer Generation",
        "content": ASCLEPIUS_CANDIDATE_GEN_SYSTEM,
        "file": "backend/asclepius/prompts.py",
        "variable": "ASCLEPIUS_CANDIDATE_GEN_SYSTEM",
        "type": "system",
        "version": "2.0.0",
    },
    "asclepius_prompt_gen": {
        "label": "Asclepius Seedmaker — Prompt Generation (nephrology)",
        "content": ASCLEPIUS_PROMPT_GEN_SYSTEM,
        "file": "backend/asclepius/prompts.py",
        "variable": "ASCLEPIUS_PROMPT_GEN_SYSTEM",
        "type": "system",
        "version": "1.0.0",
    },
    "asclepius_prompt_judge": {
        "label": "Asclepius Seedmaker — Prompt / Error-Likelihood Judge",
        "content": ASCLEPIUS_PROMPT_JUDGE_SYSTEM,
        "file": "backend/asclepius/prompts.py",
        "variable": "ASCLEPIUS_PROMPT_JUDGE_SYSTEM",
        "type": "system",
        "version": "1.0.0",
    },
    "asclepius_grounding": {
        "label": "Asclepius — Evidence Grounding Check",
        "content": ASCLEPIUS_GROUNDING_SYSTEM,
        "file": "backend/asclepius/prompts.py",
        "variable": "ASCLEPIUS_GROUNDING_SYSTEM",
        "type": "system",
        "version": "1.0.0",
    },
}


def prompt_sha(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]


def prompt_meta(prompt_id: str) -> dict:
    e = PROMPT_REGISTRY[prompt_id]
    return {
        "prompt_id": prompt_id,
        "version": e.get("version", "0.0.0"),
        "sha": prompt_sha(e["content"]),
    }
