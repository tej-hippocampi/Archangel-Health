"""Physician credentialing signals (PRD B, Phase 1).

Pure signal computation — NPI verification against the NPPES registry and
email-domain classification. This module never imports the store; callers
persist results (``store.set_npi_result`` etc.). That keeps every function
directly testable and keeps the network layer in exactly one place.

Design rule carried through everything here: a check that can gate has THREE
outcomes, not two. "Could not determine" (UNAVAILABLE) is a first-class result
that routes to human review — it must never collapse into a definitive
negative. On launch day NPPES may rate-limit us; treating "we could not check"
as "this physician does not exist" would reject real doctors at the exact
moment we cannot afford to.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import unicodedata
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

import httpx

from asclepius import plausibility

log = logging.getLogger("asclepius.credentialing")

# NPPES NPI Registry API v2.1 — free, no key, public.
NPPES = "https://npiregistry.cms.hhs.gov/api/"
NPPES_VERSION = "2.1"
DEFAULT_TIMEOUT = 6.0


def _timeout_for(timeout: float) -> Any:
    """A per-phase httpx timeout budget.

    A bare ``timeout=6.0`` applies 6s to EACH of connect/read/write/pool, so a
    single hung NPPES request can hold a worker for ~20s. These phases bound
    the true worst case to ~9s while leaving a slow-but-alive registry room to
    answer (read is the phase that legitimately takes time).
    """
    try:
        scale = max(0.05, float(timeout) / DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        scale = 1.0
    return httpx.Timeout(
        connect=2.0 * scale, read=4.0 * scale, write=2.0 * scale, pool=1.0 * scale
    )


def _npi_log_id(npi: str) -> str:
    """A short stable hash of an NPI for log lines (L1) — groups retries of the
    same number without printing an identifier tied to a named physician."""
    return hashlib.sha256((npi or "").encode("utf-8")).hexdigest()[:8]

# How long a definitive NPPES answer stays fresh (registry data moves slowly;
# launch traffic re-checks the same NPIs).
NPI_CACHE_DAYS = 30


class NpiResult(str, Enum):
    VERIFIED = "verified"        # found, active, individual, family name matches
    MISMATCH = "mismatch"        # found, but does not corroborate the signup
    NOT_FOUND = "not_found"      # definitively no registry record (or bad checksum)
    UNAVAILABLE = "unavailable"  # we could NOT check — network, timeout, rate limit


# ─── NPI format / checksum ────────────────────────────────────────────────────
# An NPI is 10 digits whose check digit is computed with the Luhn algorithm
# over the card-issuer prefix "80840" + the 9-digit base. Equivalently: Luhn
# over the 15-digit string "80840" + NPI must validate. A malformed NPI is a
# local NOT_FOUND — no network call, which drops typo traffic before NPPES.

def clean_npi(raw: str) -> str:
    """Strip whitespace, dashes and dots people paste in with the number."""
    return re.sub(r"[\s\-\.]", "", (raw or ""))


def npi_checksum_ok(npi: str) -> bool:
    if not re.fullmatch(r"\d{10}", npi or ""):
        return False
    total = 0
    for i, ch in enumerate(reversed("80840" + npi)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# ─── Family-name normalization ───────────────────────────────────────────────
# Compare the family name ONLY: given names diverge legitimately (Bob/Robert).
# Case-folded, punctuation stripped, generational/credential suffixes dropped.
_NAME_SUFFIXES = {
    "jr", "sr", "ii", "iii", "iv", "v",
    "md", "do", "phd", "mbbs", "dds", "dmd", "np", "pa", "rn", "esq",
    "faap", "facs", "facp", "mph", "msc", "ms", "mba", "frcs", "frcp",
}

# B-5.2: several suffix tokens are also real surnames — "Do" is a common
# Vietnamese family name, "Pa" and "V" occur too. Stripping them
# unconditionally reduced the name to '' and produced a guaranteed MISMATCH
# for those physicians. These are dropped only when something else survives.
_SUFFIXES_THAT_ARE_ALSO_SURNAMES = {"do", "pa", "v", "ms", "iv", "ii"}


def _strip_accents(s: str) -> str:
    """NFKD-fold and drop combining marks.

    NPPES stores names ASCII-folded, so without this every physician with an
    accented surname mismatched BY CONSTRUCTION: the old ``[^a-z\\s\\-']``
    filter turned each accented letter into a space, making "Muñoz" -> "mu oz"
    and "García" -> "garc a", neither of which matches the registry's "MUNOZ"
    / "GARCIA".
    """
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", s or "")
        if not unicodedata.combining(ch)
    )


def _normalize_family_name(name: str) -> str:
    s = _strip_accents(name).casefold()
    # ß and similar have no combining-mark decomposition; NFKD leaves them.
    s = s.replace("ß", "ss").replace("ø", "o").replace("đ", "d").replace("ł", "l")
    s = re.sub(r"[^a-z\s\-']", " ", s)
    s = s.replace("-", " ").replace("'", "")
    tokens = [t for t in s.split() if t]
    kept = [t for t in tokens if t not in _NAME_SUFFIXES]
    if not kept:
        # Everything looked like a suffix. If the name IS a suffix-shaped
        # surname ("Do"), keep it rather than returning '' — an empty
        # normalization is an automatic MISMATCH.
        kept = [t for t in tokens if t in _SUFFIXES_THAT_ARE_ALSO_SURNAMES] or tokens
    return " ".join(kept)


def family_name_from_legal_name(legal_name: str) -> str:
    """Extract the family name from a free-text legal-name field.

    ``legal_name.split()[-1]`` was wrong for the single most likely thing a
    physician types: "Jane Doe, MD" yielded "MD" and "John Smith Jr" yielded
    "Jr", both normalizing to '' and mismatching by construction. Drop a
    trailing comma-clause of credentials, then take the last token that is not
    a suffix.
    """
    s = (legal_name or "").strip()
    if not s:
        return ""
    # "Jane Doe, MD, FACP" -> "Jane Doe"; but keep "Doe, Jane" usable.
    head = s.split(",")[0].strip()
    if head:
        s = head
    tokens = [t for t in re.split(r"\s+", s) if t]
    # Drop leading honorifics so a one-word name isn't read as the title.
    while len(tokens) > 1 and tokens[0].casefold().strip(".") in (
            "dr", "doctor", "prof", "professor", "mr", "ms", "mrs", "mx"):
        tokens = tokens[1:]
    for token in reversed(tokens):
        cleaned = re.sub(r"[^\w\-']", "", token, flags=re.UNICODE)
        if not cleaned:
            continue
        low = _strip_accents(cleaned).casefold()
        if low in _NAME_SUFFIXES and len(tokens) > 1:
            # Ambiguous tokens ("Do", "Pa", "V") are real surnames as often as
            # credentials. Decide by arity: with just a given name and this
            # token, this token IS the surname ("Minh Do"); with three or more
            # there is already a surname ahead of it ("Jane Doe DO").
            if low in _SUFFIXES_THAT_ARE_ALSO_SURNAMES and len(tokens) <= 2:
                return cleaned
            continue
        return cleaned
    return tokens[-1] if tokens else ""


def has_comparable_family_name(name: str) -> bool:
    """Is there anything left to compare after normalization?

    "The names differ" and "we had nothing to compare" are different facts and
    an admin working the queue has to be able to tell them apart. Public so
    the international registry adapters can draw the same distinction without
    reaching into this module's internals.
    """
    return bool(_normalize_family_name(name))


def family_names_match(claimed: str, registry: str) -> bool:
    a, b = _normalize_family_name(claimed), _normalize_family_name(registry)
    if not a or not b:
        return False
    if a == b:
        return True
    # "smith jones" vs "smithjones", "de la cruz" vs "delacruz"
    return a.replace(" ", "") == b.replace(" ", "")


# ─── NPPES network layer ──────────────────────────────────────────────────────
def fetch_npi_record(npi: str, timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """One NPPES lookup. Returns ``{"status", "record", "reason"}`` where
    status is ``found`` | ``not_found`` | ``unavailable``.

    Every failure mode we cannot positively interpret is ``unavailable`` —
    the conservative branch that routes to a human — never ``not_found``.
    """
    try:
        resp = httpx.get(
            NPPES,
            params={"version": NPPES_VERSION, "number": npi},
            timeout=_timeout_for(timeout),
        )
    except Exception as exc:  # timeout, DNS, connection reset, TLS…
        # L1: the NPI is public registry data, but a WARNING line correlates it
        # to a named physician's signup in the log stream. Hash it; the prefix
        # is enough to group retries of the same number.
        log.debug("[credentialing] NPPES unreachable for npi#%s: %s",
                  _npi_log_id(npi), type(exc).__name__)
        return {"status": "unavailable", "record": None,
                "reason": f"network_error:{type(exc).__name__}"}

    if resp.status_code == 429:
        return {"status": "unavailable", "record": None, "reason": "rate_limited"}
    if resp.status_code != 200:
        return {"status": "unavailable", "record": None,
                "reason": f"http_{resp.status_code}"}

    try:
        data = resp.json()
    except Exception:
        return {"status": "unavailable", "record": None, "reason": "bad_json"}

    # We pre-validate the checksum, so an API-level error object means NPPES
    # rejected something about the request itself — "could not determine".
    if data.get("Errors") or data.get("errors"):
        return {"status": "unavailable", "record": None, "reason": "api_error"}

    # B-1.2: ``result_count`` ABSENT is not ``result_count`` ZERO. A 200 we
    # cannot parse — a schema change, a Cloudflare JSON error page, a proxy
    # interstitial, a captive portal — is "could not determine", NOT "this
    # physician does not exist". The difference is permanent: not_found stamps
    # npi_checked_at, which makes the row cache-eligible and suppresses the
    # retry path, so one bad response shape would reject a real physician for
    # 30 days with no further network call.
    if not isinstance(data, dict) or "result_count" not in data:
        return {"status": "unavailable", "record": None, "reason": "unrecognized_payload"}
    if not data["result_count"]:
        return {"status": "not_found", "record": None, "reason": None}
    # result_count claims a hit — the results array must actually carry one.
    # Trusting the count alone raised IndexError/KeyError/ValueError on three
    # real malformed shapes, breaking this function's three-state contract.
    results = data.get("results")
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        return {"status": "unavailable", "record": None, "reason": "unrecognized_payload"}

    # The protected-attribute strip happens HERE, not at the trim: this is the single point
    # every consumer of an NPPES record passes through, so no caller downstream can see
    # ``basic.sex`` even if it goes looking for it (PRD C §2).
    return {"status": "found", "record": _strip_protected_attributes(results[0]),
            "reason": None}


# ─── Protected-attribute boundary (PRD C §2) ─────────────────────────────────
# The free NPPES API returns ``basic.sex``. It must never reach a feature vector, a log, or
# the database — this is a live adverse-impact hazard sitting in an API response, and it is
# cheapest to close at the one point every path goes through.
#
# ``_trim_record`` is an allow-list, so ``sex`` did not survive into ``npi_payload_json`` by
# accident of construction. That is not the same thing as a boundary: nothing asserted it,
# nothing tested it, and the RAW record — which does carry it — is handed whole to
# ``_basic``/``_registry_family_names`` and to anything a future edit adds. One widening of
# the trim to ``dict(record["basic"])`` reintroduces a protected attribute silently. So the
# strip happens here, on the way OUT of the network layer, and the record no caller can see
# it in is the only record that exists downstream.
_PROTECTED_NPPES_KEYS = ("sex", "gender", "date_of_birth", "dob", "birth_date", "name_prefix")


def _strip_protected_attributes(record: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Drop protected attributes from an NPPES record, at the ingest boundary."""
    if not isinstance(record, dict):
        return record
    out = {k: v for k, v in record.items() if k not in _PROTECTED_NPPES_KEYS}
    basic = out.get("basic")
    if isinstance(basic, dict):
        out["basic"] = {k: v for k, v in basic.items() if k not in _PROTECTED_NPPES_KEYS}
    return out


def assert_no_protected_attributes(payload: Any) -> None:
    """Recursively assert a payload carries no protected attribute. Used by the audit tests
    against every shape that reaches the database or a log line."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in _PROTECTED_NPPES_KEYS:
                raise AssertionError(
                    f"protected attribute {key!r} reached a persisted/serialized payload")
            assert_no_protected_attributes(value)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            assert_no_protected_attributes(item)


def _basic(record: Dict[str, Any]) -> Dict[str, Any]:
    """The 'basic' section of a RAW NPPES record, or the record itself when it
    is already a trimmed record (the shape we persist and serve from the
    30-day cache). Evaluation must accept both shapes — the cache round-trip
    re-verifies a trimmed record for a NEW signup's name."""
    return record.get("basic") or record


def _trim_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """The subset of an NPPES record the dossier needs (kept small on purpose —
    this is what lands in ``users.npi_payload_json``). Idempotent: a record
    that is already trimmed passes through unchanged."""
    if "basic" not in record:
        return dict(record)
    basic = record.get("basic") or {}
    primary_tax: Optional[Dict[str, Any]] = None
    for tax in record.get("taxonomies") or []:
        if tax.get("primary"):
            primary_tax = tax
            break
    if primary_tax is None and (record.get("taxonomies") or []):
        primary_tax = record["taxonomies"][0]
    location = None
    for addr in record.get("addresses") or []:
        if (addr.get("address_purpose") or "").upper() == "LOCATION":
            location = {"city": addr.get("city"), "state": addr.get("state")}
            break
    return {
        "number": record.get("number"),
        "enumeration_type": record.get("enumeration_type"),
        "status": basic.get("status"),
        "first_name": basic.get("first_name"),
        "last_name": basic.get("last_name"),
        "credential": basic.get("credential"),
        "enumeration_date": basic.get("enumeration_date"),
        "taxonomy": (
            {
                "code": primary_tax.get("code"),
                "desc": primary_tax.get("desc"),
                "state": primary_tax.get("state"),
                "license": primary_tax.get("license"),
            }
            if primary_tax
            else None
        ),
        "location": location,
        # L3: keep the aliases. _trim_record output IS what the 30-day cache
        # serves, so dropping other_names made a CACHED record mismatch a
        # physician whose alias a FRESH fetch would have matched — the same
        # NPI giving two different answers depending on cache state. Names
        # only; no address, no phone.
        "other_names": [
            {"last_name": o.get("last_name")}
            for o in (record.get("other_names") or [])
            if isinstance(o, dict) and o.get("last_name")
        ],
    }


def _registry_family_names(record: Dict[str, Any]) -> List[str]:
    names = [_basic(record).get("last_name") or ""]
    for other in record.get("other_names") or []:
        if other.get("last_name"):
            names.append(other["last_name"])
    return [n for n in names if n]


def verify_npi(
    npi: str,
    family_name: str,
    timeout: float = DEFAULT_TIMEOUT,
    cached: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Check an NPI against the NPPES registry.

    Returns ``{"result", "npi", "reason", "record", "from_cache"}`` where
    ``result`` is an ``NpiResult`` value. UNAVAILABLE is a distinct result and
    must never collapse into NOT_FOUND — it routes to manual review and a
    retry, never to a rejection.

    ``cached`` — a previously fetched ``fetch_npi_record``-shaped dict (the
    store keeps definitive answers for ``NPI_CACHE_DAYS``). When present the
    network is skipped entirely and the name match is recomputed for THIS
    signup, because a cache keyed by NPI alone cannot cache the verdict.
    """
    number = clean_npi(npi)
    if not npi_checksum_ok(number):
        return {
            "result": NpiResult.NOT_FOUND.value,
            "npi": number,
            "reason": "invalid_format_or_checksum",
            "record": None,
            "from_cache": False,
        }

    fetched = cached if cached is not None else fetch_npi_record(number, timeout=timeout)
    from_cache = cached is not None
    status = fetched.get("status")

    if status == "unavailable":
        return {
            "result": NpiResult.UNAVAILABLE.value,
            "npi": number,
            "reason": fetched.get("reason") or "unavailable",
            "record": None,
            "from_cache": from_cache,
        }
    if status == "not_found":
        return {
            "result": NpiResult.NOT_FOUND.value,
            "npi": number,
            "reason": "no_registry_record",
            "record": None,
            "from_cache": from_cache,
        }

    record = fetched.get("record") or {}
    trimmed = _trim_record(record)

    # An NPI-2 is an *organization*, not an individual clinician — it exists,
    # but it does not corroborate a person. Review flag, not a rejection.
    if (record.get("enumeration_type") or "").upper() == "NPI-2":
        return {
            "result": NpiResult.MISMATCH.value,
            "npi": number,
            "reason": "organizational_npi",
            "record": trimmed,
            "from_cache": from_cache,
        }

    if (_basic(record).get("status") or "").upper() != "A":
        return {
            "result": NpiResult.MISMATCH.value,
            "npi": number,
            "reason": "npi_not_active",
            "record": trimmed,
            "from_cache": from_cache,
        }

    registry_names = _registry_family_names(record)
    matched = any(family_names_match(family_name, reg) for reg in registry_names)
    if not matched:
        # Three distinct facts, not one (L2). "The names differ" is evidence;
        # "we had nothing to compare" is not, and an admin working the queue
        # must be able to tell them apart. All three are review flags, never
        # rejections.
        if not _normalize_family_name(family_name):
            reason = "no_family_name_to_compare"
        elif not registry_names:
            reason = "registry_record_has_no_name"
        else:
            reason = "family_name_mismatch"
        return {
            "result": NpiResult.MISMATCH.value,
            "npi": number,
            "reason": reason,
            "record": trimmed,
            "from_cache": from_cache,
        }

    return {
        "result": NpiResult.VERIFIED.value,
        "npi": number,
        "reason": None,
        "record": trimmed,
        "from_cache": from_cache,
    }


# ─── Email domain classification ─────────────────────────────────────────────
# academic | business | consumer. A consumer domain is NOT disqualifying —
# plenty of practising physicians sign up with Gmail. It contributes one
# negative weight to the tier score and nothing more; treating it as a gate
# would reject a large share of real doctors on launch day.

_ACADEMIC_TLD_SUFFIXES = (
    ".edu", ".ac.uk", ".edu.au", ".ac.nz", ".ac.za", ".ac.in", ".ac.jp",
    ".edu.sg", ".edu.hk", ".edu.cn", ".ac.ir", ".edu.my",
)

# Academic medical centers whose domains are not under an academic TLD.
ACADEMIC_MEDICAL_DOMAINS = {
    "mgb.org", "partners.org",               # Mass General Brigham
    "bwh.harvard.edu", "mgh.harvard.edu",
    "ccf.org", "clevelandclinic.org",
    "mayo.edu",
    "jhmi.edu", "jhu.edu",
    "mountsinai.org",
    "nyulangone.org",
    "cshs.org", "cedars-sinai.org",
    "stanfordhealthcare.org",
    "uclahealth.org", "mednet.ucla.edu",
    "pennmedicine.upenn.edu", "uphs.upenn.edu",
    "mdanderson.org",
    "dukehealth.org", "duke.edu",
    "uwmedicine.org",
    "vumc.org",
    "wustl.edu", "bjc.org",
    "uchicagomedicine.org", "uchospitals.edu",
    "michiganmedicine.org", "med.umich.edu",
    "osumc.edu",
    "emoryhealthcare.org",
    "utsouthwestern.edu",
    "ucsf.edu", "ucsfhealth.org",
    "columbia.edu", "cumc.columbia.edu",
    "weillcornell.org", "nyp.org",
    "bcm.edu",
    "childrens.harvard.edu", "chop.edu",
}

# Known health-system (employer) domains — business, but weighted higher by
# the tier scorer because they corroborate current clinical employment.
HEALTH_SYSTEM_DOMAINS = {
    "kp.org",                    # Kaiser Permanente
    "hcahealthcare.com",
    "commonspirit.org", "dignityhealth.org",
    "ascension.org",
    "providence.org",
    "trinity-health.org",
    "advocatehealth.org", "aah.org",
    "sutterhealth.org",
    "intermountain.org", "imail.org",
    "geisinger.edu", "geisinger.org",
    "northwell.edu",
    "banner-health.com", "bannerhealth.com",
    "memorialhermann.org",
    "houstonmethodist.org",
    "baylorscottandwhite.com", "bswhealth.org",
    "sentara.com",
    "ochsner.org",
    "upmc.edu",
    "atriumhealth.org",
    "novanthealth.org",
    "adventhealth.com",
    "corewellhealth.org",
    "va.gov",                    # Veterans Health Administration
}

CONSUMER_DOMAINS = {
    "gmail.com", "googlemail.com",
    "yahoo.com", "ymail.com", "rocketmail.com",
    "outlook.com", "hotmail.com", "live.com", "msn.com",
    "icloud.com", "me.com", "mac.com",
    "aol.com",
    "proton.me", "protonmail.com", "pm.me",
    "mail.com", "gmx.com", "gmx.net", "zoho.com", "hey.com",
    "comcast.net", "verizon.net", "att.net", "sbcglobal.net",
    "cox.net", "charter.net", "bellsouth.net", "earthlink.net",
    "yandex.com", "mail.ru", "qq.com", "163.com", "126.com",
    "naver.com", "daum.net", "web.de", "orange.fr", "free.fr",
    "btinternet.com", "sky.com", "rediffmail.com",
}
# Country-code variants (yahoo.co.uk, hotmail.fr, outlook.com.au…)
_CONSUMER_BASES = ("gmail.", "yahoo.", "hotmail.", "outlook.", "live.", "icloud.", "aol.")


def email_domain(email: str) -> str:
    addr = (email or "").strip().casefold()
    if "@" not in addr:
        return ""
    domain = addr.rsplit("@", 1)[1].strip().rstrip(".")
    # L4: an internationalized domain arrives punycode-encoded (xn--…) and is
    # therefore classified as business. That is harmless — this is one weight
    # in a score, never a gate — and decoding is deliberately NOT done here,
    # because unicode-folding a domain for comparison invites homograph
    # confusion in exchange for a few points of scoring nuance.
    return domain


def is_health_system_domain(domain: str) -> bool:
    return (domain or "").strip().casefold() in HEALTH_SYSTEM_DOMAINS


def classify_email_domain(email: str) -> str:
    """academic | business | consumer.

    Not a gate — one weight in the tier score. Unparseable input classifies
    as consumer (the lowest, non-disqualifying weight) rather than erroring:
    signup must never fail on a classification helper.
    """
    domain = email_domain(email)
    if not domain:
        return "consumer"
    if domain.endswith(_ACADEMIC_TLD_SUFFIXES) or domain in ACADEMIC_MEDICAL_DOMAINS:
        return "academic"
    if domain in CONSUMER_DOMAINS or domain.startswith(_CONSUMER_BASES):
        return "consumer"
    return "business"


# ─── CV upload + best-effort parse (Phase 2) ─────────────────────────────────
# Storage reuses the content-addressed blob primitives in ``assets.py``
# directly (sha256 + atomic fsync'd write + dedupe). We deliberately do NOT go
# through ``assets.process_upload`` — that path rasterizes a PDF to a page-1
# PNG, which would destroy the CV's text layer and the rest of its pages. The
# raw document is the evidence the admin opens; nothing about it is exported.
#
# Every parsed field is a SUGGESTION shown to the admin, never an
# auto-approval input. A parsed CV is evidence for a human, not a credential —
# and failure anywhere in here is non-fatal: signup completes, the field is
# empty, the admin sees the raw file.

CV_ACCEPTED_MIMES = ("application/pdf", "text/plain")
CV_MAX_BYTES = 10 * 1024 * 1024  # a 10 MB cap comfortably fits any real CV


class CvUploadError(ValueError):
    """Bad CV upload (mime/size). Caller maps this to a 4xx, not a 500."""


def sniff_cv_mime(data: bytes) -> Optional[str]:
    """The mime implied by the BYTES (B-5.5).

    The declared Content-Type is attacker-controlled, and the stored blob is
    later served ``inline`` from the app origin to an admin whose bearer token
    lives in localStorage. Trusting the header on the way in and sniffing on
    the way out is the combination that turns an upload into stored XSS, so
    the bytes decide. Returns None when the content matches nothing we accept.
    """
    if not data:
        return None
    if data[:5] == b"%PDF-":
        return "application/pdf"
    # Anything carrying a known-executable/markup signature is rejected
    # outright rather than being allowed through as "text".
    for magic in (b"<?xml", b"<!DOCTYPE", b"<html", b"<HTML", b"<svg", b"<SVG",
                  b"PK\x03\x04", b"\x7fELF", b"MZ", b"\x89PNG", b"GIF8",
                  b"\xff\xd8\xff", b"%!PS"):
        if data[:len(magic)] == magic:
            return None
    head = data[:4096]
    if b"\x00" in head:            # NUL bytes: not a text document
        return None
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        # a multi-byte char may straddle the cut; only fail on a real problem
        try:
            data[:4096 - 4].decode("utf-8")
        except UnicodeDecodeError:
            return None
    return "text/plain"


def _scrub_pdf_metadata(data: bytes) -> bytes:
    """Drop /Info and XMP from a PDF before storage (B-5.6).

    ``process_upload`` — the metadata-stripping path — is deliberately bypassed
    for CVs because it rasterizes page 1 and would destroy the text layer and
    every later page. That left PDF XMP/DocInfo (author, producer, sometimes
    original filesystem paths) retained verbatim. Best-effort: a PDF that
    cannot be rewritten is stored as-is rather than refused, since the CV is
    optional evidence and admin-only.
    """
    try:
        from PyPDF2 import PdfReader, PdfWriter
        reader = PdfReader(io.BytesIO(data))
        if getattr(reader, "is_encrypted", False):
            return data
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        writer.add_metadata({})            # empty /Info
        try:                               # drop the XMP stream when present
            writer._root_object.pop("/Metadata", None)
        except Exception:
            pass
        out = io.BytesIO()
        writer.write(out)
        scrubbed = out.getvalue()
        return scrubbed if scrubbed.startswith(b"%PDF-") else data
    except Exception:
        log.debug("[credentialing] PDF metadata scrub skipped", exc_info=True)
        return data


def store_cv(data: bytes, mime: str) -> Dict[str, Any]:
    """Validate and persist a raw CV blob, content-addressed. Returns
    ``{"sha256", "mime", "byte_size"}`` for ``users.cv_asset_sha``.

    The stored mime is the SNIFFED one, never the declared one.
    """
    from asclepius import assets

    declared = (mime or "").strip().lower().split(";")[0]
    if not data:
        raise CvUploadError("empty upload")
    if len(data) > CV_MAX_BYTES:
        raise CvUploadError(f"CV is {len(data)} bytes; max is {CV_MAX_BYTES}")
    # The declared type is a UX gate only: someone attaching a .docx should be
    # told we take PDF or text, not have it silently accepted because the
    # first bytes happen to decode. Browsers do send octet-stream for real
    # PDFs, so that one is allowed through to the sniffer.
    if declared and declared not in CV_ACCEPTED_MIMES and declared != "application/octet-stream":
        raise CvUploadError(
            f"unsupported CV type {declared!r}; accepted: {', '.join(CV_ACCEPTED_MIMES)}")
    # The BYTES are the authority for what actually gets stored and served.
    sniffed = sniff_cv_mime(data)
    if sniffed is None or sniffed not in CV_ACCEPTED_MIMES:
        raise CvUploadError(
            f"unsupported CV content; accepted: {', '.join(CV_ACCEPTED_MIMES)}")
    if declared in CV_ACCEPTED_MIMES and declared != sniffed:
        log.info("[credentialing] CV declared %s but is %s; trusting the bytes",
                 declared, sniffed)
    if sniffed == "application/pdf":
        data = _scrub_pdf_metadata(data)
    sha = assets._sha256(data)
    assets._write_blob(sha, data)
    return {"sha256": sha, "mime": sniffed, "byte_size": len(data)}


def _pdf_text(data: bytes) -> str:
    """Text layer of a PDF: pdfminer first (best quality), PyPDF2 fallback,
    then OCR of the first pages when there is no text layer at all (scanned
    CVs) and tesseract is actually present. Best-effort throughout."""
    text = ""
    try:
        from pdfminer.high_level import extract_text as _pm_extract
        text = _pm_extract(io.BytesIO(data)) or ""
    except Exception:
        text = ""
    if len(text.strip()) < 40:
        try:
            from PyPDF2 import PdfReader
            reader = PdfReader(io.BytesIO(data))
            text = "\n".join((p.extract_text() or "") for p in reader.pages)
        except Exception:
            pass
    if len(text.strip()) < 40:
        text = _ocr_pdf_pages(data) or text
    return text or ""


def _ocr_pdf_pages(data: bytes, max_pages: int = 5) -> str:
    from asclepius.dicom_deid import ocr_available
    if not ocr_available():
        return ""
    try:
        import pytesseract
        from pdf2image import convert_from_bytes
        pages = convert_from_bytes(data, dpi=200, first_page=1, last_page=max_pages)
        return "\n".join(pytesseract.image_to_string(p) for p in pages)
    except Exception:
        return ""


def extract_cv_text(data: bytes, mime: str) -> str:
    mime = (mime or "").strip().lower().split(";")[0]
    if mime == "text/plain":
        try:
            return data.decode("utf-8", errors="replace")
        except Exception:
            return ""
    if mime == "application/pdf":
        return _pdf_text(data)
    return ""


_INSTITUTION_KEYWORDS = (
    "university", "medical school", "school of medicine", "college of medicine",
    "medical center", "medical centre", "hospital", "residency", "fellowship",
    "internship", "health system", "clinic",
)
_BOARD_LINE = re.compile(
    r"(?:board[\s\-]certified(?:\s+in)?|diplomate,?\s+(?:of\s+)?(?:the\s+)?american board of)"
    r"[\s:]*([A-Za-z][A-Za-z &,/\-]{2,60})",
    re.IGNORECASE,
)
_BOARD_ACRONYM = re.compile(r"\b(ABIM|ABFM|ABEM|ABOG|ABA|ABP|ABS|ABR|ABPN|ABOS|ABU|ABD)\b")
_YEARS_EXPLICIT = re.compile(
    r"(\d{1,2})\+?\s*years?\s+(?:of\s+)?(?:clinical\s+)?(?:experience|practice)",
    re.IGNORECASE,
)
_YEAR_RANGE = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\s*[–\-—]\s*(19[5-9]\d|20[0-4]\d|present|current)\b",
                         re.IGNORECASE)
_TRAINING_WORDS = ("residency", "fellowship", "internship", "resident", "fellow")
_CITATION_HINT = re.compile(r"\b(?:doi:|pmid[:\s]|et al\.?)", re.IGNORECASE)


def _parse_cv_text(text: str) -> Dict[str, Any]:
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]

    institutions: List[str] = []
    for ln in lines:
        low = ln.casefold()
        if any(k in low for k in _INSTITUTION_KEYWORDS) and 4 < len(ln) < 140:
            if ln not in institutions:
                institutions.append(ln)
        if len(institutions) >= 12:
            break

    certs: List[str] = []
    for m in _BOARD_LINE.finditer(text):
        val = m.group(1).strip(" .,;:").strip()
        if val and val not in certs:
            certs.append(val)
    for m in _BOARD_ACRONYM.finditer(text):
        if m.group(1) not in certs:
            certs.append(m.group(1))

    years: Optional[int] = None
    m = _YEARS_EXPLICIT.search(text)
    if m:
        years = min(60, int(m.group(1)))
    else:
        # end year of the latest training block -> years since then
        training_end: Optional[int] = None
        for ln in lines:
            low = ln.casefold()
            if not any(w in low for w in _TRAINING_WORDS):
                continue
            for rng in _YEAR_RANGE.finditer(ln):
                end_raw = rng.group(2).casefold()
                end = datetime.utcnow().year if end_raw in ("present", "current") \
                    else int(end_raw)
                training_end = max(training_end or 0, end)
        if training_end:
            years = max(0, datetime.utcnow().year - training_end)

    pubs = 0
    in_pub_section = False
    for ln in lines:
        low = ln.casefold()
        if low.startswith(("publications", "selected publications", "peer-reviewed")):
            in_pub_section = True
            continue
        if in_pub_section and re.match(
            r"^(education|training|experience|certifications|licens|awards|references)\b", low
        ):
            in_pub_section = False
        if _CITATION_HINT.search(ln) or (in_pub_section and re.search(r"\b(19|20)\d{2}\b", ln)):
            pubs += 1

    return {
        "institutions": institutions,
        "board_certifications": certs,
        "years_in_practice": years,
        "publications_count": pubs if pubs else None,
    }


def parse_cv(asset_sha: str, mime: str = "application/pdf") -> Dict[str, Any]:
    """Best-effort extraction from a stored CV: training institutions, board
    certifications, years in practice, publications. Never raises — a CV that
    cannot be parsed returns ``ok=False`` and the admin reviews the raw file.
    """
    try:
        from asclepius import assets
        data, _ = assets.load_asset(asset_sha)
    except Exception as exc:
        return {"ok": False, "asset_sha": asset_sha, "reason": f"load_failed:{type(exc).__name__}",
                "institutions": [], "board_certifications": [],
                "years_in_practice": None, "publications_count": None}
    try:
        text = extract_cv_text(data, mime)
        if len(text.strip()) < 40:
            return {"ok": False, "asset_sha": asset_sha, "reason": "no_extractable_text",
                    "institutions": [], "board_certifications": [],
                    "years_in_practice": None, "publications_count": None}
        parsed = _parse_cv_text(text)
        parsed.update({
            "ok": True,
            "asset_sha": asset_sha,
            "reason": None,
            "text_chars": len(text),
            "parsed_at": datetime.utcnow().replace(microsecond=0).isoformat(),
        })
        return parsed
    except Exception as exc:  # parsing is advisory; it must never break signup
        log.exception("[credentialing] CV parse failed for %s", asset_sha[:12])
        return {"ok": False, "asset_sha": asset_sha, "reason": f"parse_failed:{type(exc).__name__}",
                "institutions": [], "board_certifications": [],
                "years_in_practice": None, "publications_count": None}


# ─── Tier scoring (Phase 3) ──────────────────────────────────────────────────
# The score PROPOSES; a human DECIDES. Nothing here assigns a tier — the admin
# approval endpoint requires an explicit tier in the request body, always.

TIER_WEIGHTS = {
    "npi_verified":        25,   # authoritative identity
    "academic_email":      10,
    "business_email":       6,
    "consumer_email":      -4,
    "health_system_email": 10,   # known health-system employer domain (§1.3: weight higher)
    "board_certified":     20,
    "years_10_plus":       20,
    "years_5_to_10":       10,
    "years_under_5":        0,
    "subspecialty_match":  10,   # NPPES taxonomy matches a specialty we serve
    "cv_parsed":            5,
    "linkedin_present":     3,
}

REVIEWER_MIN_SCORE = 70
LABELER_MIN_SCORE = 30

# NPPES taxonomy wording → the specialty registry's names. "Cardiovascular
# Disease" is how NPPES spells cardiology; hem/onc covers oncology.
_SERVED_SPECIALTY_STEMS = {
    "nephrology": ("nephrology",),
    "cardiology": ("cardiovascular", "cardiology", "cardiac electrophysiology",
                   "interventional cardiology"),
    "oncology":   ("oncology", "hematology"),
}


def _served_specialty_for_taxonomy(taxonomy_desc: str) -> Optional[str]:
    """Which served specialty (if any) an NPPES taxonomy description maps to.
    None means "could not determine" — which is never treated as a divergence."""
    low = (taxonomy_desc or "").casefold()
    if not low:
        return None
    for served, stems in _SERVED_SPECIALTY_STEMS.items():
        if any(stem in low for stem in stems):
            return served
    return None


def _json_field(user: Dict[str, Any], key: str) -> Dict[str, Any]:
    """A user-row field that may arrive as a JSON string (raw sqlite row) or an
    already-decoded dict."""
    raw = user.get(key)
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            out = json.loads(raw)
            return out if isinstance(out, dict) else {}
        except ValueError:
            return {}
    return {}


def propose_tier(user: Dict[str, Any], *, duplicate_npi: bool = False) -> Dict[str, Any]:
    """Returns ``{"score", "proposed_tier", "reasons", "blockers"}``.

        score >= 70 and npi_verified -> 'reviewer'  (senior, time-poor: they grade)
        score >= 30                  -> 'labeler'   (they do cases from scratch)
        otherwise                    -> None        (admin decides; NOT a rejection)

    Rationale, because the mapping is counter-intuitive: the HIGHER tier does
    LESS work. A 20-year attending will not sit and label cases from scratch,
    but will spend 60 seconds grading someone else's. Matching the task to the
    supply is the whole point of the two tiers.

    **``'advisor'`` is NOT on this ladder and must never be returned from here**
    (Advisor PRD §2.3). ``TIER_WEIGHTS`` reads like a ladder and the obvious next
    move is to extend it upward with a higher threshold — do not. A medical
    advisor is a negotiated relationship with equity and a signed agreement
    attached; it is not the output of an NPI check and a years-in-practice
    weight. Advisors are APPOINTED, through ``POST /api/asclepius/admin/advisors``
    (or an explicit queue approval carrying ``agreement_ref``), and the guard
    below makes a mistake here loud instead of silent.

    ``reasons`` is human-readable — an admin must see WHY a tier was proposed
    without reading the weights. ``blockers`` suppress the proposal and force
    manual review; they are review flags, never rejections.
    """
    reasons: List[str] = []
    blockers: List[str] = []
    score = 0

    npi_payload = _json_field(user, "npi_payload_json")
    npi_result = npi_payload.get("result")
    npi_record = npi_payload.get("record") or {}
    taxonomy_desc = ((npi_record.get("taxonomy") or {}).get("desc")) or ""

    npi_ok = user.get("npi_verified") == 1
    if npi_ok:
        score += TIER_WEIGHTS["npi_verified"]
        cred = npi_record.get("credential") or ""
        reasons.append(
            f"+{TIER_WEIGHTS['npi_verified']} NPI verified against NPPES"
            + (f" ({cred})" if cred else ""))
    elif npi_result == NpiResult.UNAVAILABLE.value:
        reasons.append("±0 NPI check unavailable — retry pending (not held against them)")
    elif npi_result == NpiResult.NOT_FOUND.value:
        reasons.append("±0 NPI not found in NPPES")

    # A doctor licensed outside the US earns identity the same way, from their
    # own country's registry. Same weight: an SCFHS or NMC registration is not
    # worth less than an NPI, it is just checked somewhere else. Countries
    # whose registers we cannot query score zero here and clear identity
    # through document review instead — pending, not penalized.
    registry_payload = _json_field(user, "registry_payload_json")
    registry_result = registry_payload.get("result")
    registry_ok = user.get("registry_verified") == 1
    if not npi_ok and registry_ok:
        score += TIER_WEIGHTS["npi_verified"]
        registry_name = registry_payload.get("registry") or "the national registry"
        score_label = TIER_WEIGHTS["npi_verified"]
        reasons.append(f"+{score_label} registration verified against {registry_name}")
    elif not npi_ok and registry_result in ("document_only", "queued"):
        reasons.append("±0 identity pending document review (no public registry to query)")
    elif not npi_ok and registry_result in ("inconclusive", "unavailable"):
        reasons.append("±0 registry could not confirm — routed to document review")

    # Email domain: one weight, never a gate. A known health-system employer
    # domain corroborates current clinical employment, so it outranks generic
    # business.
    email_class = user.get("email_domain_class") or classify_email_domain(user.get("email") or "")
    domain = email_domain(user.get("email") or "")
    if email_class == "academic":
        score += TIER_WEIGHTS["academic_email"]
        reasons.append(f"+{TIER_WEIGHTS['academic_email']} academic email domain ({domain})")
    elif email_class == "business" and is_health_system_domain(domain):
        score += TIER_WEIGHTS["health_system_email"]
        reasons.append(f"+{TIER_WEIGHTS['health_system_email']} known health-system domain ({domain})")
    elif email_class == "business":
        score += TIER_WEIGHTS["business_email"]
        reasons.append(f"+{TIER_WEIGHTS['business_email']} business email domain ({domain})")
    elif email_class == "consumer":
        score += TIER_WEIGHTS["consumer_email"]
        reasons.append(f"{TIER_WEIGHTS['consumer_email']} consumer email domain (not disqualifying)")

    cv = _json_field(user, "cv_parsed_json")
    cv_ok = bool(cv.get("ok"))

    # Board certification is the single biggest self-reported weight, and it
    # used to be awarded for any non-empty string — "n;n" scored the full
    # twenty. It now has to read as a certification, or be corroborated by the
    # CV, which nobody can type their way into.
    claimed_board = (user.get("board_cert") or "").strip()
    cv_boards = cv.get("board_certifications") or []
    board_recognizable = plausibility.board_certification_is_recognizable(claimed_board)
    if board_recognizable or cv_boards:
        score += TIER_WEIGHTS["board_certified"]
        src = claimed_board or ", ".join(cv_boards)
        reasons.append(f"+{TIER_WEIGHTS['board_certified']} board certified ({src})")
    elif claimed_board:
        reasons.append(
            f"±0 board certification “{claimed_board[:40]}” does not read as one "
            "and the CV does not corroborate it")

    years = user.get("years_experience")
    if years is None:
        years = cv.get("years_in_practice")
    if years is not None:
        try:
            years = int(years)
        except (TypeError, ValueError):
            years = None
    if years is not None:
        if years >= 10:
            score += TIER_WEIGHTS["years_10_plus"]
            reasons.append(f"+{TIER_WEIGHTS['years_10_plus']} {years} years in practice")
        elif years >= 5:
            score += TIER_WEIGHTS["years_5_to_10"]
            reasons.append(f"+{TIER_WEIGHTS['years_5_to_10']} {years} years in practice")
        else:
            reasons.append(f"±0 {years} years in practice")

    taxonomy_served = _served_specialty_for_taxonomy(taxonomy_desc)
    if taxonomy_served:
        score += TIER_WEIGHTS["subspecialty_match"]
        reasons.append(
            f"+{TIER_WEIGHTS['subspecialty_match']} NPPES taxonomy "
            f"“{taxonomy_desc}” matches a specialty we serve ({taxonomy_served})")

    if cv_ok:
        score += TIER_WEIGHTS["cv_parsed"]
        reasons.append(f"+{TIER_WEIGHTS['cv_parsed']} CV uploaded and parsed")

    linkedin = (user.get("linkedin_url") or "").strip()
    if linkedin:
        if plausibility.linkedin_url_is_wellformed(linkedin):
            score += TIER_WEIGHTS["linkedin_present"]
            reasons.append(f"+{TIER_WEIGHTS['linkedin_present']} LinkedIn profile provided")
        else:
            # "nlk" is not a LinkedIn profile, and three points for typing
            # something in the box is three points for nothing.
            reasons.append(f"±0 “{linkedin[:40]}” is not a LinkedIn profile URL")

    # ── Blockers: proposal suppressed, forced manual review — never a rejection ──
    if npi_result == NpiResult.MISMATCH.value:
        blockers.append(
            "NPI mismatch: " + (npi_payload.get("reason") or "registry record does not "
                                "corroborate the signup"))
    claimed = (user.get("specialty") or "").strip().casefold()
    if claimed and taxonomy_desc:
        claimed_served = _served_specialty_for_taxonomy(claimed) or (
            claimed if claimed in _SERVED_SPECIALTY_STEMS else None)
        # Only a POSITIVE divergence blocks: both sides map to served
        # specialties and they differ. "Could not determine" (unmapped
        # taxonomy wording) is not a divergence and never blocks.
        if claimed_served and taxonomy_served and claimed_served != taxonomy_served:
            blockers.append(
                f"NPI specialty divergence: signup claims {claimed_served}, NPPES "
                f"taxonomy is “{taxonomy_desc}” ({taxonomy_served})")
    if duplicate_npi:
        blockers.append("Duplicate NPI: another account claims this NPI")
    if registry_result == "mismatch":
        blockers.append(
            "Registry mismatch: " + (registry_payload.get("reason")
                                     or "the registry record does not corroborate the signup"))

    # A signup that does not hold together goes in front of a human. These are
    # blockers for the same reason an NPI mismatch is: they suppress the
    # proposal and force review. They are not rejections, and a real doctor who
    # mistyped is exactly who this is protecting from an automatic decision.
    for finding in plausibility.flags(user, _json_field(user, "credentials_json")):
        if finding["severity"] == plausibility.SEVERITY_HIGH:
            detail = f" — {finding['detail']}" if finding.get("detail") else ""
            blockers.append(
                f"Implausible {finding['field'].replace('_', ' ')}: "
                f"{finding['issue'].replace('_', ' ')}{detail}")

    proposed: Optional[str] = None
    if not blockers:
        # Reviewer needs identity positively established, not specifically an
        # NPI: a doctor confirmed against SCFHS or the Indian register has
        # proved exactly what an NPPES match proves, and gating the senior tier
        # on a US identifier would cap every international physician at labeler
        # no matter how well they check out.
        if score >= REVIEWER_MIN_SCORE and (npi_ok or registry_ok):
            proposed = "reviewer"
        elif score >= LABELER_MIN_SCORE:
            proposed = "labeler"

    # The ladder stops at 'reviewer'. If a future edit above ever routes a score
    # to 'advisor', fail here rather than quietly appointing someone to an
    # equity relationship on the strength of an NPPES lookup.
    if proposed == "advisor":  # pragma: no cover - guard against a future edit
        raise AssertionError(
            "propose_tier must never propose 'advisor': an advisory relationship "
            "is negotiated and carries equity, not scored (Advisor PRD §2.3)")

    return {
        "score": score,
        "proposed_tier": proposed,
        "reasons": reasons,
        "blockers": blockers,
    }
