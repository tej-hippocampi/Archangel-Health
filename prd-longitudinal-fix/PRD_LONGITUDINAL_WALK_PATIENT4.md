> **Accepted revision:** the user approved the measured yields and holding
> narrative-incomplete points for review. Patient-4 detects **8 encounters / 3
> points / 2 potential outcome pairs**; **2 points are held, 1 is ready**.
> Patient-1 detects **9 points**, not 13. This revision supersedes conflicting
> historical acceptance targets below. No density/date threshold is weakened and
> no retrospective presenting narrative is invented. Predecessors depending on a
> held outcome are held too. See `IMPLEMENTATION_STATUS.md` for the current contract.

> Implementation copy: the supplied document describes an older audited archive.
> Its original line citations below are labeled historical; 27 of 30 were stale
> or ambiguous against this branch. The current symbol map is appended below.
> Acceptance conflicts are tracked in `IMPLEMENTATION_STATUS.md`. The original
> user-supplied file outside this worktree is unchanged.

# PRD — Why "Build the chart walk" returns 0, and the patient-4 walk the system must produce

Codebase audited: `Archangel-Health-main (39).zip`. Data: `patient-4…(7).zip` (identical, file for file, to the tree committed at `backend/asclepius/fixtures/patient_bundles/patient-4/`).
Companion file: `patient4_reference_walk.json` — the three-point walk, in the product's own task shape.

---

## 0. The answer in one breath

The chart you clicked on is a **snapshot taken before the fix**. `ingest_cases.case_json` is written once, at upload time, and nothing ever re-runs it. That upload was parsed by the August pipeline, which split patient-4 into three charts and quarantined the two that had the notes. The one chart that survived is the HL7 lab feed — 14 lab days, no notes — and that is what the planner reads today: 6 encounters, 0 generatable. Part A changed the code, not the row.

There is a retry endpoint, but no button calls it. Even after a re-ingest, the walk still returns 0 until an admin sets the specialty, because the chart's own signal reads cardiology at 0.36 against a 0.60 floor. With both done, the shipped pipeline produces **12 encounters → 3 decision points → 2 verifiable**, a three-point walk. The 2–5 s response is correct behaviour: the planner is pure Python (60 ms locally), and with 0 generatable no model is called.

The walk it produces is not yet labelable. At points 1 and 2 the physician sees no narrative note at all — the note budget is filled by lab-report text that duplicates the structured panels — and the discharge summary, which is the outcome, is dated to the admission day and sits inside the visible window. §3 lists eight findings; §4 is the walk built by hand; §5 turns the difference into rules.

---

## 1. Your screen, reproduced

I ran the same bytes through three pipelines in a sandbox (real `process_upload` + `plan_cases`, encryption on, fake LLM).

| | Your row (ingested 8/9) | Aug-11 code on the same zip | Current code, no manifest | Current code + specialty set |
|---|---|---|---|---|
| Charts | 3 · 2 quarantined | **3 · 2 quarantined** | 1 | 1 |
| Notes / panels | 330 / 35 | **329 / 35** | 329 / 157 | 329 / 157 |
| Chart the planner reads | labs only | **HL7 chart: 0 notes, 14 panels** | full chart | full chart |
| Encounters | 6 | **6** | 12 | 12 |
| Generatable | 0 | **0** | 0 (specialty) | **5** |
| Decision points → verifiable | — | 0 → 0 | 3 → 2 | **3 → 2** |
| Blocker text | "0 note(s) · 1 resource type (lab_panels) · specialty not determined" | identical | "specialty not served … best 0.45–0.56 … an admin must set it" | none |

The one-note difference (330 vs 329) is the bundle `README.md`, which the August classifier ingested as a clinical note; the committed tree excludes it by name (`patient_fixtures.py:_NOT_CLINICAL_DATA`).

Why the August code split the chart: no `unify_patient_keys` yet, so FHIR (`patient-4-patient`), HL7 (`hl7-<sha>`), and the unkeyed CSV + notes (`default`) landed as three patients. The notes chart quarantined on `unresolved date-like tokens: ••••-••-••` (A1/A3 not yet landed), the FHIR chart on `••/••`, and the HL7 chart — labs only — ingested clean.

---

## 2. Root cause, with the code

**R1 — the row is frozen.** `plan_cases(ic.get("case") or {}, …)` — `routers/asclepius.py (historical line 7984)-7985`. The planner reads the stored `case_json`; it never touches the raw blob. `content_summary` is written only at ingest (`ingestion.py (historical line 2129)`), which is why the row says *"ingested before specialty inference was recorded"* (`admin_shell.js (historical line 3457)`) — the summary key does not exist on rows written before Sept 7.

**R2 — re-ingest exists but is unreachable, and would half-work.** `POST /ingestion/uploads/{upload_id}/retry` — `routers/asclepius.py (historical line 7073)`. No frontend calls it (grep `/retry` across `frontend/`: only the health-system `retry-failed`). If it were called: (a) *"prior case rows for the upload stay for audit"* (`:7079`), so the row would read "4 charts · 658 notes" because `_upload_content_view` sums every case row (`:6669-6675`); (b) the new rows get their specialty from `manifest → link → 'general'` (`ingestion.py (historical line 1906)`), so an admin-assigned specialty is lost — `set_ingest_specialty_for_upload` writes only the case rows (`store.py (historical line 11866)-11875`), not the upload.

**R3 — the specialty floor blocks every encounter.** `_SPECIALTY_CONFIDENCE_FLOOR = 0.6` (`real_cases.py (historical line 1489)`). Chart-level inference on the full patient-4 chart: cardiology 0.363; per encounter 0.45–0.56, with one encounter reading hepatology 0.602. This is the *correct* behaviour — the chart is a multi-system alcohol-related liver patient with a cardiology thread — and the fix is the admin declaration, which the generate path honours (`asclepius.py (historical line 7977)`). What is missing is the *order*: the button must make the declaration a prerequisite, not a hint discovered from a blocker string. `specialtyGate` (`admin_shell.js (historical line 3447)`) already does this for measured rows; it cannot for a row with no summary.

**R4 — the button plans the first ingested row.** `previewLongitudinal` picks `find(c => c.status === 'ingested')` (`admin_shell.js (historical line 3330)`). `list_ingest_cases` orders `created_at DESC` (`store.py (historical line 5997)`), so after a retry it would pick the new row — but with the old rows still `ingested`, `_upload_content_view` and the row chips stay wrong, and `promote-all` (`asclepius.py (historical line 7574)`) would iterate both.

**R5 — 2–5 seconds is the honest cost.** `plan_cases` (`real_cases.py (historical line 1902)`) segments, gates and pairs without a model; `derive_questions` (`schemas.py (historical line 218)`, default true) authors a question only for generatable encounters (`real_cases.py (historical line 1996)-1999`). Zero generatable ⇒ zero model calls ⇒ request time is DB + JSON + planner.

**R6 — the fixture door is the other way in, and it works.** "Ingest committed patient records" (`admin_shell.js (historical line 3760)` → `asclepius_admin.py (historical line 1386)` → `patient_fixtures.ingest_committed_bundles`, `:196`) packs the committed tree with a manifest carrying `specialty` and `patient_key` (`:161`) and sends it through `/ingestion` under partner `archangel-fixture`. It is idempotent on sha256 and on `(partner, filename)`, so it does not see your Gray Scrubs row and will create a *second*, correct patient-4 upload. `tests/test_longitudinal_front_door.py (historical line 194)` pins 12 → 3 → 2 for it.

---

## 3. What the current pipeline produces on patient-4, and why it is not yet a labelable walk

Everything below is measured on the committed tree with specialty = cardiology, through the shipped code.

**F1 — every note is in the chart twice.** 329 notes = 165 `.txt` files + 161 FHIR `DocumentReference`/`DiagnosticReport` texts of the *same* documents + 3. `curate_notes` (`real_cases.py (historical line 812)`) dedupes on `_normalized_note_text` after `strip_provenance_lines` (`timeline.py (historical line 322)`), but the `.txt` copies carry a header block (`Document index / Document type / Service date / Age on report / CONTENT / ----`) that the provenance regex (`timeline.py (historical line 291)`) does not strip, and the FHIR copies have small wording differences ("section (header … redacted on image)"). Result: `dropped_duplicate: 0` on notes at every point; each point's 10-note budget is ~half duplicates; `n_events` in the density gate counts each document twice (65 / 124 / 200).

**F2 — at points 1 and 2 the physician sees no narrative at all.** Point 1 window: 32 notes, of which 26 are lab text and 1 is the discharge summary — visible 10 = 6 "Report", 2 "Lab report", 2 "Radiology". Point 2: 68 notes → visible 10 = 9 "Report", 1 "Lab report"; the DKA discharge summary is dropped over budget. **A physician at point 2 does not know the admission was DKA.** `_budget` (`real_cases.py (historical line 858)`) keeps the 9 items closest to the index plus the earliest, with no notion of note type. "Report"/"Lab report" notes are text renderings of panels the case already carries as `lab_panels` (`panels_converted: 157`).

**F3 — the discharge summary is dated to the admission day and sits in the visible window.** Point 1: discharge summary at chart day −408, index at −406. It names "cardiac team consultation … medications adjusted" and the discharge list — the outcome. It is only absent today because F2 crowds it out. The leakage guard (`assert_no_answer_leakage`, `ingestion.py (historical line 1612)`) checks the sealed key (newly established problems, newly started drugs) and would not catch prose.

**F4 — the held-out is the whole future, not the next encounter.** `_held_out_summary` (`real_cases.py (historical line 1037)`, `after = …o > index_offset`, `:1050`) takes every item after the index. At point 1 (chart day −406) the ground truth is "portal hypertension; seizure disorder; jaundice … Tazo, Dormicum … CRP 30.7 at +406d" — the admission 13 months later. The frontier difficulty probe grades a model against that. The physician-facing reveal is correct — `_outcome_point` (`asclepius.py (historical line 4317)`) and `outcome_delta` (`real_cases.py (historical line 1218)`) bound it to the next point — but the answer key and the empirical difficulty are not.

**F5 — the reveal inherits F2.** The reveal at point k is the *stored visible case* of point k+1. With F2, the reveal at point 1 shows point 2's lab text and no narrative, so "no angiography this admission" and "dapagliflozin stopped" are unscorable: `not_assessable` by construction, not by clinical judgment.

**F6 — source dates a decade off create five phantom encounters and pollute the problem list.** Encounters 0–4 (chart days −3655 … −1429) are nursing forms and medication sheets whose *printed* years read 16/2016/2020 while their content is the 2026 admission (`154_2016-01-29_vitals.txt`: rows alternate 2016 and 2026 on one sheet; `159_2020-03-04_prescription-medication.txt`: the 2026 drug list). The exporter kept "years exactly as printed". The density gate correctly rejects all five, so the walk is unaffected — but the problem list at point 1 carries "cerebrovascular event / stroke features … since −3246" from those pages, and the med "on-board" set used by `newly_started_drugs` believes levetiracetam and piperacillin-tazobactam have been on board since 2019.

**F7 — specialty is one label per chart, and this chart has two threads.** Per-encounter scores: nephrology 1.5–2.0, cardiology 1.4–2.6, hepatology 0.3–1.7. The cardiology walk in §4 is real (troponin + new EF 30–35% → DKA on a fresh SGLT2 inhibitor → recovered EF a year later), and the same chart would yield a different, equally real hepatology walk. Not a bug; a product fact the row should state.

**F8 — retry loses the admin's specialty and leaves the old rows live** (R2 above). Also: the fixture door's idempotency is per partner, so a partner row and a fixture row of the same bytes can coexist with different specialties.

---

## 4. The patient-4 walk, built by hand

Three points, cardiology, `walk_mode: solo`, `distribution: assigned_only`, `max_labels: 1`. Days are relative; there are no calendar dates anywhere (by design — `timeline.py` destroys the anchor). The JSON file carries the full visible items, the pipeline's own held-out, the bounded outcome window, and the authored layer.

**Point 1 · seq 0 · encounter 5 · chart day −406.** 40–49 M, diabetes, hypertension, known IHD. Admitted with severe abdominal pain and vomiting: lipase 242, amylase 146, bicarbonate 8.4, K 5.84, Na 131 on arrival, corrected within a day. Troponin I 0.855 → 0.609 (falling). Echo on the index day: apex + mid segments akinetic, EF 30–35%, normal chamber size. *Withheld:* the discharge summary.
Question: classify the troponin; commit to a cardiac plan for this admission — angiography now or not, and which therapies before discharge.
Candidates: A (flawed) treats it as type 1 NSTEMI → antiplatelet load, anticoagulation, in-patient angiography. B: injury in critical illness with a newly documented cardiomyopathy → GDMT now, ECG/troponin series, defer angiography, repeat echo at 6–12 weeks, hold SGLT2i while acidotic.
Sealed expectations: no urgent angiography/PCI (7 d); leaves on β-blocker + statin + antiplatelet with MRA and/or SGLT2i (7 d); next echo EF > 40% (180 d). Falsifiers: culprit lesion stented; rising troponin; EF ≤ 35% on next echo.
What the chart wrote next (bounded to point 2): "cardiac team consulted, medications adjusted", no procedure; discharged on aspirin, atorvastatin, nebivolol, spironolactone-furosemide, dapagliflozin; then, day +34, DKA.

**Point 2 · seq 1 · encounter 6 · chart day −372 (34 days later).** DKA with K 2.6, HCO3 13.2, compensated pH; troponin < 0.10 ×2; CXR bilateral lower-lobe infiltrates; ICU 48 h. On the point-1 discharge regimen. *Withheld:* this admission's discharge medication page.
Question: what do you change today, what do you continue, and the plan for the reduced EF over three months.
Candidates: A: stop dapagliflozin (SGLT2i-associated ketoacidosis), hold diuretic-MRA while K 2.6, continue β-blocker/aspirin/statin, re-echo at 8–12 weeks, ischaemic evaluation if not recovered. B (flawed): troponin negative ⇒ no cardiac decision, continue everything including dapagliflozin, echo in a year.
Sealed expectations: dapagliflozin not on the discharge list (7 d); leaves on aspirin + statin (7 d); next echo ≥ 50% (365 d). Falsifiers: dapagliflozin re-listed; EF ≤ 40%; coronary procedure within a year.
What the chart wrote next (bounded to point 3): discharged on meropenem, insulin, PPI, ondansetron, aspirin, atorvastatin — dapagliflozin, spironolactone-furosemide and nebivolol absent; three outpatient lab days and one medication order; then, day +369, alcohol-related collapse, aspiration pneumonia, first seizure, NT-proBNP 70, **echo EF ~55%**, cardiology "BB + antiplatelet + CCB + ARB".

**Point 3 · seq 2 · encounter 11 · chart day +1 (~13 months later) · terminal.** ICU day 3 of the collapse: GCS 12 improving, MRI/CSF normal, BP 160/112, HR to 151 earlier, SpO2 84% on arrival; EF ~55%, IVC collapsing, NT-proBNP 70; on aspirin 75, nebivolol 2.5, amlodipine/valsartan 5/160, SOS hydralazine, levetiracetam, clonazepam, risperidone, NG feed.
Question: given the recovered ventricle, the cardiac regimen for this admission and the year ahead — stop, keep, change, and why.
Candidates: A: treat withdrawal and sepsis first; keep aspirin/statin; HFrEF indication has lapsed, dapagliflozin stays off after DKA; keep β-blocker (rate/secondary prevention) and ARB; drop SOS hydralazine; document recovered (likely alcohol/stress) cardiomyopathy; echo in 12 months. B (flawed): restart full HFrEF therapy including dapagliflozin and spironolactone, TDS hydralazine, in-patient angiography.
Sealed expectations: no SGLT2i/MRA restarted (14 d); BP/HR settle with withdrawal management (5 d). Both will be `not_assessable` — the record ends at day +5. Verifiable points: 2 of 3, exactly as the planner reports.

The three failure modes the walk is keyed to: anchoring on the most extreme number (point 1), missing the drug-precipitated event in the medication history (point 2), carrying a stale diagnosis forward instead of updating on new evidence (point 3). The third is the one only a *walk* can test.

---

## 5. Learnings → rules the system must apply

Each rule maps to one of §3's findings and to the code that owns it.

**Rule 1 — a chart row is stale the moment ingestion changes; say so and offer re-ingest.** Stamp `pipeline_version` on `ingest_cases.report_json` at insert (`ingestion.py (historical line 2132)`); the row renders "ingested with pipeline vN — re-ingest available" when it differs from the running version. (R1, R2)

**Rule 2 — re-ingest supersedes, it never deletes.** Retry marks the upload's existing case rows `status='superseded'` (new status; rows and `case_json` untouched; `task_id` links preserved) *before* `process_upload` runs, carries the admin-assigned specialty forward (`max(specialty) where not undetermined` over the superseded rows → passed into `process_upload` as an override ahead of the `manifest → link → general` chain at `ingestion.py (historical line 1906)`), and `_upload_content_view` / `promote-all` / `previewLongitudinal` ignore `superseded`. A promoted row is never superseded (its tasks exist); retry on an upload with a promoted row creates the new rows beside it and the UI says so. (R2, R4, F8)

**Rule 3 — specialty is a declaration step, not a blocker string.** In the longitudinal modal, when `specialties` is empty *or* the summary is missing, render the picker first with "Build" disabled, and re-plan on choice — the `replan` hook already exists (`admin_shell.js (historical line 3340)`). State the chart's competing threads from `specialty_scores` ("reads cardiology 2.6 / nephrology 2.0 / hepatology 1.7 across encounters"). (R3, F7)

**Rule 4 — a note that is a rendering of a panel is not a note.** In `curate_notes`: strip the `.txt` header block (`^Document index:.*?^-{20,}\s*` multiline) before normalising; then dedupe on the first 300 normalised characters (not the whole text). Drop `note_type in {"Report","Lab report"}` from the *note* budget when the case has ≥ 1 `lab_panel` on the same offset — keep them in the chart, exclude them from the visible window and from `n_events`. Expected on patient-4: notes 329 → ~168; `n_events` at the three points roughly halves; all three still clear the gate (65→~35, 124→~62, 200→~100 against 8). (F1, F2)

**Rule 5 — budget by role, then by recency.** `_budget` for notes ranks by type class first — ER/triage, H&P, progress, consult, discharge, radiology/report interpretation, orders, nursing — then by closeness to the index, keeping the earliest as today. Raise `ASCLEPIUS_REAL_CASE_MAX_NOTES` default 10 → 16 for trajectory points only (the walk is priced per point; a point with no narrative is worth nothing). (F2, F5)

**Rule 6 — a discharge summary is dated at the end of its encounter and is the outcome of the point inside it.** In `build_encounter_case`: any note whose type matches `/discharge/i` and whose offset lies inside the encounter span is re-based to `encounter_span[1]` and marked `model_visible=false, withheld_reason="encounter_resolution"` for any point whose index < that offset. It surfaces, dated correctly, in the next point's reveal via `outcome_delta`. (F3)

**Rule 7 — the held-out ends at the next decision point.** `_held_out_summary(case, index_offset, …, until_offset=None)`; `plan_cases` passes the next qualifying encounter's index (or the record end for the terminal point). `_ground_truth_from_held_out` then grades the model on what the treating team did *next*, which is what the physician's `horizon_days` is asking about. `outcome_delta` is unchanged. (F4)

**Rule 8 — a date the chart cannot support is unknown, not early.** In `normalize_timeline` (or a planner pre-pass): a *note* dated > 3 years before the earliest structured item (lab panel / FHIR-dated resource) with no structured item within ±30 days is set to `collected_offset_days=None`, `withheld_reason="implausible_date"`, and counted in the admin's "omitted for unknown timing". The problem list entries and medications carried on those pages lose their offsets too, so they stop seeding "since" dates and the on-board set. Surface a chip: "5 documents carry dates the chart cannot support". Expected on patient-4: encounters 12 → 7, decision points unchanged 3 → 2. (F6)

**Rule 9 — the dry-run response says why it is fast.** When `generatable == 0`, the plan response carries `"why": "no model was called: 0 encounters cleared the gate"` and the modal prints it under the two numbers. (R5)

---

## 6. Execution steps

### P0 — unblock today, no code

1. Admin → Data & Task Creation → **Ingest committed patient records** (`admin_shell.js (historical line 3760)`). Four uploads land under *Archangel (fixture)* with specialty already set. Requires `DATA_ENCRYPTION_KEY` and a durable ingest volume on Railway (`asclepius_admin.py (historical line 1386)` refuses otherwise, and says which).
2. On the new patient-4 row → **Build the chart walk** → expect *12 encounters detected · 5 generatable · 3 decision points · 2 verifiable*. The dry run costs nothing; the live build authors three questions and runs the judges.
3. Leave the Gray Scrubs row alone. It is audit history; P1 gives it a re-ingest button.

### P1 — re-ingest that supersedes (R1, R2, R4, F8)

- `store.py`: add `'superseded'` to the `ingest_cases` status vocabulary; `supersede_ingest_cases_for_upload(upload_id) -> int` (UPDATE status WHERE upload_id=? AND status IN ('ingested','quarantined','needs_review','rejected')); `assigned_specialty_for_upload(upload_id)` (first non-undetermined specialty over the upload's rows, newest first).
- `ingestion.process_upload(store, upload_id, *, specialty_override=None)`: override wins ahead of `manifest → link → general` (`:1906`). Stamp `report["pipeline_version"] = INGEST_PIPELINE_VERSION` at both `insert_ingest_case` call sites (`:2132`, and the quarantine path).
- `routers/asclepius.py (historical line 7073)` retry: read the assigned specialty, supersede, then `background.add_task(process_upload, store, upload_id, specialty_override=…)`. Refuse (409, named reason) if any row is `promoted`.
- `_upload_content_view` (`:6669`) and the `ingested` / `promotable` lists (`:6801-6807`), `promote-all` (`:7574`), `prepare` (`:7524`), `previewLongitudinal` (`admin_shell.js (historical line 3330)`): exclude `superseded`.
- Row UI: "ingested with pipeline v3 · current v5 — **Re-ingest**" chip when the versions differ or the summary is missing; the button calls retry, then `load()`.
- Never delete: no `DELETE` in this PRD. Superseded rows keep `case_json`, `report_json`, `task_id`.

### P2 — the point is labelable (Rules 3–9)

- `real_cases.curate_notes`: header-block strip; prefix dedupe; lab-text exclusion from the visible window when a same-offset panel exists (Rule 4).
- `real_cases._budget`: type-class ranking for notes; trajectory-mode note cap 16 (Rule 5).
- `real_cases.build_encounter_case`: discharge re-dating + withholding (Rule 6); `_held_out_summary(..., until_offset)` (Rule 7).
- `timeline` / planner pre-pass: implausible-date rule with the admin chip (Rule 8).
- Longitudinal modal: declaration-first specialty step with the competing-thread sentence; the `why` line (Rules 3, 9).

### P3 — pin it

- `tests/test_longitudinal_front_door.py`: patient-4 numbers move to **7 encounters → 3 → 2** after Rule 8 (update `fixtures/patient_bundles/README.md` and `LONGITUDINAL_CASES.md` §2 in the same commit; patient-1's 13-point walk must still reproduce — if it moves, Rule 8's threshold is wrong, not the test).
- New `tests/test_patient4_reference_walk.py`, driven by `prd-longitudinal-fix/patient4_reference_walk.json`: for each point, assert (a) `encounter_index`, `index_event_offset`, `qualifies_as_decision_point`, `outcome_verifiable` match; (b) the visible window contains ≥ 1 narrative note and 0 notes of type Report/Lab report; (c) no note matching `/discharge/i` has an offset ≤ the index inside the point; (d) the held-out's `problems_recorded_after` at point 0 contains nothing dated after point 1's index; (e) `outcome_delta(point k+1)` for point 0 includes a discharge-summary note.
- `tests/test_ingest_retry_supersedes.py`: retry on a 3-row August-shaped upload → 3 rows `superseded`, 1 new `ingested`, content view counts the new row only, specialty carried forward, nothing deleted.

---

## 7. Acceptance

1. Gray Scrubs patient-4 row → Re-ingest → *1 chart · 168 notes · 157 panels · Cardiology (declared)* → Build → 7 encounters · 3 decision points · 2 verifiable; 3 tasks, `trajectory_id` shared, `sequence_index` 0–2, `distribution=assigned_only`, invisible to doctors until Task Routing sends them.
2. Opening point 0 as a physician: the prompt contains the presenting complaint, the two troponins, the echo, and no discharge summary. Opening the reveal after submitting: the discharge summary and the DKA presentation, and nothing dated after point 1's index.
3. Point 2 prompt names DKA.
4. Dry run with 0 generatable returns in < 5 s and says why.
5. `git grep -n "DELETE FROM ingest_cases"` returns nothing new.

---

## 8. Questions for you

1. **The four charts are committed to the repo** (`backend/asclepius/fixtures/patient_bundles/`, README claims de-id verified; `.gitignore` no longer excludes them). Earlier policy was that real de-identified records never enter git. The committed README argues reviewability. Decide: keep in git, or move to a Railway volume and point `ASCLEPIUS_PATIENT_FIXTURE_DIR` at it. The code supports both.
2. Note cap for trajectory points: 16 (my default) or higher? Each extra note is physician reading time at $1,500 a point.
3. Rule 8 threshold (3 years, ±30 days) is tuned to patient-4. Run it across patients 1–3 before merging; if patient-1's 13 points move, tell me the new numbers rather than lowering the threshold.

---

## Claude Code prompt

> Read `prd-longitudinal-fix/PRD_LONGITUDINAL_WALK_PATIENT4.md` and `patient4_reference_walk.json`. Implement P1, then P2, then P3, in separate commits. Invariants: no row in `ingest_cases` is ever deleted; `superseded` is a new status, never a removal; `patient-1` must still reproduce its 13-point walk in `tests/test_longitudinal_front_door.py`; no calendar date may appear in any case, prompt, reveal or export. Before changing `_budget`, run `pytest tests/test_longitudinal_front_door.py -k patient` and record the numbers; after P2, record them again and update `fixtures/patient_bundles/README.md` and `docs/asclepius/LONGITUDINAL_CASES.md` §2 in the same commit. Every `file:line` in the PRD was verified against `Archangel-Health-main (39)`; re-verify with `sed -n` before editing, since lines will have moved.

## Current design and invariants

No case rows are deleted. Retry is excluded atomically from task insertion.
Calendar anchors remain absent from generated cases. Density and date thresholds
are unchanged. Acceptance has been reconciled by the user; see IMPLEMENTATION_STATUS.md.

## Current tests

`backend/tests/test_patient4_reference_walk.py` and
`backend/tests/test_ingest_retry_supersedes.py` cover the implemented safeguards.
Front-door yield assertions now pin the user-approved measured results and review holds.

## Out of scope / do not touch

Production data, deployments, routing and fixture relocation are outside this
local implementation. The embedded Claude prompt above is historical reference.

## Current implementation symbols

| Symbol | Verified location |
|---|---|
| `begin_ingest_retry` | `backend/asclepius/store.py:6029` |
| `supersede_ingest_cases_for_upload` | `backend/asclepius/store.py:6018` |
| `process_upload` | `backend/asclepius/ingestion.py:1804` |
| `prepare_longitudinal_chart` | `backend/asclepius/real_cases.py:123` |
| `curate_notes` | `backend/asclepius/real_cases.py:847` |
| `_budget` | `backend/asclepius/real_cases.py:896` |
| `_held_out_summary` | `backend/asclepius/real_cases.py:1085` |
| `build_encounter_case` | `backend/asclepius/real_cases.py:1188` |
| `plan_cases` | `backend/asclepius/real_cases.py:2020` |
| `retry_ingestion_upload` | `backend/routers/asclepius.py:7084` |
| `reingestControl` | `frontend/asclepius/admin_shell.js:3396` |
| `specialtyGate` | `frontend/asclepius/admin_shell.js:3497` |
