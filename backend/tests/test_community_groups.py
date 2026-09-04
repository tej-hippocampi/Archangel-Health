"""Member-created group conversations.

A group is the same database object a case room is: a row in ``community_dms``
with a ``kind``, a title, and its roster in ``community_dm_members``. What makes
it a different THING is who decided it exists and who may read it. A case room
is opened by routing and is open to Archangel admins so a founder can step into
a stuck case; a group is opened by a physician and is private to the people in
it, exactly like the two-party DM it is an extension of.

These tests pin the parts that are easy to get wrong precisely because the two
share so much plumbing: the roster is the access list, the creator is always in
it, and adding somebody is a decision only a participant may take.

The admin-cannot-read half lives in ``test_case_rooms.py``, beside the exception
it must not become.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["TEAM_DB_PATH"] = os.path.join("/tmp", f"community_groups_audit_{uuid.uuid4().hex}.db")
os.environ.setdefault("EMAIL_DEV_MODE", "1")

from fastapi.testclient import TestClient  # noqa: E402

from tests import _asclepius as A  # noqa: E402
from community import store as community_store  # noqa: E402

client = TestClient(A.app)
COMMUNITY = "/api/community"


def _store():
    from asclepius.store import get_store
    return get_store()


def _cstore():
    from community.store import get_community_store
    return get_community_store()


@pytest.fixture(autouse=True)
def _isolated():
    from community.ws import hub as _hub
    _hub._sockets.clear()                                        # noqa: SLF001
    A.fresh_store()
    community_store.reset_community_store_for_tests(
        db_path=os.path.join("/tmp", f"community_groups_{uuid.uuid4().hex}.db"))
    yield


def _doc(store, *, specialty="nephrology"):
    """A credential-verified physician, which is what the write gate asks for."""
    user = A.make_user(store, specialty=specialty, tier="labeler")
    store.upsert_contributor_credentials(
        id_hashed=user["id_hashed"], user_id=user["id"],
        organization="Riverside Nephrology", role_title="Physician (MD)",
        credentials_verified=True,
        ship={"degree": "MD", "primary_specialty": specialty,
              "years_in_active_practice": 9, "credentials_verified": True},
        verify={"full_legal_name": "Test Physician, MD", "npi": "1234567893"})
    return store.get_user_by_id(user["id"])


def _make_group(author, others, title="Transplant call rota"):
    return client.post(f"{COMMUNITY}/dms/group",
                       json={"title": title, "user_ids": [u["id"] for u in others]},
                       headers=A.headers_for(author))


# ═══ Creation ════════════════════════════════════════════════════════════════
def test_a_group_carries_its_name_and_its_whole_roster():
    """WHY: a group has no peer, so its NAME is the only handle anyone has on it.

    A conversation named after its members renames itself every time somebody is
    added, and to the person reading it that is a different conversation. The
    title is stored, chosen by the author, and returned on the summary the rail
    renders from.
    """
    store = _store()
    a, b, c = _doc(store), _doc(store), _doc(store)

    r = _make_group(a, [b, c])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "group"
    assert body["title"] == "Transplant call rota"
    assert body.get("peer") is None, "a group with a peer can be mistaken for a DM"
    assert {m["user_id"] for m in body["participants"]} == {a["id"], b["id"], c["id"]}


def test_the_creator_is_a_member_of_their_own_group():
    """A group whose author cannot read it is not a state anything downstream is
    written to handle -- and it is what a naive "add the invited users" would
    produce."""
    store = _store()
    a, b = _doc(store), _doc(store)
    gid = _make_group(a, [b]).json()["id"]
    assert a["id"] in _cstore().room_participants(gid)
    assert client.get(f"{COMMUNITY}/dms/{gid}/messages",
                      headers=A.headers_for(a)).status_code == 200


def test_a_group_appears_in_every_members_conversation_list():
    store = _store()
    a, b = _doc(store), _doc(store)
    gid = _make_group(a, [b]).json()["id"]
    for who in (a, b):
        listed = client.get(f"{COMMUNITY}/dms", headers=A.headers_for(who)).json()["dms"]
        mine = [d for d in listed if d["id"] == gid]
        assert mine and mine[0]["kind"] == "group", who["id"]


def test_two_groups_with_the_same_people_are_two_groups():
    """WHY: both neighbours of this call are get-or-create, and a group is not.

    A two-party DM is keyed on the pair and a case room on the case, so asking
    twice must return the same object. A group is keyed on nothing -- a rota and
    a journal club can hold the same three people -- and collapsing them would
    silently drop the second conversation into the first.
    """
    store = _store()
    a, b = _doc(store), _doc(store)
    first = _make_group(a, [b], title="Rota").json()["id"]
    second = _make_group(a, [b], title="Journal club").json()["id"]
    assert first != second


def test_a_group_needs_somebody_else_in_it():
    store = _store()
    a = _doc(store)
    r = client.post(f"{COMMUNITY}/dms/group", json={"title": "Just me", "user_ids": []},
                    headers=A.headers_for(a))
    assert r.status_code == 400


def test_a_group_needs_a_name():
    store = _store()
    a, b = _doc(store), _doc(store)
    r = client.post(f"{COMMUNITY}/dms/group",
                    json={"title": "   ", "user_ids": [b["id"]]},
                    headers=A.headers_for(a))
    assert r.status_code == 400, r.text


def test_a_non_member_cannot_be_put_in_a_group():
    """The same §1 gate ``POST /dms`` applies: you cannot put an account in a
    conversation it could not itself read. Refused whole rather than opened with
    two of the three people picked, because a group that quietly lost somebody is
    worse than one that would not open."""
    store = _store()
    a, b = _doc(store), _doc(store)
    outsider = A.make_user(store, role="buyer")
    r = client.post(f"{COMMUNITY}/dms/group",
                    json={"title": "Rota", "user_ids": [b["id"], outsider["id"]]},
                    headers=A.headers_for(a))
    assert r.status_code == 404
    assert not [d for d in client.get(f"{COMMUNITY}/dms",
                                      headers=A.headers_for(a)).json()["dms"]
                if d.get("kind") == "group"], "nothing was half-created"


# ═══ Membership ══════════════════════════════════════════════════════════════
def test_a_participant_can_add_a_colleague():
    store = _store()
    a, b, c = _doc(store), _doc(store), _doc(store)
    gid = _make_group(a, [b]).json()["id"]

    r = client.post(f"{COMMUNITY}/dms/{gid}/members", json={"user_ids": [c["id"]]},
                    headers=A.headers_for(b))
    assert r.status_code == 200, r.text
    assert {m["user_id"] for m in r.json()["participants"]} == {a["id"], b["id"], c["id"]}
    assert client.get(f"{COMMUNITY}/dms/{gid}/messages",
                      headers=A.headers_for(c)).status_code == 200


def test_somebody_added_later_can_read_what_was_said_before_they_arrived():
    """Deliberate, and stated so it cannot be changed by accident.

    ``community_dm_members`` records when somebody joined, and the message list
    is not filtered by it. A group is a room you are shown into, not a stream you
    are attached to, and history that vanishes for the newest member makes a
    handoff impossible to follow. The PHI gate is what keeps that safe: nothing
    identifiable was allowed into the room in the first place.
    """
    store = _store()
    a, b, c = _doc(store), _doc(store), _doc(store)
    gid = _make_group(a, [b]).json()["id"]
    client.post(f"{COMMUNITY}/dms/{gid}/messages", json={"body": "moving to Friday"},
                headers=A.headers_for(a))
    client.post(f"{COMMUNITY}/dms/{gid}/members", json={"user_ids": [c["id"]]},
                headers=A.headers_for(a))

    msgs = client.get(f"{COMMUNITY}/dms/{gid}/messages",
                      headers=A.headers_for(c)).json()["messages"]
    assert [m["body"] for m in msgs] == ["moving to Friday"]


def test_an_outsider_cannot_add_anybody_and_learns_nothing_by_trying():
    """404, not 403: the endpoint must never be an oracle for which groups
    exist, which is the rule every other DM path here follows."""
    store = _store()
    a, b, outsider = _doc(store), _doc(store), _doc(store)
    gid = _make_group(a, [b]).json()["id"]
    r = client.post(f"{COMMUNITY}/dms/{gid}/members", json={"user_ids": [outsider["id"]]},
                    headers=A.headers_for(outsider))
    assert r.status_code == 404
    assert outsider["id"] not in _cstore().room_participants(gid)


def test_adding_somebody_twice_does_not_duplicate_them():
    store = _store()
    a, b = _doc(store), _doc(store)
    gid = _make_group(a, [b]).json()["id"]
    r = client.post(f"{COMMUNITY}/dms/{gid}/members", json={"user_ids": [b["id"]]},
                    headers=A.headers_for(a))
    assert r.status_code == 200
    assert _cstore().room_participants(gid).count(b["id"]) == 1


# ═══ It is a DM, so it behaves like one ══════════════════════════════════════
def test_the_phi_gate_runs_on_a_group_exactly_as_on_a_dm():
    """A group is where a case gets discussed by more people than a DM, which
    makes it MORE likely to attract an identifier, not less."""
    store = _store()
    a, b = _doc(store), _doc(store)
    gid = _make_group(a, [b]).json()["id"]
    r = client.post(f"{COMMUNITY}/dms/{gid}/messages",
                    json={"body": "MRN 84921734 is the one"}, headers=A.headers_for(a))
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "phi_detected"


def test_a_group_message_reaches_every_member_but_the_author():
    """One digest row per other participant, none for the sender -- the same
    plumbing a two-party DM rides, which is the point of a group being the same
    object rather than a parallel one."""
    store = _store()
    a, b, c = _doc(store), _doc(store), _doc(store)
    gid = _make_group(a, [b, c]).json()["id"]
    assert client.post(f"{COMMUNITY}/dms/{gid}/messages", json={"body": "Friday works"},
                       headers=A.headers_for(a)).status_code == 200
    queued = {(n["user_id"], n["kind"]) for n in _cstore().unsent_notifications()}
    assert queued == {(b["id"], "dm"), (c["id"], "dm")}
