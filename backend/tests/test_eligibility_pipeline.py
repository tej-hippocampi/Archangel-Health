"""Unit tests for pipeline-internal helpers."""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("UPLOAD_DIR", "/tmp/elysium-eligibility-tests")

from eligibility import pipeline as elig_pipeline  # noqa: E402
from eligibility import store as elig_store  # noqa: E402
from eligibility.pipeline import (  # noqa: E402
    _slice_by_anchors,
    _split_batch_payload,
)
from eligibility.parse_x12 import parse_x12_271  # noqa: E402


def _make_isa_header() -> str:
    return (
        "ISA*00*          *00*          *ZZ*SENDER         *ZZ*RECEIVER       "
        "*230101*0000*^*00501*000000001*0*T*:~"
        "GS*HB*SENDER*RECEIVER*20230101*0000*1*X*005010X279A1~"
    )


def test_split_batch_passes_through_single_x12():
    """A normal single-subscriber X12 should pass through unchanged."""
    payload = (
        _make_isa_header()
        + "ST*271*0001*005010X279A1~"
        + "NM1*IL*1*DOE*JANE****MI*1EG4TE5MK73~"
        + "EB*1*FAM*MA*****~"
        + "SE*4*0001~GE*1*1~IEA*1*000000001~"
    )
    out = _split_batch_payload([("file.x12", payload.encode())])
    assert len(out) == 1
    assert out[0][1] == "X12_271"


def test_split_batch_splits_multi_subscriber_x12_correctly():
    """Multi-ST envelope yields one fragment per ST that parses cleanly.

    The previous buggy implementation prepended an extra ``ST*27`` and produced
    malformed output; this test guards against regression.
    """
    payload = (
        _make_isa_header()
        + "ST*271*0001*005010X279A1~"
        + "NM1*IL*1*DOE*JANE****MI*1EG4TE5MK73~"
        + "EB*1*FAM*MA*****~"
        + "SE*3*0001~"
        + "ST*271*0002*005010X279A1~"
        + "NM1*IL*1*SMITH*JOHN****MI*2AB1CD2EF34~"
        + "EB*1*FAM*MB*****~"
        + "SE*3*0002~GE*2*1~IEA*1*000000001~"
    )
    out = _split_batch_payload([("multi.x12", payload.encode())])
    assert len(out) == 2, f"expected 2 fragments, got {len(out)}: {[o[0] for o in out]}"

    # Each fragment must parse, yield exactly one subscriber, and the subscribers
    # must be different — proving we didn't double-prefix or co-mingle.
    subs = []
    for fname, fmt, content in out:
        assert fmt == "X12_271"
        text = content.decode("utf-8")
        # No double "ST*27ST*27" — the regression we just fixed
        assert "ST*27ST*27" not in text
        ast = parse_x12_271(text)
        subs.append(ast.subscriber.get("last_name"))
    assert sorted(subs) == ["DOE", "SMITH"]


def test_split_batch_passes_through_pdf_and_csv():
    pdf_bytes = b"%PDF-1.4\nfake pdf\n"
    csv_bytes = b"first_name,last_name\nJane,Doe\n"
    out = _split_batch_payload([
        ("a.pdf", pdf_bytes),
        ("b.csv", csv_bytes),
    ])
    fmts = sorted(o[1] for o in out)
    assert fmts == ["CSV", "PDF"]


def test_split_batch_extracts_files_from_zip():
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("inner.csv", b"first_name,last_name\nJane,Doe\n")
        zf.writestr("inner.pdf", b"%PDF-1.4\nx\n")
    out = _split_batch_payload([("bundle.zip", buf.getvalue())])
    fmts = sorted(o[1] for o in out)
    assert fmts == ["CSV", "PDF"]


def test_split_batch_handles_corrupt_zip_gracefully():
    out = _split_batch_payload([("bad.zip", b"PK\x00\x00 not really a zip")])
    assert len(out) == 1
    assert out[0][1] == "OTHER"


# ─── Multi-patient anchor splitting ─────────────────────────────────────────


def test_slice_by_anchors_splits_in_document_order():
    """Anchors located out-of-order in the segment list still produce
    document-ordered slices."""
    text = (
        "HEADER A\nMargaret O'Sullivan\n... her record ...\n"
        "HEADER B\nRobert Hayes\n... his record ...\n"
        "HEADER C\nDorothy Chen\n... her record ...\n"
    )
    segments = [
        {"firstName": "Robert", "sectionAnchor": "HEADER B\nRobert Hayes"},
        {"firstName": "Margaret", "sectionAnchor": "HEADER A\nMargaret O'Sullivan"},
        {"firstName": "Dorothy", "sectionAnchor": "HEADER C\nDorothy Chen"},
    ]
    out = _slice_by_anchors(text, segments)
    names = [seg["firstName"] for seg, _ in out]
    assert names == ["Margaret", "Robert", "Dorothy"]
    assert "Margaret O'Sullivan" in out[0][1] and "Robert Hayes" not in out[0][1]
    assert "Robert Hayes" in out[1][1] and "Dorothy Chen" not in out[1][1]
    assert "Dorothy Chen" in out[2][1]


def test_slice_by_anchors_falls_back_for_missing_anchor():
    """When a segment's anchor cannot be located, that segment receives the
    whole document text (better than silently dropping a patient)."""
    text = "HEADER A\nMargaret O'Sullivan\n... record ...\n"
    segments = [
        {"firstName": "Margaret", "sectionAnchor": "HEADER A\nMargaret O'Sullivan"},
        {"firstName": "Ghost", "sectionAnchor": "this anchor does not appear"},
    ]
    out = _slice_by_anchors(text, segments)
    names = [seg["firstName"] for seg, _ in out]
    assert "Margaret" in names and "Ghost" in names
    ghost_slice = next(s for seg, s in out if seg["firstName"] == "Ghost")
    assert ghost_slice == text


def test_slice_by_anchors_no_anchors_at_all_returns_whole_text():
    text = "single patient document"
    segments = [{"firstName": "Solo", "sectionAnchor": None}]
    out = _slice_by_anchors(text, segments)
    assert len(out) == 1
    assert out[0][1] == text


# ─── Multi-patient batch fan-out ────────────────────────────────────────────


def _fake_run_pipeline(check_id, patient, document_records, freeform_notes, surgery_date):
    async def _stub():
        rec = elig_store.get_check(check_id)
        if not rec:
            return
        rec["status"] = "DONE"
        rec["overall_verdict"] = "ELIGIBLE"
        if patient.get("eligibility_status") not in ("ELIGIBLE", "INELIGIBLE"):
            patient["eligibility_status"] = "ELIGIBLE"
    return _stub()


class _FakeAppState:
    def __init__(self):
        self.patient_store: dict = {}


class _FakeApp:
    def __init__(self):
        self.state = _FakeAppState()


def _new_batch_rec(batch_id: str) -> dict:
    """Build a batch record. Must be called inside a running event loop because
    ``new_check_queue`` constructs an ``asyncio.Queue`` (Python 3.9)."""
    return {
        "id": batch_id,
        "queue": elig_store.new_check_queue(),
        "ring": elig_store.ring_buffer(),
        "created": [],
        "needs_review": [],
        "errors": [],
    }


def _run(coro):
    """Run a coroutine without closing the loop afterwards.

    ``asyncio.run`` closes the default event loop on exit, which breaks
    downstream tests in this suite (Python 3.9) that rely on
    ``asyncio.get_event_loop()`` returning a usable loop.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


def test_segments_fanout_creates_one_patient_per_segment(monkeypatch, tmp_path):
    """5-patient PDF → 5 patients created, 5 checks enqueued, 5 prep notes."""
    elig_store.ELIGIBILITY_CHECKS.clear()
    elig_store.ELIGIBILITY_DOCS.clear()
    elig_store.BATCHES.clear()

    multi_text = (
        "HEADER A\nMargaret O'Sullivan\nMRN A\nPre-Op: Margaret prep instructions\n"
        "HEADER B\nRobert Hayes\nMRN B\nPre-Op: Robert prep instructions\n"
        "HEADER C\nPatricia Lin\nMRN C\nPre-Op: Patricia prep instructions\n"
        "HEADER D\nJames Whitfield\nMRN D\nPre-Op: James prep instructions\n"
        "HEADER E\nDorothy Chen\nMRN E\nPre-Op: Dorothy prep instructions\n"
    )

    fake_segments = {
        "extracted": {
            "patients": [
                {"firstName": "Margaret", "lastName": "O'Sullivan", "mbi": "1AA1A11AA01", "dob": "1953-04-12",
                 "surgeryDate": "2026-06-15", "anchorProcedure": "LEJR", "confidence": "HIGH",
                 "sectionAnchor": "HEADER A\nMargaret O'Sullivan",
                 "preOpInstructions": "Margaret prep instructions"},
                {"firstName": "Robert", "lastName": "Hayes", "mbi": "1AA1A11AA02", "dob": "1955-09-22",
                 "surgeryDate": "2026-06-20", "anchorProcedure": "HIP_FEMUR", "confidence": "HIGH",
                 "sectionAnchor": "HEADER B\nRobert Hayes",
                 "preOpInstructions": "Robert prep instructions"},
                {"firstName": "Patricia", "lastName": "Lin", "mbi": "1AA1A11AA03", "dob": "1962-07-08",
                 "surgeryDate": "2026-07-10", "anchorProcedure": "SPINAL_FUSION", "confidence": "HIGH",
                 "sectionAnchor": "HEADER C\nPatricia Lin",
                 "preOpInstructions": "Patricia prep instructions"},
                {"firstName": "James", "lastName": "Whitfield", "mbi": "1AA1A11AA04", "dob": "1948-11-30",
                 "surgeryDate": "2026-06-25", "anchorProcedure": "CABG", "confidence": "HIGH",
                 "sectionAnchor": "HEADER D\nJames Whitfield",
                 "preOpInstructions": "James prep instructions"},
                {"firstName": "Dorothy", "lastName": "Chen", "mbi": "1AA1A11AA05", "dob": "1950-02-14",
                 "surgeryDate": "2026-07-05", "anchorProcedure": "MAJOR_BOWEL", "confidence": "HIGH",
                 "sectionAnchor": "HEADER E\nDorothy Chen",
                 "preOpInstructions": "Dorothy prep instructions"},
            ]
        },
        "request_id": "fake",
    }

    async def _fake_segments(_text):
        return fake_segments

    monkeypatch.setattr(elig_pipeline, "extract_patient_segments", _fake_segments)
    monkeypatch.setattr(elig_pipeline, "run_pipeline", _fake_run_pipeline)

    fake_app = _FakeApp()

    async def _drive():
        batch_rec = _new_batch_rec("batch-test")
        await elig_pipeline._segments_extract_and_fanout(
            filename="multi.pdf",
            fmt="OTHER",
            content=multi_text.encode("utf-8"),
            llm_text=multi_text,
            hs_id=None,
            actor="tester",
            app=fake_app,
            batch_rec=batch_rec,
            original_doc_id=None,
            original_path=None,
        )
        await asyncio.sleep(0)
        return batch_rec

    batch_rec = _run(_drive())

    assert len(fake_app.state.patient_store) == 5
    assert len(batch_rec["created"]) == 5
    assert len(batch_rec["errors"]) == 0

    names = sorted(p["name"] for p in fake_app.state.patient_store.values())
    assert names == sorted([
        "Margaret O'Sullivan",
        "Robert Hayes",
        "Patricia Lin",
        "James Whitfield",
        "Dorothy Chen",
    ])

    for p in fake_app.state.patient_store.values():
        sd = p.get("structured_data") or {}
        assert sd.get("pre_op_instructions"), f"missing prep notes for {p['name']}"
        assert p["name"].split()[0].lower() in sd["pre_op_instructions"].lower()


def test_segments_fanout_single_patient_uses_fast_path(monkeypatch, tmp_path):
    """Single-patient files reuse the original raw bytes; 1 patient is created."""
    elig_store.ELIGIBILITY_CHECKS.clear()
    elig_store.ELIGIBILITY_DOCS.clear()
    elig_store.BATCHES.clear()

    text = "Solo Patient\nMRN X\nPre-Op: solo prep\n"

    async def _fake_segments(_text):
        return {
            "extracted": {
                "patients": [
                    {
                        "firstName": "Solo", "lastName": "Patient",
                        "mbi": "1AA1A11AA77", "dob": "1960-01-01",
                        "surgeryDate": "2026-08-01", "anchorProcedure": "LEJR",
                        "confidence": "HIGH",
                        "sectionAnchor": "Solo Patient",
                        "preOpInstructions": "Solo prep instructions",
                    }
                ]
            },
            "request_id": "fake",
        }

    monkeypatch.setattr(elig_pipeline, "extract_patient_segments", _fake_segments)
    monkeypatch.setattr(elig_pipeline, "run_pipeline", _fake_run_pipeline)

    fake_app = _FakeApp()

    # Stage a fake "original" file so the fast path can replace it.
    staged = tmp_path / "solo.pdf"
    staged.write_bytes(b"%PDF-1.4 fake bytes")

    async def _drive():
        batch_rec = _new_batch_rec("batch-single")
        await elig_pipeline._segments_extract_and_fanout(
            filename="solo.pdf",
            fmt="PDF",
            content=b"%PDF-1.4 fake bytes",
            llm_text=text,
            hs_id=None,
            actor="tester",
            app=fake_app,
            batch_rec=batch_rec,
            original_doc_id="docABC",
            original_path=str(staged),
        )
        await asyncio.sleep(0)
        return batch_rec

    _run(_drive())
    assert len(fake_app.state.patient_store) == 1
    pid = next(iter(fake_app.state.patient_store))
    p = fake_app.state.patient_store[pid]
    assert p["name"] == "Solo Patient"
    assert (p["structured_data"] or {}).get("pre_op_instructions") == "Solo prep instructions"


def test_segments_fanout_no_patients_records_file_error(monkeypatch, tmp_path):
    """Empty patient list → batch error, no patient created."""
    elig_store.ELIGIBILITY_CHECKS.clear()
    elig_store.ELIGIBILITY_DOCS.clear()
    elig_store.BATCHES.clear()

    async def _fake_segments(_text):
        return {"extracted": {"patients": []}, "request_id": "fake"}

    monkeypatch.setattr(elig_pipeline, "extract_patient_segments", _fake_segments)

    fake_app = _FakeApp()

    async def _drive():
        batch_rec = _new_batch_rec("batch-empty")
        await elig_pipeline._segments_extract_and_fanout(
            filename="empty.pdf",
            fmt="PDF",
            content=b"%PDF-1.4 nothing",
            llm_text="(no parseable patient data)",
            hs_id=None,
            actor="tester",
            app=fake_app,
            batch_rec=batch_rec,
            original_doc_id=None,
            original_path=None,
        )
        return batch_rec

    batch_rec = _run(_drive())
    assert len(fake_app.state.patient_store) == 0
    assert len(batch_rec["errors"]) == 1
    assert "No patients" in batch_rec["errors"][0]["error"]


# ─── v0.3 chunked segmentation ──────────────────────────────────────────────


def test_segment_document_single_chunk(monkeypatch):
    """Documents under ``SEGMENT_CHUNK_CHARS`` go through exactly one LLM call."""
    calls: list = []

    async def fake_seg(text):
        calls.append(len(text))
        return {
            "extracted": {
                "patients": [
                    {"mbi": "1EG4TE5MK73", "lastName": "Doe", "confidence": "HIGH"}
                ]
            }
        }

    monkeypatch.setattr(elig_pipeline, "extract_patient_segments", fake_seg)
    result = _run(elig_pipeline._segment_document("x" * 50_000))
    assert len(calls) == 1
    assert len(result) == 1


def test_segment_document_chunks_long_doc(monkeypatch):
    """Documents exceeding SEGMENT_CHUNK_CHARS get chunked with overlap."""
    calls: list = []

    async def fake_seg(text):
        calls.append(len(text))
        return {"extracted": {"patients": []}}

    monkeypatch.setattr(elig_pipeline, "extract_patient_segments", fake_seg)
    _run(elig_pipeline._segment_document("x" * (elig_pipeline.SEGMENT_CHUNK_CHARS * 3)))
    assert len(calls) >= 3, f"expected at least 3 chunks, got {len(calls)}"


def test_segment_document_dedupes_overlapping_patients(monkeypatch):
    """A patient appearing in two overlapping chunks shows up exactly once."""

    async def fake_seg(text):
        return {
            "extracted": {
                "patients": [
                    {"mbi": "1EG4TE5MK73", "lastName": "Doe", "confidence": "HIGH"},
                ]
            }
        }

    monkeypatch.setattr(elig_pipeline, "extract_patient_segments", fake_seg)
    result = _run(
        elig_pipeline._segment_document("x" * (elig_pipeline.SEGMENT_CHUNK_CHARS * 2))
    )
    assert len(result) == 1


def test_segment_document_dedupe_prefers_high_confidence(monkeypatch):
    """When two chunks return the same MBI with different confidence, keep HIGH."""
    seg_low = {"mbi": "1EG4TE5MK73", "lastName": "Doe", "confidence": "LOW"}
    seg_high = {
        "mbi": "1EG4TE5MK73",
        "lastName": "Doe",
        "confidence": "HIGH",
        "sectionAnchor": "Patient header line",
    }
    deduped = elig_pipeline._dedupe_segments([seg_low, seg_high])
    assert len(deduped) == 1
    assert deduped[0]["confidence"] == "HIGH"
    assert deduped[0].get("sectionAnchor") == "Patient header line"


def test_segment_document_dedupe_falls_back_to_name_dob_when_no_mbi():
    """No MBI? Use (lastName, firstName, dob) as the key."""
    a = {"firstName": "Alice", "lastName": "Smith", "dob": "1950-01-01", "confidence": "HIGH"}
    b = {"firstName": "Alice", "lastName": "Smith", "dob": "1950-01-01", "confidence": "MEDIUM"}
    c = {"firstName": "Bob", "lastName": "Smith", "dob": "1955-05-05", "confidence": "HIGH"}
    deduped = elig_pipeline._dedupe_segments([a, b, c])
    assert len(deduped) == 2
    names = sorted((s.get("firstName"), s.get("lastName"), s.get("confidence")) for s in deduped)
    assert names == [("Alice", "Smith", "HIGH"), ("Bob", "Smith", "HIGH")]


def test_segment_document_dedupe_drops_empty_records():
    """A segment with no MBI and no name/dob is dropped (nothing to identify)."""
    deduped = elig_pipeline._dedupe_segments([{"confidence": "HIGH"}])
    assert deduped == []


def test_batch_50_patients_no_truncation(monkeypatch):
    """End-to-end: a 100k+ char synthetic doc with 50 patient sections must
    surface 50 patients (no silent drops from the old 24K char cap)."""

    sections = []
    for i in range(50):
        sections.append(
            f"=== PATIENT {i + 1:02d} ===\n"
            f"Name: Patient {i + 1:02d}\n"
            f"MBI: 1EG4TE5MK{i:02d}\n"
            f"DOB: 1950-01-01\n"
            + ("Lorem ipsum " * 200)
            + "\n"
        )
    big_text = "\n".join(sections)
    assert len(big_text) > 100_000, f"expected synthetic doc >100k chars, got {len(big_text)}"

    pat_re = re.compile(r"=== PATIENT (\d+) ===")

    async def fake_seg(text):
        ids = pat_re.findall(text)
        return {
            "extracted": {
                "patients": [
                    {
                        "firstName": "Patient",
                        "lastName": str(int(i)),
                        "mbi": f"1EG4TE5MK{int(i) - 1:02d}",
                        "sectionAnchor": f"=== PATIENT {i} ===",
                        "confidence": "HIGH",
                    }
                    for i in ids
                ]
            }
        }

    monkeypatch.setattr(elig_pipeline, "extract_patient_segments", fake_seg)
    result = _run(elig_pipeline._segment_document(big_text))
    # Each patient appears in at least one chunk; overlapping chunks dedupe by MBI.
    assert len(result) == 50, f"expected 50 patients, got {len(result)}"


class _StubGen:
    """Reusable stub: bypasses Anthropic-backed material generation in tests."""

    async def generate(self, sd, pipeline_type):
        return ("voice script body", "<div>battlecard</div>")


class _StubElevenLabs:
    """Stub that returns a deterministic audio URL — emulates production behaviour
    when ``ELEVENLABS_API_KEY`` is configured."""

    async def synthesize(self, script, patient_id, voice_id=None):
        return f"/audio/{patient_id}.mp3"


class _StubElevenLabsUnconfigured:
    """Stub that returns None — emulates dev behaviour when the key is missing."""

    async def synthesize(self, script, patient_id, voice_id=None):
        return None


def _patch_generation(monkeypatch, *, elevenlabs_cls=_StubElevenLabs):
    import pipeline.generate as _gen_mod  # noqa: PLC0415
    import integrations.elevenlabs as _el_mod  # noqa: PLC0415
    monkeypatch.setattr(_gen_mod, "GenerationLayer", _StubGen)
    monkeypatch.setattr(_el_mod, "ElevenLabsClient", elevenlabs_cls)


def test_regenerate_materials_handles_resources_none(monkeypatch):
    """Regression: batch-onboarded patients are constructed with
    ``resources: None`` (explicit None, not absent). The earlier
    ``patient.setdefault("resources", {})`` returned None and crashed on
    ``resources[key] = ...`` — which surfaced as a 500 the first time a
    doctor clicked Confirm & Generate Preparation Materials. Lock the
    coercion-to-dict behaviour in."""
    _patch_generation(monkeypatch)

    patient = {
        "name": "Test Patient",
        "structured_data": {"patient_name": "Test Patient", "procedure_name": "LEJR"},
        "resources": None,  # ← the crash trigger
    }

    _run(elig_pipeline.regenerate_materials(patient, pipeline_type="pre_op", notes_text="prep notes"))

    assert isinstance(patient["resources"], dict)
    assert patient["resources"]["preop"]["voice_script"] == "voice script body"
    assert patient["resources"]["preop"]["battlecard_html"] == "<div>battlecard</div>"
    assert patient["voice_script"] == "voice script body"
    assert patient["battlecard_html"] == "<div>battlecard</div>"


def test_regenerate_materials_preserves_existing_resources(monkeypatch):
    """When ``resources`` already contains other keys (e.g. ``postop``), the
    pre-op generation must merge — not clobber — the dict."""
    _patch_generation(monkeypatch)

    patient = {
        "name": "Test Patient",
        "structured_data": {"patient_name": "Test Patient"},
        "resources": {"postop": {"voice_script": "preexisting", "battlecard_html": "<p>old</p>"}},
    }

    _run(elig_pipeline.regenerate_materials(patient, pipeline_type="pre_op", notes_text="prep"))

    assert "postop" in patient["resources"], "postop key must not be clobbered"
    assert "preop" in patient["resources"]
    assert patient["resources"]["postop"]["voice_script"] == "preexisting"


def test_regenerate_materials_synthesizes_voice_audio_in_production(monkeypatch):
    """Production parity: when ELEVENLABS_API_KEY is configured, voice audio
    must be synthesized and surfaced both at the top-level patient dict and
    inside resources[preop] — matching the legacy /api/onboard-patient flow
    so batch-onboarded patients get the same audio experience as demo ones."""
    _patch_generation(monkeypatch, elevenlabs_cls=_StubElevenLabs)

    patient = {
        "name": "Test Patient",
        "structured_data": {"patient_name": "Test Patient", "mbi": "1EG4TE5MK73"},
        "resources": None,
    }

    _run(elig_pipeline.regenerate_materials(patient, pipeline_type="pre_op", notes_text="prep"))

    assert patient["voice_audio_url"] == "/audio/1EG4TE5MK73_preop.mp3"
    assert patient["resources"]["preop"]["voice_audio_url"] == "/audio/1EG4TE5MK73_preop.mp3"


def test_regenerate_materials_handles_unconfigured_elevenlabs(monkeypatch):
    """Dev parity: when ELEVENLABS_API_KEY is missing, synth returns None and
    we still complete successfully — voice_audio_url is left absent (top
    level) and explicitly None inside resources[preop]. Patient page falls
    back to ⚠ Audio unavailable on the frontend."""
    _patch_generation(monkeypatch, elevenlabs_cls=_StubElevenLabsUnconfigured)

    patient = {
        "name": "Test Patient",
        "structured_data": {"patient_name": "Test Patient", "mbi": "1EG4TE5MK73"},
        "resources": None,
    }

    _run(elig_pipeline.regenerate_materials(patient, pipeline_type="pre_op", notes_text="prep"))

    # No top-level write — preserves any pre-existing legacy URL on merge.
    assert patient.get("voice_audio_url") is None
    # Still records the None so consumers can disambiguate "synth attempted, no URL"
    # from "synth never ran".
    assert patient["resources"]["preop"]["voice_audio_url"] is None
    # Voice script + battlecard still produced.
    assert patient["resources"]["preop"]["voice_script"] == "voice script body"
    assert patient["resources"]["preop"]["battlecard_html"] == "<div>battlecard</div>"


def test_run_batch_split_concurrency_capped(monkeypatch):
    """``run_batch`` must process splits with bounded concurrency (≤ SPLIT_CONCURRENCY)."""
    elig_store.ELIGIBILITY_CHECKS.clear()
    elig_store.ELIGIBILITY_DOCS.clear()
    elig_store.BATCHES.clear()

    in_flight = 0
    max_in_flight = 0
    lock = asyncio.Lock()

    async def fake_process_split(split, hs_id, actor, app, rec):
        nonlocal in_flight, max_in_flight
        async with lock:
            in_flight += 1
            if in_flight > max_in_flight:
                max_in_flight = in_flight
        # Simulate LLM latency so concurrency window is observable.
        await asyncio.sleep(0.05)
        async with lock:
            in_flight -= 1

    monkeypatch.setattr(elig_pipeline, "_process_batch_split", fake_process_split)

    # Stub _split_batch_payload so we don't need real X12/PDF/CSV fixtures.
    monkeypatch.setattr(
        elig_pipeline,
        "_split_batch_payload",
        lambda payloads: [(f"f{i}.pdf", "PDF", b"%PDF-1.4 stub") for i in range(20)],
    )

    fake_app = _FakeApp()

    async def _drive():
        rec = _new_batch_rec("batch-concurrency-test")
        rec["status"] = "RUNNING"
        elig_store.BATCHES["batch-concurrency-test"] = rec
        await elig_pipeline.run_batch(
            "batch-concurrency-test",
            payloads=[("dummy.pdf", b"x")],  # ignored — _split_batch_payload is stubbed
            hs_id=None,
            actor="tester",
            app=fake_app,
        )
        return rec

    _run(_drive())
    assert max_in_flight <= elig_pipeline.SPLIT_CONCURRENCY, (
        f"concurrency cap exceeded: peak={max_in_flight}, cap={elig_pipeline.SPLIT_CONCURRENCY}"
    )
    # Sanity: must have actually run multiple splits in parallel — otherwise the test is meaningless.
    assert max_in_flight >= 2, f"test ran sequentially (peak={max_in_flight})"
