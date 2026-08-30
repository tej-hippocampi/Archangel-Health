"""The health-system portal access table, checked as a table.

Pure policy, no app, no DB — the analogue of the physician tier-capability test.
Every assertion here is about the SHAPE of the mapping, because the failures this
guards against are the silent kind: a surface added to the tuple and forgotten in
one level's set, or the NULL collapse quietly changing and locking out every
hospital that predates approval.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asclepius import hs_access as A  # noqa: E402


def test_every_level_is_mapped_and_every_surface_is_accounted_for():
    for level in A.ACCESS_LEVELS:
        assert level in A._BY_ACCESS, f"{level} has no surface set"
        unknown = A._BY_ACCESS[level] - set(A.HS_SURFACES)
        assert not unknown, f"{level} grants surfaces that do not exist: {unknown}"
    # FULL must be exhaustive, or adding a surface silently withholds it from
    # approved partners, which reads to them as the feature being broken.
    assert A._BY_ACCESS[A.FULL] == frozenset(A.HS_SURFACES)
    assert A._BY_ACCESS[A.NONE] == frozenset()


def test_null_approval_status_reaches_everything():
    """The zero-backfill guarantee. Every hospital provisioned before approval
    existed has approval_status NULL; if this flips, they all lose upload on
    deploy and the first we hear about it is a support email."""
    legacy = {"username": "mercy", "hs_id": "hs-mercy", "active": 1, "approval_status": None}
    assert A.access_level(legacy) == A.FULL
    assert A.surfaces(legacy) == frozenset(A.HS_SURFACES)
    assert A.can_surface(legacy, A.UPLOAD)
    # A row that never had the column at all behaves the same way.
    assert A.access_level({"username": "mercy", "active": 1}) == A.FULL


def test_pending_gets_the_portal_but_not_the_upload_door():
    pending = {"active": 1, "approval_status": "pending"}
    assert A.access_level(pending) == A.PROVISIONAL
    assert not A.can_surface(pending, A.UPLOAD)
    # ...and everything else stays open, deliberately: an empty product at the
    # moment someone signs up is worse than a locked tile they understand.
    for surface in (A.PAYOUTS, A.INTAKE, A.ACCOUNT):
        assert A.can_surface(pending, surface), surface


@pytest.mark.parametrize(
    "row",
    [
        {"active": 1, "approval_status": "rejected"},
        {"active": 0, "approval_status": "approved"},
        {"active": 0, "approval_status": None},
        {},
        None,
    ],
)
def test_closed_accounts_reach_nothing(row):
    assert A.access_level(row) == A.NONE
    assert A.surfaces(row) == frozenset()
    for surface in A.HS_SURFACES:
        assert not A.can_surface(row, surface)


def test_approved_reaches_everything():
    approved = {"active": 1, "approval_status": "approved"}
    assert A.access_level(approved) == A.FULL
    assert A.surfaces(approved) == frozenset(A.HS_SURFACES)


def test_status_is_read_case_and_space_insensitively():
    """The column is written by our own code today, but a value that arrives
    'Pending' must not silently resolve to full access."""
    assert A.access_level({"active": 1, "approval_status": " Pending "}) == A.PROVISIONAL
    assert A.access_level({"active": 1, "approval_status": "REJECTED"}) == A.NONE


def test_account_state_never_leaks_an_operator_token():
    """What we call our queue is not what we tell a hospital."""
    internal = {"pending", "approved", "rejected", "provisional", "full", "none"}
    for status in (None, "pending", "approved", "rejected"):
        for active in (0, 1):
            word = A.account_state({"active": active, "approval_status": status})
            assert word not in internal, f"{status}/{active} leaked {word!r}"
            assert word in ("in review", "active", "closed")


def test_module_avoids_the_provider_facing_forbidden_words():
    """asclepius_provider.py imports this, and that file plus everything it
    serves is grep-scanned. Catch a stray word here rather than in the static
    test's less obvious failure message."""
    src = (Path(__file__).resolve().parent.parent / "asclepius" / "hs_access.py").read_text()
    lowered = src.lower()
    for word in ("purpose", "broker", "brokering", "task_creation"):
        assert word not in lowered, f"hs_access.py mentions {word!r}"
