# Referrals

What a physician is told they can earn, what the ledger actually pays, and why
those two have to keep matching.

## The offer

| | |
|---|---|
| **$50** to the referrer | once, when a physician they referred has their **first case accepted** |
| **$25** to the person referred | a first-case bonus, on that same case |
| **No ceiling** | there is no lifetime limit on referral earnings |

Both amounts live in env (`ASCLEPIUS_REFERRAL_BOUNTY_CENTS`,
`ASCLEPIUS_REFEREE_BONUS_CENTS`) and are **stamped on the ledger row at
accrual**, so changing a rate can never restate a bounty already earned. The
Referral tab reads them off the wire (`funnel.payout_structure`) rather than
hardcoding a figure the env could change underneath it.

Accepted, not submitted. `accrue_referral_bounty` is guarded on
`has_approved_task_earning`, so a rushed case that fails QA pays no bounty. The
invitee's $25 only lands when the referrer's $50 settles, inheriting every
guard that settlement runs: no self-referral, live account, QA-accepted work.

## There is no ceiling, and there was

`referral_cap_cents()` used to default to **520000** ($5,200, or 104 bounties)
and the tab led with "Earn up to $5,200" in the largest type on the page.

That is a limit, printed first, to the one physician we most want introducing us
to a hundred colleagues. It capped exactly the person the program exists for and
told them so before it told them anything else.

It now defaults to **0, which means uncapped**, and the enforcement in
`_eligible()` is skipped entirely when the cap is zero:

```python
cap = referral_cap_cents()
if cap <= 0:
    return True
```

That branch is deliberately not deleted. Setting `ASCLEPIUS_REFERRAL_CAP_CENTS`
reimposes a cap without a deploy, and it is still tested.

**Watch for this if you ever set it:** a naive `earned + bounty > cap` with
`cap = 0` is true for everybody, so a zero would have made *every* referral
settle as `BOUNTY_INELIGIBLE` while the page advertised no limit. Zero has to
mean "no cap" everywhere, including `referrals.funnel()`'s `capped` flag and
`admin_earnings.js`, which otherwise renders the sentence "ceiling $0.00 per
referrer".

## Health systems are an interest form

No dollar figure, no percentage, no worked example.

The card used to say: *"a $1M data partnership at a 15 to 20 percent introducer
share is $150,000 to $200,000 for the person who made the introduction."*
Institutional terms are negotiated one deal at a time, so a number printed on
this page becomes a promise the negotiation then has to keep. A physician who
read $200,000 and was paid a fraction of it would be right to feel misled, and
right that we named the figure first and unprompted.

What it says now: if you work at, know, or run a health system that might sell
de-identified records, send a note and we will set up a meeting. The note goes
to `ENTERPRISE_NOTE_EMAIL`; a person replies.

The textarea carries **no `maxlength` and no character counter**. The server
still bounds the note, because it is pasted into an email body and an unbounded
field is a free abuse vector, but the bound is far past anything a person
writes; if it is ever hit the 422 detail lands in the inline error, which is
where a limit belongs.

## Attribution is the link, and only the link

`/join?ref=CODE` becomes a `referrals` row keyed on the invitee's email at
`/self-serve`, and `claim_referral_for_signup` attaches it at provisioning, so
closing the tab and resuming from the emailed link still credits correctly.

**Nothing anywhere asks anyone to type a code.** A manual step is one a
colleague can forget, mistype, or never be told about, and every one of those is
an introduction a physician made and does not get paid for. `POST
/step1-identity` also follows an email change, because the identity screen lets
you edit the address the attribution is keyed on.

## Where the numbers are stated

Keep these in step. Two of them are public.

| Surface | File |
|---|---|
| Rates + cap | `backend/asclepius/payments.py` |
| Funnel payload | `backend/asclepius/referrals.py` (`payout_structure`) |
| The Referral tab | `frontend/asclepius/referral.js` (reads the wire) |
| Admin money tab | `frontend/asclepius/admin_earnings.js` |
| **Public marketing** | `landing/.../routes/PhysiciansPage.tsx` |

That last one advertised **"$50–$100 / referral"** — a range matching no
constant in the codebase, which would have been half wrong on the first payout.
It is derived from nothing and nothing tests it, so check it by hand when a rate
moves.
