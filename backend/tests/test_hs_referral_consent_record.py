"""The claim we make on a physician's behalf, and the evidence for it.

We email a health-system contact in the referring physician's name, with their
address on the reply-to. The claim that email makes to its recipient is that
somebody they know asked us to write.

The checkbox that collected that claim was friction with NOTHING BEHIND IT:
`consent` was validated at the endpoint and then dropped. It was not on the
hs_referrals row, not in the audit event, nowhere. If a COO had ever said "your
platform emailed me claiming my colleague referred them", there was nothing to
produce.

So the checkbox is gone, replaced by an attestation stated beside the button,
the server gate is untouched, and the timestamp is recorded. That is strictly
more evidence than existed before, with less friction.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests._asclepius import app, fresh_store, headers_for, make_user


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _referrer(store):
    user = make_user(store, role="evaluator")
    store.set_verification_status(user["id"], "approved")
    return user


def _payload(**kw):
    body = {"contact_name": "James Okoye", "contact_email": "j.okoye@meridian.example",
            "hs_name": "Meridian Health", "relationship": "Not given", "consent": True}
    body.update(kw)
    return body


def test_the_attestation_is_recorded_and_not_merely_required(client):
    store = fresh_store()
    user = _referrer(store)
    res = client.post("/api/asclepius/referrals/health-system",
                      json=_payload(), headers=headers_for(user))
    assert res.status_code == 200, res.text

    rows = store.list_hs_referrals_for(user["id"]) if hasattr(
        store, "list_hs_referrals_for") else None
    if rows is None:                      # fall back to the raw table
        with store._conn() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM hs_referrals WHERE referrer_id = ?", (user["id"],))]
    assert rows, "the introduction was not recorded at all"
    assert rows[0]["consent_at"], "the attestation left no trace"


def test_the_server_still_refuses_an_introduction_without_it(client):
    """Defence in depth. The client sends it unconditionally now, so this gate
    exists to catch a client that stops. It is why the pydantic default stays
    False: a field defaulting to the value the gate wants is not a gate."""
    store = fresh_store()
    user = _referrer(store)
    res = client.post("/api/asclepius/referrals/health-system",
                      json=_payload(consent=False), headers=headers_for(user))
    assert res.status_code == 422


def test_a_row_written_before_the_column_existed_still_reads(client):
    """Additive migration, no backfill. NULL means the row predates this, which
    is exactly true, and nothing downstream may crash on it."""
    store = fresh_store()
    user = _referrer(store)
    client.post("/api/asclepius/referrals/health-system",
                json=_payload(), headers=headers_for(user))
    with store._conn() as conn:
        conn.execute("UPDATE hs_referrals SET consent_at = NULL WHERE referrer_id = ?",
                     (user["id"],))
    funnel = client.get("/api/asclepius/referrals", headers=headers_for(user))
    assert funnel.status_code == 200
    assert funnel.json()["health_systems"]
