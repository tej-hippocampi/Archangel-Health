"""The three qualifying questions, on the form that gets archived.

The Sep 1 meeting agreed three questions word for word: authority to license,
ability to de-identify and date-shift, and the rough shape of the data. They
lived in post-signup intake, which is a different document at a different time.

``docs/prds/prd-health-systems.md`` calls every /partner submission the legal
audit trail of an authority attestation. That sentence is only true if the
authority question is asked on the form being archived and the answer is kept as
the visitor gave it, so these tests follow one answer from the form component
through the row, the admin console and the founder notification, and assert at
every stop that nothing paraphrased it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("EMAIL_DEV_MODE", "1")
os.environ.setdefault("RATE_LIMIT_ENABLED", "0")

import routers.leads as leads_mod  # noqa: E402
from asclepius import auth as asc_auth  # noqa: E402
from team_store import TeamStore  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
PARTNER_TSX = REPO / "landing" / "src" / "app" / "components" / "PartnerInterest.tsx"
ADMIN_JS = REPO / "frontend" / "asclepius" / "admin_health.js"
ADMIN_CSS = REPO / "frontend" / "asclepius" / "asclepius.css"

AUTHORITY = "Yes, we can license de-identified clinical data to a commercial party"
DEID = "We can de-identify, but not date-shift"
SCALE = "around 80,000 patients over 12 years, mostly nephrology"


@pytest.fixture()
def store(tmp_path):
    return TeamStore(db_path=str(tmp_path / "leads.db"))


@pytest.fixture()
def sent(monkeypatch):
    """Every notification this test run would have emailed, as (to, subject, html).

    The founder notification is the only copy of a submission that a founder
    actually reads on the day it arrives, so it is asserted on directly rather
    than trusted to contain whatever the row contains.
    """
    box = []

    async def _capture(to, subject, html):
        box.append((to, subject, html))
        return True

    monkeypatch.setattr(leads_mod, "send_html_email", _capture)
    return box


@pytest.fixture()
def client(store):
    app = FastAPI()
    app.state.team_store = store
    app.include_router(leads_mod.router)
    # The admin reader is on the same router as the public write, deliberately.
    # Overridden rather than authenticated because who may read the leads is
    # test_hs_data_requests' subject, not this file's.
    app.dependency_overrides[asc_auth.require_admin] = lambda: {"email": "founder@x.org"}
    with TestClient(app) as c:
        yield c


def _submit(client, **overrides):
    body = {
        "source": "health_system_partner",
        "email": "cio@stmarys.org",
        "message": "Health system:\nSt Mary's Health",
        "authority_answer": AUTHORITY,
        "deidentification_answer": DEID,
        "data_scale_answer": SCALE,
    }
    body.update(overrides)
    return client.post("/api/leads", json=body)


def _row(store):
    with store._conn() as conn:
        return dict(conn.execute(
            "SELECT * FROM lead_submissions ORDER BY id DESC LIMIT 1").fetchone())


# ─── The row ─────────────────────────────────────────────────────────────────

def test_the_three_answers_land_in_their_own_columns(store):
    """A schema check, because the guarantee is about the SHAPE of the archive.
    An answer folded into the prose message is an answer a form redesign can
    reword out of existence without anything noticing, which is exactly the
    failure the audit-trail claim cannot survive."""
    with store._conn() as conn:
        names = {r[1] for r in conn.execute("PRAGMA table_info(lead_submissions)")}
    for col in ("authority_answer", "deidentification_answer", "data_scale_answer"):
        assert col in names, f"lead_submissions has no {col}"


def test_the_authority_answer_is_stored_exactly_as_it_was_given(client, store, sent):
    """Verbatim, not normalised to a token. A stored 'yes' is our summary of
    what they said; the sentence they picked is what they said, and only one of
    those is worth anything to whoever has to produce it later."""
    assert _submit(client).status_code == 200
    row = _row(store)
    assert row["authority_answer"] == AUTHORITY
    assert row["deidentification_answer"] == DEID
    assert row["data_scale_answer"] == SCALE


def test_a_form_that_never_asks_is_not_recorded_as_one_that_asked(client, store, sent):
    """The buyer and research forms have no qualifying questions. NULL there
    means "never asked"; an empty string would mean "asked and skipped". Losing
    that distinction turns three silent forms into three organizations that
    declined to attest."""
    assert _submit(client, source="request_data", authority_answer="",
                   deidentification_answer="", data_scale_answer="").status_code == 200
    assert _row(store)["authority_answer"] is None

    assert _submit(client, authority_answer="", deidentification_answer="",
                   data_scale_answer="").status_code == 200
    assert _row(store)["authority_answer"] == ""


def test_the_existing_fields_and_the_honeypot_still_behave(client, store, sent):
    """Three new questions are not a licence to disturb the parts of this form
    that already work. The trap in particular has to keep returning a cheerful
    200 while storing nothing, or a bot learns it was caught."""
    r = _submit(client, company_website="http://spam.example")
    assert r.status_code == 200 and r.json() == {"ok": True}
    with store._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM lead_submissions").fetchone()[0] == 0
    assert sent == []

    assert _submit(client, message="   ").status_code == 422
    assert _submit(client, source="not_a_form").status_code == 422


# ─── The admin console ───────────────────────────────────────────────────────

def test_the_answers_reach_the_admin_view_with_their_questions(client, store, sent):
    """An operator deciding whether a call is worth taking needs the answer AND
    the question it answers. Shipping the answers alone would put the wording of
    three questions in a second file, where they can drift from the form."""
    _submit(client)
    leads = client.get("/api/leads/admin").json()["leads"]
    pairs = {q["label"]: q["answer"] for q in leads[0]["qualifying"]}
    assert pairs == {
        "Authority to license": AUTHORITY,
        "De-identify and date-shift": DEID,
        "Patients, years, specialties": SCALE,
    }


def test_an_unanswered_question_reads_as_unanswered_not_as_absent(client, store, sent):
    """The gap this closes is the one that matters most. A health-system
    submission with no authority answer is not an attestation, and a console
    that renders it as a blank invites the reader to assume it was fine."""
    _submit(client, authority_answer="", deidentification_answer="",
            data_scale_answer="")
    leads = client.get("/api/leads/admin").json()["leads"]
    assert [q["answer"] for q in leads[0]["qualifying"]] == ["Not answered"] * 3


def test_a_buyer_lead_shows_no_qualifying_gaps_it_was_never_asked(client, store, sent):
    """Three "Not answered" lines under a lab's request would report a gap that
    does not exist: that form has no such questions and never did."""
    _submit(client, source="request_data", authority_answer="",
            deidentification_answer="", data_scale_answer="")
    leads = client.get("/api/leads/admin").json()["leads"]
    assert leads[0]["qualifying"] == []


# ─── The founder notification ────────────────────────────────────────────────

def test_the_founder_email_carries_all_three_answers(client, store, sent):
    """The email is what a founder reads on the day, usually on a phone, and it
    is where the decision to take the call gets made. An answer only in the
    database is an answer nobody acts on."""
    _submit(client)
    assert len(sent) == 1
    html = sent[0][2]
    for text in (AUTHORITY, DEID, SCALE):
        assert text in html, f"the notification email dropped {text!r}"
    for label in ("Authority to license", "De-identify and date-shift",
                  "Patients, years, specialties"):
        assert label in html


def test_the_founder_email_says_when_the_authority_question_went_unanswered(
        client, store, sent):
    """Same reason as the console. An email that quietly omits the question is
    indistinguishable from one where the answer was yes."""
    _submit(client, authority_answer="")
    html = sent[0][2]
    assert "Authority to license" in html and "Not answered" in html


def test_the_questions_are_worded_in_exactly_one_place():
    """Two copies of a question is how the console and the email end up telling
    two founders that an organization answered two different things."""
    labels = [label for _, label in leads_mod._QUALIFYING_QUESTIONS]
    assert len(labels) == 3 and len(set(labels)) == 3
    assert leads_mod._UNANSWERED, "an unanswered question renders as nothing"


# ─── The form that does the asking ───────────────────────────────────────────

def test_the_partner_form_asks_all_three_questions():
    """Source-level, following ``test_hs_signin_split``: there is no browser
    here, and the thing that actually breaks is a question quietly disappearing
    from the form while the column that archives it stays behind, which reads as
    working right up until somebody asks for the attestation."""
    tsx = PARTNER_TSX.read_text(encoding="utf-8")
    assert "authority to license de-identified clinical data to a commercial party" in tsx
    assert "Can you de-identify and date-shift?" in tsx
    assert "how many patients, over how many years" in tsx
    for field in ("authority_answer", "deidentification_answer", "data_scale_answer"):
        assert field in tsx, f"the form never sends {field}"


def test_the_form_will_not_submit_without_the_authority_attestation():
    """The claim in prd-health-systems.md is that the archive IS an attestation.
    A form that lets the authority question be skipped archives submissions that
    attest to nothing, and the count of those only becomes visible later."""
    tsx = PARTNER_TSX.read_text(encoding="utf-8")
    gate = tsx.split("const canSubmit =", 1)[1].split(";", 1)[0]
    assert "!!authority" in gate
    assert "!!deidentification" in gate
    # Still true of everything that was required before this change.
    for existing in ("emailValid", "name.trim()", "organization.trim()",
                     "dataHeld.trim()"):
        assert existing in gate


def test_every_class_the_lead_answers_use_has_a_rule():
    """A class with no rule renders as unstyled text in the middle of an admin
    card, where it reads as a bug rather than as a line."""
    js = ADMIN_JS.read_text(encoding="utf-8")
    css = ADMIN_CSS.read_text(encoding="utf-8")
    for cls in ("asc-hs-lead-qual", "asc-hs-lead-qual-q", "asc-hs-lead-qual-a",
                "asc-hs-lead-qual-none"):
        assert f"'{cls}" in js or f" {cls}'" in js, f"{cls} is on no element"
        assert f".{cls}" in css, f"{cls} has no rule in asclepius.css"
