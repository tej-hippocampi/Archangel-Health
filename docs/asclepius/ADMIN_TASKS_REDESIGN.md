# Admin Tasks redesign — Data & Task Creation, Task Routing

**What changed:** the admin `Tasks` tab became two pages that mirror how work
actually moves — *data comes in and becomes tasks*, then *tasks get previewed and
sent to doctors*. QA and Metrics are untouched. Every existing task and upload
was carried across; nothing was deleted, truncated or re-keyed.

`Admin → Tasks → {Data & Task Creation · Task Routing · QA · Metrics}`

The sub-tab **state keys are unchanged** (`tasks` and `assign`). Deep-link
aliases, `openBatchesFor` and the physician-row route-in all read them, and
renaming them would be silent breakage for zero benefit — the same reasoning that
kept `work` and `money` when those tabs were relabelled.

> **Where these pages live now.** PRD-F moved the admin console out of the
> physician bundle onto its own page (`/asclepius/admin`), so both pages and the
> shell that mounts them are in `frontend/asclepius/admin_shell.js` rather than
> `asclepius.js`. Behaviour is unchanged, the state keys above are unchanged, and
> the tests in §7 read the renderers from the new file with every assertion
> intact.

---

## §1 The hard invariant

No task, upload, ingest case, submission, assignment or trajectory row is
deleted, truncated or re-keyed. Enforced three ways:

1. No `DELETE` and no destructive migration. The three new columns
   (`tasks.display_bucket`, `ingest_uploads.description`,
   `ingest_uploads.task_mode`) are additive and NULLable.
2. `test_admin_tasks_redesign_migration.py` snapshots the **id set and count** of
   `tasks`, `submissions`, `assignments`, `ingest_cases` and `ingest_uploads`
   around a re-run of the real `_migrate`, and asserts both identical. A
   token-level scan (comments stripped) asserts `_migrate` contains no
   destructive SQL.
3. Old UI cards were removed; **their endpoints stay live**, asserted by calling
   each one in `test_admin_tasks_redesign_endpoints_live.py`.

---

## §2 The display bucket

`tasks.display_bucket` answers "where did this task come from" in one word:
`longitudinal_real`, `static_real`, `physician_authored`, `synthetic`.

It is a **cache of a derivation**, and three decisions make that safe:

**One derivation, shared.** `store.derive_display_bucket()` is called by
`insert_task`, by `update_task_case`, and by the boot backfill. It is
deliberately the same grouping `batch_overview` does in SQL. Two spellings of
"which batch is this in" disagree eventually, and then the Routing rail and the
task list describe the same task differently with no way to tell which is right.

**Gold is read off `source`, not `generation_json`.** The obvious predicate is
`json_extract(generation_json,'$.mode')`, and it is wrong twice over. The literal
`'gold'` is written nowhere — `gold_cases.py` writes `'gold_seed'` — so every
physician-authored case would silently classify as synthetic. And the corrected
literal still breaks: `set_task_candidates` merges a patch into that block and
"Grade real" rewrites `mode` to `'grade_real_models'`, so grading a gold case
against the frontier models would reclassify it. `source` is never rewritten
after insert.

**Order is load-bearing.** Every longitudinal point is *also*
`case_source='real_deid'`, so a rule testing `real_deid` first would empty the
longitudinal rail. Trajectory wins.

**Drift is tested, not assumed.** `case_source` is not immutable —
`update_task_case` re-derives it — so the bucket is refreshed there, and a test
re-derives **every row in the database** and asserts the stored value matches. A
cache nobody re-derives is a cache that is wrong and cannot be caught.

The backfill runs *after* the `case_source` backfill in the same migration, so it
reads the corrected value rather than the legacy NULL.

---

## §3 Page 1 — Data & Task Creation

Two boxes.

**Box 1 · Incoming data** — every upload whose purpose is undecided
(`staging === 'undecided'`). Provider, specialty, integrity, ingest counts, and
the sender's own description of what the bundle is. Two buttons: `Task creation`
and `Brokering`.

> **The brokering button is a one-way door.** The server refuses
> `brokering → task_creation` with a 409, permanently, because data a partner
> sent us to broker must never enter the task pipeline. So Brokering **confirms**
> and says what the click costs. `Task creation` does not confirm — it only ever
> removes a promotion path, and a dialog on the reversible half is what teaches
> an operator to click through the one that matters.

`staging` is a real third state. `effective_purpose` resolves NULL to
`task_creation` for *promotion*, but the admin has not answered yet, and Box 1
exists to ask.

**Box 2 · Task creation** — uploads cleared for task creation with cases still to
convert. `Static` runs the existing two-step promote (`prepare` → `promote-all`);
`Longitudinal` runs `/ingestion/cases/{id}/generate` with `trajectory: true`,
dry-run first. The choice is stored on the upload (`task_mode`) so a half-finished
batch resumes the same way and the row is self-describing tomorrow. It is refused
on a brokering upload (a control that can never act) and after the first task
exists in a different mode (it would relabel rows built the other way).

Finished bundles **fold**, they do not vanish — a row that disappears on
completion takes its history with it.

**Upload** (top right) — one modal, one required "what is this?". Real records go
through the **partner door** (mint a link, post to it), not a second admin-only
ingest endpoint: that door fails closed on unconfigured encryption and on
non-durable storage, and a second door would have to reproduce both exactly or
quietly become the unsafe way in. Gold is a button, not a file drop —
`load-gold` loads committed fixtures and takes no file.

---

## §4 Page 2 — Task Routing

Three columns, all visible at once: **rail** (what is ready) · **list** (what it
is, previewable) · **panel** (who gets it). The old shape made you navigate
between the parts of a single decision.

The panel is **context-sensitive**, which is the point of the re-cut:

| selection | offered |
|---|---|
| nothing | a one-line hint, and no controls at all |
| standalone cases | all / specialty / specific doctors, each named doctor with a **role** |
| one whole trajectory | solo walk or relay, and nothing else |

Per-doctor role rides in `roles` on the allocate payload. `assignments.role`
always carried `label` and `review`; the explicit-send builder hardcoded
`'label'`, so an admin who chose "Reviewer" got a labeler and no sign of it. A
name absent from the map is a labeler — what every explicit send meant before.

Naming a doctor without the reviewer tier is **refused at send, dry run
included**: `review.can_review` gates that queue, so the row could never have
been served. Same shape as the V4 wall check beside it.

Gold cases carry a `physician-authored` **chip in the synthetic rail** rather than
moving to Real·static: `batch_overview` counts them as synthetic (their
`case_source` is not `real_deid`), and a fourth rail item would disagree with the
backend's three.

Everything the server owns, it still owns. The implied predecessor set is
resolved by `/resolve-selection`, the send goes through `allocate`, and this
client contains no comparison of sequence indices anywhere.

---

## §5 What was removed, and what moved instead

Removed as cards: Paste tasks (JSON) · Generate candidates · Seed corpus ·
Generation jobs · the old Tasks table · Load gold cases · Load REAL V4 cases.
**Every endpoint behind them is still live and tested.**

Moved rather than dropped:

* **Frontier-model failures** → Metrics. It is a measurement, not a creation step.
* **"Grade real" and the two-frontier provenance chips** → the Routing preview.
  They lived only in the old Tasks table, and dropping them would have removed
  the only way to see a HELD `needs baseline` task.
* **Synthetic generation** → one compact card under Box 2, with no jobs table:
  its output is a task the moment it exists, so the status links to Task Routing
  rather than growing a second inventory.

`renderAdminAssign` (the pre-Batches allocator) was already unreachable before
this work — no caller. It stays that way; its endpoint is the one Routing uses.

---

## §6 Deviations from the PRD as written

Each of these was a case where building the spec literally would have shipped a
defect.

| PRD says | Shipped | Why |
|---|---|---|
| gold via `generation_json.$.mode = 'gold'` | `source = 'gold_seed'` | the literal matches nothing, and the corrected one is rewritten by "Grade real" |
| brokering is recoverable "from the Data tab" | irreversible, with a confirm | the server 409s that transition on purpose |
| Preview cases → `/uploads/{id}/review` | `/ingestion/uploads/{id}` | `/review` omits cases with no review reason, so a clean bundle opens an empty drawer |
| static preview via `promote-all` `dry_run` | `prepare` → `promote-all` | `UploadPromoteRequest` has no `dry_run`; `prepare` is the sample step |
| gold "enters as an upload row" | a button in the Upload modal | `load-gold` takes no file; a file input for it controls nothing |
| admin real-record upload lands in Box 1, purpose unset | lands in Box 2, mode set | the modal's mode picker already declared intent; Box 1 would ask a question just answered |
| V4 fixture bundle surfaced as an upload row | not surfaced | it is not staged data awaiting a decision — it auto-seeds at boot and is already tasks |
| `physician_authored` renders in Real·static | chip in the synthetic rail | `batch_overview` counts gold as synthetic; a fourth rail would disagree |

---

## §7 Tests

```
test_admin_tasks_redesign_migration.py       §1 invariant, bucket derivation, drift
test_admin_tasks_redesign_api.py             staging, task_mode, per-doctor role
test_admin_tasks_redesign_ui.py              both pages EXECUTED against the DOM shim
test_admin_tasks_redesign_endpoints_live.py  every removed card's endpoint still 200s
```

The UI tests **run** the renderers rather than grepping them. This repo has twice
shipped a section that was complete, correct and invisible because nothing
mounted it, and source-only frontend tests are blind to that.

Two guards here were mutation-checked after being written, and one was **vacuous
in two separate ways** before it was right — it set `process.env.TZ` inside the
script (V8 caches the zone at startup, so the assignment did nothing) and used an
eastern zone (which makes a task look *older* and hides the defect). If you edit
the `new`-chip guard, re-verify it fails against `Date.parse`.
