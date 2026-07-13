"""Auto case generation pipeline — unpack → classify → parse → assemble →
normalize timeline → verify de-id → deidentify → ClinicalCase(real_deid)
(Data Provider Portal PRD §5 matrix, §7 core).

This is the orchestrator the provider-upload and admin-retry endpoints call. It
turns a raw provider bundle (any mix of files, or a ``.zip``) into stored,
preview-ready ``real_deid`` cases — or routes what it can't safely ingest to
quarantine with **masked** findings. Nothing partial ships: a file that fails
typing, parsing, or de-id verification becomes a quarantine row, never a silent
drop and never a half-ingested case.

Security posture (PRD §2 B2, §5):
  * A broad ALLOWLIST of clinical formats; executables/scripts are hard-blocked
    by extension AND magic bytes.
  * ``.zip`` is unpacked defensively — caps on entry count, uncompressed size,
    and nesting; path-traversal (``../`` / absolute) entries are rejected; an
    archive-inside-an-archive is refused (no zip bombs, no traversal).
  * Imaging (DICOM) is accepted but EXCLUDED from the case (never gradable) — it
    does not fail the rest of the bundle.
  * Raw files are sealed at rest via ``field_crypto`` when a key is configured and
    auto-purged after ``ASCLEPIUS_RAW_RETENTION_DAYS`` — we keep the derived case,
    not the raw PHI-adjacent file.

Order matters (the B1 fix): timeline normalization runs BEFORE de-id verification
and the final ``deidentify()`` guard, so a real, date-shifted export can actually
clear the date-rejecting guard.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from asclepius import deid_verify
from asclepius.cases import ClinicalCase, public_case
from asclepius.case_formats import (
    FORMATS,
    CaseIngestError,
    ImagingRejected,
    deidentify,
)
from asclepius.constants import raw_retention_days
from asclepius.timeline import normalize_case_timeline

log = logging.getLogger("asclepius.ingestion")


# ─── Caps (env-tunable) ───────────────────────────────────────────────────────
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def max_files() -> int:
    return _env_int("ASCLEPIUS_UPLOAD_MAX_FILES", 200)


def max_file_bytes() -> int:
    return _env_int("ASCLEPIUS_UPLOAD_MAX_FILE_BYTES", 50 * 1024 * 1024)  # 50 MB


def max_total_bytes() -> int:
    return _env_int("ASCLEPIUS_UPLOAD_MAX_TOTAL_BYTES", 200 * 1024 * 1024)  # 200 MB


def max_zip_entries() -> int:
    return _env_int("ASCLEPIUS_UPLOAD_MAX_ZIP_ENTRIES", 2000)


def max_uncompressed_bytes() -> int:
    return _env_int("ASCLEPIUS_UPLOAD_MAX_UNCOMPRESSED_BYTES", 500 * 1024 * 1024)  # 500 MB


# ─── Classification (PRD §5 file-type matrix) ─────────────────────────────────
# Hard-blocked: executables / scripts / installers. Blocked by extension AND by
# magic bytes below — an attacker-renamed binary can't slip through on extension.
_BLOCKED_EXT = {
    ".exe", ".dll", ".so", ".dylib", ".bin", ".bat", ".cmd", ".com", ".msi",
    ".scr", ".sh", ".bash", ".zsh", ".ps1", ".psm1", ".vbs", ".vbe", ".js",
    ".jse", ".jar", ".py", ".pyc", ".rb", ".pl", ".php", ".lua", ".app",
    ".deb", ".rpm", ".apk", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar",
}
_NOTE_EXT = {".txt", ".md", ".rtf", ".text"}
_CSV_EXT = {".csv", ".tsv"}
_XLSX_EXT = {".xlsx", ".xls"}  # accepted type, but no stdlib parser → quarantine
_DICOM_EXT = {".dcm", ".dicom"}


def _ext(filename: str) -> str:
    return os.path.splitext(filename or "")[1].lower()


def _looks_executable(content: bytes) -> bool:
    head = content[:4]
    return (
        head[:2] == b"MZ"            # PE / DOS
        or head[:4] == b"\x7fELF"    # ELF
        or head[:2] == b"#!"         # shebang script
        or head[:4] == b"\xca\xfe\xba\xbe"  # Mach-O / Java class
        or head[:4] == b"\xfe\xed\xfa\xce"  # Mach-O
    )


def classify(filename: str, content: bytes) -> Tuple[str, Optional[str], str]:
    """Classify one file → ``(kind, adapter_format, reason)``.

    ``kind`` ∈ {lab_csv, fhir_r4, hl7v2, ccda, note_text, dicom, zip, blocked,
    unknown}. ``adapter_format`` is the ``case_formats.FORMATS`` key when the file
    is parseable, else ``None``. Classification is by CONTENT first (magic bytes /
    sniff), extension second — so a renamed binary is still blocked and a
    correctly-shaped export is still typed even with a wrong extension."""
    ext = _ext(filename)
    head = content[:512] if content else b""

    # 1. Hard blocks — magic bytes win over a friendly extension.
    if _looks_executable(content) or ext in _BLOCKED_EXT:
        return ("blocked", None, "executable or script — blocked")

    # 2. Archives.
    if content[:4] == b"PK\x03\x04" or ext == ".zip":
        return ("zip", None, "archive")

    # 3. Imaging — accepted but never gradable (excluded, not failed).
    if ext in _DICOM_EXT or (len(content) > 132 and content[128:132] == b"DICM"):
        return ("dicom", "dicom", "imaging — excluded from case")

    # 4. Structured clinical formats by content sniff.
    text_head = head.decode("utf-8", errors="replace").lstrip()
    if text_head.startswith("{") or text_head.startswith("["):
        # JSON — FHIR if it smells like a Bundle/resource.
        if '"resourceType"' in text_head or ext == ".json":
            return ("fhir_r4", "fhir_r4", "FHIR R4 JSON")
    if text_head.startswith("MSH|") or ext == ".hl7":
        return ("hl7v2", "hl7v2", "HL7 v2")
    if text_head.startswith("<?xml") or "<ClinicalDocument" in text_head or ext in (".xml", ".cda", ".ccda"):
        return ("ccda", "ccda", "C-CDA / clinical XML")
    if ext in _CSV_EXT or ("," in text_head and "\n" in text_head and ext not in _NOTE_EXT):
        return ("lab_csv", "lab_csv", "CSV/TSV labs")
    if ext in _NOTE_EXT:
        return ("note_text", "note_text", "clinical note")
    if ext in _XLSX_EXT:
        return ("unknown", None, "spreadsheet (.xlsx) — needs manual mapping")

    # 5. Fallback: if it is decodable text, treat as a note; else quarantine.
    try:
        content.decode("utf-8")
        return ("note_text", "note_text", "unlabeled text — treated as a note")
    except UnicodeDecodeError:
        return ("unknown", None, "unrecognized binary — needs manual mapping")


# ─── Safe unzip (PRD §5, §2 B2) ───────────────────────────────────────────────
def safe_extract(content: bytes) -> List[Tuple[str, bytes]]:
    """Unpack a zip with hard caps and traversal defense. Returns ``[(name, bytes)]``
    of the leaf files. Raises :class:`CaseIngestError` on a bomb / traversal /
    nested archive — never ``extractall()``."""
    out: List[Tuple[str, bytes]] = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise CaseIngestError(f"corrupt zip: {exc}") from exc

    infos = zf.infolist()
    if len(infos) > max_zip_entries():
        raise CaseIngestError(f"zip has too many entries ({len(infos)} > {max_zip_entries()})")

    total = 0
    for info in infos:
        name = info.filename
        if info.is_dir():
            continue
        # Path traversal / absolute path defense.
        norm = os.path.normpath(name)
        if norm.startswith("..") or os.path.isabs(norm) or ".." in norm.split(os.sep):
            raise CaseIngestError(f"zip entry escapes the archive root: {name!r}")
        # No archive-in-archive (a common bomb + confusion vector).
        if _ext(name) in {".zip"} or _ext(name) in _BLOCKED_EXT:
            raise CaseIngestError(f"disallowed nested/blocked entry in zip: {name!r}")
        total += info.file_size
        if total > max_uncompressed_bytes():
            raise CaseIngestError("zip uncompressed size exceeds the cap (possible zip bomb)")
        with zf.open(info, "r") as fh:
            data = fh.read(max_file_bytes() + 1)
            if len(data) > max_file_bytes():
                raise CaseIngestError(f"zip entry {name!r} exceeds the per-file cap")
        out.append((os.path.basename(name), data))
    return out


# ─── Encrypted raw store + retention (PRD §5, §9) ─────────────────────────────
def _raw_dir() -> str:
    d = (os.getenv("ASCLEPIUS_RAW_UPLOAD_DIR") or "/tmp/asclepius-uploads").strip()
    os.makedirs(d, exist_ok=True)
    return d


def store_raw(upload_id: str, filename: str, content: bytes) -> str:
    """Persist a raw provider file to the quarantine store, sealed at rest when
    ``field_crypto`` is configured. Returns the on-disk path. Files here are
    auto-purged after the retention window — we keep the derived case, not the
    raw PHI-adjacent bytes."""
    import field_crypto

    up_dir = os.path.join(_raw_dir(), upload_id)
    os.makedirs(up_dir, exist_ok=True)
    safe_name = os.path.basename(filename) or "file"
    blob = content
    enc = False
    try:
        if field_crypto.is_configured():
            sealed = field_crypto.encrypt_bytes(content)
            if sealed is not None:
                blob, enc = sealed, True
    except Exception:
        log.warning("raw upload encryption failed; storing with restricted perms", exc_info=True)
    # Avoid silently overwriting a same-basename sibling in the same bundle
    # (e.g. two "labs.csv" from different zip folders) — that would lose a file on
    # retry. Disambiguate with a counter before the extension.
    base = safe_name + (".enc" if enc else "")
    path = os.path.join(up_dir, base)
    if os.path.exists(path):
        stem, ext = os.path.splitext(safe_name)
        n = 1
        while os.path.exists(path):
            path = os.path.join(up_dir, f"{stem}__{n}{ext}" + (".enc" if enc else ""))
            n += 1
    with open(path, "wb") as fh:
        fh.write(blob)
    try:
        os.chmod(path, 0o600)  # never world-readable (PRD §12)
    except OSError:
        pass
    return path


def purge_expired(store: Any) -> int:
    """Delete raw upload files older than the retention window and mark their
    upload rows purged. The derived case survives; the raw bytes do not."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=raw_retention_days())
    purged = 0
    for up in store.list_uploads():
        if up.get("purged"):
            continue
        try:
            received = datetime.fromisoformat(str(up.get("received_at")).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if received.tzinfo is None:
            received = received.replace(tzinfo=timezone.utc)
        if received >= cutoff:
            continue
        up_dir = os.path.join(_raw_dir(), up["upload_id"])
        if os.path.isdir(up_dir):
            for fn in os.listdir(up_dir):
                try:
                    os.remove(os.path.join(up_dir, fn))
                except OSError:
                    pass
            try:
                os.rmdir(up_dir)
            except OSError:
                pass
        store.update_upload(up["upload_id"], purged=True)
        purged += 1
    return purged


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


# ─── Assembly (PRD §7.3) ──────────────────────────────────────────────────────
_SECTIONS = ("problem_list", "medications", "lab_panels", "notes")


def _resolve_key(frag: Dict[str, Any], *, distinct_keys: List[str], default_key: str) -> str:
    """Which patient group a fragment belongs to. When a bundle identifies exactly
    ONE patient (the common single-patient export), key-less fragments (a lab CSV
    or a note with no patient id) fold into that patient rather than splitting off
    a spurious second case — so a FHIR export + a lab CSV + two notes become ONE
    ClinicalCase (PRD §7.3 / §10 acceptance). With multiple identified patients we
    can't safely guess, so key-less fragments go to their own default group."""
    k = frag.get("patient_key")
    if k:
        return str(k)
    if len(distinct_keys) == 1:
        return distinct_keys[0]
    return default_key


def _assemble(fragments: List[Dict[str, Any]], *, default_key: str) -> Dict[str, List[Dict[str, Any]]]:
    """Group parsed fragments into one case per ``patient_key`` (manifest →
    fragment patient_key → a single default group), routing each modality to its
    section: labs→lab_panels, notes→notes, meds→medications, conditions→
    problem_list, vitals→vitals, demographics→first non-empty."""
    distinct_keys = sorted({str(f["patient_key"]) for f in fragments if f.get("patient_key")})
    groups: Dict[str, Dict[str, Any]] = {}
    for frag in fragments:
        key = _resolve_key(frag, distinct_keys=distinct_keys, default_key=default_key)
        g = groups.setdefault(key, {
            "specialty": None, "demographics": {}, "problem_list": [],
            "medications": [], "vitals": {}, "lab_panels": [], "notes": [],
        })
        for sect in _SECTIONS:
            g[sect].extend(frag.get(sect) or [])
        g["vitals"].update(frag.get("vitals") or {})
        if not g["demographics"] and frag.get("demographics"):
            g["demographics"] = dict(frag["demographics"])
        spec = (frag.get("specialty") or "").strip()
        if spec and spec != "general" and not g["specialty"]:
            g["specialty"] = spec
    return groups


# ─── The orchestrator ─────────────────────────────────────────────────────────
def process_upload(
    store: Any,
    upload_id: str,
    files: List[Dict[str, Any]],
    *,
    specialty: str = "general",
    index_event: Optional[Any] = None,
    manifest: Optional[Dict[str, Any]] = None,
    actor: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the full pipeline for one upload bundle. ``files`` is a list of
    ``{"filename": str, "content": bytes}``. Persists ``ingest_files`` rows,
    assembled ``ingest_cases`` previews, and ``ingest_quarantine`` rows via the
    store, and returns a per-file + per-case + quarantine summary. Never raises on
    a single bad file — that file is quarantined and the rest of the bundle
    continues (PRD §10: a DICOM is excluded but the rest still ingests)."""
    manifest = manifest or {}
    index_event = index_event if index_event is not None else manifest.get("index_event")
    specialty = (manifest.get("specialty") or specialty or "general").strip() or "general"

    # 1. Expand archives into leaf files (safe unzip).
    leaves: List[Tuple[str, bytes]] = []
    for f in files:
        name, content = f.get("filename") or "file", f.get("content") or b""
        kind, _fmt, _reason = classify(name, content)
        if kind == "zip":
            try:
                leaves.extend(safe_extract(content))
            except CaseIngestError as exc:
                fid = store.add_ingest_file(
                    upload_id=upload_id, filename=name, detected_type="zip",
                    size_bytes=len(content), sha256=sha256(content),
                    status="rejected", outcome=str(exc),
                )
                store.add_quarantine(upload_id=upload_id, file_id=fid["file_id"],
                                     kind="parse_error", detail=str(exc))
        else:
            leaves.append((name, content))

    # 1b. Pull an in-bundle manifest.json (top-level or inside the zip) — it makes
    # ingestion far more reliable (patient_key / index_event / specialty). A bad
    # manifest is ignored, never fatal.
    kept: List[Tuple[str, bytes]] = []
    for name, content in leaves:
        if os.path.basename(name).lower() == "manifest.json":
            try:
                import json as _json
                m = _json.loads(content.decode("utf-8", errors="replace"))
                if isinstance(m, dict):
                    manifest = {**m, **{k: v for k, v in manifest.items() if v is not None}}
                    index_event = index_event if index_event is not None else manifest.get("index_event")
                    specialty = (manifest.get("specialty") or specialty or "general").strip() or "general"
            except Exception:
                log.info("ignored unparseable manifest.json in upload %s", upload_id)
            continue
        kept.append((name, content))
    leaves = kept

    # 2. Classify + parse each leaf → fragments (or quarantine).
    fragments: List[Dict[str, Any]] = []
    file_results: List[Dict[str, Any]] = []
    n_imaging = 0
    for name, content in leaves:
        kind, fmt, reason = classify(name, content)
        digest = sha256(content)
        if kind == "blocked":
            row = store.add_ingest_file(upload_id=upload_id, filename=name,
                                        detected_type="blocked", size_bytes=len(content),
                                        sha256=digest, status="rejected", outcome=reason)
            store.add_quarantine(upload_id=upload_id, file_id=row["file_id"],
                                 kind="blocked", detail=reason)
            file_results.append({"filename": name, "detected_type": "blocked",
                                 "status": "rejected", "outcome": reason})
            continue
        if kind == "dicom":
            n_imaging += 1
            store.add_ingest_file(upload_id=upload_id, filename=name, detected_type="dicom",
                                  size_bytes=len(content), sha256=digest, status="excluded",
                                  outcome="imaging is never gradable — excluded from the case")
            file_results.append({"filename": name, "detected_type": "dicom",
                                 "status": "excluded",
                                 "outcome": "imaging excluded (the rest of the bundle still ingests)"})
            continue
        if kind == "unknown" or fmt is None:
            row = store.add_ingest_file(upload_id=upload_id, filename=name, detected_type="unknown",
                                        size_bytes=len(content), sha256=digest,
                                        status="needs_review", outcome=reason)
            store.add_quarantine(upload_id=upload_id, file_id=row["file_id"],
                                 kind="untyped", detail=reason)
            file_results.append({"filename": name, "detected_type": "unknown",
                                 "status": "needs_review", "outcome": reason})
            continue
        # Parseable clinical format.
        adapter = FORMATS.get(fmt)
        try:
            frag = adapter(content, specialty=specialty)
            frag.setdefault("specialty", specialty)
            fragments.append(frag)
            store.add_ingest_file(upload_id=upload_id, filename=name, detected_type=fmt,
                                  adapter=fmt, size_bytes=len(content), sha256=digest,
                                  status="parsed", outcome=f"parsed as {reason}")
            file_results.append({"filename": name, "detected_type": fmt,
                                 "status": "parsed", "outcome": f"parsed as {reason}"})
        except ImagingRejected as exc:
            n_imaging += 1
            store.add_ingest_file(upload_id=upload_id, filename=name, detected_type="dicom",
                                  size_bytes=len(content), sha256=digest, status="excluded",
                                  outcome=str(exc))
            file_results.append({"filename": name, "detected_type": "dicom",
                                 "status": "excluded", "outcome": "imaging excluded"})
        except CaseIngestError as exc:
            row = store.add_ingest_file(upload_id=upload_id, filename=name, detected_type=fmt,
                                        adapter=fmt, size_bytes=len(content), sha256=digest,
                                        status="needs_review", outcome=f"parse failed: {exc}")
            # Mask the reason: a parse error must not echo suspected identifiers.
            store.add_quarantine(upload_id=upload_id, file_id=row["file_id"],
                                 kind="parse_error", detail=f"{fmt} parse error")
            file_results.append({"filename": name, "detected_type": fmt,
                                 "status": "needs_review", "outcome": "could not be parsed"})

    # 3. Assemble → 4. timeline → 5. verify de-id → 6. deidentify → preview.
    # A manifest patient_key is the default group so a single-patient bundle with
    # no FHIR Patient.id still assembles into one case.
    groups = _assemble(fragments, default_key=str(manifest.get("patient_key") or upload_id))
    case_results: List[Dict[str, Any]] = []
    quarantined_cases = 0
    for patient_key, raw_case in groups.items():
        raw_case["case_source"] = "real_deid"
        # 4. Normalize the timeline BEFORE the de-id guard (the B1 fix).
        normalized, tl_report = normalize_case_timeline(raw_case, index_event=index_event)
        # 5. Verify de-id (the provider de-identifies; we verify).
        verdict = deid_verify.verify_case(normalized)
        if not verdict.get("passed"):
            store.add_quarantine(upload_id=upload_id, kind="deid_failed",
                                 masked_findings=verdict.get("findings") or [],
                                 detail=f"de-id verification failed ({verdict.get('backend')})")
            quarantined_cases += 1
            case_results.append({"patient_key": patient_key, "status": "quarantined",
                                 "findings": verdict.get("findings") or []})
            continue
        # 6. Final hard post-condition — passes now because §4 ran first.
        try:
            safe = deidentify(normalized)
        except CaseIngestError as exc:
            store.add_quarantine(upload_id=upload_id, kind="deid_failed",
                                 masked_findings=[], detail="final de-id guard rejected the case")
            quarantined_cases += 1
            case_results.append({"patient_key": patient_key, "status": "quarantined",
                                 "reason": str(exc)})
            continue
        case = ClinicalCase(**{**safe, "case_source": "real_deid",
                               "specialty": safe.get("specialty") or specialty})
        ic = store.add_ingest_case(
            upload_id=upload_id, patient_key=patient_key,
            case=public_case(case.model_dump()) or {},
            quality={"timeline": tl_report, "deid_backend": verdict.get("backend"),
                     "n_fields_scanned": verdict.get("n_fields_scanned")},
        )
        case_results.append({"ic_id": ic["ic_id"], "patient_key": patient_key,
                             "status": "preview",
                             "n_labs": len(case.lab_panels), "n_notes": len(case.notes)})

    # 7. Roll up the upload status.
    open_q = store.list_quarantine(status="open", upload_id=upload_id)
    previews = [c for c in case_results if c["status"] == "preview"]
    if open_q or quarantined_cases:
        status = "quarantined" if not previews else "needs_review"
        reason = "some items need de-id review" if previews else "held for de-id review"
    elif previews:
        status = "ingested"
        reason = None
    else:
        status = "failed"
        reason = "nothing ingestible in this bundle"
    store.update_upload(upload_id, status=status, reason=reason,
                        file_count=len(file_results), meta={"imaging_excluded": n_imaging})
    if actor is not None:
        store.log_event(entity_type="ingest_upload", entity_id=upload_id,
                        event_type="processed", actor=actor,
                        payload={"status": status, "files": len(file_results),
                                 "cases": len(previews), "quarantined": len(open_q)})
    return {"upload_id": upload_id, "status": status, "reason": reason,
            "files": file_results, "cases": case_results,
            "quarantine_open": len(open_q), "imaging_excluded": n_imaging}
