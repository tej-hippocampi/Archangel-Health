"""Async orchestrator: parse → extract → evaluate. Emits SSE events.

Per-check runs emit to ``record["queue"]`` and also buffer in ``record["ring"]``
for late subscribers / reconnects (PRD §11.13). Per-batch runs emit similar
events on the batch record so the group-upload UI can render live progress.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from eligibility import evaluate as eval_mod
from eligibility import format_detect, store
from eligibility.extract import extract_eligibility, extract_identity, extract_patient_segments
from eligibility.parse_csv import CSVParseResult, format_for_llm as csv_format, parse_csv, split_by_mbi
from eligibility.parse_pdf import (
    PDFEncryptedError,
    PDFParseError,
    format_for_llm as pdf_format,
    parse_pdf,
)
from eligibility.parse_x12 import InvalidX12Error, X12_271_AST, format_for_llm as x12_format, parse_x12_271

log = logging.getLogger("eligibility.pipeline")

from tenant_constants import ARCH_TRIAGE_DEMO_HEALTH_SYSTEM_ID, TRIAGEDM_CLINIC_CODE

_TRIAGE_FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "demo_triage_team.json"


def _is_demo_mode_env() -> bool:
    return os.getenv("DEMO_MODE", "0").strip().lower() in ("1", "true", "yes", "on")


def _patient_is_triage_demo_clinic(patient: Dict[str, Any]) -> bool:
    if not _is_demo_mode_env():
        return False
    code = (patient.get("clinic_code") or "").strip().upper()
    if code == TRIAGEDM_CLINIC_CODE:
        return True
    hid = (patient.get("health_system_id") or "").strip()
    return hid == ARCH_TRIAGE_DEMO_HEALTH_SYSTEM_ID


def _load_triage_demo_extracted() -> Dict[str, Any]:
    raw = _TRIAGE_FIXTURE_PATH.read_text(encoding="utf-8")
    return json.loads(raw)


def _utc_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


# ─── SSE emit helpers ───────────────────────────────────────────────────────
async def _emit(record: Dict[str, Any], event: str, data: Any) -> None:
    payload = {"event": event, "data": data}
    record.setdefault("ring", store.ring_buffer()).append(payload)
    try:
        record["queue"].put_nowait(payload)
    except asyncio.QueueFull:
        # Drop rather than block the pipeline — SSE consumers can replay via ring
        log.warning("SSE queue full; dropping event %s", event)


# ─── Parsing router ─────────────────────────────────────────────────────────
def _parse_one(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Parse a document record from ``ELIGIBILITY_DOCS``. Returns a dict with
    ``llm_text`` (for the extractor) and ``parse_meta`` (for telemetry)."""
    path = Path(doc["path"])
    raw = path.read_bytes()
    fmt = doc.get("format") or "OTHER"
    meta: Dict[str, Any] = {
        "filename": doc.get("filename"),
        "format": fmt,
        "size_bytes": doc.get("size_bytes"),
    }
    try:
        if fmt == "X12_271":
            ast = parse_x12_271(raw.decode("utf-8", errors="replace"))
            meta["parsed"] = ast.to_dict()
            if ast.errors:
                meta["warnings"] = [f"AAA: {e.reject_reason}" for e in ast.errors]
            return {"llm_text": x12_format(ast), "parse_meta": meta, "parse_error": None}
        if fmt == "PDF":
            try:
                pdf = parse_pdf(raw)
            except PDFEncryptedError as e:
                return {
                    "llm_text": "",
                    "parse_meta": meta,
                    "parse_error": "PDF_ENCRYPTED",
                    "parse_error_message": str(e),
                }
            except PDFParseError as e:
                return {
                    "llm_text": "",
                    "parse_meta": meta,
                    "parse_error": "PDF_PARSE_FAILED",
                    "parse_error_message": str(e),
                }
            meta["pages"] = pdf.pages
            meta["ocr_used"] = pdf.ocr_used
            meta["ocr_unavailable"] = pdf.ocr_unavailable
            return {
                "llm_text": pdf_format(pdf, doc.get("filename")),
                "parse_meta": meta,
                "parse_error": None,
            }
        if fmt == "CSV":
            csv_res = parse_csv(raw)
            meta["rows"] = csv_res.row_count
            meta["needs_llm"] = csv_res.needs_llm
            meta["resolved_columns"] = list(csv_res.resolved.keys())
            return {
                "llm_text": csv_format(csv_res, doc.get("filename")),
                "parse_meta": meta,
                "parse_error": None,
            }
        # OTHER — best-effort: utf-8 decode
        text = raw.decode("utf-8", errors="replace")
        meta["note"] = "Non-eligibility file; raw text attached as context."
        return {"llm_text": f"=== OTHER FILE ({doc.get('filename')}) ===\n{text[:20000]}", "parse_meta": meta, "parse_error": None}
    except InvalidX12Error as e:
        return {"llm_text": "", "parse_meta": meta, "parse_error": "INVALID_X12", "parse_error_message": str(e)}
    except Exception as e:  # noqa: BLE001
        log.exception("Parse failure for %s", doc.get("filename"))
        return {"llm_text": "", "parse_meta": meta, "parse_error": "PARSE_FAILED", "parse_error_message": str(e)}


# ─── Main per-check pipeline ────────────────────────────────────────────────
async def run_pipeline(
    check_id: str,
    patient: Dict[str, Any],
    document_records: List[Dict[str, Any]],
    freeform_notes: str,
    surgery_date: str,
) -> None:
    record = store.get_check(check_id)
    if not record:
        log.error("run_pipeline: check %s not found", check_id)
        return

    t_start = datetime.utcnow()
    try:
        record["status"] = "PARSING"
        record["stage"] = "PARSING"
        await _emit(record, "status", {"stage": "PARSING"})

        if _patient_is_triage_demo_clinic(patient):
            import asyncio

            await asyncio.sleep(0.7)
            record["status"] = "EXTRACTING"
            record["stage"] = "EXTRACTING"
            await _emit(record, "status", {"stage": "EXTRACTING"})
            await asyncio.sleep(0.7)
            record["status"] = "EVALUATING"
            record["stage"] = "EVALUATING"
            await _emit(record, "status", {"stage": "EVALUATING"})
            extracted = _load_triage_demo_extracted()
            record["extracted_fields"] = extracted
            record["parse_meta"] = [{"note": "triage_demo_fixture", "demo": True}]
            verdicts = eval_mod.evaluate(extracted, surgery_date)
            verdicts = eval_mod.apply_overrides(verdicts, record.get("overrides") or {})
            overall = eval_mod.overall_verdict(verdicts)
            record["verdicts"] = verdicts
            record["overall_verdict"] = overall
            record["status"] = "DONE"
            record["stage"] = "DONE"
            record["finished_at"] = _utc_iso()
            record["duration_ms"] = int((datetime.utcnow() - t_start).total_seconds() * 1000)
            if patient.get("eligibility_status") not in ("ELIGIBLE", "INELIGIBLE"):
                patient["eligibility_status"] = overall
            await _emit(
                record,
                "result",
                {
                    "verdicts": verdicts,
                    "overallVerdict": overall,
                    "extractedFields": extracted,
                    "parseMeta": record["parse_meta"],
                    "durationMs": record["duration_ms"],
                },
            )
            store.append_audit(
                action="eligibility_check_completed",
                actor=record.get("actor") or "system",
                patient_id=record["patient_id"],
                check_id=check_id,
                after={"overall": overall, "triage_demo_fixture": True},
            )
            return

        parsed = [_parse_one(d) for d in document_records]
        parse_errors = [p for p in parsed if p.get("parse_error")]

        # Hard-fail if everything errored AND no freeform notes
        if parse_errors and len(parse_errors) == len(parsed) and not (freeform_notes or "").strip():
            first = parse_errors[0]
            msg = first.get("parse_error_message") or first.get("parse_error")
            record["status"] = "ERROR"
            record["error"] = msg
            await _emit(record, "error", {"message": msg, "code": first.get("parse_error")})
            return

        llm_blocks = [p["llm_text"] for p in parsed if p.get("llm_text")]
        record["parse_meta"] = [p["parse_meta"] for p in parsed]

        record["status"] = "EXTRACTING"
        record["stage"] = "EXTRACTING"
        await _emit(record, "status", {"stage": "EXTRACTING"})

        t_extract_start = datetime.utcnow()
        try:
            result = await extract_eligibility(llm_blocks, surgery_date, freeform_notes)
        except Exception as e:  # noqa: BLE001
            record["status"] = "ERROR"
            record["error"] = str(e)
            await _emit(record, "error", {"message": str(e), "code": "EXTRACT_FAILED"})
            return

        record["extracted_fields"] = result["extracted"]
        record["llm_request_id"] = result.get("request_id")
        record["extract_duration_ms"] = int(
            (datetime.utcnow() - t_extract_start).total_seconds() * 1000
        )

        record["status"] = "EVALUATING"
        record["stage"] = "EVALUATING"
        await _emit(record, "status", {"stage": "EVALUATING"})

        verdicts = eval_mod.evaluate(result["extracted"], surgery_date)
        verdicts = eval_mod.apply_overrides(verdicts, record.get("overrides") or {})
        overall = eval_mod.overall_verdict(verdicts)

        # OCR / LOW confidence nudge: if any PDF used OCR, cap confidence at LOW
        ocr_used = any(pm.get("ocr_used") for pm in record.get("parse_meta", []))
        if ocr_used:
            result["extracted"]["overallConfidence"] = "LOW"

        record["verdicts"] = verdicts
        record["overall_verdict"] = overall
        record["status"] = "DONE"
        record["stage"] = "DONE"
        record["finished_at"] = _utc_iso()
        record["duration_ms"] = int((datetime.utcnow() - t_start).total_seconds() * 1000)

        # Update patient's eligibility_status to reflect pipeline outcome (but
        # do not override an already-finalized state)
        if patient.get("eligibility_status") not in ("ELIGIBLE", "INELIGIBLE"):
            patient["eligibility_status"] = overall

        await _emit(
            record,
            "result",
            {
                "verdicts": verdicts,
                "overallVerdict": overall,
                "extractedFields": result["extracted"],
                "parseMeta": record["parse_meta"],
                "durationMs": record["duration_ms"],
            },
        )
        store.append_audit(
            action="eligibility_check_completed",
            actor=record.get("actor") or "system",
            patient_id=record["patient_id"],
            check_id=check_id,
            after={"overall": overall},
        )
    except Exception as e:  # noqa: BLE001
        log.exception("Pipeline failure for check %s", check_id)
        record["status"] = "ERROR"
        record["error"] = str(e)
        await _emit(record, "error", {"message": str(e), "code": "PIPELINE_FAILED"})


# ─── Material (re)generation, used by Track B notes-confirm ─────────────────
async def regenerate_materials(patient: Dict[str, Any], *, pipeline_type: str, notes_text: str) -> None:
    """Run the existing GenerationLayer against the patient's structured_data
    merged with the confirmed notes. Stores voice_script + battlecard_html back
    on the patient dict.

    ``pipeline_type`` is "pre_op" or "post_op".
    """
    try:
        # Late import to avoid circular import at module-init time
        from pipeline.generate import GenerationLayer
    except Exception as e:
        log.exception("Failed to import GenerationLayer")
        raise

    sd = dict(patient.get("structured_data") or {})
    if pipeline_type == "pre_op":
        sd["pre_op_instructions"] = notes_text
    else:
        sd["post_op_instructions"] = notes_text
        sd["discharge_notes"] = notes_text

    gen = GenerationLayer()
    voice_script, battlecard_html = await gen.generate(sd, pipeline_type)

    # Synthesize the voice audio so the patient pre-op/post-op page can play it.
    # Mirrors the legacy /api/process-patient and /api/onboard-patient flows so
    # batch-onboarded patients get the *same* audio experience in production.
    # ElevenLabsClient.synthesize() returns None when ELEVENLABS_API_KEY is
    # missing (dev), and the frontend falls back to an "Audio unavailable"
    # message — never a hard error.
    audio_suffix = "preop" if pipeline_type == "pre_op" else "postop"
    pid = (
        patient.get("id")
        or (patient.get("structured_data") or {}).get("mbi")
        or "unknown"
    )
    try:
        from integrations.elevenlabs import ElevenLabsClient  # late import; same as legacy flow
        voice_audio_url = await ElevenLabsClient().synthesize(
            voice_script, f"{pid}_{audio_suffix}"
        )
    except Exception as e:  # noqa: BLE001
        log.warning("ElevenLabs synth failed for %s (%s): %s", pid, audio_suffix, e)
        voice_audio_url = None

    patient["voice_script"] = voice_script
    patient["battlecard_html"] = battlecard_html
    if voice_audio_url:
        patient["voice_audio_url"] = voice_audio_url

    # NOTE: patients onboarded via the eligibility/batch path are constructed
    # with ``"resources": None`` explicitly, so ``setdefault`` returns None and
    # the assignment below blows up. Coerce to a dict before mutating.
    resources = patient.get("resources")
    if not isinstance(resources, dict):
        resources = {}
    key = "preop" if pipeline_type == "pre_op" else "postop"
    resources[key] = {
        "voice_script": voice_script,
        "battlecard_html": battlecard_html,
        "voice_audio_url": voice_audio_url,
    }
    patient["resources"] = resources


# ─── Group / batch fan-out ──────────────────────────────────────────────────
# Split on the boundary BEFORE each ST*270 / ST*271 segment header (lookahead
# so the delimiter stays inside the resulting fragment).
X12_STX_RE = re.compile(r"(?=ST\*27[01]\*)")
MBI_RE = re.compile(r"^[1-9][A-Z][A-Z0-9][0-9][A-Z][A-Z0-9][0-9][A-Z][A-Z][0-9]{2}$")

# ─── v0.3 scaling knobs ─────────────────────────────────────────────────────
# Conservative budget — Sonnet 4.6 handles 200K input tokens, but we want a
# generous safety margin and to keep individual calls well under 60s.
# ~60,000 chars ≈ 18,000 tokens.
SEGMENT_CHUNK_CHARS = 60_000
# When chunking is needed, overlap each chunk by this many chars so a patient
# section straddling a boundary is fully visible to at least one chunk.
SEGMENT_CHUNK_OVERLAP = 4_000
# Bounded fan-out for chunked-segmentation calls within a single document.
SEGMENT_CHUNK_CONCURRENCY = 4
# Bounded fan-out for split processing inside ``run_batch``.
SPLIT_CONCURRENCY = 4
# Bounded fan-out for per-patient eligibility extraction (Anthropic-side cap).
PATIENT_EXTRACT_CONCURRENCY = 5

# Lazy-init so the semaphore binds to whatever event loop is running.
_extract_sem: Optional[asyncio.Semaphore] = None


def _patient_extract_semaphore() -> asyncio.Semaphore:
    """Module-level semaphore that bounds concurrent per-patient eligibility
    extraction. Lazy-initialised so it binds to the running loop on first use.
    """
    global _extract_sem
    if _extract_sem is None:
        _extract_sem = asyncio.Semaphore(PATIENT_EXTRACT_CONCURRENCY)
    return _extract_sem


def _chunk_for_segmentation(text: str) -> List[str]:
    """Break ``text`` into overlapping chunks for the segments LLM call.

    Returns a 1-element list when text fits in a single call.
    """
    if len(text) <= SEGMENT_CHUNK_CHARS:
        return [text]
    chunks: List[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + SEGMENT_CHUNK_CHARS, n)
        chunks.append(text[start:end])
        if end >= n:
            break
        start = end - SEGMENT_CHUNK_OVERLAP
    return chunks


def _dedupe_segments(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Dedupe patients across overlapping chunks.

    Primary key: MBI (case-insensitive, whitespace-stripped). Fallback key when
    MBI is absent: ``(lastName, firstName, dob)``. When two records collide,
    prefer the one with HIGH confidence > MEDIUM > LOW; on a tie, prefer the
    one with a non-null ``sectionAnchor``.
    """
    by_key: Dict[str, Dict[str, Any]] = {}
    conf_rank = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
    for seg in segments:
        mbi = (seg.get("mbi") or "").strip().upper()
        if mbi:
            key = f"mbi:{mbi}"
        else:
            ln = (seg.get("lastName") or "").strip().lower()
            fn = (seg.get("firstName") or "").strip().lower()
            dob = (seg.get("dob") or "").strip()
            if not (ln or fn or dob):
                continue  # nothing to identify; drop
            key = f"name:{ln}|{fn}|{dob}"
        prev = by_key.get(key)
        if prev is None:
            by_key[key] = seg
            continue
        prev_score = (
            conf_rank.get((prev.get("confidence") or "").upper(), 0),
            1 if prev.get("sectionAnchor") else 0,
        )
        new_score = (
            conf_rank.get((seg.get("confidence") or "").upper(), 0),
            1 if seg.get("sectionAnchor") else 0,
        )
        if new_score > prev_score:
            by_key[key] = seg
    return list(by_key.values())


async def _segment_document(llm_text: str) -> List[Dict[str, Any]]:
    """Run patient segmentation across (possibly multiple) chunks of
    ``llm_text`` and return a deduped list of patient segments.

    Replaces the v0.2 behaviour that silently truncated to 24,000 chars —
    long multi-patient documents are now chunked with overlap so every
    patient section reaches the LLM.
    """
    chunks = _chunk_for_segmentation(llm_text)
    if len(chunks) == 1:
        try:
            result = await extract_patient_segments(chunks[0])
        except Exception:
            raise
        return (result.get("extracted") or {}).get("patients") or []

    sem = asyncio.Semaphore(SEGMENT_CHUNK_CONCURRENCY)

    async def _one(chunk: str) -> List[Dict[str, Any]]:
        async with sem:
            try:
                r = await extract_patient_segments(chunk)
                return (r.get("extracted") or {}).get("patients") or []
            except Exception as e:  # noqa: BLE001
                log.warning("Segment chunk failed (skipping): %s", e)
                return []

    chunk_results = await asyncio.gather(*[_one(c) for c in chunks])
    flat = [seg for sublist in chunk_results for seg in sublist]
    return _dedupe_segments(flat)


def _split_batch_payload(payloads: List[Tuple[str, bytes]]) -> List[Tuple[str, str, bytes]]:
    """Flatten zips + split multi-subscriber X12 envelopes.

    Returns [(filename, format, bytes), ...]. CSVs are *not* split here — they're
    split at identity-extract time so we keep headers with each row group.

    For X12: when an envelope contains multiple ST*27x transactions (rare),
    each transaction is broken out into its own pseudo-envelope by re-using the
    enclosing ISA header. The parser is lenient and tolerates a missing GS/GE
    pair inside the fragment.
    """
    out: List[Tuple[str, str, bytes]] = []
    for filename, content in payloads:
        lower = filename.lower()
        if lower.endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(content)) as zf:
                    for name in zf.namelist():
                        if name.endswith("/") or not zf.getinfo(name).file_size:
                            continue
                        with zf.open(name) as fh:
                            inner = fh.read()
                            fmt = format_detect.detect_format(name, inner[:4096])
                            out.append((name, fmt, inner))
            except zipfile.BadZipFile:
                out.append((filename, "OTHER", content))
            continue
        fmt = format_detect.detect_format(filename, content[:4096])
        if fmt == "X12_271":
            text = content.decode("utf-8", errors="replace")
            # ISA segment ends at position 106 in standard X12; capture as-is.
            # Then split on ST*27x lookahead — each fragment already starts
            # with "ST*27..." (DO NOT re-prepend "ST*27" — that was the bug).
            parts = X12_STX_RE.split(text)
            tx_fragments = [p for p in parts[1:] if p.startswith("ST*27")]
            isa_end = text.find("GS*")
            isa_hdr = text[:isa_end] if isa_end > 0 else text[:106]
            if len(tx_fragments) > 1 and isa_hdr:
                for idx, part in enumerate(tx_fragments, 1):
                    combined = (isa_hdr + part).encode("utf-8")
                    out.append((f"{filename}#st{idx}", "X12_271", combined))
                continue
        out.append((filename, fmt, content))
    return out


async def _process_batch_split(
    split: Tuple[str, str, bytes],
    hs_id: Optional[str],
    actor: str,
    app: Any,
    batch_rec: Dict[str, Any],
) -> None:
    """Process one split: extract patient segment(s) → create patient(s) → queue check(s).

    A single input "split" (one PDF, one CSV, one X12 transaction, etc.) may
    contain ONE or MANY distinct patients. The eligibility-segments LLM call
    detects how many patients are present and returns one entry per patient
    along with a verbatim ``sectionAnchor`` we can use to slice the document
    text. Single-patient splits return a 1-element list and behave like the
    legacy ``extract_identity`` path.
    """
    filename, fmt, content = split
    store_dict = app.state.patient_store

    doc_id = uuid.uuid4().hex
    from routers.eligibility import UPLOAD_DIR

    tmp_dir = UPLOAD_DIR / "batch-staging"
    tmp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    ext = Path(filename).suffix.lower() or ".bin"
    dest = tmp_dir / f"{doc_id}{ext}"
    dest.write_bytes(content)
    try:
        dest.chmod(0o600)
    except OSError:
        pass

    parsed = _parse_one(
        {
            "path": str(dest),
            "filename": filename,
            "format": fmt,
            "size_bytes": len(content),
        }
    )
    llm_text = parsed.get("llm_text") or f"(file {filename}, no parseable content)"

    # CSV multi-MBI is split deterministically by MBI column — each group is
    # already a single patient before we hit the LLM, so dispatch each group
    # through the segments call individually.
    if fmt == "CSV":
        csv_res = parse_csv(content)
        groups = split_by_mbi(csv_res)
        if len(groups) > 1:
            for idx, rows in enumerate(groups, 1):
                group_text = "\n".join(" | ".join(f"{k}={v}" for k, v in r.items() if v) for r in rows)
                header = f"=== CSV GROUP {idx} (rows={len(rows)}) file={filename} ==="
                group_bytes = b"\n".join(
                    s.encode()
                    for s in [",".join(csv_res.headers)]
                    + [",".join(r.get(h, "") for h in csv_res.headers) for r in rows]
                )
                await _segments_extract_and_fanout(
                    filename=f"{filename}#row{idx}",
                    fmt="CSV",
                    content=group_bytes,
                    llm_text=f"{header}\n{group_text}",
                    hs_id=hs_id,
                    actor=actor,
                    app=app,
                    batch_rec=batch_rec,
                    original_doc_id=None,
                    original_path=None,
                )
            dest.unlink(missing_ok=True)
            return

    await _segments_extract_and_fanout(
        filename=filename,
        fmt=fmt,
        content=content,
        llm_text=llm_text,
        hs_id=hs_id,
        actor=actor,
        app=app,
        batch_rec=batch_rec,
        original_doc_id=doc_id,
        original_path=str(dest),
    )


def _identity_from_segment(seg: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the identity-only subset from a segment dict (drops anchors/notes)."""
    return {
        "firstName": seg.get("firstName"),
        "lastName": seg.get("lastName"),
        "dob": seg.get("dob"),
        "mbi": seg.get("mbi"),
        "surgeryDate": seg.get("surgeryDate"),
        "anchorProcedure": seg.get("anchorProcedure"),
        "confidence": seg.get("confidence") or "LOW",
    }


def _slice_by_anchors(text: str, segments: List[Dict[str, Any]]) -> List[Tuple[Dict[str, Any], str]]:
    """Slice ``text`` into per-segment substrings using each segment's
    ``sectionAnchor`` as the boundary marker.

    Returns ``[(segment, slice_text), ...]`` in document order. Segments whose
    anchor is missing or cannot be located fall back to receiving the WHOLE
    document text — better to over-include than to silently drop a patient.
    """
    located: List[Tuple[Dict[str, Any], int]] = []
    for seg in segments:
        anchor = (seg.get("sectionAnchor") or "").strip()
        pos = text.find(anchor) if anchor else -1
        located.append((seg, pos))

    found = [(seg, pos) for seg, pos in located if pos >= 0]
    found.sort(key=lambda sp: sp[1])

    if not found:
        # No anchors located. Best-effort: split the document into roughly equal
        # slices, one per segment, in document order. Better than handing every
        # patient the entire document — that inflates LLM cost N× and risks
        # cross-patient bleed (one patient's check seeing another's MA contract,
        # ESRD note, etc.).
        if not segments:
            return []
        span = max(1, len(text) // len(segments))
        return [
            (seg, text[i * span : min((i + 1) * span + SEGMENT_CHUNK_OVERLAP, len(text))])
            for i, seg in enumerate(segments)
        ]

    out: List[Tuple[Dict[str, Any], str]] = []
    for i, (seg, pos) in enumerate(found):
        end = found[i + 1][1] if i + 1 < len(found) else len(text)
        out.append((seg, text[pos:end]))

    located_set = {id(seg) for seg, _ in found}
    for seg, pos in located:
        if id(seg) not in located_set:
            out.append((seg, text))
    return out


async def _segments_extract_and_fanout(
    *,
    filename: str,
    fmt: str,
    content: bytes,
    llm_text: str,
    hs_id: Optional[str],
    actor: str,
    app: Any,
    batch_rec: Dict[str, Any],
    original_doc_id: Optional[str] = None,
    original_path: Optional[str] = None,
) -> None:
    """LLM-segment a document and dispatch one ``_register_one_segment_and_enqueue``
    call per detected patient.

    Single-patient files take a fast path that reuses the original raw bytes
    (PDF/CSV/X12 stays intact on disk for audit). Multi-patient files write a
    per-patient ``.txt`` slice and use that as the eligibility-check document
    so the extractor receives only the relevant patient's text.
    """
    try:
        segments: List[Dict[str, Any]] = await _segment_document(llm_text)
    except Exception as e:  # noqa: BLE001
        log.exception("Segment extraction failed for %s", filename)
        batch_rec["errors"].append({"filename": filename, "error": f"Identity extraction failed: {e}"})
        await _emit(batch_rec, "file_error", {"filename": filename, "error": str(e)})
        if original_path:
            try:
                Path(original_path).unlink(missing_ok=True)
            except OSError:
                pass
        return

    if not segments:
        batch_rec["errors"].append({"filename": filename, "error": "No patients detected in document"})
        await _emit(batch_rec, "file_error", {"filename": filename, "error": "No patients detected"})
        if original_path:
            try:
                Path(original_path).unlink(missing_ok=True)
            except OSError:
                pass
        return

    if len(segments) == 1:
        seg = segments[0]
        await _register_one_segment_and_enqueue(
            filename=filename,
            fmt=fmt,
            content=content,
            slice_text=llm_text,
            identity=_identity_from_segment(seg),
            pre_op_instructions=seg.get("preOpInstructions"),
            hs_id=hs_id,
            actor=actor,
            app=app,
            batch_rec=batch_rec,
            original_doc_id=original_doc_id,
            original_path=original_path,
        )
        return

    slices = _slice_by_anchors(llm_text, segments)
    for idx, (seg, slice_text) in enumerate(slices, 1):
        slice_bytes = slice_text.encode("utf-8")
        await _register_one_segment_and_enqueue(
            filename=f"{filename}#patient{idx}",
            fmt="OTHER",
            content=slice_bytes,
            slice_text=slice_text,
            identity=_identity_from_segment(seg),
            pre_op_instructions=seg.get("preOpInstructions"),
            hs_id=hs_id,
            actor=actor,
            app=app,
            batch_rec=batch_rec,
            original_doc_id=None,
            original_path=None,
        )

    if original_path:
        try:
            Path(original_path).unlink(missing_ok=True)
        except OSError:
            pass


async def _register_one_segment_and_enqueue(
    *,
    filename: str,
    fmt: str,
    content: bytes,
    slice_text: str,
    identity: Dict[str, Any],
    pre_op_instructions: Optional[str],
    hs_id: Optional[str],
    actor: str,
    app: Any,
    batch_rec: Dict[str, Any],
    original_doc_id: Optional[str] = None,
    original_path: Optional[str] = None,
) -> None:
    """Create/merge a patient record from a pre-extracted identity, persist
    its eligibility doc, populate prep notes, and enqueue an eligibility check.
    """
    store_dict = app.state.patient_store
    ident = identity or {}

    confidence = (ident.get("confidence") or "LOW").upper()
    if confidence == "LOW":
        batch_rec["needs_review"].append(
            {
                "filename": filename,
                "identity": ident,
                "llm_text_preview": slice_text[:1000],
            }
        )
        await _emit(batch_rec, "needs_review", {"filename": filename, "identity": ident})
        return

    pid, merged = _create_or_merge_patient(store_dict, ident, hs_id)

    if pre_op_instructions:
        sd = store_dict[pid].setdefault("structured_data", {})
        sd["pre_op_instructions"] = pre_op_instructions

    from routers.eligibility import UPLOAD_DIR
    from pathlib import Path as _P

    patient_dir = UPLOAD_DIR / pid
    patient_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    doc_id = original_doc_id or uuid.uuid4().hex
    suffix = _P(filename).suffix.lower() or (".txt" if fmt == "OTHER" else ".bin")
    dest = patient_dir / f"{doc_id}{suffix}"
    try:
        if original_path and _P(original_path).exists():
            _P(original_path).replace(dest)
        else:
            dest.write_bytes(content)
        try:
            dest.chmod(0o600)
        except OSError:
            pass
    except Exception as e:  # noqa: BLE001
        batch_rec["errors"].append({"filename": filename, "error": f"Failed to stage file: {e}"})
        await _emit(batch_rec, "file_error", {"filename": filename, "error": str(e)})
        return

    import hashlib as _h

    rec_doc = {
        "id": doc_id,
        "patient_id": pid,
        "filename": filename,
        "format": fmt,
        "size_bytes": len(content),
        "sha256": _h.sha256(content).hexdigest(),
        "path": str(dest),
        "status": "validated",
        "uploaded_at": _utc_iso(),
    }
    store.save_doc(doc_id, rec_doc)
    store_dict[pid].setdefault("relevant_files", []).append(doc_id)

    check_id = uuid.uuid4().hex
    surgery_date = (
        ident.get("surgeryDate")
        or (store_dict[pid].get("structured_data") or {}).get("procedure_date")
        or ""
    )
    if not surgery_date:
        batch_rec["needs_review"].append(
            {
                "filename": filename,
                "patientId": pid,
                "reason": "Missing surgery date",
                "identity": ident,
            }
        )
        await _emit(
            batch_rec,
            "needs_review",
            {"patientId": pid, "filename": filename, "reason": "missing_surgery_date"},
        )
    else:
        queue = store.new_check_queue()
        check_rec: Dict[str, Any] = {
            "id": check_id,
            "patient_id": pid,
            "document_ids": [doc_id],
            "freeform_notes": "",
            "surgery_date": surgery_date,
            "status": "PARSING",
            "stage": "PARSING",
            "created_at": _utc_iso(),
            "updated_at": _utc_iso(),
            "actor": actor,
            "verdicts": None,
            "overall_verdict": None,
            "extracted_fields": None,
            "overrides": {},
            "error": None,
            "queue": queue,
            "ring": store.ring_buffer(),
            "batch_id": batch_rec["id"],
        }
        store.save_check(check_id, check_rec)
        store_dict[pid]["eligibility_check_id"] = check_id
        store_dict[pid]["eligibility_status"] = "PENDING"

        asyncio.create_task(
            _run_patient_in_batch(check_id, store_dict[pid], [rec_doc], "", surgery_date, batch_rec)
        )

    batch_rec["created"].append(
        {
            "patientId": pid,
            "merged": merged,
            "filename": filename,
            "identity": ident,
            "check_id": check_id if surgery_date else None,
        }
    )
    await _emit(
        batch_rec,
        "patient_created",
        {
            "patientId": pid,
            "filename": filename,
            "identity": ident,
            "merged": merged,
            "check_id": check_id if surgery_date else None,
        },
    )


async def _run_patient_in_batch(check_id, patient, docs, notes, surgery_date, batch_rec):
    # Bound concurrent per-patient eligibility extraction so a 50-patient
    # batch does not fire 50 simultaneous Anthropic calls. The semaphore is
    # module-scoped (one per process) and bound to the running event loop.
    async with _patient_extract_semaphore():
        await run_pipeline(check_id, patient, docs, notes, surgery_date)
    rec = store.get_check(check_id) or {}
    await _emit(
        batch_rec,
        "patient_done",
        {
            "patientId": rec.get("patient_id"),
            "check_id": check_id,
            "overallVerdict": rec.get("overall_verdict"),
            "status": rec.get("status"),
        },
    )


def _create_or_merge_patient(store_dict: Dict[str, Any], identity: Dict[str, Any], hs_id: Optional[str]) -> Tuple[str, bool]:
    """Create a draft patient. If an existing patient has the same MBI, merge.

    Returns (patient_id, merged).
    """
    mbi = (identity or {}).get("mbi") or ""
    if mbi:
        for pid, d in store_dict.items():
            sd = d.get("structured_data") or {}
            if str(sd.get("mbi") or d.get("mbi") or "").upper().strip() == mbi.upper().strip():
                return pid, True

    first = (identity.get("firstName") or "").strip()
    last = (identity.get("lastName") or "").strip()
    name = " ".join(p for p in [first, last] if p) or "Unknown Patient"
    pid = uuid.uuid4().hex
    surgery_date = identity.get("surgeryDate") or ""
    store_dict[pid] = {
        "name": name,
        "health_system_id": hs_id,
        "phone": "",
        "email": "",
        "pipeline_type": "pre_op",  # PRD §5.3: always pre-op for group uploads
        "voice_audio_url": None,
        "battlecard_html": None,
        "avatar_url": None,
        "voice_script": None,
        "structured_data": {
            "patient_name": name,
            "procedure_name": identity.get("anchorProcedure") or "",
            "procedure_date": surgery_date,
            "status": "scheduled",
            "dob": identity.get("dob"),
            "mbi": mbi,
        },
        "clinic_code": "",
        "resource_code": "",
        "office_phone": "",
        "resources": None,
        "pcp_referral_sent": False,
        "pcp_name": "",
        "eligibility_status": "PENDING",
        "eligibility_check_id": None,
        "relevant_files": [],
        "mbi": mbi,
    }
    return pid, False


async def run_batch(
    batch_id: str,
    payloads: List[Tuple[str, bytes]],
    hs_id: Optional[str],
    actor: str,
    app: Any,
) -> None:
    rec = store.get_batch(batch_id)
    if not rec:
        log.error("run_batch: %s not found", batch_id)
        return

    try:
        await _emit(rec, "status", {"stage": "SPLITTING", "files": len(payloads)})
        splits = _split_batch_payload(payloads)
        await _emit(rec, "status", {"stage": "EXTRACTING_IDENTITIES", "splits": len(splits)})

        # Fan out identity extraction with bounded concurrency. The semaphore
        # caps the number of in-flight segment-extraction LLM calls per batch
        # so we don't trip Anthropic's rate limit on a 50-split upload.
        # NOTE: per-patient eligibility extraction is dispatched via
        # ``asyncio.create_task`` inside ``_register_one_segment_and_enqueue``
        # and continues running after this gather completes — those are
        # bounded by ``_patient_extract_semaphore()`` and stream their own
        # ``patient_done`` SSE events as they finish.
        split_sem = asyncio.Semaphore(SPLIT_CONCURRENCY)

        async def _bounded_split(split):
            async with split_sem:
                await _process_batch_split(split, hs_id, actor, app, rec)

        await asyncio.gather(*[_bounded_split(s) for s in splits])

        rec["status"] = "DONE"
        rec["finished_at"] = _utc_iso()
        await _emit(
            rec,
            "done",
            {
                "created": len(rec["created"]),
                "needs_review": len(rec["needs_review"]),
                "errors": len(rec["errors"]),
            },
        )
    except Exception as e:  # noqa: BLE001
        log.exception("Batch pipeline failure for %s", batch_id)
        rec["status"] = "ERROR"
        rec["error"] = str(e)
        await _emit(rec, "error", {"message": str(e)})
