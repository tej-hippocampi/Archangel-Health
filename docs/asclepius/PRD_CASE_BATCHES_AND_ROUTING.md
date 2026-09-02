# PRD — Case batches on admin Tasks, doctor routing, and the longitudinal gate

**Three deliverables, one invariant.** (1) The admin Tasks tab shows the three case
classes as batches with preview and send. (2) Sending routes cases to chosen doctors,
who see them next after finishing their current case. (3) Longitudinal cases merge to
main **without ever appearing on a doctor portal until sent from admin** — the doctor
portal continues to serve exactly what it serves today (V4 real cases).

Verified against `Archangel-Health-main (25)` and `longitudinalcaseschanges.zip`.

---

## §0 What already exists — build on it, do not duplicate it

### 0.1 The assignment system is LIVE on main

- `assignments` table (`store.py:1997`), `upsert_assignment` (`store.py:2964`),
  race-safe re-offer semantics.
- `POST /assignments/allocate` (`asclepius_admin.py:1978`) — **dry-run defaults TRUE**,
  proposes then commits. `GET /assignments` (:2045), `POST /assignments/{id}/revoke` (:2059).
- **The queue already serves assigned cases first.** `_PRD_ASSIGN_MINE` (`store.py:244`)
  and the priority order (`store.py:247`):
  ```sql
  ORDER BY <assigned-to-me> DESC, <label_count> DESC, t.created_at ASC
  ```
  As the store's own comment says: *"an assigned case sorts to the TOP of its
  assignee's queue and changes nothing else."*

**Consequence: the requested "continue case → my routed case populates right after"
behaviour ALREADY WORKS.** When a doctor finishes a case, the next draw serves their
assigned cases before anything else. Do not build a second delivery mechanism — §3 only
adds *targeting* (which doctors) and §5 adds the ping.

- An `Assign` subtab already renders (`asclepius.js:8126`, `renderAdminAssign` :8148).
  This PRD **replaces** its contents with the batch flow rather than adding a fourth
  competing surface.

### 0.2 What the allocator CANNOT do today — the actual gap

`AllocateBody` (`asclepius_admin.py:1903`) carries `task_ids`, `labels_per_case`,
`max_share`… but **no way to name doctors**. `allocate()` picks physicians
algorithmically. The ask requires explicit targeting: these doctors / this specialty /
everyone. That is §3.

### 0.3 The longitudinal branch does NOT gate visibility — the merge landmine

Nothing in `longitudinalcaseschanges.zip` prevents trajectory points from entering the
open queue: merged as-is, every approved doctor's next draw could serve a longitudinal
case. **§2 must land in the same PR as (or before) the longitudinal merge.**

The branch DOES already enforce the sequence seal everywhere (SQL gate + 409 on all
four by-id paths + a test asserting the client contains no sequence logic). Do not
touch any of that.

### 0.4 Existing pieces to reuse

- Preview: `GET /export/case-preview` (`asclepius_admin.py:274`) renders a case body.
- Community DM: `get_or_create_dm` (`community/store.py:837`),
  `insert_message` (:496), routes at `community/router.py:1109-1209`.
- System author: `SYSTEM_USER_ID = "u-system"` (`community/system_posts.py:33`),
  `post_system_message` (:64) for channel posts.
- Class discriminators already on every task row: `trajectory_id` (longitudinal),
  `case_source = 'real_deid'` (V4 real), else V3 synthetic (`source`/gold).

---

## §1 Data model — one new column

```python
# store.py migration, additive, house style (no DEFAULT in ALTER for decision columns
# is the rule for NULLable decision columns; here a backfill IS the decision):
if "distribution" not in cols("tasks"):
    conn.execute("ALTER TABLE tasks ADD COLUMN distribution TEXT NOT NULL DEFAULT 'open'")
```

- `'open'` — today's behaviour: any eligible doctor's queue can serve it.
- `'assigned_only'` — served **only** through an assignment row. Never appears in an
  unassigned doctor's queue, count, or dashboard.

Enforcement lives in the ONE servable predicate both `next_task_for_evaluator` and
`eligible_tasks_for_evaluator` already share:

```sql
AND (t.distribution = 'open' OR <assigned-to-me EXISTS clause>)
```

Add it beside `_PRD_ASSIGN_MINE` so there is exactly one copy. A task that is
`assigned_only` with zero assignments is INVISIBLE to doctors and shows on admin with
an "unrouted" chip — that is the correct resting state for longitudinal work, not a bug.

**Longitudinal tasks are created with `distribution='assigned_only'`.** Set it at the
trajectory-promotion write in the longitudinal branch's promote path. V3/V4-static
creation paths pass nothing and inherit `'open'` — zero behaviour change.

Dashboard counts (`/dashboard`, hero subtitle) must use the same predicate so a doctor
is never told "3 cases available" including ones they cannot draw.

---

## §2 Admin Tasks tab — the batch view

Replace `renderAdminAssign` (`asclepius.js:8148`). Subnav stays
`Tasks · Assign · QA · Metrics`; rename `Assign` → **`Batches`**.

### 2.1 Level 1 — three batch cards

```
┌───────────────────────────┐ ┌───────────────────────────┐ ┌───────────────────────────┐
│ LONGITUDINAL V4           │ │ REAL · STATIC V4          │ │ SYNTHETIC V3              │
│ 2 trajectories · 17 pts   │ │ 3 cases                   │ │ 41 cases                  │
│ 17 unrouted               │ │ open queue                │ │ open queue                │
└───────────────────────────┘ └───────────────────────────┘ └───────────────────────────┘
```

Backend: `GET /api/asclepius/admin/batches` returning per-class counts:
longitudinal (`trajectory_id IS NOT NULL`, grouped by `trajectory_id` with point counts
and per-trajectory routed/unrouted), real-static (`case_source='real_deid' AND
trajectory_id IS NULL`), synthetic (everything else with status open). One query each,
no N+1.

### 2.2 Level 2 — inside a batch

A table of cases: checkbox · case id · specialty · difficulty band · status
(`unrouted / routed → Dr. X / in open queue / labeled n/m`) · `Preview`.

**Longitudinal grouping:** rows group under their trajectory header
(`patient-1 · 13 points`, collapsible, with a select-all-in-trajectory checkbox).
Point rows show `#0 … #12` in sequence order.

**Point-level selection is allowed — with implied predecessors.** (Per your decision.)
Selecting point #5 alone would strand it: the sequence seal (longitudinal branch,
`_PRD_2_SEQUENCE_GATE`) refuses to serve #5 to a doctor who hasn't completed #0–#4.
So the UI treats a point selection as *"this point plus its unfinished predecessors"*:

```
☑ #5  (+5 required predecessors will be included)
```

The send payload carries the full implied set. The server RE-DERIVES the implied set
and refuses a payload that omits predecessors (400, naming them) — the client is not
trusted with ordering, which is the branch's own stated rule.

### 2.3 Preview — the doctor's-eye view

`Preview` opens a read-only render of the case **using the same shared case-panel
module the doctor portal uses** (the labeler's `renderCasePanel` — same module the
reviewer PRD §2.2 extracts; if that extraction hasn't landed, extract it here and the
reviewer PRD consumes it). Above it, the exact prompt text; below it, the eyebrow
`PREVIEW — read-only · exactly what Dr. sees at labeling`. Data comes from
`GET /export/case-preview` (`asclepius_admin.py:274`) extended to accept a `task_id`;
for a longitudinal point the preview shows the truncated window ONLY — the server
truncation rule applies to admin preview too, because a screenshotted future in a
Slack thread is the same leak as a served one.

### 2.4 Send

A sticky footer bar appears when anything is selected:

```
[ 3 cases selected ]   Send to: (•) All approved doctors  ( ) Specialty ▾  ( ) Specific doctors ▾
                                                       [ Preview send ] [ Send ]
```

- **All** → per your decision: cases become/stay `distribution='open'` (for
  longitudinal this flips `assigned_only → open` deliberately and the UI says so:
  *"Longitudinal cases sent to All enter the open queue — any eligible doctor may
  draw them"*), and the ping (§5) goes to every approved doctor in the case's
  specialty scope.
- **Specialty** → assignment rows for every approved doctor with that specialty.
- **Specific doctors** → a picker fed by the same `/admin/physicians` payload as the
  Approved and Labeling tab (§PRD-ADMIN): name, specialty, tier. Multi-select.
- `Preview send` calls allocate with `dry_run=true` (the endpoint's default — reuse it)
  and renders the proposal: who gets what, what could not be assigned and why.
- `Send` commits: extends `POST /assignments/allocate` with
  `user_ids: Optional[List[str]]` and `specialty: Optional[str]` on `AllocateBody`.
  When `user_ids` is given, `allocate()` is bypassed — the admin's explicit list IS the
  allocation (still respecting `labels_per_case`: 3 doctors + `labels_per_case=2` means
  first-come among the three, stated in the confirm dialog).

### 2.5 Routing from the physician side (Approved and Labeling tab)

Add a `Route cases` action per physician row that deep-links into Batches with that
doctor pre-selected in the Specific picker. One flow, entered from either end.

---

## §3 The doctor experience — nothing new to build, one thing to verify

Delivery is the existing assigned-first sort (§0.1). Verify, do not rebuild:

- Doctor finishes their current case → next draw serves the assigned case. Already the
  sort order.
- Doctor mid-case with a draft → **the draft is never interrupted.** Continue resumes
  their in-progress case; the routed case appears after submission. This is existing
  behaviour (assignment affects the NEXT draw only); the DM copy (§5) must say it
  correctly rather than promising instant replacement.
- Longitudinal `assigned_only` point invisible until assigned — new predicate, §1.

---

## §4 The community ping

### 4.1 Mechanism

System-authored **DM** per targeted doctor (your decision), plus a channel post to
`#task-announcements` only for **Send to All**.

- Reuse `get_or_create_dm` (`community/store.py:837`) + `insert_message` (:496) with
  `SYSTEM_USER_ID` as author. **Check the self-DM guard only** (`user_x == user_y`
  raises); u-system is a distinct id so it passes. If `insert_message` enforces
  membership, u-system is already a member (it posts digests).
- Idempotency: one DM per (doctor, send-event), never per case. A send of 5 cases to
  one doctor is ONE message listing 5.
- The ping fires AFTER assignment rows commit, in the same request, best-effort: a DM
  failure logs and does not roll back the assignment (the assignment is the truth; the
  ping is a courtesy — same rule as `mark_community_welcomed`'s "safe failure" comment).

### 4.2 The message

```
New cases routed to you

Dr. {last_name} — {n} new {class_label} case{s} just landed in your queue:

  · {specialty} · {difficulty} — {class_label}
  · …

They'll appear automatically: if you're mid-case, finish it — your routed case
comes up right after you submit. If you're starting fresh, just hit Start new case.

{longitudinal_only_paragraph}
Longitudinal cases walk one real patient forward in time. You'll commit to an
assessment, a plan, and what you expect to happen next — then the chart's next
encounter is revealed and you check your own prediction against what actually
happened. Take them in order; each point unlocks the next.

Questions mid-case? Post in #questions-help.
— Archangel
```

`{class_label}` ∈ `longitudinal · real de-identified · synthetic multimodal`. The
longitudinal paragraph renders only when the send includes trajectory points. No
deadline pressure language — assignments may carry `due_at`, and if set, one line:
`These are yours first until {date}.`

---

## §5 The merge-order invariant (the whole point)

1. **This PRD's §1 (distribution column + predicate) merges FIRST**, or in the same PR
   as the longitudinal branch with the promote path setting `assigned_only`.
2. Definition of done for the merge: `mockadmin`'s doctor-view portal and every real
   doctor portal show **exactly the same queue before and after** the longitudinal
   merge — V4 real cases, nothing else new. Assert it: snapshot
   `eligible_tasks_for_evaluator` for a test doctor pre/post merge in a migration test.
3. Longitudinal cases become visible to a doctor through exactly one path: an admin
   send from Batches.

## §6 Tests

```
distribution
  - default 'open' on every existing creation path (V3 gen, V4 promote, gold)
  - longitudinal promote writes 'assigned_only'
  - assigned_only + no assignment: absent from next_task, eligible_tasks, dashboard count
  - assigned_only + assignment for Dr A: visible to A, still absent for Dr B
  - flip to 'open' on Send-to-All: visible to all eligible

send targeting
  - user_ids bypasses allocate() and assigns exactly those doctors
  - specialty send resolves to approved doctors of that specialty at send time
  - point #5 selected alone -> 400 naming #0-#4  (server re-derivation)
  - implied-predecessor payload accepted; sequence gate still blocks out-of-order serve

ping
  - one DM per doctor per send, listing n cases
  - DM author is u-system; doctor can reply (thread opens to admin attention)
  - DM failure does not roll back assignments (assignment rows exist, error logged)
  - Send-to-All posts once to #task-announcements; targeted sends do not

doctor flow
  - doctor with in-progress draft: draft resumes; routed case served on NEXT draw
  - assigned case outranks open-queue cases in the draw (regression on _PRD_ASSIGN_MINE)

preview
  - longitudinal preview payload contains no offset beyond the point's end_offset
```

Suite: new file + `test_asclepius_longitudinal*.py` (branch) + existing assignment
tests + `node --check` on asclepius.js.

## §7 Do not touch

The sequence-gate SQL and 409 paths from the longitudinal branch · `_PRD_ASSIGN_MINE`
and the priority order (extend the WHERE, never the ORDER) · `allocate()`'s dry-run
default · draft/timer behaviour · `post_system_message` semantics · the κ exclusion.

---

## §8 RELAY MODE — distributed longitudinal walks

A second way to send a trajectory, alongside the solo walk. **One trajectory, N
doctors, one point each, walked in sequence as a care-team handoff.**

### 8.1 Why this is a different product, stated for the agent

The solo walk captures one physician's evolving judgment over a patient. Relay mode
captures something else: **how clinicians build on each other's reasoning** — doctor
k reads doctor k−1's committed assessment before writing their own, exactly like a
real handoff. Both are sellable; they are different rows in the catalog, and the
export must label which mode produced a record (`walk_mode` in the annex).

Two scientific consequences the agent must preserve:

- **Future-blindness is unchanged.** Truncation hides the future regardless of who
  labels. A relay doctor at point 5 sees encounters 0–5 and doctor 4's commitment —
  never the reveal, never anything past their window.
- **The κ concern inverts.** The solo walk excludes points from κ because one
  physician carries their own prior forward. In relay mode each point has a
  DIFFERENT physician — the shared-prior objection vanishes. Keep the κ exclusion
  for solo (`trajectory.kappa_exclusion_reason`), and do NOT extend it to relay
  points; instead exclude them for the ordinary reason (n=1 label per point, below
  the double-label floor). The exclusion *reason string* must differ, because a
  buyer reading the annex must see which rule applied.

### 8.2 The walk_mode column

```python
if "walk_mode" not in cols("tasks"):
    conn.execute("ALTER TABLE tasks ADD COLUMN walk_mode TEXT")  # NULL | 'solo' | 'relay'
```

Set on every point of a trajectory at SEND time (not promotion — the same trajectory
could in principle be cloned into both modes later; for now a trajectory is sent
once, and re-sending a sent trajectory is a 409).

**The sequence gate becomes mode-dependent — this is the one edit to branch code:**

- `'solo'` (and NULL): the existing rule, untouched — point k serves only to an
  evaluator who completed 0..k−1 **themselves**.
- `'relay'`: point k serves only to **its assigned doctor**, and only when points
  0..k−1 are **completed by anyone**. Both halves in SQL (`_PRD_2_SEQUENCE_GATE`
  gains a `walk_mode` branch) AND on the four by-id 409 paths. The client still
  contains no sequence logic — the existing test asserting that must keep passing.

### 8.3 The send flow (Batches → Longitudinal → a trajectory)

New option on the send bar when the selection is exactly one whole trajectory:

```
Send as:  ( ) Solo walk — one doctor takes the whole chart
          (•) Relay — one doctor per point, in sequence
```

Relay send screen:

1. Pick doctors. Picker shows approved doctors with specialty + current load.
   **n_doctors ≤ n_points required; if fewer doctors than points, assignment
   round-robins in the same random order** (13 points, 5 doctors → each doctor gets
   2–3 non-adjacent points where possible). n_doctors > n_points is a 400.
2. `Preview send` (dry_run, reusing the §2.4 machinery) shows the full mapping:
   `#0 → Dr. Faheem · #1 → Dr. Shafipour · …` — **random permutation, server-side
   seeded, shown before commit.** Admin can reshuffle or hand-edit single rows.
3. `Send` commits, atomically:
   - assignment rows for every (point, doctor) pair, `walk_mode='relay'` stamped
     on the points, `distribution` stays `assigned_only`;
   - the private case channel (§8.5) is created with all N doctors + u-system;
   - DMs go out (§8.6) — but **only doctor #0's point is serveable**. Everyone
     else's assignment exists but is blocked by the relay gate until their turn.

### 8.4 The handoff context (your "yes")

When point k is served to its relay doctor, the case panel gains one section,
rendered ABOVE the clinical question:

```
HANDOFF · Dr. {k-1 last name} at day {offset}
Assessment   {their committed assessment}
Plan         {their committed plan}
Expecting    {their committed expected trajectory / falsifier}
```

- Source: the predecessor's committed submission — **the commitment only, never
  their reveal outcome or self-score.** Server-side: the handoff block is built in
  the serve path from the predecessor submission's commit fields; the reveal fields
  are never serialized into it. A test asserts the served payload contains no
  reveal/self-score keys.
- Point #0 has no handoff block.
- The handoff text ships in the export under the point's record
  (`relay.handoff_from_sequence_index`, `relay.handoff_text`) so the buyer sees
  exactly what context the physician had.

### 8.5 The private case channel (new community primitive)

New channel type: **private, member-scoped**. Additive migration on channels
(`visibility TEXT NOT NULL DEFAULT 'public'`, `members` join table), created as
`case-{trajectory-short-id}` with the N doctors + u-system as members.

- Invisible to non-members everywhere: channel list, search, unread counts. The
  membership check lives in the store query, not the router, so a future surface
  cannot forget it.
- u-system posts the kickoff message (§8.6) and progress updates ("Point 4 of 13
  complete — Dr. Vadgama is up").
- **PHI rule unchanged:** the footer warning renders in private channels too, and
  the channel is for coordination — the case itself is only ever viewed in the
  portal. Doctors are told this in the kickoff.
- Auto-archive (read-only banner, still readable) when the last point completes.
- Guard: creating the channel is idempotent per trajectory (re-send after a partial
  failure must not make a second channel).

### 8.6 Messages

**Kickoff (channel, u-system):**
```
Care-team relay: {specialty} · {n} decision points

You {n} are walking one real de-identified patient forward in time, one
decision point each, in sequence. Each of you will see the chart up to your
point plus the previous physician's committed assessment — then commit your
own, see what actually happened next, and score your own prediction.

Order: {Dr. A → Dr. B → …}
Dr. {first} is up now. You'll each get a DM when it's your turn.
This channel is for coordination — the case itself stays in the portal.
```

**Turn DM (u-system → the doctor whose point just unlocked):**
```
You're up, Dr. {name}

Point {k+1} of {n} on the {specialty} relay case is now yours. Dr. {prev}
just committed theirs — you'll see their assessment as your handoff.

It's live in your queue now: finish any case you're mid-way through and it
comes up right after, or hit Start new case.
```

**Assignment DM at send (everyone, including #0 — #0's says "you're up now"):**
states their point number, their position in line, and that they'll be pinged
when it's their turn. Reuses §4 idempotency rules.

**The unlock ping fires on predecessor SUBMIT** — same transaction boundary as the
reveal, best-effort like §4.1 (a failed DM never blocks the submit).

### 8.7 Stalls — 24-hour nudge, admin reassign

- A relay point that is serveable but unsubmitted for **24 hours** triggers ONE
  nudge DM ("Still with you, Dr. X — the relay is waiting on point 5"). One, not
  recurring; recurring nudges to volunteers reads as nagging. Implemented in the
  existing notification/digest sweep, not a new scheduler.
- The Batches relay view shows the chain: `#0 ✓ · #1 ✓ · #2 ● waiting 31h · #3 –`
  with the stuck point highlighted past 24h.
- **Reassign**: one click on the stuck point → picker (defaults to the other relay
  doctors) → revokes the old assignment (`/assignments/{id}/revoke`, exists),
  writes the new one, posts a channel update, DMs the new doctor. The nudge clock
  resets. Reassignment is recorded in the export provenance (`relay.reassigned: true`)
  because the handoff chain now has a substitution a buyer should see.

### 8.8 Tests (adds to §6)

```
relay gate
  - relay point k serves ONLY its assignee, and only after 0..k-1 submitted by anyone
  - solo gate unchanged: same-evaluator predecessor rule still enforced (regression)
  - client sequence-logic-absence test still passes
handoff
  - point k payload contains predecessor commit fields, NEVER reveal/self-score keys
  - point 0 has no handoff block
round-robin
  - 13 points / 5 doctors: all points assigned, adjacent points different doctors
    where n_doctors > 1
channel
  - private channel invisible to a non-member in list/search/unread
  - idempotent creation per trajectory
  - auto-archives on last submit
pings
  - unlock DM fires on predecessor submit, to exactly the next assignee
  - 24h nudge fires once, not daily
  - reassign: old assignment revoked, new doctor DMed, channel updated, export
    carries relay.reassigned
```

---

## §9 AUDIT — the longitudinal branch (`longitudinalcaseschanges.zip`) vs production

Every finding below was verified by execution or by reading the shipped code, against
`Archangel-Health-main (25)`.

### 9.1 CRITICAL — the branch predates the assignments feature and DOES NOT MERGE

Proven, not inferred:

```
$ git apply --check CHANGES.patch        # against main (25)
error: patch failed: backend/asclepius/export.py:326
error: patch failed: frontend/asclepius/asclepius.js:2057
```

The branch was cut at merge-base `4ff24f1`. Main has since landed **PRD-ASSIGN**:
`assignments` table, `_PRD_ASSIGN_MINE` (`store.py:244`), a REWRITTEN
`_PRD_R_PRIORITY_ORDER` (assigned-first, `store.py:247`), allocator endpoints, the
`Assign` subtab, and export/JS changes. The branch's store still carries the OLD
priority order (`ORDER BY label_count DESC, created_at ASC`) and has no `assignments`
anywhere.

**This cannot be resolved by accepting either side.** Take the branch's
`labeler_queue_sql` → the assignment feature silently dies (assigned cases stop
sorting first — the §3 delivery mechanism of THIS PRD). Take main's → the sequence
gate silently dies (the sealed future breaks). The merge must COMBINE them:

```python
clauses = [
    _PRD_R_SERVABLE,
    "NOT EXISTS (SELECT 1 FROM submissions sm ... sm.evaluator_id = ?)",  # param 1
    _PRD_2_SEQUENCE_GATE,                                                  # param 2
    f"{_PRD_R_LABEL_COUNT} < ...",
]
params = [evaluator_id, evaluator_id]
...
ORDER BY <_PRD_ASSIGN_MINE> DESC, ...                                      # param LAST
```

**The precise silent-failure risk is positional parameter binding.** SQLite binds `?`
in order of appearance across the WHOLE statement — WHERE params first, then the
`_PRD_ASSIGN_MINE` `?` inside ORDER BY **last**. Get the order wrong and nothing
errors: an evaluator_id lands where a float belongs, the queue quietly serves the
wrong ranking. Required test: a fixture with one assigned task + one trajectory +
one measured-difficulty filter active simultaneously, asserting the served order —
that exercises every placeholder in one query.

Same three-way resolution required in `export.py` (both sides appended annex sections
at :326 — keep BOTH: assignment provenance and the trajectory annex) and
`asclepius.js` (both edited the dashboard/task-card region — keep main's assign UI
AND the branch's trajectory step cards).

**Merge protocol:** rebase the branch onto current main with the real blobs (the zip
lacks them — `--3way` failed for that exact reason), resolve the three files above by
combination, then run BOTH suites: `test_asclepius_longitudinal*` AND the assignment
tests, plus the §6 regression `assigned case outranks open-queue cases`.

### 9.2 HIGH — a retired earlier point deadlocks the walk forever

`_PRD_2_SEQUENCE_GATE`'s inner query has **no status filter**:

```sql
SELECT 1 FROM tasks p WHERE p.trajectory_id = ... AND p.sequence_index < ...
```

`_outcome_point` (`routers/asclepius.py:2493`) explicitly anticipates holes — *"an
admin can retire a point later"* — and handles them for the REVEAL. The gate does
not. A retired/void/closed earlier point still counts as "earlier point without your
submission," so every later point becomes permanently unservable to everyone —
including the physician mid-walk. Silent: the queue just never serves them again.

Fix in the merge: the gate's inner `tasks p` query must exclude whatever status the
admin retire path writes (align the exact status list with `_PRD_R_SERVABLE`'s
vocabulary), plus a test: retire point 2 of 5 mid-walk → point 3 serves.

### 9.3 MEDIUM — an abandoned solo walk is orphaned by design; §8 is the remedy

At `max_labels = 1` (the trajectory default), once Dr. A submits point 0, no other
evaluator can EVER satisfy the gate for points 1+ (they cannot submit point 0 — it is
full). If Dr. A leaves, the remaining points are dead stock, invisible as a problem
anywhere in admin. This is the solo seal working as designed — but it needs the §8.7
chain view + reassign to be OPERABLE. Ship §8.7's stuck-chain surface for solo walks
too, not only relay.

### 9.4 Verified CORRECT — leave these alone

| What | Verdict |
|---|---|
| Gate SQL NULL handling | `sequence_index IS NOT NULL` guard is right — without it, SQL three-valued logic would SERVE an unpositioned row; the comment derives this correctly |
| Queue param binding (branch as written) | `[evaluator_id, evaluator_id]` matches clause order |
| Reveal window (`outcome_delta`) | Strictly `> decision_offset` (same-day data excluded — correct); rebased to "day +N"; **fail-closed** on missing offsets with the right rationale |
| Reveal ≠ gated | Deliberate and correct: reaching reveal requires your own stored commit, so re-applying the gate could only refuse a physician their own work |
| Terminal point | Named response ("last decision point"), not an empty panel |
| Holes in a walk | `_outcome_point` takes smallest-greater index, not `idx+1` — the point before a hole still verifies |
| Migration | Additive, no DEFAULT on decision columns (with the 40k-row back-stamp rationale), composite index for the hot-path NOT EXISTS, `expected_trajectory` on SUBMISSION not task (every walker authors their own falsifier) |
| κ exclusion | Single vocabulary: `agreement.py` imports the token from `trajectory.py` — reason string cannot drift from the writer |
| Client trust | 409 `trajectory_out_of_order` handled in JS; a test asserts the client contains NO sequence comparison |
| Self-score guard | 409 when no expectations were committed; score written to the physician's own submission |
| CI | `test_ci_sharding.py` updated for the new test files |

### 9.5 Deploy checklist (supersedes "merge and hope")

1. Rebase onto current main; resolve `store.py` / `export.py` / `asclepius.js` by
   COMBINATION per 9.1. No other files conflict.
2. Apply the 9.2 status filter + test in the same PR.
3. Land §1 of this PRD (`distribution` column) in the same PR — §0.3: without it,
   merged trajectory points enter every doctor's open queue on deploy.
4. Run: `test_asclepius_longitudinal*` + assignment tests + the all-placeholders
   ordering test + `node --check`.
5. Post-deploy smoke on staging: promote a 3-point trajectory → confirm it is
   INVISIBLE to a doctor account → send to one doctor → walk all three points →
   confirm reveal windows and the terminal message.
6. Only then: Batches UI (§2), relay mode (§8).
