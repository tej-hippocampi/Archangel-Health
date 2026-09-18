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


def validate(document: dict) -> dict:
    from asclepius import onboarding_cases as bank, onboarding_media
    from asclepius.onboarding_specialties import canonical
    specialty, kind = document["specialty"], document["kind"]
    ident = bank.task_id(specialty, kind, document.get("slot", 1))
    if specialty != canonical(specialty) or document["task_id"] != ident:
        raise ValueError("Invalid library identity")
    report = document["validation"]
    if report.get("method") != "two_provider_evidence_review" or report.get("version") != bank.VERSION:
        raise ValueError("Real clinical review required for release material")
    asset = onboarding_media.reference(kind)["asset"] if specialty == "pathology" else None
    entry = bank.validate_entry(document["entry"], specialty, report["sources"], approved_asset=asset)
    digest = hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()
    if report.get("entry_sha256") != digest:
        raise ValueError("Reviewed entry checksum mismatch")
    reviews = report.get("reviews") or []
    if len(reviews) != 2 or {r.get("provider") for r in reviews} != {"anthropic", "openai"}:
        raise ValueError("Two independent clinical providers required")
    correct = "B" if entry["intended_flawed_id"] == "A" else "A"
    for review in reviews:
        bank.validate_review(review["review"], entry)
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
