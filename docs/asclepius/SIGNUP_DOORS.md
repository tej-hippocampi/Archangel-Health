# The three doors

Three links, one product. What separates them is what the person holding the
link is being asked to do, and what their account can reach once they are in.

| Link | Who it is for | Signup | What they get |
|---|---|---|---|
| `https://archangelhealth.ai/join` | A physician who wants to do the work | 9 screens: identity, code, password, institution, credentials ×3, attestations | Everything |
| `https://archangelhealth.ai/join?flavor=advisor` | Someone supporting us who is not a clinician | 4 screens: identity + one confidentiality line, code, password | The whole product, view-only. Referral is the one thing they can act on |
| `https://archangelhealth.ai/join?flavor=referrer` | Someone who knows doctors and holds a link | 4 screens: identity, code, password | Their referral page. Nothing else exists for them |

All three verify the mailbox with an OTP. That is what makes the short ones real
signups rather than a name typed into a box.

## The rule that holds it together

**A door that asks for less must produce an account that can do less.**

The two are wired to the same table. `ACCOUNT_KIND_BY_FLAVOR`
(`routers/onboarding.py`) decides which flavors skip the credential screens;
`_BY_ACCOUNT_KIND` (`asclepius/capabilities.py`) decides which account kinds are
capped. A flavor in one and not the other is the failure mode worth naming: a
signup that asks nothing and grants everything.
`tests/test_signup_links.py::test_the_short_order_is_offered_to_exactly_the_capped_flavors`
pins them together.

The cap is **intersected with the access level on every call**, so it survives
approval:

```python
cap = _BY_ACCOUNT_KIND.get(account_kind(user) or "")
if cap is not None:
    return granted_by_access & cap
```

An admin looking at an unfamiliar row in a queue will click Approve. That moves
an advisor to FULL and changes nothing about what they can reach. There is no
state and no admin action that gets a non-physician to `REAL_WORK`.

`flavor=general` is deliberately in neither table. It is the older
invited-non-clinical flavor, it relaxes the credential screens' *validation*
without removing them, and the account it produces is capped by nothing, so it
keeps the full wizard.

## What "view-only" means for an advisor

Surfaces: `browse`, `tutorial`, `community_read`, `earnings`, `referral`.
Read the omissions.

- **No `real_work`.** They are not a clinician and a real case carries real
  patient data. The Tasks tab still opens, onto the practice case rather than a
  padlock: they are not waiting for credentials to clear and never will be.
- **No `community_write`.** They read every channel. They do not post, react,
  pin, vote, RSVP, bookmark, or DM a physician. The people in those channels are
  talking to colleagues, and a non-clinical voice among them changes what the
  room is. Reading is enough to understand it; the confidentiality line they
  sign at signup is what covers the reading.
- **Not in `member_map`.** They are not in the directory, do not count toward
  the specialty and country channel thresholds, and never receive a mention or a
  digest email. They read the community; they are not part of it.
- **`earnings` reads zero**, honestly, which is what it does for a new physician
  too.

The practice case is safe to hand them because it is virtual end to end:
assembled in memory from `tutorial_case.py`, never inserted into `tasks`, and
its submission never enters the pipeline. They get the real scored reveal, which
is the most interesting thing in the demo.

### Enforced where it counts

`COMMUNITY_WRITE` was declared in `capabilities.py` from the start and nothing
imported it, so every content route in the community was `require_member` only:
reading and writing were the same permission. `require_poster` is what wires it
up. A provisional physician keeps `COMMUNITY_WRITE`, so **a doctor under review
posts exactly as they always did** — only accounts capped by `account_kind` lose
it.

`require_verified_member` (DMs, attachments) chains on `require_poster` rather
than on `require_member`. A DM is strictly more privileged than a channel post,
and without that chain an advisor passed it outright: a non-clinical account
carries a NULL `verification_status`, and `access_level` folds NULL in with
`approved`.

## Referral credit is automatic, everywhere

A physician sends their personal link. The person opens it. The credit is
recorded. Neither of them does anything about it.

`?ref=CODE` becomes a `referrals` row keyed on the invitee's email at
`/self-serve`, and `claim_referral_for_signup` attaches it when the account is
provisioned — so closing the tab and resuming from the email still credits
correctly.

**Nothing anywhere asks anyone to type a code.** A step someone can forget is a
referral we lose and a physician who is not paid for an introduction they
actually made.

One hole worth knowing about, now closed: attribution is keyed on the address
typed on `/join`, and the very next screen lets them edit it. Someone opening a
colleague's link with a personal address and correcting it to their hospital one
is doing something completely reasonable, and it silently cost the referrer the
credit. `POST /step1-identity` now moves any unclaimed referral to the new
address (`store.move_open_referrals`). A referral already attached to an account
is settled history and is never rewritten.

## Adding a fourth door

1. A flavor in `team_store._SIGNUP_FLAVORS`. **This whitelist is silent**: a
   value missing from it stores NULL, and NULL is the physician door. That is
   how `advisor` and `referrer` shipped as links that did nothing at all — the
   flavor never persisted, so the wizard saw no flavor and gave those people the
   full physician signup, and `finish` derived no account kind, so their
   accounts were provisioned uncapped.
2. An entry in `ACCOUNT_KIND_BY_FLAVOR` if it should get the short signup, and a
   matching entry in `KIND_BY_FLAVOR` in `OnboardingWizard.tsx`.
3. A surface set in `_BY_ACCOUNT_KIND`. Write the set literally; narrowing it
   later should be a one-line edit here rather than archaeology across six
   routers.
4. A test that the cap survives approval. That is the one that matters.
