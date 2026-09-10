# Longitudinal cases — operator runbook

Implements the *Longitudinal Cases* PRD. The thesis in one line:

> **The next encounter is the answer key.**

Medical post-training is said to have no verifier. That is true *prospectively*
and false *retrospectively*. A chart that continues past a decision contains what
happened after that decision. Truncate the chart at encounter *k*, have a
physician commit to an assessment, a plan and — the field nobody else sells —
what they expect to see and what would tell them they are wrong; then reveal
encounter *k+1*. The record grades the prediction. No human graded it.

Read this if you are turning a partner chart into a chart walk, or trying to
understand why a physician is being refused a case they can plainly see the id of.

---

## 1. The one rule everything else serves

**Truncation is a server responsibility.** The client never receives data it is
meant not to show. Everything downstream of the decision point is *absent from
the payload* — not hidden, not collapsed, not styled away.

A truncation implemented in CSS is a leak, and a leak here does not merely weaken
one case: **you cannot un-read a future.** The moment a physician sees encounter
*k+1* before committing at *k*, their prediction at *k* is worthless and so is
every later point in that walk, permanently.

Three enforcements, all server-side:

| Where | What |
|---|---|
| `real_cases.build_encounter_case` | builds the visible window by a total temporal split; `assert_temporal_split` fails the case if anything survives past day 0 |
| `store._PRD_2_SEQUENCE_GATE` | the labeler queue will not offer point *n* until this evaluator has submitted every earlier point |
| `routers/asclepius._require_trajectory_sequence` | the same rule on every by-ID path — fetch, reveal, answers, submit — as a **409** |

There is deliberately no client-side sequence check. `test_asclepius_longitudinal_ui`
asserts its absence: a gate in the browser is defeated by a hand-typed task id or
a second tab, and worse, its existence invites deleting the server one.

---

## 2. Decision and interval points

An **encounter** groups recorded activity separated by gaps of more than seven
days. The density thresholds remain unchanged: **two distinct dates, eight
recorded events, two resource types** (labs, notes, studies or vitals).

A **decision** point clears all three thresholds and has a visible presenting
clinical narrative from that encounter. An **interval** point records follow-up
observations: the physician re-evaluates the existing plan against the new data.
A density-qualified encounter without a presenting narrative becomes an interval
with a `downgraded` reason. It is not held, and it does not hold its predecessor.

A sparse trailing interval is excluded because no later point can verify it.
A density-qualified final encounter may close the walk as an interval when its
narrative is absent. The terminal point has no later outcome to verify.

Measured through the committed fixture ingestion door with `trajectory: true`:

| Chart | Encounters | Decision | Interval | Walk points / generatable | Verifiable | Held |
| --- | --- | --- | --- | --- | --- | --- |
| patient-1 | 22 | 2 | 20 | 22 | 21 | 0 |
| patient-3 | 5 | 3 | 2 | 5 | 4 | 0 |
| patient-4 | 7 | 3 | 4 | 7 | 6 | 0 |

Patient-2 remains quarantined; diagnostic density counts do not authorize tasks.
Static-mode patient-1 and patient-3 retain 22/9 and 5/4 encounters/decision points.
The density-qualified counts (9 and 4) are different from the walk's narrative-
qualified decision counts (2 and 3). Yield on new partner charts must be measured.

## 3. Generate a walk

```json
{ "dry_run": true, "trajectory": true, "include_interval_points": true }
```

Send to `POST /api/asclepius/ingestion/cases/{ingest_case_id}/generate` for the
plan, then use `dry_run: false` to generate. Interval points are included by
default. The admin checkbox changes both the preview and generation selection.
The chart's declared specialty applies to the entire walk.

The plan reports encounters, decision/interval counts, ready points, verifiable
points and downgrade reasons. Generation selects `qualifies_as_point` proposals
that are `generatable`; `apply_density_gate: false` includes every generatable
proposal. Content, date, leakage and empirical difficulty gates still apply.

Generated tasks share one `trajectory_id`, receive dense `sequence_index` values
0…n−1, and use `assigned_only` distribution with `max_labels = 1`. Physician pay
remains **$75 per submission for either class**. A complete seven-point patient-4
walk therefore has $525 in physician pay. There is no buyer price field.

---

## 4. What the physician sees

1. **The case, truncated.** A banner names the step ("Step 3 of 7 · Interval visit") and says
   the future is sealed.
2. **The commitment.** Assessment and plan as usual, plus the *Expected
   trajectory* card: what should happen next, with an optional horizon, and what
   would say they are wrong. **Optional, and it must stay optional** — a
   fabricated falsifier is worth less than none, because it gets scored against a
   real chart and the score means nothing.
3. **The reveal.** On submit, straight to what happened next — dated from the
   moment they committed ("day +12"), never back to a queue draw.
4. **The self-score.** Each expectation is marked `held` / `did_not_hold` /
   `not_assessable`. `not_assessable` is first-class: the next encounter
   frequently does not contain the observation the prediction was about, and
   forcing a binary manufactures a verification nobody made.
5. **Continue.** The next point opens by id, so they stay on the same patient.
   Reading a new chart is the expensive part of a task; a walk pays that once.

The reveal window runs from just after this decision point up to and including
the **next** walk point — the presenting data of encounter *k+1*, not its
resolution, which belongs to the point after. Say so if a physician asks, because
someone marking `not_assessable` needs to know whether the observation is absent
from the record or merely beyond the window.

---

## 5. Single-labelled by default, and what that implies

Trajectory points are excluded from the Cohen's κ pool **by construction**, so a
second label buys no agreement statistic. It buys a second independent walk of the
same chart — a different and more expensive product. `max_labels = 1` is forced at
generation, and both capacity-lifting paths are guarded:

* `routing.wants_second_label` — the labeler draw,
* `agreement.should_double_label` — the background sweep.

Guarding only the first would let the sweep silently re-flag a minute later and
double the physician-pay obligation without an explicit assignment.

### The consequence you will hit in operations

Generation does not route the walk. An admin assigns the whole walk to one
physician (solo), or sends a relay with a seeded rotation across the declared
specialty's physician pool. Point 0 opens first; later points unlock after the
required prior submission. Reassign an abandoned point through the relay admin
workflow. Generation and background agreement sweeps do not lift the label cap.

A **flagged prompt still advances the walk.** A physician who rejected point 3's
prompt never predicted anything at point 3, so nothing of theirs is destroyed by
point 4, and requiring a verdict would strand them there forever.

---

## 6. Why these points are not in κ

`agreement` requires `blinded = True` to enter the κ computation. **Blinding is
about not seeing the other labeler's identity. It says nothing about temporal
independence.**

A physician who labels encounter *k* and then *k+1* is blinded on both. What the
two observations share is not a co-labeler; it is their own model of that patient,
formed at *k* and carried into *k+1*. Aggregate that and you are measuring
within-physician consistency and reporting it as between-physician agreement — on
the one number a buyer audits.

The observation is still **recorded**, with `kappa_excluded_reason =
'trajectory_sequential'` stamped on it, so the exclusion is auditable rather than
invisible. The exclusion is derived inside `store.upsert_agreement`, not passed by
callers: "excluded by construction" has to mean by construction.

These points carry **outcome verification** instead, reported in
`quality_report.md` under its own heading and never folded into κ.

---

## 7. What ships to a buyer

Per record, in the `trajectory` annex (outside the profile schema, like `review`
and `supervision`):

| field | why it matters |
|---|---|
| `trajectory_id` / `sequence_index` | **the reassembly key.** The bundle is one line per record. Group by trajectory and sort by sequence to reconstruct the walk |
| `point_class` / `presenting_narrative` / `downgraded` | decision or interval, narrative evidence and downgrade reason; missing legacy classes remain null |
| `point_counts` | distinct shipped decision, interval and unclassified points per trajectory; multiple records or labels count once |
| `expected_trajectory.falsifiers[]` | the falsifier corpus: a stated, expert-authored, chart-checkable falsifier is a reward function for a clinical RL environment, written by a board-certified specialist |
| `expected_trajectory.falsifiable` | filter on it — a physician who could not name a falsifier is allowed to say so |
| `self_score.marks[]` | held / did not hold / not assessable |
| `outcome_verified` | true only where something was actually checkable |

`cases.jsonl` carries the identity, class, downgrade reason and point counts at case level too, and the
per-physician `trajectory` block on each label (two physicians on one decision
point write two different falsifiers).

The datasheet and buyer manifest report class counts for the points actually
shipped after profile filtering; they do not imply that a partial export is a
complete walk. Calendar audit timestamps and monetary fields are omitted from
chart-walk buyer files. Dated taxonomy/configuration versions use stable SHA-256
identifiers. Stored source payloads and internal audit timestamps remain intact.
Calendar-dated license expiry is rejected without changing the license or export
state. Every text companion is scanned before records are marked exported.

P5 is still pending: the reveal endpoint currently derives its delta from the
next stored task. The separate P5 branch must persist the sealed per-point
outcome so a failed or retired successor cannot change the reveal boundary.

Everything above is in `data_dictionary.md`, along with the limits — an
undocumented field in a delivered artifact is indistinguishable from a leak.

---

## 8. The limits, stated

These ship in the data dictionary, in `trajectory.LIMITATIONS`, and on the
physician's screen at the moment they grade. A buyer's methodologist will test
every one of them.

* **Not a controlled experiment.** What happened next reflects the treatment
  actually given, not the physician's plan. Where they proposed something
  different, the outcome tests the plan that was followed. **Score anticipation of
  the observed trajectory; never counterfactual outcomes.**
* **Confounding by indication.** Sicker patients get more aggressive treatment. A
  model trained naively on chart trajectories learns the treatment pattern, not
  the reasoning. Score the stated reasoning and expectation, not the plan's
  similarity to what was done.
* **Uneven density.** Decision and interval counts are separate; both pay physicians $75 per submission.
* **Survivorship.** These charts continue because the patient continued.
  Encounters ending in death or transfer are absent by construction — and that is
  exactly where the interesting failures live.
* **`study_findings_policy` varies within one walk.** It is computed per
  truncation: a window with no imaging is `visible`, a later one carrying a study
  asset is `hidden`. The same patient presents under two policies in one session,
  by design.

---

## 9. The trap that would quarantine every early point

`required_modalities` is computed **from the truncated window**, never inherited
from the parent chart (`ingestion.modalities_present_in`).

Inheriting is the obvious implementation, which is why it is ruled out in writing
in the source. A case truncated at encounter *k* legitimately lacks modalities the
full chart carries — patient-1's ERCP report exists at day −1242, and a case
truncated at day −1810 must not contain it or claim to. Inherit, and
`completeness_check` returns `missing = ['ERCP procedure report']` — a token it
recognised and confirmed absent — which quarantines the case with:

> *"the case's decisive evidence is absent — quarantining rather than shipping an
> unanswerable case"*

A clinical-sounding rejection for what is correct behaviour, on every early
decision point in every trajectory.

A decision point is not an incomplete case. It is a **complete case about an
earlier moment.**

---

## 10. Where things live

| Concern | File |
|---|---|
| Policy (the sequence rule, κ exclusion, falsifier shapes, the metric, the limits) | `asclepius/trajectory.py` — pure, imports nothing from the package |
| Segmentation, the density gate, pairing, truncation, the outcome delta | `asclepius/real_cases.py` |
| Per-truncation modality declaration | `asclepius/ingestion.py` |
| Columns, the sequence gate SQL, walk queries | `asclepius/store.py` |
| Capacity policy | `asclepius/routing.py`, `asclepius/agreement.py` |
| Endpoints, the 409, the reveal | `routers/asclepius.py` |
| The annex and the data dictionary | `asclepius/packaging.py`, `asclepius/export.py` |
| Physician + admin surfaces | `frontend/asclepius/asclepius.js` |
| Tests | `tests/test_asclepius_longitudinal.py`, `tests/test_asclepius_longitudinal_ui.py` |
