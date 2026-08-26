"""Country channels: where a doctor's colleagues actually are.

A nephrologist in Riyadh and a nephrologist in Boston share a specialty and
almost nothing else about practising it -- the guidelines, the referral
pathways, the conferences worth flying to, and what medical AI even means in
a hospital are all local. #nephrology is the right room for the medicine;
#saudi-arabia is the right room for the rest.

Config only, deliberately: this module never touches a database, so the
community plane can import it without reaching into the asclepius plane. The
caller that knows which countries have members (main.py at startup, and the
morning run) passes them in.

The timezone is the reason each entry carries one. The morning routine fires
at local 7am, which means a doctor in India gets their brief with breakfast
rather than at four in the afternoon, and that is most of what makes a daily
routine feel like it was written for them. Countries spanning several zones
get one representative zone, named below where the choice is real: a physician
in Seattle gets the US brief on New York's clock. Splitting those cohorts is a
later problem and an easy one; picking nothing would have meant everybody gets
UTC, which is nobody's morning.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional


@dataclass(frozen=True)
class Country:
    code: str          # ISO 3166-1 alpha-2
    name: str
    slug: str          # channel slug
    timezone: str      # IANA zone used for "local 7am"


#: Seeded from the countries the registry config already knows how to verify,
#: plus the ones physicians actually come from. A country absent here still
#: onboards fine -- it simply has no channel of its own until it is added.
COUNTRIES: Dict[str, Country] = {
    c.code: c for c in (
        # US spans six zones; Eastern is where most of the physician base and
        # all of the company's own hours are.
        Country("US", "United States", "usa", "America/New_York"),
        Country("CA", "Canada", "canada", "America/Toronto"),
        Country("GB", "United Kingdom", "united-kingdom", "Europe/London"),
        Country("IE", "Ireland", "ireland", "Europe/Dublin"),
        Country("IN", "India", "india", "Asia/Kolkata"),
        Country("PK", "Pakistan", "pakistan", "Asia/Karachi"),
        Country("BD", "Bangladesh", "bangladesh", "Asia/Dhaka"),
        Country("LK", "Sri Lanka", "sri-lanka", "Asia/Colombo"),
        Country("PH", "Philippines", "philippines", "Asia/Manila"),
        Country("SA", "Saudi Arabia", "saudi-arabia", "Asia/Riyadh"),
        Country("AE", "United Arab Emirates", "uae", "Asia/Dubai"),
        Country("QA", "Qatar", "qatar", "Asia/Qatar"),
        Country("KW", "Kuwait", "kuwait", "Asia/Kuwait"),
        Country("EG", "Egypt", "egypt", "Africa/Cairo"),
        Country("NG", "Nigeria", "nigeria", "Africa/Lagos"),
        Country("KE", "Kenya", "kenya", "Africa/Nairobi"),
        Country("GH", "Ghana", "ghana", "Africa/Accra"),
        Country("ZA", "South Africa", "south-africa", "Africa/Johannesburg"),
        Country("AU", "Australia", "australia", "Australia/Sydney"),
        Country("NZ", "New Zealand", "new-zealand", "Pacific/Auckland"),
        Country("DE", "Germany", "germany", "Europe/Berlin"),
        Country("FR", "France", "france", "Europe/Paris"),
        Country("ES", "Spain", "spain", "Europe/Madrid"),
        Country("IT", "Italy", "italy", "Europe/Rome"),
        Country("NL", "Netherlands", "netherlands", "Europe/Amsterdam"),
        Country("BR", "Brazil", "brazil", "America/Sao_Paulo"),
        Country("MX", "Mexico", "mexico", "America/Mexico_City"),
        Country("SG", "Singapore", "singapore", "Asia/Singapore"),
        Country("MY", "Malaysia", "malaysia", "Asia/Kuala_Lumpur"),
        Country("JP", "Japan", "japan", "Asia/Tokyo"),
    )
}

#: Where the company keeps its own clock, for content that belongs to no
#: single country.
DEFAULT_TIMEZONE = "America/New_York"


def get(code: Optional[str]) -> Optional[Country]:
    return COUNTRIES.get((code or "").strip().upper())


def timezone_for(code: Optional[str]) -> str:
    country = get(code)
    return country.timezone if country else DEFAULT_TIMEZONE


def channel_defs(codes: Iterable[str]) -> List[Dict[str, object]]:
    """Channel definitions for the countries that actually have members.

    Only those: a rail listing thirty countries, twenty-eight of them empty, is
    a directory rather than a community. A country's channel appears within a
    day of its first physician signing up.
    """
    seen = []
    for raw in codes or ():
        country = get(raw)
        if country and country.code not in seen:
            seen.append(country.code)
    return [
        {
            "slug": COUNTRIES[code].slug,
            "name": COUNTRIES[code].slug,
            "description": (
                f"Physicians practising in {COUNTRIES[code].name}: local events, "
                "regulation, and how medical AI is actually landing there."
            ),
            "post_policy": "all",
            "grp": "country",
            "country": code,
        }
        for code in seen
    ]
