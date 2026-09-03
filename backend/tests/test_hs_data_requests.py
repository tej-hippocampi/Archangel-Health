"""Data-request broadcasts, and the admin's read of the partner leads.

Two features, one file, because they answer the same operator question from the
two ends: what did we ask partners for, and who wrote in asking us.

The through-line for the broadcast half is that WHO HEARS IT is a property of
the organization's paperwork, not of anyone's memory. Every gate is asserted
from the outside, through the HTTP surface a real operator and a real partner
use, following ``test_hs_onboarding.py``: the account's surface and the
organization's state are separate objects and the response is the only place
they are guaranteed to compose.
"""
from __future__ import annotations

import base64
import hashlib
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402

API = "/api/asclepius"
_KEY = base64.urlsafe_b64encode(b"hs-data-request-test-key-32-byte").decode()
PASSWORD = "harbor-thistle-meadow-41"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    A.fresh_store()
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))
    monkeypatch.setenv("ASCLEPIUS_ASSET_STORE", str(tmp_path / "assets"))
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", _KEY)
    monkeypatch.setenv("ENV", "test")
    # The portal's fixed response-time budget makes every request take at least
    # 120 ms, which is right in production and minutes of nothing here.
    monkeypatch.setenv("ASCLEPIUS_PORTAL_BUDGET_MS", "0")
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


def _client() -> TestClient:
    return TestClient(A.app, base_url="https://testserver")


def _admin_headers(store):
    return A.headers_for(A.make_user(store, role="admin"))


class _Mailbox(list):
    """The letters a drain produced, plus the knob that makes the transport
    refuse. Failure is induced at the TRANSPORT rather than by calling the
    store's mark-failed method, because what the failure path has to get right
    is what the drain does with a refusal, not what the store records."""

    def __init__(self):
        super().__init__()
        self.ok = True
        self.reason = "mailbox unavailable"


@pytest.fixture()
def sent(monkeypatch):
    box = _Mailbox()

    async def _fake(to, subject, body, **kw):
        if not box.ok:
            return False, box.reason
        box.append({"to": to, "subject": subject, "body": body})
        return True, None

    monkeypatch.setattr("email_utils.send_html_email_with_reason", _fake,
                        raising=False)
    return box


# ─── Organizations, in each of the five states ──────────────────────────────
def _make_org(*, state: str, members=("dana@example.org",), active=True):
    """A health system with portal members, parked in one onboarding state.

    Built through the store rather than by walking the signup flow: this file is
    about who hears a broadcast, and five signup walks per test would make the
    thing under test the slowest part of the setup. ``state=None`` produces the
    LEGACY row (NULL onboarding_state), which is a real shape in production and
    the one the collapse exists for.
    """
    store = _store()
    name = f"Test Health {uuid.uuid4().hex[:6]}"
    hs = store.create_health_system_unclaimed(name)
    if state is not None:
        with store._conn() as conn:
            conn.execute("UPDATE health_systems SET onboarding_state = ? "
                         "WHERE hs_id = ?", (state, hs["hs_id"]))
    if not active:
        with store._conn() as conn:
            conn.execute("UPDATE health_systems SET active = 0 WHERE hs_id = ?",
                         (hs["hs_id"],))
    for email in members:
        store.create_hs_portal_user(
            username=f"{uuid.uuid4().hex[:10]}", hs_id=hs["hs_id"],
            password=PASSWORD, email=email, must_reset=False,
            approval_status="approved")
    return store.get_health_system(hs["hs_id"])


def _sign_in(hs_id) -> TestClient:
    """A portal session for the first member of an organization."""
    store = _store()
    user = [u for u in store.list_hs_portal_users(hs_id) if u.get("active")][0]
    client = _client()
    r = client.post(f"{API}/hs/login",
                    json={"username": user["username"], "password": PASSWORD})
    assert r.status_code == 200, r.text
    return client


def _create_request(client, store, **overrides):
    body = {"title": "100 nephrology cases", "specialty": "nephrology",
            "case_count": 100, "due_date": "2026-10-01",
            "details": "CKD stage 4 and 5, with labs."}
    body.update(overrides)
    return client.post(f"{API}/admin/hs-requests", json=body,
                       headers=_admin_headers(store))


def _drain():
    from asclepius import hs_request_notify
    return hs_request_notify.drain_outbox(_store())


# ════════════════════════════════════════════════════════════════════════════
#  §1, §7 — who hears a broadcast
# ════════════════════════════════════════════════════════════════════════════
def test_a_broadcast_reaches_every_member_of_every_active_org_and_nobody_else(sent):
    """The gate is the organization's paperwork, and it is the whole feature.

    A partner in intake, in review, or holding an unsigned agreement has not
    licensed us anything. Asking them for patient data is asking them to do
    something the contract does not yet permit, and an operator who sends one
    broadcast must not have to remember which of forty partners have signed.

    Every member is mailed, not one contact per organization: the person who
    signed the agreement is often not the person who can pull the cases.
    """
    store = _store()
    active = _make_org(state="active", members=("a1@x.org", "a2@x.org"))
    legacy = _make_org(state=None, members=("legacy@x.org",))
    for suppressed in ("intake", "submitted", "approved_awaiting_dla", "declined"):
        _make_org(state=suppressed, members=(f"{suppressed}@x.org",))

    client = _client()
    r = _create_request(client, store)
    assert r.status_code == 200, r.text
    assert r.json()["recipients"] == 3

    _drain()
    heard = {m["to"] for m in sent}
    assert heard == {"a1@x.org", "a2@x.org", "legacy@x.org"}
    assert active["hs_id"] and legacy["hs_id"]


def test_the_legacy_null_state_org_is_broadcast_to():
    """The NULL collapse is load-bearing and it is easy to break by accident.

    An organization provisioned before the state machine existed has a NULL
    ``onboarding_state`` and has in several cases been uploading for months.
    Reading NULL as anything but ACTIVE would silently stop asking our oldest
    partners for data, and nothing would report that it had happened.
    """
    from asclepius import hs_request_notify

    store = _store()
    legacy = _make_org(state=None, members=("legacy@x.org",))
    eligible = {hs["hs_id"] for hs in hs_request_notify.eligible_health_systems(store)}
    assert legacy["hs_id"] in eligible


def test_a_deactivated_org_hears_nothing_even_in_the_active_state(sent):
    """``health_systems.active`` is the operator's revocation switch and the
    state machine is the paperwork. Both have to hold: an organization we have
    switched off must not keep receiving requests because its last recorded
    state was ACTIVE."""
    store = _store()
    _make_org(state="active", members=("off@x.org",), active=False)
    _make_org(state="active", members=("on@x.org",))

    _create_request(_client(), store)
    _drain()
    assert {m["to"] for m in sent} == {"on@x.org"}


def test_a_member_without_an_email_address_is_skipped_not_crashed(sent):
    """Portal accounts may carry a NULL email: the address is optional on the
    row and an operator-provisioned account predates self-signup. One such
    member must not take the rest of the broadcast down with it."""
    store = _store()
    hs = _make_org(state="active", members=("real@x.org",))
    store.create_hs_portal_user(username="noaddress", hs_id=hs["hs_id"],
                                password=PASSWORD, email=None, must_reset=False,
                                approval_status="approved")

    r = _create_request(_client(), store)
    assert r.json()["recipients"] == 1
    _drain()
    assert {m["to"] for m in sent} == {"real@x.org"}


# ════════════════════════════════════════════════════════════════════════════
#  §2, §3 — the outbox: idempotency and per-row defensiveness
# ════════════════════════════════════════════════════════════════════════════
def test_rebroadcasting_a_request_enqueues_nothing_new(sent):
    """The idempotency key is the arbiter, not a flag on the request.

    The console's obvious failure mode is a second click on a button whose
    first click took a second to answer, and the cost of getting this wrong is
    every partner receiving the same letter twice.
    """
    from asclepius import hs_request_notify

    store = _store()
    _make_org(state="active", members=("a@x.org", "b@x.org"))
    request_id = _create_request(_client(), store).json()["request"]["id"]

    again = hs_request_notify.enqueue_for_request(store, request_id=request_id)
    assert again == 0
    assert len(store.list_hs_request_outbox(request_id)) == 2

    _drain()
    assert len(sent) == 2
    # And a drain after everything is sent sends nothing a second time: `sent`
    # rows are not pending, which is the property that makes the loop safe to
    # run on a tick forever.
    assert _drain() == (0, 0)
    assert len(sent) == 2


def test_the_same_address_at_two_organizations_hears_once_per_organization(sent):
    """The key is (request, organization, recipient) rather than (request,
    recipient), because each organization is separately being asked and one
    person may hold an account at two of them. Collapsing to the address would
    silently drop the second organization's ask."""
    store = _store()
    _make_org(state="active", members=("shared@x.org",))
    _make_org(state="active", members=("shared@x.org",))

    assert _create_request(_client(), store).json()["recipients"] == 2
    _drain()
    assert [m["to"] for m in sent] == ["shared@x.org", "shared@x.org"]


def test_a_failed_send_marks_its_own_row_and_leaves_the_batch_alone(sent):
    """Per-row defensiveness is why the outbox exists.

    A broadcast that stopped at the first bad address would under-deliver a
    request the operator believes went out, and the two states look identical
    from the console. So a failure is recorded ON the row, the rest of the batch
    continues, and a later drain does not resend anything already sent.
    """
    store = _store()
    _make_org(state="active", members=("a@x.org", "b@x.org"))
    request_id = _create_request(_client(), store).json()["request"]["id"]

    sent.ok = False
    assert _drain() == (0, 2)
    assert not sent
    rows = store.list_hs_request_outbox(request_id)
    assert {r["status"] for r in rows} == {"failed"}
    assert all("mailbox unavailable" in (r["last_error"] or "") for r in rows)

    # A failed row is terminal for the automatic loop: it is not pending, so the
    # next tick does not hammer a mailbox that just refused us. Recovering it is
    # an operator's decision, and the row holds the reason they need to make it.
    sent.ok = True
    assert _drain() == (0, 0)
    assert not sent


def test_a_drain_whose_request_vanished_fails_the_row_rather_than_looping(sent):
    """A pending row pointing at a deleted request has no letter to write, and
    leaving it pending would retry it on every tick for the life of the
    deployment. It fails once, with the reason, and stops."""
    store = _store()
    _make_org(state="active", members=("a@x.org",))
    request_id = _create_request(_client(), store).json()["request"]["id"]
    with store._conn() as conn:
        conn.execute("DELETE FROM hs_data_requests WHERE id = ?", (request_id,))

    assert _drain() == (0, 1)
    assert not sent
    row = store.list_hs_request_outbox(request_id)[0]
    assert row["status"] == "failed"
    assert "no longer exists" in row["last_error"]


def test_the_admin_request_sends_nothing_inline(sent):
    """Requirement 3, asserted as an absence: creating a request must not send.

    A thousand emails inline holds the console open for minutes and loses the
    tail on any restart, which is the entire reason there is an outbox rather
    than a loop over ``send_html_email``.
    """
    store = _store()
    _make_org(state="active", members=("a@x.org", "b@x.org"))
    assert _create_request(_client(), store).json()["recipients"] == 2
    assert not sent, "the admin request sent mail inline"


def test_the_broadcast_drains_from_the_existing_loop_not_a_second_timer():
    """Structural, and worth asserting: two loops is two things that can
    silently stop.

    The second one stopping would be invisible until an operator asked why a
    broadcast nobody received had produced no replies, so the new outbox drains
    on the same tick as the task-notify one rather than starting its own timer.
    A DOM-free test cannot observe a running loop; it can observe that only one
    was written, which is the same guarantee one step earlier.
    """
    src = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
    start = src.index("def _start_asclepius_task_notify_loop")
    body = src[start:src.index("\n@app.on_event(\"shutdown\")", start)]
    assert "hs_request_notify.drain_outbox" in body
    assert "task_notify.drain_outbox" in body
    # One timer inside that function, not two: the new drain rides the tick
    # that already exists rather than starting a second one beside it.
    assert body.count("asyncio.create_task") == 1


def test_the_letter_says_several_partners_may_answer(sent):
    """A request that reads as exclusive turns an invitation into a race: the
    first partner to see it treats a reply as a claim and the second does not
    bother. The copy has to say the opposite out loud."""
    store = _store()
    _make_org(state="active", members=("a@x.org",))
    _create_request(_client(), store)
    _drain()

    body = sent[0]["body"]
    assert "Several partners" in body
    assert "confirms what we accept" in body
    assert "100 cases" in body and "Nephrology" in body
    assert sent[0]["subject"] == "Data request: 100 nephrology cases"


def test_a_title_cannot_carry_markup_into_a_partners_inbox(sent):
    """``_h1`` does not escape, by design, so every caller that interpolates a
    typed value must. The title is typed by an admin and lands in the headline
    of a letter sent to every partner."""
    store = _store()
    _make_org(state="active", members=("a@x.org",))
    _create_request(_client(), store,
                    title="<script>alert(1)</script>",
                    details="<img src=x onerror=1>")
    _drain()

    body = sent[0]["body"]
    assert "<script>" not in body
    assert "&lt;script&gt;" in body
    assert "<img src=x" not in body


# ════════════════════════════════════════════════════════════════════════════
#  §4, §7 — the portal list
# ════════════════════════════════════════════════════════════════════════════
def test_an_active_partner_sees_open_requests_and_the_copy_that_matters():
    """The list is scoped by SESSION and takes no identifier, the same property
    ``/hs/payouts`` holds: there is deliberately no ``/hs/requests/{hs_id}``."""
    store = _store()
    hs = _make_org(state="active")
    _create_request(_client(), store)

    portal = _sign_in(hs["hs_id"])
    r = portal.get(f"{API}/hs/requests")
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data["requests"]) == 1
    entry = data["requests"][0]
    assert entry["title"] == "100 nephrology cases"
    assert entry["specialty"] == "nephrology"
    assert entry["case_count"] == 100
    assert entry["due_date"] == "2026-10-01"
    assert entry["details"].startswith("CKD stage 4")
    assert "More than one may send cases" in data["how_it_works"]
    assert "confirms what we accept" in data["how_it_works"]


@pytest.mark.parametrize("state", ["intake", "submitted", "approved_awaiting_dla"])
def test_a_partner_who_has_not_signed_cannot_see_the_request_list(state):
    """Same answer every sibling upload surface gives, for the same reason. A
    partner who cannot upload must not be handed a list of things to upload:
    that is a week spent assembling cases we cannot legally accept from them."""
    hs = _make_org(state=state)
    _create_request(_client(), _store())

    portal = _sign_in(hs["hs_id"])
    assert portal.get(f"{API}/hs/requests").status_code == 403


def test_a_closed_request_leaves_the_portal_immediately():
    """A request we have stopped asking for, still sitting on the portal, is how
    a partner spends a week on cases nobody is waiting for."""
    store = _store()
    hs = _make_org(state="active")
    request_id = _create_request(_client(), store).json()["request"]["id"]
    portal = _sign_in(hs["hs_id"])
    assert len(portal.get(f"{API}/hs/requests").json()["requests"]) == 1

    admin = _client()
    r = admin.post(f"{API}/admin/hs-requests/{request_id}/close",
                   json={"reason": "fulfilled"}, headers=_admin_headers(store))
    assert r.status_code == 200, r.text
    assert portal.get(f"{API}/hs/requests").json()["requests"] == []


# ════════════════════════════════════════════════════════════════════════════
#  §5 — closing
# ════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("reason", ["fulfilled", "withdrawn"])
def test_a_request_closes_with_either_reason_and_stays_queryable(reason):
    """Closed is not deleted. The request row is the record of what we asked for
    and when, which is the only thing that makes a partner's upload history
    readable as a response to anything."""
    store = _store()
    admin = _client()
    request_id = _create_request(admin, store).json()["request"]["id"]

    r = admin.post(f"{API}/admin/hs-requests/{request_id}/close",
                   json={"reason": reason}, headers=_admin_headers(store))
    assert r.status_code == 200, r.text
    assert r.json()["request"]["status"] == reason
    assert r.json()["request"]["closed_reason"] == reason
    assert r.json()["request"]["closed_at"]

    listed = admin.get(f"{API}/admin/hs-requests",
                       headers=_admin_headers(store)).json()["requests"]
    assert [x["id"] for x in listed] == [request_id]


def test_closing_twice_is_a_conflict_not_a_silent_overwrite():
    """A second close would overwrite the first one's reason and timestamp, so
    the record would say ``withdrawn`` about a request that was fulfilled."""
    store = _store()
    admin = _client()
    request_id = _create_request(admin, store).json()["request"]["id"]
    admin.post(f"{API}/admin/hs-requests/{request_id}/close",
               json={"reason": "fulfilled"}, headers=_admin_headers(store))

    r = admin.post(f"{API}/admin/hs-requests/{request_id}/close",
                   json={"reason": "withdrawn"}, headers=_admin_headers(store))
    assert r.status_code == 409
    detail = admin.get(f"{API}/admin/hs-requests/{request_id}",
                       headers=_admin_headers(store)).json()
    assert detail["request"]["closed_reason"] == "fulfilled"


def test_an_invented_close_reason_is_refused():
    """Two reasons and no free text, because the reason is read by whoever is
    scanning the list rather than by whoever wrote it."""
    store = _store()
    admin = _client()
    request_id = _create_request(admin, store).json()["request"]["id"]
    r = admin.post(f"{API}/admin/hs-requests/{request_id}/close",
                   json={"reason": "maybe"}, headers=_admin_headers(store))
    assert r.status_code == 400


def test_every_request_endpoint_refuses_a_caller_who_is_not_an_admin():
    """A data request names a specialty and a volume we are buying, which is
    commercial intent. None of these doors open without admin auth."""
    store = _store()
    request_id = _create_request(_client(), store).json()["request"]["id"]
    anon = _client()
    assert anon.post(f"{API}/admin/hs-requests", json={
        "title": "x", "specialty": "y", "case_count": 1}).status_code in (401, 403)
    assert anon.get(f"{API}/admin/hs-requests").status_code in (401, 403)
    assert anon.get(f"{API}/admin/hs-requests/{request_id}").status_code in (401, 403)
    assert anon.post(f"{API}/admin/hs-requests/{request_id}/close",
                     json={"reason": "fulfilled"}).status_code in (401, 403)


# ════════════════════════════════════════════════════════════════════════════
#  §6 — the optional request_id on both upload doors
# ════════════════════════════════════════════════════════════════════════════
def _upload(portal, *, request_id=None, name="cases.json"):
    files = {"files": (name, b'{"cases": []}', "application/json")}
    data = {"request_id": request_id} if request_id is not None else None
    return portal.post(f"{API}/hs/uploads", files=files, data=data)


def test_an_upload_that_names_an_open_request_is_tagged_with_it():
    """The tag is what turns a pile of uploads into an answer to a question.
    Without it the admin sees files arriving and cannot tell what they are
    for."""
    store = _store()
    hs = _make_org(state="active")
    request_id = _create_request(_client(), store).json()["request"]["id"]

    portal = _sign_in(hs["hs_id"])
    r = _upload(portal, request_id=request_id)
    assert r.status_code == 200, r.text
    tagged = store.list_uploads_for_request(request_id)
    assert [u["upload_id"] for u in tagged] == [r.json()["upload_id"]]


def test_an_upload_without_a_request_id_works_exactly_as_before():
    """Absence changes nothing and always will. Most uploads predate or ignore
    every request, and a partner who just sends us data must not meet a new
    precondition because a broadcast feature shipped."""
    hs = _make_org(state="active")
    portal = _sign_in(hs["hs_id"])
    r = _upload(portal)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "received"


@pytest.mark.parametrize("bad", ["hsreq-doesnotexist", "../../etc/passwd"])
def test_an_unknown_request_id_is_a_400(bad):
    """Present and wrong is refused rather than silently dropped. A partner who
    answered a request and had the tag quietly discarded would believe they had
    responded to something we have no record of them responding to."""
    hs = _make_org(state="active")
    portal = _sign_in(hs["hs_id"])
    assert _upload(portal, request_id=bad).status_code == 400


def test_a_closed_request_id_is_a_400_that_says_to_send_it_anyway():
    """The refusal has to leave a door open. A partner who assembled the cases
    before we closed the request still has cases we probably want."""
    store = _store()
    hs = _make_org(state="active")
    request_id = _create_request(_client(), store).json()["request"]["id"]
    _client().post(f"{API}/admin/hs-requests/{request_id}/close",
                   json={"reason": "withdrawn"}, headers=_admin_headers(store))

    portal = _sign_in(hs["hs_id"])
    r = _upload(portal, request_id=request_id)
    assert r.status_code == 400
    assert "without it" in r.json()["detail"]


def test_the_chunked_door_takes_the_same_tag_and_refuses_the_same_ids():
    """A gate applied at one door and forgotten at another is not a gate, it is
    a detour sign. The chunked declare validates identically, and the tag has
    to survive the WHOLE door: parked on the session at declare, copied onto
    the upload row at complete. Asserting only the session half would pass with
    the complete-side copy deleted, and the admin's who-answered view reads the
    upload rows, not the sessions."""
    store = _store()
    hs = _make_org(state="active")
    request_id = _create_request(_client(), store).json()["request"]["id"]
    portal = _sign_in(hs["hs_id"])
    data = b'{"cases": []}' * 300
    body = {"filename": "big.zip", "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "content_type": "application/zip"}

    bad = portal.post(f"{API}/hs/uploads/sessions",
                      json={**body, "request_id": "hsreq-nope"})
    assert bad.status_code == 400

    ok = portal.post(f"{API}/hs/uploads/sessions",
                     json={**body, "request_id": request_id})
    assert ok.status_code == 200, ok.text
    session_id = ok.json()["session_id"]
    session = store.get_upload_session(session_id)
    # Parked on the SESSION, so what the upload answers is fixed at declare and
    # cannot be renamed by whoever completes it minutes later.
    assert session["request_id"] == request_id

    put = portal.put(
        f"{API}/hs/uploads/sessions/{session_id}/parts/1", content=data,
        headers={"X-Chunk-SHA256": hashlib.sha256(data).hexdigest()})
    assert put.status_code == 200, put.text
    done = portal.post(f"{API}/hs/uploads/sessions/{session_id}/complete")
    assert done.status_code == 200, done.text
    tagged = store.list_uploads_for_request(request_id)
    assert done.json()["upload_id"] in [u["upload_id"] for u in tagged]


def test_the_detail_view_tallies_tagged_uploads_per_health_system():
    """The question the view answers is "who answered", not "what arrived":
    three uploads from one partner is one partner responding, and a flat list
    reads as three."""
    store = _store()
    first = _make_org(state="active")
    second = _make_org(state="active")
    admin = _client()
    request_id = _create_request(admin, store).json()["request"]["id"]

    p1 = _sign_in(first["hs_id"])
    _upload(p1, request_id=request_id, name="one.json")
    _upload(p1, request_id=request_id, name="two.json")
    p2 = _sign_in(second["hs_id"])
    _upload(p2, request_id=request_id, name="three.json")
    _upload(p2)  # untagged: belongs to nobody's request

    detail = admin.get(f"{API}/admin/hs-requests/{request_id}",
                       headers=_admin_headers(store)).json()
    assert detail["uploads_count"] == 3
    counts = {e["hs_id"]: len(e["uploads"]) for e in detail["responders"]}
    assert counts == {first["hs_id"]: 2, second["hs_id"]: 1}


def test_the_admin_list_reports_how_the_broadcast_actually_went(sent):
    """An operator whose request produced no replies has to be able to tell
    "nobody had the cases" from "nobody was told". Those look identical from
    the outside, so the delivery tally is on the request."""
    store = _store()
    _make_org(state="active", members=("a@x.org", "b@x.org"))
    admin = _client()
    request_id = _create_request(admin, store).json()["request"]["id"]

    listed = admin.get(f"{API}/admin/hs-requests",
                       headers=_admin_headers(store)).json()["requests"][0]
    assert listed["delivery"] == {"pending": 2, "sent": 0, "failed": 0}

    _drain()
    listed = admin.get(f"{API}/admin/hs-requests/{request_id}",
                       headers=_admin_headers(store)).json()["request"]
    assert listed["delivery"] == {"pending": 0, "sent": 2, "failed": 0}


def test_retry_failed_flips_failed_rows_back_to_pending_and_only_those(sent):
    """A failed outbox row used to be terminal: re-broadcasting enqueues
    nothing because every idempotency key already exists, so one transport
    outage permanently under-delivered the request. Retry flips exactly the
    failed rows back to pending for the next drain, and touches nothing sent."""
    store = _store()
    _make_org(state="active", members=("a@x.org", "b@x.org"))
    admin = _client()
    request_id = _create_request(admin, store).json()["request"]["id"]
    headers = _admin_headers(store)

    sent.ok = False
    _drain()
    r = admin.post(f"{API}/admin/hs-requests/{request_id}/retry-failed",
                   headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["retried"] == 2
    assert r.json()["delivery"] == {"pending": 2, "sent": 0, "failed": 0}

    sent.ok = True
    _drain()
    listed = admin.get(f"{API}/admin/hs-requests/{request_id}",
                       headers=headers).json()["request"]
    assert listed["delivery"] == {"pending": 0, "sent": 2, "failed": 0}

    # Nothing left to retry, and sent rows stay sent.
    again = admin.post(f"{API}/admin/hs-requests/{request_id}/retry-failed",
                       headers=headers)
    assert again.json()["retried"] == 0
    assert again.json()["delivery"] == {"pending": 0, "sent": 2, "failed": 0}

    assert admin.post(f"{API}/admin/hs-requests/hsreq-nope/retry-failed",
                      headers=headers).status_code == 404
    anon = _client()
    assert anon.post(f"{API}/admin/hs-requests/{request_id}/retry-failed"
                     ).status_code in (401, 403)


# ════════════════════════════════════════════════════════════════════════════
#  §8, §9, §10 — the admin lead view
# ════════════════════════════════════════════════════════════════════════════
@pytest.fixture()
def leads(monkeypatch, tmp_path):
    """A throwaway team store for the lead half of this file.

    ``A.fresh_store()`` resets the asclepius DB only; team.db is a different
    file on a different plane and is shared for the whole session, so without
    this every lead assertion would read rows written by the test before it.
    """
    from team_store import TeamStore

    monkeypatch.setenv("EMAIL_DEV_MODE", "1")
    monkeypatch.setattr(A.app.state, "team_store",
                        TeamStore(db_path=str(tmp_path / "leads.db")))
    return _client()


def _submit_lead(client, **overrides):
    body = {"source": "health_system_partner", "email": "cio@stmarys.org",
            "message": "We hold about 40k nephrology encounters."}
    body.update(overrides)
    return client.post("/api/leads", json=body)


def test_the_lead_table_can_finally_be_read_back(leads):
    """``lead_submissions`` has been write-only since it was created.

    Every submission is an attestation about authority over de-identified data,
    which makes it a legal audit trail, and an audit trail nobody can read is a
    file nobody keeps. Newest first, because the operator is answering the last
    person who wrote in.
    """
    store = _store()
    _submit_lead(leads, email="first@x.org", message="First.")
    _submit_lead(leads, email="second@x.org", message="Second.",
                 source="request_data")

    r = leads.get("/api/leads/admin", headers=_admin_headers(store))
    assert r.status_code == 200, r.text
    rows = r.json()["leads"]
    assert [x["email"] for x in rows] == ["second@x.org", "first@x.org"]
    # The message VERBATIM: it is the attestation, and a summary of an
    # attestation is not one.
    assert rows[1]["message"] == "First."
    assert rows[0]["source_label"] == "Request products \u00b7 AI lab / buyer"


def test_all_four_sources_come_back_and_one_can_be_filtered(leads):
    """The health-system pipeline and the buyer pipeline are read by different
    people and neither wants to scroll past the other."""
    store = _store()
    for source in ("request_data", "provide_data", "research_notify",
                   "health_system_partner"):
        _submit_lead(leads, source=source, email=f"{source}@x.org")

    everything = leads.get("/api/leads/admin",
                           headers=_admin_headers(store)).json()["leads"]
    assert {x["source"] for x in everything} == {
        "request_data", "provide_data", "research_notify", "health_system_partner"}

    partners = leads.get("/api/leads/admin?source=health_system_partner",
                         headers=_admin_headers(store)).json()["leads"]
    assert [x["source"] for x in partners] == ["health_system_partner"]


def test_a_honeypot_submission_is_absent_because_it_was_never_stored(leads):
    """Requirement 10: nothing about the write path changes. The honeypot still
    returns a cheerful 200 and writes nothing, so the reader has nothing to
    filter and no filter to get wrong."""
    store = _store()
    r = _submit_lead(leads, email="bot@x.org", company_website="http://spam")
    assert r.status_code == 200 and r.json() == {"ok": True}

    assert leads.get("/api/leads/admin",
                     headers=_admin_headers(store)).json()["leads"] == []


def test_paging_is_keyset_so_a_live_form_cannot_shift_page_two(leads):
    """A form still accepting submissions shifts every OFFSET under the reader,
    so page two would repeat rows page one already showed. The cursor is the
    id, and the server hands it back rather than making the client know that."""
    store = _store()
    for n in range(5):
        _submit_lead(leads, email=f"lead{n}@x.org", message=f"Message {n}.")

    first = leads.get("/api/leads/admin?limit=2",
                      headers=_admin_headers(store)).json()
    assert [x["message"] for x in first["leads"]] == ["Message 4.", "Message 3."]
    assert first["next_before_id"]

    # A submission arrives between the two page loads. Keyset paging is what
    # makes the second page still continue from where the first one stopped.
    _submit_lead(leads, email="late@x.org", message="Late.")
    second = leads.get(
        f"/api/leads/admin?limit=2&before_id={first['next_before_id']}",
        headers=_admin_headers(store)).json()
    assert [x["message"] for x in second["leads"]] == ["Message 2.", "Message 1."]


def test_the_lead_reader_never_returns_the_forensic_columns(leads):
    """``user_agent`` and ``client_ip`` are stored for abuse forensics on a
    public form. The console's job is to show who wrote in and what they said,
    and shipping a submitter's IP into a browser is not that."""
    store = _store()
    _submit_lead(leads)
    lead = leads.get("/api/leads/admin",
                     headers=_admin_headers(store)).json()["leads"][0]
    assert set(lead) == {"id", "source", "source_label", "email", "message",
                         "created_at"}


def test_the_lead_reader_is_admin_only():
    """The rows carry an email address and a description of what an
    organization holds. That is a target list, and it does not leave the admin
    plane."""
    assert _client().get("/api/leads/admin").status_code in (401, 403)
