"""Gold Standard runtime configuration + taxonomy loading.

The error-label taxonomy lives in ``gold/taxonomy.json`` (loaded the way
``tuning.json`` is loaded for triage) so it can be extended without code
changes. Everything else (specialty default, retention window, de-id provider)
comes from environment variables.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict

_TAXONOMY_PATH = Path(__file__).with_name("taxonomy.json")


@lru_cache(maxsize=1)
def load_taxonomy() -> Dict[str, Any]:
    """Load the error-label taxonomy from config (cached)."""
    with _TAXONOMY_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def taxonomy_types() -> set[str]:
    return {t["type"] for t in load_taxonomy().get("types", [])}


def taxonomy_severities() -> set[str]:
    return set(load_taxonomy().get("severities", []))


def taxonomy_sections() -> set[str]:
    return set(load_taxonomy().get("sections", []))


def workflow_tasks() -> list[str]:
    """Workflow tasks a record can serve (note_generation always implicit)."""
    return list(load_taxonomy().get("workflow_tasks", [
        "note_generation", "icd10_coding", "cpt_coding", "prior_auth",
    ]))


def allow_self_qa() -> bool:
    """Escape hatch (default OFF): permit the submitter to also QA-approve."""
    return (os.getenv("GOLD_ALLOW_SELF_QA") or "0").strip().lower() in ("1", "true", "yes", "on")


def default_specialty() -> str:
    return (os.getenv("GOLD_DEFAULT_SPECIALTY") or "general_surgery").strip() or "general_surgery"


def default_encounter_type() -> str:
    return (os.getenv("GOLD_DEFAULT_ENCOUNTER_TYPE") or "post-op follow-up").strip() or "post-op follow-up"


def audio_retention_days() -> int:
    try:
        return max(0, int(os.getenv("GOLD_AUDIO_RETENTION_DAYS") or "30"))
    except ValueError:
        return 30


def deid_provider() -> str:
    """One of: llm | presidio | both | regex. Default llm (with regex baseline)."""
    return (os.getenv("GOLD_DEID_PROVIDER") or "llm").strip().lower() or "llm"
