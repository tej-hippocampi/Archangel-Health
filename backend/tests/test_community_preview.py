"""An applicant sees what the community looks like, and never sees the community.

The founders asked for an applicant to be able to look around the product while
their credentials are checked, and named the community as part of that. The
codebase argues, at length and correctly, that they must not be admitted to it:
those rooms are worth reading precisely because everyone in them is a
credential-verified clinician, an account that has done nothing but submit a
form is not that yet, and rejecting the application afterwards does not unread
the messages.

Both things are true at once here. The real gate is untouched, and what an
applicant gets is a FIXTURE rendered through the real interface.

These tests exist to hold the seam shut in both directions: the fixture must not
be able to reach real data, and the real rooms must stay refused.
"""

from __future__ import annotations

import inspect

import pytest
from fastapi.testclient import TestClient

from tests._asclepius import app, fresh_store, headers_for, make_user

from asclepius import capabilities as asc_caps
from asclepius import community_preview as preview


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _applicant(store):
    user = make_user(store, role="evaluator")
    store.set_verification_status(user["id"], "pending")
    return user


def test_an_applicant_gets_the_preview(client):
    store = fresh_store()
    res = client.get("/api/asclepius/community/preview",
                     headers=headers_for(_applicant(store)))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["preview"] is True
    assert body["can_post"] is False
    assert [c["slug"] for c in body["channels"]]
    assert body["messages"]


def test_the_preview_says_it_is_a_preview_and_cannot_be_dismissed(client):
    """The one way this feature could mislead somebody is a reader taking these
    for real colleagues, so the banner is part of the payload rather than
    something the client chooses to render."""
    store = fresh_store()
    body = client.get("/api/asclepius/community/preview",
                      headers=headers_for(_applicant(store))).json()
    assert "Preview" in body["banner"]
    assert "not real colleagues" in body["banner"]


def test_an_approved_physician_is_not_offered_a_fixture(client):
    """They have the real thing one tab away. Handing a verified colleague
    invented conversations is a way to make them doubt everything else on the
    screen."""
    store = fresh_store()
    approved = make_user(store, role="evaluator")
    store.set_verification_status(approved["id"], "approved")
    res = client.get("/api/asclepius/community/preview", headers=headers_for(approved))
    assert res.status_code == 404, res.text


def test_the_real_community_is_still_refused_to_an_applicant(client):
    """The guardrail for the whole feature. If this ever goes green, the
    preview has leaked into the gate it was built to avoid touching."""
    store = fresh_store()
    user = _applicant(store)
    for path in ("/api/community/me", "/api/community/channels"):
        res = client.get(path, headers=headers_for(user))
        assert res.status_code == 403, f"{path} admitted an applicant: {res.text}"


def test_an_applicant_holds_no_community_surface():
    """Read off the policy table, not off a route, so a new community endpoint
    added tomorrow cannot quietly be open to them."""
    store = fresh_store()
    user = store.get_user_by_id(_applicant(store)["id"])
    assert not asc_caps.can_surface(user, asc_caps.COMMUNITY_READ)
    assert not asc_caps.can_surface(user, asc_caps.COMMUNITY_WRITE)


def test_the_preview_module_cannot_reach_a_database():
    """Structural, not careful.

    The module is a pure fixture with no store import, so it is not "written so
    as not to leak real messages", it is incapable of leaking them. If this
    fails, the feature has become something else and wants reconsidering rather
    than rewiring.
    """
    src = inspect.getsource(preview)
    for forbidden in ("get_store", "sqlite3", "_cstore", "from community",
                      "import store", "conn.execute"):
        assert forbidden not in src, f"community_preview reaches {forbidden}"


def test_the_fixture_names_are_obviously_illustrative():
    """A plausible-looking roster of physicians is the failure mode. Initialled
    surnames read as examples; "Dr. Rachel Kessler, Lakeshore Nephrology" would
    read as a colleague."""
    for member in preview.preview_payload()["members"]:
        name = member["display_name"]
        assert name.startswith(("Dr. ", "Archangel")), name
        if name.startswith("Dr. "):
            first = name.split()[1]
            assert first.endswith("."), f"{name} reads as a real person's name"
