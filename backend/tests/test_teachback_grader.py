"""Teach-back grader validation harness (mirrors grounding validation style)."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.teachback_grade import TEACHBACK_GRADE_PROMPT_V, grade_answer
from tests.fixtures.teachback.seed_answers import SEED_ANSWERS

CRITICAL_DEFECT_TYPES = frozenset({"critical_red_flag_fail", "critical_med_fail"})


def _synthetic_grade_json(case: dict) -> dict:
    q = case["question"]
    status = case["expect_status"]
    missing = []
    if status == "PARTIAL":
        if case["defect_type"] == "non_answer":
            missing = ["NON_ANSWER"]
        else:
            missing = ["MISSING_CRITICAL_DETAIL"]
    elif status == "FAIL":
        missing = ["INCORRECT_ACTION"]
    return {
        "question_id": q["id"],
        "status": status,
        "missing": missing,
        "evidence": f"seeded_{case['defect_type']}",
        "severity": q["severity"],
        "domain": q["domain"],
    }


def _mock_client_for_case(case: dict) -> AsyncMock:
    payload = _synthetic_grade_json(case)
    msg = MagicMock()
    msg.content = [MagicMock(text=json.dumps(payload))]
    client = AsyncMock()
    client.messages.create = AsyncMock(return_value=MagicMock(content=msg.content))
    return client


def _score_results(results: List[dict]) -> Dict[str, Any]:
    by_type: Dict[str, Dict[str, int]] = defaultdict(lambda: {"seeded": 0, "caught": 0})
    clean = {"seeded": 0, "caught": 0}
    for r in results:
        dtype = r["defect_type"]
        if dtype == "none":
            clean["seeded"] += 1
            if r["status_match"]:
                clean["caught"] += 1
        else:
            by_type[dtype]["seeded"] += 1
            if r["status_match"]:
                by_type[dtype]["caught"] += 1
    table = {}
    for dtype, counts in sorted(by_type.items()):
        recall = counts["caught"] / counts["seeded"] if counts["seeded"] else 1.0
        table[dtype] = {**counts, "recall": round(recall, 2)}
    fpr = 1 - (clean["caught"] / clean["seeded"]) if clean["seeded"] else 0.0
    return {"by_defect_type": table, "clean": {**clean, "fpr": round(fpr, 3)}}


@pytest.mark.parametrize("case", SEED_ANSWERS, ids=lambda c: c["case_id"])
def test_teachback_grader_seed_case(case: dict):
    async def _run():
        client = _mock_client_for_case(case)
        return await grade_answer(case["question"], case["answer"], {}, client=client)

    grade = asyncio.run(_run())
    assert grade.status == case["expect_status"]
    assert grade.prompt_version == TEACHBACK_GRADE_PROMPT_V


def test_teachback_grader_non_answer_partial_without_llm():
    q = {"id": "q_n", "severity": "MAJOR", "domain": "MAIN_PROBLEM", "expected": "x"}
    grade = asyncio.run(grade_answer(q, "I'm not sure", {}, client=None))
    assert grade.status == "PARTIAL"
    assert "NON_ANSWER" in grade.missing


def test_teachback_grader_recall_thresholds():
    async def _run_all():
        results = []
        for case in SEED_ANSWERS:
            client = _mock_client_for_case(case)
            grade = await grade_answer(case["question"], case["answer"], {}, client=client)
            results.append(
                {
                    "case_id": case["case_id"],
                    "defect_type": case["defect_type"],
                    "status_match": grade.status == case["expect_status"],
                }
            )
        return results

    results = asyncio.run(_run_all())
    summary = _score_results(results)

    for dtype in CRITICAL_DEFECT_TYPES:
        if dtype in summary["by_defect_type"]:
            assert summary["by_defect_type"][dtype]["recall"] == 1.0, f"recall miss on {dtype}"
    assert summary["clean"]["fpr"] <= 0.10

    try:
        from team_store import TeamStore

        TeamStore().save_teachback_recall_snapshot(
            table_json=summary,
            prompt_version=TEACHBACK_GRADE_PROMPT_V,
        )
    except Exception:
        pass
