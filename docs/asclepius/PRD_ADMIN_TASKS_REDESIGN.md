# PRD — Admin "Tasks" redesign: Data & Task Creation → Task Routing

**One sentence:** the Tasks tab becomes two pages that mirror how work actually moves —
*data comes in and becomes tasks* (Data & Task Creation), then *tasks get previewed and
sent to doctors* (Task Routing) — with QA and Metrics untouched, and **every existing
task and upload carried across with zero deletions.**

Verified against `Archangel-Health-main (27)`: `asclepius.js` 13,368 lines,
`renderAdminTasks` at :10621 (291 lines + helpers), `renderAdminBatches` :8851,
`renderAdminAssign` :9325, subnav at :8819.

---

## §0 The hard invariant — read first, test last

**No task, upload, ingest case, submission, assignment, or trajectory row is deleted,
truncated, or re-keyed by this PRD.** The 56 tasks on admin today are the same 56 rows
after; they change *where they are displayed*, never *what they are*.

Enforced three ways:
1. This PRD contains **no `DELETE` and no destructive migration**. New columns are
   additive and NULLable. Old UI cards are removed; the endpoints behind them stay.
2. A migration test snapshots `COUNT(*)` and the full id set of `tasks`, `submissions`,
   `assignments`, `ingest_cases`, `uploads` before and after, and asserts equality.
3. A one-time classification pass (§5) writes a **display bucket** for every existing
   task from columns it already has (`trajectory_id`, `case_source`, `generation_json`,
   `source`). It reads; it never writes anything but the new column.

If the agent finds itself writing a `DELETE FROM tasks`, it has misread this PRD.

---

## §1 What exists — reuse map (this is 80% plumbing, 20% new)

| Need | Already built | Where |
|---|---|---|
| Uploads list, download, review/quarantine | ✓ | `/ingestion/uploads*` (`asclepius.py:5316–5602`) |
| Per-upload purpose (task_creation / brokering / unset) | ✓ | `/uploads/{id}/purpose` (`asclepius_admin.py:496`) |
| Per-upload specialty | ✓ | `/uploads/{id}/specialty` (`:1616`) |
| Ingest case → **static** V4 task | ✓ | `/ingestion/cases/{id}/promote` (`asclepius.py:5867`), `promote-all` (`:6073`) |
| Ingest case → **longitudinal** points | ✓ | `/ingestion/cases/{id}/generate` with `body.trajectory=true` (`:6429`, `:6510`) |
| Synthetic generation V1–V3 | ✓ | `/generation/{specialty}`, `/topup`, `/jobs` |
| Physician-authored (gold) load | ✓ | `/generation/{specialty}/load-gold` (`:1564`) |
| Committed V4 fixtures load | ✓ | `/generation/load-v4-real-cases` (`:1411`) |
| JSON/CSV task upload | ✓ | `/tasks/upload-file` (`:1292`) |
| Batches: preview, send, relay, distribution gate | ✓ | `renderAdminBatches`, `/admin/batches*`, `store.py:309` |
| Assignment (labeler/reviewer per doctor) | ✓ | `assignments` table, `/assignments/allocate` with `user_ids` |

**What's genuinely missing (the new build):**
- A **staging concept** — "this data is destined for task creation but hasn't been made
  into tasks yet, and I haven't chosen static vs longitudinal." Today that decision is
  buried in a request body flag. §3 makes it a first-class, visible state.
- **Upload descriptions from the health system.** `POST /partner/uploads` accepts only
  `file` + token (`:5082`). There is no field for "what is this data." §3.1 adds one,
  and backfills from the org's intake answers (HS Onboarding PRD §3) where present.
- A unified **"where did this task come from"** display bucket (§5).

---

## §2 Information architecture

Subnav (`asclepius.js:8819`) becomes:

```
Data & Task Creation  ·  Task Routing  ·  QA  ·  Metrics
```

`QA` and `Metrics` render exactly what they render today (`renderAdminQa`,
`renderAdminMetrics` — untouched). `Tasks` and `Batches` are replaced by the two new
pages. `renderAdminAssign` (:9325, the pre-Batches allocator UI) is removed from the
subnav; its endpoint stays.

**Removed from the UI (endpoints preserved):** Paste tasks (JSON) · Generate candidates
· Frontier-model failures · Seed corpus · Generation jobs card · the old Tasks table ·
Load gold cases · Load REAL de-identified cases as standalone cards. Each of these
either folds into §3 or moves to the Metrics/QA surfaces where it belongs
(`Frontier-model failures` → Metrics, since it is a measurement, not a creation step).

Buyer/export cards live in `renderAdminBuyers` (:11127), a separate function — not in
scope, not touched.

---

## §3 Page 1 — Data & Task Creation

Two boxes. Nothing else on the page.

### 3.1 Box 1 — Incoming data

**What it lists:** every upload with `purpose IS NULL` (undecided), from every source —
health-system partner uploads, admin uploads via the Upload button, and the committed
V4 fixture loader (surfaced here as one synthetic "upload" row per fixture bundle so the
loader stops being a hidden button). One row per upload:

```
┌────────────────────────────────────────────────────────────────────────────────┐
│ St. Mary's Health · uploaded Aug 28 · 3 files · 412 MB · sha ✓                  │
│ Nephrology · "2019–2023 CKD cohort, structured labs + progress notes, deid'd   │
│              in-house (Safe Harbor)"                                            │
│ Provider intake: authority ✓ · deid in-house · notes+structured · ~4k pts/5 yr  │
│ 27 ingest cases · 2 need review · 0 quarantined                                 │
│                      [ Download ]  [ Preview cases ]  → [ Task creation ] [ Brokering ] │
└────────────────────────────────────────────────────────────────────────────────┘
```

- Provider name, specialty (editable inline via `/uploads/{id}/specialty`), file
  summary, integrity, ingest counts — all from `/ingestion/uploads` today.
- **Description**: new `uploads.description TEXT` + new optional field on
  `POST /partner/uploads` (`description: str = Form("")`) and on the admin Upload
  modal. For orgs onboarded through the HS PRD, the intake answers (authority /
  deid / export scope / scale) render as the "Provider intake" line — read from the
  org record, not duplicated.
- **Download** → existing `/ingestion/uploads/{id}/download`. **Preview cases** →
  existing `/ingestion/uploads/{id}/review` in a drawer.
- **The decision is the two buttons.** `Brokering` calls `/uploads/{id}/purpose`
  with `brokering` → row leaves this page and appears under Data → Systems (the
  existing Data tab already shows purpose-resolved uploads). `Task creation` sets
  `task_creation` → row moves to Box 2. Purpose is one-way from the admin's view but
  the existing endpoint still allows re-resolution from the Data tab if a mistake is
  made — nothing is destroyed by choosing wrong.

### 3.2 Box 2 — Task creation

**What it lists:** every upload with `purpose = 'task_creation'` whose ingest cases
have not all been turned into tasks. One row per upload, same header as Box 1, plus
the case-level state:

```
│ 27 cases · 25 eligible · 2 blocked (review)  ·  0 made into tasks                │
│ Make tasks as:  (•) Static real cases   ( ) Longitudinal real cases              │
│                 [ Preview one ]                      [ Create 25 tasks → ]        │
```

- **Static** → `promote-all` (`:6073`) with `dry_run` first; the preview drawer shows
  the existing sample case view. Commit inserts `case_source='real_deid'` tasks.
- **Longitudinal** → `/ingestion/cases/{id}/generate` with `trajectory: true` per
  eligible case, run as a background job (already how generation runs); the row shows
  progress "12 / 25 points built". Points land with `distribution='assigned_only'`
  (Batches PRD §1) — invisible to doctors until routed.
- The choice is stored on the upload (`uploads.task_mode TEXT`, `static|longitudinal`,
  NULLable) so a half-finished batch resumes with the same mode and the row is
  self-describing when you come back tomorrow.
- When every eligible case has a task, the row shows `✓ 25 tasks created · view in
  Task Routing →` and collapses. It does not disappear — history stays visible in a
  "Done" foldout at the bottom of the box.

### 3.3 The Upload button (top-right of the page — the only other control)

One modal: file drop + **required** mode picker:

```
What is this?   ( ) Real records — static    ( ) Real records — longitudinal
                ( ) Physician-authored cases (gold)    ( ) Task file (JSON/CSV)
Description     [ free text — what am I looking at?                     ]
```

- Real records → existing partner-upload ingest path, admin as provider, lands in
  **Box 1** with `purpose` unset (you still decide brokering vs task creation there —
  one flow, no special case), `task_mode` pre-filled from the picker.
- Physician-authored → existing `load-gold` behaviour, but wrapped as an upload row
  in **Box 2** with mode locked to static, so gold cases enter through the same door
  as everything else.
- Task file → existing `/tasks/upload-file`; the resulting tasks appear directly in
  Task Routing under the synthetic bucket (they already are tasks; nothing to stage).

### 3.4 Auto-generate (Synthetic V1–V3) — stays, as one compact card under Box 2

Specialty · count · Generate. Output goes **straight to Task Routing** (they are
tasks the moment they're generated) — no jobs table on this page; a one-line
"Generating 20 nephrology cases… 14 done" status that links to Task Routing.

---

## §4 Page 2 — Task Routing

This is `renderAdminBatches` (:8851) **promoted to a page and re-cut by the admin's
question**, which is always "what's ready to send, what does it look like, who gets
it." Three columns of work, left to right:

### 4.1 Left rail — batches (already exists, relabeled)

```
Longitudinal · real      4 trajectories · 41 points · 27 unrouted
Static · real            18 cases · 6 unrouted
Synthetic · V1–V3        34 cases · in open queue
```
Counts from `/admin/batches` (exists). Clicking a batch fills the center.

### 4.2 Center — the task list with preview

Rows grouped as today (trajectories collapse; static and synthetic flat). **Every row
has `Preview`**, which opens the doctor's-eye render (`renderCasePanel` — the shared
module; for a longitudinal point the truncated window only, per Batches PRD §2.3).
Status chip per row: `unrouted · routed → Dr. X · in open queue · labeled n/m`.

New tasks arriving from §3 land here with a `new` chip for 24h so you can find what
you just made.

### 4.3 Right panel — assignment (context-sensitive, this is the UX fix)

The panel changes shape by what's selected, because the logic differs:

**Static or synthetic selection:**
```
Send 3 cases
To:   ( ) All approved doctors   ( ) Specialty ▾   (•) Specific doctors
      ☑ Dr. Faheem      role: (•) Labeler  ( ) Reviewer
      ☑ Dr. Vadgama     role: ( ) Labeler  (•) Reviewer
Labels per case  [2]         [ Preview send ]  [ Send ]
```
Per-doctor **role** is the addition the ask requires — `assignments.role` already
exists (`label` / `review`); the UI just exposes it per row. Backend: `AllocateBody`
gains `roles: Dict[user_id, 'label'|'review']` alongside `user_ids`.

**Longitudinal selection (one trajectory):**
```
Send trajectory patient-1 · 13 points
Mode: ( ) Solo walk — one doctor, all points   (•) Relay — one doctor per point
[relay picker as built: N doctors, random order shown, reshuffle, per-point role]
[ Preview send ]  [ Send ]
```
Exactly the Batches PRD §8 flow, already implemented (`relayWalk`, `relaySeed`,
`/batches/relay/*`). Point-level selection with implied predecessors stays.

The right panel is empty with a one-line hint when nothing is selected. It never shows
controls that don't apply to the selection — that is the single biggest source of the
"fluff" today.

---

## §5 Migration — carrying the 56 tasks across (additive only)

Add `tasks.display_bucket TEXT` (NULLable). One idempotent backfill at boot, in
`_migrate`:

```sql
UPDATE tasks SET display_bucket = CASE
  WHEN trajectory_id IS NOT NULL                         THEN 'longitudinal_real'
  WHEN case_source = 'real_deid'                         THEN 'static_real'
  WHEN json_extract(generation_json,'$.mode') = 'gold'   THEN 'physician_authored'
  ELSE                                                        'synthetic'
END WHERE display_bucket IS NULL;
```

- `physician_authored` renders inside the Static · real batch with a chip, so gold
  cases are visible without a fourth rail item.
- **Read-only derivation** — the four source columns are never modified. If a task's
  bucket looks wrong, fix the CASE, re-run; nothing else moves.
- Uploads: any existing upload with `purpose` unset appears in Box 1; `task_creation`
  in Box 2 (with `task_mode` NULL → both radio buttons unselected, "choose a mode");
  `brokering` stays on the Data tab. **No upload row changes.**
- Test (the §0 invariant): snapshot ids + counts of the five tables before migration;
  run; assert identical; assert every task has a non-NULL bucket; assert the 56-row
  fixture DB yields exactly the counts the CASE predicts.

---

## §6 Design rules for this page pair (the anti-fluff contract)

1. **Two boxes on page 1, three columns on page 2. No additional cards.** A feature
   that needs a card goes to QA, Metrics, or Data — or doesn't ship.
2. Every row answers "what is it, where is it from, what happens next" in ≤3 lines.
3. One primary action per row/panel. Secondary actions are quiet text buttons.
4. State is on the object (chips), never in a separate status card.
5. Preview is always one click away and never navigates.
6. Design tokens: canvas `#eef0ef`, card `#fbfcfa`, ink `#1a1b1a`, lime for `new`,
   green only for physician-authored/routed-to-doctor states. Instrument Sans.
   `h()` hyperscript, zero innerHTML, orphan-class guard — house rules.

---

## §7 Tests

```
invariant: five-table id/count snapshot identical pre/post migration; no DELETE in diff
buckets:   fixture with trajectory/real/gold/synthetic tasks classifies as predicted
box 1:     purpose NULL uploads listed with provider, specialty, description, counts
           brokering → gone from page, present on Data tab; task_creation → Box 2
box 2:     static → promote-all dry-run then commit; longitudinal → generate trajectory=true,
           points assigned_only; task_mode persists; done rows fold, never vanish
upload:    every mode lands where §3.3 says; description stored; gold enters as upload row
generate:  synthetic output appears in Task Routing with `new` chip
routing:   right panel renders static vs longitudinal controls by selection;
           per-doctor role writes assignments.role; relay flow unchanged (regression)
removed UI: paste/seed-corpus/generation-jobs/load-gold/load-real cards absent;
           their endpoints still 200 (regression)
node --check asclepius.js
```

## §8 Do not touch

Promotion/generation internals (`promote_ingest_case`, `generate`, hardness/case-judge
gates) · the distribution gate and sequence gate · `_PRD_ASSIGN_MINE` · relay logic ·
QA and Metrics renderers · buyer/export renderers · any endpoint (remove UI, keep API).
