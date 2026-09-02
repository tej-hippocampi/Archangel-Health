# PRD 2 — Longitudinal cases: the next encounter is the answer key

**Design proposal. Read §1 before the mechanics — it is the reason to build this.**

Measured against `patient-1` … `patient-4`.

---

## §1 The thesis

Your YC application says the hard part out loud:

> *Medical post-training has no verifier. Code and math have an answer key you can run a
> script against. "Was this the right treatment, did the patient get better" doesn't.*

That is true **prospectively**. It is false **retrospectively.**

A chart that continues past a decision contains what happened after that decision. If a
physician commits to a plan at day −1242 and the record continues to day −1202, the record
itself says whether the reasoning held. **The next encounter is the verifier.**

This is the difference between selling preference data and selling a **verifiable reward
environment**, and it is the largest step available to you on price. RLVR — reinforcement
learning from verifiable rewards — is the technique labs most want and least often get
outside code and math. Medicine has been assumed impossible because the reward is
unobservable. In a longitudinal chart it is observable; it is just delayed.

### 1.1 Why this is worth more, in the buyer's language

| | Single-shot case | Longitudinal case |
|---|---|---|
| What ships | one preference pair per case | a trajectory: state → action → outcome, repeated |
| Reward | human preference | **preference AND a retrospectively verifiable outcome** |
| Trains | SFT, DPO | SFT, DPO, **process reward models, RLVR** |
| Failure it exposes | wrong answer | wrong answer, *and* right answer for a reason that does not generalise |
| Buyer | anyone selling annotation | labs building agents that act over time |

Current research is unambiguous that this is the axis that matters. Step-level process
supervision beats outcome-only supervision across reasoning tasks; 2026 work on agentic RL
finds step-wise PRM signal *essential* for long-horizon tasks, with process-supervised
models outperforming both SFT and outcome-only RL. A longitudinal chart is the only place
in medicine where you can produce that signal **and** check it against something that
actually happened.

### 1.2 The second argument: it is your own stated moat, earlier

You argue that the next wave of RLHF data is *"sequential, in-context clinical judgment"*
and only exists where care happens. That is true of workflow capture, which needs a hospital
deployment and a year. **A longitudinal chart is sequential, in-context clinical judgment
that you already own.** This ships the thesis now, on data in your possession, and it is the
natural on-ramp to the RL environment you are already building.

---

## §2 What the records actually contain

Segmenting on activity gaps > 7 days (`real_cases.py:122 segment_longitudinal_record`,
already built):

| Chart | Span | Active dates | Encounters | Pass density gate | **Verifiable** |
|---|---|---|---|---|---|
| patient-1 | 5.0 y | 62 | 23 | 14 | **13** |
| patient-2 | 20.7 y | 29 | 17 | 3 | **2** |
| patient-3 | 2.6 y | 14 | 6 | 4 | **3** |
| patient-4 | 10.5 y | 25 | 13 | 4 | **3** |
| **Total** | | **130** | **59** | **25** | **21** |

Density gate: ≥ 2 distinct dates, ≥ 8 events, ≥ 2 resource types. "Verifiable" means a
later qualifying encounter exists to check the decision against.

**Four charts yield 21 verifiable decision points.** The same four charts yield three
single-shot cases. That is a 7× increase in sellable decisions from data you have already
paid for, and each unit is worth more than the ones it replaces.

Patient-1 alone carries 13. Its day −1242…−1202 encounter holds 221 events across five
resource types over 40 days — an entire admission, with the ERCP, the post-procedure
pancreatitis, and the enzyme resolution all inside one window.

### 2.1 Most encounters are not decision points, and that is fine

34 of 59 fail the gate — single-date, few-event contacts. **Do not lower the gate to raise
the count.** A repeat lab draw is not a decision, and a task built on one teaches a model
that medicine is a series of trivia questions. The gate is the product.

---

## §3 Task design

### 3.1 The unit: a decision point with a sealed future

For each qualifying encounter *k* in chart *C*:

```
STATE      everything in C up to and including encounter k
QUESTION   derived from what actually changed at k
ACTION     the physician commits to: assessment, plan, and what they expect to see
OUTCOME    encounter k+1 — SEALED until the action is committed
```

The physician answers with the chart truncated at *k*. They cannot see forward. Once
committed, encounter *k+1* is revealed and they grade their own anticipation.

### 3.2 Why the commitment must be sealed

If the physician can see the future, the task collapses into narration. The seal is what
converts an opinion into a **prediction**, and a prediction is the only thing an outcome can
verify. This is the same reason your models are run blind before a case ships.

### 3.3 What the physician commits to

Three fields, and the third is the one buyers do not have:

1. **Assessment** — what is going on
2. **Plan** — what to do
3. **Expected trajectory** — *what should happen next if this assessment is right, and what would tell me I am wrong*

Field 3 is the reward function, written by a specialist, in advance. It is a falsifiable
prediction attached to a real chart, and there is nowhere else to buy it.

Use Case A to see it work. At encounter *k* the physician sees bilirubin 15.04 → 17.77 with
GGT 1361 → 123. A strong answer says: *drainage worked; bilirubin is delta-bound and lags;
I expect enzymes to stay down and bilirubin to fall over 2–3 weeks; if GGT climbs again, the
stent has occluded.* Encounter *k+1* shows GGT back at 983. **The physician's own stated
falsifier fired, and the chart proves it.** No human graded that. The record did.

### 3.4 Scoring, three signals per decision point

| Signal | Source | Trains |
|---|---|---|
| Preference between two physicians | reviewer (PRD 1) | DPO |
| Step-level correctness | reviewer's `step_divergence` | process reward models |
| **Anticipation vs. what happened** | **the next encounter** | **RLVR** |

The third is automatic and free of human grading, which is what makes it scale past the
constraint your whole business runs into: subspecialist hours.

### 3.5 Chain the points into a trajectory

Do not stop at independent decision points. Run a physician through encounters *k*, *k+1*,
*k+2* in order, revealing each outcome only after commitment. That produces an actual
trajectory over one patient — state, action, observed transition — which is the literal
shape an RL environment consumes, and it is your V5 product with real medicine in it
instead of a simulation.

Patient-1 supports a 13-step trajectory. That is a single sellable artifact worth
considerably more than 13 cases.

---

## §4 What to build

**Phase 1 — segmentation and gating.** `segment_longitudinal_record` exists. Add
`qualify_encounter()` implementing the §2 gate, and `pair_decision_points()` returning
`(k, k+1)` pairs where both qualify.

**Phase 2 — truncated case rendering.** The case panel must render a chart *as of* an
offset. Everything downstream of `end_offset` is withheld — not styled as hidden, **absent
from the payload**. A truncation implemented in CSS is a leak.

**Phase 3 — the commitment surface.** Assessment, plan, expected trajectory. Reuse the
labeler's authoring surface; add the third field.

**Phase 4 — reveal and self-score.** After commit, show encounter *k+1* and ask the
physician to mark which of their expectations held. Their own falsifier is the rubric.

**Phase 5 — trajectory mode.** Chain the points, one session, sealed reveals.

### 4.1 The correctness rule that governs all of it

**Truncation is a server responsibility.** The client must never receive data it is meant
not to show. State it in the module docstring, test it by asserting the served payload
contains no offset greater than `end_offset`, and never rely on the frontend to hide a
future the physician is being asked to predict.

---

### 4.2 Schema impact — what changes, and the one thing that will quarantine everything

**`ClinicalCase` does not change.** A truncated case is the same object with fewer items in
the timed collections. `lab_panels`, `notes`, `studies`, `medications` and `problem_list`
already carry `collected_offset_days` (`real_cases.py:59, 95`), so truncation is a filter,
not a new shape. `extra="forbid"` stays untouched.

Four things around it do change. The first is a trap.

#### 4.2.1 `required_modalities` must be recomputed per truncation — not inherited

`ingestion.py:1883`:

```python
if comp["missing"]:
    raise cf.CaseIngestError(
        f"bundle declares required modalities not delivered: {sorted(comp['missing'])}; "
        f"the case's decisive evidence is absent — quarantining rather than "
        f"shipping an unanswerable case")
```

`completeness_check` (`ingestion.py:1205`) returns `{present, missing, unresolved}` and
**only `missing` quarantines** — a token it recognised and confirmed absent.

A case truncated at encounter *k* **legitimately lacks** modalities the full chart carries.
Patient-1's ERCP report exists at day −1242; a case truncated at day −1810 must not contain
it. Inherit the chart's declaration and every early decision point produces
`missing = ['ERCP procedure report']` → **recognised, confirmed absent → quarantine.**

Worse, the failure reads as *"the case's decisive evidence is absent"* — a clinical-sounding
rejection for what is actually correct behaviour.

```python
# Compute the declaration FROM the truncated window, never from the parent chart.
# A decision point is not an incomplete case; it is a complete case about an
# earlier moment. Declaring the chart's full modality set on it asserts evidence
# the physician is not supposed to have yet.
required_modalities = modalities_present_in(case_truncated_at(chart, k))
```

Inheriting is the obvious implementation, which is exactly why it has to be ruled out in
writing.

#### 4.2.2 Tasks need an ordering; do not reuse `env_runs`

Today the physician queue has no grouping: one chart → one case → one task. Add to the task
row:

```
trajectory_id    TEXT     -- shared across all decision points from one chart
sequence_index   INTEGER  -- 0-based position; ordering is the whole point
```

**Do not put these in `env_runs`.** That table already carries trajectory vocabulary
(`store.py:722`: *"a `mode='rollout'` row is one agent trajectory over that environment,
sharing `task_id`"*) — but it holds **agent rollouts for V5**, not physician sessions.
Same word, different actor. Merging them makes "trajectory" ambiguous in exactly the table a
buyer audits.

#### 4.2.3 One new submission field, and it must reach the export

`expected_trajectory` is genuinely new — no existing field is a variant of it. It needs a
column on the submission and a line in the export annex and data dictionary, or the
falsifier corpus (§7) ships invisible.

#### 4.2.4 Trajectory points must NOT enter the κ pool

This is subtle and will not announce itself.

`agreement.py:32` requires `blinded = True` to enter the κ computation, and `_blinded_only`
(line 193) enforces it. But **blinding is about not seeing the other labeler's identity — it
says nothing about temporal independence.**

A physician who labels encounter *k* and then *k+1* is blinded on both. Both observations
pass the gate and enter κ. What they share is not a co-labeler; it is **their own model of
that patient**, formed at *k* and carried into *k+1*. Aggregate that and you are measuring
within-physician consistency and reporting it as between-physician agreement — on the one
number a buyer audits.

```python
# Trajectory decision points are excluded from the κ pool by construction.
# Blinding does not make sequential labels by one physician independent; they
# share a prior the physician formed at the previous encounter. These points
# carry outcome verification instead, which is a stronger claim than agreement.
```

Report them as their own named metric, for the same reason `export.py:331` keeps review
acceptance and κ separately named.

#### 4.2.5 The export is per-record; a trajectory is not

The bundle writes one JSONL line per record. Thirteen decision points from patient-1 ship as
thirteen disconnected lines unless `trajectory_id` and `sequence_index` travel with them and
the data dictionary explains the relation. A buyer who cannot reassemble the sequence has
bought thirteen single-shot cases at a trajectory price, and will say so.


## §5 Why the physician experience gets better, not worse

Counter-intuitive, and worth stating because it sounds like more work:

**The question stops being artificial.** A single-shot case asks a specialist to answer a
question assembled for them. A longitudinal case asks the question their actual job asks:
here is a patient, here is what has happened, what now.

**They find out if they were right.** Nothing in your current product tells a physician
whether their judgment held. This does — from the chart, not from a reviewer's opinion.
That is the single most requested thing in expert annotation work and the reason good
clinicians disengage from labeling.

**Context loads once, amortised over several decisions.** Reading a new chart is the
expensive part of a task. A trajectory session pays that cost once and then asks four or
five questions against it. Per-decision time goes *down* even though session time goes up.

**It reads as medicine.** Your differentiator is that physicians are treated as clinicians
rather than annotators. A sealed-future trajectory over a real patient is the most
clinician-shaped thing you could put in front of them.

---

## §6 Risks, honestly

**The chart is not a controlled experiment.** What happened next reflects the treatment
actually given, not the physician's plan. If they propose something different, the outcome
does not test *their* plan — it tests the one that was followed. **Score anticipation of
the observed trajectory, never counterfactual outcomes.** Anything else is a claim the data
cannot support, and a buyer's methodologist will catch it.

**Confounding by indication.** Sicker patients get more aggressive treatment. A model
trained naively on chart trajectories learns the treatment pattern, not the reasoning.
Mitigate by scoring the *stated reasoning and expectation*, not the plan's similarity to
what was done.

**Density is unevenly distributed.** Patient-1 gives 13; patient-2 gives 2. Yield per chart
is not predictable, which matters for pricing a dataset by record count. Price by **decision
point**, not by chart.

**Survivorship.** These charts continue because the patient continued. Encounters that end
in death or transfer are absent by construction, and that is exactly where the interesting
failures live. Say so in the data dictionary.

---

## §7 What to sell, and how to price it

| Product | Unit | Notes |
|---|---|---|
| Verified decision point | 1 | preference + step signal + outcome check |
| Chained trajectory | 1 chart | 13 points on patient-1; the RL-environment unit |
| Falsifier corpus | per point | specialist-written "what would tell me I am wrong" — nobody else has this |

The falsifier corpus is the sleeper. A stated, expert-authored, chart-checkable falsifier is
a reward function for a clinical RL environment, written by a board-certified specialist.
That is not annotation, and it should not be priced like it.

---

## §8 What I would validate before building Phase 3

1. Run 3 of the 21 points past frontier models blind. If they anticipate the trajectory
   correctly, the point is not hard enough to sell — same discipline as your existing gate.
2. Put one in front of two advisors and time it. If a decision point takes more than 20
   minutes, the encounter window is too wide.
3. Confirm with one buyer that outcome-verified decision points price above preference
   pairs **before** building Phases 4 and 5. The thesis is strong; the price is untested.

Sources: [Process-level reward modeling for agentic tasks](https://arxiv.org/html/2604.24198v1) · [Agentic RL, ICLR 2026](https://proceedings.iclr.cc/paper_files/paper/2026/file/d1e6739f319be4ba8e748cc05f23bba1-Paper-Conference.pdf) · [PAIR: prefix-aware internal reward for multi-turn agents](https://arxiv.org/pdf/2605.17877)

---

## §9 Production-safety scan

Run against `Archangel-Health-main (21)`. Everything below is verified in code, not inferred.

### 9.1 BLOCKER — the queue's priority sort breaks the seal

`store.py:232`:

```python
_PRD_R_PRIORITY_ORDER = f"ORDER BY {_PRD_R_LABEL_COUNT} DESC, t.created_at ASC"
```

**Label count is the PRIMARY sort key.** A task carrying one label is offered before every
unlabelled task (PRD R §1.2 — a case awaiting its second label jumps the queue). `created_at`
only breaks ties.

Now put patient-1's 13 decision points in that queue, inserted in sequence order:

1. Physician A labels point 0. Point 0 now has `label_count = 1`.
2. Physician B labels point 5 — for any reason: they drew it, it was flagged, capacity lifted.
   Point 5 now has `label_count = 1`.
3. Physician A returns. Point 0 is excluded (they wrote it). **Point 5 sorts first**, ahead of
   points 1–4, which are still at 0.
4. Physician A is served **encounter 5**, whose state block contains encounters 1–4 — the
   outcomes of the four decisions they were supposed to predict.

**The seal is gone, and so is the RLVR claim for that physician's whole trajectory.** This is
not a race condition or an edge case; it is the ordinary behaviour of the priority sort the
moment two physicians work the same chart.

**Required fix — a sequence gate in the candidate query, not in the UI.**

```sql
-- A trajectory point is servable to THIS evaluator only when every earlier point
-- in its trajectory already carries a submission FROM THIS EVALUATOR. Sequence is
-- a correctness property of the task, so it belongs in the query that decides
-- servability — never in the frontend, which cannot enforce it against a
-- hand-typed task id or a second tab.
AND (
  t.trajectory_id IS NULL
  OR NOT EXISTS (
    SELECT 1 FROM tasks p
    WHERE p.trajectory_id = t.trajectory_id
      AND p.sequence_index < t.sequence_index
      AND NOT EXISTS (
        SELECT 1 FROM submissions s
        WHERE s.task_id = p.task_id AND s.evaluator_id = :evaluator_id
      )
  )
)
```

`t.trajectory_id IS NULL` first, so every existing V1–V4 task is unaffected by construction.

**Also gate the direct-open path.** `openTaskById` and `GET /tasks/{id}` must apply the same
rule and return **409** on an out-of-order trajectory point. A queue-only fix is not a fix —
the physician has the task id in the URL.

### 9.2 `insert_task` needs a signature change

`store.py:3554` is keyword-only with every parameter written out explicitly. Adding
`trajectory_id` and `sequence_index` means editing that signature, the INSERT, and the
`tasks` table.

Follow the pattern `open_to_all_specialties` already set (line 3575) — additive column,
explicit caller decision, never derived, with the migration written as
`if col not in cols("tasks"): ALTER TABLE ... ADD COLUMN` and **no DEFAULT**, matching the
house rule at `store.py:1957`.

### 9.3 Cost — state it before you generate 21 of these

`payments.py:198` — `tl_rate_cents()` is **$75 per completed submission**, and a decision
point is a submission.

| | Points | Single-labelled | Double-labelled (`PAIR_LABELS = 2`) |
|---|---|---|---|
| patient-1 | 13 | $975 | **$1,950** |
| All four charts | 21 | $1,575 | **$3,150** |

A trajectory is not a discount on physician time; it is 13 tasks that happen to share a
chart. Price the product accordingly (§7) and decide deliberately whether trajectory points
are double-labelled at all — see 9.6.

### 9.4 Verified safe — no change needed

| Concern | Finding |
|---|---|
| Dedupe by `patient_key` blocking 13 cases from one chart | No such guard exists |
| A "one case per patient per labeler" rule | None in `routing.py` |
| Draft collisions across points | Drafts key on `task_id` (`DRAFT_PREFIX`), so distinct |
| `assert_no_answer_leakage` misfiring on truncation | Explicitly written not to quarantine notes that legitimately state a diagnosis (`ingestion.py:1443`) |

### 9.5 `study_findings_policy` will vary across one trajectory

`ingestion.py:1871` sets it per case: `"hidden" if any_asset else "visible"`. A truncation
with no imaging gets `visible`; a later one carrying a study asset gets `hidden`.

That is defensible — findings visibility should reflect what that window contains — but it
means the *same patient* presents under two policies within one session, and a physician may
notice findings disappearing as they move forward in time. Decide it deliberately and note
it in the data dictionary rather than discovering it in a buyer's diligence.

### 9.6 Each point carries its own phase; nothing chains them

`routing.py:40` — `PHASES = (awaiting_first, awaiting_second, review_ready, adjudicated)` is
per task. Thirteen points means thirteen independent phases. A trajectory can therefore sit
half-adjudicated, and `wants_second_label` may lift capacity on point 7 while point 3 has no
first label.

Combined with 9.1, that is the mechanism that produces out-of-order serving. The sequence
gate fixes the symptom; the honest structural answer is:

**Do not double-label trajectory points by default.** They are excluded from the κ pool
anyway (§4.2.4), so the second label buys no agreement statistic — it buys a second
independent trajectory, which is a different and more expensive product. Set
`max_labels = 1` on trajectory points and make double-walking an explicit, priced decision.

### 9.7 Ship order

1. Migration + `insert_task` (9.2) — additive, nothing observes it yet
2. **Sequence gate in the candidate query AND the direct-open path (9.1)** — before a single
   trajectory task exists in production
3. Per-truncation `required_modalities` (§4.2.1)
4. κ exclusion (§4.2.4)
5. Then generate the first trajectory, on one chart, with `max_labels = 1`

Steps 1–4 are invisible to physicians and safe to ship alone. **Do not create a trajectory
task before step 2 lands** — the first one will be served out of order and the seal cannot
be un-broken after a physician has read forward.
