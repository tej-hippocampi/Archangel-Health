"""Asclepius V4 Build Spec — Phase 1: completeness parser + tri-state.

The real de-identified partner bundle (nephrology_pgnmid_real.json, 73 entries) must
become a V4 case. It quarantined before this phase on a '+'-delimited required-
modalities declaration whose technique tokens ('pronase IF') could never match the
coarse Study.modality enum ('pathology'). Spec §Phase-1 acceptance, tests 1-8.
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import ingestion as I  # noqa: E402
from asclepius.adapters import fhir_r4  # noqa: E402

_REAL = Path(__file__).resolve().parent / "fixtures" / "nephrology_pgnmid_real.json"
_KEY = base64.urlsafe_b64encode(b"v4-phase1-completeness-key-32byte").decode()


@pytest.fixture(autouse=True)
def _iso(monkeypatch, tmp_path):
    A.fresh_store()
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))
    monkeypatch.setenv("ASCLEPIUS_ASSET_STORE", str(tmp_path / "assets"))
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", _KEY)
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


def _bytes() -> bytes:
    return _REAL.read_bytes()


def _ingest(store, raw: bytes, specialty="nephrology"):
    data = I.wrap_loose_files([{"filename": "bundle.json", "content": raw}], specialty=specialty)
    uid = store.new_upload_id()
    rp = I.store_raw(uid, data)
    store.insert_ingest_upload(upload_id=uid, link_id="L1", partner_id="P1",
                               filename="bundle.json", sha256=I.sha256_hex(data),
                               size_bytes=len(data), raw_path=rp, source_ip=None)
    s = I.process_upload(store, uid)
    s["upload_id"] = uid
    return s


def _case(store, summary):
    cases = store.list_ingest_cases(upload_id=summary["upload_id"])
    assert len(cases) == 1, cases
    return cases[0]


# ── test 1 — THE GATE ─────────────────────────────────────────────────────────
def test_real_bundle_ingests_not_quarantines():
    store = _store()
    summary = _ingest(store, _bytes())
    assert summary["status"] == "ingested", (summary,
        _case(store, summary)["report"].get("quarantine_reason"))
    assert _case(store, summary)["case"]["completeness_status"] == "verified"


# ── test 2 ────────────────────────────────────────────────────────────────────
def test_required_modalities_plus_delimited():
    frag = fhir_r4.parse(_bytes(), specialty="nephrology")
    mods = frag["_eval_task"]["required_modalities"]
    assert len(mods) == 8, mods
    assert "pronase IF" in mods and "LM" in mods and "hematology studies" in mods


# ── test 3 ────────────────────────────────────────────────────────────────────
def test_completeness_unresolved_does_not_quarantine():
    # A token we cannot resolve is a parser gap, not missing evidence: ingest +
    # flag unverified, never quarantine.
    case = {"lab_panels": [{"panel": "BMP", "results": [{"analyte": "Creatinine"}]}],
            "notes": [{"note_type": "Progress", "text": "renal note"}], "studies": []}
    comp = I.completeness_check(["a totally novel unparseable modality phrase here now"], case)
    assert comp["unresolved"] and not comp["missing"]
    # end to end: an unresolvable extra token must not flip the real bundle to quarantine
    bundle = json.loads(_bytes())
    for e in bundle["entry"]:
        r = e["resource"]
        if r.get("resourceType") == "Basic" and r.get("id") == "eval-task":
            for ext in r["extension"]:
                if ext["url"].endswith("required-modalities"):
                    # >4 words and not a known synonym → unresolved (parser gap), not
                    # missing evidence — must ingest + flag, never quarantine.
                    ext["valueString"] += " + zebrafish tangential imaging acquisition protocol variant"
    store = _store()
    summary = _ingest(store, json.dumps(bundle).encode())
    assert summary["status"] == "ingested"
    assert _case(store, summary)["case"]["completeness_status"] == "unverified"


# ── test 4 — the gate must STILL fire on genuinely-absent evidence ────────────
def test_completeness_true_missing_quarantines():
    # completeness_check unit: a recognised modality with no delivered evidence is
    # missing (the real bundle names 'pronase' in both the Media and a pathology
    # note, so a faithful drop must remove ALL pronase evidence).
    case = {"studies": [{"modality": "pathology", "label": "Renal biopsy PAS 400x",
                         "findings": "endocapillary proliferation"}],
            "lab_panels": [{"panel": "BMP", "results": [{"analyte": "Creatinine"}]}],
            "notes": [{"note_type": "Nephrology", "text": "AKI on CKD; biopsy done"}]}
    comp = I.completeness_check(["pronase IF", "EM"], case)
    assert "pronase IF" in comp["missing"] and "EM" in comp["missing"]
    # end to end: strip every pronase/paraffin mention → quarantine
    bundle = json.loads(_bytes())
    kept = []
    for e in bundle["entry"]:
        r = e["resource"]
        if r.get("resourceType") == "Media" and r.get("id") == "media-pronase":
            continue  # drop the pronase image
        if r.get("resourceType") == "DocumentReference":
            for cc in r.get("content") or []:
                att = cc.get("attachment") or {}
                if att.get("data"):
                    txt = base64.b64decode(att["data"]).decode("utf-8", "replace")
                    if "pronase" in txt.lower() or "paraffin" in txt.lower():
                        att["data"] = base64.b64encode(
                            b"Biopsy tissue received and reviewed; adequate for evaluation.").decode()
        kept.append(e)
    bundle["entry"] = kept
    store = _store()
    summary = _ingest(store, json.dumps(bundle).encode())
    assert summary["status"] == "quarantined", summary
    assert "pronase IF" in (_case(store, summary)["report"].get("completeness") or {}).get("missing", [])


# ── test 5 ────────────────────────────────────────────────────────────────────
def test_all_five_media_become_assets():
    from asclepius.cases import study_has_valid_asset
    frag = fhir_r4.parse(_bytes(), specialty="nephrology")
    assert len(frag["studies"]) == 5
    assert frag["_imaging_skipped"] == 0
    assert all(study_has_valid_asset(s) for s in frag["studies"])


# ── test 6 ────────────────────────────────────────────────────────────────────
def test_seven_source_refs_captured():
    store = _store()
    summary = _ingest(store, _bytes())
    case = _case(store, summary)["case"]
    assert len(case["source_refs"]) == 7
    assert all(r.get("identifier") for r in case["source_refs"])
    assert "grounded" not in case  # source_refs never feed annotator grounded


# ── test 7 ────────────────────────────────────────────────────────────────────
def test_no_phi_in_assembled_case():
    store = _store()
    summary = _ingest(store, _bytes())
    blob = json.dumps(_case(store, summary)["case"])
    for needle in ("Eleanor", "Price", "010-8821", "SYN-MRN", "958XX", "Sacramento"):
        assert needle not in blob, needle


# ── test 8 ────────────────────────────────────────────────────────────────────
def test_timeline_offsets_preserved():
    store = _store()
    summary = _ingest(store, _bytes())
    case = _case(store, summary)["case"]
    offsets = sorted(p.get("collected_offset_days") for p in case["lab_panels"])
    assert offsets == [-19, -5, -3, 0], offsets
    assert not (_case(store, summary)["report"].get("timeline") or {}).get("unresolved")
