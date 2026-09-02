# Asclepius — the labeling product, start to finish

Running now on your machine. Portal `http://localhost:8000/asclepius`, landing `http://localhost:5173`.

Restart later:  `cd "~/Claude Code/Archangel-Health" && ./scripts/dev-hub.sh`

Emails are never sent. `EMAIL_DEV_MODE=1` prints them to the server terminal.

---

## The cast (all created and verified against a live login)

| Role | Login | Password | State |
|---|---|---|---|
| **Brand-new physician** | `newdoc@demo.local` | `NewDoc-2026` | No tier. Tutorial not started. **Start here.** |
| Second labeler | `labeler2@demo.local` | `Labeler2-2026` | Tier: Labeler |
| Reviewer | `reviewer@demo.local` | `Reviewer-2026` | Tier: Reviewer |
| Sandbox contributor | `mockadmin` | `MockContributor-2026` | Tier: Labeler, exports excluded |
| Admin | `admin@localhost` | `dev-admin-password` | Approves, tiers, exports |

All sign in at the same door: **http://localhost:8000/asclepius**

---

## 1 · How a physician arrives

**http://localhost:5173/physicians** → "Become a contributor" → **/join**

Enter an email; the backend mints a 7-day onboarding link and "emails" it (it
prints to the server terminal). The wizard at `/onboard/<token>` collects
credentials, training, board certification, CV and attestations, then an email
OTP.

A live link is already minted for you:

  http://localhost:5173/onboard/1bZCEqBGSvmuoPgmWNSgLLwXLBtlPCyU_Mw2PPU-mEY

Mint another for any address:

    curl -s -X POST http://localhost:8000/api/onboarding/self-serve \
      -H 'Content-Type: application/json' \
      -d '{"email":"you@example.com","first_name":"Ada","last_name":"Reyes"}'

Referral links are the other door: every contributor gets one
(`/join?ref=CODE`), worth **$50** to the referrer and **$25** to the new joiner.

## 2 · The waiting room — sign in as `newdoc@demo.local`

http://localhost:8000/asclepius → `newdoc@demo.local` / `NewDoc-2026`

This is the most important screen in the product and it is the one people skip.
The account has **no tier**, so it cannot draw work. Try it and the server says:

> Your account is not yet assigned a contributor tier, so it cannot draw or
> submit tasks. An admin assigns one when your credentials are approved.

Notice the left rail does not hide Tasks — it shows it **locked**, with
"Opens when your credentials clear". Hiding it would make the product look
empty at the exact moment a new physician is most excited. Referral, Earnings,
Guide and Profile are all live right now, before verification. Earnings reads
$0, which is true rather than hidden.

## 3 · The practice case — Calibration Case 1

Available to `newdoc` immediately, before any approval, and now **mandatory**:
no real case opens until it is passed.

A real hard nephrology case: creatinine rising 1.4 to 1.9 on IV furosemide in
acute decompensated heart failure. Interpret the rise, decide what to do with
the diuresis.

- It opens at **step 1 of 14** with a welcome screen, from a cold start and
  after abandoning mid-tour. (It used to open on step 3.)
- **Pass** = you chose the answer the reference panel chose, AND matched 3 of
  its 4 findings, AND did not use "Skip this step" on a graded step.
- Failing shows you what you wrote next to what the panel read, and offers
  "Take it again". Retries are unlimited.
- You never see a score. The headline is a verdict in words.

Try clicking "Skip this step" through the whole tour: it scores 4 of 4 and
still does not pass, because the placeholders are the answer key's own words.

## 4 · Admin approves them and assigns a tier

Sign in as `admin@localhost` / `dev-admin-password` → **Physicians** → the
verification queue → approve → **assign a tier**.

Tier is the whole access model:

- **Labeler** — can draw and label cases
- **Reviewer** — can additionally adjudicate a completed pair

No tier means no work. That refusal in step 2 was the guard, not a bug.

## 5 · Label a case

Sign in as a labeler → **Tasks** → draw. **12 nephrology cases are queued**,
10 multimodal and 2 text, all rated hard.

A real one: confusion with a serum sodium of 110 — full problem list,
medications, vitals, and a flagged BMP panel. Classify the hyponatremia and
correct it safely.

**The anti-peeking mechanism is the thing to look at.** The AI's candidate
answers are withheld. You write your own independent answer first, it commits
server-side, and only that commit reveals the AI answers. The order is enforced
by the API, not by the honor system — so at packaging time we can prove the
physician's answer was written before they saw the model's.

Then: grade the candidates, capture reasoning step by step, and cite evidence
for each claim.

Newest annotation UI: http://localhost:8000/asclepius/v5/annotate

## 6 · Complete a pair

Every case wants two independent labels. **Three cases are already sitting at
"awaiting second label" right now** — sign in as `labeler2@demo.local`, label
one of those same cases, and the pair completes.

## 7 · Adjudicate

Sign in as `reviewer@demo.local` / `Reviewer-2026` → the pair queue.

Reviewer stats read: `unreviewed 3 · awaiting_second 3 · review_ready 0`.
After step 6 one of those moves to review_ready and you can adjudicate it.

## 8 · Money

Real rates, live in the product:

| | |
|---|---|
| Task labeled | **$75** |
| Review session | **$100** |
| Referral bounty (referrer) | **$50** |
| Referral bonus (new joiner) | **$25** |

Earnings shows accrual the moment you submit, and separately what is still
pending review. A review session only qualifies for payout past a minimum
duration floor — I set it to **60 seconds** locally
(`ASCLEPIUS_TR_MIN_SECONDS=60`) so a demo pays out in one sitting. Production
is 20 minutes.

## 9 · Where cases come from

Admin → mint an upload link (purpose is required at mint time: task creation
vs brokering). Hospital-side portal: **http://localhost:8000/provider**

Uploads arrive with **no specialty**, because specialty is a property of the
data and hospital IT is never asked for it. The row shows "Specialty not set"
and **Promote stays disabled until you pick one.**

Say this part out loud when you demo it: ingest refusing to guess is the
product working. A wrong specialty routes the case to the wrong physician pool
and mislabels it in the export, invisibly, and neither is recoverable once the
bundle ships.

## 10 · Quality and export

Admin → Exports and Metrics: per-contributor and per-organization quality,
value-per-time, credential summaries, and export bundles.

**κ will read `null`.** The floor is 30 double-labeled observations; below that
the report prints "kappa is not reportable below 30" instead of a number.
Say it before a buyer discovers it: *we return nothing rather than a number
nobody should trust.* Do not lower the threshold to make it look better — a κ
computed on n=4 is exactly what the floor exists to prevent.

`mockadmin` submissions are hard-excluded from every export, so you can click
around the sandbox without polluting a shipped batch.

## 11 · Community

http://localhost:8000/community — contributor channels, digests, events, polls.

---

## What I changed to make this work

Created three accounts (`newdoc@`, `labeler2@`, `reviewer@demo.local`) via the
admin API, and appended two lines to `backend/.env` (original backed up
alongside as `.env.bak-*`):

    DEMO_MODE=1                  # seeds demo data
    ASCLEPIUS_TR_MIN_SECONDS=60  # payout floor, so earnings appear in one sitting

Local dev only. Delete the two lines to get the stock instance back.
