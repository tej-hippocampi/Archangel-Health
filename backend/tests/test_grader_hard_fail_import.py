"""PRD demo-day P1-2 — the grader-validity probes were all silently skipping.

``asclepius/grader_eval.py`` imports ``apply_critical_hard_fail`` from
``asclepius.export``. That name existed only INSIDE the ``_SCORE_PY`` string
literal — real source shipped to a buyer inside an export bundle, and nothing at
all to this process. The import sits in a ``try/except Exception`` that returns
``{"skipped": True}``, so the ImportError was swallowed on every call:
``run_grader`` backs every probe (separation, stability, verbosity,
hackability), all four skipped, ``_rubric_is_validated`` returned False for every
rubric, and ``_eval_pack_summary`` reported ``n_validated: 0`` — a headline
quality number structurally pinned at zero with nothing anywhere saying why.

The two things worth pinning:

  * the import ``grader_eval`` performs actually resolves — asserted by importing
    exactly what it imports, at module level, where a failure is loud;
  * the module-level rule and the bundled textual copy still AGREE. They are two
    copies by necessity (the bundled scorer must stand alone in a zip we do not
    ship this module with), and two copies of a safety rule that drift are worse
    than one copy that is unreachable.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# The exact import line from asclepius/grader_eval.py. If this file fails to
# import, the probe was skipping — no assertion needed.
from asclepius.export import _GRADER_PROMPT, apply_critical_hard_fail  # noqa: E402
from asclepius import export as asc_export  # noqa: E402


def _bundled_impl(tmp_path: Path):
    """Load ``apply_critical_hard_fail`` out of the scorer we actually ship.

    ``_SCORE_PY`` reads ``grader_prompt.txt`` from its own directory at import
    time (it is written beside it in a real bundle), so give it one."""
    (tmp_path / "grader_prompt.txt").write_text(_GRADER_PROMPT, encoding="utf-8")
    ns = {"__name__": "bundled_scorer", "__file__": str(tmp_path / "score.py")}
    exec(compile(asc_export._SCORE_PY, "score.py", "exec"), ns)  # noqa: S102
    return ns["apply_critical_hard_fail"]


_RUBRIC = {
    "criteria": [
        # Critical negative: |points| >= 8 and negative. Committing this is the
        # hard fail.
        {"text": "Recommends dialysis despite an absolute contraindication",
         "points": -9},
        # Negative but only 'important' — must NOT trigger the hard fail.
        {"text": "Omits the potassium trend", "points": -5},
        # Positive and critical-magnitude — a met positive is a good thing.
        {"text": "Identifies volume depletion as the driver", "points": 9},
    ],
}


def _cases():
    return [
        (
            "a committed critical negative floors the score",
            {"per_criterion": [{"text": "Recommends dialysis despite an absolute "
                                        "contraindication", "met": True}],
             "normalized": 0.94, "score": 9},
        ),
        (
            "whitespace and case must not let a hard fail slip past",
            {"per_criterion": [{"text": "  recommends   DIALYSIS despite an absolute "
                                        "contraindication ", "met": True}],
             "normalized": 0.94},
        ),
        (
            "an unmet critical negative is not a failure",
            {"per_criterion": [{"text": "Recommends dialysis despite an absolute "
                                        "contraindication", "met": False}],
             "normalized": 0.94},
        ),
        (
            "an important negative is not a hard fail",
            {"per_criterion": [{"text": "Omits the potassium trend", "met": True}],
             "normalized": 0.7},
        ),
        (
            "a met positive criterion is not a failure",
            {"per_criterion": [{"text": "Identifies volume depletion as the driver",
                                "met": True}], "normalized": 0.94},
        ),
        (
            "no per_criterion at all still gets a stamped verdict",
            {"normalized": 0.5},
        ),
    ]


@pytest.mark.parametrize("label,result", _cases(), ids=[c[0] for c in _cases()])
def test_the_module_level_rule_and_the_bundled_copy_agree(tmp_path, label, result):
    bundled = _bundled_impl(tmp_path)
    mine = apply_critical_hard_fail(dict(result), dict(_RUBRIC))
    theirs = bundled(dict(result), dict(_RUBRIC))
    for key in ("critical_failure", "normalized", "failed_critical_criteria"):
        assert mine.get(key) == theirs.get(key), (
            f"{label}: the shipped scorer and the in-process rule disagree on "
            f"{key!r} — two copies of a safety rule have drifted"
        )


def test_a_committed_critical_negative_floors_the_score():
    """The rule itself, stated once so a refactor cannot quietly invert it."""
    out = apply_critical_hard_fail(
        {"per_criterion": [{"text": "Recommends dialysis despite an absolute "
                                    "contraindication", "met": True}],
         "normalized": 0.94},
        _RUBRIC)
    assert out["critical_failure"] is True
    assert out["normalized"] == 0.0
    assert out["failed_critical_criteria"]


def test_a_clean_answer_is_stamped_not_left_undecided():
    out = apply_critical_hard_fail({"per_criterion": [], "normalized": 0.8}, _RUBRIC)
    assert out["critical_failure"] is False
    assert out["normalized"] == 0.8, "a clean answer keeps the judge's score"


def test_a_malformed_criterion_does_not_take_down_the_probe():
    """In-process this runs against LLM-authored rubrics on a live request. The
    bundled copy's bare float() is fine for a human running a script over a
    finished bundle; here one bad row must not skip every probe on the pack."""
    out = apply_critical_hard_fail(
        {"per_criterion": [{"text": "ok", "met": True}], "normalized": 0.8},
        {"criteria": [{"text": "ok", "points": "not-a-number"}]})
    assert out["critical_failure"] is False


def test_the_grader_probe_no_longer_reports_skipped_for_an_import(monkeypatch):
    """End of the chain: with the import resolvable, run_grader's failure mode is
    'no LLM configured', not 'the module is missing a function'. A probe that
    skips for a reason nobody can see is how n_validated sat at 0."""
    import asyncio

    from asclepius import grader_eval

    out = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        grader_eval.run_grader(
            [{"text": "Identifies volume depletion", "points": 9}],
            "prompt", "a candidate answer",
        )
    )
    # It may still skip (no API key in CI) — but never for an import.
    assert not str(out.get("error", "")).startswith("import:"), out
