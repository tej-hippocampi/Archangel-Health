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

## Health systems: we capture a contact, and we still print no figure

Two separate rules used to be bundled together here, and only one of them was
load-bearing.

The rule that **stays**: no dollar figure, no percentage, no worked example, for
an institutional introduction. The card once said *"a $1M data partnership at a
15 to 20 percent introducer share is $150,000 to $200,000 for the person who
made the introduction."* Institutional terms are negotiated one deal at a time,
so a number printed on this page becomes a promise the negotiation then has to
keep. A physician who read $200,000 and was paid a fraction of it would be right
to feel misled, and right that we named the figure first and unprompted. That
reasoning is untouched by anything below, and it is asserted rather than
trusted: `test_referral_section_dom` fails on a `$` or a `%` anywhere in the column
(including the copyable blurb), and `test_hs_referral_email` fails on one
anywhere in the email body.

The rule that **went**: that the card should therefore be a free-text note.

Those do not follow from each other, and pairing them cost us the thing the card
exists for. A physician typed a paragraph, `ENTERPRISE_NOTE_EMAIL` got it, a
founder replied **to the physician**, and the person they actually wanted us
to meet never heard from anybody. Meanwhile the hero directly above the card
advertised institutional introductions as *"the largest agreements we sign"*.
The page promised the biggest thing you can do here and then routed it into a
mailbox.

### What it does now

The card asks who: their name, their email, their role, the health system, and
how the physician knows them. Then:

1. **A row, immediately.** `hs_referrals`, with a `landing_token` minted at
   insert. Written before the response returns, so the introduction exists and
   is attributable even if everything after it fails.
2. **One light enrichment call** (`asclepius/hs_enrich.py`): does this person
   hold this role, is this a real provider organization, and is there any reason
   not to write at all. Deliberately small; see that module's header for why a
   thorough research step is the wrong trade here.
3. **An introduction email to the contact**, in the physician's name, with
   reply-to set to the physician. Institutional offer, not the contributor one:
   license de-identified records, and separately their physicians can earn on
   evaluation work. An hourly rate quoted to a COO is aimed at the wrong person.
4. **A founder alert either way**, saying who was written to and which body
   they saw, or that nothing was sent and why.
5. **A funnel row the referrer can watch**, in sentences, with no amount column.

### The gate on personalization

Research is an enhancement to an email that is already worth sending. The
enriched fact is cited only when `hs_enrich.may_personalize` holds: a fact, a
source URL for it, medium-or-better confidence, and a confirmed organization.
Anything short of all four sends the clean body instead, one fewer sentence,
rather than one wrong sentence in front of the person whose introduction a
physician staked their own relationship on.

Two outcomes stop the send entirely: `do_not_contact` from enrichment (a
positive finding, not an absence of findings), and
`ASCLEPIUS_HS_REFERRAL_SEND_ENABLED=0`. Both still record the row and still
alert a founder, if something is wrong with what we are sending, the lead a
physician just handed us is the last thing that should be lost while it is fixed.

### Why `hs_referrals` is its own table

"Two referral tables is how a bounty gets paid twice" is the warning at the top
of this file, and it holds for a second *physician* referral system. This is
not one.

`accrue_referral_bounty`, `claim_referral_for_signup`,
`advance_referral_for_user` and `sweep_expiries` all assume physician semantics:
a signup, a first accepted case, a 90-day expiry, a rate stamped at accrual. An
institutional introduction has none of those. Threading a `kind` column through
`referrals` would put a discriminator inside the money path that every future
edit has to remember, and forgetting it once pays a physician bounty for a
health-system introduction.

`hs_referrals` has **no accrual path at all**. The only way money attaches to
one is an admin typing an amount
(`POST /admin/hs-referrals/{id}/reward`, kind `hs_referral`), which is how
`hs_payouts` already works and for the same reason: there is no rate to compute
because there is nothing to compute it from. It cannot double-pay by
construction rather than by vigilance, and `test_hs_referrals` asserts an
untouched `earnings` table across the whole flow.

### Consent is a field, not a formality

The email says a person you know asked us to write, and puts their address on
the reply-to. The checkbox is where the physician asserts that is true before we
say it on their behalf. It is required by the endpoint, not only by the form.

### The public prefill endpoint

`GET /api/asclepius/hs-referral/{token}` is unauthenticated and returns only
what that person was already sent, their own name, address, role, organization,
and the referring physician's first name. Not the referrer's address, not the
note, not the enrichment. An unknown token is a **200 with `found: false`**, not
a 404: a 404 would make it a membership oracle, and the page should render an
ordinary empty form for a stale link anyway.

## Attribution is the link, and only the link

`/join?ref=CODE` becomes a `referrals` row keyed on the invitee's email at
`/self-serve`, and `claim_referral_for_signup` attaches it at provisioning, so
closing the tab and resuming from the emailed link still credits correctly.

**Nothing anywhere asks anyone to type a code.** A manual step is one a
colleague can forget, mistype, or never be told about, and every one of those is
an introduction a physician made and does not get paid for. `POST
/step1-identity` also follows an email change, because the identity screen lets
you edit the address the attribution is keyed on.

**`/partner` honours `?ref=` too, and did not used to.** `partner_url` has
always built `/partner?ref=CODE&hs=TOKEN`, but the page read only `hs`, the
per-referral landing token that prefills the form. A physician who copied the
plain referral link out of their own dashboard and forwarded it to a health
system therefore got no credit at all, which is the cheapest introduction we
ever get and the one we most want repeated. `PartnerInterest.tsx` now sends
`referral_code` as well, `routers/leads.py` resolves it with
`get_user_by_referral_code` and stores the resolved USER ID on
`lead_submissions.referred_by_user_id`, and the admin lead row prints the
physician by name.

The id, not the code: a code can be reissued, and the question the row has to
answer months later is which person made the introduction. An unknown code is a
silent no-op and never an error, because a stale link must not cost us a health
system's submission.

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
