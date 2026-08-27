# V4 real cases + promotion — operator runbook

Implements the *V4 Cases and Promotion* PRD. Everything below was built against
`patient-1`, `patient-3` and `patient-4`.

Read this if you are trying to get a partner bundle from "uploaded" to "a
physician is labelling it".

---

## 1. Ship a manifest — do this first, it unblocks everything

A bundle with no `manifest.json` and no specialty on its upload link resolves to
`general`. `general` is the **absence** of a specialty, not a specialty, so
`POST /ingestion/cases/{id}/promote` refuses it:

> **409 — Specialty not determined for this case.**

That gate is correct and must not be weakened: promoting on `general` routes the
case to the wrong physician pool and mislabels the export invisibly. Fix the
input.

Put this at the **root** of each bundle before upload:

```json
{
  "specialty": "hepatology",
  "patient_key": "ehr-1-patient",
  "index_event": "2023-04-04",
  "source": "partner-deid-export-v1"
}
```

`manifest.patient_key` short-circuits patient-key unification entirely and is the
authoritative grouping hint. `index_event` is the decision point; without it the
timeline anchors to the latest structured observation, which is usually but not
always what you want.

| Bundle | `specialty` | `patient_key` | Enabled? |
|---|---|---|---|
| patient-1 | `hepatology` | `ehr-1-patient` | **Yes** — registered, see §3 |
| patient-3 | `nephrology` | `patient-3-patient` | Yes |
| patient-4 | `cardiology` | `patient-4-patient` | Yes |

patient-3 and patient-4 route to those specialties **because that is where their
promoted decision point actually lives**, not because it was convenient: the AKI
question in patient-3 is nephrology and the troponin question in patient-4 is
cardiology.

**These records are multi-specialty and one manifest specialty cannot express
that.** patient-4 is neuro *and* hepatobiliary *and* endocrine. The manifest
specialty is the **routing** specialty for the ingest case; each promoted task
then carries the specialty of its own decision point. Set the manifest to the
specialty of the *primary* decision point you intend to promote, and use
`POST /admin/uploads/{id}/specialty` to re-route before promoting a second case
from the same chart.

**If you cannot edit the bundles:** set `specialty` when you mint the upload
link. Same effect, no file changes, and it is the path a hospital partner should
use in production.

> Note: `UploadLinkRequest.specialty` still defaults to `nephrology`. A link
> minted without thinking therefore stamps nephrology on whatever arrives — the
> same class of invisible mislabel this section is about. Always set it
> explicitly, or set it to `""` so the promote gate makes the operator choose.

---

## 2. Partner-quality finding to send back

Every note in these bundles carries a de-identification header — 413 of them
across the three. One of those headers contains a raw date:

```
patient-4/clinical-notes/148_2021-01-28_prescription-medication.txt:5
De-identification: Omitted nurse name/designation fields (green redaction); year as printed (11/7/21)
```

**One line for their next export:**

> `148_2021-01-28_prescription-medication.txt` line 5 contains `(11/7/21)`, an
> unshifted original date, inside the de-identification header. The header should
> carry no dates at all.

On our side the header is now removed before any date scan or rewrite
(`timeline.strip_provenance_lines`), and what was removed is reported rather than
dropped:

* `report.timeline.provenance_lines_stripped` — how many headers we removed;
* `report.timeline.provenance_header_dates` — the **masked** date tokens found
  inside them;
* an **advisory** review reason `provenance_header_dates` on the ingest case.

Advisory, not blocking. The chart's clinical text is clean, so it proceeds; the
finding is about the partner's pipeline and is recorded so somebody can act on
it. The header used to either quarantine the chart (on the no-anchor path and on
the quarantine scrub re-check) or, worse, be silently rewritten into a
plausible-looking `[day -1257]` — laundering a real leak into fake metadata.

---

## 3. Hepatology is now a registered specialty

`seed_corpus/hepatology.v1.json` + `HEPATOLOGY_TAXONOMY` + a registry entry, per
`docs/ADD_A_SPECIALTY.md`. Five buckets, all `min_difficulty: hard`, and 13 real
seed items — not a stub. An enabled specialty with an empty corpus does not fail;
it silently sorts every case into **no** bucket and ships an unusable taxonomy
field, so `specialties._assert_enabled_specialties_have_corpora()` now runs at
import and refuses to start with that state.

Bucket routing also needs `_SUBTOPIC_SIGNALS` and `_SPECIALTY_SIGNALS` entries in
`real_cases.py`. The corpus guard catches a missing corpus; only a signal makes
the bucket reachable. Both are in place for hepatology.

Neurology is deliberately **not** registered: patient-4's promotable question is
cardiological, and registering a specialty with no seed corpus and no reviewer
pool buys nothing.

---

## 4. Fan-out — visibility is not the same as paid labels

|  | Meaning | Control |
|---|---|---|
| **Visible** to every approved physician | The task appears in their queue | Specialty routing (+ `open_to_all_specialties`) |
| **Labelled** by every approved physician | Each one is paid to complete it | `max_labels` |

At $75/case, `max_labels = 60` is **$4,500 per case**. The V4 cases load with
`max_labels = 3` — one labeller plus two independent for Cohen's κ (the floor is
n=30 blinded double-labelled cases).

`open_to_all_specialties` bypasses specialty routing for **visibility only**. It
does not change `max_labels`, so it does not change what we pay. Off by default:
specialty routing is a quality control, and this suspends it deliberately and
visibly rather than by accident. It appears in the promote review modal as
*"Show to all approved physicians (ignores specialty routing)"*, unchecked.

It never widens anything else — a `real_deid` task flagged for fan-out is still
invisible to a v1/v2/v3 session and still requires `real_data_approved`.

Be exact about what it widens: specialty routing is a **matching** control, not a
credential boundary. The picker already lets any labeller request any enabled
specialty's queue, so this flag does not defeat an access check — there isn't one
on that axis. What changes is that a case reaches a pool that did not ask for it,
and the annotator's own specialty still ships on the record, so the mismatch is
visible to a buyer rather than hidden. The real boundaries are `require_label`
(contributor tier) and the `real_deid` wall, and neither is touched.

---

## 5. Promotion debug runbook

Work these in order.

**Step 1 — is the upload brokering?**
```
GET /api/asclepius/ingestion/uploads → row.promote_block
```
`reason: "brokering"` is terminal. Brokering data can never become a task. Check
`ingest_case_effective_purpose` — it reads `COALESCE(case.purpose, upload.purpose)`,
so a case can be brokering even when the upload is not.

**Step 2 — did any case reach `ingested`?**
```
GET /api/asclepius/ingestion/uploads/{id}/cases
```
`quarantined` → read `quarantine_reason`. `needs_review` → clear the hold in
Partner uploads. The provenance-header date is no longer a quarantine reason.

**Step 3 — is specialty determined?**
```
row.specialty == "general"  →  409 on promote
POST /api/asclepius/admin/uploads/{upload_id}/specialty  {"specialty": "hepatology"}
```

**Step 3b — which endpoint are you calling?** There are two and they fail
differently:

| Endpoint | Specialty check | Failure |
|---|---|---|
| `POST /ingestion/cases/{id}/promote` | `specialty_is_undetermined` | **409** |
| `POST /ingestion/cases/{id}/generate` | `is_enabled` allowlist | **400** |

A 409 means "nobody chose a specialty". A 400 naming the enabled set means "you
chose one we do not serve". Both paths carry their own brokering refusal — a
guard added to only one is a hole.

**Step 4 — did the conversion gate fail?** A 422 with
`{"error": "case_judge_gate", "failures": [...]}` means the case converted but did
not clear the hardness or case-judge floors. Not a bug — sharpen the clinical
question and retry. Floors are env-tunable.

**Step 5 — is `_convert_and_gate` erroring?** Needs a live LLM. A 5xx is a
provider problem, not a pipeline problem. Check the Anthropic key and rate limits
before touching ingestion code.

### 5.1 Dry run — see it before you commit it

```
POST /api/asclepius/ingestion/cases/{id}/promote?dry_run=true
```

Runs everything — brokering refusal, status check, specialty gate, conversion,
candidate generation, both judges — and returns `_sample_case_view`: the public
case, rendered prompt, candidates, judge scores and difficulty band. It writes
**nothing**: no task, no status change, no `case_promoted` event. Idempotent and
repeatable.

It returns the sample **even when the gate failed**, which is the point: "this
scored 0.4 on divergence" is what you need to write a sharper question. Read
`would_promote` and `sample.failures`.

`{"dry_run": true}` in the body works too. The query parameter can only ever turn
the dry run **on** — it cannot force a commit the body asked to be a dry run.

---

## 6. The three V4 cases

`backend/asclepius/v4_cases.py`, loaded by
`POST /api/asclepius/generation/load-v4-real-cases` and, when the V4 queue is
empty, by the draw itself.

| Case | Specialty | The trap | Status |
|---|---|---|---|
| `v4-hep-001` | hepatology | Bilirubin rises 15.04 → 17.77 while GGT falls 1361 → 123 | **Loads** |
| `v4-neph-001` | nephrology | Hgb corrected 5.4 → 11.1 looks like success; it is the harm | **Loads** |
| `v4-card-001` | cardiology | Troponin 0.855 in an uncharacterised acute stroke | **HELD** — see below |

Each ships with an authored A/B preference pair, so loading needs no LLM.

### Why `v4-card-001` is held

`cases._assert_specialty_studies` requires a cardiology case to carry ≥1 `ecg` or
`echo` study, and it is right to — cardiology reasoning lives in the tracing.
patient-4's ECG report is not among the artifacts this case was built from, and
nothing in `v4_cases.py` is invented: a fabricated tracing inside a record stamped
`case_source: real_deid` is exactly the invisible mislabel every guard in this
codebase exists to prevent.

So the case is built in full and held, and `load_v4_cases` names it in `holds`
rather than letting it be silently absent.

**To release it:** attach patient-4's admission ECG report from the bundle's
`clinical-notes/` to `CASE_C["studies"]` as

```python
{"modality": "ecg", "label": "12-lead ECG", "collected_offset_days": 0,
 "findings": "<the report text>"}
```

No code change and no restart — the content gate is re-checked on every
`load_v4_cases` call.

### What is not in these cases

* **No post-decision data.** A panel, note or study dated after the decision
  point *is* the answer. The confirming trajectories (patient-1's day +60 stent
  occlusion, patient-3's day +1 creatinine, patient-4's day +2 chemistry) live in
  `ground_truth.evidence.subsequent_course`, never as visible items.
* **Not the 1.7 bicarbonate.** patient-4 carries three conflicting bicarbonates
  on one day, one of them not survivable and contradicted by the same day's ABG.
  It is an OCR artifact; `real_cases.implausible_value` now drops it, and no case
  is built on it.

### Before selling any of these as "hard"

They load with `difficulty: "hard"` but `difficulty_measured: false` — a
**declared** claim, not a measured one. Run them past frontier models blind
(`grade-real-models`) and keep only the ones the models fail. A case a model gets
right is not worth a physician's twenty minutes.

---

## 7. Lab data quality

Three repairs, all in `real_cases.py`, all applied at case curation:

1. **Polluted units** (measured: 42 occurrences, caught by nothing). The
   partner's OCR concatenates unit + range + interpretation into the unit column
   (`mmol/L (19 - 24) — low`). `split_polluted_unit` salvages the unit and
   recovers the range. **The value is never dropped** — a polluted unit is a
   formatting problem, not a bad result.
2. **Dates inside reference ranges** (measured: 16). `ref='(0.25-08-2021)'` means
   the range is unusable. It is nulled, the value is kept, and the row is marked
   `ref_range_unusable` so a physician and a buyer can see *why* it has no range.
   No attempt is made to parse it into anything.
3. **Physiologically impossible values.** A small table of hard bounds
   (bicarbonate 5–50, pH 6.5–8.0, potassium 1.5–9.0, sodium 100–190). Out of
   bounds → drop the result, count it as `implausible_value`, **never quarantine
   the chart**. Deliberately narrow: if you cannot name a bound as "incompatible
   with life" it does not belong in that table, because a tight bound silently
   deletes the extreme-but-real values that make a case hard.

**`derive_flag` may only use a range the partner actually supplied.** A flag
computed from a range reconstructed out of a corrupted unit string is a clinical
claim built on OCR repair, and it will be wrong silently. The recovered range is
counted in the curation stats and never written to `ref_low`/`ref_high`, so
`derive_flag` structurally cannot reach it; a row whose range was nulled as a date
is refused outright.

---

## 8. Do not touch

`unify_patient_keys` · `_KEY_SOURCE_PRECEDENCE` · `specialty_is_undetermined` and
the promote specialty gate · the brokering refusals on **both** promote paths ·
`_CLINICAL_RATIO_RE` / `_is_clinical_ratio` · `public_case` and
`_INTERNAL_CASE_KEYS` · `is_enabled` (extend the registry, never bypass the
check) · `_assert_specialty_studies`.

Every one is either correct as written or is the guard that makes a silent
failure loud. If a change appears to need one of them relaxed, the change is
wrong. Patient-key unification in particular was investigated and found correct:
one key per source, `fhir_r4` wins precedence, everything merges into one case.

---

## 9. Tests

```
python3 -m pytest backend/tests/test_v4_promotion.py \
                 backend/tests/test_asclepius_ingestion.py \
                 backend/tests/test_asclepius_real_case_gen.py \
                 backend/tests/test_promotion_gate.py -q
```
