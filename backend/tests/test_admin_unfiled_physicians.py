"""Physicians the console cannot see, because no tab claims their state.

The Physicians screen has two tabs and they do not cover the space between them:
the roster renders ``verification_status == 'approved'`` and the queue renders
``status=pending`` plus mid-wizard signups. An evaluator whose verification was
NEVER DECIDED is on neither.

That is not a cosmetic gap. The roster is how an operator reaches an account at
all, so such a physician cannot be approved, tiered, or sent a real case from the
console — while being perfectly able to sign in, draw synthetic cases and label
them. The doctor sees a working product and the admin sees an empty roster, and
nothing errors on either side. It is the same invisibility
``_misfiled_physicians`` exists for, one column over: that check looks at the
ROLE, which in this case is correct, and the STATUS is what has no home.

Reported from production: an account provisioned directly through the director
onboarding (which mails an access key and creates a working evaluator without
entering the verification queue) was labelling cases and absent from the roster.
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


def _physician(store, *, status, specialty="nephrology", tier="labeler"):
    u = A.make_user(store, specialty=specialty, tier=tier)
    store.set_verification_status(u["id"], status)
    return store.get_user_by_id(u["id"])


def _roster(store):
    return client.get("/api/asclepius/admin/physicians", headers=_admin(store)).json()


# ═══════════════════════════════════════════════════════════════════════════════
# The gap
# ═══════════════════════════════════════════════════════════════════════════════
def test_an_undecided_physician_is_in_neither_tab_and_is_reported():
    """The bug, stated as the operator experiences it: they are not in the
    roster (not approved) and not in the queue (not pending)."""
    store = _store()
    doc = _physician(store, status=None)
    body = _roster(store)

    in_roster_tab = [p for p in body["physicians"]
                     if p["verification_status"] == "approved"]
    assert doc["email"] not in [p["email"] for p in in_roster_tab]

    assert body["unfiled_count"] == 1
    assert body["unfiled_physicians"][0]["email"] == doc["email"]


def test_an_approved_physician_is_not_reported_as_unfiled():
    store = _store()
    _physician(store, status="approved")
    assert _roster(store)["unfiled_count"] == 0


def test_a_pending_physician_is_not_reported_as_unfiled():
    """They have a home — the queue tab — and duplicating them into a banner
    would turn the one screen that means "decide these" into noise."""
    store = _store()
    _physician(store, status="pending")
    assert _roster(store)["unfiled_count"] == 0


def test_a_rejected_physician_is_reported_because_no_tab_shows_them_either():
    """"We decided no" is a thing an operator should be able to see and
    reconsider. The row carries its status so it is never confused with an
    account nobody has looked at."""
    store = _store()
    doc = _physician(store, status="rejected")
    body = _roster(store)
    assert body["unfiled_count"] == 1
    assert body["unfiled_physicians"][0]["verification_status"] == "rejected"


def test_the_row_says_whether_they_have_been_working_while_invisible():
    """A doctor who has labelled thirty cases nobody can see is a different
    problem from an account created and never used."""
    store = _store()
    doc = _physician(store, status=None)
    row = _roster(store)["unfiled_physicians"][0]
    assert row["submissions_total"] == 0
    assert row["specialty"] == "nephrology"
    assert row["tier"] == "labeler"


def test_a_mock_contributor_is_never_reported():
    """The sandbox account is deliberately outside the roster; surfacing it as
    a physician awaiting a decision would be a permanent false positive."""
    store = _store()
    from asclepius import auth as asc_auth

    asc_auth.ensure_mock_contributor(store)
    assert _roster(store)["unfiled_count"] == 0


# ═══════════════════════════════════════════════════════════════════════════════
# The repair, through the route that already exists
# ═══════════════════════════════════════════════════════════════════════════════
def test_approving_from_the_card_puts_them_in_the_roster_as_a_labeler():
    """One action, and the outcome the operator was actually chasing: approved,
    labeling, and cleared for the real-case queue."""
    store = _store()
    doc = _physician(store, status=None)
    res = client.post(
        f"/api/asclepius/admin/physicians/restore?email={doc['email']}",
        json={"approve_verification": True, "tier": "labeler"},
        headers=_admin(store))
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["after"]["verification_status"] == "approved"
    assert out["after"]["tier"] == "labeler"
    assert out["can_label_real_cases"] is True

    body = _roster(store)
    assert body["unfiled_count"] == 0
    approved = [p for p in body["physicians"] if p["verification_status"] == "approved"]
    assert doc["email"] in [p["email"] for p in approved]
    assert approved[0]["real_data_approved"] is True


def test_real_data_approval_is_derived_not_granted_by_the_card():
    """The card must not become a side door around APPROVED + LABELING. It sets
    verification and tier; the policy grants the flag."""
    import inspect

    from routers import asclepius_admin

    src = inspect.getsource(asclepius_admin.restore_physician)
    assert "sync_real_data_approval()" in src
    assert "set_real_data_approved" not in src


# ═══════════════════════════════════════════════════════════════════════════════
# The client renders it above the tabs, for the reason the other card does
# ═══════════════════════════════════════════════════════════════════════════════
def test_the_card_is_rendered_above_the_tabs():
    """An account in here is invisible in BOTH tabs, so a banner inside a tab
    would be hidden by the same bug it reports."""
    js = (Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
          / "admin_physicians.js").read_text()
    assert "function unfiledCard(" in js
    tabs_at = js.index("container.appendChild(tabStrip(")
    mount_at = js.index("const unfiled = unfiledCard(ctx, container);")
    assert mount_at > tabs_at, "must mount after the strip is built"
    # ...and before either tab body renders, so it is visible on both.
    assert mount_at < js.index("renderPendingTab(container")
    assert mount_at < js.index("renderApprovedTab(container")


def test_the_card_repairs_through_the_existing_route_not_a_new_one():
    js = (Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
          / "admin_physicians.js").read_text()
    start = js.index("function unfiledRow(")
    body = js[start:start + 2600]
    assert "'/admin/physicians/restore?email='" in body
    assert "approve_verification: true" in body
    assert "tier: 'labeler'" in body


def test_the_card_says_when_approval_did_not_unblock_real_cases():
    """Approved and still unable to draw a real case is a real outcome (an unset
    specialty, say). Saying nothing would leave the operator to discover it as
    an empty queue on the physician's side."""
    js = (Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
          / "admin_physicians.js").read_text()
    start = js.index("function unfiledRow(")
    assert "can_label_real_cases === false" in js[start:start + 2600]
