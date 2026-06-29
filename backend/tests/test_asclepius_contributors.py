"""Contributors view + tiered export tests (Contributors feature spec).

Covers the governing rule: "Export Data" ships Tier A credential attributes only;
"Further Credential Summary" releases the Tier B vault under NDA. Asserts the
hard Tier B leak gate, the org/contributor drill-down directory, the dossier
(PDF + JSON) generation + audit, and the per-org/per-contributor metrics.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import credentials as asc_credentials  # noqa: E402
from asclepius import pipeline as asc_pipeline  # noqa: E402
from asclepius import profiles as asc_profiles  # noqa: E402

client = TestClient(A.app)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    A.fresh_store()
    asc_profiles.clear_cache()

    async def _ok_critic(task, submission):
        return {"consistent": True, "issues": [], "skipped": True}

    async def _ok_grounding(task, submission):
        return {"grounding_ok": True, "issues": [], "skipped": True, "checked_anchors": 0}

    monkeypatch.setattr(asc_pipeline, "run_critic", _ok_critic)
    monkeypatch.setattr(asc_pipeline, "run_grounding_check", _ok_grounding)
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


def _admin_h():
    return A.headers_for(A.make_user(_store(), role="admin", email=f"admin-{uuid.uuid4().hex[:6]}@asclepius.example"))


_SHIP = {
    "degree": "MD",
    "board_certifications": "ABIM Nephrology (active)",
    "primary_specialty": "nephrology",
    "subspecialties": ["dialysis", "transplant"],
    "years_in_active_practice": 17,
    "active_practice": True,
    "practice_setting_type": "private_practice",
    "languages": ["English"],
    "fellowship_trained": True,
    "credentials_verified": True,
}
_VERIFY = {
    "full_legal_name": "Jane A. Doe, MD",
    "npi": "1234567893",
    "medical_license_number": "A-104872",
    "license_state": "CA",
    "medical_school": "UCSF",
    "residency": "Stanford",
    "fellowship": "UCLA",
    "practice_name": "Riverside Nephrology Associates",
    "practice_address": "1200 Riverside Dr, Sacramento, CA",
}


def _make_contributor(org="Riverside Nephrology Associates", verify=True):
    store = _store()
    user = A.make_user(store, role="evaluator", specialty="nephrology",
                       board_cert="board_certified_nephrology", years_experience=17,
                       organization=org)
    store.upsert_contributor_credentials(
        id_hashed=user["id_hashed"], user_id=user["id"], organization=org,
        role_title="Physician (MD)", credentials_verified=True,
        ship=_SHIP, verify=_VERIFY if verify else {},
    )
    return user


def _submit_export_ready(admin_h, ev_h):
    body = {
        "specialty": "nephrology", "difficulty": "hard", "max_labels": 1,
        "prompt": f"Hyperkalemia case {uuid.uuid4().hex[:8]}?",
        "candidate_answers": [{"id": "A", "text": "Calcium then dialyze."},
                              {"id": "B", "text": "Dialysate K+ 1.0."}],
    }
    tid = client.post("/api/asclepius/tasks", json={"tasks": [body]}, headers=admin_h).json()["created"][0]
    sid = "s-" + uuid.uuid4().hex[:12]
    r = client.post("/api/asclepius/submissions", json={
        "submission_id": sid, "task_id": tid, "verdict": "A_better",
        "chosen_id": "A", "rejected_id": "B", "time_spent_sec": 130,
        "chosen_revision": {"edited": False, "why_better_notes": "B over-lowers K+"},
        "rejected_critique": {"error_tags": ["dosing_error"], "why_worse": "too aggressive"},
    }, headers=ev_h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "export_ready"
    return sid


# ─── Directory / drill-down ───────────────────────────────────────────────────
def test_organizations_and_contributors_directory():
    admin_h = _admin_h()
    user = _make_contributor()
    _submit_export_ready(admin_h, A.headers_for(user))

    orgs = client.get("/api/asclepius/organizations", headers=admin_h).json()["organizations"]
    org = next(o for o in orgs if o["organization"] == "Riverside Nephrology Associates")
    assert org["contributor_count"] == 1
    assert org["record_count"] >= 1
    assert org["verified_count"] == 1
    assert org["last_labeled_at"]

    contributors = client.get(
        "/api/asclepius/contributors", headers=admin_h,
        params={"organization": "Riverside Nephrology Associates"},
    ).json()["contributors"]
    assert len(contributors) == 1
    c = contributors[0]
    assert c["id_hashed"] == user["id_hashed"]
    assert c["primary_specialty"] == "nephrology"
    assert c["record_count"] >= 1
    assert c["credentials_verified"] is True
    assert c["last_labeled_at"]


def test_contributor_profile_blurb_and_masked_verify():
    admin_h = _admin_h()
    user = _make_contributor()
    prof = client.get(f"/api/asclepius/contributors/{user['id_hashed']}", headers=admin_h).json()
    assert "Board-certified" in prof["blurb"]
    assert prof["buttons"] == ["export_data", "further_credential_summary"]
    # Tier B values are masked on the plain profile read.
    assert "verify" not in prof["credentials"]
    assert prof["credentials"]["has_verify_vault"] is True

    # include_verify exposes the vault (admin edit path).
    prof2 = client.get(
        f"/api/asclepius/contributors/{user['id_hashed']}",
        headers=admin_h, params={"include_verify": "true"},
    ).json()
    assert prof2["credentials"]["verify"]["npi"] == "1234567893"


# ─── Export Data (Tier A only) ────────────────────────────────────────────────
def test_contributor_export_is_tier_a_only():
    admin_h = _admin_h()
    user = _make_contributor()
    _submit_export_ready(admin_h, A.headers_for(user))

    manifest = client.post(
        f"/api/asclepius/contributors/{user['id_hashed']}/export",
        json={"profile": "default", "note": "buyer batch"}, headers=admin_h,
    ).json()
    assert manifest["record_count"] >= 1
    assert manifest["scope"]["type"] == "contributor"
    assert manifest["tier_b_leak_gate"] == "passed"
    assert manifest["filters"]["annotator_id_hashed"] == user["id_hashed"]

    out_dir = Path(manifest["dir_path"])
    jsonl = (out_dir / "records.jsonl").read_text()
    # No Tier B field name or value ever appears in the shipped batch.
    for forbidden in ("full_legal_name", "npi", "medical_license_number", "Jane A. Doe", "1234567893", "UCSF"):
        assert forbidden not in jsonl, forbidden
    # The datasheet carries the auto-generated aggregate credential line (Tier A).
    datasheet = (out_dir / "datasheet.md").read_text()
    assert "Contributor scope" in datasheet
    assert "Board-certified" in datasheet


def test_org_export_covers_all_contributors():
    admin_h = _admin_h()
    org = "Riverside Nephrology Associates"
    u1 = _make_contributor(org=org)
    u2 = _make_contributor(org=org)
    _submit_export_ready(admin_h, A.headers_for(u1))
    _submit_export_ready(admin_h, A.headers_for(u2))

    manifest = client.post(
        f"/api/asclepius/organizations/{org}/export",
        json={"profile": "default"}, headers=admin_h,
    ).json()
    assert manifest["scope"]["type"] == "organization"
    assert manifest["record_count"] >= 2
    assert set(manifest["filters"]["annotator_ids"]) == {u1["id_hashed"], u2["id_hashed"]}


# ─── Tier B leak gate (hard guardrail) ────────────────────────────────────────
def test_tier_b_leak_gate_blocks_export(tmp_path, monkeypatch):
    # A misconfigured buyer profile that maps a record field to a Tier B key name
    # must fail the WHOLE batch loudly (422), never ship.
    leaky = {
        "name": "leaky",
        "preference_variant": "flat",
        "record_types": ["preference"],
        "field_maps": {"flat": {}, "preference": {"flat": {
            "type": "type", "prompt": "prompt", "chosen": "chosen", "rejected": "rejected",
            "annotator_credential": "npi",  # <-- smuggles a Tier B key name
        }}},
    }
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    (pdir / "leaky.json").write_text(json.dumps(leaky))
    monkeypatch.setenv("ASCLEPIUS_PROFILES_DIR", str(pdir))
    asc_profiles.clear_cache()

    admin_h = _admin_h()
    user = _make_contributor()
    _submit_export_ready(admin_h, A.headers_for(user))

    r = client.post(
        f"/api/asclepius/contributors/{user['id_hashed']}/export",
        json={"profile": "leaky"}, headers=admin_h,
    )
    assert r.status_code == 422, r.text
    assert "npi" in r.text and "Tier B leak" in r.text


# ─── Further Credential Summary (dossier) ─────────────────────────────────────
def test_credential_summary_requires_ack():
    admin_h = _admin_h()
    user = _make_contributor()
    r = client.post(
        f"/api/asclepius/contributors/{user['id_hashed']}/credential-summary",
        json={"recipient": "Acme Labs", "acknowledged": False}, headers=admin_h,
    )
    assert r.status_code == 400
    assert "acknowledge" in r.text.lower()


def test_credential_summary_generates_pdf_json_and_audit():
    admin_h = _admin_h()
    user = _make_contributor()
    res = client.post(
        f"/api/asclepius/contributors/{user['id_hashed']}/credential-summary",
        json={"recipient": "Acme Verification Lab", "acknowledged": True}, headers=admin_h,
    ).json()
    sid = res["summary_id"]
    assert res["watermark"].startswith("CONFIDENTIAL")
    assert res["verification_handles"]["nppes_npi_lookup"].endswith("1234567893")

    base = f"/api/asclepius/contributors/{user['id_hashed']}/credential-summary/{sid}/download"
    pdf = client.get(base, headers=admin_h, params={"format": "pdf"})
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.content[:4] == b"%PDF"

    js = client.get(base, headers=admin_h, params={"format": "json"}).json()
    # Dossier carries Tier B + Tier A + the §9 notice, keyed by hashed id.
    assert js["hashed_annotator_id"] == user["id_hashed"]
    assert js["identifying_credentials"]["npi"] == "1234567893"
    assert js["credential_attributes"]["primary_specialty"] == "nephrology"
    assert "twenty-four (24) months" in js["non_circumvention_notice"]

    # Generation is audited.
    audit = client.get(
        f"/api/asclepius/contributors/{user['id_hashed']}/credential-summaries", headers=admin_h,
    ).json()["summaries"]
    assert any(s["summary_id"] == sid and s["recipient"] == "Acme Verification Lab" for s in audit)


# ─── Metrics ──────────────────────────────────────────────────────────────────
def test_metrics_org_and_contributor_with_last_labeled():
    admin_h = _admin_h()
    user = _make_contributor()
    _submit_export_ready(admin_h, A.headers_for(user))

    orgs = client.get("/api/asclepius/metrics/organizations", headers=admin_h).json()["organizations"]
    org = next(o for o in orgs if o["organization"] == "Riverside Nephrology Associates")
    assert org["last_labeled_at"]
    assert org["submission_count"] >= 1

    contribs = client.get("/api/asclepius/metrics/contributors", headers=admin_h).json()["contributors"]
    c = next(c for c in contribs if c["id_hashed"] == user["id_hashed"])
    assert c["last_labeled_at"]
    assert c["submission_count"] >= 1


# ─── Unit: blurb + leak scanner ───────────────────────────────────────────────
def test_blurb_and_leak_unit():
    blurb = asc_credentials.generalized_blurb(_SHIP)
    assert "Board-certified" in blurb and "fellowship-trained" in blurb and "NPI-verified" in blurb
    assert asc_credentials.find_tier_b_leak({"prompt": "x", "license": "CC", "annotator_credential": "y"}) is None
    assert asc_credentials.find_tier_b_leak({"a": {"npi": "1"}}) == "npi"
