# Case batches, routing, and relay walks

How a generated case reaches a specific physician — and, for a longitudinal chart
walk, how it is held back until somebody decides it should.

Implements `PRD_CASE_BATCHES_AND_ROUTING`. Read
[`LONGITUDINAL_CASES.md`](LONGITUDINAL_CASES.md) first; this document assumes the
sealed-future rule it describes.

---

## The one-sentence version

A promoted chart walk is **invisible to every doctor until an admin sends it**,
and it can be sent two ways: one physician takes the whole chart (**solo**), or
one physician takes each decision point in turn, reading the previous one's
commitment as a handoff (**relay**).

---

## §1 `tasks.distribution` — the merge landmine, disarmed

| value | meaning |
|---|---|
| `open` | today's behaviour: any eligible doctor's queue may serve it |
| `assigned_only` | served **only** through an assignment row |

Every task that existed before this column is backfilled `open`, which restates
what was already true of it rather than asserting anything new. Longitudinal
points are created `assigned_only`.

**Why the column exists.** A trajectory point is an ordinary task row with two
extra columns. The labeler queue has no notion of "generated but not released",
so merging the longitudinal work without this would have put every point of every
promoted walk into every approved physician's queue on deploy. Nothing errors —
the queue is simply wrong about what exists.

`assigned_only` with **zero assignments is the correct resting state** of a
promoted walk: invisible to doctors, listed for admin as unrouted.

### It is orthogonal to assignment, not a restatement of it

Main's priority order already sorts an assigned case to the top of its assignee's
queue *while leaving it visible to everyone else* — an assignment is a priority,
not a permission. `distribution` is the switch that turns the same assignment
into a permission. Two switches, four states, and the one that matters is
`(open + assigned)`: a case can be routed without being hidden.

Both consult one definition of "assigned to me" (`_PRD_ASSIGN_MINE`), so the sort
and the filter cannot drift.

---

## §2 The Task Routing screen

`Admin → Tasks → Task Routing` (called *Batches* until the Admin Tasks redesign;
the sub-tab's state key is still `assign`). Three classes, grouped on
discriminators every task row already carries, so this can never disagree with
what the queue thinks a case is:

| class | discriminator |
|---|---|
| Longitudinal V4 | `trajectory_id IS NOT NULL` |
| Real · static V4 | `case_source='real_deid'` and no trajectory |
| Synthetic V3 | everything else |

### The client holds no sequence authority

Selecting point 5 of a walk requires points 0–4 — the sequence gate will not
serve 5 to a physician who has not done them, so an assignment written without
them sits unservable forever and reads as a broken product.

The obvious implementation computes that set in JavaScript. **It is not done that
way.** A client that knows how to order a walk is one somebody later trusts to
enforce the order, and the seal is then one hand-typed task id from being
defeated. `POST /admin/batches/resolve-selection` answers it server-side, using
the same function `allocate` refuses with, so the count previewed and the set
committed are one derivation. `test_the_client_never_enforces_the_sequence_itself`
scans the whole shipped client and holds the admin surface to the same standard as
the doctor's.

### Preview is the doctor's payload

`GET /admin/batches/preview/{task_id}` returns what `_blind_task` returns — the
serve path's own function. A preview that assembled its own view would be a second
definition of "what may be seen", and the first time the two drifted, admin would
render a future the portal correctly hides. **A screenshot of encounter 6 in a
Slack thread leaks the answer to decision 5 exactly as thoroughly as serving it.**

### Sending

`POST /admin/assignments/allocate`, `dry_run` defaults true. Three targeting
modes, mutually exclusive by validation because they mean different things:

| mode | who | distribution after |
|---|---|---|
| `user_ids` | exactly those doctors; `allocate()` is bypassed | `assigned_only` |
| `specialty` | approved doctors of that specialty, resolved **at send time** | `assigned_only` |
| `to_all` | nobody in particular — no assignment rows at all | `open` |

`to_all` on a longitudinal batch **un-seals the walk**. That is a real choice and
the right-hand routing panel says so before the click.

Since the redesign that panel is **context-sensitive**: a whole-trajectory
selection is offered solo-vs-relay and nothing else, a standalone selection is
offered the three targeting modes above, and an empty selection is offered a
one-line hint instead of controls that cannot act. Each named doctor also
carries a **role** (labeler or reviewer) that rides in `roles` on the allocate
payload; naming a doctor without the reviewer tier is refused at send, for the
same reason the V4 wall is — the assignment could never be served.

The V4 wall is enforced at send: naming a doctor explicitly is still not
permission to show them real patient data, and an assignment written past that
check could never be served.

---

## §4 The ping

One DM per doctor per **send**, never per case — thirteen DMs for a thirteen-point
walk is a reason to mute the sender. `to_all` posts once to `#task-announcements`
and DMs nobody.

Fired after the assignment rows commit, best-effort: the assignment is the truth,
the ping is a courtesy, and a community outage must not roll back routing the
queue is already honouring. The endpoint returns what actually went out, so an
admin sees `0 DMs` on screen rather than finding out from a physician.

The copy does **not** promise an interruption. An assignment affects the *next*
draw, so it says "finish the one you're on — yours comes up right after".

---

## §8 Relay mode

**One trajectory, N doctors, one point each, in sequence.**

A different product from the solo walk, not a variant. Solo captures one
physician's judgment evolving over a patient; relay captures **how clinicians
build on each other's reasoning** — doctor *k* reads doctor *k−1*'s committed
assessment before writing their own, exactly like a ward handoff. The export
labels which mode produced a record, because the rows are otherwise identical and
they are priced and analysed differently.

### The gate inverts

| mode | predecessors | whose turn |
|---|---|---|
| `solo` (and `NULL`) | answered **by this evaluator** | anyone eligible |
| `relay` | answered **by anyone** | **only the assignee** |

`NULL` reads as solo — the stricter rule — because every walk that existed before
relay did was a solo walk.

**Both halves of the relay rule are enforced in the gate itself.** Leaning on
`distribution='assigned_only'` for the turn-taking happens to work today, and a
seal that depends on another switch's current value is not a seal: one admin
flipping a relay walk to `open` would unseal it entirely. A test flips it and
asserts the order still holds.

Enforced in SQL (`_PRD_2_SEQUENCE_GATE`) **and** on the four by-id 409 paths, so
the queue and the URL cannot disagree about whose turn it is.

### The handoff

Above the clinical question, because it is read before deciding:

```
HANDOFF · the physician at decision 3
Assessment            biliary obstruction — stent it
Expecting             bilirubin falls within 14 days
Would change my mind  if GGT climbs the stent has occluded
```

**The commitment only.** Never their reveal outcome, never their self-score —
those are what this physician is being asked to predict, and shipping them turns
the relay into reading comprehension. `store.relay_handoff` selects the commit
columns by name so a future column cannot ride along, and it reads the **blind
pre-reveal commit** rather than the post-reveal submission, which had already been
influenced by the candidate answers.

The author is named by **position**, not identity: labelers are blinded to each
other everywhere else, and "the physician before you on this chart" is the whole
clinically relevant fact.

Point 0 has no handoff. Neither does a solo walk — the predecessor there is the
same physician, who does not need to be handed their own note.

### Rotation

Round-robin over a **shuffled** roster, seeded so the preview and the commit are
one permutation rather than two draws.

* **Adjacent points go to different physicians** — otherwise a "handoff" is
  somebody reading their own note back.
* The shuffle stops the same person always drawing point 0, which is the only
  point with no handoff to read and therefore systematically the easiest.
* 13 points / 5 doctors → 2–3 each.

Refused: more doctors than points (somebody never gets a turn), fewer than two (a
solo walk wearing a relay label — every handoff would be self-referential and the
κ annex would claim independent raters that do not exist), and re-sending a walk
already sent (a second rotation would silently take a point away from a doctor
already told it was theirs).

### κ — same outcome, different reason, and the difference matters

Solo points are excluded because blinding does not make one physician's sequential
labels independent. **In relay that objection does not apply** — every point has a
different physician. They are excluded for the ordinary reason instead: one label
per point, below the double-label floor.

Two tokens, two rationales, reported separately (`excluded_trajectory_sequential`
vs `excluded_trajectory_relay_single`), because a methodologist must be able to
tell *"we judged this dependent"* from *"we only have one rater"* — **the second is
fixed by paying for a second walk and the first is not.**

---

## §9.2 A retired predecessor no longer deadlocks a walk

The gate asked "is there an earlier point this evaluator has not submitted?" and
refused the later point if so. Correct while that point is answerable; a deadlock
once it is removed from the walk — nobody can submit it, so every later point
became permanently unservable, silently.

The list of statuses that release a successor is deliberately two words. The
failure modes are asymmetric: too narrow deadlocks a walk (annoying, recoverable,
visible), too wide serves a physician a point whose predecessor they never
answered (unrecoverable — nobody can un-read a future). So a merely **full**
predecessor still blocks; its remedy is releasing or reassigning the point, never
loosening the seal.

Nothing writes those statuses today. It is the vocabulary the retire path must use
when it lands, declared in `trajectory.py` and interpolated into both enforcement
sites.

---

### §8.7 Stalls — the sweep, the chain, reassignment

A relay is a queue of people waiting on each other, so one physician who gets busy
stops four. A **solo** walk is worse: at `max_labels=1`, once somebody submits
point 0 nobody else can ever satisfy the sequence gate for the rest of the chart,
so an abandoned solo walk is unrecoverable by anyone — and it used to be
unrecoverable *and* invisible. The chain view is therefore built for both modes.

```
#0 ✓ · #1 ✓ · #2 ● waiting 31h · #3 –
```

**Only the point the chart is actually waiting on is marked.** A 13-point walk
sitting at point 2 has one problem, not eleven; flagging the rest would make the
view unreadable exactly when it matters.

**The clock runs from availability, not assignment.** Every relay point is assigned
at send, so a clock started there would report a 13-point relay as thirteen
simultaneous stalls the day after it went out — and nudge twelve physicians about
work they cannot do. A point's clock starts when its predecessor was submitted.

**The nudge ships OFF** (`ASCLEPIUS_RELAY_NUDGE_ENABLED`, default `0`). This is the
only place the product messages a physician on a timer with no human deciding to,
and volunteers with clinics to run deserve an observation window first. The sweep
still runs and logs what it *would* send — and marks nothing, so enabling it later
does not fire a backlog of chases at everyone who was stalled meanwhile. One nudge
per (point, doctor), never recurring; `nudged_at` lives on the assignment, so a
reassignment resets the clock and the replacement gets their own.

Reassignment revokes the old holder, writes the new one, DMs them, and is recorded
in the export as `trajectory.reassigned` — a relay walk with a substitution in the
middle is a handoff chain a buyer should be able to see. An already-answered point
cannot be reassigned; that would take finished work from the physician who did it.

**`expire_stale_assignments` had no caller.** It was written with a clear rationale
and nothing ever ran it, so exclusive assignments have never actually expired in
production. It now runs hourly in the same maintenance loop.

---

## §8.5 The private case channel — decided against, not deferred

The PRD specifies a private, member-scoped channel per relay. **We are not
building it**, and the reasoning belongs on the record rather than in a backlog.

Everything the channel would deliver — kickoff context, who is up, chain progress
— is *broadcast*, and relay already DMs every doctor at send and pings the next on
unlock. Its one unique affordance is doctors seeing each other, and the PRD itself
rules out the obvious use for that: *"the case itself stays in the portal"*. So it
would be a group space for coordinating about a case that may not be discussed in
it.

Against that: a new channel type is permanent surface area across ~20 call sites —
`list_channels`, `unread_counts`, `search_messages`, the newsletter, the morning
brief — every one of which currently assumes channels are public. The failure mode
is a private case channel surfacing in somebody's morning digest.

**If this is revisited**, the cheaper shape is a group DM (extending the DM model
past two participants) rather than member-scoped channels: it buys the shared space
without touching channel listing, search, unread, or the digests.

Consequence: §8.7's "post a channel update" on reassignment is a **DM to the new
doctor** instead.

### §8.5 addendum: the endorsed alternative is now built

The rejection above still stands: there are no member-scoped channels, and there
will not be. What the last paragraph named as the cheaper shape is what shipped
(Task Pipeline PRD §B): a **group DM**, `community_dms.kind = 'case_room'`, with
membership in a `community_dm_members` join table. It rides the existing DM
pipeline (PHI gate, reactions, edit and soft delete, read cursors, audit), so
channel listing, search, unread counts and the digests are untouched, which was
the objection.

Three properties the room ships with, each answering an objection raised above:

* **Coordination only.** §8.5's premise holds: the case stays in the portal. The
  bot's pinned introduction names the participants, their roles, the case TYPE and
  the specialty, and then states the no-case-content rule outright. Nothing about
  the case is ever in the room.
* **Keyed on the case, not the member set.** One room per `case_ref` (trajectory
  id for a walk, task id otherwise), so a substitution posts into the same room
  rather than forking a second one and stranding the history.
* **Admin-visible, and it says so.** Ordinary two-party DMs keep their existing
  privacy exactly: an admin still gets a 404 on one. A case room is the
  exception, because the point is that founders can step into a stuck case, and
  the intro tells participants that is true.

Consequently the reassignment consequence above is now additive rather than a
substitute: the replacement still gets their DM, AND the room is told the roster
changed.

---

## What is NOT built

* **§8.5 the private case channel**: decided against; see above. The group-DM
  alternative that section endorsed IS built; see the §8.5 addendum.
* **The chain view is not linked from anywhere but the Task Routing longitudinal
  batch.** An admin who wants it opens that batch; there is no "walks that are
  stuck" landing view across all trajectories.
