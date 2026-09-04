# The four committed patient records

Four de-identified longitudinal charts, as the partner exported them. They are
the *input* to the longitudinal pipeline — the thing that was missing for as long
as the Longitudinal batch read `0 trajectories · 0 points`.

Read `asclepius/patient_fixtures.py` for the door they go in through, and
`docs/asclepius/LONGITUDINAL_CASES.md` for what the pipeline does with them.

## Why they are here rather than in `tests/fixtures/`

Because they are not test data. `generate` — the only code path that creates a
trajectory — takes an **ingest case**, i.e. a chart that came in through
`/ingestion`. These charts had never been uploaded, so no ingest case existed for
them, so no trajectory could ever be built from them. The three hand-authored
"REAL · STATIC V4" cases in `v4_cases.py` were written *from* patients 1, 3 and 4,
but writing a case from a chart is not the same as feeding the chart to the
pipeline, and the gap between those two things is the whole reason this directory
exists.

They are committed as **directory trees, not zips**. `*.zip` is gitignored (the
repo prefers source commits), and a tree is reviewable: a reader can check the
de-identification claim below by reading the notes, which is not true of an
opaque archive. `patient_fixtures.pack_bundle` packs a tree into the bytes a
partner would have posted — sorted names, fixed timestamps, `ZIP_STORED` — so the
pack is deterministic and the sha256 idempotency key means something.

## De-identification

Each bundle carries its own `README.md` stating what the exporter removed: no
names, phone numbers, MRNs, national IDs, emails, addresses, or facility/provider
names; synthetic patient ids; ages over 89 bucketed; clinical dates shifted
consistently so longitudinal intervals survive.

That claim is not taken on trust. Every bundle runs the shipped
`deid_verify.verify_deid` hard guard on the way in, and three of the four pass it
clean. The fourth does not, and it does not for a reason worth reading:

## Measured yield — 55 encounters → 22 decision points → 18 verifiable

Measured on these exact trees, through the shipped ingestion path, and pinned by
`tests/test_longitudinal_front_door.py` so a gate change cannot move it quietly.

| bundle | specialty | encounters | decision points | verifiable | status |
|---|---|---|---|---|---|
| patient-1 | hepatology | 22 | **13** | 12 | ingested |
| patient-2 | oncology | 16 | 2 | 1 | **quarantined** — see below |
| patient-3 | hepatology | 5 | 4 | 3 | ingested |
| patient-4 | cardiology | 12 | 3 | 2 | ingested |

`LONGITUDINAL_CASES.md` quotes **59 → 25 → 21** for these four charts. That figure
was inherited from the PRD and measured elsewhere; what this repository's code
does to this repository's copies of the charts is 55 → 22 → 18. The difference is
three encounters and three decision points, and it is **not** gate drift:
patient-1's thirteen-point walk — the number the product is demoed on — reproduces
exactly. Treat 55/22/18 as the reproducible figure and the published one as
provenance.

If a change makes these numbers move, read §2 of `LONGITUDINAL_CASES.md` before
deciding it is progress. (patient-3 is declared **hepatology** — decompensated
HCV cirrhosis with renal monitoring; the chart's own signal splits hepatology and
nephrology almost evenly, which is why the declaration matters. See
`docs/asclepius/CASE_GENERATION_CHECK.md` for the run that measured it.) Lowering the event floor raises `decision_points` (the
number quoted in a pitch) and leaves `verifiable_decision_points` (the number that
matters) alone — which is precisely how a lowered gate misleads.

## Why patient-2 quarantines

Its OCR annotation reads:

> `MEDICATION FORM. Date column header ~21/06/2026 (written as 12/26).`

`12/26` is a date-shaped token in **clinical text**. It is not in a
de-identification header — those are stripped before any scan, and 134 of them
were — so the timeline normalizer sees an ambiguous date it cannot anchor, and
ingestion quarantines rather than guessing. That is the rule working: a wrong
guess turns a recoverable quarantine into a silently wrong offset, and a wrong
offset destroys the clinical meaning of every value around it.

**Do not relax the date scan to admit this chart.** The documented, audited path
is `POST /ingestion/quarantine/{ingest_case_id}/override` with a written reason —
the hard `deidentify()` guard still runs and still cannot be overridden. A human
reading the sentence above can see that `12/26` and `21/06/2026` are the same
date; no general rule can.

## Adding another bundle

1. Drop the tree in as `<name>/`.
2. Add `<name>` to `v4_cases.FIXTURE_BUNDLE_SPECIALTIES`. Packing refuses an
   unmapped bundle rather than defaulting it to `general`.
3. Add its measured yield to `_EXPECTED` in
   `tests/test_longitudinal_front_door.py`.

`ASCLEPIUS_PATIENT_FIXTURE_DIR` points the loader at a different root — a mounted
volume, for bundles too large or too sensitive to commit. Same shape: one
directory per bundle.

## What is in the archive, and what is not

`patient_fixtures.pack_bundle` packs each `patient-N/` tree into the exact bytes a
partner would have posted — **except this file and the per-bundle READMEs**, which
are excluded by name (`patient_fixtures._NOT_CLINICAL_DATA`).

Two reasons, either sufficient:

* `ingestion._classify` reads `.md` as `note_text` — a clinical note. Packed in,
  `patient-1/README.md` was ingested as that chart's 369th note, and its *Clinical
  synopsis* paragraph names the outcome. It never reached a physician
  (`real_cases._visible` fails closed on an undated item, so it was dropped from
  every decision point), but a documentation file should not be one timestamp rule
  away from a decision point, and it should not be counted as a dropped clinical
  note in the "omitted for unknown timing" number an admin reads.
* `.dockerignore` strips `*.md` from the build context, so the deployed image and
  a developer's checkout packed **different bytes** — and that sha256 is the
  idempotency key in `ingest_committed_bundles`. Excluding the documentation makes
  the archive reproducible across environments, not just across two calls on one
  disk.

Measured both ways: the encounter counts are identical (22 / 16 / 5 / 12 = 55), so
no yield number in this file moves. Pinned by
`test_longitudinal_front_door.test_the_bytes_are_the_same_in_the_deployed_image`.
