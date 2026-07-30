"""Bundle ingestion orchestration (EHR Ingestion PRD §3, §5, §8).

    unpack (zip-bomb-safe) → classify each entry → format adapters →
    assemble one case per patient → normalize timeline → verify de-id →
    deidentify() hard guard → ingest_cases row ('ingested' | 'quarantined')

Design rules enforced here:
  * NOTHING PARTIAL LANDS: a patient's case either fully validates or the whole
    case quarantines with a readable (masked) reason. A DICOM entry rejects that
    ENTRY and the rest of the bundle continues — unless imaging was the only
    content, which rejects the upload.
  * The raw partner zip lives ONLY as an AES-GCM-encrypted blob under the
    quarantine dir (0700), auto-purged after ``ASCLEPIUS_RAW_RETENTION_DAYS``.
  * Chain of custody: every step emits a ``store.log_event`` audit event.
  * Malware scanning is a pluggable hook (``ASCLEPIUS_MALWARE_SCAN_CMD`` — any
    command returning non-zero rejects the upload); the built-in baseline
    validates zip magic/structure and rejects executable entries. State that
    honestly in ops docs: a real AV engine is the hook's job.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import shlex
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic import ValidationError

from asclepius import case_formats as cf
from asclepius import deid_verify
from asclepius.cases import ClinicalCase
from asclepius.store import _utcnow_iso
from asclepius.timeline import TimelineError, normalize_timeline

log = logging.getLogger("asclepius.ingestion")

# ─── Limits (env-tunable) ─────────────────────────────────────────────────────
def max_zip_bytes() -> int:
    try:
        return int(os.getenv("ASCLEPIUS_INGEST_MAX_ZIP_BYTES", str(100 * 1024 * 1024)))
    except ValueError:
        return 100 * 1024 * 1024


def max_entries() -> int:
    try:
        return int(os.getenv("ASCLEPIUS_INGEST_MAX_ENTRIES", "500"))
    except ValueError:
        return 500


def max_uncompressed_bytes() -> int:
    try:
        return int(os.getenv("ASCLEPIUS_INGEST_MAX_UNCOMPRESSED", str(500 * 1024 * 1024)))
    except ValueError:
        return 500 * 1024 * 1024


def raw_retention_days() -> int:
    try:
        return max(1, int(os.getenv("ASCLEPIUS_RAW_RETENTION_DAYS", "30")))
    except ValueError:
        return 30


def _default_ingest_dir() -> Path:
    """Co-locate raw blobs with the persistent DB, so the two share durability.

    The admin download + retry paths read this encrypted blob days after upload,
    so it MUST survive redeploys/restarts. Defaulting to ``/tmp`` was the bug:
    on Railway/Render ``/tmp`` is ephemeral and wiped on every redeploy, while
    the DB (``ASCLEPIUS_DB_PATH`` → mounted volume) persists — leaving the upload
    row pointing at a blob that no longer exists, which the download endpoint
    reports as a spurious 410 "already purged". Placing the ingest dir next to
    the DB file means a raw blob is exactly as durable as its DB row. Mirrors
    ``AsclepiusStore``'s DB-path resolution so the two never diverge."""
    db_path = os.getenv("ASCLEPIUS_DB_PATH") or os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "asclepius.db")
    return Path(os.path.dirname(os.path.abspath(db_path))) / "asclepius-ingest"


def quarantine_root() -> Path:
    root = Path(os.getenv("ASCLEPIUS_INGEST_DIR") or _default_ingest_dir()).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


_EXECUTABLE_EXTS = (".exe", ".dll", ".so", ".sh", ".bat", ".cmd", ".ps1", ".msi",
                    ".jar", ".app", ".scr", ".com", ".vbs", ".js", ".py")


class BundleRejected(ValueError):
    """The whole upload is unusable (not a zip, zip bomb, malware-scan fail,
    imaging-only). Recorded on the upload row with the reason."""


# ─── Raw storage (encrypted at rest) ─────────────────────────────────────────
def store_raw(upload_id: str, data: bytes) -> str:
    """Write the raw partner zip as an AES-GCM blob (field_crypto; passthrough
    only when no DATA_ENCRYPTION_KEY is configured — dev). 0700 dir, 0600 file."""
    from field_crypto import encrypt_bytes
    path = quarantine_root() / f"{upload_id}.zip.enc"
    path.write_bytes(encrypt_bytes(data))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return str(path)


def load_raw(raw_path: str) -> bytes:
    from field_crypto import decrypt_bytes
    return decrypt_bytes(Path(raw_path).read_bytes())


def delete_raw(raw_path: Optional[str]) -> None:
    """Best-effort removal of a raw blob (cleanup when a one-time claim is lost
    after the bytes were already written). Never raises."""
    if not raw_path:
        return
    try:
        Path(raw_path).unlink()
    except OSError:
        pass


# Filesystems where a redeploy/restart wipes the data — never durable for the
# raw partner bundle (this is what caused the "download failed (410)" incident:
# blobs on /tmp vanished on redeploy while the DB row survived).
_EPHEMERAL_PREFIXES = ("/tmp", "/var/tmp", "/dev/shm", "/run")


def ingest_storage_durable() -> Tuple[bool, str]:
    """(ok, detail) — is the raw ingest dir safe to keep partner bundles in?

    Two signals: (1) the dir must not sit on an ephemeral, redeploy-wiped path;
    (2) it should live on the same volume (device) as the DB, so a blob is
    exactly as durable as its row. (1) is fail-closed-worthy; (2) is a warning
    (a deliberately separate durable mount is legitimate)."""
    root = quarantine_root()
    root_str = str(root)
    for pre in _EPHEMERAL_PREFIXES:
        if root_str == pre or root_str.startswith(pre + "/"):
            return False, (
                f"raw ingest dir {root_str} is on ephemeral storage ({pre}); "
                "a redeploy will delete partner uploads. Set ASCLEPIUS_INGEST_DIR "
                "to a path on your persistent volume (e.g. beside ASCLEPIUS_DB_PATH)."
            )
    db_path = os.getenv("ASCLEPIUS_DB_PATH") or os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "asclepius.db")
    db_dir = os.path.dirname(os.path.abspath(db_path)) or "/"
    try:
        if os.stat(root_str).st_dev != os.stat(db_dir).st_dev:
            return True, (
                f"raw ingest dir {root_str} is on a different volume than the DB "
                f"({db_dir}); confirm that volume is persistent, or move the ingest "
                "dir next to the DB so raw blobs share its durability."
            )
    except OSError:
        pass
    return True, f"raw ingest dir {root_str} is on the DB's volume"


def purge_expired_raw(store: Any) -> int:
    """Delete raw blobs older than the retention window (PRD §4: we keep the
    derived case, not the partner file). Called opportunistically on ingestion
    activity — no cron needed at pod scale. Returns files deleted."""
    cutoff = time.time() - raw_retention_days() * 86400
    deleted = 0
    for p in quarantine_root().glob("*.zip.enc"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                deleted += 1
        except OSError:
            continue
    if deleted:
        store.log_event(entity_type="ingest", event_type="raw_purged",
                        payload={"deleted": deleted, "retention_days": raw_retention_days()})
    return deleted


# Non-terminal upload states: the pipeline was mid-flight. A redeploy kills the
# in-process BackgroundTask, so without recovery these would sit stuck forever.
_NON_TERMINAL_UPLOAD_STATUSES = ["received", "scanning", "parsing"]


def recover_interrupted_uploads(store: Any) -> int:
    """Re-run the pipeline for uploads left mid-flight by a crash/redeploy.

    The raw blob is durable (persistent volume), so reprocessing is lossless.
    We clear each upload's un-promoted cases first, so a partially-processed
    upload reprocesses cleanly instead of double-inserting cases. An upload
    whose raw blob is genuinely gone is marked rejected (never left dangling).
    Returns the number of uploads re-enqueued/handled. Best-effort; never raises."""
    handled = 0
    try:
        stuck = store.list_uploads_in_status(_NON_TERMINAL_UPLOAD_STATUSES)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("ingest recovery: could not list interrupted uploads: %s", exc)
        return 0
    for upload in stuck:
        uid = upload.get("upload_id")
        raw_path = upload.get("raw_path")
        try:
            if not raw_path or not os.path.exists(raw_path):
                store.update_ingest_upload(
                    uid, status="rejected",
                    reason="raw upload was lost before processing completed "
                           "(interrupted by a restart); ask the partner to re-upload")
                store.log_event(entity_type="ingest_upload", entity_id=uid,
                                event_type="upload_recovery_failed",
                                payload={"reason": "raw blob missing"})
                try:
                    from asclepius import ingest_notify
                    ingest_notify.notify_upload_failed(
                        store, store.get_ingest_upload(uid), outcome="lost")
                except Exception:  # pragma: no cover - defensive
                    pass
                handled += 1
                continue
            removed = store.delete_unpromoted_ingest_cases(uid)
            store.log_event(entity_type="ingest_upload", entity_id=uid,
                            event_type="upload_recovery_requeued",
                            payload={"prior_status": upload.get("status"),
                                     "cleared_cases": removed})
            process_upload(store, uid)
            handled += 1
        except Exception as exc:  # pragma: no cover - defensive per-upload
            log.warning("ingest recovery: upload %s failed to reprocess: %s", uid, exc)
    if handled:
        log.info("ingest recovery: handled %d interrupted upload(s)", handled)
    return handled


# ─── Malware scan hook ────────────────────────────────────────────────────────
def malware_scan(path: str) -> Tuple[bool, str]:
    """(ok, detail). With ASCLEPIUS_MALWARE_SCAN_CMD set (e.g. ``clamscan
    --no-summary``), the command runs against the file and non-zero rejects.
    Without it, the baseline is structural zip validation only — honest floor,
    not an AV engine."""
    cmd = (os.getenv("ASCLEPIUS_MALWARE_SCAN_CMD") or "").strip()
    if not cmd:
        return True, "baseline (structural checks only; set ASCLEPIUS_MALWARE_SCAN_CMD for AV)"
    try:
        res = subprocess.run(shlex.split(cmd) + [path], capture_output=True, timeout=120)
        if res.returncode != 0:
            return False, f"malware scan flagged the upload (exit {res.returncode})"
        return True, "scanned clean"
    except Exception as exc:
        # Fail CLOSED: a configured scanner that cannot run means we cannot claim
        # the file is safe.
        return False, f"malware scanner unavailable ({exc}); upload rejected (fail-closed)"


# ─── Loose-file wrapping — single source of truth for BOTH upload doors ───────
# (Buyer Response PRD §2 A1). The magic-link door (routers/asclepius.py
# partner_upload) and the account door (routers/asclepius_provider.py
# provider_upload) used to disagree: the account door WRAPPED loose files into a
# zip, while the link door REJECTED anything whose first two bytes were not the
# ``PK`` zip magic. That meant the exact partner file we mailed a health-system
# succeeded or failed depending only on which URL we happened to send. This is the
# one packing implementation both doors call before ``store_raw``, so they cannot
# drift again.
def wrap_loose_files(files: List[Dict[str, Any]], *, specialty: Optional[str]) -> bytes:
    """Loose partner files -> one zip for the shared pipeline.

    A genuinely-zip single upload passes through untouched (keyed on the ``PK``
    magic bytes, NOT the extension, so a mis-named ``.zip`` that is really a CSV
    still gets wrapped instead of failing the unpacker). Everything else is
    packed, with a synthesized ``manifest.json`` carrying the specialty when the
    bundle does not already include one.

    Each file is ``{"filename": str, "content": bytes}``. Extracted from
    routers/asclepius_provider.py so the magic-link door and the account door
    cannot drift again.
    """
    if len(files) == 1 and (files[0].get("content") or b"")[:2] == b"PK":
        return files[0]["content"]
    has_manifest = any(
        os.path.basename((f.get("filename") or "")).lower() == "manifest.json"
        for f in files
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.writestr(os.path.basename(f.get("filename") or "") or "file",
                       f.get("content") or b"")
        if not has_manifest and specialty:
            z.writestr("manifest.json", json.dumps({"specialty": specialty}))
    return buf.getvalue()


# Partner-facing copy for an upload we genuinely cannot read (used by both doors).
# A hospital IT team must be told what to DO, not that the "zip magic bytes" were
# wrong — a message that means nothing to them.
UNREADABLE_UPLOAD_MESSAGE = (
    "We could not read this upload. Send a .zip, or individual .json / .csv / "
    ".hl7 / .txt files and we will package them. If the problem persists, contact "
    "your Archangel Health point of contact and we will take it by secure transfer."
)


# ─── Unpack + classify (PRD §5) ───────────────────────────────────────────────
def _classify(name: str, head: bytes, text_head: str) -> str:
    lower = name.lower()
    base = os.path.basename(lower)
    if head[:4] == b"DICM" or head[128:132] == b"DICM" or lower.endswith(".dcm"):
        return "dicom"
    if base == "manifest.json":
        return "manifest"
    if lower.endswith((".json",)):
        return "fhir_r4" if '"resourceType"' in text_head and '"Bundle"' in text_head else "unsupported"
    if lower.endswith((".hl7", ".oru")) or text_head.startswith("MSH|"):
        return "hl7v2"
    if lower.endswith((".csv", ".tsv")):
        return "lab_csv"
    if lower.endswith((".txt", ".md", ".note")):
        return "note_text"
    return "unsupported"


def unpack_bundle(zip_bytes: bytes) -> Dict[str, Any]:
    """Zip → classified entries, with zip-bomb + path-traversal defense.
    Returns ``{"entries": [{name, kind, data|None, reason}], "manifest": {...}}``.
    Raises ``BundleRejected`` when the archive itself is unusable."""
    if not zip_bytes or zip_bytes[:2] != b"PK":
        raise BundleRejected("not a zip archive (bad magic bytes)")
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        raise BundleRejected(f"corrupt zip: {exc}") from exc

    infos = [i for i in zf.infolist() if not i.is_dir()]
    if len(infos) > max_entries():
        raise BundleRejected(f"too many entries ({len(infos)} > {max_entries()})")
    total_uncompressed = sum(i.file_size for i in infos)
    if total_uncompressed > max_uncompressed_bytes():
        raise BundleRejected(
            f"uncompressed size {total_uncompressed} exceeds the "
            f"{max_uncompressed_bytes()} cap (zip-bomb defense)")

    entries: List[Dict[str, Any]] = []
    manifest: Dict[str, Any] = {}
    for info in infos:
        name = info.filename
        # Path traversal / absolute paths: reject the ENTRY, keep the bundle.
        if name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/"):
            entries.append({"name": name, "kind": "rejected", "reason": "path traversal"})
            continue
        if any(name.lower().endswith(ext) for ext in _EXECUTABLE_EXTS):
            entries.append({"name": name, "kind": "rejected", "reason": "executable entry"})
            continue
        if info.file_size > max_uncompressed_bytes():
            entries.append({"name": name, "kind": "rejected", "reason": "entry too large"})
            continue
        data = zf.read(info)  # bounded by the caps above
        head = data[:512]
        text_head = head.decode("utf-8", errors="replace").lstrip()[:200]
        kind = _classify(name, data[:256] if len(data) < 512 else data[:512], text_head)
        if kind == "manifest":
            try:
                manifest = json.loads(data.decode("utf-8", errors="replace"))
                entries.append({"name": name, "kind": "manifest"})
            except Exception:
                entries.append({"name": name, "kind": "rejected", "reason": "unparseable manifest.json"})
            continue
        entries.append({"name": name, "kind": kind, "data": data})
    return {"entries": entries, "manifest": manifest if isinstance(manifest, dict) else {}}


# ─── Assembly (PRD §3) ────────────────────────────────────────────────────────
def _merge_fragments(parts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge per-file fragments into ONE case's fragments: lists concatenate,
    demographics/vitals merge (first non-empty wins per key), the latest
    ``_index_event`` wins.

    ``studies`` and ``source_refs`` (Buyer Response PRD §2 A2/A3) are model-facing
    case fields and MUST be carried here — without the ``studies`` key, adapter
    studies were silently dropped during bundle assembly (the bug that would make
    A2 look fixed in a unit test and still broken in production). Answer-adjacent
    metadata (sealed key, eval task, case provenance) is carried under underscore
    keys so ``deidentify``/``_strip_meta`` keep it out of the model-visible body."""
    out: Dict[str, Any] = {"demographics": {}, "lab_panels": [], "notes": [],
                           "medications": [], "problem_list": [], "vitals": {},
                           "studies": [], "source_refs": []}
    index_event = None
    sealed = None
    eval_task = None
    case_provenance = None
    synthetic_declared = False
    for p in parts:
        for k in ("lab_panels", "notes", "medications", "problem_list", "studies",
                  "source_refs"):
            out[k].extend(p.get(k) or [])
        for k, v in (p.get("demographics") or {}).items():
            out["demographics"].setdefault(k, v)
        for k, v in (p.get("vitals") or {}).items():
            out["vitals"].setdefault(k, v)
        ie = p.get("_index_event")
        if ie and (index_event is None or str(ie) > str(index_event)):
            index_event = ie
        if p.get("_sealed_ground_truth") and sealed is None:
            sealed = p["_sealed_ground_truth"]
        if p.get("_eval_task") and eval_task is None:
            eval_task = p["_eval_task"]
        if p.get("_case_provenance") and case_provenance is None:
            case_provenance = p["_case_provenance"]
        synthetic_declared = synthetic_declared or bool(p.get("_synthetic_declared"))
    if index_event:
        out["_index_event"] = index_event
    if sealed:
        out["_sealed_ground_truth"] = sealed
    if eval_task:
        out["_eval_task"] = eval_task
    if case_provenance:
        out["_case_provenance"] = case_provenance
    out["_synthetic_declared"] = synthetic_declared
    return out


# ─── Completeness + answer-leakage guards (Buyer Response PRD §2 A4, §3 B1) ────
_COMPLETENESS_STOPWORDS = frozenset({
    "the", "and", "of", "a", "an", "with", "for", "in", "on", "to", "longitudinal",
    "clinical", "serial", "required", "study", "studies", "panel", "panels", "test",
    "tests", "imaging", "image", "images", "data",
})
# A declared token we cannot confidently resolve is NOT a missing modality. Synonyms
# let the common abbreviations resolve; anything still unresolved is reported as
# unverified rather than treated as absent (Audit PRD §P1).
_MODALITY_SYNONYMS = {
    "lm": ("light microscopy", "pas", "h&e", "hematoxylin"),
    "em": ("electron microscopy", "ultrastructural"),
    "if": ("immunofluorescence",),
    "routine if": ("frozen if", "immunofluorescence"),
    "pronase if": ("pronase", "paraffin immunofluorescence"),
    "urine microscopy": ("urine sediment", "sediment", "urinalysis"),
    "hematology studies": ("flow cytometry", "spep", "free light chain", "immunofixation"),
    "longitudinal labs": ("labs", "laboratory"),
    "clinical notes": ("note", "progress", "consult", "discharge"),
}


def _completeness_haystack(case: Dict[str, Any]) -> str:
    """Everything a declared modality could legitimately name. Declarations are
    TECHNIQUES ('pronase IF', 'EM'); Study.modality is a coarse ENUM ('pathology').
    Matching modality alone can never satisfy them, so the label and findings — where
    the technique is actually named — are part of the haystack."""
    bits: List[str] = []
    for s in case.get("studies") or []:
        bits += [str(s.get("modality") or ""), str(s.get("label") or ""),
                 str(s.get("findings") or "")]
    for p in case.get("lab_panels") or []:
        bits.append(str(p.get("panel") or ""))
        for r in p.get("results") or []:
            bits.append(str(r.get("analyte") or ""))
    for n in case.get("notes") or []:
        bits += [str(n.get("note_type") or ""), str(n.get("text") or "")]
    if case.get("lab_panels"):
        bits.append("labs longitudinal laboratory")
    if case.get("notes"):
        bits.append("clinical notes note")
    if case.get("medications"):
        bits.append("medications")
    return " ".join(bits).lower()


def completeness_check(declared: List[str], case: Dict[str, Any]) -> Dict[str, Any]:
    """Tri-state completeness (Buyer Response PRD §2 A4, corrected — Audit PRD §P1).

    Returns ``{present, missing, unresolved}``. Only ``missing`` — a token we
    RECOGNISED and confirmed absent — may quarantine. An ``unresolved`` token means our
    matcher did not understand the partner's wording, which is a fact about our parser,
    not about their data; quarantining on it rejects good cases (exactly what happened
    to the real PGNMID bundle) and hides the parser gap behind a clinical-sounding
    rejection."""
    hay = _completeness_haystack(case)
    present, missing, unresolved = [], [], []
    for tok in declared or []:
        t = tok.strip().lower()
        if not t:
            continue
        candidates = (t,) + tuple(_MODALITY_SYNONYMS.get(t, ()))
        recognised = t in _MODALITY_SYNONYMS or len(t.split()) <= 4
        if any(c in hay for c in candidates):
            present.append(tok)
        elif recognised:
            missing.append(tok)
        else:
            unresolved.append(tok)
    return {"present": present, "missing": missing, "unresolved": unresolved}


# ─── Admin review queue (Audit PRD §20-§21) ──────────────────────────────────
def _raise_review(reasons: List[Dict[str, Any]], reason: str, severity: str,
                  detail: str) -> None:
    """Append a review reason instead of logging it into a void (Audit PRD §21.2).
    ``blocking`` holds the case out of the annotation queue (a physician must not see
    it yet); ``advisory`` does not (the case is clinically intact, only a claim about
    it is unverified — holding it would rebuild the completeness bug with a nicer UI)."""
    reasons.append({"reason": reason, "severity": severity, "detail": detail,
                    "raised_at": _utcnow_iso()})


def _upload_status_from_cases(ingested: int, quarantined: int, needs_review: int) -> str:
    """Upload status is the WORST state among its cases, with needs_review ranked
    ABOVE ingested (Audit PRD §21.3). A partner upload where one case needs review
    must not render as a clean green row — an unreviewed blocking flag that looks
    finished is exactly the failure this whole spec is about."""
    if ingested == 0 and quarantined == 0 and needs_review == 0:
        return "rejected"
    if needs_review:
        return "needs_review"
    if ingested:
        return "ingested"
    return "quarantined"


def cf_case_has_asset(study: Dict[str, Any]) -> bool:
    from asclepius.cases import study_has_valid_asset
    return study_has_valid_asset(study)


def key_image_series_cap() -> int:
    """Above this many instances in one series, the partner (or an annotator) MUST
    designate key images — the 1-5 instances the reasoning depends on (Buyer Response
    PRD §4 C2). Without it, one CT study produces 200 assets, blows the prompt budget,
    and buries the finding, so a large series is archived-only until key images are
    named."""
    try:
        return max(1, int(os.getenv("ASCLEPIUS_KEY_IMAGE_SERIES_CAP", "5")))
    except ValueError:
        return 5


def _dicom_entries_to_studies(
    dicom_entries: List[Dict[str, Any]], manifest: Dict[str, Any], specialty: str,
) -> Tuple[Dict[str, List[Dict[str, Any]]], List[Dict[str, Any]], bool]:
    """Turn DICOM bundle entries into per-patient Study fragments (Buyer Response PRD
    §4 C1-C3). Each entry: de-identify (PS3.15 Annex E) → burned-in risk → render/
    archive. Returns (per_patient_frags, file_outcomes, produced_any_gradable).

    Key-image discipline (§4 C2): a series larger than the cap promotes an asset ONLY
    for instances the manifest designates as key images; the rest are archived so a
    200-instance CT does not produce 200 gradable assets."""
    from asclepius import dicom_deid

    key_ids = {str(k).strip().lower() for k in (manifest.get("key_images") or [])}
    cap = key_image_series_cap()

    # First pass: parse + de-id, group by series, so we know each series' size.
    parsed: List[Dict[str, Any]] = []
    series_counts: Dict[str, int] = {}
    for e in dicom_entries:
        name = e.get("name")
        try:
            ds = dicom_deid.read(e["data"])
            clean, dreport = dicom_deid.deidentify_dicom(ds)
        except dicom_deid.DicomDeidError as exc:
            parsed.append({"name": name, "error": str(exc)})
            continue
        series = str(getattr(clean, "SeriesInstanceUID", "") or name)
        sop = str(getattr(clean, "SOPInstanceUID", "") or "")
        series_counts[series] = series_counts.get(series, 0) + 1
        parsed.append({"name": name, "ds": clean, "series": series, "sop": sop,
                       "report": dreport})

    per_patient: Dict[str, List[Dict[str, Any]]] = {}
    outcomes: List[Dict[str, Any]] = []
    produced = False
    pk = str(manifest.get("patient_key") or "default")
    for p in parsed:
        name = p["name"]
        if p.get("error"):
            outcomes.append({"name": name, "kind": "dicom",
                             "outcome": f"rejected_unreadable: {p['error']}"})
            continue
        ds = p["ds"]
        risk, why = dicom_deid.burned_in_risk(ds)
        if risk == "blocked":
            outcomes.append({"name": name, "kind": "dicom",
                             "outcome": f"rejected_burned_in_phi: {why}"})
            continue
        # Key-image gate: a large series promotes an asset only for designated images.
        is_key = (series_counts.get(p["series"], 1) <= cap
                  or (name or "").lower() in key_ids
                  or p["sop"].lower() in key_ids)
        if not is_key:
            outcomes.append({"name": name, "kind": "dicom",
                             "outcome": "archived_only: large series, not a designated key image"})
            continue
        # Rendering can fail on undecodable/compressed pixel data or a missing
        # decoder — that must NEVER crash process_upload (a background task that must
        # always land a terminal status). A render failure downgrades this ONE entry
        # to an unreadable outcome; the rest of the bundle continues.
        try:
            frag = dicom_deid.to_study_fragment(
                ds, render=(risk == "clear"), needs_review=(risk == "suspect"),
                specialty=specialty, risk=risk, reason=why)
        except Exception as exc:
            log.warning("dicom render/fragment failed for %s: %s", name, exc)
            outcomes.append({"name": name, "kind": "dicom",
                             "outcome": f"rejected_unreadable: could not render pixels ({exc})"})
            continue
        study = {k: v for k, v in frag.items() if not str(k).startswith("_")}
        per_patient.setdefault(pk, []).append({"studies": [study], "_dicom": True})
        if risk == "clear" and cf_case_has_asset(study):
            produced = True
            outcomes.append({"name": name, "kind": "dicom", "outcome": "parsed"})
        else:
            outcomes.append({"name": name, "kind": "dicom",
                             "outcome": f"needs_burnin_review: {why}"})
    return per_patient, outcomes, produced


def _store_sealed_ground_truth(store: Any, ingest_case_id: str, upload_id: str,
                               sealed: Optional[Dict[str, Any]], pk: str) -> bool:
    """Persist the sealed answer key encrypted, keyed to the ingest case (Buyer
    Response PRD §3 B1). Returns True on success, False on storage failure.

    A storage failure must SURFACE, not silently drop an answer key we then cannot
    diff against the physician's independent answer: the caller re-classifies the case
    as quarantined so it never ships as cleanly-ingested with a lost answer key. Only
    the audit ``log_event`` is best-effort."""
    if not sealed:
        return True
    try:
        store.insert_sealed_ground_truth(
            ingest_case_id=ingest_case_id, upload_id=upload_id, payload=sealed)
    except Exception as exc:
        log.error("sealed ground truth storage failed for %s: %s", ingest_case_id, exc)
        try:
            store.log_event(entity_type="ingest_case", entity_id=ingest_case_id,
                            event_type="sealed_storage_failed",
                            payload={"upload_id": upload_id, "error": str(exc)})
        except Exception:  # pragma: no cover
            pass
        return False
    try:
        store.log_event(entity_type="ingest_case", entity_id=ingest_case_id,
                        event_type="sealed_ground_truth_ingested",
                        payload={"upload_id": upload_id,
                                 "patient_key": opaque_patient_key(pk)})
    except Exception:  # pragma: no cover - audit is best-effort
        pass
    return True


class AnswerLeakageError(BundleRejected):
    """A distinctive sealed-answer span appears in the model-visible case (Buyer
    Response PRD §3 B1). This must FAIL THE INGEST — a leaked answer silently
    invalidates every score computed from the case."""


def _distinctive_tokens(text: str) -> List[str]:
    return [w for w in re.findall(r"[a-z0-9]+", str(text or "").lower())
            if w not in _COMPLETENESS_STOPWORDS and len(w) >= 3]


def _longest_contiguous_run(needle: List[str], hay_padded: str) -> int:
    """Longest contiguous run of ``needle`` tokens appearing as a whole-token span in
    ``hay_padded`` (which is the space-joined hay wrapped in sentinel spaces so a match
    respects token boundaries — 'a b c' never matches inside 'xa b cx')."""
    best = 0
    for i in range(len(needle)):
        j = i
        while j < len(needle) and (" " + " ".join(needle[i:j + 1]) + " ") in hay_padded:
            j += 1
        best = max(best, j - i)
    return best


def assert_no_answer_leakage(case: Dict[str, Any], sealed: Optional[Dict[str, Any]]) -> None:
    """Post-condition (Buyer Response PRD §3 B1): the SEALED answer-key resource must
    not have leaked into the model-visible case. Runs after ``deidentify()``, before
    the case is stored.

    The failure mode this guards is the sealed ``Basic`` being MERGED into the case
    body during parsing (a note, a study finding). It must NOT quarantine a real
    de-identified record whose clinical notes legitimately state the diagnosis — a
    pathology report says "Final: <diagnosis>" and a problem list carries the known
    condition, and the adjudicated answer key naturally restates those same facts.
    Quarantining on that coincidence rejects real hospital data, the exact
    two-conditions-one-check anti-pattern the audit calls out (§17).

    The distinguisher: a genuine merge reproduces a whole answer-key LEAF nearly
    verbatim; a clinical coincidence reproduces only the diagnosis NAME, a fraction of
    the leaf. So we flag only when a sealed leaf is SUBSTANTIALLY REPRODUCED (>=80% of
    its distinctive tokens appear as one contiguous run). ``source_refs`` are excluded
    because they are never rendered into the model prompt (annotator-only, §H4)."""
    if not sealed:
        return
    from asclepius.cases import render_case_prompt

    # Model-visible surface ONLY — render_case_prompt already honors model_visible
    # notes + study_findings_policy. source_refs are NOT model-visible.
    hay = " ".join(_distinctive_tokens(render_case_prompt(case, "")))
    hay_padded = " " + hay + " "

    answer_key = sealed.get("answer_key") if isinstance(sealed, dict) else sealed
    for leaf in _sealed_leaf_strings(answer_key):
        toks = _distinctive_tokens(leaf)
        if len(toks) < 2:
            continue  # single-token leaves handled by the rarity-gated check (M1)
        need = max(2, -(-len(toks) * 8 // 10))  # ceil(0.8 * len)
        if _longest_contiguous_run(toks, hay_padded) >= need:
            raise AnswerLeakageError(
                "sealed answer key leaked into the model-visible case: a sealed leaf "
                "was substantially reproduced; refusing to ship (Buyer Response PRD §3 B1)")


def _sealed_leaf_strings(obj: Any) -> List[str]:
    """Every leaf string value inside the sealed answer key (recursively), so the
    leakage guard checks the ANSWER content, not JSON scaffolding tokens."""
    out: List[str] = []
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            out.extend(_sealed_leaf_strings(v))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out.extend(_sealed_leaf_strings(v))
    elif obj is not None:
        out.append(str(obj))
    return out


def _patient_key_of(fragment: Dict[str, Any], entry_name: str, manifest: Dict[str, Any]) -> str:
    # The manifest is the AUTHORITATIVE grouping hint (PRD §5): when the partner
    # declares a patient_key, every entry in the bundle belongs to that one case
    # (FHIR ids / CSV keys are per-system and would otherwise split the case).
    if manifest.get("patient_key"):
        return str(manifest["patient_key"])
    keys = fragment.get("_patient_keys") or []
    if keys:
        return str(keys[0])
    # filename convention: "<patient>__anything.ext"
    base = os.path.basename(entry_name)
    if "__" in base:
        return base.split("__", 1)[0]
    return "default"


def opaque_patient_key(raw_key: str) -> str:
    """The PERSISTED/LOGGED form of a grouping key (security review): a partner
    may put an MRN or a name in the CSV/manifest ``patient_key`` — which never
    passes through the case-body PHI scan — so anything stored in ingest_cases
    or emitted to the audit log is an opaque SHA-256 tag, never the raw key. The
    raw key exists only in-memory for grouping within one ingest run."""
    return "pk-" + hashlib.sha256((raw_key or "default").encode("utf-8")).hexdigest()[:12]


# ─── The orchestration (PRD §3) ───────────────────────────────────────────────
def process_upload(store: Any, upload_id: str) -> Dict[str, Any]:
    """Run the full pipeline for a received upload. Never raises — every outcome
    (ingested / quarantined / rejected) lands on the upload + case rows with
    audit events. Returns a summary dict."""
    upload = store.get_ingest_upload(upload_id)
    if not upload:
        return {"error": "upload not found"}

    def _fail(reason: str) -> Dict[str, Any]:
        store.update_ingest_upload(upload_id, status="rejected", reason=reason)
        store.log_event(entity_type="ingest_upload", entity_id=upload_id,
                        event_type="upload_rejected", payload={"reason": reason})
        # Auto-notify the sender their upload didn't come through (no PHI). Best
        # effort — a notification issue must never affect the pipeline outcome.
        try:
            from asclepius import ingest_notify
            ingest_notify.notify_upload_failed(
                store, store.get_ingest_upload(upload_id), outcome="rejected")
        except Exception:  # pragma: no cover - defensive
            pass
        return {"status": "rejected", "reason": reason}

    store.update_ingest_upload(upload_id, status="scanning")
    ok, detail = malware_scan(upload["raw_path"])
    store.log_event(entity_type="ingest_upload", entity_id=upload_id,
                    event_type="malware_scan", payload={"ok": ok, "detail": detail})
    if not ok:
        return _fail(detail)

    store.update_ingest_upload(upload_id, status="parsing")
    try:
        raw = load_raw(upload["raw_path"])
        bundle = unpack_bundle(raw)
    except BundleRejected as exc:
        return _fail(str(exc))
    except Exception as exc:  # unreadable blob, key rotation issue, …
        return _fail(f"could not read/unpack the upload: {exc}")

    manifest = bundle["manifest"]
    specialty = (manifest.get("specialty")
                 or (store.get_upload_link(upload["link_id"]) or {}).get("specialty")
                 or "nephrology")

    # Adapter pass: entry → fragments, grouped per patient.
    per_patient: Dict[str, List[Dict[str, Any]]] = {}
    file_outcomes: List[Dict[str, Any]] = []
    imaging_rejected = 0
    parsed_any = False
    # DICOM is no longer an automatic rejection (Buyer Response PRD §4 C3, retiring the
    # "no imaging" invariant, dated 2026-07): de-identify (PS3.15 Annex E) → burned-in
    # risk → render/archive. Handled as a batch so series size (key-image discipline)
    # is known.
    dicom_entries = [e for e in bundle["entries"] if e.get("kind") == "dicom"]
    if dicom_entries:
        try:
            d_per_patient, d_outcomes, d_produced = _dicom_entries_to_studies(
                dicom_entries, manifest, specialty)
        except Exception as exc:  # never strand the upload on a DICOM surprise
            log.warning("dicom batch processing failed for upload %s: %s", upload_id, exc)
            d_per_patient, d_produced = {}, False
            d_outcomes = [{"name": e.get("name"), "kind": "dicom",
                           "outcome": f"rejected_unreadable: {exc}"} for e in dicom_entries]
        for pk, frags in d_per_patient.items():
            per_patient.setdefault(pk, []).extend(frags)
        file_outcomes.extend(d_outcomes)
        parsed_any = parsed_any or d_produced
    for e in bundle["entries"]:
        name, kind = e.get("name"), e.get("kind")
        if kind == "manifest":
            file_outcomes.append({"name": name, "kind": kind, "outcome": "used"})
            continue
        if kind == "dicom":
            continue  # handled in the batch above
        if kind in ("rejected", "unsupported"):
            file_outcomes.append({"name": name, "kind": kind,
                                  "outcome": e.get("reason") or "unsupported"})
            continue
        entry_manifest = dict(manifest)
        entry_manifest["filename"] = name
        try:
            frag = cf.FORMATS[kind](e["data"], specialty=specialty, manifest=entry_manifest)
            parsed_any = True
            pk = _patient_key_of(frag, name, manifest)
            per_patient.setdefault(pk, []).append(frag)
            file_outcomes.append({"name": name, "kind": kind, "outcome": "parsed",
                                  "patient_key": opaque_patient_key(pk)})
        except Exception as exc:
            file_outcomes.append({"name": name, "kind": kind, "outcome": f"parse_failed: {exc}"})

    if not parsed_any:
        store.update_ingest_upload(upload_id, files_json=file_outcomes)
        # The "imaging-only bundle is rejected wholesale" invariant is RETIRED (Buyer
        # Response PRD §4 C3, 2026-07): a pathology or radiology case may legitimately
        # be imaging-only. What we require instead is at least one GRADABLE study — a
        # cleared/reviewer-approved asset. A bundle whose only DICOMs were blocked or
        # left pending burned-in review produced nothing gradable and is rejected with
        # that reason, not a blanket "imaging is never gradable".
        if dicom_entries and not any(
            e.get("kind") not in ("manifest", "dicom") for e in bundle["entries"]
        ):
            return _fail("no gradable study in the bundle: every image was blocked or "
                         "left pending burned-in review (needs a cleared/approved asset)")
        return _fail("no parseable clinical content in the bundle")

    # Per patient: assemble → normalize → verify → hard guard → land or quarantine.
    ingested, quarantined, needs_review = 0, 0, 0
    for pk, parts in per_patient.items():
        merged = _merge_fragments(parts)
        report: Dict[str, Any] = {"patient_key": opaque_patient_key(pk)}
        # Answer-adjacent + author metadata pulled OUT before assembling the body —
        # never merged into a model-visible field (Buyer Response PRD §2 A4, §3 B1).
        sealed = merged.get("_sealed_ground_truth")
        eval_task = merged.get("_eval_task") or {}
        case_provenance = merged.get("_case_provenance")
        if sealed:
            report["sealed_present"] = True
        if eval_task:
            report["eval_task"] = {k: v for k, v in eval_task.items() if k != "answer_key"}
        # The quarantined body must be EXACTLY the object the findings describe
        # (spans are offsets into it) — the normalized case once normalization
        # succeeds, the raw merge only when normalization itself failed.
        quarantine_body = {k: v for k, v in merged.items() if not str(k).startswith("_")}
        try:
            normalized, treport = normalize_timeline(
                quarantine_body,
                index_event=manifest.get("index_event") or merged.get("_index_event"),
            )
            report["timeline"] = treport
            quarantine_body = normalized
            if treport.get("unresolved"):
                raise cf.CaseIngestError(
                    "unresolved date-like tokens: " + ", ".join(treport["unresolved"][:5]))
            verification = deid_verify.verify_deid(normalized)
            report["verification"] = verification
            if verification["status"] == "flagged":
                raise cf.CaseIngestError(
                    f"de-id verification flagged {len(verification['findings'])} finding(s)")
            safe = cf.deidentify(normalized)
            # Inject author-declared metadata (Buyer Response PRD §2 A3/A4) + compute
            # the study-findings policy (§3 B2): hidden when any study carries a
            # resolvable image asset (the real multimodal test), visible otherwise.
            declared_mods = list(eval_task.get("required_modalities") or [])
            any_asset = any(cf_case_has_asset(s) for s in (safe.get("studies") or []))
            case = ClinicalCase(**{
                **safe, "case_source": "real_deid",
                "specialty": safe.get("specialty") or specialty,
                "declared_difficulty": eval_task.get("declared_difficulty"),
                "required_modalities": declared_mods,
                "case_provenance": case_provenance,
                "study_findings_policy": "hidden" if any_asset else "visible",
            }).model_dump()
            # Declared-vs-delivered completeness (Buyer Response PRD §2 A4; corrected
            # to tri-state — Audit PRD §P1). Only a RECOGNISED-and-absent token
            # quarantines. An UNRESOLVED token is a parser gap, not missing evidence:
            # ingest, flag as unverified, and (Phase 4) surface it as an ADVISORY
            # review reason — quarantining on it rejects good hospital data.
            comp = completeness_check(declared_mods, case)
            report["completeness"] = comp
            if comp["missing"]:
                report["missing_modalities"] = comp["missing"]
                raise cf.CaseIngestError(
                    f"bundle declares required modalities not delivered: {sorted(comp['missing'])}; "
                    f"the case's decisive evidence is absent — quarantining rather than "
                    f"shipping an unanswerable case")
            case["completeness_status"] = "unverified" if comp["unresolved"] else "verified"
            # Post-condition (Buyer Response PRD §3 B1): no distinctive sealed-answer
            # span may appear in the model-visible case. Runs after deidentify(),
            # before the case is stored.
            assert_no_answer_leakage(case, sealed)
            # Admin review queue (Audit PRD §21). Collect the three "unknown" states
            # that used to have nowhere to go and route them to a human. Blocking
            # reasons hold the case out of the annotation queue; advisory reasons
            # ingest cleanly (the case is intact, only a claim about it is unverified).
            review_reasons: List[Dict[str, Any]] = []
            for st in (case.get("studies") or []):
                phi = st.get("phi_screening") or {}
                if phi.get("burned_in_risk") == "suspect":
                    _raise_review(review_reasons, "burned_in_phi_unverified", "blocking",
                                  phi.get("reason")
                                  or f"study '{st.get('label') or st.get('modality')}': "
                                     "burned-in PHI could not be screened")
                elif phi.get("method") == "tag_only":
                    _raise_review(review_reasons, "deid_partner_flag_only", "advisory",
                                  f"study '{st.get('label') or st.get('modality')}': "
                                  "OCR screening unavailable; cleared on DICOM tags alone")
            if comp["unresolved"]:
                _raise_review(review_reasons, "completeness_unverified", "advisory",
                              "could not resolve declared modalities against delivered "
                              f"evidence: {sorted(comp['unresolved'])}")
            blocking = [r for r in review_reasons if r["severity"] == "blocking"]
            case_status = "needs_review" if blocking else "ingested"
            ic = store.insert_ingest_case(upload_id=upload_id,
                                          patient_key=opaque_patient_key(pk),
                                          specialty=specialty, case=case,
                                          status=case_status, report=report,
                                          review_status=("needs_review" if review_reasons else None),
                                          review_json=review_reasons or None)
            # Answer-key storage must succeed for the case to ship as ingested: if it
            # fails we re-classify the case as quarantined rather than shipping it
            # cleanly with a lost adjudication (Buyer Response PRD §3 B1).
            if not _store_sealed_ground_truth(store, ic["ingest_case_id"], upload_id, sealed, pk):
                report["quarantine_reason"] = (
                    "sealed answer key could not be stored; quarantining rather than "
                    "shipping a case whose adjudication was lost")
                store.update_ingest_case(ic["ingest_case_id"], status="quarantined",
                                         report_json=report)
                quarantined += 1
                store.log_event(entity_type="ingest_case", entity_id=ic["ingest_case_id"],
                                event_type="case_quarantined",
                                payload={"upload_id": upload_id,
                                         "patient_key": opaque_patient_key(pk),
                                         "reason": "sealed_storage_failed"})
                continue
            if blocking:
                needs_review += 1
            else:
                ingested += 1
            store.log_event(entity_type="ingest_case", entity_id=ic["ingest_case_id"],
                            event_type=("case_needs_review" if blocking else "case_ingested"),
                            payload={"upload_id": upload_id,
                                     "patient_key": opaque_patient_key(pk),
                                     "panels": len(case.get("lab_panels") or []),
                                     "notes": len(case.get("notes") or []),
                                     "studies": len(case.get("studies") or []),
                                     "source_refs": len(case.get("source_refs") or []),
                                     "review_reasons": [r["reason"] for r in review_reasons],
                                     "sealed_stored": bool(sealed)})
        except (cf.CaseIngestError, TimelineError, ValidationError, AnswerLeakageError) as exc:
            # ValidationError (BUG-1 hardening): a real bundle whose structure
            # drifts from the ClinicalCase schema — now that the case models are
            # extra="forbid" — quarantines with a readable reason instead of
            # silently dropping the stray field (the old extra="ignore" data loss)
            # OR crashing the background ingest job. Loud, recoverable, never silent.
            report["quarantine_reason"] = str(exc)
            # The quarantine body is a plain merge that may still carry the raw
            # ``studies``/``source_refs`` — strip any answer-adjacent metadata was
            # already excluded (underscore keys). Never let a sealed key ride along.
            ic = store.insert_ingest_case(
                upload_id=upload_id, patient_key=opaque_patient_key(pk),
                specialty=specialty, case=quarantine_body,
                status="quarantined", report=report)
            quarantined += 1
            # Sealed answer key is stored separately + encrypted even for a
            # quarantined case (it is still the referring institution's adjudication,
            # used for §7 F3 external-agreement once the case is reviewed).
            _store_sealed_ground_truth(store, ic["ingest_case_id"], upload_id, sealed, pk)
            store.log_event(entity_type="ingest_case", entity_id=ic["ingest_case_id"],
                            event_type="case_quarantined",
                            payload={"upload_id": upload_id,
                                     "patient_key": opaque_patient_key(pk),
                                     "reason": str(exc)})
        except Exception as exc:  # infra error (store/DB/log) — never strand the upload
            # process_upload must always land a terminal status. An unexpected error
            # (DB write, log_event, crypto) is recorded as a per-patient quarantine
            # rather than escaping the BackgroundTask and leaving the upload stuck in
            # 'parsing' forever.
            log.warning("ingest: unexpected error for patient %s in upload %s: %s",
                        opaque_patient_key(pk), upload_id, exc)
            try:
                report["quarantine_reason"] = f"unexpected ingest error: {exc}"
                ic = store.insert_ingest_case(
                    upload_id=upload_id, patient_key=opaque_patient_key(pk),
                    specialty=specialty, case=quarantine_body,
                    status="quarantined", report=report)
                quarantined += 1
                store.log_event(entity_type="ingest_case", entity_id=ic["ingest_case_id"],
                                event_type="case_quarantined",
                                payload={"upload_id": upload_id, "reason": "ingest_error"})
            except Exception:  # pragma: no cover - last-resort; do not re-raise
                log.exception("ingest: could not even quarantine patient %s in upload %s",
                              opaque_patient_key(pk), upload_id)

    status = _upload_status_from_cases(ingested, quarantined, needs_review)
    reason = None
    if status == "needs_review":
        reason = "one or more cases held for admin review"
    elif status == "quarantined":
        reason = "all cases quarantined — review findings"
    elif status == "rejected":
        reason = "nothing ingested"
    store.update_ingest_upload(upload_id, status=status, reason=reason, files_json=file_outcomes)
    store.log_event(entity_type="ingest_upload", entity_id=upload_id,
                    event_type="upload_processed",
                    payload={"status": status, "ingested": ingested,
                             "quarantined": quarantined, "needs_review": needs_review,
                             "imaging_rejected": imaging_rejected})
    purge_expired_raw(store)
    return {"status": status, "ingested": ingested, "quarantined": quarantined,
            "needs_review": needs_review, "imaging_rejected": imaging_rejected,
            "files": file_outcomes}


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
