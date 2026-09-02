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


def test_a_physician_cannot_read_their_own_score_because_the_route_is_gone():
    """The score is internal. The portal stopped rendering it, but while the
    route existed the number was one curl away from the person it judges, and
    a surface nothing renders is still a surface.

    Asserted for an APPROVED physician, which is the session that used to get
    a 200 here: a 403 would mean the route still exists behind a gate somebody
    could later widen."""
    doc = _physician(tier_score=72)
    r = client.get("/api/asclepius/score", headers=A.headers_for(doc))
    assert r.status_code == 404, r.text


def test_no_physician_reachable_response_carries_the_score_vocabulary():
    """The narrow version of the rule (delete one route) is easy to satisfy
    and easy to undo by adding the number to a payload that already exists.
    The rule is about the number, not the URL, so check the endpoints a
    physician session actually reads."""
    doc = _physician(tier_score=72)
    for path in ("/api/asclepius/me/profile", "/api/asclepius/me/stats",
                 "/api/asclepius/auth/me"):
        r = client.get(path, headers=A.headers_for(doc))
        if r.status_code != 200:
            continue
        blob = r.text.lower()
        for leaked in ("tier_score", "\"band\"", "reviewer band", "labeler band",
                       "contributor_score"):
            assert leaked not in blob, f"{path} leaks {leaked}"


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
