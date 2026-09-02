# Longitudinal end-to-end — the §4 check, run

The *Longitudinal end-to-end* PRD §4 lists eight steps of the routing and
notification path, says "nothing to build — everything below exists", and asks
for each one to be **run** rather than read, with the result recorded here and
any failure fixed and cited by `file:line`.

This is that record. Every row below is an executed test in
`backend/tests/test_longitudinal_e2e_routing.py`, driven against a trajectory
produced by the real generation route (`POST /ingestion/cases/{id}/generate` with
`trajectory: true`) — not against hand-inserted rows.

```
cd backend && python3 -m pytest tests/test_longitudinal_e2e_routing.py -q
10 passed
```

(Eight rows, plus the two edges of the row-3 fix — see below.)

**Running them found one real defect and one wording ambiguity.** Both are
recorded below; the defect is fixed.

---

## The eight rows

| # | Step | Result | Enforced at |
|---|---|---|---|
| 1 | Batch counts | **PASS** | `store.batch_overview` |
| 2 | Preview | **PASS** | `real_cases.build_encounter_case` + `assert_temporal_split` |
| 3 | Solo send | **FIXED** — see below | `store._PRD_CB_DISTRIBUTION` + `routers/asclepius._require_distribution` |
| 4 | Relay send | **PASS**, with a wording note | `routers/asclepius_admin.admin_send_relay` |
| 5 | Unlock ping | **PASS** | `route_notify.notify_relay_unlock` |
| 6 | Reveal | **PASS** | `GET /tasks/{id}/trajectory-outcome` |
| 7 | Stall / reassign | **PASS** | `POST /batches/relay/{id}/reassign` |
| 8 | Retired point | **PASS** | `store._PRD_2_RETIRED_SQL` |

### 1 · Batch counts

After generation the Longitudinal batch reports **1 trajectory, N points, N
unrouted**, and `/admin/batches/longitudinal` returns exactly the walk's task ids
with contiguous `sequence_index` 0…n−1.

The batch read `0 trajectories · 0 points` for as long as it did because nothing
had ever been generated — the counting was never wrong. That is the whole
diagnosis of this PRD, and it is now visible as a passing assertion rather than
as an absence.

### 2 · Preview shows no future

For every point *k*, the admin preview payload
(`GET /admin/batches/preview/{task_id}`) carries no `collected_offset_days > 0`.
Checked on the **admin** surface as well as the physician's, because truncation
is a server responsibility and the preview is served by the same builder.

### 3 · Solo send — **a real leak, found by running the row**

The assignee sees point 0 only; nobody else sees anything in the queue. That half
always held.

**The by-ID half did not.** The distribution gate lived only in the queue SQL
(`store._PRD_CB_DISTRIBUTION`). So an `assigned_only` trajectory point
that had been generated and **not yet sent** was:

* invisible in every queue — correct, and the reason nobody noticed;
* and simultaneously **openable, revealable and submittable by task id** by any
  real-data-approved physician.

Point 0 of a walk clears the sequence gate by construction — there are no earlier
points — so nothing else stood in the way. Reproduced before the fix:

```
queue  : []          ← invisible, as designed
by id  : 200         ← should have been 403
reveal : 200
submit : 200
```

The damage is not only disclosure. Trajectory points are single-labelled
(`TRAJECTORY_MAX_LABELS = 1`), so a stranger's submission **consumes the point's
one label**: the walk an admin was about to send is silently taken, the intended
physician is then blocked by capacity, and the Batches screen still shows the
walk as unrouted.

**Fixed** by `routers/asclepius._require_distribution`, applied to the same
by-ID paths the sequence gate covers — `get_task`, `reveal_task_answers`,
`get_task_answers` and `submit`.

*(Cited by SYMBOL, not by `file:line`. The first version of this document used
line numbers and five of the eight were stale one merge later — a citation that
rots on every merge is worse than none, because it sends a reader to the wrong
code with confidence.)* It returns **403**, not the sequence
gate's 409: this is an authorization failure in the ordinary sense, and telling a
physician to "complete the earlier points" would be false. Admins and QA are
exempt, as on the V4 wall.

This is the same lesson the sequence gate already had a comment about — *"a
queue-only fix is not a fix"* — applied to the one gate that had not learned it.

**Three things the fix itself had to get right**, each of which would have been a
new defect:

* **Order.** The sequence gate runs FIRST on all four paths. Run the other way
  round, a relay doctor opening a point that is somebody else's turn is told
  "this case was not sent to you" — false, because the walk *was* sent to them,
  just not that point. `test_asclepius_relay` has asserted the accurate message
  since before either gate existed. The ordering closes the same set either way:
  point 0 of an unrouted walk clears the sequence gate by construction and is
  caught by the distribution gate; every later point is caught by the sequence
  gate anyway. Only the message differs, and the more specific one should win.
* **Roles.** Reads admit `label` or `review` — a reviewer opens the case to review
  it. Writes admit `label` only, matching `store._PRD_ASSIGN_MINE` exactly.
  Otherwise a reviewer could bank a label on a case the queue would never have
  offered them, consuming a single-labelled point's one slot.
* **Work in flight.** Routing a case to specific doctors flips it to
  `assigned_only`, and an admin may do that while somebody is halfway through it.
  Holding an independent commit is a carve-out, so their blind capture is not
  lost. It cannot be abused: a commit exists only by passing through `/reveal`,
  which is itself behind this gate.

### 4 · Relay send

Three doctors, a rotation fixed by `seed` so the mapping shown in the dry run is
byte-identical to the one committed, `walk_mode='relay'` stamped on every point,
`distribution` left at `assigned_only`, and one DM per doctor
(`notified: {"dms": 3, "errors": []}`), verified by reading the messages out of
the community store rather than trusting the endpoint's own report.

**Wording note.** §4 row 4 says "private channel created, DM per doctor". There is
no *shared* relay channel where the three doctors can see each other; the private
channel is the system↔doctor DM (`route_notify._dm_one` →
`get_or_create_dm(SYSTEM_USER_ID, doctor)`), one per doctor. Read that way the row
passes as written, and it is recorded here rather than resolved by inventing a
group channel nobody asked for. If a shared per-walk channel is wanted, it is a
feature, not a missing gate.

### 5 · Unlock ping

Doctor k+1's DM count strictly increases when doctor k submits. Fired on the
predecessor's submit — the moment the relay gate starts letting the next point
through — so the message and the availability are the same event rather than a
sweep noticing later.

*(A helper bug worth naming, because it would have made this row vacuous:
`community.store.list_messages` returns `(rows, has_more)`, so `len()` of the
result is always 2 and every "a DM was sent" assertion passes unconditionally.
The helper unpacks the pair.)*

### 6 · Reveal

For every non-terminal point the outcome carries `days_after_decision > 0` —
**strictly** after, because an event at day 0 is the decision, not its result. The
terminal point returns `outcome: null` with a reason naming it as the last
decision point rather than an empty object a client would render as "no change".

### 7 · Stall / reassign

Reassigning a relay point revokes the stalled doctor's assignment
(`status='revoked'`), offers it to the replacement, and DMs the replacement.

The 24-hour nudge itself is **not asserted to send**, and that is deliberate
rather than an omission: `route_notify` ships it OFF (it is the only thing in the
product that messages a physician on a timer with nobody deciding to). The row
covers what an operator actually does — reassign — and the nudge's own gate is
tested where it lives.

### 8 · Retired point

With point 0 answered and point 1 retired (`status='void'`), the queue offers
point **2** and never point 1. Without the retired-status clause a removed point
blocks every later point forever, for everyone, silently — the queue simply stops
offering them.

---

## What the §4 run did NOT cover

* **Model quality.** The four model legs (question authoring, the frontier
  difficulty probe, candidate generation, the two judges) are stubbed, with the
  real return shapes. These rows prove the wiring and the gates; they say nothing
  about whether a generated question is good.
* **The 24-hour nudge actually sending** — see row 7.
* **Live email.** Notifications are DMs in the community store. No mail transport
  is exercised.

## §5.4 definition of done — where it is deliberately not met

The PRD's §5.4 asks that

```
grep -rn "'v5'\|\"v5\"" backend/ frontend/ --include=*.py --include=*.js | grep -v tests
```

return "only Group A/B sites with longitudinal meaning; zero hits mention
'environment' or 'agentic' next to `v5`".

Every code site now carries longitudinal meaning. **Six sites still mention `v5`
next to the environments tier, and they are deliberate**, not a missed rename:

* `constants.ENV_LEGACY_PORTAL_VERSION` and `is_env_portal_version(allow_legacy=)`
  (`constants.ENV_LEGACY_PORTAL_VERSION`, `constants.is_env_portal_version`);
* the two env-route guards that opt into it (`asclepius_env.annotation_queue`,
  `asclepius_env.annotate_environment`);
* the §5.2 migration and its boot log
  (`store.migrate_portal_versions_for_longitudinal`, and its caller in
  `main.startup_team_scheduler`), which cannot describe an env→`env` re-stamp
  without naming the old literal.

The first four are one release of back-compat: a page cached before the rename
still posts `portal_version: 'v5'`, and refusing it would 400 an annotation a
physician has already typed, over a string their browser stops sending on reload.
It is accepted only on env routes, never on anything that decides what is
written, and the value stored is always `env`. **Delete after one deploy cycle**,
and the grep is then clean.

## The suite, after all of this

Run as CI runs it — four shards, `python3 scripts/ci_shard.py <n> 4`:

```
shard1: 5 failed, 1164 passed
shard2: 1489 passed
shard3:  925 passed, 1 skipped
shard4: 1213 passed, 1 skipped
```

**4791 passed, 2 skipped, 5 failed.** All five failures are pre-existing and
unrelated to this work, and both claims were checked rather than assumed:

* `test_llm_model_constraints` (2) — reproduce on `origin/main`, where that file
  fails far more broadly. Nothing here touches the model-constraints table.
* `test_triage_demo` (3) — pass in isolation, repeatedly; they fail only under a
  four-way parallel run. They exercise `triage_demo_seed`, a subsystem no file in
  this branch touches, and they assert on seed staleness, which is time-sensitive
  by construction.

An earlier run of the same four shards caught four failures that WERE this
branch's — two in `test_asclepius_relay` (the gate ordering, above) and two DOM
tests that clicked Send without previewing. Both are fixed; that is what the
second run confirms.

## Related

* `LONGITUDINAL_CASES.md` — the operator runbook and the density gate.
* `backend/asclepius/fixtures/patient_bundles/README.md` — the four real charts,
  their measured yield, and the one that quarantines.
* `backend/tests/test_longitudinal_v5_relabel.py` — the §5 relabel (V5 =
  longitudinal, ENV = environments), including the V4/V5 queue partition that row
  3's queue assertions depend on.
