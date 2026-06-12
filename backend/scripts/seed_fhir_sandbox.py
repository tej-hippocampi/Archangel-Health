#!/usr/bin/env python3
"""Seed the local HAPI FHIR sandbox with synthetic TEAM-eligibility patients.

Loads FHIR R4 Patient + Coverage + DocumentReference resources that mirror two
of the ``sample_ehrs/`` personas, so the FHIR import flow can be exercised
end-to-end against the existing eligibility pipeline:

  - Margaret Okafor  — Original Medicare A+B, primary, age basis → ELIGIBLE
  - Harold Brennan   — Humana MA (Part C) enrolled               → INELIGIBLE

All data is synthetic (same fixtures as sample_ehrs). DEV ONLY.

Usage:
    docker compose -f docker-compose.fhir.yml up -d   # from repo root
    cd backend && python3 scripts/seed_fhir_sandbox.py
    # optional: --base http://localhost:8090/fhir
"""

from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path

import httpx

MBI_SYSTEM = "http://hl7.org/fhir/sid/us-mbi"
MRN_SYSTEM = "http://hartland-regional.example.org/mrn"
SAMPLE_EHRS = Path(__file__).resolve().parent.parent.parent / "sample_ehrs"


def _patient(pid: str, family: str, given: list[str], dob: str, gender: str, mbi: str, mrn: str) -> dict:
    return {
        "resourceType": "Patient",
        "id": pid,
        "identifier": [
            {"system": MBI_SYSTEM, "value": mbi},
            {"system": MRN_SYSTEM, "value": mrn},
        ],
        "name": [{"family": family, "given": given}],
        "birthDate": dob,
        "gender": gender,
    }


def _coverage(cid: str, patient_id: str, *, status: str, type_code: str, type_display: str,
              payor: str, subscriber_id: str, start: str, order: int) -> dict:
    return {
        "resourceType": "Coverage",
        "id": cid,
        "status": status,
        "type": {
            "coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
                "code": type_code,
                "display": type_display,
            }]
        },
        "beneficiary": {"reference": f"Patient/{patient_id}"},
        "subscriberId": subscriber_id,
        "payor": [{"display": payor}],
        "period": {"start": start},
        "order": order,
    }


def _docref(did: str, patient_id: str, title: str, text: str) -> dict:
    return {
        "resourceType": "DocumentReference",
        "id": did,
        "status": "current",
        "type": {"text": "Pre-operative eligibility summary"},
        "subject": {"reference": f"Patient/{patient_id}"},
        "content": [{
            "attachment": {
                "contentType": "text/plain",
                "title": title,
                "data": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            }
        }],
    }


def build_resources() -> list[dict]:
    resources: list[dict] = []

    # ── Margaret Okafor — eligible (Original Medicare A+B primary) ─────────
    resources.append(_patient(
        "okafor-margaret", "Okafor", ["Margaret", "Anne"], "1954-09-17", "female",
        "4WH7QD2RT55", "HRH-0049821",
    ))
    resources.append(_coverage(
        "okafor-medicare-a", "okafor-margaret", status="active",
        type_code="MCPOL", type_display="Medicare Part A (Hospital Insurance)",
        payor="Original Medicare (Fee-for-Service)", subscriber_id="4WH7QD2RT55",
        start="2019-10-01", order=1,
    ))
    resources.append(_coverage(
        "okafor-medicare-b", "okafor-margaret", status="active",
        type_code="MCPOL", type_display="Medicare Part B (Medical Insurance)",
        payor="Original Medicare (Fee-for-Service)", subscriber_id="4WH7QD2RT55",
        start="2019-10-01", order=1,
    ))
    resources.append(_coverage(
        "okafor-medigap", "okafor-margaret", status="active",
        type_code="SUPP", type_display="Medigap Plan G (supplemental)",
        payor="Mutual of Omaha", subscriber_id="MOO-7741280", start="2019-11-01", order=2,
    ))

    # ── Harold Brennan — ineligible (Medicare Advantage / Part C) ──────────
    resources.append(_patient(
        "brennan-harold", "Brennan", ["Harold", "James"], "1951-03-02", "male",
        "7TN3KF8WP91", "HRH-0051173",
    ))
    resources.append(_coverage(
        "brennan-ma-plan", "brennan-harold", status="active",
        type_code="MCPOL", type_display="Medicare Advantage — Humana Gold Plus HMO (MAPD), contract H1036 PBP 142",
        payor="Humana Gold Plus HMO (Medicare Part C)", subscriber_id="7TN3KF8WP91",
        start="2024-01-01", order=1,
    ))

    # ── DocumentReferences: attach the matching sample EHR text notes ──────
    for did, pid, fname in (
        ("okafor-preop-summary", "okafor-margaret", "01_okafor_eligible_clean.txt"),
        ("brennan-preop-summary", "brennan-harold", "02_brennan_ineligible_medicare_advantage.txt"),
    ):
        path = SAMPLE_EHRS / fname
        if path.exists():
            resources.append(_docref(did, pid, fname, path.read_text(encoding="utf-8")))
        else:
            print(f"  (skipping DocumentReference {did}: {path} not found)")
    return resources


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://localhost:8090/fhir", help="FHIR base URL")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    with httpx.Client(timeout=30) as http:
        try:
            meta = http.get(f"{base}/metadata", headers={"Accept": "application/fhir+json"})
            meta.raise_for_status()
        except httpx.HTTPError as e:
            print(f"Cannot reach FHIR server at {base}: {e}", file=sys.stderr)
            print("Start it with: docker compose -f docker-compose.fhir.yml up -d", file=sys.stderr)
            return 1
        print(f"Connected: FHIR {meta.json().get('fhirVersion', '?')} at {base}")

        for res in build_resources():
            rt, rid = res["resourceType"], res["id"]
            # PUT (update-as-create) keeps the script idempotent — rerun freely.
            resp = http.put(
                f"{base}/{rt}/{rid}",
                json=res,
                headers={"Content-Type": "application/fhir+json"},
            )
            if resp.status_code in (200, 201):
                print(f"  {'created' if resp.status_code == 201 else 'updated'}: {rt}/{rid}")
            else:
                print(f"  FAILED {rt}/{rid}: HTTP {resp.status_code} {resp.text[:200]}", file=sys.stderr)
                return 1

    print("\nSandbox seeded. Try:")
    print(f"  curl '{base}/Patient?identifier={MBI_SYSTEM}|4WH7QD2RT55'")
    print(f"  curl '{base}/Coverage?patient=okafor-margaret'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
