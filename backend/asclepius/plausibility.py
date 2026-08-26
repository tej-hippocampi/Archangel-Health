"""Does this signup hold together?

A test account filled the credential form with keyboard noise — board
certification "n;n", LinkedIn "nlk", licence number "kkkl", licence state
"BL", a fellowship finishing in 7689 — and scored 29 out of 100. Not because
29 is what nonsense is worth, but because the scorer only ever asked whether a
field was non-empty: "n;n" is a truthy string, so it earned the full twenty
points for board certification, and "nlk" earned three for a LinkedIn profile.
Nothing looked at whether any of it could be true.

This module looks. Every check is deterministic, explainable in one sentence,
and cheap; none of them call anything. They answer two different questions,
and the difference matters:

  ``implausible(...)``  can this value be what it claims to be? A weight the
                        scorer should not award for a field that fails.
  ``flags(...)``        what should a human be told about this signup?

A flag is never a rejection. Real doctors mistype, transliterate, hold
qualifications we have not modeled, and finish training in years our
assumptions did not expect. Everything here routes to review, and review is a
person reading the record — the same standing rule as every other check in
this codebase: three outcomes, not two.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

#: Severity is about what a human should do, not how bad the value looks.
SEVERITY_HIGH = "high"      # this cannot be right; look before approving
SEVERITY_MEDIUM = "medium"  # unlikely; worth a glance
SEVERITY_LOW = "low"        # a note, not a concern

_VOWELS = set("aeiouy")

# Real medical-board vocabulary. A certification naming none of these is not
# necessarily fake — it may be a board in a country we have not listed — so it
# is a flag for a human, never a rejection.
_BOARD_WORDS = {
    "board", "american", "abim", "abms", "abfm", "abp", "abr", "abs", "abpn",
    "abog", "aba", "abem", "abpath", "abd", "abo", "aap", "acgme", "college",
    "royal", "mrcp", "mrcs", "frcp", "frcs", "frcpc", "frcsc", "facp", "facs",
    "faap", "fccp", "facc", "fasn", "fashp", "fesc", "certified", "certificate",
    "diplomate", "fellow", "fellowship", "specialist", "consultant", "md",
    "internal", "medicine", "surgery", "pediatrics", "psychiatry", "radiology",
    "pathology", "anesthesiology", "anaesthesia", "cardiology", "nephrology",
    "oncology", "neurology", "dermatology", "emergency", "family", "obstetrics",
    "gynecology", "gynaecology", "ophthalmology", "orthopaedic", "orthopedic",
    "otolaryngology", "urology", "geriatrics", "rheumatology", "endocrinology",
    "gastroenterology", "pulmonary", "critical", "care", "infectious", "disease",
    "hematology", "haematology", "physicians", "surgeons", "national", "speciality",
    "specialty", "mbbs", "dnb", "dm", "mch", "ms", "diploma",
}

_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC", "PR", "VI", "GU", "AS", "MP",
}

_LINKEDIN_RE = re.compile(
    r"^(?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/(?:in|pub|profile)/[^/\s]{2,}",
    re.I,
)

#: Nobody practising today qualified before this, and a year after now is a
#: typo or a plan, not a credential.
_EARLIEST_PLAUSIBLE_YEAR = 1940


def _now_year() -> int:
    return datetime.now(timezone.utc).year


def _fold(text: str) -> str:
    """NFKD-fold and drop combining marks, so "Björk" stays one word.

    Without this, splitting on ASCII letters cuts accented names in half and
    the fragments look vowel-free — which is how a heuristic ends up calling a
    real surname gibberish.
    """
    import unicodedata

    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text or "")
        if not unicodedata.combining(ch)
    )


def _words(text: str) -> List[str]:
    return [w for w in re.split(r"[^A-Za-z]+", _fold(text)) if w]


def _word_is_noise(word: str) -> bool:
    """Judge ONE word.

    Per-word rather than over the whole string joined together: concatenating
    "…Examinations DNB Nephrology" manufactures a consonant run ("nsdnbn")
    that none of its words contains.
    """
    lowered = word.lower()
    if len(lowered) < 2:
        # A lone letter is an initial in a name and noise on its own; the
        # caller decides, by looking at whether the WHOLE string is lone
        # letters ("n;n") or a real name carrying one ("KRISHNA C").
        return True

    # An acronym is not noise. Real credentials are full of vowel-free ones —
    # FRCPC, MRCP, DNB, MBBS — and they are exactly the shape a naive
    # "no vowels" rule punishes.
    if word.isupper() and len(word) <= 6:
        return False

    if len(lowered) >= 3 and not (set(lowered) & _VOWELS):
        return True
    if len(set(lowered)) == 1 and len(lowered) >= 3:
        return True
    if re.search(r"[bcdfghjklmnpqrstvwxz]{6,}", lowered):
        return True
    return False


def looks_like_noise(text: str, *, min_len: int = 2) -> bool:
    """Is this string keyboard noise rather than language?

    Deliberately conservative — it should fire on "kkkl" and "n;n" and stay
    silent on anything a person might really have typed, including short
    abbreviations, acronyms and non-English words. A missed flag costs a
    glance; a false one tells a real doctor their name looks fake.

    A string is noise only when EVERY word in it is: one unreadable token in a
    sentence is a typo, not a fake credential.
    """
    raw = (text or "").strip()
    if len(raw) < min_len:
        return False

    words = _words(raw)
    if not words:
        # Punctuation and digits only, e.g. "n;n" with nothing readable left.
        return True

    if len("".join(words)) < min_len:
        return True

    return all(_word_is_noise(w) for w in words)


def identifier_looks_like_noise(text: str) -> bool:
    """Is this identifier keyboard noise?

    A different test from :func:`looks_like_noise`, because a licence number is
    a code, not language: "A94021" is a perfectly good licence and a terrible
    word, and running the language heuristic over it flags real doctors. What
    licence and registration numbers do have is digits — an all-letters
    "licence number" is either noise or a placeholder.
    """
    value = (text or "").strip()
    if len(value) < 2:
        return True
    if any(ch.isdigit() for ch in value):
        return False
    return looks_like_noise(value, min_len=2)


def plausible_year(value: Any, *, allow_future: int = 0) -> Optional[int]:
    """Parse a four-digit year, or None when it cannot be one.

    Rejects 7689 and 2910 without opinions about anything in range.
    """
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{4}", text):
        return None
    year = int(text)
    if year < _EARLIEST_PLAUSIBLE_YEAR or year > _now_year() + allow_future:
        return None
    return year


def board_certification_is_recognizable(text: str) -> bool:
    """Does this name a board certification?

    Word-based rather than fuzzy: a real certification says who certified them
    or in what. "n;n" says neither.
    """
    value = (text or "").strip()
    if not value or looks_like_noise(value):
        return False
    words = {w.lower() for w in _words(value)}
    if words & _BOARD_WORDS:
        return True
    # A credential acronym we have not listed — a national board abroad, a
    # fellowship postnominal — is plausible on its own. Keyboard noise already
    # failed the check above.
    tokens = _words(value)
    if any(t.isupper() and 3 <= len(t) <= 8 for t in tokens):
        return True
    # Two or more real words. "n;n" reduces to single letters and does not
    # clear this; "Consultant Physician" does.
    return sum(1 for t in tokens if len(t) >= 2) >= 2


def linkedin_url_is_wellformed(url: str) -> bool:
    value = (url or "").strip()
    if not value:
        return False
    return bool(_LINKEDIN_RE.match(value))


def _flag(field: str, issue: str, severity: str, detail: str = "") -> Dict[str, str]:
    return {"field": field, "issue": issue, "severity": severity, "detail": detail}


def flags(
    user: Optional[Dict[str, Any]] = None,
    credentials: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, str]]:
    """Everything about this signup a human should be told, worst first.

    Reads the user row and the credentials blob; both are optional so this can
    run at signup, in the verification agent, and from the admin queue without
    caring which one it has.
    """
    user = user or {}
    creds = credentials or {}
    out: List[Dict[str, str]] = []

    country = (creds.get("countryOfLicensure") or user.get("country_of_licensure") or "US").upper()

    # ── Board certification ──────────────────────────────────────────────
    board = (user.get("board_cert") or "").strip()
    if not board:
        certs = creds.get("boardCertifications") or []
        board = ", ".join(
            str(c.get("board") or "") for c in certs if isinstance(c, dict)
        ).strip(", ")
    if board and not board_certification_is_recognizable(board):
        out.append(_flag(
            "board_cert", "does_not_read_as_a_board_certification", SEVERITY_HIGH,
            f"“{board[:60]}”",
        ))

    # ── LinkedIn ─────────────────────────────────────────────────────────
    linkedin = (user.get("linkedin_url") or creds.get("linkedinUrl") or "").strip()
    if linkedin and not linkedin_url_is_wellformed(linkedin):
        out.append(_flag(
            "linkedin_url", "not_a_linkedin_profile_url", SEVERITY_MEDIUM,
            f"“{linkedin[:60]}”",
        ))

    # ── Free-text identity fields ────────────────────────────────────────
    for key, field in (
        ("primarySpecialty", "primary_specialty"),
        ("specialtyNiche", "specialty_niche"),
        ("fullLegalName", "full_legal_name"),
    ):
        value = str(creds.get(key) or "").strip()
        if value and looks_like_noise(value, min_len=3):
            out.append(_flag(field, "reads_as_noise", SEVERITY_HIGH, f"“{value[:60]}”"))

    # ── Training institutions ────────────────────────────────────────────
    for key, field in (("residency", "residency"), ("fellowship", "fellowship")):
        block = creds.get(key)
        if not isinstance(block, dict):
            continue
        institution = str(block.get("institution") or "").strip()
        if institution and looks_like_noise(institution, min_len=3):
            out.append(_flag(
                f"{field}_institution", "reads_as_noise", SEVERITY_HIGH,
                f"“{institution[:60]}”",
            ))
        year_raw = block.get("year")
        if str(year_raw or "").strip() and plausible_year(year_raw) is None:
            out.append(_flag(
                f"{field}_year", "not_a_possible_year", SEVERITY_HIGH,
                f"“{str(year_raw)[:20]}”",
            ))

    # ── Licence / registration number ────────────────────────────────────
    if country == "US":
        licence = str(creds.get("licenseNumber") or "").strip()
        if licence and identifier_looks_like_noise(licence):
            out.append(_flag(
                "license_number", "reads_as_noise", SEVERITY_HIGH, f"“{licence[:40]}”",
            ))
        state = str(creds.get("licenseState") or "").strip().upper()
        if state and state not in _US_STATES:
            out.append(_flag(
                "license_state", "not_a_us_state", SEVERITY_HIGH, f"“{state[:10]}”",
            ))
    else:
        # Non-US: the shape check belongs to the country's registry config,
        # which knows what that country's numbers look like — and treats
        # "no published format" as "anything goes".
        registration = str(
            creds.get("registrationNumber") or user.get("registry_id") or ""
        ).strip()
        if registration:
            from asclepius.registry import config as registry_config

            if not registry_config.format_looks_right(country, registration):
                cfg = registry_config.for_country(country)
                out.append(_flag(
                    "registration_number", "unusual_format_for_country",
                    SEVERITY_MEDIUM,
                    f"“{registration[:40]}” for {cfg.registry_name}",
                ))

    # ── Timeline ─────────────────────────────────────────────────────────
    out.extend(_timeline_flags(user, creds))

    order = {SEVERITY_HIGH: 0, SEVERITY_MEDIUM: 1, SEVERITY_LOW: 2}
    out.sort(key=lambda f: order.get(f["severity"], 3))
    return out


def _timeline_flags(user: Dict[str, Any], creds: Dict[str, Any]) -> List[Dict[str, str]]:
    """Years that cannot sit in this order.

    Only fires when both ends are known and real: an absent year says nothing,
    and saying nothing is not suspicious.
    """
    out: List[Dict[str, str]] = []

    def year_of(block_key: str) -> Optional[int]:
        block = creds.get(block_key)
        if not isinstance(block, dict):
            return None
        return plausible_year(block.get("year"))

    residency = year_of("residency")
    fellowship = year_of("fellowship")
    degree = plausible_year(creds.get("degreeYear"))

    if degree and residency and residency < degree:
        out.append(_flag(
            "residency_year", "finished_residency_before_qualifying", SEVERITY_HIGH,
            f"degree {degree}, residency {residency}",
        ))
    if residency and fellowship and fellowship < residency:
        out.append(_flag(
            "fellowship_year", "fellowship_before_residency", SEVERITY_MEDIUM,
            f"residency {residency}, fellowship {fellowship}",
        ))

    years_claimed = user.get("years_experience")
    try:
        years_claimed = int(years_claimed) if years_claimed is not None else None
    except (TypeError, ValueError):
        years_claimed = None

    if years_claimed is not None:
        if years_claimed < 0 or years_claimed > 70:
            out.append(_flag(
                "years_experience", "outside_a_working_lifetime", SEVERITY_HIGH,
                f"{years_claimed} years",
            ))
        else:
            anchor = degree or residency
            if anchor:
                # Four years of medical school is already behind the degree
                # year; practice cannot predate qualifying by more than a
                # rounding error.
                possible = _now_year() - anchor + 1
                if years_claimed > possible:
                    out.append(_flag(
                        "years_experience", "more_practice_than_time_since_qualifying",
                        SEVERITY_HIGH,
                        f"{years_claimed} years claimed, qualified {anchor}",
                    ))
    return out


def worst_severity(found: List[Dict[str, str]]) -> Optional[str]:
    for severity in (SEVERITY_HIGH, SEVERITY_MEDIUM, SEVERITY_LOW):
        if any(f.get("severity") == severity for f in found):
            return severity
    return None


def should_flag(found: List[Dict[str, str]]) -> bool:
    """A signup is flagged for a human when anything high-severity fires, or
    when the small stuff piles up past coincidence."""
    if any(f.get("severity") == SEVERITY_HIGH for f in found):
        return True
    return sum(1 for f in found if f.get("severity") == SEVERITY_MEDIUM) >= 2
