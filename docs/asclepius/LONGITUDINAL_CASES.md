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

## 2. What clears the gate, and why you must not lower it

An **encounter** is a cluster of recorded activity separated by a gap of more than
7 days. A **decision point** is an encounter that clears the density gate:

| threshold | value |
|---|---|
| distinct dates | ≥ 2 |
| recorded events | ≥ 8 |
| resource types (labs / notes / studies / vitals) | ≥ 2 |

Measured across `patient-1` … `patient-4`: **55 encounters → 22 decision points →
18 verifiable ones.** The rest fail, and they fail because they are single-date,
few-event contacts.

> **Where that number comes from — corrected.** The four charts ARE in this
> repository now (`asclepius/fixtures/patient_bundles/`), so the figure is
> measured rather than inherited. Running the shipped ingestion path over them
> gives 55 → 22 → 18; this document previously quoted **59 → 25 → 21** from the
> PRD, measured elsewhere on copies we did not hold. Both are recorded because
> the difference matters to anyone auditing a pitch: quote 55 / 22 / 18, which
> `test_longitudinal_front_door.py` pins per chart so a gate change cannot move it
> quietly.
>
> The difference is **not** gate drift. patient-1's thirteen-point walk — the
> number the product is demoed on — reproduces exactly.
>
> `patient-2` quarantines on an ambiguous date token in an OCR annotation, and is
> recoverable only through the documented override path. See the bundles' own
> README; do not relax the date scan to admit it.
>
> The other real chart in the tree, `tests/fixtures/nephrology_pgnmid_bundle.json`,
> yields **zero** decision points: it is a cross-sectional diagnostic workup,
> three of its four encounters are a single lab draw, and the fourth misses the
> event floor 4-to-8. That zero is pinned in
> `test_asclepius_longitudinal_real_bundle.py`, including the counterfactual that
> lowering the floor to admit it would still produce nothing pairable.
>
> **Yield on a NEW partner's charts is still unverified until you run the plan
> (`dry_run: true`) against them.** Quote the gate, not the number.

**Do not lower the gate to raise the count.** A repeat lab draw is not a decision,
and a task built on one teaches a model that medicine is a series of trivia
questions. Every point below the gate is a point a specialist is paid $75 to
answer and a buyer is asked to price as clinical judgment. The gate is the
product.

`apply_density_gate: false` exists on the generate request **only** to inspect
what the gate is rejecting.

### Two numbers, and they are never the same number

* **decision points** — how many encounters clear the gate.
* **verifiable decision points** — how many of those have a *later qualifying*
  encounter to be checked against. Always one fewer: the terminal point has
  nothing after it in the record.

Both are returned by the plan and both are shown in the admin console. Price by
**decision point**; yield per chart is not predictable (patient-1 gives 13,
patient-2 gives 2).

---

## 3. Generate a walk

```
POST /api/asclepius/ingestion/cases/{ingest_case_id}/generate
{ "dry_run": true }                       # the plan — writes nothing
{ "dry_run": false, "trajectory": true }  # the walk
```

The dry run returns every proposal *including the ones that were rejected*, each
with its `density` block naming which threshold it missed. A preview that lists
only the survivors reads as "this chart had two decision points" when it had
seventeen.

A live trajectory run:

* keeps only proposals that clear the density gate,
* orders them by `encounter_index` (the sequence index **is** the chronology),
* mints one `trajectory_id` and assigns `sequence_index` 0…n−1, advancing only on
  points that actually became tasks,
* forces `max_labels = 1` — see §5.

The response carries `trajectory_id`, `trajectory_points`,
`trajectory_verifiable_points` and `estimated_cost_usd`.

**Cost, before you click.** A trajectory is not a discount on physician time; it
is N tasks that happen to share a chart. At `tl_rate_cents` ($75 a completed
submission): patient-1's 13 points is **$975**, all four charts' 21 points is
**$1,575**. Double-labelled, double that.

---

## 4. What the physician sees

1. **The case, truncated.** A banner names the step ("Decision 3 of 13") and says
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
the **next** decision point — the presenting data of encounter *k+1*, not its
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
turn a $975 chart walk into $1,950 with nobody deciding to.

### The consequence you will hit in operations

At `max_labels = 1`, **the first physician to take point 0 owns the walk.** Every
later point is gated behind point 0, and point 0 is at capacity for everyone else.
That is the policy working — but a physician who takes point 0 and never returns
leaves the rest of the chart unreachable.

**The release:** lift point 0 to two labels.

```python
store.flag_tasks_for_double_label(
    [{"task_id": "<point 0>", "specialty": "<specialty>", "current_rate": None}])
```

A second physician can then start the walk from the beginning. This is a priced
decision — a second full walk — which is exactly why it is explicit.

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
| `trajectory_id` / `sequence_index` | **the reassembly key.** The bundle is one line per record. Without these, thirteen decision points arrive as thirteen unrelated rows — thirteen single-shot cases at a trajectory price, and the buyer will say so |
| `expected_trajectory.falsifiers[]` | the falsifier corpus: a stated, expert-authored, chart-checkable falsifier is a reward function for a clinical RL environment, written by a board-certified specialist |
| `expected_trajectory.falsifiable` | filter on it — a physician who could not name a falsifier is allowed to say so |
| `self_score.marks[]` | held / did not hold / not assessable |
| `outcome_verified` | true only where something was actually checkable |

`cases.jsonl` carries `trajectory_id`/`sequence_index` at case level too, and the
per-physician `trajectory` block on each label (two physicians on one decision
point write two different falsifiers).

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
* **Uneven density.** Price by decision point, not by chart.
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
