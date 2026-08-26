"""PRD demo-day P0-3 / P0-4 — a pending physician is TOLD they are pending.

Two halves of one gap. The portal said nothing (a 403 meaning "awaiting
credential verification" was handled exactly like an expired session: token
dropped, blank login form, no message), and the screen written to explain the
wait could not run (``public_user()`` never returned ``verification_status``, so
the Guide section gated on ``showWhen: 'pending'`` matched for no user in any
state).

What is asserted here:

  * ``public_user()`` carries ``verification_status`` — every state, including
    the NULL of a pre-verification-era account, which must stay distinguishable
    from ``pending``;
  * the gate's 403 carries a MACHINE-READABLE state alongside its prose, so the
    portal can tell "still waiting" from "refused" without matching on copy that
    a writer is free to edit;
  * the SSO bridge really does hand a pending clinician a working token and then
    403 the next call — the loop that made a normal waiting state look like a
    broken login — so the frontend's "drop the token and explain" path is
    written against real behavior, not an assumption.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests._asclepius import app, fresh_store, headers_for, make_user

from asclepius import auth as asc_auth

_PORTAL_JS = Path(__file__).resolve().parents[2] / "frontend" / "asclepius" / "asclepius.js"
_MANUAL_JS = Path(__file__).resolve().parents[2] / "frontend" / "asclepius" / "manual-content.js"


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


# ─── P0-4: the field the waiting screen is gated on actually ships ────────────
def test_public_user_carries_the_verification_status():
    store = fresh_store()
    user = make_user(store, role="evaluator")
    store.set_verification_status(user["id"], "pending")
    user = store.get_user_by_id(user["id"])

    pub = asc_auth.public_user(user)
    assert "verification_status" in pub, (
        "the Guide's awaiting-verification section is gated on this key; without "
        "it the comparison is `'pending' === undefined` and the section is dead "
        "code for every user in every state"
    )
    assert pub["verification_status"] == "pending"


def test_a_pre_verification_era_account_is_null_not_pending():
    """NULL means "predates the verification migration" and passes the gate.
    Coercing it to 'pending' would both lock those accounts out and show them a
    waiting screen for a wait that is not happening."""
    store = fresh_store()
    user = make_user(store, role="evaluator")
    pub = asc_auth.public_user(store.get_user_by_id(user["id"]))
    assert pub["verification_status"] is None


def test_an_approved_physician_reports_approved_over_the_wire(client):
    store = fresh_store()
    user = make_user(store, role="evaluator")
    store.set_verification_status(user["id"], "approved")
    res = client.get("/api/asclepius/auth/me", headers=headers_for(user))
    assert res.status_code == 200
    assert res.json()["verification_status"] == "approved"


# ─── P0-3: the 403 is legible to the client, not just to a human ──────────────
@pytest.mark.parametrize("status", ["pending", "rejected"])
def test_the_gate_403_carries_a_machine_readable_state(client, status):
    store = fresh_store()
    user = make_user(store, role="evaluator")
    store.set_verification_status(user["id"], status)

    # /auth/me is deliberately open to a physician awaiting verification (they
    # are inside the product now), so the gate is asserted where it actually
    # bites: the real-work surface.
    probe = (
        "/api/asclepius/auth/me" if status == "rejected" else "/api/asclepius/tasks/next"
    )
    res = client.get(probe, headers=headers_for(user))
    assert res.status_code == 403
    assert res.headers.get(asc_auth.AUTH_GATE_HEADER) == status, (
        "the portal shows a different screen for 'still waiting' than for "
        "'refused'; telling them apart by matching the detail string would "
        "break the moment the copy is reworded"
    )
    # The prose stays in `detail`, unchanged in shape, for every existing caller.
    assert isinstance(res.json()["detail"], str) and res.json()["detail"].strip()


def test_taxonomy_is_open_to_a_physician_awaiting_verification(client):
    """The boot sequence is /auth/me then /taxonomy. Both are now open while we
    check credentials: a signup lands IN the product and waits there, instead of
    bouncing off a wall in front of it."""
    store = fresh_store()
    user = make_user(store, role="evaluator")
    store.set_verification_status(user["id"], "pending")

    res = client.get("/api/asclepius/taxonomy", headers=headers_for(user))
    assert res.status_code == 200

    me = client.get("/api/asclepius/auth/me", headers=headers_for(user))
    assert me.status_code == 200
    assert me.json()["access_level"] == "provisional"
    # verification_status stays on the wire verbatim: four states must remain
    # distinguishable for the admin queue even though the gate sees three.
    assert me.json()["verification_status"] == "pending"


def test_the_real_work_surface_is_still_shut_for_the_same_physician(client):
    """The other half of the same change. Getting in must not mean getting
    patient data."""
    store = fresh_store()
    user = make_user(store, role="evaluator")
    store.set_verification_status(user["id"], "pending")

    for path in ("/api/asclepius/tasks/next", "/api/asclepius/tasks/available",
                 "/api/asclepius/me/stats"):
        res = client.get(path, headers=headers_for(user))
        assert res.status_code == 403, path
        assert res.headers.get(asc_auth.AUTH_GATE_HEADER) == "pending", path


def test_the_money_surfaces_open_and_read_zero(client):
    """Money is protected by there being none, not by hiding the page.

    A physician awaiting verification cannot draw a case, so cannot earn from
    one; their ledger reads zero because zero is true. Locking the tab as well
    made the product look empty at the exact moment we were trying to show
    them what they had joined -- and locking Referral cost us the referral,
    since the bounty pays on the referred doctor's first accepted case no
    matter when the introduction was made."""
    store = fresh_store()
    user = make_user(store, role="evaluator")
    store.set_verification_status(user["id"], "pending")

    earnings = client.get("/api/asclepius/earnings", headers=headers_for(user))
    assert earnings.status_code == 200
    assert earnings.json()["approved_cents"] == 0

    referrals = client.get("/api/asclepius/referrals", headers=headers_for(user))
    assert referrals.status_code == 200
    assert referrals.json()["invite_url"]


def test_a_role_gate_403_carries_no_verification_state(client):
    """`data_partner` and `buyer` are refused by the same dependency with the
    same status code. They are not waiting for anything, and dressing their 403
    as a waiting screen would tell a data provider their credentials are under
    review when no such review exists."""
    store = fresh_store()
    partner = make_user(store, role="data_partner")
    res = client.get("/api/asclepius/auth/me", headers=headers_for(partner))
    assert res.status_code == 403
    assert res.headers.get(asc_auth.AUTH_GATE_HEADER) is None


def test_an_admin_is_exempt_and_sees_no_gate(client):
    store = fresh_store()
    admin = make_user(store, role="admin")
    store.set_verification_status(admin["id"], "pending")
    res = client.get("/api/asclepius/auth/me", headers=headers_for(admin))
    assert res.status_code == 200
    assert res.json()["verification_status"] == "pending"


# ─── The SSO loop this was actually reported as ───────────────────────────────
def test_sso_mints_a_token_for_a_pending_clinician_who_lands_inside_the_product(client):
    """The exact sequence behind "the login just doesn't work".

    /auth/sso does not run the verification gate, so it returns 200 + a valid
    token. The client stores it, calls /taxonomy, and gets a 403. Leaving that
    token in localStorage made every reload resume it, 403 again, and land back
    on a blank form. The portal now drops it and renders the waiting screen —
    this test pins the server behavior that path is written against.
    """
    import tenant_jwt

    fresh_store()
    email = "sso-pending@hospital.example.org"
    token = tenant_jwt.create_tenant_staff_token(
        email=email,
        name="Dr Pending",
        role="doctor",
        health_system_id="hs-demo",
        tenant_slug="demo",
        health_system_code="DEMO",
    )

    res = client.post("/api/asclepius/auth/sso", json={"token": token})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body.get("token"), "SSO hands back a usable session token"
    # …and provisions the account pending, on purpose.
    assert body["user"]["verification_status"] == "pending"

    hdrs = {"Authorization": "Bearer " + body["token"]}
    # They are in the product: this used to 403 and drop them on a waiting wall.
    assert client.get("/api/asclepius/taxonomy", headers=hdrs).status_code == 200
    # And still cannot draw real work.
    gated = client.get("/api/asclepius/tasks/next", headers=hdrs)
    assert gated.status_code == 403
    assert gated.headers.get(asc_auth.AUTH_GATE_HEADER) == "pending"


# ─── The portal actually consumes it ──────────────────────────────────────────
#
# Source assertions, deliberately: the browser paths above have no DOM harness in
# this suite, and the failure mode being guarded is not "the code is wrong" but
# "nobody wired it up" — which is exactly what shipped. These are cheap and they
# fail loudly if a future edit drops the wiring again.
def test_the_portal_reads_the_gate_header_and_never_matches_on_prose():
    src = _PORTAL_JS.read_text(encoding="utf-8")
    assert asc_auth.AUTH_GATE_HEADER in src, (
        "asclepius.js must read the gate header to tell pending from rejected"
    )
    assert "renderAwaitingVerification" in src
    # The waiting screen must come from the manual's data, not a second copy of
    # the words that will drift from the first.
    assert "awaiting-verification" in src


def test_the_waiting_section_still_exists_in_the_manual():
    """The screen renders `manual-content.js`'s section by id. If the section is
    renamed or removed, the screen silently loses its body — so pin the id."""
    src = _MANUAL_JS.read_text(encoding="utf-8")
    assert re.search(r"id:\s*'awaiting-verification'", src)
    assert re.search(r"showWhen:\s*'pending'", src)


def test_the_gate_header_is_exposed_to_cross_origin_readers():
    """A custom response header is invisible to cross-origin JS unless CORS
    exposes it. The portal is same-origin today, so this changes nothing now —
    but if it is ever served from its own host, an unexposed header would make
    the waiting screen silently degrade to the bare login form it replaced, and
    the regression would look exactly like the original bug.

    The middleware carries the name as a literal (it is configured before
    asclepius.auth is imported), so the two are pinned to each other here.
    """
    import main
    from fastapi.middleware.cors import CORSMiddleware

    exposed = []
    for mw in main.app.user_middleware:
        if mw.cls is CORSMiddleware:
            exposed = list(mw.kwargs.get("expose_headers") or [])
    assert asc_auth.AUTH_GATE_HEADER in exposed, (
        f"CORS must expose {asc_auth.AUTH_GATE_HEADER!r}; exposed={exposed}"
    )


# ─── The portal renders the new state, rather than merely tolerating it ───────
#
# Source assertions, same reason as the block above: there is no DOM harness
# here, and the failure being guarded is "nobody wired it up", which is exactly
# what shipped last time.

def test_the_portal_reads_the_surface_list_and_denies_on_absent():
    src = _PORTAL_JS.read_text(encoding="utf-8")
    assert "function sessionHasSurface" in src
    # Deny-on-absent: an older token or a cached payload carrying no surface
    # list must not be read as permission.
    i = src.index("function sessionHasSurface")
    body = src[i:i + 400]
    assert "|| []" in body and "indexOf" in body


def test_a_locked_rail_item_is_shown_not_hidden():
    """Hiding the surfaces a provisional physician is about to get makes the
    product look empty at exactly the moment we are showing them what they
    joined. Capability-gated items still hide; surface-gated ones lock."""
    src = _PORTAL_JS.read_text(encoding="utf-8")
    assert "locked: !!it.surface && !sessionHasSurface(it.surface)" in src
    assert "'aria-disabled': item.locked ? 'true' : null" in src
    # A locked item opens the explanation instead of a destination that 403s.
    assert "if (item.locked) { setPanel('verification'); return; }" in src


def test_the_dashboard_does_not_call_gated_endpoints_while_provisional():
    """Two 403s would render 'we could not load your queue', which is a bug
    report. The truth is the queue does not exist for them yet."""
    src = _PORTAL_JS.read_text(encoding="utf-8")
    assert "const provisional = sessionIsProvisional();" in src
    assert "if (provisional) throw { __provisional: true };" in src


def test_the_waiting_copy_now_lives_inside_the_product():
    src = _PORTAL_JS.read_text(encoding="utf-8")
    assert "function provisionalBannerEl" in src
    assert "function renderVerificationPanel" in src
    # Built from the SAME manual section the old wall used, so the guide and the
    # panel cannot drift apart.
    assert "awaitingVerificationSection()" in src[src.index("function renderVerificationPanel"):]
