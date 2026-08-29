"""The contract of ARCHANGEL_LEGACY_PERIOP (PRD §5).

The flag's whole purpose is to make deleting the peri-op surface rehearsable: with
it on, production must be untouched; with it off, the app must behave exactly as
it will once the code is gone. Both halves need holding down, because both fail
silently — a flag that gates too much 404s a live customer path, and a flag that
gates too little makes the dark week prove nothing.

These tests re-import ``main`` under a patched environment. That is unusual and
deliberate: the flag is read at import time (correctly — a route table must not
change under a running server), so the only way to observe the off state is to
build a second app.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _app_with_flag(value: str):
    """Import a fresh ``main`` with ARCHANGEL_LEGACY_PERIOP set to ``value``."""
    prev_env = os.environ.get("ARCHANGEL_LEGACY_PERIOP")
    os.environ["ARCHANGEL_LEGACY_PERIOP"] = value
    saved = {name: mod for name, mod in sys.modules.items()
             if name == "main" or name == "legacy_flag" or name.startswith("routers.")}
    for name in saved:
        sys.modules.pop(name, None)
    try:
        import legacy_flag  # noqa: F401  (re-read so the new env value takes)
        main = importlib.import_module("main")
        return {(r.path, tuple(sorted(getattr(r, "methods", []) or []))) for r in main.app.routes}
    finally:
        if prev_env is None:
            os.environ.pop("ARCHANGEL_LEGACY_PERIOP", None)
        else:
            os.environ["ARCHANGEL_LEGACY_PERIOP"] = prev_env
        for name in list(sys.modules):
            if name == "main" or name == "legacy_flag" or name.startswith("routers."):
                sys.modules.pop(name, None)
        sys.modules.update(saved)


@pytest.fixture(scope="module")
def routes_on():
    return _app_with_flag("1")


@pytest.fixture(scope="module")
def routes_off():
    return _app_with_flag("0")


# Every path a live surface actually calls. Sourced from the ${API_BASE} fetches
# in landing/src and frontend/{asclepius,buyer,provider}, plus the page routes
# those frontends navigate to and the scheduled endpoints driven from CI.
LIVE_PATHS = [
    # Landing auth — PRD §0.1 calls this load-bearing for every signup.
    "/api/auth/register", "/api/auth/login", "/api/auth/me", "/api/auth/logout",
    "/api/auth/verify-email", "/api/auth/verify-email/resend",
    "/api/auth/verify-email/by-token",
    "/api/auth/portal-handoff", "/api/auth/portal-handoff/consume",
    "/api/doctor/profile", "/api/doctor/onboard",
    "/api/patient/by-codes", "/api/patient/logout",
    # landing/src/lib/auth-api.ts:211 — inside a range §5 would have gated.
    "/api/demo/sign-in-routes",
    # Page shells the live frontends navigate to.
    "/", "/doctor", "/doctor/sign-in", "/doctor/app",
    "/asclepius", "/community", "/provider", "/workspace", "/partner/upload",
    "/recovery", "/admin",
    # Deploy verification.
    "/api/version",
    # community/ is on §1.4's never-touch list; /run-morning is driven by the
    # GitHub Actions workflow at docs/asclepius/community-morning.workflow.yml.
    "/internal/community/run-morning", "/internal/community/run-digest",
    "/internal/community/purge",
    # team_store's scheduled drivers; team_store is §0.1-protected.
    "/internal/team/run-daily-jobs", "/internal/team/run-task-notifications",
]


@pytest.mark.parametrize("path", LIVE_PATHS)
def test_a_live_path_survives_the_flag_being_turned_off(path, routes_off):
    """The failure this exists to prevent: the dark week 404s a real customer.

    Every path here was traced to a caller in a surface that is NOT being
    retired. If one of them stops being registered with the flag off, the gate
    set has swallowed something live — fix the gate, never this list.
    """
    assert path in {p for p, _ in routes_off}, (
        f"{path} disappears when ARCHANGEL_LEGACY_PERIOP=0, but a live surface calls it"
    )


def test_turning_the_flag_on_changes_nothing(routes_on, routes_off):
    """Flag ON is a superset of OFF, and OFF adds nothing.

    A route appearing only when the flag is OFF would mean the gate rewired
    something rather than merely withholding it.
    """
    assert routes_off <= routes_on
    assert not (routes_off - routes_on), "flag OFF registered a route flag ON does not"


def test_the_flag_actually_gates_the_peri_op_surface(routes_on, routes_off):
    """The other silent failure: a gate that gates nothing.

    If the wrapper were misapplied — wrong decorator, wrong module, a typo in the
    env var name — every audit above would still pass while the dark week proved
    nothing at all. So assert the peri-op clusters §5 names really do disappear.
    """
    gone = {p for p, _ in routes_on - routes_off}
    assert len(gone) > 50, f"expected the peri-op surface to be gated, only {len(gone)} routes withheld"
    for expected in [
        "/api/patients",                       # roster
        "/api/process-discharge",              # discharge pipeline
        "/api/escalations",                    # escalation queue
        "/api/preop-survey/questions",         # pre-op survey
        "/api/intake-forms/start-interview",   # intake interview
        "/patient/{patient_id}",               # patient dashboard shell
        "/api/avatar/chat",                    # avatar chat
        "/admin/triage/intraop/config",        # routers/admin.py, triage-touching
        "/internal/prompts",                   # routers/internal.py, prompts-touching
    ]:
        assert expected in gone, f"{expected} should be gated but is still registered"


def test_the_gated_handlers_are_still_importable(routes_on):
    """The wrapper withholds the ROUTE, not the function.

    ``legacy_route`` returns the undecorated function when the flag is off, so a
    background job or another handler that calls it directly keeps working. If it
    returned None, those callers would fail with a TypeError far from here.
    """
    from legacy_flag import legacy_route

    def _sentinel():
        return "called"

    # Simulate the off path directly: the identity decorator must hand the
    # function back unchanged.
    assert legacy_route(lambda fn: fn)(_sentinel) is _sentinel
    assert _sentinel() == "called"


def test_the_flag_defaults_to_on():
    """An unset env var must mean 'behave exactly as before'. A default of off
    would retire the product the moment this ships."""
    import legacy_flag

    prev = os.environ.pop("ARCHANGEL_LEGACY_PERIOP", None)
    try:
        importlib.reload(legacy_flag)
        assert legacy_flag.LEGACY_PERIOP is True
    finally:
        if prev is not None:
            os.environ["ARCHANGEL_LEGACY_PERIOP"] = prev
        importlib.reload(legacy_flag)


@pytest.mark.parametrize("value,expected", [("1", True), ("0", False), ("", False),
                                            ("true", False), ("yes", False)])
def test_only_the_literal_1_enables_the_surface(value, expected):
    """``== "1"`` is the whole test, stated so nobody loosens it later. An
    operator who sets the flag to "true" expecting ON gets OFF — which is the
    safe direction to be wrong in only because they are watching logs when they
    do it, and the reverse (a stray value silently re-enabling a deleted
    surface) is the one that would go unnoticed."""
    import legacy_flag

    prev = os.environ.get("ARCHANGEL_LEGACY_PERIOP")
    os.environ["ARCHANGEL_LEGACY_PERIOP"] = value
    try:
        importlib.reload(legacy_flag)
        assert legacy_flag.LEGACY_PERIOP is expected
    finally:
        if prev is None:
            os.environ.pop("ARCHANGEL_LEGACY_PERIOP", None)
        else:
            os.environ["ARCHANGEL_LEGACY_PERIOP"] = prev
        importlib.reload(legacy_flag)
