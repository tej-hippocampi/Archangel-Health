"""What a physician can read back about themselves, and what they cannot.

Everything in the training-and-practice panel was already collected at signup
and already shown to the admin reviewing the application. The physician who
typed it could not see it back. That asymmetry is the bug, and it is also what
makes a completeness prompt honest: asking somebody to fill a gap they cannot
see is a demand, and showing them the gap makes it an offer.

The other half is what must never appear. The credential blob carries keys this
product deliberately does not use, and a profile page is exactly where they
would reappear by accident, because rendering "everything we hold" is the
obvious implementation.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

import tests._asclepius as A

from asclepius import tiering

client = TestClient(A.app)

_PROFILE = "/api/asclepius/me/profile"

_RICH = {
    "languages": ["English", "Urdu"],
    "subspecialties": ["Interventional nephrology"],
    "practiceSettings": ["Academic medical centre"],
    "yearsInActivePractice": 11,
    "residency": [{"program": "Mass General", "year": 2012}],
    "boardCertifications": [{"board": "ABIM", "subspecialty": "Nephrology", "active": True}],
    "structuredReviewExperience": ["journal peer review"],
    # Never-collect keys, planted here on purpose: a legacy blob can carry them,
    # and the profile must not render one just because it is present.
    "medicalSchool": "Somewhere Medical College",
    "gradYear": 2007,
    "dateOfBirth": "1981-04-02",
    "sex": "F",
    "imgStatus": True,
}


def _doc(store, creds=None):
    u = A.make_user(store, role="evaluator", specialty="nephrology")
    with store._conn() as conn:
        conn.execute("UPDATE users SET credentials_json = ?, verification_status = 'approved' "
                     "WHERE id = ?", (json.dumps(creds or {}), u["id"]))
    return store.get_user_by_id(u["id"])


# ─── What comes back ─────────────────────────────────────────────────────────
def test_the_training_and_practice_a_physician_typed_is_readable_back():
    store = A.fresh_store()
    doc = _doc(store, _RICH)
    body = client.get(_PROFILE, headers=A.headers_for(doc)).json()
    detail = body["training_and_practice"]

    assert detail["languages"] == ["English", "Urdu"]
    assert detail["subspecialties"] == ["Interventional nephrology"]
    assert detail["practice_settings"] == ["Academic medical centre"]
    assert detail["years_in_active_practice"] == 11
    assert detail["board_certifications"] and detail["residency"]


def test_an_absent_field_is_absent_rather_than_an_empty_row():
    """A key returned as [] renders as a heading with nothing under it, which
    reads like data somebody entered and then deleted."""
    store = A.fresh_store()
    doc = _doc(store, {"languages": ["English"], "subspecialties": []})
    detail = client.get(_PROFILE, headers=A.headers_for(doc)).json()["training_and_practice"]
    assert "languages" in detail
    assert "subspecialties" not in detail


# ─── What must never come back ───────────────────────────────────────────────
def test_no_never_collect_field_reaches_the_profile():
    """Medical school, graduation year, date of birth, sex and IMG status are
    absent from this product as a fairness position, not an oversight. A legacy
    blob can still carry them, so the panel whitelists keys rather than
    filtering a payload: a key nobody has decided to show stays invisible."""
    store = A.fresh_store()
    doc = _doc(store, _RICH)
    body = client.get(_PROFILE, headers=A.headers_for(doc)).json()

    # Keys checked structurally rather than as substrings: "age" lives inside
    # "languages", so a substring sweep reports a leak that is not there and
    # would train the next person to delete the assertion.
    forbidden = {k.casefold() for k in tiering.FORBIDDEN_CREDENTIAL_KEYS}

    def walk_keys(node):
        if isinstance(node, dict):
            for k, v in node.items():
                assert k.casefold() not in forbidden, f"profile leaks the key {k}"
                walk_keys(v)
        elif isinstance(node, list):
            for v in node:
                walk_keys(v)

    walk_keys(body)

    # And the VALUES, which is the leak that survives a key rename.
    blob = json.dumps(body).lower()
    for value in ("somewhere medical college", "1981-04-02", "2007"):
        assert value not in blob, f"profile leaks the value {value}"


def test_the_profile_still_carries_no_internal_score():
    store = A.fresh_store()
    doc = _doc(store, _RICH)
    blob = client.get(_PROFILE, headers=A.headers_for(doc)).text.lower()
    for leaked in ("tier_score", "contributor_score", "\"band\"", "reviewer band"):
        assert leaked not in blob


# ─── Completeness ────────────────────────────────────────────────────────────
def test_completeness_names_what_is_missing_in_the_order_we_would_ask():
    store = A.fresh_store()
    doc = _doc(store, {})
    comp = client.get(_PROFILE, headers=A.headers_for(doc)).json()["completeness"]

    assert comp["complete"] is False
    assert 0 <= comp["percent"] < 100
    fields = [m["field"] for m in comp["missing"]]
    assert fields[0] == "languages", "routing-relevant fields come first"
    assert all(m["label"] for m in comp["missing"]), "every gap needs words to ask with"


def test_a_full_profile_reads_complete_and_asks_for_nothing():
    """The other half: a meter that can never reach the end is a permanent
    reproach rather than a prompt."""
    store = A.fresh_store()
    doc = _doc(store, _RICH)
    with store._conn() as conn:
        conn.execute("UPDATE users SET avatar_asset_sha = 'sha', specialty_niche = 'CKD', "
                     "linkedin_url = 'https://example.org/in/x' WHERE id = ?", (doc["id"],))
    doc = store.get_user_by_id(doc["id"])

    comp = client.get(_PROFILE, headers=A.headers_for(doc)).json()["completeness"]
    assert comp["complete"] is True
    assert comp["percent"] == 100
    assert comp["missing"] == []


def test_completeness_gates_nothing():
    """It exists because a fuller profile routes better work, not as a
    requirement. An incomplete profile must still reach the product."""
    store = A.fresh_store()
    doc = _doc(store, {})
    assert client.get("/api/asclepius/me/stats",
                      headers=A.headers_for(doc)).status_code == 200


# ─── History ─────────────────────────────────────────────────────────────────
def test_stats_carry_a_monthly_series_and_no_quality_signal():
    """The one work-history surface the physician reads about themselves. The
    moment a field here derives from grading it is the internal score wearing a
    chart."""
    store = A.fresh_store()
    doc = _doc(store, _RICH)
    body = client.get("/api/asclepius/me/stats", headers=A.headers_for(doc)).json()

    assert "monthly" in body and isinstance(body["monthly"], list)
    blob = json.dumps(body).lower()
    for leaked in ("kappa", "agreement", "score", "band", "quality"):
        assert leaked not in blob, f"self stats leak {leaked}"
