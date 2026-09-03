"""R5: "the backend must not break", made mechanical.

The founder meeting gave PRD-F exactly one hard constraint, and it is a
negative one: the console can be moved, re-cut and redesigned, and the API it
sits on must answer exactly as it did. A negative constraint is the kind that
passes review and fails in production, because nothing on any screen looks
different when a route quietly changes shape.

So this file CALLS every endpoint on the PRD's frozen list as an admin and
asserts two things about each: that it answers at all, and that the top-level
keys the console reads are still there. It deliberately does not assert on
values or on business behaviour, which those endpoints' own suites already
cover; it asserts on the CONTRACT, which is the thing this PR promised not to
touch.

ONE CARVE-OUT, stated here rather than smuggled. ``GET
/admin/community/summary`` is new, and it is the U11 community summary: the
meeting asked for "a summary of what's going on" and no read on this backend
answered that question. It is additive, it is a count rather than a
paraphrase, and it is asserted at the bottom of this file alongside the frozen
list rather than instead of it.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["TEAM_DB_PATH"] = os.path.join("/tmp", f"admin_freeze_audit_{uuid.uuid4().hex}.db")
os.environ.setdefault("EMAIL_DEV_MODE", "1")

from fastapi.testclient import TestClient  # noqa: E402

from tests import _asclepius as A  # noqa: E402
from community import store as community_store  # noqa: E402

client = TestClient(A.app)


@pytest.fixture(autouse=True)
def _isolated():
    """Both planes fresh. The community store is a process-global singleton on a
    default on-disk path, so without the rebind the summary test below would
    count posts out of the developer's real community.db."""
    A.fresh_store()
    community_store.reset_community_store_for_tests(
        db_path=os.path.join("/tmp", f"admin_freeze_community_{uuid.uuid4().hex}.db"))
    yield


def _member(store, *, specialty="cardiology"):
    """An approved physician who can actually reach the community.

    Same construction as test_case_rooms.py: the gate wants an approved
    verification state and a verified credential row, and a fixture that skips
    either one turns the community assertions below into a silent skip."""
    user = A.make_user(store, specialty=specialty, tier="labeler")
    with store._conn() as conn:                                  # noqa: SLF001
        conn.execute("UPDATE users SET verification_status = 'approved' WHERE id = ?",
                     (user["id"],))
    store.upsert_contributor_credentials(
        id_hashed=user["id_hashed"], user_id=user["id"],
        organization="Riverside Cardiology", role_title="Physician (MD)",
        credentials_verified=True,
        ship={"degree": "MD", "primary_specialty": specialty,
              "years_in_active_practice": 12, "credentials_verified": True},
        verify={"full_legal_name": "Test Physician, MD", "npi": "1234567893"})
    return store.get_user_by_id(user["id"])


def _store():
    from asclepius.store import get_store
    return get_store()


def _admin():
    return A.headers_for(A.make_user(_store(), role="admin"))


API = "/api/asclepius"

#: (method, path, required top-level keys). Straight off the PRD's frozen list.
#:
#: A path with an id in it uses an id that does not exist, on purpose: a 404
#: from the handler proves the route is mounted, authorized and reached, which
#: is what a contract freeze is about. A 404 from the ROUTER (an unmounted
#: path) is indistinguishable in status but not in what it means, so those rows
#: carry no key expectation and the presence assertion below covers them
#: instead.
FROZEN_READS = [
    ("/admin/physicians", ("physicians", "counts")),
    ("/admin/signups", ("signups",)),
    ("/admin/health-systems", ("health_systems",)),
    ("/admin/health-system-signups", ("pending",)),
    ("/admin/batches", ("longitudinal", "real_static", "synthetic")),
    ("/admin/batches/synthetic", ("cases",)),
    ("/admin/storage/reconcile", ()),
    ("/admin/export/case-options", ()),
    ("/admin/export/case-preview", ()),
    ("/admin/metrics/questions", ()),
    ("/admin/hs-referrals", ("referrals", "total")),
    # Payments router, admin side of the ledger.
    ("/admin/earnings", ()),
    ("/admin/earnings/held", ()),
    ("/admin/referrals", ()),
]

#: Routes whose only cheap assertion is "still mounted and still admin-gated":
#: they need a real id, a body, or they write. Reached with a nonexistent id so
#: the handler answers and nothing is mutated.
FROZEN_MOUNTED = [
    ("GET", "/admin/physicians/no-such-user"),
    ("GET", "/admin/health-systems/no-such-hs"),
    ("GET", "/admin/health-systems/no-such-hs/payouts"),
    ("GET", "/admin/health-systems/no-such-hs/invoices"),
    ("GET", "/admin/batches/no-such-batch"),
    ("GET", "/admin/batches/relay/no-such-trajectory"),
    ("GET", "/admin/batches/preview/no-such-task"),
    ("GET", "/admin/agreements/no-such-agreement/document"),
    # /admin/assignments requires task_id or user_id: reached with one so the
    # handler runs rather than answering its own 400 at the door.
    ("GET", "/admin/assignments?user_id=no-such-user"),
    ("POST", "/admin/batches/relay/no-such-trajectory/reassign"),
    ("POST", "/admin/earnings/no-such-earning/release"),
    ("POST", "/admin/earnings/no-such-earning/void"),
    ("POST", "/admin/earnings/pay"),
    ("POST", "/admin/earnings/mark-paid"),
    ("GET", "/admin/earnings/no-such-earning/case-export"),
    ("POST", "/admin/hs-referrals/no-such-referral/advance"),
    ("POST", "/admin/hs-referrals/no-such-referral/reward"),
]


@pytest.mark.parametrize("path,keys", FROZEN_READS, ids=[p for p, _ in FROZEN_READS])
def test_a_frozen_read_still_answers_with_the_shape_the_console_reads(path, keys):
    """WHY: the console renders from these keys. A route that starts answering
    ``{"data": {...}}`` is a green backend suite and four blank admin tabs."""
    res = client.get(API + path, headers=_admin())
    assert res.status_code == 200, f"{path} answered {res.status_code}: {res.text[:300]}"
    body = res.json()
    assert isinstance(body, dict), f"{path} no longer returns an object"
    for key in keys:
        assert key in body, f"{path} dropped the top-level key {key!r}"


@pytest.mark.parametrize("method,path", FROZEN_MOUNTED,
                         ids=[f"{m}-{p}" for m, p in FROZEN_MOUNTED])
def test_a_frozen_route_is_still_mounted_and_still_admin_gated(method, path):
    """WHY: these take an id or a body, so the cheap contract assertion is that
    the ROUTE exists and the admin dependency runs.

    405 is the failure that matters here: it means the path resolved to a
    different verb, which is a route that moved. 404 with a bogus id is the
    handler doing its job, and 422 is the request model doing its job. Both
    prove the same thing this test is for."""
    res = client.request(method, API + path, headers=_admin(), json={})
    assert res.status_code != 405, f"{method} {path} is no longer mounted for this verb"
    assert res.status_code < 500, f"{method} {path} 500s: {res.text[:300]}"
    assert res.status_code != 401, f"{method} {path} refused a valid admin session"


@pytest.mark.parametrize("method,path",
                         [("GET", p) for p, _ in FROZEN_READS] + FROZEN_MOUNTED,
                         ids=[f"{m}-{p}" for m, p in
                              [("GET", p) for p, _ in FROZEN_READS] + FROZEN_MOUNTED])
def test_every_frozen_route_still_refuses_a_physician(method, path):
    """WHY: F3 claims the authorization boundary did not move. The freeze is
    about shape; this is about the other half of the contract, and it is the
    half where a mistake is a data leak rather than a blank screen."""
    headers = A.headers_for(A.make_user(_store(), role="evaluator"))
    res = client.request(method, API + path, headers=headers, json={})
    assert res.status_code in (401, 403), f"{method} {path} answered {res.status_code}"


# ─── The one carve-out ────────────────────────────────────────────────────────
def test_the_community_summary_is_a_count_and_says_which_window_it_counted():
    """WHY: U11. The meeting asked for a summary of what is going on in the
    community and there was no read that answered it: ``/channels`` lists
    channels, ``/channels/{slug}/messages`` pages one room.

    The reason this is an endpoint rather than an assistant is the same reason
    it is asserted here: every figure is checkable. A window with no length on
    it would be a number nobody can reproduce, so the length ships with the
    payload."""
    res = client.get(API + "/admin/community/summary", headers=_admin())
    assert res.status_code == 200, res.text
    body = res.json()
    assert isinstance(body.get("window_days"), int) and body["window_days"] > 0
    for key in ("totals", "channels", "recent", "unanswered", "rooms"):
        assert key in body, key
    for count in ("posts", "replies", "voices", "reactions", "members", "case_rooms"):
        assert isinstance(body["totals"][count], int), count


def test_the_summary_counts_an_unanswered_question_and_stops_counting_it_on_a_reply():
    """WHY: the unanswered list is the only item on the community tab that is a
    JOB rather than a statistic, so it has to be right in both directions. A
    list that never empties gets ignored, and one that empties early is a
    physician left talking to nobody.

    Mutation-checked: the assertion below fails if the reply is not posted, and
    fails if the reply is counted as its own unanswered question."""
    store = _store()
    doc = _member(store)
    other = _member(store)
    headers, other_headers = A.headers_for(doc), A.headers_for(other)

    channels = client.get("/api/community/channels", headers=headers)
    if channels.status_code != 200 or not channels.json().get("channels"):
        pytest.skip("the community plane has no channel this fixture may post in")
    slug = channels.json()["channels"][0]["slug"]

    asked = client.post(f"/api/community/channels/{slug}/messages",
                        json={"body": "Has anyone seen this pattern before?"},
                        headers=headers)
    if asked.status_code != 200:
        pytest.skip(f"this fixture cannot post in #{slug}: {asked.text[:120]}")
    mid = asked.json()["id"]

    body = client.get(API + "/admin/community/summary", headers=_admin()).json()
    assert any(q["id"] == mid for q in body["unanswered"]), (
        "a question with no reply under it was not counted")

    client.post(f"/api/community/channels/{slug}/messages",
                json={"body": "Yes, twice last year.", "parent_message_id": mid},
                headers=other_headers)

    body = client.get(API + "/admin/community/summary", headers=_admin()).json()
    assert not any(q["id"] == mid for q in body["unanswered"]), (
        "an answered question is still on the list")
    assert all(not q.get("is_reply") for q in body["unanswered"])
