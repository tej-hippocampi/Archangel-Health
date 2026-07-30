"""Asclepius V4 Build Spec — Phase 5: decisive action (B3).

`supervision.DecisiveAction` existed and `packaging` read `task["decisive_action"]`,
but nothing ever wrote it — so `has_verifiable_outcome` was false on every record and
no preference label could become an RLVR reward. This phase captures the physician-named
verifiable outcome from the submission, persists it on the task, and ships it as the
record's `verifiable_outcome`. Spec Phase 5 tests 26-28 / Audit §13.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import pipeline as asc_pipeline  # noqa: E402
from asclepius import validation as VAL  # noqa: E402

client = TestClient(A.app)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    A.fresh_store()

    async def _ok_critic(task, submission):
        return {"consistent": True, "issues": [], "skipped": True}

    async def _ok_grounding(task, submission):
        return {"grounding_ok": True, "issues": [], "skipped": True, "checked_anchors": 0}

    monkeypatch.setattr(asc_pipeline, "run_critic", _ok_critic)
    monkeypatch.setattr(asc_pipeline, "run_grounding_check", _ok_grounding)
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


def _admin_h():
    return A.headers_for(A.make_user(_store(), role="admin"))


def _evaluator_h(specialty="nephrology"):
    return A.headers_for(A.make_user(_store(), role="evaluator", specialty=specialty,
                                     board_cert="board_certified_nephrology", years_experience=12))


def _task_body(**kw):
    base = {
        "specialty": "nephrology", "difficulty": "hard", "max_labels": 1,
        "prompt": f"Nephrotic-range proteinuria, monoclonal spike — dx? {A.uniq(8)}",
        "candidate_answers": [{"id": "A", "text": "PGNMID after pronase IF."},
                              {"id": "B", "text": "Primary membranous."}],
    }
    base.update(kw)
    return base


def _submit(ev_h, admin_h, *, decisive_action=None, **task_kw):
    tid = client.post("/api/asclepius/tasks", json={"tasks": [_task_body(**task_kw)]},
                      headers=admin_h).json()["created"][0]
    sid = "s-" + uuid.uuid4().hex[:12]
    body = {
        "submission_id": sid, "task_id": tid, "verdict": "A_better",
        "chosen_id": "A", "rejected_id": "B", "time_spent_sec": 130,
        "prompt_review": {"reviewed": True, "verdict": "valid"},
        "independent_answer": {"text": "Monotypic IgG deposits confirmed after pronase-digested "
                               "paraffin immunofluorescence; this is PGNMID, not membranous."},
        "chosen_revision": {"edited": False, "why_better_notes": "names the confirmatory step"},
        "rejected_critique": {"error_tags": ["wrong_diagnosis"], "why_worse": "skips confirmation"},
    }
    if decisive_action is not None:
        body["decisive_action"] = decisive_action
    r = client.post("/api/asclepius/submissions", json=body, headers=ev_h)
    return tid, sid, r


# ── test 26 ───────────────────────────────────────────────────────────────────
def test_decisive_action_roundtrip():
    """Submit a named decisive action → the task stores it, and every packaged record
    ships a `verifiable_outcome` with `supervision_type == 'environment_verifiable'`."""
    admin_h, ev_h = _admin_h(), _evaluator_h()
    tid, sid, r = _submit(ev_h, admin_h, decisive_action={
        "action": "order pronase-digested paraffin immunofluorescence",
        "tool_name": "order_pronase_if",
        "rationale": "unmasks monotypic deposits masked on routine IF",
    })
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "export_ready", r.json()

    # Persisted on the task (written from the submission, not by an admin/model).
    task = _store().get_task(tid)
    da = task.get("decisive_action")
    assert da and da["action"].startswith("order pronase")
    assert da["physician_authored"] is True and da["author_id_hashed"]
    assert da["authored_at"]

    # Every packaged record carries the verifiable outcome + the lifted ladder rung.
    recs = _store().records_for_submission(sid)
    assert recs
    for rec in recs:
        p = rec["payload"]
        assert p["supervision_type"] == "environment_verifiable", p.get("type")
        vo = p.get("verifiable_outcome")
        assert vo and vo["verifier"] == "decisive_action_precedes_final_answer"
        assert vo["action"].startswith("order pronase")
        assert p["has_verifiable_outcome"] is True


# ── test 27 ───────────────────────────────────────────────────────────────────
def test_decisive_action_optional():
    """No decisive action → submission still succeeds, and the record stays on the
    `pairwise_preference` rung (a physician must never be forced to invent one)."""
    admin_h, ev_h = _admin_h(), _evaluator_h()
    tid, sid, r = _submit(ev_h, admin_h, decisive_action=None)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "export_ready", r.json()
    assert _store().get_task(tid).get("decisive_action") in (None, {})
    recs = _store().records_for_submission(sid)
    assert recs
    for rec in recs:
        p = rec["payload"]
        assert p["supervision_type"] == "pairwise_preference"
        assert p.get("has_verifiable_outcome") is False
        assert "verifiable_outcome" not in p


# ── test 27b — a blank action string is treated as "none", not persisted ──────
def test_blank_action_not_persisted():
    admin_h, ev_h = _admin_h(), _evaluator_h()
    tid, sid, r = _submit(ev_h, admin_h, decisive_action={"action": "   ", "tool_name": "x"})
    assert r.status_code == 200 and r.json()["status"] == "export_ready", r.text
    assert _store().get_task(tid).get("decisive_action") in (None, {})


# ── test 28 ───────────────────────────────────────────────────────────────────
def test_decisive_action_rejects_one_word():
    """A one-word action ("labs") is not a verifiable outcome — validation flags it,
    so a fabricated/degenerate decisive action never ships as environment-verifiable."""
    task = {"task_id": "t1", "specialty": "nephrology", "prompt": "dx?",
            "candidate_answers": [{"id": "A", "text": "a"}, {"id": "B", "text": "b"}]}
    submission = {"verdict": "A_better", "chosen_id": "A", "rejected_id": "B",
                  "time_spent_sec": 300,
                  "payload": {"verdict": "A_better", "chosen_id": "A", "rejected_id": "B",
                              "independent_answer": {"text": "a full ideal answer here"},
                              "decisive_action": {"action": "labs"}}}
    res = VAL.validate_submission(task, submission, [])
    assert "decisive_action_underspecified" in res["issues"], res["issues"]
    # A two-word action clears this particular check.
    submission["payload"]["decisive_action"]["action"] = "order labs"
    res2 = VAL.validate_submission(task, submission, [])
    assert "decisive_action_underspecified" not in res2["issues"]
