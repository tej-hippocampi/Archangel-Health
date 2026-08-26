"""What a medical credential looks like, country by country.

The signup flow was built around one country: a 10-digit NPI and a two-letter
US state licence, both required. That turns away every doctor who has neither
— a Saudi consultant registered with SCFHS, an Indian physician with a state
council number — not because we would not take them, but because the form has
nowhere to put what they actually have.

This module is the data behind asking the right question instead. One entry
per country: what the identifier is called there, who issues it, what shape it
takes, what else the registry needs before it will answer, and how far we can
actually check it.

Three honest verification methods, and the difference between them is what a
miss MEANS:

  ``api``       a real API we can query. A definitive "no such registrant" is
                evidence. Only these registries are ``authoritative``.
  ``scrape``    a public search page with no API. Good enough to confirm a
                match, never good enough to disprove one: these registers go
                stale (India's IMR says so on the page itself) and pages move.
                A miss is inconclusive and routes to document review.
  ``document``  no usable public lookup at all — captcha-walled (Saudi
                Arabia), paywalled (Nigeria), licensed-only (Australia), or
                simply absent (Germany, Egypt). The doctor uploads their
                registration certificate and a human reads it.

Adding a country is an entry here plus, if it is ``api``/``scrape``, an
adapter in ``adapters/``. Nothing else in the codebase should carry a country
list. Countries with no entry fall back to ``DEFAULT_REGISTRY`` so the wizard
can never dead-end on a nationality we have not thought about yet.

Sources for formats and lookup URLs are cited per entry; they were verified
against the live registries when this was written. Regexes here are ADVISORY
— they warn the doctor that a number looks unusual, they never block a
signup. Several countries genuinely have no published format, and a doctor
who cannot get past a regex is a doctor we lose to a guess about punctuation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

# Verification methods (see the module docstring for what each one means).
METHOD_API = "api"
METHOD_SCRAPE = "scrape"
METHOD_DOCUMENT = "document"


@dataclass(frozen=True)
class FieldSpec:
    """One extra input a registry needs before it can answer.

    India cannot be searched without knowing which state council registered
    the doctor; the Philippines' by-number lookup requires a date of birth to
    confirm identity. Asking for these up front is the difference between a
    check that runs and a check that sits in the queue waiting for an email.
    """

    key: str                       # stored under credentials.registryExtras[key]
    label: str
    kind: str = "text"             # text | select | date
    options: Tuple[str, ...] = ()  # for kind="select"
    required: bool = True
    hint: str = ""


@dataclass(frozen=True)
class RegistryConfig:
    country: str                   # ISO 3166-1 alpha-2
    country_name: str
    registry_name: str             # the body a doctor would recognize
    id_label: str                  # what to call the number on the form
    method: str
    id_regex: Optional[str] = None  # advisory only — never a gate
    id_hint: str = ""              # placeholder / example
    extra_fields: Tuple[FieldSpec, ...] = ()
    lookup_url: Optional[str] = None  # where an admin verifies by hand
    authoritative: bool = False    # may a miss here count as evidence?
    note: str = ""                 # shown to admins working the queue

    def extra_field(self, key: str) -> Optional[FieldSpec]:
        for spec in self.extra_fields:
            if spec.key == key:
                return spec
        return None


# ─── India ───────────────────────────────────────────────────────────────────
# There is no national registration format: each state council numbers its own
# way, from bare serials to council/year-prefixed strings. The real key is the
# PAIR (number, council), so the council is a required field rather than a
# nicety. The newer NMR portal is Aadhaar-linked and will eventually be the
# right target, but registration on it is voluntary and coverage is still far
# below 1% of doctors, so the IMR is what we search today.
_INDIAN_COUNCILS: Tuple[str, ...] = (
    "Andhra Pradesh Medical Council",
    "Arunachal Pradesh Medical Council",
    "Assam Medical Council",
    "Bihar Medical Council",
    "Chandigarh Medical Council",
    "Chhattisgarh Medical Council",
    "Delhi Medical Council",
    "Goa Medical Council",
    "Gujarat Medical Council",
    "Haryana Medical Council",
    "Himachal Pradesh Medical Council",
    "Jammu & Kashmir Medical Council",
    "Jharkhand Medical Council",
    "Karnataka Medical Council",
    "Kerala State Medical Council",
    "Madhya Pradesh Medical Council",
    "Maharashtra Medical Council",
    "Manipur Medical Council",
    "Meghalaya Medical Council",
    "Mizoram Medical Council",
    "Nagaland Medical Council",
    "Odisha Council of Medical Registration",
    "Puducherry Medical Council",
    "Punjab Medical Council",
    "Rajasthan Medical Council",
    "Sikkim Medical Council",
    "Tamil Nadu Medical Council",
    "Telangana State Medical Council",
    "Tripura State Medical Council",
    "Uttar Pradesh Medical Council",
    "Uttarakhand Medical Council",
    "West Bengal Medical Council",
    "Medical Council of India (pre-2020 registration)",
)

_CANADIAN_COLLEGES: Tuple[str, ...] = (
    "College of Physicians and Surgeons of Ontario",
    "College of Physicians and Surgeons of British Columbia",
    "College of Physicians and Surgeons of Alberta",
    "Collège des médecins du Québec",
    "College of Physicians and Surgeons of Manitoba",
    "College of Physicians and Surgeons of Saskatchewan",
    "College of Physicians and Surgeons of Nova Scotia",
    "College of Physicians and Surgeons of New Brunswick",
    "College of Physicians and Surgeons of Newfoundland and Labrador",
    "College of Physicians and Surgeons of Prince Edward Island",
    "Other / territorial college",
)

_UAE_REGULATORS: Tuple[str, ...] = (
    "Dubai Health Authority (DHA)",
    "Department of Health Abu Dhabi (DOH)",
    "Ministry of Health and Prevention (MOHAP)",
)


REGISTRY_CONFIGS: Dict[str, RegistryConfig] = {
    # ── Fully automatable: real APIs, a miss is evidence ──────────────────
    "US": RegistryConfig(
        country="US",
        country_name="United States",
        registry_name="NPPES (CMS National Provider Identifier)",
        id_label="NPI number",
        method=METHOD_API,
        id_regex=r"^[12]\d{9}$",
        id_hint="10 digits",
        lookup_url="https://npiregistry.cms.hhs.gov/provider-view/{id}",
        authoritative=True,
        note=(
            "An NPI proves enumeration, not licensure. State licence is "
            "collected separately and cross-checked against the NPPES taxonomy."
        ),
    ),
    "FR": RegistryConfig(
        country="FR",
        country_name="France",
        registry_name="Annuaire Santé (RPPS)",
        id_label="RPPS number",
        method=METHOD_DOCUMENT,
        id_regex=r"^\d{11}$",
        id_hint="11 digits",
        lookup_url="https://annuaire.esante.gouv.fr/",
        note=(
            "France publishes a free FHIR API over the Annuaire Santé, which "
            "would make this fully automatic — it needs an ANS API key in "
            "RPPS_API_KEY and an adapter. Until then the public directory link "
            "answers in one search."
        ),
    ),
    "NL": RegistryConfig(
        country="NL",
        country_name="Netherlands",
        registry_name="BIG-register (CIBG)",
        id_label="BIG number",
        method=METHOD_DOCUMENT,
        id_regex=r"^\d{11}$",
        id_hint="11 digits",
        lookup_url="https://zoeken.bigregister.nl/",
        note=(
            "CIBG offers an official SOAP lookup that would automate this; "
            "until an adapter exists, the public search answers by BIG number."
        ),
    ),

    # ── Undocumented but real JSON search endpoints behind the public
    #    register pages. Automatable, but not sources we let say "no".
    "IN": RegistryConfig(
        country="IN",
        country_name="India",
        registry_name="National Medical Commission — Indian Medical Register",
        id_label="Medical council registration number",
        method=METHOD_SCRAPE,
        # Councils number in wildly different shapes; accept anything sane.
        id_regex=r"^[A-Za-z0-9/\-. ]{2,30}$",
        id_hint="as printed on your council certificate",
        extra_fields=(
            FieldSpec(
                key="stateCouncil",
                label="State medical council",
                kind="select",
                options=_INDIAN_COUNCILS,
                hint="the council that issued your registration",
            ),
            FieldSpec(
                key="registrationYear",
                label="Year of registration",
                kind="text",
                required=False,
                hint="YYYY",
            ),
        ),
        lookup_url="https://www.nmc.org.in/information-desk/indian-medical-register/",
        note=(
            "The IMR lags state council data and says so on the page. A doctor "
            "who is not found may still be registered — check the state "
            "council's own register or their certificate before concluding "
            "anything."
        ),
    ),
    "PK": RegistryConfig(
        country="PK",
        country_name="Pakistan",
        registry_name="Pakistan Medical & Dental Council",
        id_label="PMDC registration number",
        method=METHOD_SCRAPE,
        id_regex=r"^\d{3,6}-[A-Za-z]$",
        id_hint="e.g. 81910-P",
        lookup_url="https://pmdc.pk/",
    ),
    "PH": RegistryConfig(
        country="PH",
        country_name="Philippines",
        registry_name="Professional Regulation Commission",
        id_label="PRC licence number",
        method=METHOD_DOCUMENT,
        id_regex=r"^\d{4,7}$",
        id_hint="up to 7 digits",
        lookup_url="https://verification.prc.gov.ph/",
        note=(
            "PRC's by-number check also requires a date of birth, which we do "
            "not collect — it is not needed for anything else here, and a "
            "birth date is not a thing to hold because one registry's form "
            "wants it. Search by name at the link, or read the licence card."
        ),
    ),
    "KE": RegistryConfig(
        country="KE",
        country_name="Kenya",
        registry_name="Kenya Medical Practitioners and Dentists Council",
        id_label="KMPDC registration number",
        method=METHOD_DOCUMENT,
        id_hint="as printed on your retention certificate",
        lookup_url="https://registers.kmpdc.go.ke/localPractitioners/getLicensedMedicalPractitioners/",
        note=(
            "KMPDC publishes the whole register as one page, but masks "
            "registration numbers (A0**9), so a number cannot be confirmed "
            "automatically. Search the page for the doctor's name and check "
            "the status column."
        ),
    ),

    # ── Document review: the register is closed, priced, or captcha-walled ─
    "SA": RegistryConfig(
        country="SA",
        country_name="Saudi Arabia",
        registry_name="Saudi Commission for Health Specialties (SCFHS)",
        id_label="SCFHS registration number",
        method=METHOD_DOCUMENT,
        id_hint="your MumarisPlus registration number",
        lookup_url="https://scfhs.org.sa/en/E-Services/regvaliddescription",
        note=(
            "SCFHS's public check is behind two captchas and is keyed on a "
            "national ID, Iqama or passport number — identifiers we do not ask "
            "for. Verify from the uploaded MumarisPlus card or good-standing "
            "certificate; the registration number alone will not run."
        ),
    ),
    "GB": RegistryConfig(
        country="GB",
        country_name="United Kingdom",
        registry_name="General Medical Council",
        id_label="GMC reference number",
        method=METHOD_DOCUMENT,
        id_regex=r"^\d{7}$",
        id_hint="7 digits",
        lookup_url="https://www.gmc-uk.org/registration-and-licensing/our-registers",
        note=(
            "The GMC's public register is free to search by hand; the only "
            "programmatic feed is a paid annual download. Check the number "
            "against the register link."
        ),
    ),
    "AU": RegistryConfig(
        country="AU",
        country_name="Australia",
        registry_name="Ahpra / Medical Board of Australia",
        id_label="Ahpra registration number",
        method=METHOD_DOCUMENT,
        id_regex=r"^MED\d{10}$",
        id_hint="e.g. MED0000932846",
        lookup_url="https://www.ahpra.gov.au/Registration/Registers-of-Practitioners.aspx",
        note=(
            "Ahpra's public register blocks automated access and its data feed "
            "(PIE) is licensed. Check by hand against the register link."
        ),
    ),
    "BD": RegistryConfig(
        country="BD",
        country_name="Bangladesh",
        registry_name="Bangladesh Medical & Dental Council",
        id_label="BM&DC registration number",
        method=METHOD_DOCUMENT,
        id_regex=r"^A-?\d{4,6}$",
        id_hint="e.g. A-114993",
        lookup_url="https://verify.bmdc.org.bd/",
        note="BM&DC's lookup is captcha-gated; verify by hand or from the certificate.",
    ),
    "DE": RegistryConfig(
        country="DE",
        country_name="Germany",
        registry_name="Landesärztekammer (state medical chamber)",
        id_label="EFN (Einheitliche Fortbildungsnummer)",
        method=METHOD_DOCUMENT,
        id_regex=r"^80276\d{10}$",
        id_hint="15 digits, starting 80276",
        note=(
            "Germany publishes no national physician register — chamber "
            "directories list only doctors who consented. Verify from the "
            "Approbationsurkunde."
        ),
    ),
    "EG": RegistryConfig(
        country="EG",
        country_name="Egypt",
        registry_name="Egyptian Medical Syndicate",
        id_label="Syndicate membership number",
        method=METHOD_DOCUMENT,
        note=(
            "The syndicate has no public third-party lookup. Verify from the "
            "syndicate card or a good-standing certificate."
        ),
    ),
    "NG": RegistryConfig(
        country="NG",
        country_name="Nigeria",
        registry_name="Medical and Dental Council of Nigeria",
        id_label="MDCN folio number",
        method=METHOD_DOCUMENT,
        lookup_url="https://www.portal.mdcn.gov.ng/confirm-doctor-status",
        note="MDCN's online confirmation charges a per-check verification fee.",
    ),
    "CA": RegistryConfig(
        country="CA",
        country_name="Canada",
        registry_name="Provincial college of physicians and surgeons",
        id_label="College registration number",
        method=METHOD_DOCUMENT,
        extra_fields=(
            FieldSpec(
                key="province",
                label="Licensing college",
                kind="select",
                options=_CANADIAN_COLLEGES,
            ),
        ),
        note=(
            "Licensure is provincial and every college runs its own register "
            "with no API. Check the college named on the signup."
        ),
    ),
    "AE": RegistryConfig(
        country="AE",
        country_name="United Arab Emirates",
        registry_name="DHA / DOH / MOHAP",
        id_label="Licence number",
        method=METHOD_DOCUMENT,
        extra_fields=(
            FieldSpec(
                key="regulator",
                label="Licensing authority",
                kind="select",
                options=_UAE_REGULATORS,
            ),
        ),
        note="Each emirate licenses separately; check the authority named on the signup.",
    ),
    "IE": RegistryConfig(
        country="IE",
        country_name="Ireland",
        registry_name="Irish Medical Council",
        id_label="IMC registration number",
        method=METHOD_DOCUMENT,
        lookup_url="https://www.medicalcouncil.ie/public-information/check-the-register/",
    ),
    "NZ": RegistryConfig(
        country="NZ",
        country_name="New Zealand",
        registry_name="Medical Council of New Zealand",
        id_label="MCNZ registration number",
        method=METHOD_DOCUMENT,
        lookup_url="https://www.mcnz.org.nz/registration/register-of-doctors/",
    ),
    "ES": RegistryConfig(
        country="ES",
        country_name="Spain",
        registry_name="Consejo General de Colegios Oficiales de Médicos",
        id_label="Número de colegiado",
        method=METHOD_DOCUMENT,
        lookup_url="https://www.cgcom.es/consulta-publica-colegiados",
        note="The national register's search is reCAPTCHA-protected; check by hand.",
    ),
    "IT": RegistryConfig(
        country="IT",
        country_name="Italy",
        registry_name="FNOMCeO — Albo Unico Nazionale",
        id_label="Albo registration number",
        method=METHOD_DOCUMENT,
        lookup_url="https://albounico.fnomceo.it/",
        note=(
            "FNOMCeO's terms prohibit automated access to the Albo. Check by "
            "hand against the register link."
        ),
    ),
}


DEFAULT_REGISTRY = RegistryConfig(
    country="",
    country_name="",
    registry_name="National medical regulator",
    id_label="Medical registration number",
    method=METHOD_DOCUMENT,
    id_hint="as printed on your registration certificate",
    note=(
        "No registry adapter for this country. Verify from the uploaded "
        "registration certificate."
    ),
)


def normalize_country(country: Optional[str]) -> str:
    """ISO alpha-2, upper-cased. Empty when we were told nothing.

    A blank country means a legacy row, and legacy rows are US — that is who
    signed up before the form could ask. Callers decide that; this only
    normalizes what it is given.
    """
    return (country or "").strip().upper()[:2]


def for_country(country: Optional[str]) -> RegistryConfig:
    """The config for a country, or a document-review fallback carrying that
    country's code so the admin queue still says where the doctor practises."""
    code = normalize_country(country)
    found = REGISTRY_CONFIGS.get(code)
    if found:
        return found
    if not code:
        return DEFAULT_REGISTRY
    from dataclasses import replace

    return replace(DEFAULT_REGISTRY, country=code, country_name=code)


def supported_countries() -> Tuple[Dict[str, Any], ...]:
    """Every configured country, for the signup form's picker.

    Sorted by name so the list reads alphabetically; the caller decides what
    to float to the top.
    """
    return tuple(
        {
            "country": cfg.country,
            "country_name": cfg.country_name,
            "registry_name": cfg.registry_name,
            "id_label": cfg.id_label,
            "id_regex": cfg.id_regex,
            "id_hint": cfg.id_hint,
            "method": cfg.method,
            "extra_fields": [
                {
                    "key": f.key,
                    "label": f.label,
                    "kind": f.kind,
                    "options": list(f.options),
                    "required": f.required,
                    "hint": f.hint,
                }
                for f in cfg.extra_fields
            ],
        }
        for cfg in sorted(REGISTRY_CONFIGS.values(), key=lambda c: c.country_name)
    )


def format_looks_right(country: Optional[str], identifier: str) -> bool:
    """Does this identifier match the country's usual shape?

    Advisory. A False warns the doctor and flags the signup for a human; it
    never blocks a submission, because several countries have no published
    format and none of them owe us one.
    """
    cfg = for_country(country)
    if not cfg.id_regex:
        return True
    import re

    return bool(re.fullmatch(cfg.id_regex, (identifier or "").strip()))
