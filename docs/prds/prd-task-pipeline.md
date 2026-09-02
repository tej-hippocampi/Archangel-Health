# PRD: Task pipeline hardening (group D)

Difficulty gate ON, per-case group-DM rooms, per-physician roster metrics.
Ships in PR-3 alongside the payments rail (group G, its own PRD).

## Problem (from the meeting)

Three gaps between what the meeting requires and what runs today:

1. **The difficulty gate is built but off.** The meeting's core economics: a task
   is only valuable if frontier models fail it. The full measurement pipeline
   exists (`empirical_difficulty.py`) but ships dark, so every case currently
   serves on a DECLARED difficulty from the hardness-judge proxy, and the relaxed
   multimodal gates admit cases below the quality floors. We are generating
   inventory whose central value claim is unmeasured.
2. **Routed doctors work alone.** The meeting wants a per-case room: the 2
   labelers + 1 reviewer, introduced to each other, with founders able to step
   in. Today routing sends each doctor a solo DM from the bot and nothing
   connects the three people on one case. Relay reassignment is documented as
   silently invisible to the rest of the chain
   (`docs/asclepius/CASE_BATCHES_AND_ROUTING.md`, "What is NOT built", lines
   309-312).
3. **The roster answers "who", not "how fast or how consistently".** The meeting
   wants per-task metrics on people: labeling speed and QA quality. Both numbers
   exist in the backend and neither reaches the admin roster.

## Decisions

**Locked (founder meeting + planning session):**

- Per-case rooms are **group DMs, not channels**. `CASE_BATCHES_AND_ROUTING.md`
  §8.5 decided against private case channels and named this exact alternative:
  extending the DM model past two participants buys the shared space without
  touching channel listing, search, unread counts, or the digests, all of which
  assume channels are public. That reasoning stands; we build the alternative it
  endorsed, not the thing it rejected.
- The room is for **coordination and introductions only**. §8.5's premise stays
  true: the case itself stays in the portal and case CONTENT discussion is
  forbidden. The room's pinned intro says so explicitly.
- Difficulty gate rollout is **staged**: measure-only first, then require.

**Made here, with rationale:**

- **D1. Measure-only soak before the hard gate.** Flipping
  `ASCLEPIUS_REQUIRE_MEASURED_DIFFICULTY=1` on day one would gate serving on a
  distribution we have never observed with live keys. Stage 1 turns on
  measurement so every new case carries a measured value; stage 2 turns on the
  requirement once the measured pass rate has been reviewed. The gate itself
  already enforces the honest number: `passes_gate` uses the Wilson LOWER bound,
  not the point estimate (`empirical_difficulty.py:239`), so with the default
  k=2 draws across 2 models a case needs to fail decisively to ship.
- **D2. One room per routed order, keyed on the order, not the member set.**
  Reassignment changes the members; a room keyed on membership would fork into a
  second room on every substitution and strand the history. The room is
  get-or-create on a `case_ref` (batch id for a standard send, trajectory id for
  a relay), so a roster change posts into the same room.
- **D3. Case rooms are admin-visible; ordinary DMs stay private.** The community
  router deliberately gives admins no read access to private conversations
  (`backend/community/router.py:544`). The meeting wants founders able to step
  in, so `kind='case_room'` rows are an explicit exception, and the intro copy
  tells participants that Archangel admins can see the room. Ordinary two-party
  DMs keep their existing privacy unchanged.
- **D4. The room does not touch kappa blinding.** Independence of the two labels
  is guaranteed by the pre-reveal blind commit (`agreement.blinding_of_pair`,
  `backend/asclepius/agreement.py:184`), not by the labelers being strangers.
  The intro names people and the case type or specialty only, never content, so
  the blind commit remains valid evidence. The residual risk (content discussed
  in the room despite the rule) is mitigated by admin visibility (D3) and by
  room creation being logged as an event, so a suspect pair can be checked.
- **D5. Roster metrics are computed in batch, never per row.** The roster
  endpoint already refuses per-row recomputation for contributor score
  (`backend/routers/asclepius_admin.py:826-831`, "compute is a query per
  submission"). Median seconds and kappa follow the same rule: one SQL pass
  each, joined into the roster in memory.
- **D6. Unknown renders as unknown.** `evaluator_median_seconds` returns `None`
  until a physician has any timed submission (`store.py:6180`); per-physician
  kappa is `None` below the minimum pair count. Both surface as `null` in the
  API and as the roster's existing null placeholder in the UI, never as zero. A
  zero reads as a bad physician rather than an unmeasured one; this file
  convention already exists on the roster and the money screen.

## Requirements

### A. Difficulty gate

- **A1.** Stage 1 (this PR's deploy): `ASCLEPIUS_MEASURE_EMPIRICAL_DIFFICULTY=1`
  on Railway and documented in `.env.example`. Every newly generated case
  carries a `generation['empirical_difficulty']` block with `measured=True`
  whenever the frontier keys are reachable.
- **A2.** Stage 2 (a config flip after the soak, no code change):
  `ASCLEPIUS_REQUIRE_MEASURED_DIFFICULTY=1` and
  `ASCLEPIUS_RELAX_MULTIMODAL_GATES=0`. The serving gate then refuses any case
  not live-measured at or above the floor, and the strict multimodal quality
  floors (necessity >= 0.8, hardness >= 0.75, coherence, ground truth) apply
  again in full.
- **A3.** The stage-2 review is a written check, not a feeling: measured
  distribution over at least 20 stage-1 cases, the below-floor discard rate, and
  the projected token cost per SHIPPED case (see cost note below). Recorded in
  the PR that flips stage 2.
- **A4.** No behavior change to the graceful degrade: with no reachable frontier
  key, measurement returns `measured=False` and the case keeps its declared
  value (`empirical_difficulty.py:13-16, 212-219`); under stage 2 such a case is
  held, not served, which is the gate working as designed.
- **A5.** `.env.example` gains the three flags with the staged-rollout comment;
  `docs/DEPLOY_BACKEND_RAILWAY.md` env table gains the same.

**Cost implication, stated plainly:** measurement spends real frontier tokens on
every generated case: `baseline_models()` (default 2, one OpenAI + one
Anthropic, `constants.py:891`) times `empirical_difficulty_attempts()` (default
k=2, `constants.py:654`) frontier answers, plus one judge call per answer, so
roughly 8 LLM calls per case, on full multimodal case prompts. Under stage 2 a
below-floor case is discarded AFTER that spend, so with a floor of 0.5 the
effective cost per shipped case is the measurement cost divided by the pass
rate. That is the product working as intended (we only sell what frontier
models fail), but it is a real per-case bill that scales with generation volume,
which is why stage 2 waits for the A3 numbers.

### B. Group-DM case rooms

- **B1.** Schema (additive, in `community/store.py` `_migrate` style): a
  `community_dm_members(dm_id, user_id, added_at, removed_at)` join table, plus
  nullable `community_dms.kind` (default `'dm'`) and `community_dms.case_ref`
  columns with a unique index on `case_ref`. Existing two-party rows are
  backfilled into the members table from `user_a`/`user_b` at migration. The
  `UNIQUE (user_a, user_b)` constraint and `get_or_create_dm`
  (`community/store.py:837`) keep working unchanged for two-party DMs.
- **B2.** New store API: `get_or_create_case_room(case_ref, participant_ids)`
  (race-safe on the `case_ref` unique index, same ON CONFLICT pattern as
  `get_or_create_dm`), `room_participants(dm_id)`, `add_room_participant`,
  `remove_room_participant` (sets `removed_at`; history stays readable to
  remaining members). `list_dms_for` (`community/store.py:863`) is extended to
  include rooms via the members table; for `kind='case_room'` the summary
  carries a `title` instead of `peer_user_id`.
- **B3.** Router: the participant-membership checks on every DM path
  (`community/router.py:1119-1258`: list, post, read, typing) resolve membership
  through the members table, so rooms ride the existing message pipeline (PHI
  gate, reactions, edit and soft delete, read cursors, audit) with no second
  code path. Posting into a room requires current (not removed) membership.
- **B4.** Auto-create on routed send: `route_notify.notify_routed`
  (`route_notify.py:149`) and `notify_relay_send` create or reuse the room for
  the send's `case_ref` with the assigned labelers + reviewer, and the Archangel
  bot (`SYSTEM_USER_ID`) posts the intro: participant first names with roles,
  the case TYPE and specialty only, the independence rule ("the case itself
  stays in the portal; do not discuss case content here"), and the
  admin-visibility sentence (D3). Room creation failure is reported in the
  existing notify report and never fails the send, matching `notify_routed`'s
  never-raises contract.
- **B5.** Reassignment posts a roster-change notice. The relay reassign endpoint
  (`routers/asclepius_admin.py:2385`, notify at 2434) adds the replacement to
  the room, marks the departed member removed, and the bot posts "Dr. X now has
  point N" into the room. This closes the documented gap: today the replacement
  is DMed and nobody else is told the roster changed
  (`CASE_BATCHES_AND_ROUTING.md:309-312`). The existing solo DM to the
  replacement stays; the room notice is additive.
- **B6.** Send-to-all (open queue, no assignments) creates no room: there is no
  roster to introduce. Rooms exist only for explicit routed sends and relays.
- **B7.** Room creation and membership changes are logged via the existing
  community audit path, with `case_ref` in the payload (supports D4).

### C. Per-physician roster metrics

- **C1.** New batch store methods: `evaluator_median_seconds_by_user()` (one
  query over `submissions`, same median definition as the existing per-user
  `evaluator_median_seconds`, `store.py:6180`) and
  `evaluator_kappa_by_user()`: per-physician Cohen's kappa over that
  physician's kappa-eligible agreement observations, reusing `cohens_kappa`
  (`agreement.py:264`) and the SAME eligibility gates as the pooled number
  (`_blinded_only` at 228, `_pool_eligible` at 237). A per-physician kappa
  computed over rows the pooled kappa excludes would be a different metric
  wearing the same name.
- **C2.** Each kappa result carries its `n` (pair count) and is `None` below
  `kappa_min_n()` (`agreement.py:69`), for the same reason the aggregate gates:
  a kappa on 3 pairs is noise presented as measurement.
- **C3.** `GET /api/asclepius/admin/physicians` (`asclepius_admin.py:776`) adds
  three fields per row: `median_seconds`, `kappa`, `kappa_n`. Backend lands in
  this PR.
- **C4.** UI: two columns on the "Approved and Labeling" roster tab in
  `admin_physicians.js`: "Median time" (rendered as minutes+seconds) and
  "Agreement" (kappa to two decimals with n on hover or beside it). The
  admin-tasks redesign that just landed (`docs/asclepius/ADMIN_TASKS_REDESIGN.md`)
  did not touch the Physicians roster, so the columns land on the current
  two-tab roster; they follow its stated vocabulary rules (words not raw
  tokens; null renders the roster's null placeholder, never a zero, per D6).
  If PR-4's admin separation is mid-flight when this merges, the columns are
  carried into the moved roster rather than duplicated.
- **C5.** No metric is ever shown to a physician. These are admin roster fields;
  the internal-score-stays-internal rule from the meeting extends to speed and
  agreement numbers.

## What exists today (verified in the working tree)

- `backend/asclepius/constants.py:660` `measure_empirical_difficulty_enabled()`
  default OFF; `:646` `require_measured_difficulty()` default OFF; `:694`
  `relax_multimodal_gates()` default ON; `:638` floor 0.5; `:654` k default 2;
  `:891` `baseline_models()` two ids, one per provider.
- `backend/asclepius/empirical_difficulty.py:127` full measurement: k draws per
  model at temperature 0, both-axes judging (wrong answer OR unsound reasoning),
  span-verified judge verdicts, Wilson interval, `passes_gate` on the lower
  bound (line 239), graceful `measured=False` degrade.
- `backend/community/store.py:206-214` `community_dms` schema (strictly
  two-party, `UNIQUE (user_a, user_b)`); `:837` `get_or_create_dm`; `:863`
  `list_dms_for` (assumes a single peer).
- `backend/asclepius/route_notify.py:140` `_dm_one` (bot-to-doctor solo DM);
  `:149` `notify_routed`; `:485` `notify_reassigned` (DMs the replacement only).
- `backend/routers/asclepius_admin.py:2385` relay reassign endpoint; `:2742`
  the routed-send `notify_routed` call site; `:776` the roster endpoint (has
  `contributor_score`, has no speed or kappa fields).
- `backend/asclepius/store.py:6180` `evaluator_median_seconds` (per-user, no
  batch variant); `:708` agreement rows are one per double-labeled task.
- `backend/asclepius/agreement.py:325` `aggregate_kappa` reports overall and
  by-specialty only; nothing per-physician.
- `docs/asclepius/CASE_BATCHES_AND_ROUTING.md:280` §8.5 decision record; `:297`
  the group-DM alternative; `:309` the silent-reassignment gap.

## Gaps / changes per file

| File | Change |
|---|---|
| `backend/asclepius/constants.py` | none (flags exist); comments updated to name the staged rollout |
| `.env.example`, `docs/DEPLOY_BACKEND_RAILWAY.md` | A1/A2/A5 flag documentation |
| `backend/community/store.py` | B1 schema + B2 room API + B3 membership resolution + `list_dms_for` extension |
| `backend/community/router.py` | B3 membership checks via members table; case-room admin visibility (D3) |
| `backend/asclepius/route_notify.py` | B4 room create + intro on routed and relay sends; B5 roster-change post in `notify_reassigned` |
| `backend/routers/asclepius_admin.py` | B5 add/remove membership on reassign; C3 roster fields |
| `backend/asclepius/store.py` | C1 batch methods |
| `backend/asclepius/agreement.py` | C1 per-physician kappa helper (reusing existing gates) |
| `frontend/asclepius/admin_physicians.js` | C4 two roster columns |
| `frontend/asclepius/community.js` | render rooms in the conversation list (title, members) |
| `docs/asclepius/CASE_BATCHES_AND_ROUTING.md` | §8.5 addendum: the endorsed alternative is now built; strike lines 309-312 from "not built" |

## Email / notification touchpoints

- No new email. Rooms are in-app; the existing routed-send DM and email
  notification paths are unchanged.
- The room intro and roster-change notice are community messages authored by
  `SYSTEM_USER_ID`, so they ride the existing unread and digest plumbing that
  DMs already have. No case content ever appears in them, so nothing new
  reaches the email digest surface.
- Stage-2 flip is config only; no notification changes.

## Test plan (plain pytest functions with WHY docstrings, repo style)

- `test_difficulty_gate_stages.py`
  - `test_measure_only_never_blocks_serving`: WHY: stage 1 must be a pure soak;
    a case below floor with require off still serves.
  - `test_require_on_refuses_unmeasured_case`: WHY: stage 2's whole point; a
    declared-only case must not serve when the flag is on.
  - `test_gate_uses_wilson_lower_bound_not_point_estimate`: WHY: a 0.5 point
    estimate on 4 attempts has a lower bound far below floor; passing it would
    ship a claim diligence cannot support.
- `test_case_rooms.py`
  - `test_room_created_once_per_case_ref`: WHY: reassignment must not fork the
    room; two sends for one order share one room (D2).
  - `test_room_intro_names_no_case_content`: WHY: §8.5's forbidden-content rule
    is the condition under which rooms were approved at all.
  - `test_reassignment_posts_roster_notice_and_swaps_membership`: WHY: closes
    the documented silent-DM gap; the old member loses posting rights.
  - `test_two_party_dms_unchanged`: WHY: the migration backfill must not alter
    existing DM behavior, privacy included.
  - `test_admin_can_read_case_room_but_not_private_dm`: WHY: D3 is an exception
    scoped to `kind='case_room'`; widening it to DMs would break a stated
    privacy property.
  - `test_room_failure_does_not_fail_send`: WHY: `notify_routed` never raises;
    routing must not become hostage to community availability.
- `test_roster_metrics.py`
  - `test_median_seconds_none_without_timed_submissions`: WHY: zero reads as a
    bad physician; unknown must stay None (D6).
  - `test_kappa_none_below_min_n_and_matches_pool_gates`: WHY: a per-physician
    kappa over pool-excluded rows would be a different metric under the same
    name (C1/C2).
  - `test_roster_endpoint_is_batch_not_per_row`: WHY: the roster's own comment
    forbids a query per physician; assert query count stays flat as the roster
    grows.

## Out of scope

- Flipping stage 2 in this PR (it is a config flip gated on the A3 review).
- Rooms for send-to-all or open-queue pickups (B6).
- Any physician-facing metrics display (C5).
- Member-scoped channels of any kind (§8.5's rejection stands).
- The relay stall nudge (`ASCLEPIUS_RELAY_NUDGE_ENABLED` stays off; §8.7).
- Synthetic-augmentation research questions from the meeting (research item,
  not build).
