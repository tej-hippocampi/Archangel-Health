"""Pakistan — Pakistan Medical & Dental Council.

The practitioner search on pmdc.pk posts to a JSON endpoint that answers by
registration number and returns the registration type, the dates it runs
between, and a status word:

    {"status": true,
     "data": [{"RegistrationNo", "Name", "FatherName", "RegistrationType",
               "RegistrationDate", "ValidUpto", "Status", ...}],
     "message": "1 Records Found!"}

The endpoint rejects a request that omits any of its three search parameters,
so the empty ones are sent explicitly rather than left out.

Not marked ``authoritative``: this is an undocumented endpoint behind a public
page, and it can change shape without anyone telling us. ``dispatch`` turns a
miss into INCONCLUSIVE and sends the doctor down the document path.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from asclepius import credentialing
from asclepius.registry.adapters import _http

log = logging.getLogger("asclepius.registry.pmdc_pk")

SEARCH_URL = "https://hospitals-inspections.pmdc.pk/api/DRC/GetData"

#: Status words that mean the registration is not currently good. Compared
#: case-folded; anything unrecognized is passed through for a human to read
#: rather than guessed at.
_NOT_IN_GOOD_STANDING = {
    "suspended", "cancelled", "canceled", "expired", "inactive", "blocked",
}


def fetch(
    identifier: str, *, extras: Optional[Dict[str, Any]] = None,
    timeout: float = _http.DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    number = (identifier or "").strip().upper()
    if not number:
        return _http.not_found()

    try:
        response = _http.request(
            "POST", SEARCH_URL, timeout=timeout,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
            # All three keys are required or the endpoint answers
            # "Invalid Parameters".
            data={"RegistrationNo": number, "Name": "", "FatherName": ""},
        )
    except Exception as exc:  # noqa: BLE001
        log.info("[registry:PK] request failed: %s", exc)
        return _http.unavailable("request_failed")

    if response.status_code != 200:
        return _http.unavailable(f"http_{response.status_code}")

    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        return _http.unavailable("unparseable_response")

    if not payload.get("status"):
        # The endpoint's own "we could not process this" answer. Not evidence
        # about the doctor.
        return _http.unavailable("registry_rejected_query")

    rows = payload.get("data")
    if not isinstance(rows, list):
        return _http.unavailable("unexpected_response_shape")
    if not rows:
        return _http.not_found()

    row = rows[0] or {}
    return _http.found({
        "full_name": " ".join(str(row.get("Name") or "").split()),
        "father_name": " ".join(str(row.get("FatherName") or "").split()),
        "registration_number": str(row.get("RegistrationNo") or "").strip(),
        "registration_type": str(row.get("RegistrationType") or "").strip(),
        "registered_on": str(row.get("RegistrationDate") or "").strip(),
        "valid_until": str(row.get("ValidUpto") or "").strip(),
        "status": str(row.get("Status") or "").strip(),
        "match_count": len(rows),
    })


def match(
    record: Dict[str, Any], family_name: str, extras: Optional[Dict[str, Any]] = None
) -> Tuple[str, Optional[str]]:
    from asclepius.registry.dispatch import RegistryResult

    if not credentialing.has_comparable_family_name(family_name):
        return RegistryResult.MISMATCH.value, "no_family_name_to_compare"

    name = record.get("full_name") or ""
    if not name:
        return RegistryResult.MISMATCH.value, "registry_record_has_no_name"

    if not credentialing.family_names_match(family_name, name):
        return (
            RegistryResult.MISMATCH.value,
            "family_name_mismatch_possible_transliteration",
        )

    status = (record.get("status") or "").strip().lower()
    if status in _NOT_IN_GOOD_STANDING:
        return RegistryResult.MISMATCH.value, f"registration_{status}"

    return RegistryResult.VERIFIED.value, None
