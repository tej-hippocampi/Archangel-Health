"""India — National Medical Commission, Indian Medical Register.

The public IMR page at nmc.org.in drives a JSON search endpoint, which is what
we call rather than parsing the page around it. It answers by registration
number, optionally narrowed by year, and returns DataTables-style positional
rows:

    [serial, year, registrationNo, stateMedicalCouncil, name, fatherName, html]

Two things shape everything below.

A registration number is only unique WITHIN a state council — searching
"45678" returns a Maharashtra doctor registered in 1981 and a Tamil Nadu
doctor registered in 1989, both correct. So the council is part of the key,
and a bare number that matches several rows is not an identification.

The IMR lags the state councils and says so on its own page. A doctor who is
not in it may be perfectly well registered. That is why this registry is not
``authoritative`` in the config: ``dispatch`` turns a miss here into
INCONCLUSIVE and routes it to document review, never a rejection.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from asclepius import credentialing
from asclepius.registry.adapters import _http

log = logging.getLogger("asclepius.registry.nmc_in")

SEARCH_URL = "https://www.nmc.org.in/MCIRest/open/getPaginatedData"
SERVICE = "getPaginatedDoctor"

#: Positional columns in a result row.
_COL_YEAR = 1
_COL_REG_NO = 2
_COL_COUNCIL = 3
_COL_NAME = 4
_COL_FATHER = 5

#: More than this and the number is not identifying on its own; we still
#: return them so an admin can see the ambiguity, but we do not fetch forever.
_MAX_ROWS = 25


def _row_to_record(row: List[Any]) -> Dict[str, Any]:
    def at(i: int) -> str:
        try:
            value = row[i]
        except IndexError:
            return ""
        return "" if value is None else str(value).strip()

    return {
        "full_name": " ".join(at(_COL_NAME).split()),
        "father_name": " ".join(at(_COL_FATHER).split()),
        "registration_number": at(_COL_REG_NO),
        "council": at(_COL_COUNCIL),
        "registration_year": at(_COL_YEAR),
    }


def fetch(
    identifier: str, *, extras: Optional[Dict[str, Any]] = None,
    timeout: float = _http.DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """Look up a registration number in the IMR.

    Returns every row the number matches (councils reuse numbers); ``match``
    below decides whether they identify this doctor.
    """
    extras = extras or {}
    number = (identifier or "").strip()
    if not number:
        return _http.not_found()

    params = {
        "service": SERVICE,
        "draw": "1",
        "start": "0",
        "length": str(_MAX_ROWS),
        "registrationNo": number,
    }
    year = str(extras.get("registrationYear") or "").strip()
    if year.isdigit():
        params["year"] = year

    try:
        response = _http.request(
            "GET", SEARCH_URL, params=params, timeout=timeout,
            headers={"Accept": "application/json"},
        )
    except Exception as exc:  # noqa: BLE001
        log.info("[registry:IN] request failed: %s", exc)
        return _http.unavailable("request_failed")

    if response.status_code != 200:
        return _http.unavailable(f"http_{response.status_code}")

    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 - an HTML error page where JSON was promised
        return _http.unavailable("unparseable_response")

    rows = payload.get("data")
    if not isinstance(rows, list):
        # The endpoint answered but not in the shape we know: treat as a
        # failure to check, never as an absence of the doctor.
        return _http.unavailable("unexpected_response_shape")
    if not rows:
        return _http.not_found()

    records = [_row_to_record(r) for r in rows if isinstance(r, list)]
    if not records:
        return _http.unavailable("unexpected_row_shape")

    return _http.found({
        "matches": records,
        "match_count": len(records),
        # Convenience for the default serializers / admin dossier.
        "full_name": records[0]["full_name"],
        "registration_number": records[0]["registration_number"],
        "council": records[0]["council"],
        "registration_year": records[0]["registration_year"],
    })


def _council_matches(claimed: str, registry_value: str) -> bool:
    """Loose containment both ways: the picker says "Maharashtra Medical
    Council" and the register says "Maharashtra Medical Council", but a
    doctor may pick the pre-2020 MCI entry, and punctuation drifts."""
    a = " ".join((claimed or "").lower().replace("&", "and").split())
    b = " ".join((registry_value or "").lower().replace("&", "and").split())
    if not a or not b:
        return False
    a_key = a.replace("medical council", "").replace("council of medical registration", "").strip()
    b_key = b.replace("medical council", "").replace("council of medical registration", "").strip()
    return a == b or (bool(a_key) and (a_key in b or b_key in a))


# Honorifics and parenthetical asides the register carries inside the name
# field: "KUMAWAT, (MISS) SUMAN", "Resmi Pal ( Mrs. Kundu )", "Dr J R Wambwa".
_HONORIFICS = {"dr", "prof", "mr", "mrs", "miss", "ms", "smt", "shri", "sri", "md"}


def _name_tokens(name: str) -> set:
    """Significant name tokens, order-independent.

    Indian register entries have no single convention: "Purushothaman,
    Munuswamy" puts the surname first, "SINGH PRADEEP KUMAR" puts it first
    without a comma, "Anoopkumar Prakash" puts it last, and married names
    arrive as a parenthetical. Taking the last token as the family name — the
    NPPES rule — is wrong about half the time here, so compare the set of
    tokens instead of their positions.

    Single letters go: "KRISHNA C" is Krishna with an initial, and matching on
    "C" would match everybody.
    """
    import re as _re

    cleaned = _re.sub(r"\(([^)]*)\)", r" \1 ", name or "")   # unwrap parentheticals
    cleaned = _re.sub(r"[^\w\s]", " ", cleaned)               # commas, dots, hyphens
    tokens = set()
    for raw in cleaned.split():
        token = credentialing._normalize_family_name(raw)  # noqa: SLF001 - accent folding
        if len(token) >= 3 and token not in _HONORIFICS:
            tokens.add(token)
    return tokens


def match(
    record: Dict[str, Any], family_name: str, extras: Optional[Dict[str, Any]] = None
) -> Tuple[str, Optional[str]]:
    """Decide whether the rows this number returned identify this doctor.

    A registration number is only unique within a council, and the IMR's
    search matches loosely, so one number routinely returns a dozen unrelated
    doctors. Identification is (number + council + a name that overlaps), and
    anything ambiguous goes to a human rather than being guessed at.
    """
    from asclepius.registry.dispatch import RegistryResult

    extras = extras or {}
    matches = record.get("matches") or []
    if not matches:
        return RegistryResult.INCONCLUSIVE.value, "no_rows_to_compare"

    claimed = _name_tokens(family_name)
    if not claimed:
        return RegistryResult.MISMATCH.value, "no_family_name_to_compare"

    council = str(extras.get("stateCouncil") or "").strip()
    if council:
        narrowed = [m for m in matches if _council_matches(council, m.get("council", ""))]
        if not narrowed:
            # The number exists, but not under the council the doctor named:
            # a typo, or the wrong council picked from the list. Human.
            return (
                RegistryResult.MISMATCH.value,
                "registration_found_under_a_different_council",
            )
    else:
        narrowed = list(matches)

    year = str(extras.get("registrationYear") or "").strip()
    if year:
        by_year = [m for m in narrowed if str(m.get("registration_year") or "").strip() == year]
        if by_year:
            narrowed = by_year

    hits = [m for m in narrowed if claimed & _name_tokens(m.get("full_name") or "")]

    if len(hits) == 1:
        return RegistryResult.VERIFIED.value, None
    if len(hits) > 1:
        # Two registrants under one council whose names both overlap: common
        # surnames make this reachable, and it is not an identification.
        return (
            RegistryResult.MISMATCH.value,
            "several_registrants_match_this_name_and_number",
        )

    # No name overlap. A review flag, never a rejection: transliteration,
    # married names and initials-only entries all land here legitimately.
    if len(narrowed) > 1:
        return (
            RegistryResult.MISMATCH.value,
            "number_matches_several_councils_none_by_this_name",
        )
    return RegistryResult.MISMATCH.value, "family_name_mismatch_possible_transliteration"
