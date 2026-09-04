# PRD — Real-record case generation: why it yields nothing, the fix, and the admin cleanup around it

Status: **built**. Every citation below is against the current tree; the run
that verified Part A is recorded in `CASE_GENERATION_CHECK.md`. The section
"What the tree actually did" at the end records where the diagnosis, written
against an older snapshot, differed from what running it found — read that
before assuming a defect below is still open.

---

## §0 The invariant — read first

**Nothing a partner sent is lost, and nothing a physician sees leaks the
future.** Every change in Part A adds timing to items that already existed —
it never invents a date, never guesses a date it cannot read (an unparseable or
absent one leaves the item undated, which `real_cases._visible` fails closed
on), and never moves a gate. The density-gate constants, the multi-patient
never-merge rule, `deidentify()`, the provenance strip and the clinical-ratio
exemption are untouched, and the per-chart yield triples the front-door test
pins are unchanged. Part B removes nothing but screen furniture: no endpoint,
table, row, upload, lead, request or export changes.

---

# PART A — Why static and longitudinal generation fail on real records

## §A0 The symptom chain, in one line

Bundle uploads → the plain-text notes carry no date → they never form or join an
encounter → the planner sees labs alone on encounters the notes would have
carried, and without a declared specialty its inference sits under the floor →
**0 generatable** → the UI shows `[object Object]`.

Each arrow is a separate defect. The five below are in pipeline order.

## §A1 Defect 1 — the note adapter threw the note's date away — **fixed**

`asclepius/adapters/note_text.py:150 parse()` used to emit `{note_type,
author_role, text}` and nothing else. The date is available twice in every
partner file — the filename (`072_2025-07-01_discharge-summary.txt`) and the
header (`Service date: 2025-07-01`) — and the adapter read neither.

Measured: after `normalize_timeline`, **74 of 155** notes on patient-3 carried
an offset, every one of them a FHIR `DocumentReference`; **0 of the 79** text
files did. `_timed_offsets` (`asclepius/real_cases.py:100`) only
counts items with an integer offset, so undated notes never formed or joined an
encounter, and `cases.py:495` "no clinical note ≥ 200 chars" was true of the
visible window on a chart with four discharge summaries.

**Fix.** `note_date_from` (`asclepius/adapters/note_text.py:112`)
sets `collected_at` from, in order: `manifest.date`; the header line `Service
date:` (also `Date of service`, `Report date` — never `Admission Date`, which
dates the stay rather than the document and would place a discharge summary
before the course it narrates); the filename pattern
`^\d+_(\d{4}-\d{2}-\d{2})_`. Only a value `parse_datetime` reads is emitted, so
`unknown-date` pages and malformed headers stay undated instead of quarantining
the chart.
`note_type` comes from the filename token (`_FILENAME_TYPE_TOKENS`,
`asclepius/adapters/note_text.py:68`):
`discharge-summary → Discharge`, `clinical-note → Progress`, `radiology-report →
Radiology`, `*-lab / rft / cbc / lft / electrolytes / … → Lab report`.
Result on patient-3: **149 / 155** notes dated; 56 Lab report · 15 Radiology ·
4 Discharge instead of 79 Progress. Tests:
`tests/test_case_generation_fix.py` (§A1 block).

## §A2 Defect 2 — a bundle's files split into 2–3 ingest cases — **did not reproduce; pinned**

`asclepius/ingestion.py:1683 _patient_key_and_source` keys a fragment by the
manifest's `patient_key`, else the adapter's `_patient_keys`, else a `__`
filename convention, else `"default"`. On patient-3 without a manifest three
groups form: `{patient-3-patient}` (FHIR), `{hl7-…}` (HL7) and `{default}` (80
text/CSV files). `unify_patient_keys` (`asclepius/ingestion.py:1710`) already
treats `default` as the *absence* of an identity and folds it into the single
keyed patient; the fixture door already declares `manifest.patient_key`
(`asclepius/patient_fixtures.py:161`). On this tree the bundle lands as **one**
case through both doors.

**Done.** The report names the absorption (`unification:
"single_keyed_patient_absorbed_unkeyed"`, `unkeyed_fragments_absorbed`,
`asclepius/ingestion.py:1786`); the bare-zip path and the two-real-keys safety
rule are pinned by test. A second idempotency key on the fixture door
(`asclepius/patient_fixtures.py:249`, `asclepius/store.py:5613`) stops a
re-packed bundle — the manifest is part of the bytes, so §A5's specialty change
moves the sha256 — from landing the same chart twice.

## §A3 Defect 3 — a notes-only case has no anchor, so every date becomes a "hold" — **fixed**

`_collect_structured_dates` (`asclepius/timeline.py:507`) built the index-event
pool from lab panels only. A fragment with notes and no panels got `index =
None`, and the no-anchor branch masked every real date in the text into the
"unresolved date-like tokens" hold. The dates were never unparseable; there was
nothing to measure them against.

**Fix.** One declaration of every structured date field per collection
(`_STRUCTURED_DATE_KEYS`, `asclepius/timeline.py:479`) feeds the anchor pool,
the day-first inference and the offset assignment, so a notes-only fragment
anchors on its latest note (`index_source: latest_notes`; the pool's counts are
reported as `index_pool`, `asclepius/timeline.py:688`). When no anchor exists and
dates do, the report carries `hold_reason` (`asclepius/timeline.py:830`) and
ingestion quarantines with it (`asclepius/ingestion.py:2041`): *"no index
anchor: N date-like token(s) … no manifest index_event"* — a message an admin can
act on.

## §A4 Defect 4 — structured dates ignored the record's day-first reading — **fixed**

`infer_date_order` (`asclepius/timeline.py:149`) reads patient-3 as **DMY** and
the note rewriter uses it (`rewrite_note_dates`, `asclepius/timeline.py:361`),
but every structured `parse_datetime` call took no order: `"06/01/2024"` parsed
as June 1 where the record meant January 6.

**Fix.** `parse_datetime(value, *, date_order=)` (`asclepius/timeline.py:182`),
default unchanged, threaded through every structured call in
`normalize_timeline`; the raw structured date strings now also count as
inference evidence, so a `dd/mm/yyyy` CSV with no notes beside it is still read
day-first. Patient-3's CSV is ISO, so its numbers do not move; the fix is for the
next partner.

## §A5 Defect 5 — specialty inference has nothing to read — **partly; the picker is now the step**

With the notes attached the per-encounter signal barely moves on patient-3:
hepatology and nephrology split a decompensated-cirrhosis chart with renal
monitoring almost evenly (best 0.43–0.58 against the 0.6 floor,
`asclepius/real_cases.py:1489`), and whole-chart inference reads nephrology at
0.38. The floor is kept. Three things done:

* The fixture map declares patient-3 **hepatology** (`asclepius/v4_cases.py:896`).
  The generate route already uses the upload's declared specialty as the hint,
  so the fixture charts plan with it.
* Ingest records a `content_summary` per chart (`asclepius/ingestion.py:2240`):
  counts, encounters, decision points, and the inferred specialty with its
  confidence and whether it clears the floor. `/ingestion/uploads` emits it as
  `content` (`routers/asclepius.py:6296`).
* Box 2 (`frontend/asclepius/admin_shell.js:3312 specialtyGate`) disables
  **Build** and shows the picker with copy naming the confidence and the floor
  when no specialty is declared and the inference is below it; the plan modal
  offers the picker on any encounter blocked by `specialty not served`
  (`asclepius/real_cases.py:1987`) and re-plans once it is set.

## §A6 The `[object Object]` — **fixed**

`errText(e, fallback)` (`frontend/asclepius/admin_shell.js:177`) → the detail's
`message`, else its `error` code, else a string detail, else the normalized
message, else the fallback — used at every catch in the file (pinned: no `(e &&
e.detail) ||` remains). A 422 with object detail renders its message.

## §A7 Definition of done for Part A — executed

Recorded in `CASE_GENERATION_CHECK.md`. In short, with the declared specialty:

| Record | Ingest cases | Notes with offset | Encounters | Gate pass | Generatable static | Longitudinal points |
|---|---|---|---|---|---|---|
| patient-3 | **1** | **149/155** (was 74) | 5 | 4 | **1** | **1** |
| patient-1 | 1 | 244/278 | 22 | 13 | 16 | 11 |
| patient-4 | 1 | 301/329 | 12 | 3 | 5 | 3 |

The per-chart density triples are unchanged from the pinned 22/13/12 · 5/4/3 ·
12/3/2 — no gate moved. patient-3's static yield stops at 1, not the ≥3 this
section asked for, because of a floor the PRD did not name: see "What the tree
actually did", item 4.

---

# PART B — Admin cleanup (Data & Task Creation, Systems, Export)

## §B1 Incoming data (Box 1) — **done**

`frontend/asclepius/admin_shell.js:3236 headerLines` / `:3398 box1Row`. One row
= one bundle, three lines, two decisions: sender · file · size · SHA · time;
specialty (declared, or "Hepatology (inferred 0.71)" in amber) · "1 chart · 12
encounters · 79 notes · 45 panels"; the description. Sizes read "260 KB", not
"0 MB" (`:3226 humanBytes`). No inline irreversibility sentence — "This cannot
be undone" stays in the Brokering confirm dialog. The empty state is one quiet
line. Counts come from the unified ingest case.

## §B2 "Unknown sender" and "0 MB" — **done**

`_partner_label_for_upload` (`routers/asclepius.py:6273`) now resolves a
magic-link label, a data-provider `org_name`, **or a health system's name** by
`partner_id`, then the raw id, and never returns None. `/ingestion/uploads`
emits `size_bytes` as an integer on every row (`routers/asclepius.py:6411`) —
the column existed; the "0 MB" was the row rounding 260 KB to megabytes.

## §B3 Delete Partner leads — **done**

`renderPartnerLeads` and its slot are gone from `frontend/asclepius/admin_health.js`.
The lead rows and `/api/leads/admin` stay (landing-form audit trail).

## §B4 Data requests — one form, pick recipients — **done**

`frontend/asclepius/admin_health.js:236 renderDataRequests`: a message, a
recipient row (every organization with an ACTIVE agreement, multi-select with
Select all), Send, and a collapsed history reading "Sent · Gray Scrubs, St.
Mary's · date · "message"". The four structured fields are gone from the screen.
Backend: `HsDataRequestBody` (`routers/asclepius_admin.py:3081`) accepts
`message` + `recipient_hs_ids` (`:3098`) alongside the original structured shape,
which still works; `enqueue_for_request` (`asclepius/hs_request_notify.py:69`)
filters to the chosen organizations, and an ineligible recipient is refused by
name. The letter (`onboarding_emails.py:2343`) omits the specialty/count rows
for a message-only request. The admin view reports `recipients` by name.

## §B5 Export — strip to the five scopes and one button — **done**

`frontend/asclepius/admin_export.js`: the Licensed-to input, Exclusive
checkbox, Exclusive-until date, the explanatory paragraph, the `Export + send
to ▾` select and the Exclusive commitments section are gone. Scope selector,
preview and **Export bundle** remain. The bundle request's `licensed_to` /
`exclusive` fields (`routers/asclepius_admin.py:196-197`) and the exclusivity
endpoints stay, optional and unused by this screen; buyer deliveries remain
reachable from the buyer portal.

## §B6 No-loss contract — **held**

Route diff empty (`python3 scripts/route_baseline.py --diff`). No table, row,
upload, lead, request or export touched. Backend additions are read-path
(`partner_label` fallback, `size_bytes`, `content`) plus the additive request
body and one store lookup.

---

## Tests

`tests/test_case_generation_fix.py` (A1–A6, B2, B4, B5, `node --check` on the
three files) · `tests/test_admin_tasks_redesign_ui.py` (B1 row and the A5 gate,
rendered against the DOM shim) · `tests/test_export_exclusivity.py` (B5: the
screen stopped asking, the endpoint still answers) ·
`tests/test_longitudinal_front_door.py` (the yield pins, unchanged).

## Do not touch — **untouched**

Density-gate constants (`asclepius/real_cases.py:178-179`) · the multi-patient
never-merge rule · the sequence gate · `deidentify()` and PHI verification · the
provenance-footer strip · the clinical-ratio exemption in `_DATELIKE_RE`.

---

## What the tree actually did (read before reopening a section)

1. **§A2 was already fixed** when this PRD was built; the "3 case(s)" it
   describes came from an older snapshot. Both doors land one case.
2. **§A1 was the real loss**, and it changed the note count on every encounter
   (patient-3 enc0: 56 → 77 events) without changing the encounter triples,
   because the text notes share dates with the FHIR notes they duplicate.
3. **§A5's premise — "discharge summaries clear hepatology easily" — is not
   what the inference says.** The renal panels pull the score toward nephrology
   on every encounter. The declaration is the fix, and the row now demands it.
4. **The blocker §A7's "≥3 generatable" runs into is the medication floor** in
   `assert_multimodal_content` (`asclepius/cases.py:433`, "never
   weakened"): patient-3's FHIR bundle carries no `MedicationStatement`, and
   its medication lists live in discharge summaries written *after* each
   decision point, so they are sealed as outcome — correctly. Making the
   medication requirement advisory for `real_deid` charts, as the study
   requirement already is, would take patient-3 to 4 generatable / 4 points.
   That is a product call about what ships to a physician, and it is left
   open here rather than made quietly.
