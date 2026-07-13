"""The V4 wall (Data Provider Portal PRD §8) — a real de-identified case is a V4
task and ONLY a V4 task, enforced server-side in routing, derivation, and
packaging (never the UI).

Pure-unit: exercises store routing, cases.derive_portal_version, the packaging
assertion, and the value premium without importing the FastAPI app.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asclepius import value as asc_value  # noqa: E402
from asclepius.cases import (  # noqa: E402
    V4WallViolation,
    derive_portal_version,
    is_real_case_task,
)
from asclepius.packaging import v4_wall_violation  # noqa: E402
from asclepius.store import AsclepiusStore  # noqa: E402

REAL_CASE = {"case_source": "real_deid", "specialty": "nephrology",
             "lab_panels": [{"panel": "BMP", "collected_offset_days": 0, "results": []}]}
SYNTH_CASE = {"case_source": "synthetic", "specialty": "nephrology", "lab_panels": []}


def _store():
    d = tempfile.mkdtemp(prefix="asc_v4_")
    return AsclepiusStore(db_path=os.path.join(d, "t.db"))


# ─── derivation (PRD §8.2) ────────────────────────────────────────────────────
def test_real_case_derives_v4_regardless_of_declared():
    task = {"case": REAL_CASE}
    assert derive_portal_version(task, None) == "v4"
    assert derive_portal_version(task, "v4") == "v4"


def test_real_case_rejects_non_v4_client_claim():
    task = {"case": REAL_CASE}
    for claimed in ("v1", "v2", "v3"):
        try:
            derive_portal_version(task, claimed)
            assert False, f"expected V4WallViolation for {claimed}"
        except V4WallViolation:
            pass


def test_synthetic_case_cannot_claim_v4():
    task = {"case": SYNTH_CASE}
    try:
        derive_portal_version(task, "v4")
        assert False, "synthetic case claiming v4 must raise"
    except V4WallViolation:
        pass
    assert derive_portal_version(task, "v3") == "v3"


def test_is_real_case_task():
    assert is_real_case_task({"case": REAL_CASE})
    assert not is_real_case_task({"case": SYNTH_CASE})
    assert not is_real_case_task({"prompt": "text task, no case"})


# ─── routing (PRD §8.1) ───────────────────────────────────────────────────────
def test_v4_queue_serves_only_real_cases():
    store = _store()
    store.insert_task(prompt="synthetic q", case=SYNTH_CASE, specialty="nephrology")
    real = store.insert_task(prompt="real q", case=REAL_CASE, specialty="nephrology")

    # V4 (real_only) serves only the real case.
    got = store.next_task_for_evaluator(
        evaluator_id="e1", specialty="nephrology", real_only=True, exclude_real=False)
    assert got and got["task_id"] == real["task_id"]


def test_v123_queue_excludes_real_cases():
    store = _store()
    synth = store.insert_task(prompt="synthetic q", case=SYNTH_CASE, specialty="nephrology")
    store.insert_task(prompt="real q", case=REAL_CASE, specialty="nephrology")

    # v1/v2/v3 (exclude_real) never serve the real case.
    got = store.next_task_for_evaluator(
        evaluator_id="e1", specialty="nephrology", real_only=False, exclude_real=True)
    assert got and got["task_id"] == synth["task_id"]

    eligible = store.eligible_tasks_for_evaluator(
        evaluator_id="e1", specialty="nephrology", exclude_real=True)
    assert all((t.get("case") or {}).get("case_source") != "real_deid" for t in eligible)


# ─── packaging assertion (PRD §8.3) ───────────────────────────────────────────
def test_packaging_assertion_flags_mismatch():
    real_task = {"case": REAL_CASE}
    synth_task = {"case": SYNTH_CASE}
    assert v4_wall_violation(real_task, "v3")          # real must be v4
    assert v4_wall_violation(synth_task, "v4")         # v4 needs a real case
    assert v4_wall_violation(real_task, "v4") is None   # consistent
    assert v4_wall_violation(synth_task, "v2") is None  # consistent


# ─── value premium (PRD §8) ───────────────────────────────────────────────────
def test_real_case_earns_more_than_synthetic_multimodal():
    task_real = {"case": REAL_CASE, "modality": "multimodal", "difficulty": "medium",
                 "source": "lab_supplied"}
    task_synth = {"case": SYNTH_CASE, "modality": "multimodal", "difficulty": "medium",
                  "source": "lab_supplied"}
    recs = [{"type": "preference", "chosen": "a", "rejected": "b"}]
    sub = {"payload": {}}
    v_real = asc_value.estimate_value(recs, task_real, sub)
    v_synth = asc_value.estimate_value(recs, task_synth, sub)
    assert v_real["breakdown"]["is_real_case"] is True
    assert v_synth["breakdown"]["is_real_case"] is False
    assert v_real["tier_mult"] > v_synth["tier_mult"]
