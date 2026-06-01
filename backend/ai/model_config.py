import os
from typing import Any

APP_AI_CONFIG_VERSION = "2026-05-31.1"

# temperature: None  -> do NOT send temperature (API default)
#              float -> send exact value
MODEL_REGISTRY: dict[str, dict[str, Any]] = {
    "generation": {"model": "claude-sonnet-4-6", "temperature": None, "max_tokens": 2000},
    "extraction": {"model": "claude-sonnet-4-6", "temperature": None, "max_tokens": 2500},
    "eligibility_extract": {"model": "claude-sonnet-4-6", "temperature": 0.0, "max_tokens": 4000},
    "intraop_extract": {"model": "claude-sonnet-4-6", "temperature": 0.0, "max_tokens": 4000},
    "intake_chat": {"model": "claude-sonnet-4-6", "temperature": 0.2, "max_tokens": 3000},
    "escalation_classifier": {"model": "claude-sonnet-4-6", "temperature": None, "max_tokens": 120},
    "care_companion_chat": {"model": "claude-sonnet-4-6", "temperature": None, "max_tokens": 350},
    "avatar_chat": {"model": "claude-sonnet-4-6", "temperature": None, "max_tokens": 150},
    "grounding_judge": {"model": "claude-sonnet-4-6", "temperature": 0.0, "max_tokens": 1500},
}

_LEGACY_ENV = {"intraop_extract": "INTRAOP_EXTRACTOR_MODEL"}


def resolve(role: str) -> dict[str, Any]:
    cfg = dict(MODEL_REGISTRY[role])
    env_model = os.getenv(f"MODEL_{role.upper()}")
    if not env_model and role in _LEGACY_ENV:
        env_model = os.getenv(_LEGACY_ENV[role])
    if env_model:
        cfg["model"] = env_model
    return cfg
