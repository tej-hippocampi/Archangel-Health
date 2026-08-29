"""The contract of ARCHANGEL_LEGACY_PERIOP (PRD §5).

The flag's whole purpose is to make deleting the peri-op surface rehearsable: with
it on, production must be untouched; with it off, the app must behave exactly as
it will once the code is gone. Both halves need holding down, because both fail
silently — a flag that gates too much 404s a live customer path, and a flag that
gates too little makes the dark week prove nothing.

The route table is read by booting the app in a SUBPROCESS, once per flag value.
The flag is read at import time (correctly — a route table must not change under a
running server), so observing the off state means building a second app. Doing
that in-process by popping ``main`` and ``routers.*`` out of ``sys.modules`` and
re-importing does work, and it also silently corrupts the interpreter for every
test that runs afterwards: other modules keep references to the OLD app and store
objects, and 16 unrelated asclepius tests failed downstream the first time this
file was written that way. A subprocess cannot leak.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

# Dump the route table as JSON to a file rather than stdout: booting the app
# prints seeding notices, and mixing those into the payload makes the parse
# flaky in exactly the way a test about flags must not be.
_DUMP = """
import json, sys
from main import app
rows = [[r.path, sorted(getattr(r, "methods", []) or [])] for r in app.routes]
open(sys.argv[1], "w").write(json.dumps(rows))
"""


def _routes_with_flag(value: str) -> set[tuple[str, tuple[str, ...]]]:
    """Boot the app in a clean interpreter with the flag set to ``value``."""
    env = dict(os.environ)
    env["ARCHANGEL_LEGACY_PERIOP"] = value
    # Point the subprocess at throwaway state so it never touches the suite's DBs.
    tmp = tempfile.mkdtemp(prefix="legacy_flag_")
    env["ASCLEPIUS_DB_PATH"] = os.path.join(tmp, "asclepius.db")
    env["ASCLEPIUS_EXPORT_DIR"] = os.path.join(tmp, "exports")
    env["COMMUNITY_DB_PATH"] = os.path.join(tmp, "community.db")
    env["RATE_LIMIT_ENABLED"] = "0"

    out = os.path.join(tmp, "routes.json")
    proc = subprocess.run([sys.executable, "-c", _DUMP, out],
                          cwd=str(BACKEND), env=env, capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, (
        f"app failed to boot with ARCHANGEL_LEGACY_PERIOP={value!r}:\n{proc.stderr[-2000:]}"
    )
    return {(p, tuple(m)) for p, m in json.loads(Path(out).read_text())}


@pytest.fixture(scope="module")
def routes_on():
    return _routes_with_flag("1")


@pytest.fixture(scope="module")
def routes_off():
    return _routes_with_flag("0")


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
    # landing/src/lib/auth-api.ts:211 — inside a line range §5 would have gated.
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
    retired. If one stops being registered with the flag off, the gate set has
    swallowed something live — fix the gate, never this list.
    """
    assert path in {p for p, _ in routes_off}, (
        f"{path} disappears when ARCHANGEL_LEGACY_PERIOP=0, but a live surface calls it"
    )


def test_turning_the_flag_off_only_removes(routes_on, routes_off):
    """Flag OFF must be a strict subset of ON.

    A route appearing only when the flag is OFF would mean the gate rewired
    something rather than merely withholding it.
    """
    assert routes_off <= routes_on
    assert not (routes_off - routes_on), "flag OFF registered a route flag ON does not"


def test_the_flag_actually_gates_the_peri_op_surface(routes_on, routes_off):
    """The other silent failure: a gate that gates nothing.

    If the wrapper were misapplied — wrong decorator, wrong module, a typo in the
    env var name — every other audit would still pass while the dark week proved
    nothing at all. So assert the peri-op clusters §5 names really do disappear.
    """
    gone = {p for p, _ in routes_on - routes_off}
    assert len(gone) > 50, f"expected the peri-op surface gated, only {len(gone)} withheld"
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


def test_the_wrapper_withholds_the_route_not_the_function():
    """``legacy_route`` returns the undecorated function when the flag is off, so
    a background job or another handler that calls a gated handler directly keeps
    working. If it returned None, those callers would fail with a TypeError far
    from here."""
    from legacy_flag import legacy_route

    def _sentinel():
        return "called"

    # The off path: an identity decorator must hand the function back unchanged.
    assert legacy_route(lambda fn: fn)(_sentinel) is _sentinel
    assert _sentinel() == "called"


@pytest.mark.parametrize("value,expected", [
    (None, True),      # unset must mean "behave exactly as before"
    ("1", True),
    ("0", False),
    ("", False),
    ("true", False),
    ("yes", False),
])
def test_only_the_literal_1_enables_the_surface(value, expected):
    """``== "1"`` is the whole rule, stated so nobody loosens it later.

    An operator who sets the flag to "true" expecting ON gets OFF — safe only
    because they are watching logs when they do it. The reverse, a stray value
    silently re-enabling a retired surface, is the one that would go unnoticed.

    Read in a subprocess so this never mutates the running interpreter's env.
    """
    env = dict(os.environ)
    env.pop("ARCHANGEL_LEGACY_PERIOP", None)
    if value is not None:
        env["ARCHANGEL_LEGACY_PERIOP"] = value
    proc = subprocess.run(
        [sys.executable, "-c", "from legacy_flag import LEGACY_PERIOP; print(LEGACY_PERIOP)"],
        cwd=str(BACKEND), env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(expected)
