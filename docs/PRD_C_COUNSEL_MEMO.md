# Counsel memo — algorithmic tiering of physician contractors

**For:** outside counsel · employment / AI-hiring practice
**Re:** Asclepius physician tiering (TL vs TR), `backend/asclepius/tiering.py`
**Prepared by:** engineering. **This is a disclosure document, not a legal opinion.**

Context pack §7.4 flags this for review: New York State and New York City extend
anti-discrimination protection to independent contractors, and **NYC Local Law 144** may
require an annual bias audit for automated employment decision tools. Everything below is
written so the reviewer can decide whether LL144 applies without reading the code.

---

## 1. What the tool does, and what it does not

Physicians sign up as independent contractors to label clinical cases. Every physician is
classified as a **task labeler (TL)** — builds a case answer from scratch — or a **task
reviewer (TR)** — grades what two TLs produced. TR is *not* a promotion or a seniority rank; it
is a **per-domain role**, and the same physician is a TR on a nephrology case and not on a
cardiology one. Pay differs between the two roles.

**The tool proposes; a human decides.** No tier is ever assigned by the model. The admin
approval endpoint rejects any request that does not carry an explicit tier, and the admin's
override is recorded and fed back as the training signal. Whether "an automated tool that
produces a scored recommendation a human then acts on" is an AEDT under LL144 is the first
question for counsel; we have assumed it is and built accordingly.

Two layers:

- **Seven hard gates (A1–A7)** — eligibility. Rules, never learned, never overridable by the
  score. NPI verified against the federal NPPES registry; state licence; physician degree;
  residency complete; not on the OIG exclusion list; no active board disciplinary action;
  signed confidentiality and independence attestations.
- **A nine-parameter logistic score** — ranks *within* the eligible set. It updates from admin
  overrides by regularized Bayesian logistic regression.

---

## 2. What is deliberately never collected, derived, or logged

Enforced in code, not by policy. `tiering.FORBIDDEN_CREDENTIAL_KEYS`:

medical school name or rank · US-MD vs IMG · ECFMG certification *as a score input* · graduation
year · date of birth · age · sex · gender · continuous years in practice · practice ZIP or
region · name origin · self-rated expertise.

`basic.sex` is returned by the free NPPES API and is **stripped at the ingest boundary** before
any caller sees the record.

Seven protected proxies — `sex`, `img_status`, `grad_year`, `med_school_tier`,
`practice_region`, `age`, `name_origin` — exist as **real rows in the weight table** with
weight pinned to `0.0` and precision `1e6`. They pass through the learning update on every
batch and are asserted to have moved by exactly `0.0`. The test feeds them values that
correlate *perfectly* with the admin's decision — the adversarial case — and they still do not
move. An independent audit additionally corrupted the database directly
(`UPDATE tiering_weights SET m=3.0, pinned=0 WHERE feature='sex'`) and the guardrail restored
them on the next batch.

**Demographics for the fairness monitor are collected separately, voluntarily, and are not
joinable to the model.** They live in their own table keyed by an HMAC pseudonym; the decided
tier and the feature vector are copied onto that row at decision time, so the monitor needs no
join back to the physician record. The scoring path has no route to them.

---

## 3. Features that reach the score and are demographic-adjacent

**These are the items counsel most needs to see.** Each is a genuine, job-related criterion
that we also believe carries an adverse-impact exposure. None can be pinned to zero without
deleting a real criterion, so each is mitigated and monitored instead. The residual exposure is
stated rather than argued away.

### 3.1 `currently_practicing` — weight 0.80 (third-largest term)

**What it measures.** Whether the physician is currently engaged in clinical practice. Currency
of practice is what a clinical trial's Clinical Events Committee selects for; a physician who
stopped seeing patients years ago has illness scripts that are no longer current.

**The exposure.** Part-time clinical practice is not evenly distributed by **sex**, **caregiving
status**, or **disability**. The original encoding was a hard binary cliff — full weight at four
or more clinical half-days per month, zero below it — so a physician working three half-days
scored identically to one who had left medicine entirely. A physician on parental or medical
leave scored the same, because leave produces the same number: zero.

**Mitigation, shipped.** Three ordinal levels instead of a cliff:

| | |
|---|---|
| **1.0** | practising at the usual cadence (≥4 clinical half-days/month), **or on protected leave** from such a practice — the pre-leave figure is scored, not the leave |
| **0.5** | practising part-time (1–3 half-days/month) |
| **0.0** | not currently in clinical practice |

Protected leave is an explicit enumerated field — parental, medical, caregiving, military,
sabbatical — precisely because "0 half-days because I am on leave" and "0 half-days because I
stopped practising in 2019" are the same number and opposite facts. A blank pre-leave figure is
treated as a missing answer, not a zero.

**Residual exposure.** Part-time practice still scores 0.40 below full practice. We believe
this is job-related and consistent with business necessity — but it is a judgment, and it is
the judgment we most want reviewed. The alternative (collapsing to "practises at all vs not")
would remove the exposure and also most of the feature's signal.

### 3.2 `structured_review_exp` — weight 0.70

**What it measures.** Whether the physician has adjudicated against a rubric before: Clinical
Events Committee or DSMB service, journal peer review, board item writing, guideline panel
membership, core faculty or program director. This is the closest available proxy for the
actual task, and it is the criterion with the clearest job-relatedness in the model.

**The exposure.** Every one of those activities is in practice gated on **academic medical
center affiliation**, which is strongly associated with **IMG status** and **national origin**.
Both of those are pinned to zero in the model — so the model cannot use them directly, and this
feature can carry the same difference into the score anyway. It is a route around the pin.

**Mitigation, shipped.** It cannot be pinned without deleting a real criterion, so it is
**monitored**: the fairness table now records the feature vector alongside the decided tier, and
the monitor reports each feature's mean by self-reported group with a four-fifths comparison. If
`structured_review_exp` is materially lower for a group, the monitor names *that feature*, not
merely the outcome gap — so the mechanism is visible rather than inferred.

**Residual exposure.** Real and unquantified until there are enough volunteers in the fairness
table to measure. The monitor is the control; it is not yet evidence.

### 3.3 `post_residency_ge_3yr` — weight 0.50, and the unresolved conflict

**What it measures.** At least three years since completing residency.

**The exposure and the conflict.** Gate A4 requires a residency completion *year*. Product
policy says never collect graduation year, because it is the most direct available proxy for
age. Those two requirements are in direct conflict and no engineering choice dissolves it —
A4 has no other evidence source.

**Mitigation, shipped.** The year is read exactly twice — once by gate A4, once to compute this
boolean — and discarded at both. There is no continuous years-in-practice term anywhere in the
model, deliberately: the Choudhry systematic review in *Annals of Internal Medicine* found an
**inverse** relationship between years in practice and quality of care, so a continuous term
would be both legally exposed and empirically backwards. Medical school name and medical school
graduation year have been removed from the signup form entirely.

**Residual exposure.** Residency completion year is still stored, and it is an age proxy sitting
in the database one boolean away from a tiering decision. A property test asserts that varying
it — along with eleven other protected proxies — changes the score by exactly `0.0`. Whether
storing it at all is acceptable is a legal question, not an engineering one, and it is the
second thing we most want reviewed.

---

## 4. The controls, and what they actually prove

| Control | Proves | Does not prove |
|---|---|---|
| Pinned zero weights + immobility assertion | The learned model cannot rediscover a protected characteristic through admin behaviour | Nothing about features that are *not* pinned — see §3 |
| `FORBIDDEN_CREDENTIAL_KEYS` + AST test over the encoder | The scoring code does not read a protected proxy, checked against the source rather than a list | Nothing about correlated features it *does* read |
| Hard gates evaluated before the score | A high score can never open a closed gate | Nothing about whether the gates themselves are fair |
| Four-fifths monitor by voluntary self-report | Outcome disparity, per dimension, when volunteers exist | Anything at all until physicians volunteer — it is currently empty |
| Per-feature breakdown (§3.2) | *Which* feature carries a disparity | Causation |
| `|Δm| ≤ 0.25` per batch + `applied_at` replay guard | One admin cannot rewrite the model; no decision is counted twice | — |

**The honest limit on all of it:** the model has nine parameters and will be trained on roughly
one hundred admin decisions. It cannot discover a criterion nobody encoded; it can only
re-weight the ones that were. Every criterion in it was chosen by a human and every one is
listed above.

---

## 5. Specific questions for counsel

1. Does a scored recommendation that a human must explicitly override or accept constitute an
   **AEDT under NYC Local Law 144**, given that the tier is never written without an explicit
   human decision? If so, the annual bias audit and the candidate notice obligations attach.
2. **§3.1** — is the 0.40 residual penalty on part-time clinical practice defensible as
   job-related and consistent with business necessity, given the known sex/caregiving/disability
   distribution of part-time work?
3. **§3.2** — is monitoring an adequate control for a feature we believe correlates with IMG
   status and national origin, or does it need a hard mitigation?
4. **§3.3** — may we store residency completion year at all, given it is an age proxy, when the
   only alternative is dropping a hard eligibility gate?
5. Does the voluntary demographics collection itself create exposure, and is the HMAC-pseudonym
   design sufficient separation?
6. TL and TR are paid differently. Does that make this a **compensation** decision rather than
   an assignment decision, and does that change the analysis?
7. What notice, if any, must a physician receive that an automated tool contributed to their
   tier — and does our current signup flow provide it? (Today it does not.)

---

## 6. Where to look in the code

| | |
|---|---|
| Features, weights, pinned proxies | `backend/asclepius/tiering.py` — `FEATURES`, `PINNED_ZERO`, `FORBIDDEN_CREDENTIAL_KEYS` |
| The §3.1 encoding | `tiering._practising_value` |
| Hard gates | `tiering.hard_gates` |
| Learning update + guardrails | `tiering.fit_batch`, `tiering.apply_guardrails` |
| Fairness monitor | `store.fairness_selection_rates`, `GET /api/asclepius/verify/fairness` |
| Evidence for every claim above | `backend/tests/test_tiering_score.py`, `test_tiering_learning.py`, `test_tiering_audit_c.py` |

Design departures and their reasoning: `docs/PRD_C_PROCESS_REVIEW.md`.
Day-one operational blockers: `docs/PRD_C_LAUNCH_CHECKLIST.md`.
