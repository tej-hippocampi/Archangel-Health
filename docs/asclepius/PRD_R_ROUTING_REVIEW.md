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

## 4. Findings for other lanes — as first reported (items 1 and 3 are now FIXED, see §4b)

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

## 4b. Audit round — every finding, and what it cost

An external audit ran against the merged tree and returned **do not merge**. All
findings are fixed, in the order it specified. Each fix landed test-first.

| # | Finding | Fix |
|---|---|---|
| **C1** | `redact_identity` never terminates. The marker contains ordinary English fragments (`dent`, `tail`, `move`, `remo`) and the scan restarted from zero after each substitution, so it re-found the needle **inside the marker it had just written**. A physician registering as `remo@mercy.org` pinned a worker at 100% CPU inside a synchronous authenticated GET, allocating until OOM. | Single pass by construction: one compiled alternation, one `re.sub`. Replacement text is never rescanned, so the failure is structurally impossible rather than guarded against. Termination is asserted over **every 4-, 5- and 6-gram of the marker**, under a SIGALRM deadline so a hang fails instead of taking the suite with it. |
| **C2** | `upsert_agreement(blinded=True)` with no caller ever passing it — so `_blinded_only` was a permanent no-op, `quality_report.md` printed "unblinded observations excluded: 0" unconditionally, and every packaged record claimed `independent_second_label` on the strength of an observation merely existing. The 0.15 → 1.0 rate change took that from ~15% of the dataset to **100%**. | The flag is now a measurement. `agreement.blinding_of_pair` is tri-state: **False** on measured de-blinding risk (a labeler holding a role that can read the other's submission, or one person authoring both), **True** only when both labelers passed the pre-reveal blind-commit gate, **None** otherwise — reported as `excluded_unverified`, excluded from κ either way. `upsert_agreement` defaults to `None`. |
| **U1** | The countdown was a lie. `earnings.js` builds a complete server-authoritative heartbeat client and documents that the review surface calls it; `review.html` never loaded it. The page read `credited_seconds` once and added wall-clock drift, rendering "this session has met its minimum" at 20:00 while the server had counted **zero**. | The page computes no time. It loads `earnings.js` first, hands P's client the server's session, and renders `state().continuous_seconds`. `test_the_clock_does_not_advance_on_its_own` fires every registered interval sixty times and asserts not one digit moves. |
| **U2** | An absent clock and a working-but-unpaid clock were indistinguishable. | With a session open but no heartbeat client, the clock says **"Session · not being timed"** in lime, with a note that the review is not accruing paid time. |
| **H1** | `stronger` stored in the reviewer's **shuffled** positions, in the column beside the **canonical** `pair_sub_a`/`pair_sub_b`. Half the rows named the wrong physician to the only sane reading of two adjacent columns. | Resolved through the seeded map and re-expressed canonically, plus a `stronger_submission_id` no reader can misinterpret. Tested over both permutations. |
| **H2** | The single-review predicates were checked at draw time and never again, while the labeler queue kept the same task servable to a second labeler — two `case_reviews` rows for one case, an acceptance rate counting it twice, and the first reviewer's work destroyed by a 409 saying "your claim expired", which was false. | One adjudication per case from either queue, in SQL and on both POSTs. The single-review POST now 409s with `became_a_pair` and **hands the reviewer's judgment back** in the response. |
| **H3** | The pair queue gated on `>= 2`; `ab_pair` truncated to two in Python. A third physician's label was invisible to the reviewer and then marked `reviewed` by them anyway — paid work retired unseen, adjudicated never. | Exactly two, in SQL. A case with more is not review-ready; it is **counted** as `over_labelled` so a human sees it. Adjudication retires exactly the two labels it showed. |
| **H4** | Five correlated scalar subqueries per row on an unbounded fetch, on the single writer labeler submissions need: 0.167 s and 98.5 MB per draw at 20,000 tasks. No index can serve a sort over a computed expression. | One `labeler_queue_sql` builder shared by both queues. Counts materialize once via a grouped join over a covering index; `not mine` resolves in SQL, which is what makes the scan window safe; lean projection with the full row fetched only for candidates actually considered. **Measured after: 0.09 MB, 7 ms.** |
| **M1** | Capacity counted every submission while eligibility and priority counted only verdict-bearing ones — one verdict-less write from a case stuck at "awaiting second" with nobody able to take it. | One number, `n_labels`, for all three. |
| **M2** | An adjudication spanned five independent transactions. A crash mid-way left a `case_reviews` row counting in `review_acceptance` with NULL pair columns beside a task still `in_review` — reproducing H2 on lease expiry. | One transaction. Costs a second INSERT site for `case_reviews`; `test_both_review_writers_produce_the_same_row_shape` is the guard. |
| **M3** | `accept_with_edits` hardcoded a null side, so an edited accept anchored to the canonical oldest rather than the physician actually corrected. | The edited accept follows the reviewer's own "which is stronger" answer, and tracks it if they change it. |
| **M4** | The header stats ran a correlated subquery per task over the whole table, on every draw. | Same grouped-join shape as the queue. |
| **M5** | A case parked as not-independent stranded two physicians' labels behind an ERROR log nothing reads. | Counted as `parked`, distinct from `adjudicated`. |
| **U3** | **No route from the portal to the review console.** A promoted reviewer signed in and never found the surface — it linked back, nothing linked forward. It fell in the gap between two ownership lists. | A rail entry gated on the server's `review` capability, re-checked in the destination router. |
| **U4** | `corrections_withheld` exists so a reviewer can rewrite a note that will not ship; the page discarded it and advanced. | The reviewer is shown what was flagged and why, before moving on. |
| **U5** | Four raw `#fff`, and `var(--orange)` on a physician judgment control — orange means model output. The design guard scanned `asclepius.css` only, so `review.html` was a stylesheet outside it. | Tokens throughout; *Accept with edits* is **lime** ("needs attention"), accept stays green, reject stays pink. The guard now covers `review.html`. |

**Cross-lane edits, made deliberately and flagged here:** `pipeline.py` (C2 — the
policy lives in `agreement.py`; the neighbour's file only supplies facts) and
`asclepius.js` (U3 — one rail entry, one icon, one router branch, in the same
shape Agent P's entry will take; a merge conflict there is "keep both sides").

**On the load-shedding knob** (previously open question #3, and understated):
lowering `ASCLEPIUS_DOUBLE_LABEL_RATE` produces an **oscillation, not a
reduction** — the queue passes `specialty_n=None` and sheds, while the sweep
passes it and re-flags what the queue just declined, because `specialty_n < 30`
routes unconditionally. There is now one incident switch,
`ASCLEPIUS_DOUBLE_LABEL_HALT`, checked ahead of all three predicates. It is a
flag, not a rate: an operator reaching for it at 3am wants "stop".

---

## 4c. The payments seam, closed from both ends

Agent P read this document, audited the seam against R's actual code, and sent
back two items. Both were in `review.js`; neither was a defect in the reasoning.
They are what §6.8 predicted — *"the heartbeat contract is inferred, not read"* —
and they are the shape a seam takes when two agents build it from opposite sides
without being able to read each other's files.

**P fixed, on P's side:** `start()` did not exist (P's client exposed
`open`/`attach`). Every guard on R's call worked, and the outcome was still: the
server opens a billable session, the page never beats, a physician reviews for an
hour and is paid nothing — and nothing throws or logs. The reviewer sees R's
honest `Session · not being timed`, which on a tree without payments is exactly
what it should say, so nobody looks twice. `start(payload, progressKey)` now
exists and is idempotent.

**Fixed here:**

| # | Gap | Fix |
|---|---|---|
| 1 | **Beats named no work.** Every heartbeat read `session:<id>`, so per-case accounting was unanswerable and the per-key credit ceiling had nothing to bind to. | `S.start(SESSION, PAIR && PAIR.task_id)`. The key must name *work*, and only R knows what work is — if payments had to know what a review pair is, the boundary has slipped (PRD-P §8). |
| 2 | **The beats did not stop when the work did.** P's client beats until told to stop or the tab hides. On an empty queue R dropped `SESSION` and hid the clock — so a reviewer idling there kept accruing paid time **and could not see it**, because hiding the clock was correct. Twenty continuous minutes is $100. | `stopSession(reason)`, feature-detected, at every no-work transition. |

Item 2 covers three transitions, not the one P named. An error screen and a
signed-out page are the same shape as an empty queue: no work on screen, and no
way for the reviewer to tell that time is still being counted. Fixing one third
of a defect because only one third was reported is how the next audit finds the
other two.

`stop()` is idempotent and settles through the normal close path, so a session
that already earned its minimum is still paid.

**Two properties of P's `open_session` return that R must not break** (§3 of P's
note): a resumed open carries `nonce: null`, and `open_session` can raise
`PaymentsDenied`. R forwards the session opaquely and `_open_review_session`
already catches `Exception` → `None`, so both degrade exactly as designed.
`test_review_dom.py` asserts the word `nonce` never appears in `review.js`.

**Merge:** for `review.js` and `review.html`, **R's version wins** — P made a
temporary wiring edit to both before this seam was documented. `asclepius.js` and
`asclepius.css` are "keep both sides".

---

## 5. Founder actions this build assumes

Unchanged from context pack §7 — none of them are engineering problems:
persistent `ASCLEPIUS_ASSET_STORE`, Railway's 5-minute body cap, and counsel on
the review-payment cliff (PRD P §1.4) and on algorithmic tiering.
