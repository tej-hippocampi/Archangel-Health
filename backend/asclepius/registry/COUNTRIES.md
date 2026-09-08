# Onboarding country catalogue

countries.json contains the 249 ISO 3166-1 alpha-2 entries from the public-domain IANA tzdb iso3166.tab (Paul Eggert, 2025-07-01; code set references ISO/TC 46 N1127, 2024-02-29), plus Kosovo under the commonly used user-assigned code XK. XK is not an officially assigned ISO country code. Names are the source's usual English labels; configured registries retain their existing display names.

Source: https://data.iana.org/time-zones/tzdb/iso3166.tab
Standard: https://www.iso.org/iso-3166-country-codes.html

The backend catalogue is mirrored at landing/src/lib/countries.json so offline/loading/error states and older servers cannot remove international choices. Update both together; the catalogue test enforces equality. No runtime network lookup or added dependency is required.

Registry adapters remain a separate, smaller map. An unconfigured country uses document review and must never be advertised as automatically verified. Country selection enables an application; approval and work access retain their existing gates.
