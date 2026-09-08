# Asclepius, start to finish, on your machine

Start it:  `cd "~/Claude Code/Archangel-Health" && ./scripts/dev-hub.sh`

Portal `http://localhost:8000/asclepius` · landing `http://localhost:5173` ·
admin `http://localhost:8000/asclepius/admin` · community
`http://localhost:8000/community`

Emails are never sent. `EMAIL_DEV_MODE=1` prints them to the server terminal.

---

## The cast

| Role | Login | Password |
|---|---|---|
| Approved labeler | `newdoc@demo.local` | `NewDoc-2026` |
| Second labeler | `labeler2@demo.local` | `Labeler2-2026` |
| Reviewer | `reviewer@demo.local` | `Reviewer-2026` |
| Sandbox contributor | `mockadmin` | `MockContributor-2026` |
| Admin | `admin@localhost` | `dev-admin-password` |

Everyone signs in at **http://localhost:8000/asclepius**, except the admin
console, which has its own door at **/asclepius/admin**.

**There is no standing "brand-new physician" account, on purpose.** The point of
that state is the twenty minutes after somebody applies, and the honest way to
see it is to apply. §1 takes about three minutes.

---

## 1 · Apply, as a physician who has never been here

**http://localhost:5173/physicians** → "Become a contributor" → **/join**

Or mint a link straight into the wizard. Note the address: `.local` is a
reserved TLD and the validator refuses it, so use `example.com`.

```bash
curl -s -X POST http://localhost:8000/api/onboarding/self-serve \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","first_name":"Ada","last_name":"Reyes"}'
```

Screen 1 takes a password you choose. Screen 2 wants a six-digit code, which is
printed to the server terminal rather than sent:

```bash
grep -i "DEV ONBOARDING OTP" <the dev-hub terminal>
```

Then the CV step, then **the review screen**, which is the one worth looking at.
It is three collapsible boxes, each saying why it is asking, with an honest
count. Only two fields block Submit and they say `Required`. The NPI says
`Needed` in pink with a note underneath, and **you can still submit without it**:
red means "missing and it matters", never "you may not send this". Fields the CV
filled in carry a lime `from your CV` chip, because those are our reading of
your CV and not yet your word.

## 2 · The twenty minutes after applying

Sign in as the account you just made. You land on **Welcome to Archangel**, not
inside a case, and you are offered two doors: be emailed when we decide, or
start onboarding now.

Take "start onboarding". You get the explainer, then **step one: learning how to
label** (a demo video and the practice case, both optional and both labelled so),
then the examination.

Choosing "just email me" is not a dead end. The dashboard still offers the way
in, because the commonest reason to pick it is not yet knowing the rest is
fifteen minutes.

The left rail: Tasks is open, Community, Referral and Earnings each carry a
small padlock. They are **view only**, and they work: clicking Community opens a
fixture of what the rooms look like, clearly banded as a preview, so an applicant
can see what they are applying to without a single real colleague reaching an
account nobody has checked.

## 3 · The examination

One real case in the applicant's own specialty, in the real workspace with the
real validation. A cardiologist gets a cardiology case; someone whose specialty
we hold no case set for gets nephrology **and is told so on screen**.

You can pause it and go back to the learning materials, and the draft survives.

Nothing is graded on submit and no verdict is ever shown to the physician.

## 4 · Approve them

`admin@localhost` → **Physicians**. The queue has an **Examination** column
beside Practice case. Open a row and the dossier leads with the **Examination**
card: which candidate the case was authored to make wrong and which they
rejected, which of the answer key's data points their writing reached for, the
key itself, and what they wrote before they saw either candidate.

Every line is a fact with the key beside it. There is no score, no band and no
pass mark, because whether this physician is good enough is your call.

Approve, then **assign a tier**:

- **Labeler** draws and labels cases
- **Reviewer** can additionally adjudicate a completed pair

No tier means no work.

## 5 · Label, pair, adjudicate

Sign in as a labeler → **Tasks**. **12 nephrology cases are queued**, 10
multimodal and 2 text, all rated hard.

The anti-peeking mechanism is the thing to look at: the AI's candidate answers
are withheld until your own answer commits server-side, and the order is
enforced by the API rather than the honour system, so at packaging time we can
prove the physician wrote first.

Three cases sit at "awaiting second label". Sign in as `labeler2@demo.local`,
label one of those, and the pair completes. Then `reviewer@demo.local` can
adjudicate it.

## 6 · Money

| | |
|---|---|
| Task labeled | **$75** |
| Review session | **$100** |
| Referral bounty (referrer) | **$50** |
| Referral bonus (new joiner) | **$25** |

A review session only qualifies past a minimum duration floor. It is 60 seconds
locally (`ASCLEPIUS_TR_MIN_SECONDS=60`) so a demo pays out in one sitting;
production is 20 minutes.

## 7 · Referral

Two things for a physician: a personalised link whose **Copy invite** puts a
whole message on the clipboard rather than a bare URL, and an email invitation.

Three for a health system: a copy link, "I work at a health system, connect it",
and a three-field introduction. No consent checkbox: what we do with it is
stated beside the button, and the attestation is now recorded rather than merely
demanded.

## 8 · The community, and the morning routine

`newdoc@demo.local` → Community opens the real thing.

The morning routine posts events, medical AI news, research and opportunities,
and a discussion prompt at 7am local per channel. It needs `ANTHROPIC_API_KEY`
to source anything. To watch it work with no key at all, on throwaway
databases:

```bash
TMP=$(mktemp -d)
cd backend && ASCLEPIUS_LLM_PROVIDER=fake COMMUNITY_FAKE_SEARCH=1 \
  COMMUNITY_MORNING_ENABLED=1 COMMUNITY_DB_PATH=$TMP/c.db \
  ASCLEPIUS_DB_PATH=$TMP/a.db TEAM_DB_PATH=$TMP/t.db \
  python3 -c "import asyncio, main; from community import morning; \
  print(asyncio.run(morning.run_morning(force=True)))"
```

See `docs/asclepius/MORNING_ROUTINE_SETUP.md` for what each reason means.

## 9 · Where cases come from

Admin → mint an upload link (purpose is required at mint time). Hospital portal:
**http://localhost:8000/provider**

Uploads arrive with **no specialty**, because specialty is a property of the
data and hospital IT is never asked for it. Promote stays disabled until you
pick one. Say that part out loud when you demo it: ingest refusing to guess is
the product working, because a wrong specialty routes the case to the wrong
physician pool and mislabels it in the export, invisibly, and neither is
recoverable once the bundle ships.

## 10 · Quality and export

Admin → Exports and Metrics. **κ will read `null`.** The floor is 30
double-labeled observations; below that the report prints "kappa is not
reportable below 30" instead of a number. Say it before a buyer finds it: we
return nothing rather than a number nobody should trust.

`mockadmin` submissions are hard-excluded from every export.

---

## Two things this instance has that a stock one does not

Two lines in `backend/.env` (local dev only; delete them for the stock
instance):

    DEMO_MODE=1                  # seeds demo data
    ASCLEPIUS_TR_MIN_SECONDS=60  # payout floor, so earnings appear in one sitting

## What is deliberately missing

**No founder photograph is committed.** Three files are referenced by name and
none is in git: they are photographs of real people, `/email-assets` is public
and unauthenticated, and a blank placeholder would be worse than the absence,
since every consumer degrades to something deliberate (initials, or the names
alone, or no image element). See `backend/assets/README.md` for the three paths.

**No onboarding demo video is installed.** The "Watch the demo" card hides
itself until an admin uploads one under Admin → Health.
