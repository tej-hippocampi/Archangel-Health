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
# The verification agent is a background loop that polls verification_jobs on a
# timer. Every TestClient(app) in this suite runs the startup hooks, so leaving
# it on means dozens of pollers running against a store the tests keep
# rebinding under them via fresh_store(). Hard-assign (not setdefault) for the
# same reason as the sampling flag above: a value in the CI runner's
# environment must not be able to re-enable it. The agent's own behavior is
# exercised directly in tests/test_auto_verification.py, which calls run_one()
# rather than waiting on the loop.
os.environ["ASCLEPIUS_VERIFY_AGENT_ENABLED"] = "0"
# V3 multimodal-by-default is ON in production. It's now a PREFERENCE (serve a
# structured case when one exists, else fall back to the hard text queue), so the
# existing V3 text-serving tests would pass either way — but hard-assign OFF for a
# deterministic baseline. The preference + its no-empty-queue fallback are exercised
# explicitly via monkeypatch (mirrors the QA-sampling pattern above).
os.environ["ASCLEPIUS_V3_MULTIMODAL_ONLY"] = "0"
# The V3 bring-up relaxation of the multimodal QUALITY gates ships ON in production
# (so V3 can show a case before real generation quality is dialed in), but the
# generation suite asserts the STRICT gate drops. Hard-assign OFF for a deterministic
# strict baseline; the relaxed behavior is exercised explicitly via monkeypatch.
os.environ["ASCLEPIUS_V3_RELAX_MM_GATES"] = "0"
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

__all__ = ["app", "fresh_store", "make_user", "pass_practice_case", "token_for",
           "headers_for", "TMP_DIR", "uniq"]

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


#: Roles for which a contributor tier is meaningful (mirrors
#: ``capabilities._CAPABLE_ROLES``). A ``data_partner`` or ``buyer`` fixture must
#: NOT be handed one — those roles are denied the evaluator surface outright, and
#: a fixture that quietly carries a tier would hide a regression in that denial.
_TIERED_ROLES = ("evaluator", "qa_reviewer")


def make_user(store, role: str = "evaluator", **kw):
    """A fixture physician who can actually work.

    ``tier`` defaults to ``labeler`` for contributor roles, because that is what
    a real approved account looks like: the verification queue assigns a tier at
    the moment of approval, and ``capabilities.LABEL`` is enforced at
    /tasks/next and /submissions. A NULL-tier fixture is a signed-up-but-not-yet-
    approved account, so tests that want that state pass ``tier=None``
    explicitly rather than getting it by accident.
    """
    email = kw.pop("email", f"{role}-{uuid.uuid4().hex[:8]}@asclepius.example.com")
    if role in _TIERED_ROLES and "tier" not in kw:
        kw["tier"] = "labeler"
    practice_case = kw.pop("practice_case", None)
    user = store.create_user(email=email, password="pw-12345678", role=role, **kw)
    if practice_case is None:
        practice_case = role in _TIERED_ROLES and kw.get("tier") is not None
    if practice_case:
        pass_practice_case(store, user["id"])
        user = store.get_user_by_id(user["id"])
    return user


def pass_practice_case(store, user_id: str) -> None:
    """Open the practice-case gate on a fixture physician.

    The practice case is a hard gate on /tasks/next, /tasks/available and
    /submissions, so without this every fixture that draws or submits would 403
    and dozens of unrelated test files would go red at once, hiding whatever
    they were actually written to catch.

    Defaulted ON for tiered contributor roles for the same reason ``tier``
    defaults to labeler above: that is what a real working account looks like.
    A test that wants the gate SHUT passes ``practice_case=False`` explicitly,
    so the gated state is always something a test asked for rather than
    something it inherited.

    Public, because a test that builds its physician through the real approval
    flow rather than through make_user still has to open this gate: approval
    and the practice case are two different axes, and passing one does not pass
    the other.
    """
    from asclepius import tutorial_case as _tc  # noqa: PLC0415 - test-only

    state = store.get_tutorial_state(user_id)
    state["status"] = "completed"
    state["gate"] = {
        "state": "passed",
        "passed_version": _tc.TUTORIAL_VERSION,
        "attempts": 1,
        "source": "fixture",
    }
    store.set_tutorial_state(user_id, state)


def token_for(user) -> str:
    return asc_auth.create_token(user)


def headers_for(user) -> dict:
    return {"Authorization": f"Bearer {token_for(user)}"}
