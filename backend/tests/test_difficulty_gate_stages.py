"""Staging the empirical-difficulty gate (Task Pipeline PRD §A).

The gate is the product's central economic claim: a case is only worth selling
if frontier models fail it. The measurement that establishes that spends real
frontier tokens on every generated case, so it ships in two stages and the order
matters. Stage 1 measures and blocks nothing, which is what produces the
distribution nobody has observed with live keys. Stage 2 enforces, and is a
config flip a human makes after reading numbers.

These tests pin the two properties that make the staging honest: measure-only
cannot empty the queue, and the enforcing stage tests the Wilson LOWER bound
rather than the point estimate.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import empirical_difficulty as ED  # noqa: E402


def _store():
    from asclepius.store import get_store
    return get_store()


def _measured_task(store, *, value, measured=True, specialty="cardiology"):
    """A task carrying an empirical-difficulty verdict, through the real write
    path so the first-class serving columns are populated the way generation
    populates them."""
    return store.insert_task(
        prompt="a case", specialty=specialty, difficulty="hard",
        generation={"empirical_difficulty": {"value": value, "measured": measured}})


# ═══ Stage 1: measure only ═══════════════════════════════════════════════════
def test_measure_only_never_blocks_serving():
    """WHY: stage 1 has to be a pure soak.

    Its whole purpose is to observe the measured distribution before anything
    depends on it. If a below-floor case stopped serving the moment measurement
    was switched on, stage 1 would BE stage 2, the queue would drain on the
    deploy that was supposed to be observational, and nobody would have the
    numbers the real decision is meant to be made from.
    """
    A.fresh_store()
    store = _store()
    below = _measured_task(store, value=0.1)

    served = store.next_task_for_evaluator(
        evaluator_id="e-soak", specialty="cardiology", hard_only=True)
    assert served is not None, "measurement alone must not gate serving"
    assert served["task_id"] == below["task_id"]
    # And the measurement really is on the row: a soak that measured nothing
    # would pass this test by being a no-op.
    assert bool(store.get_task(below["task_id"])["difficulty_measured"]) is True


# ═══ Stage 2: require measured ═══════════════════════════════════════════════
def test_require_on_refuses_unmeasured_case():
    """WHY: this is the entire point of stage 2.

    A case that only ever carried a DECLARED difficulty from the hardness-judge
    proxy has no evidence a frontier model fails it. Serving one under the
    enforcing flag would sell the claim without the measurement behind it, which
    is the thing diligence asks for and the thing we could not produce.
    """
    A.fresh_store()
    store = _store()
    _measured_task(store, value=0.9, measured=False)   # declared only
    kwargs = dict(evaluator_id="e-strict", specialty="cardiology", hard_only=True)

    assert store.next_task_for_evaluator(**kwargs) is not None, (
        "with the requirement off, a declared case still serves")
    assert store.next_task_for_evaluator(
        require_measured_difficulty=True, min_empirical_difficulty=0.5,
        **kwargs) is None

    # A LIVE-measured case above the floor is what stage 2 does serve, so the
    # refusal above is about measurement and not about the flag refusing
    # everything.
    _measured_task(store, value=0.9, measured=True)
    assert store.next_task_for_evaluator(
        require_measured_difficulty=True, min_empirical_difficulty=0.5,
        **kwargs) is not None


# ═══ The honest number ═══════════════════════════════════════════════════════
def test_gate_uses_wilson_lower_bound_not_point_estimate():
    """WHY: a point estimate from four attempts is not a measurement.

    Two failures in four draws is a point estimate of exactly the floor, and the
    interval around it runs down near 0.15. Shipping that case as frontier-hard
    would be a claim we could not defend in a buyer's diligence, so the gate
    tests the LOWER bound. This test exists because the cheap version of this
    code compares ``value`` and passes.
    """
    calls = {"n": 0}

    async def _answer(model, prompt, image_blocks=None):
        calls["n"] += 1
        return "an answer"

    async def _judge(case, question, model_answer):
        # Alternating verdicts: two of four draws fail, so value is exactly 0.5.
        return {"failed": calls["n"] % 2 == 0, "answer_correct": True,
                "reasoning_sound": calls["n"] % 2 == 1}

    ED._one_frontier_answer = _answer      # noqa: SLF001 - module-level seam
    ED._judge_failure = _judge             # noqa: SLF001
    try:
        import asyncio
        out = asyncio.run(ED.measure_empirical_difficulty(
            {"demographics": {"sex": "F"}}, "what next?",
            models=["m-openai", "m-anthropic"], k=2))
    finally:
        import importlib
        importlib.reload(ED)

    assert out["measured"] is True
    assert out["n_attempts"] == 4 and out["n_failures"] == 2
    assert out["value"] == 0.5, "the point estimate sits exactly on the floor"
    assert out["value_lower"] < 0.5, "the interval on four draws runs well below it"
    assert out["passes_gate"] is False, (
        "the gate must read the lower bound; on the point estimate this case "
        "would ship")


def test_no_frontier_key_degrades_to_unmeasured_rather_than_failing():
    """WHY: PRD A4 -- the graceful degrade is unchanged by the staging.

    With no reachable frontier model the measurement returns ``measured=False``
    and the caller keeps the declared value. Under stage 2 such a case is HELD,
    which is the gate working. What must never happen is generation raising:
    an unreachable provider would then take out case creation entirely.
    """
    async def _no_answer(model, prompt, image_blocks=None):
        return None

    ED._one_frontier_answer = _no_answer   # noqa: SLF001
    try:
        import asyncio
        out = asyncio.run(ED.measure_empirical_difficulty(
            {"demographics": {"sex": "M"}}, "what next?", models=["m"], k=2))
    finally:
        import importlib
        importlib.reload(ED)

    assert out["measured"] is False and out["value"] is None
    assert out["n_attempts"] == 0
    assert "kept declared difficulty" in out["note"]
