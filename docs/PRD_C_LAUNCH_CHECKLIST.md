# PRD C — launch checklist

**There are TWO independent blockers between a deployed system and the first task reviewer.
Both must clear. Resolving one ships zero TRs and looks like a modelling problem.**

That is the whole reason this file exists. An earlier version of the docs named only the empty
calibration item bank, because that is the blocker that fails loudly. The other one — the OIG
exclusion snapshot — fails *quietly and identically to model indecision*: every physician lands
in the admin band with `proposed_tier: null`, which is exactly what an over-cautious classifier
looks like.

Check the live answer at any time:

```
GET /api/asclepius/verify/readiness      →  {"tr_possible": false, "outstanding": [...]}
```

---

## Blocker 1 — OIG LEIE exclusion snapshot · owner: **operations**

**Symptom if skipped:** hard gate A5 answers `unknown` for every physician. `propose()`
short-circuits on any undetermined gate, so **no one is proposed as a reviewer regardless of
their score.** Nothing errors. Nothing is red. The queue simply never proposes.

This is correct behaviour — a never-loaded exclusion list must not answer "clear", because a
check that fails open is not a check — and it is indistinguishable from the model being
cautious unless you know to look.

**To clear:**

1. Download the monthly updated LEIE file from `https://oig.hhs.gov/exclusions/exclusions_list.asp`
   (the "Updated LEIE Database" CSV, ~100 MB).
2. `POST /api/asclepius/verify/leie/load` with `{"csv_text": "<file contents>", "source_note": "YYYY-MM"}`.
3. Confirm: `GET /api/asclepius/verify/leie/status` → `{"gate_a5": "active"}`.

**Cadence: monthly.** OIG republishes monthly and the snapshot is a point-in-time copy. The
loader is atomic — DELETE and INSERT inside one transaction — so a reader never observes an
empty table mid-load and concludes everyone is clear.

Rows without a 10-digit NPI are dropped rather than name-matched. Roughly half the file has a
blank NPI, and a false positive on this gate ends someone's participation, so the only
acceptable match is an exact identifier.

## Blocker 2 — calibration exam item bank · owner: **clinical**

**Symptom if skipped:** `GET /api/asclepius/verify/calibration/exam` returns **503** with
`ExamNotAvailable: <specialty>: 0 admissible keyed items, need 12`. Signup is unaffected; the
physician simply cannot sit the exam, so `calibration exam at the TR gate` stays in
`tr_missing` forever.

This one fails loudly, which is why it was the blocker everyone already knew about.

**To clear, per specialty:**

1. Select ≥12 real cases from the actual task distribution — `calibration.build_item_from_task()`
   sources directly from `tasks` rows, which is what keeps the exam measuring the job rather
   than a textbook.
2. Have **≥3 experts independently judge each one**: which of the two model answers is better,
   which reasoning step breaks, which criteria are load-bearing. Independently — discussion
   raises within-pair agreement without raising across-pair reliability, so a panel that
   deliberated is not three judgments.
3. `calibration.aggregate_panel()` turns those into a key. **An item whose panel did not
   converge on the better answer is not admissible** and will not be served: if the experts
   disagree, the item measures the item, not the candidate.
4. Confirm: `GET /api/asclepius/verify/readiness` → `calibration_item_bank.blocking == false`.

Budget realistically: 12 items × 3 experts × 3 specialties = 108 independent judgments. This is
the long pole in the release and it is only half an engineering problem.

---

## Not blocking, but do it before the first physician signs up

| | Owner | Why |
|---|---|---|
| **Counsel review** of `PRD_C_COUNSEL_MEMO.md` | founder | NY/NYC extend anti-discrimination protection to independent contractors; NYC Local Law 144 may require an annual bias audit. §5 of the memo lists seven specific questions. |
| **`ASCLEPIUS_FAIRNESS_SALT`** set to a real secret | operations | Falls back to `ASCLEPIUS_AUTH_SECRET`, then to a constant. On the constant, the demographics pseudonyms are guessable and the separation §6 promises is gone. |
| **`ASCLEPIUS_ASSET_STORE`** on a mounted volume | founder | Context pack §7.1 — CV blobs live here. Railway's ephemeral disk is wiped on redeploy. |
| Decide the **demographics collection moment** | product | The fairness monitor is structurally correct and permanently empty until physicians are asked. Asking during signup pressures people to answer; asking after approval biases who responds. This is a product call, not a technical one. |

## Ongoing, once live

- **Weekly:** `GET /api/asclepius/verify/fairness` — four-fifths comparison by group, plus the
  per-feature breakdown that says *which* feature carries a gap (`feature_alerts`).
- **Weekly:** `GET /api/asclepius/verify/tiering-weights` — watch `drift` and `sd`. The `sd`
  column should be shrinking. If it stops while decisions are still arriving, the loop is dead
  rather than converged, and those two look identical from anywhere else.
- **Every batch:** `POST /api/asclepius/verify/tiering-weights/apply` returns `quarantined`.
  Anything above zero is a bug in whatever wrote those rows; the decisions are preserved and
  re-runnable, but they are not learning until someone fixes them.
- **Monthly:** re-load the LEIE snapshot (blocker 1).
