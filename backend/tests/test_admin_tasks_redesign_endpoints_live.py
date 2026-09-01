"""§2 removed CARDS, not capabilities: every endpoint behind a deleted card is
still reachable.

The removal list is the risky half of this PRD. A card is easy to delete and its
endpoint is easy to delete with it — and the endpoints here are not decoration:
``/tasks`` is how a task file becomes tasks, ``load-gold`` is the only way to
populate a queue without an LLM key, ``seed-corpus`` is the corpus health an
operator reads before a generation run, and ``generation/jobs`` is where a
zero-yield batch explains itself.

So this file asserts the contract §2 actually states — "removed from the UI
(endpoints preserved)" — by CALLING each one. A 404 here means someone deleted a
capability while deleting its card.

It deliberately does not assert on the response body. The point is reachability
and authorization, not behaviour those endpoints' own tests already cover.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402

client = TestClient(A.app)


@pytest.fixture(autouse=True)
def _isolated():
    A.fresh_store()
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


def _admin(store):
    return A.headers_for(A.make_user(store, role="admin"))


#: (method, path) for every endpoint whose CARD §2 removed. A 404/405 is the
#: failure this file exists to catch; a 503 (no LLM key) or a 400 (bad payload)
#: still proves the route is mounted and authorized.
REMOVED_CARD_ENDPOINTS = [
    ("POST", "/api/asclepius/tasks"),                          # Paste tasks (JSON)
    ("POST", "/api/asclepius/tasks/generate"),                 # Generate candidates
    ("GET", "/api/asclepius/generation/seed-corpus"),          # Seed corpus
    ("GET", "/api/asclepius/generation/jobs"),                 # Generation jobs
    ("POST", "/api/asclepius/generation/nephrology/load-gold"),  # Load gold cases
    ("POST", "/api/asclepius/generation/load-v4-real-cases"),  # Load REAL V4 cases
    ("GET", "/api/asclepius/tasks"),                           # the old Tasks table
    ("GET", "/api/asclepius/baselines/model-failures"),        # Frontier-model failures
]


@pytest.mark.parametrize("method,path", REMOVED_CARD_ENDPOINTS)
def test_the_endpoint_behind_a_removed_card_is_still_mounted(method, path):
    store = _store()
    headers = _admin(store)
    res = (client.get(path, headers=headers) if method == "GET"
           else client.post(path, json={}, headers=headers))
    assert res.status_code not in (404, 405), (
        f"{method} {path} is gone — §2 removes the card, never the endpoint")


def test_the_pre_batches_allocator_endpoint_survives_its_removed_screen():
    """``renderAdminAssign`` was already unreachable before this PRD (no caller);
    §2 confirms it stays that way. Its endpoint is the one Batches itself uses,
    so a deletion here would take the new Routing page down with it."""
    store = _store()
    res = client.post("/api/asclepius/admin/assignments/allocate",
                      json={"task_ids": []}, headers=_admin(store))
    assert res.status_code == 400, "reachable and validating, not missing"


def test_upload_file_still_accepts_a_task_file():
    """The Upload modal's fourth mode posts here."""
    store = _store()
    csv = b"prompt,specialty,difficulty,answer_a,answer_b\nq?,nephrology,hard,A,B\n"
    res = client.post("/api/asclepius/tasks/upload-file",
                      files={"file": ("t.csv", csv, "text/csv")},
                      headers=_admin(store))
    assert res.status_code == 200, res.text
    assert res.json()["count"] == 1
