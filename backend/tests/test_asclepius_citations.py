"""Citation library + auto-suggest tests (Seamless PRD WS3)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests._asclepius as A  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from asclepius import citations as C  # noqa: E402
from asclepius.packaging import package_submission  # noqa: E402

client = TestClient(A.app)


def _store():
    from asclepius.store import get_store
    return get_store()


def _ev():
    return A.make_user(A.fresh_store(), role="evaluator", specialty="nephrology")


# ─── Library + retrieval (pure) ───────────────────────────────────────────────
def test_library_loads_and_unknown_specialty_degrades():
    C.clear_cache()
    lib = C.load_library("nephrology")
    assert lib and len(lib) >= 12
    assert all(c.get("id") and c.get("title") for c in lib)
    # A specialty with no library file → None (→ skipped, not an error).
    assert C.load_library("dermatology") is None


@pytest.mark.parametrize("text,expected_id_fragment", [
    ("finerenone starting dose with eGFR 40 and potassium 4.8", "KERENDIA"),
    ("apixaban dose reduction in CKD stage 4 for atrial fibrillation", "ELIQUIS"),
    ("severe hyponatremia Na 108 with seizures, safe correction rate", "Hyponatraemia"),
    ("K+ 6.4 with peaked T-waves on hemodialysis, dialysate bath", "hyperkalemia"),
    ("IgA nephropathy proteinuria sparsentan versus irbesartan", "PROTECT"),
])
def test_retrieval_picks_the_right_source(text, expected_id_fragment):
    top = C.suggest_citations(text, "nephrology", k=2)
    assert top, f"no suggestion for: {text}"
    ids = " ".join((c.get("identifier") or "") + " " + (c.get("id") or "") for c in top)
    assert expected_id_fragment.lower() in ids.lower()


def test_suggestions_expose_only_public_fields():
    top = C.suggest_citations("finerenone eGFR potassium", "nephrology", k=1)
    assert top
    assert set(top[0].keys()) <= {"id", "title", "section", "source_type", "identifier", "url", "snippet"}


def test_ranked_skips_without_library():
    res = asyncio.run(C.suggest_citations_ranked("anything", specialty="dermatology"))
    assert res["skipped"] is True and res["suggestions"] == []


def test_ranked_returns_retrieval_offline():
    # No LLM key in the test env → deterministic retrieval order, not skipped.
    res = asyncio.run(C.suggest_citations_ranked("finerenone eGFR 40 potassium", specialty="nephrology", k=3))
    assert res["skipped"] is False
    assert res["source"] == "retrieval"
    assert 1 <= len(res["suggestions"]) <= 3


# ─── Endpoint ─────────────────────────────────────────────────────────────────
def test_assist_cite_endpoint_returns_suggestions():
    ev_h = A.headers_for(_ev())
    r = client.post("/api/asclepius/assist/cite",
                    json={"text": "finerenone starting dose eGFR 40, potassium 4.8", "specialty": "nephrology"},
                    headers=ev_h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["skipped"] is False
    assert body["suggestions"]
    assert "identifier" in body["suggestions"][0]


def test_assist_cite_degrades_for_unknown_specialty():
    ev_h = A.headers_for(_ev())
    r = client.post("/api/asclepius/assist/cite",
                    json={"text": "some rationale", "specialty": "dermatology"}, headers=ev_h)
    assert r.status_code == 200
    assert r.json()["skipped"] is True


def test_assist_cite_empty_text():
    ev_h = A.headers_for(_ev())
    r = client.post("/api/asclepius/assist/cite", json={"text": "   "}, headers=ev_h)
    assert r.status_code == 200
    assert r.json()["suggestions"] == []


def test_assist_cite_requires_auth():
    assert client.post("/api/asclepius/assist/cite", json={"text": "x"}).status_code == 401


# ─── Packaging carries the confirmed citation through ─────────────────────────
def test_confirmed_citation_rides_the_record():
    task = {
        "task_id": "t1", "specialty": "nephrology", "difficulty": "hard", "source": "lab_supplied",
        "prompt": "Finerenone add-on dosing?",
        "candidate_answers": [{"id": "A", "text": "10 mg"}, {"id": "B", "text": "20 mg"}],
    }
    anchor = {
        "citation_text": "KERENDIA label — start 10 mg at eGFR 25–<60",
        "source_type": "fda_label", "identifier": "FDA Label — KERENDIA (finerenone)",
        "url": "https://example.org/kerendia", "citation_confirmed": True,
    }
    submission = {
        "submission_id": "s1", "task_id": "t1", "verdict": "A_better",
        "chosen_id": "A", "rejected_id": "B", "confidence": "high", "created_at": "2026-07-07T00:00:00",
        "annotator": {"id_hashed": "x", "credentials": "board_certified_nephrology"},
        "payload": {
            "verdict": "A_better", "chosen_id": "A", "rejected_id": "B", "portal_version": "v3",
            "independent_answer": {"text": "start 10 mg", "kind": "instinct"},
            "chosen_revision": {"edited": True, "revised_text": "Start 10 mg", "why_better_notes": "eGFR-appropriate", "evidence_anchor": anchor},
            "rejected_critique": {"error_tags": ["dosing_error"]},
        },
    }
    recs = package_submission(task, submission)
    pref = [r for r in recs if r["type"] == "preference"][0]
    assert pref["grounded"] is True
    assert pref["evidence_anchor"]["citation_confirmed"] is True
    assert pref["evidence_anchor"]["url"] == "https://example.org/kerendia"
