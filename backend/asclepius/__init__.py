"""Asclepius — Expert Evaluation Portal (Product #3 MVP).

A fully isolated, standalone feature: credentialed clinicians evaluate blinded
A/B AI answers to medical prompts, and every submission is automatically
captured -> packaged -> validated -> QA'd -> exported as frontier-lab-ready
JSONL training data (preference pairs, ideal-answer SFT, reasoning traces).

Isolation guarantees (PRD §0, §10):
  * Own SQLite database (``asclepius.db`` via ``ASCLEPIUS_DB_PATH``) — never
    touches ``team.db``.
  * Own standalone JWT auth (``ASCLEPIUS_AUTH_SECRET``) and user table —
    independent of the clinical RBAC (``auth_roles.py`` / tenant JWT).
  * Reuses the shared Anthropic client (``ai.llm_client.call_llm``) and the
    subprocessor BAA gate for any LLM call.
  * Disabled simply by not mounting ``routers/asclepius.py`` in ``main.py``.

No PHI: prompts are synthetic / de-identified. A defensive PHI scan still runs
on every submission (PRD §5, §13).
"""

from .constants import (
    ASCLEPIUS_TAXONOMY_VERSION,
    CONFIDENCE_LEVELS,
    ERROR_TAXONOMY,
    ROLES,
    SUBMISSION_STATUSES,
    VERDICTS,
    WHY_BETTER_TAGS,
)

__all__ = [
    "ASCLEPIUS_TAXONOMY_VERSION",
    "CONFIDENCE_LEVELS",
    "ERROR_TAXONOMY",
    "ROLES",
    "SUBMISSION_STATUSES",
    "VERDICTS",
    "WHY_BETTER_TAGS",
]
