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


# ── The client half ─────────────────────────────────────────────────────────
#
# The backend above shipped complete, with these tests, and nothing ever
# rendered it: community.js had no `preview` branch, so `/community?preview=1`
# fell through to /me, took the 403 and painted the gate. The applicant's rail
# sent them at a dead end that told them to come back when they were verified.
# These assertions hold the client half shut the same way.

import pathlib  # noqa: E402

_FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
_COMMUNITY_JS = (_FRONTEND / "community.js").read_text(encoding="utf-8")
_COMMUNITY_CSS = (_FRONTEND / "community.css").read_text(encoding="utf-8")
_PORTAL_JS = (_FRONTEND / "asclepius.js").read_text(encoding="utf-8")


def _boot_fn() -> str:
    start = _COMMUNITY_JS.index("async function boot()")
    return _COMMUNITY_JS[start:_COMMUNITY_JS.index("\n  function renderSignedOut", start)]


def test_the_client_reads_the_preview_flag_the_portal_sends():
    """asclepius.js opens '/community?preview=1' for an account without the
    surface. If the page ignores the parameter the whole feature is dark."""
    assert "preview" in _COMMUNITY_JS and "'1'" in _COMMUNITY_JS
    assert "async function bootPreview" in _COMMUNITY_JS


def test_a_refused_reader_is_offered_the_fixture_before_the_gate():
    """The gate is the dead end the founders walked into. A 403 now tries the
    preview first; the endpoint 404s anyone who can read the real rooms, so
    asking cannot hand a fixture to a colleague who should see the community."""
    boot = _boot_fn()
    gate = boot.index("renderGate()")
    assert "bootPreview()" in boot[:gate], "the 403 branch reaches the gate without trying the preview"


def test_a_session_less_visitor_never_reaches_the_preview():
    """Without this the preview is a public page that looks like a room full of
    real physicians, which is a credibility problem the first time it is
    screenshotted."""
    boot = _boot_fn()
    assert boot.index("renderSignedOut()") < boot.index("bootPreview()")


def test_the_preview_opens_no_socket_and_fetches_no_history():
    """There is no server state behind any of it, and openChannel would also
    mark messages read in rooms this account may not read."""
    start = _COMMUNITY_JS.index("async function bootPreview")
    body = _COMMUNITY_JS[start:_COMMUNITY_JS.index("\n  function renderSignedOut", start)]
    for forbidden in ("connectWs(", "openChannel(", "loadChannels(", "loadMembers(", "loadDms("):
        assert forbidden not in body, f"bootPreview reaches {forbidden}"


def test_the_preview_is_read_only_through_the_existing_switch():
    """canPost already hides the composer, reactions, pins, polls and
    bookmarks. Reusing it is what keeps a second read-only code path from
    existing and drifting."""
    start = _COMMUNITY_JS.index("async function bootPreview")
    body = _COMMUNITY_JS[start:_COMMUNITY_JS.index("\n  function renderSignedOut", start)]
    assert "state.canPost = false" in body


def test_the_banner_cannot_be_dismissed():
    """One sentence is what keeps a fixture from reading as real colleagues, so
    it sits outside the scroller and carries no close control."""
    assert "cm-preview-banner" in _COMMUNITY_JS
    assert "cm-preview-banner" in _COMMUNITY_CSS
    start = _COMMUNITY_JS.index("cm-preview-banner")
    block = _COMMUNITY_JS[start:start + 400]
    for dismissal in ("✕", "Dismiss", "onClick"):
        assert dismissal not in block, "the preview banner offers a way to close it"


def test_the_banner_sentence_comes_from_the_server():
    """Inventing the wording here means two sentences to keep true."""
    assert "state.previewBanner" in _COMMUNITY_JS


def test_the_portal_stops_asking_for_a_community_session_it_cannot_have():
    """Both community polls 403 for an applicant, every 60 seconds, forever.
    The rail sends them to the preview, which needs neither."""
    start = _PORTAL_JS.index("function pollCommunityOnce")
    body = _PORTAL_JS[start:start + 900]
    assert "sessionHasSurface('community_read')" in body
