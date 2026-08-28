# Quality-adjusted pay

Ships **off**. `ASCLEPIUS_PAYOUT_QUALITY_ENABLED=0`.

## What it does

Pay was flat: `amount_cents = rate_cents` for every payable submission. Quality
acted only as a binary gate, where any payable verdict paid the full rate and
only "reject both" paid nothing.

With this on, a case's payment is `rate x multiplier`, where the multiplier
comes from two case-outcome facts:

- the **stamped per-case quality number** (`contributor_score.case_score`), and
- the **reviewer's verdict**, where "accept with edits" means a reviewer had to
  correct the work before it could ship.

Defaults: 85+ pays +15%, 70 to 85 pays the rate, 55 to 70 pays -10%, below 55
pays -25%, and an accept-with-edits is a further -10%. Bounded to a 0.60 floor
and a 1.25 ceiling.

The floor is a backstop, not a working number. The worst the default weights can
do is -35%, so the floor is never actually reached; if a later change starts
hitting it, the weights moved too far.

## The four rules it is built on

**It takes no physician attribute as input.** Only facts about the case and how
it was graded. Not who labelled it, not their tier, not their history, not their
score across other cases. `test_payout.py` asserts this on the function
signature: a value it cannot receive is one it cannot weigh. Same discipline as
`tiering.FORBIDDEN_CREDENTIAL_KEYS` and the pinned-to-zero protected features.

**It proposes; a human decides.** Any multiplier below 1.0 holds the ledger row
(`earnings.quality_hold`) instead of approving it. Neither a verdict nor the
fourteen-day auto-approve window can approve a held row. An admin decides it at
`POST /api/asclepius/admin/earnings/{id}/release`, and may override the proposal
and pay the full rate. The decision is attributed and timestamped, because
reducing a physician's pay is consequential and an unattributable reduction
cannot be appealed.

**Nothing is restated.** The multiplier, its reasons and its ruleset version are
stamped onto the earning while it is still `accrued`, and `set_earning_quality`
refuses a row that is approved or paid. The case-quality number underneath it is
stamped the same way, keyed on `contributor_score.CASE_QUALITY_VERSION`. Tuning
a coefficient never changes what a physician was already paid and told.

**Every adjustment is itemized**, in the same signed convention `credentialing`
uses, and the reasons ride onto the physician's own Earnings page. A silent
deduction is the worst possible version of this feature.

## What it does not change

The no-clawback rule is untouched: a later accept may restore money, a later
reject never takes back money already approved. A rejected case is still voided
rather than reduced. Reviewer session pay is untouched, since it is paid per
qualifying session rather than per case.

## Before switching it on

This is algorithmic management of contractor compensation. It is a stronger
version of the question the tiering work already went to outside counsel on
under **NYC Local Law 144**: `docs/PRD_C_COUNSEL_MEMO.md` covers how a physician
is classified TL or TR, and does **not** cover how they are paid.

The admin gate, the floor, the itemized reasons and the no-physician-attributes
rule are what make this defensible, and they are in the design for that reason.
The recommendation is that the memo be extended to cover pay before this flag is
turned on, rather than after.

## Operationally

`GET /api/asclepius/admin/earnings/held` is the queue of proposed reductions
waiting on a person. If it grows, physicians are waiting on us. That queue
existing and being visible is the difference between "the tool proposes" and "an
automated decision with extra steps".
