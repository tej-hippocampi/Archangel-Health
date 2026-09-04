# Real-record case generation — the §A7 check, run

The *Case Generation Fix* PRD (`PRD_CASE_GENERATION_FIX.md`) diagnoses why the
four committed patient records yielded nothing on the Task-creation screen and
asks, in §A7, for the fixture-ingest to be **run** on patients 1–4 after the fix
and the result recorded here — executed, not asserted.

This is that record. The numbers below were produced by sending each bundle
through `patient_fixtures.ingest_committed_bundles` — the real door (`POST
/admin/fixtures/ingest-patient-records`), background unpack included — and then
running `real_cases.plan_cases` on the resulting ingest case, once with no
specialty hint and once with the upload's declared specialty (which is what the
generate route passes). Reproduce with:

```
cd backend && python3 -m pytest tests/test_longitudinal_front_door.py tests/test_case_generation_fix.py -q
```

## The table

| Record | Ingest cases | Notes with offset | Encounters | Density-gate pass | Verifiable | Generatable static (declared specialty) | Longitudinal points (declared specialty) |
|---|---|---|---|---|---|---|---|
| patient-1 | **1** | 244 / 278 · all dated files | 22 | 13 | 12 | 16 | 11 |
| patient-2 | 1 (quarantined — see below) | 244 / 278 | — | — | — | — | — |
| patient-3 | **1** (was 1 through this door; 3 only on the older tree the PRD was written against) | **149 / 155** (was 74 / 155) | 5 | 4 | 3 | **1** | **1** |
| patient-4 | **1** | 301 / 329 | 12 | 3 | 2 | 5 | 3 |

Without the declared specialty, patient-3 and patient-4 are **0 generatable**
on every encounter (`specialty not served … best 0.38–0.58`) and patient-1 loses
one. That is the screen the PRD describes, and it is the state of any bundle
that arrives through the hospital portal, which carries no manifest and lands as
`general`. §A5 makes the picker a required step on that row.

Reference: `LONGITUDINAL_CASES.md` quotes 55 → 22 → 18 for these four charts
(59 → 25 → 21 in the original PRD). This run gives **39 encounters → 20 decision
points → 17 verifiable** on the three charts that ingest (patient-2's 16 → 2 → 1
is measured on the quarantined body and adds up to the same 55 → 22 → 18). No
gate constant moved; `tests/test_longitudinal_front_door.py::_EXPECTED` pins the
per-chart triples and passes unchanged.

## What each defect turned out to be, on this tree

The PRD was verified against an older snapshot. Running it here:

| § | Claim | Found | Done |
|---|---|---|---|
| A1 | Text notes carry no date → 0/79 with offset | **True.** 74 of 155 notes had an offset, all of them FHIR `DocumentReference`s; every plain-text file was undated. | `adapters/note_text.py` reads `manifest.date` → `Service date:` header (also `Date of service`, `Report date`, `Admission Date`) → the `NNN_YYYY-MM-DD_` filename; `note_type` from the filename token. Result: 149/155 dated; 56 "Lab report", 15 "Radiology", 4 "Discharge" instead of 79 "Progress". |
| A2 | A bundle splits into 2–3 ingest cases | **Does not reproduce.** `unify_patient_keys` already folds `default`-keyed fragments into the one keyed patient (patient-3 without any manifest: 3 groups → 1), and the fixture door already declares `manifest.patient_key`. | Report now names it (`unification: single_keyed_patient_absorbed_unkeyed`, `unkeyed_fragments_absorbed: 80`); the bare-zip path is pinned by test. A second idempotency key (partner + filename) stops a re-packed fixture from landing twice. |
| A3 | Notes-only fragment has no anchor → every date becomes a "hold" | **True in the code path**, rare in practice once A2 holds. | `_collect_structured_dates` reads notes/studies/problems/medications; `index_source` says `latest_notes` when a note anchored the chart; the no-anchor hold reads `no index anchor: N date-like token(s) … no manifest index_event`. |
| A4 | Structured dates ignore the record's day-first reading | **True.** `parse_datetime("06/01/2024")` was June 1 in a DMY record. | `parse_datetime(date_order=)` threaded through every structured call; structured slash dates now also count as inference evidence. Patient-3's CSV is ISO, so its numbers do not move — the fix is for the next partner. |
| A5 | Specialty inference has nothing to read | **Partly.** With the notes attached the per-encounter signal rises (0.53 → 0.56, 0.59 → 0.58 on patient-3) but hepatology and nephrology split the score on a decompensated-cirrhosis chart with renal monitoring; whole-chart inference reads nephrology 0.38. The declaration is the fix. | Fixture map: patient-3 → `hepatology`. Box 1 row shows "Nephrology (inferred 0.38)" in amber; Box 2 disables Build and shows the picker with the reason; the plan modal shows the picker on any encounter blocked by the specialty and re-plans on set. |
| A6 | `[object Object]` | **True** — the generate route's 422 detail is `{error, blockers}`. | `errText(e, fallback)` in `admin_shell.js`, used at every catch (pinned: no `(e && e.detail) ||` remains). |

## The blocker the PRD did not see

With dates and specialty fixed, patient-3 is still **1 generatable of 4 decision
points**, and the reason is not a gate the PRD names:

```
case has an empty medication list   ← encounters 0, 2, 3
```

`cases.assert_multimodal_content` (the text content floor: ≥1 panel, ≥2
well-formed results, ≥1 note ≥200 chars, ≥1 problem, **≥1 medication**) runs on
the VISIBLE window of every proposal. Patient-3's FHIR bundle carries no
`MedicationStatement`; its medications live in the four discharge summaries and
one ER triage note. A discharge summary is written at discharge — *after* the
decision point, because "discharge" is itself a disposition change that ends the
window — so its medication list is sealed as part of the outcome, correctly. Only
encounter 4 has a pre-decision note (the ER triage sheet) that names drugs.

The floor's docstring says it is *never weakened*, and it is not weakened here.
The specialty-study requirement was made advisory for `real_deid` charts with the
argument that "the partner's export either contains the ECG or it does not";
the same argument applies to a medication list, and applying it would take
patient-3 to 4 generatable / 4 longitudinal points. That is a product decision
about what ships to a physician, not a bug, so it is recorded here and left for
the owner to make. patient-1 (FHIR `MedicationStatement`s present) and patient-4
(159 medications) are unaffected.

## Through the admin UI

* **Preview cases** on patient-3 shows real notes in the case panel: a Discharge
  summary, Lab reports and Radiology reports with day offsets, not 79 "Progress"
  notes with none.
* **Build the chart walk** on patient-3 with the specialty declared produces 1
  ordered point (see above); on patient-1, 11; on patient-4, 3.
* The Longitudinal batch on Task Routing shows ≥1 trajectory after a build
  (`tests/test_longitudinal_front_door.py::test_patient_one_becomes_a_sealed_ordered_walk`).

A number materially below this table means a gate constant moved — read
`LONGITUDINAL_CASES.md` §2 before "fixing" the gate.
