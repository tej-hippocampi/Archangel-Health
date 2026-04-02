"""
Prompt Registry — maps prompt_id → metadata for the internal Prompt Lab.
"""

from .diagnosis import DIAGNOSIS_VOICE_PROMPT, DIAGNOSIS_BATTLECARD_PROMPT
from .treatment import TREATMENT_VOICE_PROMPT, TREATMENT_BATTLECARD_PROMPT
from .preop import PREOP_VOICE_PROMPT, PREOP_BATTLECARD_PROMPT
from .postop import POSTOP_VOICE_PROMPT, POSTOP_BATTLECARD_PROMPT
from .avatar import AVATAR_BEHAVIOR_TEMPLATE

PROMPT_REGISTRY: dict = {
    "avatar_chat": {
        "label": "Digital Care Companion — Chat Persona",
        "content": AVATAR_BEHAVIOR_TEMPLATE,
        "file": "backend/prompts/avatar.py",
        "variable": "AVATAR_BEHAVIOR_TEMPLATE",
        "type": "avatar",
    },
    "diagnosis_voice": {
        "label": "Diagnosis — Voice Script",
        "content": DIAGNOSIS_VOICE_PROMPT,
        "file": "backend/prompts/diagnosis.py",
        "variable": "DIAGNOSIS_VOICE_PROMPT",
        "type": "voice",
        "paired_battlecard": "diagnosis_battlecard",
    },
    "diagnosis_battlecard": {
        "label": "Diagnosis — Battlecard",
        "content": DIAGNOSIS_BATTLECARD_PROMPT,
        "file": "backend/prompts/diagnosis.py",
        "variable": "DIAGNOSIS_BATTLECARD_PROMPT",
        "type": "battlecard",
        "paired_voice": "diagnosis_voice",
    },
    "treatment_voice": {
        "label": "Treatment — Voice Script",
        "content": TREATMENT_VOICE_PROMPT,
        "file": "backend/prompts/treatment.py",
        "variable": "TREATMENT_VOICE_PROMPT",
        "type": "voice",
        "paired_battlecard": "treatment_battlecard",
    },
    "treatment_battlecard": {
        "label": "Treatment — Battlecard",
        "content": TREATMENT_BATTLECARD_PROMPT,
        "file": "backend/prompts/treatment.py",
        "variable": "TREATMENT_BATTLECARD_PROMPT",
        "type": "battlecard",
        "paired_voice": "treatment_voice",
    },
    "preop_voice": {
        "label": "Pre-Op — Voice Script",
        "content": PREOP_VOICE_PROMPT,
        "file": "backend/prompts/preop.py",
        "variable": "PREOP_VOICE_PROMPT",
        "type": "voice",
        "paired_battlecard": "preop_battlecard",
    },
    "preop_battlecard": {
        "label": "Pre-Op — Battlecard",
        "content": PREOP_BATTLECARD_PROMPT,
        "file": "backend/prompts/preop.py",
        "variable": "PREOP_BATTLECARD_PROMPT",
        "type": "battlecard",
        "paired_voice": "preop_voice",
    },
    "postop_voice": {
        "label": "Post-Op — Voice Script (Legacy)",
        "content": POSTOP_VOICE_PROMPT,
        "file": "backend/prompts/postop.py",
        "variable": "POSTOP_VOICE_PROMPT",
        "type": "voice",
        "paired_battlecard": "postop_battlecard",
    },
    "postop_battlecard": {
        "label": "Post-Op — Battlecard (Legacy)",
        "content": POSTOP_BATTLECARD_PROMPT,
        "file": "backend/prompts/postop.py",
        "variable": "POSTOP_BATTLECARD_PROMPT",
        "type": "battlecard",
        "paired_voice": "postop_voice",
    },
}
