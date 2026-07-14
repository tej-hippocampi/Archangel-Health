"""Shared fixtures/helpers for the Asclepius test suite.

Env (DB path, export dir, auth secret, QA sampling off) is set BEFORE importing
``main`` so the standalone portal resolves to temp paths and a stable signing
secret — mirroring ``tests/test_gold_router.py``. Each test resets the store to a
fresh temp DB (``fresh_store``) for isolation, and mints Asclepius JWTs via
``asclepius.auth.create_token``. team.db and the clinical RBAC are never touched.
"""

from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = tempfile.mkdtemp(prefix="asclepius_test_")
os.environ.setdefault("ASCLEPIUS_DB_PATH", os.path.join(_TMP, "asclepius.db"))
os.environ.setdefault("ASCLEPIUS_EXPORT_DIR", os.path.join(_TMP, "exports"))
os.environ.setdefault("ASCLEPIUS_AUTH_SECRET", "asclepius-test-secret-0123456789-abcdefXYZ")
# QA sampling MUST be off for a deterministic suite: many tests assert a clean
# submission reaches ``export_ready``, and random sampling would route ~15% of
# them to ``needs_qa`` (a flake). Hard-assign (not setdefault) so a non-zero
# value in the CI runner's environment can never re-enable it. The sampling
# path itself is exercised explicitly where needed via
# ``monkeypatch.setattr(pipeline, "_should_sample", lambda: True)``.
os.environ["ASCLEPIUS_QA_SAMPLE_PCT"] = "0"
# V3 multimodal-by-default is ON in production, but the existing V3 serving tests
# use TEXT hard tasks (multimodal generation needs an LLM the suite doesn't have).
# Hard-assign OFF so those stay valid; the multimodal-only behavior is exercised
# explicitly where needed via monkeypatch (mirrors the QA-sampling pattern above).
os.environ["ASCLEPIUS_V3_MULTIMODAL_ONLY"] = "0"
os.environ.setdefault("ASCLEPIUS_TIME_FLOOR_SEC", "20")
os.environ.setdefault("RATE_LIMIT_ENABLED", "0")
# Seedmaker generation: deterministic thresholds + small bounds for fast tests.
os.environ.setdefault("ASCLEPIUS_GEN_MIN_ERROR_LIKELIHOOD", "0.5")
os.environ.setdefault("ASCLEPIUS_GEN_MIN_REVISION_VALUE", "0.5")
os.environ.setdefault("ASCLEPIUS_GEN_MAX_ATTEMPTS_PER_TASK", "4")
os.environ.setdefault("ASCLEPIUS_GEN_FEWSHOT_K", "4")

from main import app  # noqa: E402  (import after env is set)
from asclepius import auth as asc_auth  # noqa: E402
from asclepius import store as asc_store  # noqa: E402

__all__ = ["app", "fresh_store", "make_user", "token_for", "headers_for", "TMP_DIR", "uniq"]

TMP_DIR = _TMP

# Alpha-only unique token for test fixtures. A bare ``uuid4().hex[:N]`` slice can
# land on 7+ consecutive digits, which the PHI scanner's long-number rule
# (``\b\d{7,}\b``) legitimately flags — intermittently routing an otherwise-clean
# submission to QA and flaking any test that asserts ``export_ready``. Mapping the
# digits to letters keeps uniqueness while guaranteeing no numeric run.
_DIGIT_TO_ALPHA = str.maketrans("0123456789", "ghijklmnop")


def uniq(n: int = 8) -> str:
    return uuid.uuid4().hex[:n].translate(_DIGIT_TO_ALPHA)


def fresh_store():
    """Rebind the process-wide store to a brand-new temp DB for test isolation."""
    path = os.path.join(_TMP, f"asclepius_{uuid.uuid4().hex}.db")
    return asc_store.reset_store_for_tests(db_path=path)


def make_user(store, role: str = "evaluator", **kw):
    email = kw.pop("email", f"{role}-{uuid.uuid4().hex[:8]}@asclepius.example.com")
    return store.create_user(email=email, password="pw-12345678", role=role, **kw)


def token_for(user) -> str:
    return asc_auth.create_token(user)


def headers_for(user) -> dict:
    return {"Authorization": f"Bearer {token_for(user)}"}
