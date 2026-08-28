# Scale plan: reviewer, quality, pay, assignment, community

**Status: built.** This document was written as a plan and is kept as the
rationale. What shipped, and where it differs from the plan, is recorded in
"What was actually built" at the bottom. Read that first if you are trying to
find something.

Five pull requests, in order, one per area. Written against `main` at `ec06b50`.

Every claim below was checked against the code at that commit. Where the plan
says "today", it means today, not what a doc says.

## The four decisions this plan is built on

1. "Slack" means the **community tab on the platform**. There is no Slack.com
   integration in this repo and none is being added.
2. Morning content comes from **paid search plus an agentic research pass**, on
   top of the existing feeds, with a per-day cost cap.
3. Payouts go as far as **algorithm plus ledger**. No Stripe rail. Money still
   leaves the building by hand against a `payout_batch_id`.
4. **One PR per area, merged before the next starts.**

## Recommended order (one deviation, flagged)

The prompt listed community last. I recommend it goes **first**, as PR 0, and
the rest follow in the stated order. Reason: the other four areas are things
that do not exist yet, so they cost nothing while they wait. The community
surface is different. It is built, switched off, and two of its live email paths
are actively wrong right now (§5.1). Every day it stays as-is, doctors get mail
they cannot unsubscribe from. It is also the cheapest of the five and shares no
files with the other four, so it cannot conflict.

If you would rather keep the original order, nothing else in this plan changes.

| PR | Area | Depends on |
|---|---|---|
| 0 | Community agent and email nudges | nothing |
| 1 | The reviewer sees what the labeler captured | nothing |
| 2 | The internal case-quality metric | PR 1 |
| 3 | Quality-adjusted payout | PR 2 |
| 4 | Case assignment | PR 2 |

---

# PR 0 . Community agent and email nudges

## 0.1 What is actually wrong

The morning routine is **off**. `COMMUNITY_MORNING_ENABLED` defaults to `"0"`
(`backend/community/morning.py:53`) and `.env.example:334` ships `0`.

Because it is off: `#events`, `#medical-ai-news`, `#research-and-opportunities`,
every country channel and every specialty channel are empty; the one-time pinned
"what this room is for" post never runs (`morning.py:474-504`), so eleven-plus
rooms are unlabeled; and the per-doctor morning email never sends.

Four more defects, all verified:

**(a) The highest-value cases are the quietest.** `_notify_new_tasks` has exactly
two callers, `backend/routers/asclepius.py:1131` and `:1166`, both admin task-upload
endpoints. The three real-EHR paths (`_commit_promoted_task`, the real-case
generate path, `load-v4-real-cases`) and the buyer-batch path all call
`store.insert_task` and notify nobody, post nothing, broadcast nothing. A real
de-identified nephrology chart promoted to a gradable task is silent.

**(b) Nothing ever posts into a specialty channel.**
`task_notify.post_community_announcement` hardcodes
`channel_slug="task-announcements"`. `#nephrology` is a room only humans can
write to. The stated goal, a nephrology case landing in the nephrology room and
in general, is not implemented anywhere.

**(c) Recipient matching is strict string equality.**
`store.list_evaluators_by_specialty` (`store.py:2850-2878`) is
`lower(trim(specialty)) = lower(trim(?))`. A task filed as `"renal"` or
`"nephrology - transplant"` matches zero physicians and enqueues zero rows, and
`task_notify.py:81-83` swallows the empty result. No log, no error.

**(d) No drain loop.** `drain_outbox` runs only from a `BackgroundTasks` hook
right after upload and from a manual admin endpoint. A crashed background task
leaves outbox rows `pending` forever.

## 0.2 The two trust bugs, fixed before anything is switched on

**`digest.py:249` builds a dead unsubscribe URL.** It emits
`{PUBLIC_BASE_URL}/community/unsubscribe?token=...`. The only unsubscribe route
is under `prefix="/api/community"` (`community/router.py:49`, `:1033-1042`), so
the live path is `/api/community/unsubscribe`. `newsletter.py:165` gets this
right. Every news-digest unsubscribe link currently 404s.

**`notify.flush_pending` never reads `email_prefs`.** `notify.py:110-146` mails
every user with queued rows regardless of `news_frequency == "off"`, and that
email carries no unsubscribe link at all (`onboarding_emails.py:955-980`). A
doctor who unsubscribed still receives mention, DM, broadcast and announcement
digests every five minutes.

These two ship first, on their own, and are the reason this PR goes first.

## 0.3 Sourcing: paid search plus agentic research

Today `backend/community/websearch.py` has exactly one provider: Anthropic's
server-side `web_search_20250305` tool (`websearch.py:33`), routed through
`call_llm(role="community_digest")` on `claude-haiku-4-5-20251001`. Without
`ANTHROPIC_API_KEY` it returns `[]` and the morning is silently quiet.

Change:

- Introduce a provider interface in `websearch.py`. Anthropic's tool stays as one
  rung. Add **Exa** (neural search, strong on research, fellowships, conference
  listings) and **Firecrawl** (fetch and extract, for turning a found URL into a
  real summary rather than a snippet). Both are already in the operator's stack.
- Add an **agentic research pass** for the weekly discussion prompt
  (`COMMUNITY_DISCUSSION_DOW`), which is the one item that benefits from
  multi-step research rather than a single query.
- **The citation gate is the invariant and it applies to every provider.**
  `_cited_urls()` (`websearch.py:55-84`) walks the response tree, collects every
  URL the tool actually returned, and `_ask()` drops any item whose normalized
  URL is not in that set. This is the module's entire reason to exist. A new
  provider that cannot produce a verifiable source URL does not get to post.
- **A real cost cap.** Today the only ceiling is `COMMUNITY_WEBSEARCH_MAX_USES`
  per call, with no per-day budget. Add a daily spend ceiling recorded on the
  run ledger, and treat exhaustion as a quiet day, which the code already models
  correctly (`morning.py:377-380`).

## 0.4 Case to channel to inbox

- Add a `channel_slug` parameter to `post_community_announcement` and post into
  `#<specialty>` and `#general`, not only `#task-announcements`.
- Call `_notify_new_tasks` from all five `insert_task` paths, not two.
- Widen recipient matching: specialty aliases, subspecialty to parent mapping,
  and an `open_to_all_specialties` fan-out. **Make a zero-recipient enqueue loud.**
  A batch that reaches nobody is the failure this system is for.
- Add a periodic `drain_outbox` tick alongside the existing loops in `main.py`.
- Turn the morning on. The ledger-based idempotence (`is_due`, `morning.py:82-111`)
  already makes double-posting impossible across the in-process loop and the
  external trigger, and the unwired workflow at
  `docs/asclepius/community-morning.workflow.yml` becomes the backstop.

## 0.5 Tests

`backend/asclepius/task_notify.py` and `backend/community/newsletter.py` have
**zero tests** today. That absence is exactly why §0.1(a) has been invisible.
Both get covered: the specialty-match recipient query, the idempotency key, the
outbox drain, the same-day dedupe, `_member_channels` routing, and cohort
due-ness.

---

# PR 1 . The reviewer sees what the labeler captured

## 1.1 What PRD-1 already fixed

Commit `33782ed` (2026-08-28) did real work here and it is already on `main`:
review moved into the shell as `window.AsclepiusReview.render`, `review.html` is
deleted, `case_panel.js` is extracted so the labeler and the reviewer render one
chart through one code path, the judgment panel is pinned, reasoning steps are
aligned with forks marked on both columns, and a clean accept is six keystrokes.

The speed problem is solved. The **coupling** problem is not.

## 1.2 The coupling problem, verified on current main

There is no shared schema. There are three independent definitions of "a label":

| Layer | Location |
|---|---|
| Truth | `schemas.py:535` `SubmissionIn` and its nested models |
| Reviewer server whitelist | `review.py:196-199` `_SUBMISSION_PAYLOAD_VIEW_KEYS`, a hand-maintained 10-string tuple |
| Reviewer client renderer | `review.js` `answerCard()`, hand-written `if (a.X)` branches |

The whitelist is deliberately fail-closed for privacy (`review.py:190-195`: a new
identity field added upstream stays invisible by default instead of leaking by
default). That is correct. It is also why it fails closed for **product**: a new
labeling question is invisible to the reviewer with zero error, zero test
failure, zero log line.

**`citations` is a phantom key.** It sits in the whitelist (`review.py:198`) and
is rendered at `review.js:596`. No submission has ever carried a top-level
`citations` key. Citations live nested as `evidence_anchor` and
`evidence_anchors` on `chosen_revision`, `from_scratch`, `independent_answer`,
every `reasoning_steps[]` entry, every `rubric[]` entry, and
`rejected_critique.error_tag_anchors` (`schemas.py:356-471`). Meanwhile the
labeler has a whole "CITE YOUR SOURCES" substage (`asclepius.js:2914`).

**A reviewer has never seen a single citation a labeler entered**, while one of
the four dimensions they grade is `rubric_quality` and the premium export SKU is
literally "grounded".

Whitelisted but never rendered: `why_better_tags`, `severities`,
`error_tag_reasons`, `failure_tags` (the entire Model-Failure Taxonomy capture,
a named export SKU), the reasoning-step metadata (`label`, `corrected`,
`confirmed`, `added`, `original_text`, `correction_reason`, `step_error_tag`,
`critique`), the rubric's `axes`, `tier`, `critical`, `specific`, and
`independent_answer.kind`. A grep for those names in `review.js` returns zero.
Never whitelisted at all: `prompt_review`, the Stage-1 clinician sign-off.

## 1.3 The build

**A single declarative field map.** New module `backend/asclepius/label_view.py`
declaring, per labeling field: path, reviewer visibility, display group, and
render kind (prose, chips, anchors, steps, rubric). Both the server whitelist and
the client render order derive from it, and it ships to the client over
`/review/me` next to the dimension vocabulary. That server-to-client vocabulary
pattern already exists and works (`asclepius_review.py:143-151`), which is why
the reviewer's four dimensions have never drifted.

**A completeness test that fails loudly.** Walk `SubmissionIn`'s Pydantic fields
recursively and assert every leaf is either declared visible or explicitly
declared withheld with a stated reason. Add a field to `SubmissionIn` without
touching the map and the suite goes red. This is the missing feedback loop, and
its absence is precisely how a phantom key survived in a whitelist for months.
Today the closest guard, `test_paired_review.py:603`, pins the review row shape,
not the labeling-to-review mapping.

**Kill the phantom key and surface real anchors.** Collect evidence anchors from
every nested location and render each one **attached to the claim it supports**,
not in a separate "Citations" fold. A citation divorced from its claim is not
reviewable.

**Render what becomes visible**, in the `case_panel.js` idiom PRD-1 established:
error severities and failure-mode tags on the critique column; step-level
correction reasons inline on the aligned traces; rubric axes, tier and critical
as chips; `independent_answer.kind` as a mono eyebrow, so a ten-second `instinct`
one-liner is never mistaken for a `full` blind answer.

**Reviewer draft persistence.** `R` is in-memory only in `review.js`. A refresh
mid-adjudication loses the judgment. That is tolerable for four segmented
controls and not tolerable once the form grows. Mirror the labeler's draft
pattern (`draftKey`, `saveDraft`, `asclepius.js:2082-2201`). Check the in-flight
`claude/server-side-draft-storage` branch first and reuse it if it fits.

**Blinding invariants are untouched.** Every newly visible field still passes
`_scrub_metadata`, the `_IDENTITY_MARKERS` walk, and the Safe-Harbor scan. The
map declares visibility; it never bypasses the scrub. Default is withheld.

---

# PR 2 . The internal case-quality metric

Reading the request as: an internal per-case quality number, computed from time
to label, what the case asked for, review outcome and difficulty, shown in the
admin panel next to each case, not sold to buyers, refined over time.

## 2.1 What exists

`contributor_score.case_score()` already returns `{score, components}` per
submission: `outcome_base` (85 accepted / 70 accepted-with-edits / 30 rejected),
`citation_bonus` (+1 per anchor, cap +5), `reasoning_bonus` (+0.5 per step, cap
+5), `agreement_adj` (kappa, clamped +-5), `time_adj` (+3 in band, -5 below a
quarter of expected). Per-case components are stored on
`contributor_score_history`. `expected_minutes` already prefers measured
empirical difficulty over declared.

## 2.2 The gaps

**The QA path does not recompute.** `contributor_score.py:15-18` claims the hooks
ride on QA decisions and review submissions. In reality `recompute_for_submission`
is called from exactly two places, both in the review router
(`asclepius_review.py:456` and `:590`). `POST /qa/{submission_id}/decision`
(`routers/asclepius.py:3173-3189`) applies the decision and returns. A
QA-only-graded submission never moves the stored score. Nothing tests this.

**No difficulty term of its own.** Difficulty enters only through `expected_minutes`.
A hard case labeled adequately should outscore an easy case labeled adequately,
and today it does not.

**No "what did this case ask for" term.** Cases differ in what they demand:
`portal_version`, whether `capture_reasoning` was on, `grounding_mode`, and
whether the verdict forced `from_scratch` rather than an A/B refine. The metric
should compare like with like.

**It is not on the money screen.** It renders only in the physician dossier
(`admin_physicians.js:1076-1106`). Earnings L2 columns are
`Case / Specialty / Time / Pay / Status / Export / Void`
(`admin_earnings.js:232-233`), with no quality column. The physician roster
(`admin_physicians.js:456-459`) has no score column.

## 2.3 The build

- Wire the QA path, and test it.
- Add the difficulty and case-shape terms.
- Add a `Quality` column to Earnings L2 between `Time` and `Pay`. The data is one
  hop away: `_enrich_case_context` (`asclepius_payments.py:479-518`) already
  walks `store.get_submission(ref_id)` per row. Note it is computed only for
  `user_id`-scoped requests today, deliberately, because it is a query per row.
- Add a `Score` column to the physician roster, fed from `/admin/physicians`.
- **Make every number explainable.** Render itemized components with reasons in
  the same string convention `credentialing` uses (`"+25 NPI verified against
  NPPES (MD)"`, `"-4 consumer email domain"`). This number will be contested the
  moment PR 3 attaches it to money.

**The rule that carries into PR 3:** the metric is computed and **stored per case
at grade time**, never recomputed at pay time. Same discipline as `rate_cents`,
which is stamped at accrual (`payments.py:179-182`) so changing an env var never
restates a past earning. Without this, tuning a coefficient silently restates
what physicians were owed.

---

# PR 3 . Quality-adjusted payout

## 3.1 Today

Flat. `reconcile_task_accruals` writes `amount_cents=rate, rate_cents=rate`
unconditionally for every payable submission (`payments.py:1247`). No quality
term, no difficulty term, no specialty term. $75 per labeler submission, $100 per
qualifying reviewer session of 20 continuous minutes.

Quality acts only as a **binary gate**: `PAYABLE_VERDICTS = {accept,
accept_with_edits}` maps to `approved` at full rate; only a reject with no
payable verdict voids to $0 (`payments.py:1174-1203`).

## 3.2 The build

**A pure function.** New `backend/asclepius/payout.py` exposing
`quality_multiplier(case_score, review_verdict, difficulty, case_shape) ->
(multiplier, itemized_reasons)`. Pure, in the idiom of `routing.py` and
`value.py`: no store, no FastAPI, testable in isolation and readable by counsel
without reading the app.

**Bounded on both sides.** A floor, because the physician did the work and a
near-zero payout for delivered work is a wage claim. And a **ceiling above 1.0**,
because a hard case done excellently paying more is the incentive actually being
asked for, and upside is a far safer instrument than pure downside.

**Stamped, never restated.** `earnings` gains `quality_multiplier REAL` and
`quality_reasons_json` beside the existing `rate_cents`. `amount_cents =
round(rate * multiplier)`. Past rows are immutable, in keeping with
`test_payments_schema.py`, which pins that no status column carries a DEFAULT.

**Computed in the sweep, not in the review router.** It goes inside
`reconcile_task_accruals`, which already reads `submissions` and `case_reviews`
read-only and materializes missing rows (`payments.py:71-86`). This respects the
payments/review seam that `test_paired_review.py:1077` enforces by grepping the
review router for payment vocabulary. Do not put pay logic in the review path.

**A human approves every reduction.** This is the most important choice in the
plan. The algorithm **proposes**. A row at full rate keeps auto-approving on the
existing 14-day window. A row **below** full rate lands as `accrued` carrying a
proposed deduction and its itemized reasons, and an admin approves it before it
becomes `approved`. This mirrors the tiering precedent exactly, which the counsel
memo describes as "the tool proposes; a human decides" and which was built that
way on purpose.

**The physician sees the reason.** The Earnings page shows the itemized
adjustment. A silent deduction is the worst possible version of this feature.

**No clawback.** Preserve the existing rule (`payments.py:1186-1197`): a later
accept may restore money, a later reject never claws back an already-approved
row. In a paired adjudication the verdict applies to both labels, because the
blind second label is the product.

**The multiplier takes no physician attribute as input.** Only case-outcome
facts. The same discipline as `tiering.FORBIDDEN_CREDENTIAL_KEYS` and the
`PINNED_ZERO` weights, and it should be asserted the same way, adversarially.

**Onboarding stipulation**, as asked: state in the contractor agreement and the
signup attestations that payment is quality-conditioned, and state the floor.
`backend/asclepius/agreement.py` is where that text lives.

## 3.3 The thing to get a read on before this merges

This turns an internal score into contractor compensation. That is algorithmic
management of pay, and it is a stronger version of the question the tiering work
already went to outside counsel on under **NYC Local Law 144**
(`docs/PRD_C_COUNSEL_MEMO.md`). The memo covers tier assignment. It does not
cover pay.

I am building it as specified. The admin gate in §3.2, the itemized reasons, the
floor, and the no-physician-attributes rule are what make it defensible, and they
are in the design for that reason. The recommendation is that the counsel memo be
extended to cover pay **before** this ships rather than after, and that is a call
for you to make, not a blocker I am imposing.

---

# PR 4 . Case assignment

## 4.1 Today: there is nothing

No assignments table, no `assigned_to` column, no assignee, no endpoint, no UI.
Verified by grep across the backend. The only "claim" primitives are a background
worker lease and a reviewer's compare-and-swap lease on a draw they pulled
themselves.

What happens to 100 nephrology cases right now: admin promotes the batch, one
email fans out per matching clinician, and then it is a free-for-all pull queue
ordered `n_labels DESC, created_at ASC`. No load balancing, no per-doctor cap, no
reservation, no matching on contributor score or `domain_match`. **One fast
labeler can take all 100.** Independence only stops the same person taking the
same case twice.

## 4.2 The build

**New table.** `assignments(assignment_id, task_id, user_id, role('label'|'review'),
assigned_by, assigned_at, status('offered'|'claimed'|'done'|'expired'|'revoked'),
due_at, UNIQUE(task_id, user_id))`.

**A sort, never a filter.** `store.py:226-231` states the law and
`test_routing_priority.py:170` pins it: the moment priority becomes a `WHERE`
clause, a labeler with no eligible work sees an empty queue and stops working. An
assigned task sorts to the top of its assignee's queue. If an **exclusive** mode
is wanted for cases that must go to a named physician, it must be time-boxed with
an expiry that returns the case to the pool, or one doctor's vacation wedges the
queue.

**A pure allocator.** `allocate(cases, physicians, policy) -> proposal`. Every
input already exists and is already pure:

- `tiering.domain_match` (subspecialty 1.0, specialty 0.5, neither 0.0)
- `tiering.tr_eligibility` (the hard clauses for reviewer: active board cert,
  calibration pass, 3 years post-residency, `domain_match >= 0.5`)
- `contributor_score.compute` (the running quality number)
- `value.routing_score` (expected realized value divided by that clinician's
  rolling median minutes, with a neutral 7-minute fallback so a new clinician is
  not starved on day one)
- `empirical_difficulty`

Constraints: pair independence (two labels, two different physicians), reviewer
authored neither label, specialty match, `real_data_approved` for `real_deid`
cases, per-physician load balance, and **reviewer supply**, since reserving too
many reviewers starves labeling. The worked example (10 nephrologists, 5
reviewer-capable, 100 cases, out comes roughly 8 labelers and the 2
highest-scored as reviewers) is the acceptance test.

**Propose, do not dictate.** The admin sees the proposed allocation as a table
(physician, role, case count, why), adjusts it, and commits. Dry-run first, the
same shape as the existing `promote --dry_run`, which exists so an admin can
iterate before a physician is paid to look at anything. Record the admin's
override as signal, as tiering already does.

**Where it lives.** A fifth sub-tab under Tasks (`asclepius.js:7917-7919`).
Notify on assign through the `task_notify` outbox rebuilt in PR 0.

## 4.3 One gap to decide

`users.tier` is a **single global scalar** (`labeler | reviewer | NULL`). But the
counsel memo says, in writing, that TR "is a per-domain role, and the same
physician is a TR on a nephrology case and not on a cardiology one". The code
does not match the memo. Today it half-works because the reviewer draw is
specialty-filtered in SQL and `tr_eligibility` requires `domain_match >= 0.5` at
approval time.

Assignment is where that gap becomes load-bearing. Recommendation: add a
`(user, specialty) -> role` table and make the per-domain role real, because that
is what counsel was told the product does.

---

## Sequencing and risk

PR 1 before PR 2, because a quality metric grades what the reviewer can actually
see. PR 2 before PR 3, because pay needs a metric that is stored, stamped and
explainable. PR 4 after PR 2, because the allocator reads the contributor score.
PR 0 is independent of all of them and shares no files.

The three places this can go wrong, in order of consequence:

1. **PR 3 without the admin gate.** An automated pay cut with no human in the
   loop is a materially different legal object than a proposal a person approves.
2. **PR 4 as a filter instead of a sort.** It empties queues, and the failure
   looks like "nobody is working" rather than like a bug.
3. **PR 0 switched on before §0.2.** Turning the morning routine on multiplies
   two live email defects across every doctor on the platform.


---

# What was actually built

All five areas, on one branch, one commit per area so it stays bisectable.

| Commit | Area | New files |
|---|---|---|
| `4cd5742` | The two live email defects, plus specialty matching | `community/links.py`, `tests/test_community_links.py` |
| `5abc2c2` | The four silent task paths, specialty-room posts, outbox drain loop | `tests/test_task_notify.py` |
| `e5800dc` | Exa/Firecrawl retrieval, agentic pass, durable call cap | `community/search_providers.py`, `tests/test_community_search_providers.py` |
| `937b78c` | The reviewer field map, real citations, draft persistence | `asclepius/label_view.py`, `tests/test_label_view.py` |
| `27e2413` | The case-quality metric, the QA hook, the stamp | `tests/test_case_quality.py` |
| `1f13811` | Quality-adjusted pay | `asclepius/payout.py`, `tests/test_payout.py`, `docs/asclepius/QUALITY_ADJUSTED_PAY.md` |
| `5b483e5` | Assignment | `asclepius/allocation.py`, `tests/test_assignment.py` |

## Where it differs from the plan

**Two things ship OFF rather than on.** Quality-adjusted pay
(`ASCLEPIUS_PAYOUT_QUALITY_ENABLED=0`) and the paid search providers
(`COMMUNITY_SEARCH_PROVIDERS=anthropic`) are both switched off by default. The
plan did not say this. It became obvious while building that turning the payout
on changes what every physician is paid, and that should be a decision made on a
particular day rather than a side effect of a deploy. The case-quality metric
underneath it is computed and stamped either way, so switching it on later
starts with a history rather than cold.

**The morning routine is still off too.** The plan's §0.5 said "turn it on". The
plumbing it depends on is fixed and tested, and switching
`COMMUNITY_MORNING_ENABLED=1` is now a safe operator action rather than a code
change, but flipping it in the same PR that rewrote the sourcing would have
merged a behaviour change and a code change together.

**`chosen_id` stayed visible to the reviewer.** The plan implied trimming
model-identity fields from the label view. `review.js` resolves the final answer
text through `chosen_id`; withholding it blanks the answer the reviewer is there
to grade. It is the judgment, not a tell.

**A rejected case is voided, not reduced.** The payout multiplier is never
applied to a rejected case: it pays nothing already, and running a multiplier
over it both means nothing and misreports the voided amount.

## What is still open

**The counsel memo does not cover pay.** `docs/PRD_C_COUNSEL_MEMO.md` covers how
a physician is CLASSIFIED under NYC Local Law 144. It does not cover how they
are PAID. The recommendation stands: extend it before
`ASCLEPIUS_PAYOUT_QUALITY_ENABLED` is turned on, not after. The flag defaulting
to off is what makes that ordering possible.

**The per-domain reviewer role is still not real.** `users.tier` remains a
single global scalar while the counsel memo describes TR as a per-domain role.
The allocator works around it by requiring `domain_match >= 0.5` for a review
assignment, which is the same clause `tiering.tr_eligibility` applies, so
behaviour is correct today. The data model still does not match the memo. Plan
§4.3 recommended a `(user, specialty) -> role` table; that was not built.

**Four tests fail on macOS and pass in CI.** Three in
`test_asclepius_v4_phase1_completeness.py` and one in
`test_asclepius_v4_phase3_durability.py`. All four were reproduced on clean
`main` in a separate worktree before this branch was written, and none of them
touch code this branch changed. The durability one is a path heuristic
(`tmp_path` is under `/tmp` on Linux and `/var/folders` on macOS).

**`test_community.py` deadlocks when run alongside the other community test
files.** Also pre-existing and also reproduced on clean `main`. It passes alone
(82 tests, ~6s) and CI's 4-way sharding evidently separates them. Not fixed
here; worth fixing.
