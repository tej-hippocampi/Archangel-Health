"""Read-only release material, prepared and reviewed before applicants arrive.

Files are backend-only, never a static/public download. Existing ready database
rows take precedence; this library does not overwrite past draws or paid batches.
"""
from __future__ import annotations

from contextvars import ContextVar
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re

USE_BUNDLED = ContextVar("onboarding_use_bundled", default=True)

ROOT = Path(__file__).with_name("onboarding_material") / "cases"
LEGACY_AUDITS = Path(__file__).with_name("onboarding_material") / "legacy_evidence_audits.json"
AUTHORING_FEEDBACK = Path(__file__).with_name("onboarding_material") / "authoring_feedback.json"
OPENAI_AUDITS = Path(__file__).with_name("onboarding_material") / "openai_release_audits.json"
AUDIT_REPORTS = Path(__file__).with_name("onboarding_material") / "audits"


def authoring_feedback(ident: str) -> dict | None:
    """Retain independent audit findings when a rejected CI draft is regenerated.

    This is authoring context only, never evidence or an approval. It contains no
    applicant data and is not part of the public/blinded case payload.
    """
    return json.loads(AUTHORING_FEEDBACK.read_text()).get(ident)


def validate(document: dict) -> dict:
    from asclepius import onboarding_cases as bank, onboarding_media
    from asclepius.onboarding_specialties import canonical
    from asclepius.onboarding_catalog import age_scope_for
    specialty, kind = document["specialty"], document["kind"]
    ident = bank.task_id(specialty, kind, document.get("slot", 1))
    if specialty != canonical(specialty) or document["task_id"] != ident:
        raise ValueError("Invalid library identity")
    report = document["validation"]
    openai_audited = report.get("method") == "openai_only_trial"
    if openai_audited:
        # A model pass is not a publication instruction. Only exact immutable
        # documents explicitly accepted by the separate artifact audit qualify.
        # Preserve the original method: these are two OpenAI models, not two
        # providers and never physician ratification. Runtime generation still
        # uses the original cross-provider requirement.
        digest = hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()
        audits = json.loads(OPENAI_AUDITS.read_text()) if OPENAI_AUDITS.is_file() else {}
        accepted = audits.get("artifacts", {}).get(ident, {})
        if (audits.get("protocol") != "audited_openai_models_v1"
                or document.get("release_protocol") != "audited_openai_models_v1"
                or document.get("release_eligible") is not True
                or report.get("physician_ratified") is not False
                or report.get("source_quote_review") is not True
                or accepted.get("document_sha256") != digest
                or accepted.get("disposition") != "clear"
                or not accepted.get("audit_report")
                or not re.fullmatch(r"[a-f0-9]{64}", accepted.get("source_artifact_sha256", ""))):
            raise ValueError("Real clinical review required: exact OpenAI artifact audit missing")
        audit_name = accepted["audit_report"]
        if (not isinstance(audit_name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+\.json", audit_name)
                or not (AUDIT_REPORTS / audit_name).is_file()
                or hashlib.sha256((AUDIT_REPORTS / audit_name).read_bytes()).hexdigest() != accepted.get("audit_sha256")):
            raise ValueError("Independent artifact audit report missing or changed")
        independent = json.loads((AUDIT_REPORTS / audit_name).read_text())
        findings = independent.get("artifacts", independent.get("cases", []))
        if not any(row.get("task_id") == ident
                   and row.get("entry_sha256") == report.get("entry_sha256")
                   and (row.get("verdict") or row.get("disposition")) == "clear"
                   for row in findings):
            raise ValueError("Independent audit did not clear this exact entry")
    elif report.get("method") != "two_provider_evidence_review":
        raise ValueError("Real clinical review required for release material")
    elif document.get("release_protocol") or "release_eligible" in document:
        raise ValueError("Trial envelope cannot claim cross-provider provenance")
    if report.get("version") != bank.VERSION:
        raise ValueError("Unsupported clinical review version")
    quoted = report.get("source_quote_review") is True
    if not quoted:
        # Preserve only the exact pre-protocol documents independently audited
        # against retained source text. A new report cannot opt out by deleting
        # its protocol marker, or by reusing a legacy identity with changed text.
        digest = hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()
        audited = json.loads(LEGACY_AUDITS.read_text())["artifacts"]
        if not any(r["task_id"] == ident and r["document_sha256"] == digest for r in audited):
            raise ValueError("Source-quote review required for new release material")
    asset = onboarding_media.reference(kind)["asset"] if specialty == "pathology" else None
    entry = bank.validate_entry(document["entry"], specialty, report["sources"], approved_asset=asset,
                                age_scope=age_scope_for(specialty))
    digest = hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()
    if report.get("entry_sha256") != digest:
        raise ValueError("Reviewed entry checksum mismatch")
    reviews = report.get("reviews") or []
    if openai_audited:
        if (len(reviews) != 2 or {r.get("provider") for r in reviews} != {"openai"}
                or {r.get("model") for r in reviews} != {"gpt-5.6-sol", "gpt-5.5"}):
            raise ValueError("Two distinct approved OpenAI models required")
    elif len(reviews) != 2 or {r.get("provider") for r in reviews} != {"anthropic", "openai"}:
        raise ValueError("Two independent clinical providers required")
    correct = "B" if entry["intended_flawed_id"] == "A" else "A"
    for review in reviews:
        from ai.model_config import resolve_provider
        if resolve_provider(review.get("model")) != review.get("provider"):
            raise ValueError("Reviewer provider does not match model provenance")
        bank.validate_review(review["review"], entry,
                             report["sources"] if quoted else None)
        solved = review["blind_solution"]
        confidence = solved.get("confidence")
        if (solved.get("best_answer_id") != correct or not solved.get("rationale")
                or isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                or not .9 <= confidence <= 1):
            raise ValueError("Blinded clinical agreement required")
        if asset and (review.get("image_sha256") != asset["sha256"]
                or review["review"].get("image_supports_key") is not True
                or review["review"].get("image_has_no_identifiers") is not True
                or not review["review"].get("image_observations")):
            raise ValueError("Pixel review required")
    if asset:
        onboarding_media.load(asset)
    return document


@lru_cache(maxsize=128)
def _read(path: str, mtime: int) -> str:
    document = validate(json.loads(Path(path).read_text()))
    return json.dumps(document)


def row_for(ident: str) -> dict | None:
    if not USE_BUNDLED.get():
        return None
    if not re.fullmatch(r"onboarding-(practice|examination)-v\d+-[a-f0-9]{20}-\d+", ident or ""):
        return None
    path = ROOT / (ident + ".json")
    if not path.is_file():
        return None
    doc = json.loads(_read(str(path), path.stat().st_mtime_ns))
    if doc["validation"].get("method") == "openai_only_trial":
        # Audit approval may be revoked without editing the immutable case file.
        # Recheck the registry/report on lookup instead of caching that authority.
        validate(doc)
    if doc["task_id"] != ident:
        raise ValueError("Library file does not match its requested case identity")
    return {"task_id": ident, "specialty": doc["specialty"], "kind": doc["kind"],
        "slot": doc.get("slot", 1), "version": doc["validation"]["version"], "status": "ready",
        "entry_json": json.dumps(doc["entry"]), "validation_json": json.dumps(doc["validation"])}


def coverage() -> list[dict]:
    from asclepius.onboarding_catalog import SPECIALTIES
    from asclepius.onboarding_cases import task_id
    return [{"specialty": specialty, "kind": kind, "ready": bool(row_for(task_id(specialty, kind)))}
            for specialty in SPECIALTIES for kind in ("practice", "examination")]
