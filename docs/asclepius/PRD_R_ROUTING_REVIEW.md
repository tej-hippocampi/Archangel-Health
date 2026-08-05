# PRD R — Task routing and the paired review · build notes

Branch `claude/prd-implementation-audit-t2xtzj`. This document records what was
built, the places the PRD's specified process was **changed** and why, and the
findings that belong to another agent's lane.

Definition of done (PRD R §6):

```bash
python3 -m pytest backend/tests/test_routing_priority.py \
                  backend/tests/test_paired_review.py \
                  backend/tests/test_review_dom.py -q
```

---

## 1. Audit of the PRD's own claims, against the code

Every "already correct — do not rebuild" claim in PRD R §1 was verified before
anything was written. All five hold:

| Claim | Verified at | Verdict |
|---|---|---|
| `flag_task_for_double_label` lifts `max_labels` **and reopens** | `store.py` `flag_task_for_double_label` | correct — the reopen is load-bearing exactly as described |
| self-review + prior-submitter exclusion are in SQL | `next_review_for`, `next_double_label_for` | correct |
| the review claim is a compare-and-swap | `claim_submission_for_review` | correct |
| `blinded` is derived from the payload served | `payload_is_blinded` + the draw route | correct |
| `cohens_kappa` / `review_acceptance` are separated and correctly named | `agreement.py` | correct — and `export.py` already reports them as two statistics |

Two inaccuracies in the PRD text, neither material:

- **§1.1 says the rate default is `0.20`; §1 says "15%".** The actual default was
  `agreement.DEFAULT_DOUBLE_LABEL_RATE = 0.15`. It is now `1.0`, in that one
  place, and `review.double_label_rate()` and `routing.second_label_is_default()`
  both delegate to it.
- **§4's grep test contradicts §4's own instruction.** It forbids the string
  `credited_seconds` in `asclepius_review.py` while telling the router to return
  `session["credited_seconds"]`. Resolved by forwarding P's session dict
  **opaquely** — which is better anyway: P can add a field without R changing.

---

## 2. Where the specified process was changed, and why

### 2.1 The second-label flag is written at DRAW time, not on the submit path

**PRD R §1.1 asks for:** flag synchronously on the submit path, right after
`refresh_task_status`.

**Why that could not be done as written:** the submit path is
`routers/asclepius.py` (`_finalize_submission`). That file is not in Agent R's
write allowlist (context pack §2), and the one hook on that path that R *can*
reach — `refresh_task_status` — is explicitly read-only.

**What was built instead, and why it is stronger:**

1. **Queue eligibility and capacity are DERIVED** (`_PRD_R_SERVABLE`,
   `routing.effective_capacity`). A `'done'` task carrying exactly one label and
   never lifted is servable, and its capacity is computed as 2 when the policy
   wants a second label. The queue is therefore correct on the very next draw
   with **no write at all** on the submit path.
2. **The stored `max_labels` catches up when the case is served**
   (`_prd_r_lift_capacity`). One batched, idempotent UPDATE carrying
   `AND max_labels < 2`, so a task is written exactly once, ever.

This is the PRD's own §7 principle applied one level further: the flag stops
being a *precondition* for the queue being right and becomes bookkeeping. It also
means the existing background sweep is now a **second, independent** path to the
same state rather than a latency the queue depends on.

Bookkeeping the lift still buys: the value-per-minute multiplier
(`value._tier_mult`'s `is_double_labeled_credentialed`), `next_double_label_for`,
and `refresh_task_status` closing the task at two labels rather than one.

### 2.2 The starvation guard was already free; it is now tested

PRD R §1.2 treats the starvation guard as something to add. In this codebase the
`mine`/capacity filtering already happens in Python **after** the SQL sort, so a
`DESC` sort falls through to fresh work automatically. Nothing was added — but
`test_routing_priority.py` now pins the behaviour twice, including the degenerate
case where every case except the newest carries this labeler's first label.

### 2.3 "Accept A" / "Accept B" are ONE verdict plus a side

**PRD R §2.3 asks for:** `[Accept A] [Accept B] [Accept with edits] [Reject both]`.

Storing `accept_a` / `accept_b` as verdict tokens would have broken the headline
metric silently: `agreement.review_acceptance` counts exactly
`{accept, accept_with_edits, reject}`, and anything else lands in
`n_unclassified` — so the dashboard would have read **0% acceptance** while
appearing nowhere as an error. That is precisely the failure §7 warns about.

So the UI shows four buttons and the row stores three fields:

| stored | values |
|---|---|
| `verdict` | `accept` · `accept_with_edits` · `reject` (unchanged vocabulary) |
| `accepted_submission_id` | which physician's work the verdict accepts; NULL for reject-both |
| `stronger` | `A` · `B` · `equivalent` |

`review_acceptance` and `cohens_kappa` keep their names, their inputs and their
separation. `test_paired_review.py` asserts the denominator never leaks.

### 2.4 A leaked name is REDACTED, not merely measured

**PRD R §2.2 asks for:** "a test that seeds a labeler's own name into their free
text and asserts **it is not served**."

The existing single-submission flow only *measures* the leak and records
`blinded = 0`. That satisfies κ's statistics and fails the sentence: the reviewer
has already read the name, and the adjudication is biased before κ ever gets to
exclude the observation.

The pair view therefore **redacts, then measures** (`review.redact_identity` →
`review.pair_is_blinded`). Clinical content survives with a visible
`[identifying detail removed]` marker; blinding is then honestly `True`, which
keeps the observation inside κ's denominator instead of shrinking it.

Needle precision was extended in the same pass: `marguerite.okonkwo` (an email
local-part) and `Marguerite Okonkwo` (how a physician signs a note) are now the
same identity to the scanner. **Only multi-token names produce variants** — a
single-token rule would redact "Cushing" from a labeler named Cushing writing
about Cushing's syndrome, corrupting the record it was meant to protect.

**Stated residual risk, not hidden:** bare surnames, initials-only signatures,
and **writing style**. Two answers side by side make style a far stronger signal
than one answer alone, and no scanner fixes it.

### 2.5 Three structural leaks the PRD did not name, found by the tests

- **`captured_at`** — the pre-reveal independent-answer commit timestamp, nested
  two levels under a field the reviewer genuinely needs. A direct "who went
  first" tell. Now pruned, along with `portal_version` and the other ordering
  metadata (`review._ORDERING_METADATA_KEYS`).
- **A reviewer reloading the page lost their own case.** The CAS excluded any
  `in_review` row inside its lease, including one the *same* reviewer held, so a
  refresh reported an empty queue. `review_claimed_by = ?` is now an accepted
  branch of both the draw and the claim.
- **`max_labels = 3` disagreed with the state machine.** A SQL check of
  `label_count >= 2` alone would have adjudicated a case still wanting a third
  label. The pair query now mirrors `routing.phase` exactly.

---

## 3. Boundaries held

- **Agent P (§4).** The only call is `payments.open_session(store, user_id=…,
  kind="review")`, guarded so R still works on a tree where P has not merged, and
  non-fatal so a physician is never blocked from reviewing by the payments
  module. The session dict is forwarded verbatim. `test_paired_review.py` greps
  `review.py`, `routing.py` and `asclepius_review.py` for P's concepts and
  asserts the only attribute touched on the payments module is `open_session`.
- **`refresh_task_status`** — read, never edited.
- **`asclepius.js`** — untouched.
- **The statistics' names** — unchanged; only their inputs grew.
- Files written are exactly Agent R's allowlist. The two assertions changed in
  `test_review_tier.py` are the ones that pinned the old `0.15` default, which
  PRD R §1.1 mandates changing.

---

## 4. Findings for other lanes — reported, not fixed

1. **`pipeline.compute_and_store_agreement` writes `blinded=True` unconditionally.**
   (`upsert_agreement(..., blinded: bool = True)` — the argument is never
   passed.) Under the paired flow this is *usually* true — the second labeler
   never sees the first's answer, enforced in SQL — but it is an **asserted
   constant on the buyer-facing honesty claim**, which is the same defect class
   `payload_is_blinded` was built to remove. `pipeline.py` is outside Agent R's
   allowlist. Mitigation in lane: `routing.pair_is_independent` is asserted at
   the point the pair is served, and a task whose two labels share an evaluator
   is never adjudicated.

2. **`review_pair_queue_stats` runs a correlated subquery per task.** Fine at pod
   scale and it backs a header, but it is the one new query that scales with the
   task table rather than with an index. Worth a materialized counter before the
   task table gets large.

3. **The load-shedding knob does not fully shed load yet.** PRD R §1.1 keeps
   `ASCLEPIUS_DOUBLE_LABEL_RATE` "because a future backlog may need to shed
   load". Setting it below 1.0 shrinks the top-up, but
   `agreement.should_double_label` also routes **every case in a specialty with
   fewer than 30 stored agreement observations**, unconditionally. Until each
   live specialty passes 30 observations, lowering the rate changes almost
   nothing. That rule is correct (κ per specialty needs a denominator) and was
   left alone — but an operator reaching for the knob in an incident should know
   it will not bite immediately.

4. **An impossible pair is parked, not recovered.** If a task ever carried two
   labels from the same physician, the draw logs at ERROR and sets
   `tasks.review_status = 'reviewed'` so it stops re-occupying the head of the
   queue. That case then needs a human. Both queues exclude prior submitters in
   SQL, so reaching that line means an invariant broke — the loud log is the
   point.

5. **Value-aware routing priority is structural, not absolute.** On the assisted
   queue (`v2`/`v3`) `_value_aware_next` re-ranks by expected value-per-minute.
   An awaiting-second case wins because lifting it to `max_labels = 2` turns on
   the double-labeled-credentialed multiplier — not because the SQL said so. A
   sufficiently high-value fresh case (real_deid + multimodal) could still
   outrank it. Making the priority absolute on that path needs a line in
   `value.routing_score` or `routers/asclepius.py`, both outside this lane.
   `test_routing_priority.py::test_value_aware_routing_sees_the_same_priority`
   pins the behaviour that does hold.

---

## 5. Founder actions this build assumes

Unchanged from context pack §7 — none of them are engineering problems:
persistent `ASCLEPIUS_ASSET_STORE`, Railway's 5-minute body cap, and counsel on
the review-payment cliff (PRD P §1.4) and on algorithmic tiering.
