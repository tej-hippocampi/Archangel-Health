"""The console role grant: how the second founder becomes an admin.

One endpoint, two roles, and the guards that keep it from being a foot-gun:
admin-gated, self-demotion refused, non-physician roles untouchable, every
change audited.
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


def _grant(actor, target_id, role, expect):
    r = client.post(f"/api/asclepius/admin/users/{target_id}/role",
                    json={"role": role}, headers=A.headers_for(actor))
    assert r.status_code == expect, r.text
    return r


def test_an_admin_promotes_a_physician_and_the_change_is_audited():
    store = get_store()
    admin = A.make_user(store, role="admin")
    doc = A.make_user(store, role="evaluator", specialty="nephrology")
    r = _grant(admin, doc["id"], "admin", 200)
    assert r.json()["role"] == "admin"
    assert store.get_user_by_id(doc["id"])["role"] == "admin"
    events = [e for e in store.list_events(entity_type="user", entity_id=doc["id"])
              if e["event_type"] == "role_changed"]
    assert events and events[-1]["payload"]["to"] == "admin"


def test_a_physician_cannot_grant_roles():
    store = get_store()
    doc = A.make_user(store, role="evaluator", specialty="nephrology")
    other = A.make_user(store, role="evaluator", specialty="nephrology")
    _grant(doc, other["id"], "admin", 403)


def test_self_demotion_is_refused_so_the_console_keeps_an_operator():
    store = get_store()
    admin = A.make_user(store, role="admin")
    _grant(admin, admin["id"], "evaluator", 422)
    assert store.get_user_by_id(admin["id"])["role"] == "admin"


def test_only_physician_and_admin_accounts_move_between_the_two_roles():
    store = get_store()
    admin = A.make_user(store, role="admin")
    buyer = A.make_user(store, role="buyer")
    _grant(admin, buyer["id"], "admin", 422)
    _grant(admin, "u-nope", "admin", 404)
    doc = A.make_user(store, role="evaluator", specialty="nephrology")
    _grant(admin, doc["id"], "superuser", 422)
