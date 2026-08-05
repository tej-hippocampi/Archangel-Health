# PRD C — review of the specified process, and where the build departs from it

The PRD asks for a rigorous check of its own method before implementation, and for a better
process to be taken where one exists. This is that check. Everything below was verified
against the code, not reasoned about in the abstract; each departure names the test that
holds it.

**Summary.** The PRD's architecture is sound and the three §0 findings are load-bearing and
correct. Six things in the specified process do not work as written. Five are fixed here; one
is a conflict inside the PRD that only a human can settle.

---

## ✗ 1. The thresholds and the priors contradict Definition of Done §7.1

**This is the most important finding, because the PRD's own acceptance test fails on the PRD's
own numbers.**

§7.1 requires that a nephrologist "is proposed **TR** on a nephrology case and lands in the
**admin band** on a cardiology case". §3.2 gives the priors and the thresholds
(`s ≥ +1.0 → TR`, `s ≤ −1.0 → TL`). Computing it:

| | on a nephrology case | on a cardiology case |
|---|---|---|
| intercept | −2.5 | −2.5 |
| board_certified_active | +1.2 | +1.2 |
| subspecialty_certified | +0.9 | 0 (correctly gated by `1[domain_match > 0]`) |
| domain_match | +1.6 | 0 |
| post_residency_ge_3yr | +0.5 | +0.5 |
| currently_practicing | +0.8 | +0.8 |
| continuing_cert | +0.4 | +0.4 |
| structured_review_exp | +0.7 | +0.7 |
| calibration_z (=1.0) | +1.1 | +1.1 |
| **s** | **+4.70** | **+2.20** |

**+2.20 is above the +1.0 TR threshold.** The `subspecialty_certified` indicator that §3.2
correctly insists on removes only 0.9 of the 2.5 domain-linked points; the intercept plus the
six general-seniority terms carry +2.2 on their own. A score-only reading of the PRD therefore
promotes a distinguished cardiologist onto a nephrology case — which §8 names as the definitive
sign that the encoding is wrong.

The indicator encoding is necessary but not sufficient. Three ways to close the gap:

| Option | Verdict |
|---|---|
| Retune the intercept to about −4.7 | Rejected. It fixes the arithmetic and destroys the meaning — the intercept stops being "TR is the minority role" and becomes a fudge factor, and every other feature's interpretation shifts with it. |
| Make `domain_match` multiplicative over the whole score | Rejected. It is not what §3.2 specifies, it makes the model non-linear and so breaks the logistic update in §5.2, and it silently forces `s = 0` (P = 0.5) for every off-domain physician rather than saying anything true about them. |
| **Let the §3.1 eligibility rule do the job it is already specified to do** | **Taken.** |

§3.1 already states `TR_eligible ⇔ … ∧ domain match ≥ 0.5 ∧ …`, and §2 already states that the
score "ranks **within** the eligible set and can never override a gate". Composing them as
specified resolves it: the score says TR, the rule blocks it, and the result is the admin band.
§3.2's own commentary agrees — *"An off-domain subspecialist should land in the admin band,
which is the correct behaviour."*

**One thing the PRD leaves open, decided here:** when the score says TR and the rule blocks,
the proposal is `admin`, never `TL`. Being off-domain says nothing whatsoever about whether
someone is a good labeler, and routing them to TL would be a confident answer to a question
nobody asked. "The model cannot decide this one" is the honest output.

`tiering.propose` · `test_tiering_score.py::test_nephrologist_is_TR_on_nephrology_and_admin_band_on_cardiology`

---

## ✗ 2. "A few Newton steps" diverges, and it fails in the exact shape §8 warns about

§5.2 specifies minimizing the objective "by a few Newton steps". Undamped Newton on this
objective **diverges at 7 or more non-zero features**, and the way it fails is genuinely
dangerous rather than merely wrong.

Measured, on a batch of 200 decisions that flatly contradict the prior:

| non-zero features | total ‖Δm‖ produced |
|---|---|
| 2 | 3.03 |
| 4 | 5.32 |
| 6 | 6.55 |
| **7** | **0.0017** |
| **8** | **0.0000** |

The mechanism: the full step overshoots into the saturated tail of the logistic, where the
likelihood gradient vanishes. The only remaining gradient is the prior term `q(w − m)`, and the
only remaining Hessian is `Q` — so the next step is exactly `w ← m`, back to the prior. The
model reports **a delta of exactly zero after two hundred informative decisions**.

That is precisely the failure §8 names: *"A model that silently stops updating looks exactly
like a model that has converged."* The PRD's own guardrail against it — logging the delta every
run — would have reported `{}` and been believed.

Fixed with a backtracking line search satisfying the Armijo condition. The problem is convex,
so this is guaranteed to make progress and converges in a handful of steps.

`tiering.fit_batch` · `test_tiering_learning.py::test_delta_is_clipped_at_quarter_point_per_batch`,
`::test_the_model_learns_the_direction_the_admins_actually_used`

---

## ✗ 3. Precision only ever grows, so the model eventually freezes

§5.2's update is `qᵢ ← qᵢ + Σⱼ xᵢⱼ² pⱼ(1−pⱼ)`. `q` is monotonically increasing and nothing ever
reduces it. Combined with the `|Δm| ≤ 0.25` clip, a feature that has seen a few thousand
observations becomes immovable: a genuine shift in admin policy can never be tracked, and —
again — a frozen model is indistinguishable from a converged one from the outside.

Added a precision ceiling `Q_MAX = 400` (posterior sd 0.05) for unpinned features. High enough
that a well-established weight is not swung by one noisy batch, finite enough that the tail of
the learning curve stays alive. Pinned features keep `q = 1e6` and are exempt, because for them
immobility is the point.

`test_tiering_learning.py::test_precision_is_capped_so_the_model_never_fully_freezes`

---

## ✗ 4. "Nine rows" undercounts the persisted state, and the pinned rows are the interesting ones

§5.1 describes `tiering_weights` as holding nine rows. §3.2 defines nine scored features
(intercept + eight), §3.2 adds `MEASURED_QUALITY` outside that budget, and §3.3 requires the
seven protected proxies to be instantiated "as real features". That is **17 rows**, not nine.

Persisting all 17 is the correct reading, and the pinned seven are the ones that matter most:
§3.3's whole argument is that a pinned feature must be *"a cheap, auditable proof"* and *"the
concrete artifact you show a bias auditor"*. A row that exists only in a Python constant is a
promise; a row in the database that the learning loop demonstrably passes through and
demonstrably fails to move is evidence.

So the pinned features are carried through `fit_batch` on every batch, and the test feeds them
values that **correlate perfectly with the admin's decision** — the adversarial case — and
asserts they move by exactly `0.0`. A pinned feature that is always zero in the data cannot
move for trivial reasons, and a test that only proves that is worth nothing.

`test_tiering_learning.py::test_hundred_decisions_reduce_posterior_variance_and_move_pinned_by_exactly_zero`

---

## ✗ 5. Dawid–Skene's output is noisier than the weight it is given

§5.4 specifies One-Coin Dawid–Skene, which is the right choice for small datasets. But
`MEASURED_QUALITY` has prior mean **2.0** — the largest in the model — so an unshrunk z-score
swings `s` by more than the entire `domain_match` term.

Measured over 60 synthetic runs (60 items, 30% gold subset):

- separating a 0.95-accuracy annotator from a 0.55 one: **60/60 correct**
- ordering 0.95 > 0.75 > 0.55: **52/60 correct**

The estimator is reliable for the coarse distinction and not for the fine one. Feeding its raw
output into the model's heaviest weight would let an EM fixed-point coin-flip outrank domain
expertise.

Added shrinkage by `n/(n+40)` on the number of **co-labelled items** — not the physician's total
task count, which is what §5.4's `n_tasks ≥ 20` gate measures and is not what the estimate's
precision depends on — plus a hard floor of 10 co-labelled items below which no z is emitted at
all.

`tiering.measured_quality_z` ·
`test_tiering_learning.py::test_dawid_skene_fine_grained_ordering_is_noisy_and_that_is_why_z_is_shrunk`

---

## ✗ 6. §6's fairness table cannot be non-joinable if it is keyed by user id

§6 requires demographics *"stored in a table that is **not joinable into the feature store**"*.
A table with a `user_id` column is joinable; naming it otherwise is a convention enforced by
good intentions. The obvious alternative — a `users.demographics_json` column — is strictly
worse, because every feature path loads a physician with `SELECT * FROM users`.

Made structural instead:

- the row is keyed by `blake2b(user_id, key=secret)`, a per-purpose pseudonym;
- the **decided tier is copied onto the row at decision time**, so the monitor needs no join at
  all;
- `fairness_observations()` strips `subject_key` before returning, so it never leaves the store.

Possession of the whole table re-identifies nobody, and the scorer has no path to it.

`store.record_fairness_observation` ·
`test_tiering_learning.py::test_demographics_never_land_on_the_users_row`

---

## ⚠ 7. Unresolved: A4 requires a year that §3.3 forbids collecting

§2 gate A4 requires *"Residency complete, not in training — attestation **and year**"*. §3.3
says never collect graduation year. These are in direct conflict and no engineering choice
dissolves it, because A4 has no other evidence source.

What is done here, and it is mitigation rather than resolution:

1. Medical school institution and medical school graduation year are **removed from the signup
   form** — they satisfy no gate and are both on the never-collect list.
2. Residency completion year is kept, read exactly twice (gate A4, and the binary
   `post_residency_ge_3yr`), and **discarded at both**. No continuous years term exists.
3. `tiering.FORBIDDEN_CREDENTIAL_KEYS` plus a property test assert that varying `gradYear`,
   `medicalSchool`, `dob`, `sex`, `imgStatus`, `practiceZip` and six others across their
   plausible ranges changes the score by **exactly 0.0**.

**For counsel, per context pack §7.4:** residency completion year remains an age proxy stored in
the database, one boolean away from a tiering decision. The boolean is auditable and the
property test is the evidence. Whether storing it at all is acceptable under NYC Local Law 144
is a legal question, not an engineering one.

---

## ✓ What the PRD got right, and where the build follows it exactly

- **§0.1 — TR is a per-domain role.** Correct and the single most consequential decision in the
  document. Everything is keyed on `P(TR | physician, case domain)`.
- **§0.2 — years in practice as a capped binary.** Correct on both the evidence and the legal
  exposure. Implemented as specified and enforced by test.
- **§0.3 — credentials decay as measured quality arrives.** Correct, and implemented including
  the 0.6 decay at 50 tasks, with `domain_match` and `calibration_z` deliberately exempt:
  domain is a property of the case–physician pair, not a credential, and a work sample is not a
  credential either.
- **`subspecialty_certified` as `x · 1[domain_match > 0]`.** Necessary. Verified as a real
  failure of the naive encoding.
- **The prior-as-rule framing** (`m` = the rule, `q` = how sure you are). This is the best idea
  in the PRD. It makes the hand-tuned rules reviewable and correctable rather than permanent.
- **§4 — the calibration exam.** Correctly identified as the highest-value item. Built,
  including panel-aggregated keys, raw-response storage, and re-keying without re-testing.
- **§5.3 — exploration.** Correct diagnosis of the circularity: admin overrides are labels of
  admin opinion on cases the system itself routed to an admin. Thompson sampling and
  double-weighted shadow-TR outcomes both implemented.
- **§5.5 — the honest ceiling.** Accurate, and reproduced in the module docstring as instructed.
- **§8 — "the learning loop is the part most likely to be built wrong in a way nobody
  notices."** This turned out to be exactly right, twice over: findings 2 and 3 above are both
  instances of it, and both were found only because the tests were written to distinguish a
  dead loop from a converged one rather than to observe that a number changed.

## Additions the PRD did not ask for

- **A per-subscore floor on the calibration exam.** §4 scores three things separately and then
  gates on one number. A candidate scoring 1.00 on choice and 0.55 on localization composites
  to 0.85 and passes — while being unable to do the second of the three things the job is. A
  0.70 floor per subscore closes it. (`calibration.TR_SUBSCORE_FLOOR`)
- **Panel admissibility.** An item whose reference panel did not itself converge cannot key
  anything. If the experts disagree, the item does not measure the candidate — it measures the
  item, and scoring against it pushes real physicians below the gate for no reason.
- **Tri-state on every hard gate.** §1.1's discipline for NPI, applied to A2–A7. "We could not
  check" routes to the admin band; only a definitive failure blocks. This matters most for A5:
  a never-loaded OIG exclusion list answers `unknown` for everyone, never `clear`. A check that
  fails open is not a check, and `GET /verify/leie/status` says so in one word so the resulting
  indecision is not mistaken for the model's opinion.
