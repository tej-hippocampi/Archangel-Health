"""Landing lead-capture endpoint (POST /api/leads).

Self-contained: mounts just the leads router on a throwaway TeamStore so the
test needs none of the full app's heavy import chain. EMAIL_DEV_MODE makes
send_html_email succeed without a transport, so the handler exercises the real
store + email path.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("EMAIL_DEV_MODE", "1")  # send_html_email -> success, no network
os.environ.setdefault("RATE_LIMIT_ENABLED", "0")

from routers.leads import router as leads_router  # noqa: E402
from team_store import TeamStore  # noqa: E402


@pytest.fixture()
def store(tmp_path):
    return TeamStore(db_path=str(tmp_path / "leads.db"))


@pytest.fixture()
def client(store):
    app = FastAPI()
    app.state.team_store = store
    app.include_router(leads_router)
    with TestClient(app) as c:
        yield c


def _count(store) -> int:
    with store._conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM lead_submissions").fetchone()[0]


def test_request_data_lead_stored_and_ok(client, store):
    r = client.post(
        "/api/leads",
        json={
            "source": "request_data",
            "email": "buyer@lab.com",
            "message": "Improving our medical model reasoning on hard cases.",
        },
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert _count(store) == 1


def test_provide_data_lead_stored(client, store):
    r = client.post(
        "/api/leads",
        json={
            "source": "provide_data",
            "email": "ops@nephro.org",
            "message": "De-identified EMR + outcomes across ~5k patients.",
        },
    )
    assert r.status_code == 200
    assert _count(store) == 1


def test_invalid_email_rejected(client, store):
    r = client.post("/api/leads", json={"source": "request_data", "email": "not-an-email", "message": "hi"})
    assert r.status_code == 422
    assert _count(store) == 0


def test_empty_message_rejected(client, store):
    r = client.post("/api/leads", json={"source": "request_data", "email": "a@b.com", "message": "   "})
    assert r.status_code == 422
    assert _count(store) == 0


def test_unknown_source_rejected(client, store):
    r = client.post("/api/leads", json={"source": "phishing", "email": "a@b.com", "message": "x"})
    assert r.status_code == 422
    assert _count(store) == 0


def test_honeypot_silently_dropped(client, store):
    r = client.post(
        "/api/leads",
        json={
            "source": "request_data",
            "email": "bot@spam.example",
            "message": "spam",
            "company_website": "http://spam.example",
        },
    )
    # Bots get a normal-looking 200, but nothing is stored or emailed.
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert _count(store) == 0


def test_health_system_partner_lead_stored(client, store):
    """The /partner one-pager link. Its six answers arrive folded into one
    labelled `message`, so the endpoint needs no schema change — only the new
    source label, which is what this asserts is actually wired."""
    r = client.post(
        "/api/leads",
        json={
            "source": "health_system_partner",
            "email": "d.reyes@stmarys.org",
            "message": (
                "Health system:\nSt Mary's Health\n\n"
                "Their role:\nCMIO\n\n"
                "Data they hold:\nEpic, ~12 years of nephrology encounters."
            ),
        },
    )
    assert r.status_code == 200
    assert _count(store) == 1
    with store._conn() as conn:
        row = conn.execute(
            "SELECT source, email FROM lead_submissions ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    # Stored under its own source so the health-system pipeline stays separable
    # from the generic provide_data one.
    assert row[0] == "health_system_partner"
    assert row[1] == "d.reyes@stmarys.org"


# ─── ?ref= on /partner is money ──────────────────────────────────────────────
# ``asclepius/referrals.py::partner_url`` has always built
# ``/partner?ref=CODE&hs=TOKEN``, but the page read only ``hs``. A physician who
# copied their plain referral link out of their own dashboard and sent it to a
# health system therefore got no credit for the introduction at all, which is
# the cheapest introduction we ever get and the one we most want repeated.


@pytest.fixture()
def referring_physician(monkeypatch):
    """A stand-in asclepius store holding one physician with one code.

    Faked rather than instantiated: the leads router resolves the code through
    ``asclepius.store.get_store`` and this file deliberately owns none of that
    import chain (see the module docstring). What is under test is the wiring,
    not the lookup, which has its own tests.
    """
    class _Store:
        def get_user_by_referral_code(self, code):
            if (code or "").strip().upper() != "DRTOBY7":
                return None
            return {"id": "user-toby", "full_name": "Toby Ferrand"}

        def get_user_by_id(self, uid):
            return {"id": "user-toby", "full_name": "Toby Ferrand"} if uid == "user-toby" else None

    import asclepius.store as asc_store
    monkeypatch.setattr(asc_store, "get_store", _Store, raising=False)
    return _Store()


def _partner_body(**overrides):
    body = {
        "source": "health_system_partner",
        "email": "cio@stmarys.org",
        "message": "Contact:\nDana Reyes\n\nHealth system:\nSt Mary's Health",
    }
    body.update(overrides)
    return body


def test_a_referral_code_on_the_link_is_attributed_to_the_physician(
        client, store, referring_physician):
    """Resolved to a user id at write time rather than stored raw: a code can be
    reissued, and the question this row answers months later is which PERSON
    made the introduction."""
    r = client.post("/api/leads", json=_partner_body(referral_code="DRTOBY7"))
    assert r.status_code == 200
    with store._conn() as conn:
        row = conn.execute("SELECT referred_by_user_id FROM lead_submissions "
                           "ORDER BY id DESC LIMIT 1").fetchone()
    assert row[0] == "user-toby"


def test_an_unknown_referral_code_is_a_silent_no_op(client, store, referring_physician):
    """A stale or mistyped code must never cost us the submission. The form is
    the thing we want completed; the attribution is a bonus on top of it."""
    r = client.post("/api/leads", json=_partner_body(referral_code="NOTACODE"))
    assert r.status_code == 200
    assert _count(store) == 1
    with store._conn() as conn:
        row = conn.execute("SELECT referred_by_user_id FROM lead_submissions "
                           "ORDER BY id DESC LIMIT 1").fetchone()
    assert row[0] is None


def test_a_lead_with_no_code_at_all_is_unattributed(client, store):
    """Most /partner visits are nobody's referral, and the column has to be able
    to say so. This also runs with no asclepius store patched in, which is the
    real deployment shape for the landing app."""
    assert client.post("/api/leads", json=_partner_body()).status_code == 200
    with store._conn() as conn:
        row = conn.execute("SELECT referred_by_user_id FROM lead_submissions "
                           "ORDER BY id DESC LIMIT 1").fetchone()
    assert row[0] is None


def test_the_referring_physician_reaches_the_admin_console(
        client, store, referring_physician, monkeypatch):
    """An attribution nobody can see is one nobody pays. By NAME, because the id
    is ours and the operator is deciding whether a physician's introduction
    earned something."""
    from asclepius import auth as asc_auth

    client.app.dependency_overrides[asc_auth.require_admin] = lambda: {"email": "f@x.org"}
    client.post("/api/leads", json=_partner_body(referral_code="DRTOBY7"))
    lead = client.get("/api/leads/admin").json()["leads"][0]
    assert lead["referred_by"] == "Toby Ferrand"


def test_the_partner_page_reads_ref_off_the_query_string():
    """Source-level, following ``test_landing_config``: the failure this catches
    is the page quietly going back to reading only ``hs``, which looks like
    nothing at all until a physician asks why their introduction was not
    credited."""
    tsx = (Path(__file__).resolve().parents[2] / "landing" / "src" / "app" /
           "components" / "PartnerInterest.tsx").read_text(encoding="utf-8")
    assert 'params.get("ref")' in tsx
    assert "referral_code" in tsx
