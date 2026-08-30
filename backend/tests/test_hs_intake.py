"""Intake: what a health system tells us about itself, and when we ask.

The gate is ``intake_at IS NULL AND approval_status = 'pending'``. The second
clause is the whole reason there is a test file: without it, every hospital
provisioned before intake existed gets ambushed by a form on its next login,
having already told us all of this on a call months ago.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402

PASSWORD = "harbor-thistle-meadow-41"

ANSWERS = {
    "organization": "St Mary's Health, and I run the data platform team.",
    "size_type": "900 beds across 4 sites",
    "data_held": "Epic, about 12 years of nephrology encounters with labs and outcomes.",
    "licensable": "Not sure yet, probably the nephrology cohort first.",
    "timeline": "Budgeted for next year.",
}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    A.fresh_store()
    monkeypatch.setenv("ENV", "test")
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


def _account(approval_status, name=None):
    store = _store()
    uname = "hs" + uuid.uuid4().hex[:10]
    hs = store.create_health_system_unclaimed(name or ("Intake Test " + uname))
    store.create_hs_portal_user(username=uname, hs_id=hs["hs_id"], password=PASSWORD,
                                email="it@stmarys.org", must_reset=False,
                                full_name="Dana Reyes", approval_status=approval_status)
    client = TestClient(A.app, base_url="https://testserver")
    client.post("/api/asclepius/hs/login", json={"username": uname, "password": PASSWORD})
    return client, uname, hs


# ─── When we ask ─────────────────────────────────────────────────────────────

def test_a_new_signup_is_routed_into_intake_and_out_again():
    client, uname, hs = _account("pending")
    assert client.get("/api/asclepius/hs/me").json()["intake_needed"] is True
    r = client.post("/api/asclepius/hs/intake", json=ANSWERS)
    assert r.status_code == 200, r.text
    assert client.get("/api/asclepius/hs/me").json()["intake_needed"] is False


def test_an_existing_partner_is_never_ambushed_by_the_form():
    """approval_status NULL is every hospital that predates this. They have no
    intake row either, so a gate on intake_at alone would put a questionnaire in
    front of a partner who has been uploading for months."""
    client, uname, hs = _account(None)
    me = client.get("/api/asclepius/hs/me").json()
    assert _store().get_health_system(hs["hs_id"])["intake_at"] is None
    assert me["intake_needed"] is False
    # They can still reach it voluntarily; they are just never made to.
    assert "intake" in me["surfaces"]
    assert client.get("/api/asclepius/hs/intake").status_code == 200


# ─── What gets stored ────────────────────────────────────────────────────────

def test_answers_are_stored_and_the_gate_is_stamped_together():
    client, uname, hs = _account("pending")
    client.post("/api/asclepius/hs/intake", json=ANSWERS)
    store = _store()
    rows = store.list_hs_intake(hs["hs_id"])
    assert len(rows) == 1
    assert rows[0]["answers"]["data_held"].startswith("Epic")
    assert rows[0]["username"] == uname
    # Same write, per the C-5.5 lesson: a split would let a caller read
    # intake_at still NULL and route them back into the form they just filled.
    assert store.get_health_system(hs["hs_id"])["intake_at"] == rows[0]["submitted_at"]


def test_submitting_again_appends_rather_than_overwriting():
    """Not health_systems.notes, which has no author and no timestamp.
    Overwriting free text a partner wrote destroys evidence."""
    client, uname, hs = _account("pending")
    client.post("/api/asclepius/hs/intake", json=ANSWERS)
    client.post("/api/asclepius/hs/intake",
                json={**ANSWERS, "data_held": "Correction: Cerner, not Epic."})
    rows = _store().list_hs_intake(hs["hs_id"])
    assert len(rows) == 2
    held = {r["answers"]["data_held"] for r in rows}
    assert any(v.startswith("Epic") for v in held), "the first answer was destroyed"
    assert any(v.startswith("Correction") for v in held)


def test_a_field_the_client_invents_is_dropped():
    """The explicit model is the control: an invented field must not reach
    storage, whatever a caller decides to send."""
    client, uname, hs = _account("pending")
    r = client.post("/api/asclepius/hs/intake",
                    json={**ANSWERS, "internal_note": "x", "is_admin": True})
    assert r.status_code == 200
    stored = _store().list_hs_intake(hs["hs_id"])[0]["answers"]
    assert "internal_note" not in stored and "is_admin" not in stored
    assert set(stored) == set(ANSWERS)


def test_the_two_required_answers_are_required():
    client, uname, hs = _account("pending")
    for missing in ("organization", "data_held"):
        payload = {**ANSWERS, missing: "   "}
        assert client.post("/api/asclepius/hs/intake", json=payload).status_code == 400
    # ...and the optional three genuinely are optional.
    r = client.post("/api/asclepius/hs/intake",
                    json={"organization": "St Mary's", "data_held": "Epic"})
    assert r.status_code == 200


def test_long_answers_are_truncated_rather_than_refused():
    """Someone pasting a data dictionary into a textarea should not lose the
    submission over it."""
    client, uname, hs = _account("pending")
    r = client.post("/api/asclepius/hs/intake",
                    json={**ANSWERS, "data_held": "E" * 9000})
    assert r.status_code == 200
    assert len(_store().list_hs_intake(hs["hs_id"])[0]["answers"]["data_held"]) == 4000


# ─── The prompts themselves ──────────────────────────────────────────────────

def test_the_prompts_are_server_owned_and_say_nothing_they_should_not():
    """Copy shown to a partner lives here so one grep covers it. The static
    isolation test scans this file too; this catches it with a clearer message."""
    client, uname, hs = _account("pending")
    body = client.get("/api/asclepius/hs/intake").json()
    keys = [p["key"] for p in body["prompts"]]
    assert keys == ["organization", "size_type", "data_held", "licensable", "timeline"]
    blob = " ".join(f"{p['label']} {p['placeholder']}" for p in body["prompts"]).lower()
    for word in ("purpose", "broker", "brokering", "task_creation"):
        assert word not in blob, f"an intake prompt says {word!r}"


def test_the_admin_can_read_what_they_told_us():
    client, uname, hs = _account("pending")
    client.post("/api/asclepius/hs/intake", json=ANSWERS)
    admin = A.make_user(_store(), role="admin")
    r = TestClient(A.app).get("/api/asclepius/admin/health-system-signups",
                              headers=A.headers_for(admin))
    assert r.status_code == 200
    row = next(p for p in r.json()["pending"] if p["username"] == uname)
    assert row["intake"][0]["answers"]["data_held"].startswith("Epic")
    assert row["full_name"] == "Dana Reyes"


# ─── Latency ─────────────────────────────────────────────────────────────────

def test_the_founder_alert_is_dispatched_in_the_background_not_awaited():
    """This route sits behind the portal time budget, and a mail round trip is
    several times it. Awaiting the send would make response time a function of
    whether email is configured, which is a differential this whole file exists
    to avoid.

    Asserted structurally rather than on the clock: TestClient drains background
    tasks inside the response cycle, so wall time cannot tell the two apart and
    a timing assertion here would pass whether or not the bug was present.
    """
    import inspect
    import typing

    from routers import asclepius_provider as P

    src = inspect.getsource(P.hs_intake_post)
    assert "background.add_task(_notify_hs_intake" in src, \
        "the intake alert is no longer dispatched as a background task"
    assert "await _notify_hs_intake" not in src
    assert "await send_html_email" not in src, \
        "a mail round trip was moved inside the handler"
    # The route must still be handed a BackgroundTasks to dispatch onto.
    # The module uses `from __future__ import annotations`, so signature()
    # yields strings; resolve them rather than comparing to the class.
    from fastapi import BackgroundTasks
    hints = typing.get_type_hints(P.hs_intake_post)
    assert BackgroundTasks in hints.values(), \
        "the route no longer receives a BackgroundTasks"


def test_the_alert_actually_fires_and_carries_the_answers(monkeypatch):
    seen = {}

    def _capture(store, portal_user, answers):
        seen["org"] = portal_user["health_system"]["name"]
        seen["answers"] = answers

    from routers import asclepius_provider as P
    monkeypatch.setattr(P, "_notify_hs_intake", _capture)

    client, uname, hs = _account("pending")
    assert client.post("/api/asclepius/hs/intake", json=ANSWERS).status_code == 200
    assert seen["answers"]["data_held"].startswith("Epic")
    assert seen["org"] == hs["name"]
