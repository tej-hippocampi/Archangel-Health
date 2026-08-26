# Verifying a doctor who is not American

Signup asked for a 10-digit NPI and a two-letter US state licence, both
required. A Saudi consultant registered with SCFHS has neither, and neither
does an Indian physician with a state council number, so the Continue button
simply never enabled for them. A doctor asked us directly whether we accept
Saudi degrees. We do. The site did not say so anywhere, and the form actively
disagreed.

## How it works now

The form asks where you practise and where you are licensed — separately,
because they routinely differ — and the answer decides what it asks next.
US signups see exactly what they saw before. Everyone else gets their own
registry's field, labelled the way their regulator labels it, plus whatever
that registry needs before it will answer.

`backend/asclepius/registry/config.py` is one entry per country: the
identifier's name, who issues it, its shape, what else the lookup needs, and
how far we can actually check it. **Adding a country is an entry there** plus,
if it is checkable, an adapter in `registry/adapters/`. Nothing else in the
codebase should carry a country list. Countries with no entry fall back to
document review, so the wizard can never dead-end on a nationality nobody
thought about.

## The rule that matters

**A source we do not fully trust may confirm a doctor. It may never disprove
one.**

Only registries backed by a real API are `authoritative` and may return
`not_found`. Everywhere else a miss becomes `inconclusive` and routes to
document review. India's own register warns on the page that it lags the state
councils, so treating "not in the IMR" as "not a doctor" would reject real
physicians on the strength of a stale database. That translation happens in one
place — `registry/dispatch.py` — so no future adapter can get it wrong alone.

Results: `verified`, `mismatch`, `not_found` (authoritative only),
`inconclusive`, `unavailable`, `document_only`, `queued`. Only the first three
are definitive; the rest are retried and never overwrite evidence already held.

## What each country actually supports

| Country | Identifier | Check | Notes |
|---|---|---|---|
| US | NPI (10 digits, Luhn) | **Automatic** | NPPES. Proves enumeration, not licensure |
| India | Council registration + council name | **Automatic** | The IMR's JSON search. A number is only unique *within* a council |
| Pakistan | PMDC `NNNNN-P` | **Automatic** | Returns registration type and validity dates |
| UK | GMC (7 digits) | Manual | Free to search by hand; the only feed is a paid annual download |
| Saudi Arabia | SCFHS registration | Manual | Public check is behind two captchas and keyed on a national ID |
| Australia | Ahpra `MED##########` | Manual | Register blocks automated access; the data feed is licensed |
| Bangladesh | BM&DC `A-#####` | Manual | Captcha-gated |
| Kenya | KMPDC registration | Manual | Whole register is public but registration numbers are masked (`A0**9`) |
| Philippines | PRC licence | Manual | The by-number lookup also needs a date of birth, which we do not collect |
| France | RPPS (11 digits) | Manual (automatable) | A free FHIR API exists; needs `RPPS_API_KEY` and an adapter |
| Netherlands | BIG (11 digits) | Manual (automatable) | CIBG offers an official SOAP lookup |
| Germany | EFN | Manual | No national register exists; chamber directories are consent-only |
| Egypt | Syndicate number | Manual | No public third-party lookup |
| Nigeria | MDCN folio | Manual | The online check charges a per-check fee |
| Canada | Provincial college | Manual | Licensure is provincial; every college has its own register |
| UAE | DHA / DOH / MOHAP | Manual | Each emirate licenses separately |

"Manual" is not a gap in the product. It is the honest answer for a register
that cannot be queried, and the admin queue is built for it: the registration
number, a deep link to the official register, and a line saying what to check.

## Gates

`tiering.py` A1–A7 no longer assume America:

- **A1** reads whichever registry answers. A miss in a register that admits it
  is incomplete is `UNKNOWN`, not `FAIL`.
- **A2** does not ask for a state licence from a country that issues none;
  outside the US, registration *is* the licence and A1 already judged it.
- **A3** accepts qualifications that are not called MD, Staatsexamen among
  them — Germany awards no degree in the usual sense.
- **A5** cannot pass an international doctor on the strength of the OIG
  exclusion list, which they were never subject to. That unresolved gate is
  exactly why every international signup reaches a human.

## Adding a country

1. An entry in `REGISTRY_CONFIGS`. If its register cannot be queried, set
   `method=METHOD_DOCUMENT` and write a `note` telling the admin what to do.
   That is a complete, shippable answer.
2. If it *can* be queried: a module in `registry/adapters/` exporting
   `fetch(identifier, *, extras, timeout)` and optionally `match(...)`,
   registered in `adapters/_ADAPTER_MODULES`. Adapters report what the registry
   said; `dispatch` decides what it means.
3. `authoritative=True` only for a real, documented API. It is the flag that
   lets a miss reject somebody.
4. A test with a recorded payload. `tests/test_registry_dispatch.py` runs
   offline against real response shapes.

### One live gotcha

`nmc.org.in` serves an incomplete certificate chain: browsers and curl chase
the missing intermediate, Python's OpenSSL does not, so the handshake fails
against a perfectly valid certificate. The intermediate is bundled in
`registry/adapters/ca/`. If a registry starts failing with
`CERTIFICATE_VERIFY_FAILED`, that is the shape of it — supply the intermediate
rather than reaching for `verify=False`, which trades their packaging mistake
for a hole of our own.

## What is deliberately not collected

Medical school name and graduation year. PRD C §3.3 removed them to keep
prestige and IMG status out of scoring, and a school picker would have walked
that straight back in. Country routes verification and is pinned immobile at
the encoder — see `CONTRIBUTOR_SCORE.md`.
