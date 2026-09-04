# Pre-delivery export review — the prompt

Paste the block below at the start of a session, with the bundle attached or its
path given. It catches the class of defect `/export-audit` does not: not "is a
forbidden field present", but **"does the prose agree with the records"**.

`/export-audit` and `backend/scripts/export_audit.py` check what must never be in
a bundle — an NC license, a physician's name, an `answer_key`, a payment field, a
roster that does not match the records, a malformed JSONL line. Every one of those
passed on the Centaur nephrology sample. Seven separate defects shipped anyway,
because they were all the same shape: **a companion document asserting something
the records do not say.** That is what this prompt is for. Run both.

---

## The prompt

```
You are reviewing an Asclepius export bundle before it is sent to a buyer. The
bundle is the only artefact the buyer ever sees, so everything wrong with it is
wrong in public. Assume nothing in it is true until you have checked it against
records.jsonl.

Bundle: <path to the .zip>

Work in this order and report as you go.

STEP 0 — RUN THE EXISTING GATE FIRST
    python3 backend/scripts/export_audit.py <bundle.zip>
Exit 2 means stop and fix; a clean exit means nothing here is proven yet. Then
continue with every step below. Do NOT skip this because the bundle "looks
finished" — the Centaur sample went out having never been run through it, and it
failed on three assertions the moment it was.

STEP 1 — VERIFY THE MANIFEST BEFORE READING ANYTHING ELSE
Recompute SHA-256 for every entry in batch.json's `content_hashes` and compare.
Any mismatch means a file was modified after the manifest was written, and the
whole bundle is suspect until you know which and why. Also assert:
  - every file in `files` (except batch.json) has a hash entry;
  - the folder contains exactly the files `files` lists — no extras, none missing.
Report which files verified, not just the failures.

STEP 2 — EVERY PROSE CLAIM MUST BE DERIVED FROM THE RECORDS
This is the main event. For each of datasheet.md, quality_report.md,
EVAL_PACK.md, data_dictionary.md and any PDF or README, extract every factual
claim and check it against records.jsonl / cases.jsonl / batch.json. In
particular, for every claim of the form "records carry `field: value`", read that
field out of the records and compare literally. A template that hardcodes a field
value is the most common failure here — it will be right for the batch the
template was written against and silently wrong for every other one.
Report as: claim -> file:line -> what the records actually say -> verdict.

STEP 3 — EVERY STATISTIC MUST NAME ITS SCOPE
For each number in quality_report.md and datasheet.md, answer: is this computed
over THIS BATCH, or over the whole store? A platform-wide number printed under a
per-batch heading is a misrepresentation even when the number is accurate. Any
figure whose denominator exceeds the batch's own record/submission count is
either mislabelled or wrong. Each statistic must be labelled "this batch" or
"platform-wide, for context" — never left ambiguous.

STEP 4 — CLAIMS OF VALIDATION MUST MATCH THE VALIDATION REPORT
Read validity_report.json first, then read the prose that describes it. If
`n_probed` / `n_validated` is 0, or `separation` is null, then no prose anywhere
in the bundle may say validity was proven, established, demonstrated or shown.
State the status positively instead ("this rubric is unprobed"), in the prose AND
as a machine-readable field. Check the same for kappa (null below the minimum-n
gate is "not reportable", never "0" and never omitted), for expert review, and
for any measured difficulty that carries `measured: false`. Pricing may be
stated, but a price is never evidence of validation — label it as list price.

STEP 5 — TEXT A MACHINE CONSUMES MUST READ AS AN INSTRUCTION
Rubric criteria are read literally by an LLM judge. For every criterion assert:
  - it is a grammatical sentence (auto-generation from an error tag produces
    things like "A correct answer never unsafe recommendation.");
  - a negative criterion states what an answer must NEVER do — a criterion worth
    negative points that is phrased as an instruction to do the thing will be
    mis-awarded;
  - `tier` and |points| agree (critical 8-10, important 4-7, helpful 1-3);
  - positives sum to `max_points`, and `n_positive` / `n_negative` are right.
Rewording a criterion for clarity is allowed and must preserve its meaning
exactly. NEVER edit a physician's free text — `rationale`, `stance`,
`approach_notes` and reviewer prose ship as written, typos included; they are
provenance, and rewriting them fabricates the annotator.

STEP 6 — VOCABULARIES MUST COVER WHAT THE BATCH ACTUALLY CONTAINS
data_dictionary.md is the buyer's decoder. For every enum field in the records
(`source`, `portal_version`, `case_source`, `record_type`, `modality`,
`distribution`, `walk_mode`, status vocabularies), assert the dictionary lists
the value this batch actually carries. A dictionary that stops at v2 while the
records are v4 tells the buyer the batch is something it is not.

STEP 7 — BOILERPLATE MUST APPLY TO THIS BATCH
Any paragraph that describes a condition (related-party annotators, longitudinal
trajectories, image assets, held-out answer keys, ratified seed corpora) must
either apply to this batch or say plainly that it does not. Check the flag before
believing the paragraph: a related-party disclosure on a bundle where every
record is `related_party: false` reads as a disclosure of something.

STEP 8 — READ IT AS THE BUYER
Open every buyer-facing document and read it end to end. Flag internal jargon in
a buyer-facing field (`note: "Admin export cut"`), any file the orientation text
fails to mention, empty or contradictory sections, and any claim you would be
embarrassed to defend on a call.

OUTPUT
A table: finding | file | what the records say | severity | fix. Then a verdict:
SHIP or DO NOT SHIP. Rank by what a technical buyer would catch first — anything
checkable in one command (hashes, a field value, an arithmetic sum) is the most
damaging, because they will run it.

FIXING
Fix the EXPORT PATH that produced the bundle, then rebuild and re-run this
review. Do not hand-patch a zip: a hand-edited bundle is unreproducible and the
next export carries the same defect. If a deadline forces a patched bundle, say
so explicitly in your report and open the generator fix in the same breath.
Whenever a bundle is rebuilt or patched, recompute the content hashes LAST,
after every other file is final — hashing before the last write is how a
manifest and a bundle disagree.
```

---

## Why each step exists

Every one of these is a defect that shipped in `exp-20260903-040304-036795`, the
single-case nephrology sample cut for Centaur, and every one passed
`/export-audit`.

| Step | What shipped | Why it costs you |
|---|---|---|
| 1 | `datasheet.md` and `quality_report.md` did not match their own hashes | Two red lines on the buyer's first integrity check |
| 2 | Datasheet asserted `source: internal_prompt_bank`; every record said `partner_ehr` | A one-command contradiction between doc and data |
| 3 | `QA pass rate 1.0 (37/37)` in a 4-record batch — a store-wide number | Reads as an inflated batch statistic |
| 4 | `EVAL_PACK.md` said validity was "proven at package time"; `validity_report.json` said 0 probed, 0 validated, `separation: null` | Selling a proof the bundle disproves |
| 5 | `"A correct answer never unsafe recommendation."` and a −5 criterion phrased as an instruction to transfuse | The judge reads these literally; mis-scoring |
| 6 | `data_dictionary.md` defined `portal_version` as v1/v2; records were v4, and `source` omitted `partner_ehr` | The decoder does not decode the batch |
| 7 | Related-party disclosure on a bundle with `related_party: false` everywhere | Discloses a conflict that does not exist |
| 8 | `note: "Admin export cut"` in the buyer's manifest | Internal jargon on the artefact of record |
| 0 | `export_audit.py` exit 2: no license, no specialty, no portal_version in `batch.json` | The bundle failed Archangel's own CI gate, unrun |

## Where the generator fixes belong

Steps 2, 3 and 6 are template bugs, not export-run bugs, and will recur on every
future bundle until the generator is fixed:

- `backend/asclepius/export.py:737` — the datasheet hardcodes
  `` `source: internal_prompt_bank` `` in a section that fires whenever a record
  carries a `generation` block, including `partner_ehr` records
  (`_synthetic_records`, `backend/asclepius/export.py:648`).
- `backend/asclepius/export.py:1157` — the QA pass rate comes from store-wide
  `stats` and prints inside the per-batch report.
- The `portal_version` and `source` rows of `_data_dictionary_md`
  (`backend/asclepius/export.py:533`, `:536`) predate v3/v4/v5 and `partner_ehr`.

The manifest gap behind Step 0 is a generator bug too: `batch.json` gets a
`licensing` block only when an export is cut against a licensed key
(`backend/asclepius/export.py:2275`), so an unlicensed cut names no terms at all
even though every record carries `license` and the datasheet states it. Specialty
and portal version are likewise recorded only indirectly — in `scope` and in
`counts.by_portal_version` — which is not where a consumer or the audit script
looks. The manifest should state all three plainly, derived from the records.

Steps 1, 4, 5 and 7 are worth adding as assertions to
`backend/scripts/export_audit.py` so they fail CI rather than a buyer's inbox.
