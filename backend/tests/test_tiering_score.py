"""PRD C §2–§3 — hard gates, the score, and the properties that must hold forever.

The load-bearing property under test is finding 1: **TR is a per-domain role, not a seniority
rank.** The same physician is a different answer on a nephrology case and a cardiology one,
and every test here that looks like it is about arithmetic is really about that.

Path tests issue real HTTP requests (context pack §6): a test that builds state by calling the
store directly proves the store works, which is not the thing that was broken the last three
times.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests._asclepius import app, fresh_store, headers_for, make_user, uniq

from asclepius import calibration, capabilities, tiering

VALID_NPI = "1234567893"


# ─── fixtures ─────────────────────────────────────────────────────────────────

def _credentials(**over):
    """A complete, gate-passing credential record. Every test starts from a physician who
    SHOULD qualify and knocks out one thing, so a failure names the thing it knocked out."""
    creds = {
        "fullLegalName": "Jane Doe, MD",
        "npi": VALID_NPI,
        "degree": "MD",
        "primarySpecialty": "Nephrology",
        "licenseNumber": "MD-99881",
        "licenseState": "MA",
        "residencyCompleted": True,
        "residencyCompletionYear": 2010,
        "clinicalHalfDaysPerMonth": 8,
        "continuingCertification": True,
        "structuredReviewExperience": ["journal_peer_review"],
        "boardCertifications": [
            {"board": "American Board of Internal Medicine", "specialty": "Internal Medicine",
             "subspecialty": "Nephrology", "active": True},
        ],
        "fellowship": [{"institution": "Cedars-Sinai", "specialty": "Nephrology",
                        "year": "2010"}],
    }
    creds.update(over)
    return creds


def _attestations(**over):
    a = {
        "attestConfidentiality": True,
        "attestIndependentJudgment": True,
        "attestNoDisciplinaryAction": True,
        "consentCredentialShare": True,
        "noPhi": True,
    }
    a.update(over)
    return a


def _physician(store, *, creds=None, atts=None, npi_verified=1, specialty="nephrology"):
    user = store.provision_user(
        email=f"doc-{uniq()}@example.com", password="pw-12345678", role="evaluator",
        full_name="Jane Doe", specialty=specialty, npi=VALID_NPI,
        credentials=creds if creds is not None else _credentials(),
        attestations=atts if atts is not None else _attestations(),
    )
    if npi_verified is not None:
        store.set_npi_result(user["id"], {
            "result": "verified" if npi_verified else "not_found",
            "npi": VALID_NPI, "reason": None,
            "record": {"number": VALID_NPI, "status": "A", "last_name": "Doe",
                       "credential": "M.D.",
                       "taxonomy": {"desc": "Nephrology", "state": "MA",
                                    "license": "MD-99881"}},
        })
    # Gate A5 needs a loaded LEIE snapshot to answer anything but UNKNOWN.
    store.replace_leie_exclusions(
        [{"npi": "9999999999", "excl_type": "1128a1", "excl_date": "20200101"}],
        source_note="test snapshot")
    return store.get_user_by_id(user["id"])


def _passed_calibration(composite=0.92):
    return {"calibration_z": calibration.calibration_z(composite, []),
            "tr_gate_passed": True, "composite": composite}


def seed_calibration_items(store, specialty="nephrology", n=14, panel_n=3):
    """N admissible items keyed by a converged reference panel of ``panel_n`` judgments."""
    ids = []
    for i in range(n):
        judgments = [{"better_answer_id": "A", "break_step": "step_2",
                      "load_bearing": ["dose adjustment", "eGFR threshold"]}
                     for _ in range(panel_n)]
        key = calibration.aggregate_panel(judgments)
        item = store.upsert_calibration_item(
            specialty=specialty,
            vignette={"stem": f"Case {i}", "criteria": ["dose adjustment", "eGFR threshold"],
                      "answers": [{"id": "A", "text": "…"}, {"id": "B", "text": "…"}],
                      "steps": [{"id": "step_1", "text": "…"}, {"id": "step_2", "text": "…"}]},
            key=key)
        ids.append(item["item_id"])
    return ids


def seat_passing_calibration(store, user, specialty="nephrology", wrong=0):
    """Put a graded, TR-gate-passing attempt on file, the way production does: real items,
    real raw responses, real grading. ``wrong`` misses that many items so a test can walk a
    candidate down through the gate."""
    ids = seed_calibration_items(store, specialty)
    attempt = store.start_calibration_attempt(
        user_id=user["id"], specialty=specialty, item_ids=ids)
    responses = {}
    for n, item_id in enumerate(ids):
        miss = n < wrong
        responses[item_id] = {
            "better_answer_id": "B" if miss else "A",
            "break_step": "step_1" if miss else "step_2",
            "load_bearing": ["something else"] if miss
            else ["dose adjustment", "eGFR threshold"],
        }
    store.record_calibration_responses(attempt["attempt_id"], responses)
    result = calibration.grade(store.get_calibration_items(ids), responses)
    store.record_calibration_score(attempt["attempt_id"], result)
    return result


_DEFAULT = object()   # so `calibration_result=None` can mean "never sat the exam"


def _propose(store, user, domain, *, calibration_result=_DEFAULT, **kw):
    return tiering.propose(
        user, case_domain=domain,
        calibration=(_passed_calibration() if calibration_result is _DEFAULT
                     else calibration_result),
        leie_status=store.leie_status(VALID_NPI),
        **kw)


# ═══ DoD §7.1 — the whole point of the module ═════════════════════════════════

def test_nephrologist_is_TR_on_nephrology_and_admin_band_on_cardiology():
    """A nephrologist signing up is proposed TR on a nephrology case and lands in the ADMIN
    BAND on a cardiology case.

    Note what is asserted about cardiology and what is not: the score is still HIGH there
    (the six general-seniority terms carry ~2.2 on their own), which is precisely why a
    score-only reading of the PRD promotes a nephrologist onto a cardiology case. The TR
    eligibility rule is what blocks it, and the result is 'admin', never 'labeler' — being
    off-domain says nothing about whether they are a good labeler.
    """
    store = fresh_store()
    doc = _physician(store)

    on_domain = _propose(store, doc, "nephrology")
    assert on_domain["proposed_tier"] == capabilities.REVIEWER
    assert on_domain["features"]["domain_match"] == 1.0
    assert on_domain["tr_eligible"] is True

    off_domain = _propose(store, doc, "cardiology")
    assert off_domain["features"]["domain_match"] == 0.0
    assert off_domain["proposed_tier"] is None
    assert off_domain["band"] == "admin"
    assert "domain match for this case" in off_domain["tr_missing"]
    # The score alone would have promoted them — that is the trap this test exists for.
    assert off_domain["score"] > tiering.TR_THRESHOLD


def test_off_domain_subspecialty_certification_does_not_count():
    """`subspecialty_certified` is encoded as x · 1[domain_match > 0].

    Without the indicator, a board-certified nephrologist scores the subspecialty term on a
    cardiology case on the strength of holding *a* subspecialty. Verified here as a real
    failure of the naive encoding rather than a hypothetical one.
    """
    store = fresh_store()
    doc = _physician(store)
    assert _propose(store, doc, "nephrology")["features"]["subspecialty_certified"] == 1.0
    assert _propose(store, doc, "cardiology")["features"]["subspecialty_certified"] == 0.0


def test_specialty_only_match_scores_half_not_full():
    """Declared specialty without a subspecialty certification is 0.5, not 1.0 — enough to
    clear the TR eligibility floor, not enough to score like domain expertise."""
    store = fresh_store()
    creds = _credentials(boardCertifications=[
        {"board": "ABIM", "specialty": "Internal Medicine", "subspecialty": "", "active": True}],
        fellowship=[])
    doc = _physician(store, creds=creds)
    assert _propose(store, doc, "nephrology")["features"]["domain_match"] == 0.5


# ═══ DoD §7.2 — a hard gate is never overridden by a high score ═══════════════

@pytest.mark.parametrize("gate,creds_patch,atts_patch", [
    ("A3", {"degree": "RN"}, {}),
    ("A4", {"residencyCompleted": False}, {}),
    ("A6", {}, {"attestNoDisciplinaryAction": False}),
    ("A7", {}, {"attestConfidentiality": False}),
])
def test_a_failed_gate_blocks_regardless_of_score(gate, creds_patch, atts_patch):
    store = fresh_store()
    doc = _physician(store, creds=_credentials(**creds_patch),
                     atts=_attestations(**atts_patch))
    out = _propose(store, doc, "nephrology")
    assert out["gates"][gate]["state"] == tiering.FAIL
    assert out["proposed_tier"] is None
    assert out["band"] == "blocked"
    # The score is still computed and still high — the gate is what stops it, not a low score.
    assert out["score"] > tiering.TR_THRESHOLD
    assert any(r.startswith("BLOCKER") for r in out["reasons"])


def test_npi_not_found_is_a_gate_failure_not_a_scoring_note():
    """C-0.8. The legacy scorer treated a NOT_FOUND NPI as a ±0 note, so a physician with an
    academic email and a board certification still proposed 'labeler' on an NPI nobody could
    find. A1 is a hard gate."""
    store = fresh_store()
    doc = _physician(store, npi_verified=0)
    out = _propose(store, doc, "nephrology")
    assert out["gates"]["A1"]["state"] == tiering.FAIL
    assert out["proposed_tier"] is None


def test_a_duplicate_npi_fails_A1():
    store = fresh_store()
    doc = _physician(store)
    out = _propose(store, doc, "nephrology", duplicate_npi=True)
    assert out["gates"]["A1"]["state"] == tiering.FAIL
    assert out["proposed_tier"] is None


def test_excluded_physician_fails_A5():
    store = fresh_store()
    doc = _physician(store)
    store.replace_leie_exclusions([{"npi": VALID_NPI, "excl_type": "1128a1",
                                    "excl_date": "20210301"}])
    out = _propose(store, doc, "nephrology")
    assert out["gates"]["A5"]["state"] == tiering.FAIL
    assert out["proposed_tier"] is None


# ═══ The tri-state discipline, carried into the gates ════════════════════════

def test_unchecked_npi_is_unknown_not_a_failure():
    """'We could not check' is not 'this person does not exist'. It routes to the admin band,
    never to blocked — the same rule §1.1 enforces on verify_npi, one layer up."""
    store = fresh_store()
    doc = _physician(store, npi_verified=None)
    out = _propose(store, doc, "nephrology")
    assert out["gates"]["A1"]["state"] == tiering.UNKNOWN
    assert out["band"] == "admin"
    assert out["proposed_tier"] is None
    assert "A1" in out["gates_undetermined"]


def test_never_loaded_leie_is_unknown_for_everyone_not_clear():
    """An exclusion list that fails open is not a check. With no snapshot loaded, A5 answers
    UNKNOWN and every physician lands in the admin band — correct, and deliberately visible
    via GET /verify/leie/status so it is not mistaken for model indecision."""
    store = fresh_store()
    doc = _physician(store)
    out = tiering.propose(doc, case_domain="nephrology",
                          calibration=_passed_calibration(),
                          leie_status=store.leie_status(""))
    assert out["gates"]["A5"]["state"] == tiering.UNKNOWN
    assert out["proposed_tier"] is None


def test_no_calibration_means_not_TR_eligible():
    """§3.1 requires a calibration exam at the gate. Nobody is TR-eligible before sitting it,
    and the reason is named rather than folded into a low score."""
    store = fresh_store()
    doc = _physician(store)
    out = _propose(store, doc, "nephrology", calibration_result=None)
    assert out["tr_eligible"] is False
    assert "calibration exam at the TR gate" in out["tr_missing"]
    assert out["proposed_tier"] is None


# ═══ §3.3 — protected proxies, proven immobile at the encoder ════════════════

@pytest.mark.parametrize("key,values", [
    ("medicalSchool", [{"institution": "Harvard Medical School", "year": "1998"},
                       {"institution": "Ross University", "year": "2016"}]),
    ("medicalSchoolYear", ["1985", "2019"]),
    ("gradYear", ["1985", "2019"]),
    ("dateOfBirth", ["1955-01-01", "1995-01-01"]),
    ("sex", ["M", "F"]),
    ("gender", ["man", "woman"]),
    ("imgStatus", [True, False]),
    ("ecfmgCertified", [True, False]),
    ("practiceZip", ["02115", "39530"]),
    ("nameOrigin", ["anglo", "south_asian"]),
    ("selfRatedExpertise", [1, 10]),
])
def test_forbidden_credential_keys_change_the_score_by_exactly_zero(key, values):
    """A property test over the encoder, not a code-review convention.

    §3.3 lists what must never be collected, derived or logged. Some of it IS collected today
    (`medicalSchool` was on the signup form) and some of it lives in legacy rows. The
    guarantee that matters is not that the key is absent from the database — it is that
    varying it across its plausible range moves the score by 0.0.
    """
    store = fresh_store()
    scores = set()
    for value in values:
        doc = _physician(store, creds=_credentials(**{key: value}))
        out = _propose(store, doc, "nephrology")
        scores.add(round(out["score"], 12))
        assert key not in out["features"]
    assert len(scores) == 1, f"{key} moved the score: {scores}"


def test_pinned_features_are_real_rows_with_zero_weight():
    """§3.3: instantiate them as real features so their immobility is auditable, not implied."""
    weights = tiering.default_weights()
    for name in tiering.PINNED_ZERO:
        assert weights[name]["m"] == 0.0
        assert weights[name]["q"] == tiering.PINNED_PRECISION
        assert weights[name]["pinned"] == 1
    tiering.assert_pinned_immobile(weights)


def test_years_in_practice_is_binary_and_capped():
    """Finding 2: an inverse relationship with quality of care AND the most direct available
    proxy for age. A 3-year attending and a 40-year attending encode identically."""
    store = fresh_store()
    junior = _physician(store, creds=_credentials(residencyCompletionYear=2015))
    senior = _physician(store, creds=_credentials(residencyCompletionYear=1980))
    a = _propose(store, junior, "nephrology")
    b = _propose(store, senior, "nephrology")
    assert a["features"]["post_residency_ge_3yr"] == b["features"]["post_residency_ge_3yr"] == 1.0
    assert a["score"] == b["score"]


def test_a_fresh_graduate_does_not_clear_the_three_year_gate():
    store = fresh_store()
    import datetime
    this_year = datetime.datetime.utcnow().year
    doc = _physician(store, creds=_credentials(residencyCompletionYear=this_year))
    out = _propose(store, doc, "nephrology")
    assert out["features"]["post_residency_ge_3yr"] == 0.0
    assert "3 years post-residency" in out["tr_missing"]


def test_advisor_is_never_proposed():
    """A medical advisor is a negotiated relationship with equity attached. It is not the
    output of an NPI check, and the ladder must not be extended upward."""
    store = fresh_store()
    doc = _physician(store)
    for domain in ("nephrology", "cardiology", "oncology", None):
        assert _propose(store, doc, domain)["proposed_tier"] != capabilities.ADVISOR


def test_tier_vocabulary_comes_from_capabilities():
    """§8: capabilities.TIERS is the single enumeration. This module must not carry a second
    list of tier strings that can drift from it."""
    assert tiering.TL in capabilities.TIERS
    assert tiering.TR in capabilities.TIERS
    assert tiering.TL == capabilities.LABELER and tiering.TR == capabilities.REVIEWER


# ═══ §5.4 — the hand-off to measured quality ═════════════════════════════════

def test_measured_quality_is_structurally_zero_below_twenty_tasks():
    store = fresh_store()
    doc = _physician(store)
    out = _propose(store, doc, "nephrology", measured_quality_z=2.5, n_tasks=19)
    assert out["features"]["measured_quality_z"] == 0.0


def test_credentials_decay_once_measured_quality_is_available():
    """Credentials are scaffolding. At 50 completed tasks the credential features are decayed
    by 0.6 and measured quality carries the weight instead."""
    store = fresh_store()
    doc = _physician(store)
    before = _propose(store, doc, "nephrology", n_tasks=10)
    after = _propose(store, doc, "nephrology", measured_quality_z=1.0, n_tasks=50)
    assert after["credential_decay_applied"] is True
    for name in tiering.CREDENTIAL_FEATURES:
        if before["features"][name]:
            assert after["features"][name] == pytest.approx(
                before["features"][name] * tiering.CREDENTIAL_DECAY)
    # domain_match is NOT a credential and must not decay — domain stays load-bearing forever.
    assert after["features"]["domain_match"] == before["features"]["domain_match"]


# ═══ Reasons are legible ═════════════════════════════════════════════════════

def test_reasons_are_ranked_by_contribution_and_in_plain_words():
    """An admin who cannot see WHY will ignore the score, and then the scoring system is
    decoration."""
    store = fresh_store()
    doc = _physician(store)
    out = _propose(store, doc, "nephrology")
    ranked = [c["contribution"] for c in out["ranked_contributions"]]
    assert ranked == sorted(ranked, key=abs, reverse=True)
    assert any("domain expertise matches this case" in r for r in out["reasons"])
    assert not any(r.strip().startswith(("intercept", "domain_match")) for r in out["reasons"])


# ═══ PATH TESTS — real HTTP, real auth ═══════════════════════════════════════

def test_queue_and_tiering_endpoints_serve_the_proposal():
    store = fresh_store()
    doc = _physician(store)
    store.set_verification_status(doc["id"], "pending")
    admin = make_user(store, role="admin")
    client = TestClient(app)

    r = client.get("/api/asclepius/verify/queue?status=pending", headers=headers_for(admin))
    assert r.status_code == 200, r.text
    row = next(x for x in r.json()["queue"] if x["user_id"] == doc["id"])
    assert "tiering" in row
    assert row["tiering"]["gates"]["A1"]["state"] == tiering.PASS

    r = client.get(f"/api/asclepius/verify/tiering/{doc['id']}?case_domain=cardiology",
                   headers=headers_for(admin))
    assert r.status_code == 200, r.text
    assert r.json()["features"]["domain_match"] == 0.0
    assert r.json()["proposed_tier"] is None


def test_tiering_endpoint_requires_admin():
    store = fresh_store()
    doc = _physician(store)
    r = TestClient(app).get(f"/api/asclepius/verify/tiering/{doc['id']}",
                            headers=headers_for(doc))
    assert r.status_code in (401, 403)


def test_dossier_rescores_for_a_named_case_domain():
    store = fresh_store()
    doc = _physician(store)
    store.set_verification_status(doc["id"], "pending")
    admin = make_user(store, role="admin")
    client = TestClient(app)
    base = client.get(f"/api/asclepius/verify/queue/{doc['id']}",
                      headers=headers_for(admin)).json()
    assert base["tiering"]["features"]["domain_match"] == 1.0
    other = client.get(f"/api/asclepius/verify/queue/{doc['id']}?case_domain=oncology",
                       headers=headers_for(admin)).json()
    assert other["tiering"]["features"]["domain_match"] == 0.0


def test_leie_load_and_status_path():
    store = fresh_store()
    admin = make_user(store, role="admin")
    client = TestClient(app)
    csv_text = ("LASTNAME,FIRSTNAME,NPI,EXCLTYPE,EXCLDATE\n"
                "SMITH,JOHN,1234567893,1128a1,20200101\n"
                "NONPI,PERSON,0000000000,1128a1,20200101\n"
                "BLANK,PERSON,,1128b4,20200101\n")
    r = client.post("/api/asclepius/verify/leie/load",
                    json={"csv_text": csv_text, "source_note": "unit"},
                    headers=headers_for(admin))
    assert r.status_code == 200, r.text
    # Rows with no usable NPI are dropped, never name-matched: a false positive on this gate
    # ends someone's participation.
    assert r.json()["rows"] == 1
    assert client.get("/api/asclepius/verify/leie/status",
                      headers=headers_for(admin)).json()["gate_a5"] == "active"
    assert store.leie_status("1234567893") == "excluded"
    assert store.leie_status("1861763010") == "clear"
