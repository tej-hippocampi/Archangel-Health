"""Pinned, public-domain teaching images. Never reads partner upload storage."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).with_name("onboarding_material")


def reference(kind: str) -> dict:
    if kind not in ("practice", "examination"):
        raise ValueError("Invalid reference image purpose")
    from asclepius.cases import StudyAsset
    row = json.loads((ROOT / "image_sources.json").read_text())[kind]
    row["asset"] = StudyAsset.model_validate(row["asset"]).model_dump()
    return row


def load(asset: dict) -> bytes:
    known = next((reference(k)["asset"] for k in ("practice", "examination")
                  if reference(k)["asset"] == asset), None)
    if not known:
        raise ValueError("Unknown onboarding image")
    data = (ROOT / "images" / (known["sha256"] + ".png")).read_bytes()
    if hashlib.sha256(data).hexdigest() != known["sha256"]:
        raise ValueError("Onboarding image checksum mismatch")
    return data


def message(payload: dict, asset: dict | None = None) -> list[dict]:
    text = json.dumps(payload)
    if not asset:
        return [{"role": "user", "content": text}]
    from ai.llm_client import image_block
    return [{"role": "user", "content": [{"type": "text", "text": text},
        image_block(asset["mime"], base64.b64encode(load(asset)).decode())]}]


def authorized_asset(store, user: dict, asset_id: str) -> dict | None:
    """Only a drawn practice/exam grants access; knowing an id grants nothing."""
    from asclepius import onboarding_cases
    if user.get("role") == "admin":
        # Credentialing reviewers need the same pixels as the applicant.
        return next((reference(k)["asset"] for k in ("practice", "examination")
                     if reference(k)["asset"]["asset_id"] == asset_id), None)
    state = store.get_tutorial_state(user["id"]) or {}
    exam = state.get("exam") or {}
    ids = [state.get("practice_task_id")]
    if exam.get("state") in ("in_progress", "submitted"):
        ids.append(exam.get("task_id"))
    for ident in ids:
        entry = onboarding_cases.entry_for(store, ident) if ident else None
        for study in (entry or {}).get("case", {}).get("studies", []):
            asset = study.get("asset") or {}
            if asset.get("asset_id") == asset_id:
                return asset
    return None
