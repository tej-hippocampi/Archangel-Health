"""PRD-SCORE — the score endpoints hold their boundaries.

The physician endpoint is session-scoped by route shape (no id parameter
exists to tamper with); the admin endpoint requires the admin role and 404s
on an unknown physician rather than inventing one.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius.store import get_store  # noqa: E402

client = TestClient(A.app)


@pytest.fixture(autouse=True)
def _isolated():
    A.fresh_store()
    yield


def _physician(tier_score=72):
    store = get_store()
    u = A.make_user(store, role="evaluator", specialty="nephrology")
    with store._conn() as conn:
        conn.execute(
            "UPDATE users SET verification_status = 'approved', tier = 'labeler', "
            "tier_score = ? WHERE id = ?", (tier_score, u["id"]))
    return store.get_user_by_id(u["id"])


def test_a_physician_reads_their_own_score_with_bands_and_vocabulary():
    doc = _physician(tier_score=72)
    r = client.get("/api/asclepius/score", headers=A.headers_for(doc))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["score"] == 72.0
    assert body["band"] == "Reviewer band"
    assert body["bands"] == {"reviewer": 70, "labeler": 30}
    assert body["in_review"] is False
    assert body["n_cases"] == 0


def test_a_pending_physician_sees_the_in_review_state():
    store = get_store()
    u = A.make_user(store, role="evaluator", specialty="nephrology")
    with store._conn() as conn:
        conn.execute("UPDATE users SET verification_status = 'pending', "
                     "tier = NULL WHERE id = ?", (u["id"],))
    doc = store.get_user_by_id(u["id"])
    r = client.get("/api/asclepius/score", headers=A.headers_for(doc))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["in_review"] is True
    assert 0 <= body["score"] <= 100


def test_the_admin_endpoint_is_admin_only_and_404s_on_unknowns():
    doc = _physician()
    admin = A.make_user(get_store(), role="admin")
    assert client.get(f"/api/asclepius/admin/scores/{doc['id']}",
                      headers=A.headers_for(doc)).status_code == 403
    ok = client.get(f"/api/asclepius/admin/scores/{doc['id']}",
                    headers=A.headers_for(admin))
    assert ok.status_code == 200
    assert "history" in ok.json()
    assert client.get("/api/asclepius/admin/scores/u-nope",
                      headers=A.headers_for(admin)).status_code == 404
