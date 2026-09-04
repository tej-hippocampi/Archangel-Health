# Health-system onboarding

An organization goes from the landing page to an active, DLA-signed upload
portal without a phone call. This is what happens between those two points, and
which module owns each part of it.

## The path

```
landing dialog          three fields, one Continue
  │                     POST /api/asclepius/hs/signup   (no password)
  ▼
six-digit code          POST /api/asclepius/hs/signup/verify
  │                     ├─ health_systems row (never merged by name)
  │                     ├─ portal account, invite token hash, must_reset=1
  │                     ├─ onboarding_state = intake
  │                     └─ the §2.3 access email, carrying a CLAIM LINK
  ▼
portal · claim          /provider?invite=<token>
  │                     GET  /hs/invite/{token}          (found:false, never 404)
  │                     POST /hs/invite/{token}/claim
  │                     ├─ their name and THEIR password
  │                     ├─ token cleared, must_reset cleared, session_epoch++
  │                     └─ signed in, no second login
  ▼
portal · intake         four questions + teammates
  │                     POST /hs/application  → state = submitted
  │                     a teammate added here gets the SAME claim link,
  │                     never a passphrase
  ▼
admin · submitted       the four answers verbatim, two buttons
  │                     POST /admin/health-systems/{id}/approve
  │                     ├─ every active account → approval_status = approved
  │                     ├─ state = approved_awaiting_dla
  │                     └─ the agreement email, to EVERY member
  ▼
portal · awaiting DLA   full text on screen, two checkboxes, typed name + title
  │                     POST /hs/agreement/sign
  │                     ├─ signed_agreements row (append-only, by trigger)
  │                     ├─ countersigned PDF → asset store
  │                     ├─ state = active
  │                     └─ copies emailed: the signer, us, and everyone
  ▼
portal · active         uploads open
```

`Decline` is the other outcome at the admin step: a required reason on every
account, every account deactivated, `state = declined`, and no email — at this
deal size a refusal is a conversation somebody has.

## Nothing that guards an account travels by email

Two of the three doors used to mail a generated passphrase and a username
derived from the organization name (`unique_hs_username`: "Mass General
Hospital" becomes `massgeneral`). Both now mail a **claim link** instead, and
the recipient sets their own password on arrival.

| Door | What is mailed | Where |
|---|---|---|
| A colleague adds a teammate | claim link | `POST /hs/members` |
| Self-signup clears its code | claim link | `POST /hs/signup/verify` |
| An operator provisions a partner | passphrase, still | `routers/asclepius_admin.py` |

The third one keeps the passphrase deliberately. It is the case where somebody
is handed an account they did not ask for, during a call, and the credential is
read out loud rather than clicked.

Three things the link buys, in order of how much they matter:

* **No credential in an inbox.** A passphrase in an email is a passphrase in
  every forward of that email, and it guarded the account until somebody got
  round to replacing it.
* **No forced-reset screen.** The first thing a partner did in the product used
  to be replacing something we had given them a minute earlier.
* **A name.** The account had none, so the portal header rendered the derived
  username and greeted a hospital executive as "Berkeley Health System /
  Berkeley 2". Claiming asks for a name, which is why `renderHeader` has one to
  show, and why it now falls back to the account email rather than to the
  username.

Only the SHA-256 of the token is stored (`hs_portal_users.invite_token_hash`),
following `routers/asclepius.py::_token_hash`: a database holding live invite
tokens is a database whose backup is a set of working accounts. The claim clears
the hash in the same statement that writes the password, which is what makes it
single use. `GET /hs/invite/{token}` answers **200 with `found: false`** for
unknown, expired and already-spent alike, for the same reason
`GET /api/asclepius/hs-referral/{token}` does: a 404 turns a token guess into a
question about whether a health system is talking to us.

## Before any of this: the /partner lead

`/partner` is the other door, the link at the bottom of the health-system
one-pager, and it produces a `lead_submissions` row rather than an account. It
used to end at a Calendly button on its own success screen, so a visitor who did
not click it in that second was gone: nothing had been sent, there was no
address we had said anything to, and nothing to follow up.

The screen now says thank you and offers nothing to click. The booking link
lives in the email the submit triggers, where it can be forwarded to whoever
holds the calendar. Three clock columns on the row carry the whole schedule, and
each is written by exactly one thing:

| Column | Written by | Means |
|---|---|---|
| `thanks_sent_at` | `submit_lead`, only on a send that succeeded | the reminder's clock has started |
| `reminder_sent_at` | `partner_lead_nudge.sweep`, claimed before sending | the one reminder is spent |
| `call_booked_at` | an operator, `POST /api/leads/admin/{id}/booked` | stop chasing |

There is no second reminder, ever. Idempotency is the conditional
`UPDATE ... WHERE reminder_sent_at IS NULL`, not something the scheduler
remembers, so a restart cannot double-send and two workers racing one row cannot
both send. The sweep rides the verification agent's poll loop beside
`onboarding_nudge`, which is the same cadence and the same reason: a second
scheduler is a second thing to get wrong on deploy.

Calendly does not call us back, so `call_booked_at` can only be set by the person
who saw the meeting appear in their calendar. That is the button on the lead row
in the admin console, and what it buys is silence rather than a letter asking a
partner to book a meeting they are already coming to.

## Two gates, not one

They are different objects and both apply to every upload.

| | Asks | Reads | Lives in |
|---|---|---|---|
| **Access** | may this LOGIN touch this surface | `hs_portal_users.approval_status` | `asclepius/hs_access.py` |
| **State** | has this ORGANIZATION finished the paperwork | `health_systems.onboarding_state` | `asclepius/hs_states.py` |

They are separate because one organization holds several logins and the
agreement is signed **once, by one of them, on behalf of all of them**. Collapse
them and you get either every member re-signing the same contract or one
member's approval silently authorizing another member's session.

Both are enforced server-side in `_hs_upload_preconditions`, which every one of
the four upload doors calls. The rail in the portal reads both too, but that is
what the page LOOKS like — it is never what is allowed.

## The zero-backfill rule

A NULL `onboarding_state` means *this organization predates the state machine*.
`hs_states.state_of` collapses it to `active`, exactly as
`hs_access.access_level` collapses a NULL `approval_status` to full. No sweep
stamps a state on rows that predate the question, so no hospital that has been
uploading for months wakes up locked out on a deploy.

The cost, stated because it is invisible otherwise: an operator cannot tell a
legacy partner from a signed one by state alone. That is why the admin list
renders the DLA chip **separately** — "active, no agreement on file" is a real
and visible condition.

## The agreement

`docs/legal/DLA_v1.md` is **source**, not documentation. `asclepius/dla.py`
reads it at request time, substitutes the organization's name, and renders it
into the portal.

* **What is hashed** is the exact text the signer saw — after the name goes in
  and with the signature lines still blank. Not the file on disk (two
  organizations sign different texts) and not the PDF (its bytes carry a
  timestamp that did not exist when they read the words).
* **The effective date is a phrase**, not a date, so the text is identical at
  read time and at signature and the two hashes agree. The real date is in the
  signature record.
* **Editing a version anyone has signed is wrong.** It changes the file's hash
  and produces a document that no longer matches the signatures already taken.
  Fix a typo by shipping `DLA_v2.md` and bumping `dla.CURRENT_VERSION`.
* **The row is the record.** Everything the PDF prints is on it, so both
  download paths rebuild from the row when the blob is missing — and the admin
  one sets `x-agreement-source: rebuilt-from-row` rather than passing a rebuild
  off as the stored artifact.
* **`signed_agreements` is append-only, enforced by SQLite triggers.** UPDATE
  and DELETE abort. INSERT is untouched, which is what makes a re-signed newer
  version a new row.

### Packaging

The Dockerfile has to `COPY docs/legal/`, and `.dockerignore` has to carry
`!docs/legal/*.md` **after** its blanket `*.md`. Get either wrong and every
agreement page 503s on a deploy with nothing failing in CI.
`tests/test_hs_onboarding.py::test_the_agreement_ships_with_the_application`
holds both.

## Everything lands in storage

There are three purposes, not two: `task_creation`, `brokering`, and **`storage`
— the default**. An upload arrives, is stored, and is used for **nothing** until
a person has read the file and said what it is for.

```
upload ──▶ storage ──read it──▶ task_creation   promote controls open
                     └────────▶ brokering       leaves the task workflow entirely
```

* `asclepius.ingestion.blocks_promotion()` is the gate, and it is an
  **allowlist**: only `task_creation` passes. The old test was
  `not is_brokering(...)`, which was correct while there were exactly two
  purposes and silently admitted the third the moment it existed.
* **NULL now means storage.** It used to resolve to `task_creation`, so a row
  from before the column existed kept promoting — the one place the system
  decided something consequential because nobody had decided it. It fails closed
  now. The cost is real and is the point: rows that predate this stop being
  promotable until somebody resolves them, and the control that resolves one is
  on the row.
* `is_brokering()` still means *literally brokering*, and is what the brokering
  bucket and the export rules read. The two questions came apart when storage
  arrived; conflating them again is how an unreviewed file becomes a task.
* Accounts are minted as storage by `create_hs_portal_user`. The default lives
  with the **column**, not with a caller, because the provider router mints
  accounts on the self-signup path and is forbidden from naming a purpose at all.

The admin's **Held in storage** bucket is the review queue: download, read, then
`Set: Task creation` or `Set: Brokering` on the row. It carries no Promote
button — the same rule brokering has, because a control that 409s teaches an
operator to work around the workflow rather than through it.

## Files

| Piece | Where |
|---|---|
| The state machine | `backend/asclepius/hs_states.py` |
| Minting an account (one path, three callers) | `backend/asclepius/hs_provisioning.py` |
| The lead that arrives before any of this (`/partner`) | `backend/routers/leads.py`, `landing/.../PartnerInterest.tsx` |
| Its thanks letter, and the one reminder | `backend/asclepius/partner_lead_nudge.py` |
| Agreement source, rendering, hashing, PDF | `backend/asclepius/dla.py`, `docs/legal/DLA_v1.md` |
| The PDF writer | `backend/asclepius/pdf_render.py` |
| Partner-facing routes | `backend/routers/asclepius_provider.py` |
| Operator-facing routes | `backend/routers/asclepius_admin.py` |
| The portal | `frontend/provider/` |
| The admin section | `frontend/asclepius/admin_health.js` |
| The landing dialogs | `landing/src/app/components/Sign{In,Up}Dialog.tsx` |
| Tests | `backend/tests/test_hs_onboarding.py`, `test_hs_signin_split.py`, `test_hs_invite_claim.py`, `test_partner_lead_followup.py` |

## One duplication, on purpose

The question wording exists twice: `_HS_APPLICATION_QUESTIONS` in the provider
router (what the partner reads) and `_HS_ANSWER_WORDS` in the admin router (what
the operator reads). The provider file is held to a stricter standard than the
admin file and is scanned by `test_purpose_isolation.py`; importing across that
boundary to save eight lines would be the first crack in a separation the whole
isolation suite rests on.
`test_hs_onboarding.py::test_the_two_copies_of_the_answer_wording_stay_in_step`
is the cost of that decision, paid in one test.
