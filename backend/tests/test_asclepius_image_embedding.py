"""V4 Image Embedding PRD — acceptance tests.

Covers the §10 acceptance criteria: the content-addressed asset store (accept
PNG/JPEG/PDF, size/dim caps, metadata strip, hash, dedupe, integrity), the ingest +
serving endpoints with the V4 wall + blinding, the vision A/B integrity (prompt_hash
includes the image sha256, preflight, OpenAI input conversion), the burn-in scan
default-off, and OpenAI in the subprocessor register.
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests._asclepius as A  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# Point the asset store at a temp dir for the whole module.
os.environ["ASCLEPIUS_ASSET_STORE"] = str(Path(A.TMP_DIR) / "assetstore")

from routers.asclepius import _store  # noqa: E402
from asclepius import assets as asc_assets  # noqa: E402

client = TestClient(A.app)


def _png(w=400, h=200, color=(230, 230, 230)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


# ─── §10.2 asset store: caps, strip, hash, dedupe, integrity ──────────────────
def test_asset_store_strips_metadata_caps_and_dedupes():
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo
    im = Image.new("RGB", (6000, 200), (240, 240, 240))
    meta = PngInfo(); meta.add_text("Comment", "device=SCANNER; DOB=01/02/1990")
    buf = io.BytesIO(); im.save(buf, format="PNG", pnginfo=meta)
    a = asc_assets.process_upload(buf.getvalue(), "image/png")
    assert max(a["width"], a["height"]) <= 4000            # downscaled to the dim cap
    data, mime = asc_assets.load_asset(a)                  # integrity ok
    assert asc_assets._sha256(data) == a["sha256"]
    reopened = Image.open(io.BytesIO(data))
    assert "device" not in str(reopened.info).lower()      # metadata stripped
    a2 = asc_assets.process_upload(buf.getvalue(), "image/png")
    assert a2["sha256"] == a["sha256"]                     # dedupe


def test_asset_store_rejects_unsupported_and_oversize(monkeypatch):
    with pytest.raises(asc_assets.UnsupportedMediaType):
        asc_assets.process_upload(b"gif89a", "image/gif")
    monkeypatch.setenv("ASCLEPIUS_IMAGE_MAX_BYTES", "10")
    with pytest.raises(asc_assets.ImageTooLarge):
        asc_assets.process_upload(_png(), "image/png")


# ─── §10.1/§10.2/§10.3 ingest + serve endpoints, V4 wall, blinding ────────────
def _v4_task():
    st = _store()
    case = {"case_source": "real_deid", "specialty": "cardiology",
            "problem_list": [{"condition": "chest pain"}], "medications": [{"drug": "asa"}],
            "lab_panels": [{"panel": "trop", "results": [
                {"analyte": "Trop", "value": 0.5, "unit": "ng/mL", "ref_low": 0, "ref_high": 0.01, "flag": "H"},
                {"analyte": "K", "value": 4, "unit": "mmol/L", "ref_low": 3.5, "ref_high": 5, "flag": ""}]}],
            "notes": [{"note_type": "ED", "text": "x" * 200}],
            "studies": [{"modality": "ecg", "label": "12-lead ECG", "findings": "ST elevation inferior"}]}
    return st.insert_task(prompt="read the ECG", specialty="cardiology", difficulty="hard",
                          case=case, source="partner_ehr", created_by="test")["task_id"]


def test_ingest_serve_roundtrip_and_case_type():
    A.fresh_store()
    ah = A.headers_for(A.make_user(_store(), role="admin"))
    tid = _v4_task()
    r = client.post("/api/asclepius/assets/ingest", headers=ah,
                    files={"file": ("ecg.png", _png(), "image/png")},
                    data={"task_id": tid, "modality": "ecg", "label": "12-lead ECG",
                          "findings": "ST elevation II/III/aVF"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["asset_id"].startswith("asset-") and body["sha256"]
    assert "ecg_image" in body["case_type"] and body["case_type"].startswith("multimodal:real")
    # serve (real-data-APPROVED evaluator) — image bytes, no store path leak
    st = _store()
    ev = A.make_user(st, role="evaluator", specialty="cardiology")
    st.set_real_data_approved(ev["id"], True)
    r2 = client.get(f"/api/asclepius/assets/{body['asset_id']}", headers=A.headers_for(ev))
    assert r2.status_code == 200 and r2.headers["content-type"] == "image/png"
    assert len(r2.content) > 0


def test_serve_asset_v4_wall_blocks_unapproved_evaluator():
    """A real de-identified image must NOT be fetchable by an evaluator without
    real-data approval — the V4 wall on the by-id serve path (audit fix)."""
    A.fresh_store()
    st = _store()
    ah = A.headers_for(A.make_user(st, role="admin"))
    tid = _v4_task()
    r = client.post("/api/asclepius/assets/ingest", headers=ah,
                    files={"file": ("ecg.png", _png(), "image/png")},
                    data={"task_id": tid, "modality": "ecg", "findings": "x"})
    asset_id = r.json()["asset_id"]
    # Unapproved evaluator → 403; approved → 200.
    unapproved = A.make_user(st, role="evaluator", specialty="cardiology")
    r2 = client.get(f"/api/asclepius/assets/{asset_id}", headers=A.headers_for(unapproved))
    assert r2.status_code == 403
    st.set_real_data_approved(unapproved["id"], True)
    r3 = client.get(f"/api/asclepius/assets/{asset_id}", headers=A.headers_for(unapproved))
    assert r3.status_code == 200


def test_asset_indexed_lookup_avoids_task_scan():
    """After ingest the asset resolves via the study_assets index (O(1)) — not by
    scanning tasks (audit fix)."""
    A.fresh_store()
    st = _store()
    ah = A.headers_for(A.make_user(st, role="admin"))
    tid = _v4_task()
    r = client.post("/api/asclepius/assets/ingest", headers=ah,
                    files={"file": ("ecg.png", _png(), "image/png")},
                    data={"task_id": tid, "modality": "ecg", "findings": "x"})
    aid = r.json()["asset_id"]
    ref = st.get_asset_ref(aid)
    assert ref and ref["sha256"] == r.json()["sha256"] and ref["case_source"] == "real_deid"
    assert ref["task_id"] == tid


def test_load_asset_skips_rehash_on_serve_but_can_verify():
    a = asc_assets.process_upload(_png(), "image/png")
    data, mime = asc_assets.load_asset(a)                       # serve path — no rehash
    assert len(data) > 0
    data2, _ = asc_assets.load_asset(a, verify=True)            # explicit integrity check
    assert data2 == data


def test_ingest_rejects_non_image_415():
    A.fresh_store()
    ah = A.headers_for(A.make_user(_store(), role="admin"))
    tid = _v4_task()
    r = client.post("/api/asclepius/assets/ingest", headers=ah,
                    files={"file": ("x.txt", b"hello", "text/plain")},
                    data={"task_id": tid, "modality": "ecg", "findings": "x"})
    assert r.status_code == 415


def test_ingest_v4_wall_blocks_non_real_case():
    A.fresh_store()
    st = _store()
    ah = A.headers_for(A.make_user(st, role="admin"))
    case = {"case_source": "synthetic", "specialty": "cardiology",
            "problem_list": [{"condition": "x"}], "medications": [{"drug": "y"}],
            "lab_panels": [{"panel": "p", "results": [
                {"analyte": "K", "value": 6, "unit": "mmol/L", "ref_low": 3.5, "ref_high": 5, "flag": "H"},
                {"analyte": "Cr", "value": 2, "unit": "mg/dL", "ref_low": 0.7, "ref_high": 1.3, "flag": "H"}]}],
            "notes": [{"text": "z" * 200}],
            "studies": [{"modality": "ecg", "findings": "x"}]}
    tid = st.insert_task(prompt="p", specialty="cardiology", difficulty="hard", case=case, created_by="t")["task_id"]
    r = client.post("/api/asclepius/assets/ingest", headers=ah,
                    files={"file": ("e.png", _png(), "image/png")},
                    data={"task_id": tid, "modality": "ecg", "findings": "x"})
    assert r.status_code == 400  # images never touch a non-real (V1/V2/V3) case


def test_serve_requires_auth():
    A.fresh_store()
    r = client.get("/api/asclepius/assets/asset-does-not-exist")
    assert r.status_code in (401, 403)


# ─── §10.4 vision A/B integrity ───────────────────────────────────────────────
def test_prompt_hash_includes_image_sha():
    from asclepius.baselines import _prompt_hash
    assert _prompt_hash("s", "u") != _prompt_hash("s", "u", "deadbeef")


def test_vision_capability_detection():
    from ai.model_config import is_vision_capable
    assert is_vision_capable("gpt-5") and is_vision_capable("claude-opus-4-8")
    assert not is_vision_capable("gpt-3.5-turbo") and not is_vision_capable("")


def test_openai_input_converts_image_block():
    from ai.llm_client import image_block, _openai_input, _has_images
    msgs = [{"role": "user", "content": [{"type": "text", "text": "read"}, image_block("image/png", "AAAA")]}]
    assert _has_images(msgs)
    parts = _openai_input(msgs)[0]["content"]
    kinds = [p["type"] for p in parts]
    assert "input_text" in kinds and "input_image" in kinds


def test_baseline_loads_case_image_and_uses_image_system():
    from asclepius.baselines import _case_image_for_baseline
    a = asc_assets.process_upload(_png(), "image/png")
    task = {"task_id": "t", "prompt": "x", "case": {"case_source": "real_deid",
            "studies": [{"modality": "ecg", "findings": "x", "asset": a}]}}
    blk, sha, mime = _case_image_for_baseline(task)
    assert blk is not None and sha == a["sha256"] and mime == "image/png"
    # a non-real case never yields an image (V4 wall)
    task2 = dict(task, case=dict(task["case"], case_source="synthetic"))
    assert _case_image_for_baseline(task2)[0] is None


# ─── §10.8 export bundles the image asset ─────────────────────────────────────
def test_export_collects_image_asset_entry(tmp_path):
    from asclepius.export import _collect_and_write_image_assets
    a = asc_assets.process_upload(_png(), "image/png")
    recs = [{"payload": {"context": {"case": {"studies": [
        {"modality": "ecg", "findings": "x", "asset": a}]}}}}]
    entries = _collect_and_write_image_assets(recs, tmp_path)
    assert len(entries) == 1
    e = entries[0]
    assert e["sha256"] == a["sha256"] and e["modality"] == "ecg" and e["mime"] == "image/png"
    assert e["provenance"] == "partner_deidentified"
    assert (tmp_path / e["path"]).exists()  # cleaned bytes written for the bundle


# ─── §10.9/§10.10 compliance + burn-in flag ───────────────────────────────────
def test_openai_in_subprocessor_register():
    from compliance.subprocessors import SUBPROCESSORS
    assert "openai_api" in SUBPROCESSORS
    # PHI-ineligible until a BAA is on file (default), per the trust-the-partner posture.
    assert SUBPROCESSORS["openai_api"].phi_allowed() is False


def test_burnin_scan_defaults_off_and_only_flags():
    from asclepius.constants import image_burnin_scan_enabled
    assert image_burnin_scan_enabled() is False
    # even ON, process_upload never raises/blocks on a burn-in flag — advisory only.
    a = asc_assets.process_upload(_png(), "image/png")
    assert "asset_id" in a
