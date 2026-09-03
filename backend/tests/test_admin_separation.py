"""The separation IS the deliverable (PRD-F R1-R4, R6, R10).

Three properties, each of which was a real defect before this PR or would be a
real defect after it:

  1. **The physician page ships no admin code.** ``index.html`` loaded four
     admin bundles unconditionally, so every doctor downloaded roughly 3,600
     lines of console they could never run. That is the thing being fixed, so
     it is the thing asserted, by reading the HTML that ships.

  2. **The state keys survive byte for byte.** ``ADMIN_TASKS_REDESIGN.md``
     records why: ``work``, ``money``, ``tasks`` and ``assign`` are read by the
     alias table, the subnav lookups, ``openBatchesFor`` and the physician-row
     route-in. A move that renamed one would be silent breakage for zero
     benefit, and no screen would look wrong.

  3. **A non-admin is told the truth.** The API would 401 either way, so this
     is not a security assertion; it is an honesty one. A console that mounts
     for a physician and then fails every fetch reads as a broken product
     rather than a door they do not hold the key to.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402

client = TestClient(A.app)


@pytest.fixture
def store():
    return A.fresh_store()

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
_INDEX = (_FRONTEND / "index.html").read_text(encoding="utf-8")
_ADMIN_HTML = (_FRONTEND / "admin.html").read_text(encoding="utf-8")
_SHELL = (_FRONTEND / "admin_shell.js").read_text(encoding="utf-8")
_PORTAL = (_FRONTEND / "asclepius.js").read_text(encoding="utf-8")

#: Script tags only. The comment left where those four tags used to be names
#: the files on purpose, so a test that greps the whole document would fail on
#: the explanation of its own rule and teach the next person to delete it.
_SCRIPT_SRC = re.compile(r'<script[^>]*src="([^"]+)"')


def test_the_admin_page_is_served_and_the_physician_page_ships_no_admin_js():
    """WHY: R1 and R3 together are the deliverable. Serving the console is half
    of it; the half that pays for itself on every page load is the physician
    bundle no longer carrying a console nobody on it can open."""
    res = client.get("/asclepius/admin")
    assert res.status_code == 200, res.text
    assert "admin_shell.js" in res.text
    assert "Asclepius Operations" in res.text

    portal = client.get("/asclepius")
    assert portal.status_code == 200
    served = _SCRIPT_SRC.findall(portal.text)
    leaked = [s for s in served if "admin_" in s]
    assert not leaked, f"the physician page still loads admin code: {leaked}"

    # And the file on disk, not only what this route happened to render.
    assert not [s for s in _SCRIPT_SRC.findall(_INDEX) if "admin_" in s]


def test_the_console_page_does_not_load_the_physician_experience():
    """WHY: the separation has to cut both ways or it is half a move. The
    console has no queue, no case player, no first-run walkthrough and no
    earnings page, so loading those modules would be dead weight in the other
    direction and an invitation to mount one."""
    served = set(_SCRIPT_SRC.findall(_ADMIN_HTML))
    for physician_only in ("first_run.js", "earnings.js", "review.js",
                           "onboarding.js", "referral.js", "asclepius.js"):
        assert not any(s.endswith("/" + physician_only) for s in served), physician_only


def test_the_admin_shell_preserves_the_state_keys_and_the_alias_table():
    """WHY: F4. The redesign doc is explicit that renaming ``work``, ``money``,
    ``tasks`` or ``assign`` is silent breakage for zero benefit: the aliases,
    the subnav lookups and the physician-row route-in all read them, and none
    of those failures is visible on a screen.

    The five PRD tabs are asserted as (key, label) pairs so a future relabel
    cannot quietly take the key with it."""
    for pair in ("['physicians', 'Physicians']", "['work', 'Tasks']",
                 "['money', 'Money and Metrics']", "['data', 'Data']",
                 "['community', 'Community']"):
        assert pair in _SHELL, pair

    aliases = _SHELL.split("ADMIN_TAB_ALIASES = {")[1].split("};")[0]
    for legacy in ("tasks:", "qa:", "metrics:", "ingestion:", "health:",
                   "export:", "buyers:", "exports:"):
        assert legacy in aliases, legacy

    for sub in ("work: 'tasks'", "money: 'earnings'", "data: 'systems'",
                "export: 'bycase'"):
        assert sub in _SHELL, sub
    assert "['assign', 'Task Routing']" in _SHELL
    assert "['tasks', 'Data & Task Creation']" in _SHELL


def test_the_console_shell_left_the_physician_bundle():
    """WHY: R3 asks for a MOVE, and a move that copies is worse than no move:
    two renderers drift, and the one nobody is looking at is the one that goes
    wrong. So the console's entry points must be absent from asclepius.js, and
    the console button must be a link rather than a view switch."""
    for gone in ("function renderAdminView(", "function adminSectionCtx(",
                 "function renderAdminTasks(", "function renderAdminBatches(",
                 "function renderAdminMetrics(", "state.adminTab"):
        assert gone not in _PORTAL, f"{gone!r} is still in the physician bundle"
    assert "href: '/asclepius/admin'" in _PORTAL
    assert "'Admin console'" in _PORTAL, "the door itself must still be there"


def test_a_non_admin_session_gets_a_gate_and_no_console_furniture():
    """WHY: R4. The distinction between "no session" and "a session that is not
    an operator's" is the whole point: the first is a form to fill in, and
    printing a sign-in form at somebody who is already signed in is how a
    product teaches people to distrust it.

    Asserted on the shell's own branch rather than through a browser, because
    what must be true is that no admin section is REACHED, and a screenshot of
    a gate cannot prove that a fetch did not fire."""
    gate = _SHELL[_SHELL.index("function renderGate("):
                  _SHELL.index("function isAdminSession(")]
    assert "Admin credentials required" in gate
    assert "wrongAccount" in gate, "the two states must not be one screen"
    # The gate paints and stops. It must never fall through into the console.
    assert "renderAdminView" not in gate

    enter = _SHELL[_SHELL.index("async function enterConsole("):
                   _SHELL.index("async function boot(")]
    assert "if (!isAdminSession()) { renderGate(); return; }" in enter, (
        "a non-admin session must be refused BEFORE any section mounts")
    assert "ADMIN_ROLES = ['admin', 'qa_reviewer']" in _SHELL, (
        "F5: qa_reviewer keeps the door it had")


def test_the_server_still_refuses_a_physician_at_every_admin_endpoint(store):
    """WHY: F3 says the separation is hygiene and the boundary is the server.
    That claim is only worth writing down if it is checked: a reader could
    reasonably conclude from R4 that the gate is now doing the work."""
    physician = A.make_user(store, role="evaluator")
    headers = A.headers_for(physician)
    for path in ("/api/asclepius/admin/physicians",
                 "/api/asclepius/admin/batches",
                 "/api/asclepius/admin/hs-referrals",
                 "/api/asclepius/admin/community/summary"):
        res = client.get(path, headers=headers)
        assert res.status_code in (401, 403), f"{path} answered {res.status_code}"


def test_no_assistant_or_model_call_lives_on_the_console():
    """WHY: R10 is a locked decision, and the way it gets broken is not somebody
    building a chat panel on purpose. It is one convenience call to a model
    provider from a section, which is a second admin surface to secure arrived
    by accident.

    Narrow on purpose. ``/ingestion/cases/{id}/generate`` and the frontier-model
    metrics are model-ADJACENT work the console has always driven, and a guard
    that trips on them would be deleted within a week. What is forbidden is the
    console talking to a model itself, or growing a place to type at one."""
    for name in ("admin_shell.js", "admin_community.js", "admin_referrals.js",
                 "admin_physicians.js", "admin_health.js", "admin_export.js",
                 "admin_earnings.js"):
        src = (_FRONTEND / name).read_text(encoding="utf-8")
        for banned in ("api.anthropic.com", "api.openai.com", "chat/completions",
                       "/assist/prelabel", "window.claude", "EventSource("):
            assert banned not in src, f"{name} reaches for {banned}"

    # The composer is LINKED, not rebuilt (F7). One implementation of posting as
    # the persona, with the channel allow-list and the announce rule already
    # right; a second would be a second place to get the fan-out wrong.
    community = (_FRONTEND / "admin_community.js").read_text(encoding="utf-8")
    assert "'/community'" in community
    assert "/admin/community/post" not in community, (
        "the persona composer belongs to community.js, once")
