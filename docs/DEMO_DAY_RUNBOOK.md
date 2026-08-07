# Demo-day runbook

Everything a live walkthrough needs that is **not** a code fix. Three of these are
working exactly as designed and will still look wrong on stage; they are handled
by staging the demo instance, not by changing the product.

---

## 1 · Before you start: stage the instance

| Variable | Demo value | Why |
|---|---|---|
| `ASCLEPIUS_TR_MIN_SECONDS` | `60` | Production is `1200` (20 min). A paired-review session only qualifies for payout past that floor, so in a 10-minute demo the reviewer completes a full adjudication and earns **nothing visible** — which reads as "the payments system is broken". Alternative: pre-seed one qualified session so Earnings has a real `$100` row. |
| — serve over **HTTPS** | required | The provider-portal cookie is unconditionally `Secure` (`asclepius_provider.py`). On a plain-HTTP host the login returns 200, the browser silently drops the cookie, and the next call 401s. It reads as "sign-in fails". **Do not remove the flag** — demo over TLS. |
| `ASCLEPIUS_KAPPA_MIN_N` | leave at `30` | See §3. |

Set the specialty on any hospital upload you plan to promote (see §2) — or use
the in-product selector, which now exists.

---

## 2 · The four-workstream walkthrough, in the order a partner will ask for it

1. **Sign up** → the landing wizard. Verification email links now resolve
   (`/verify-email/:token` has a Vercel rewrite).
2. **The waiting screen.** A new physician is `pending` until an admin approves
   them, and the portal now says so on a real screen instead of a blank login
   form. Worth showing deliberately: *"we verify every clinician before they can
   touch a case"* is the pitch, and this is where it is visible.
3. **Approve in admin** → Physicians → verification queue. Approval assigns a
   **tier**; the tier is what grants the LABEL capability, so an unapproved
   account genuinely cannot draw work.
4. **Draw a task → submit.** Earnings immediately shows the accrued amount **and**
   counts the task as awaiting review.
5. **Second physician labels the same case** → the pair is complete.
6. **Reviewer draws the pair** → adjudicates.
7. **Earnings shows money.** With `ASCLEPIUS_TR_MIN_SECONDS=60` the review session
   qualifies inside the demo.
8. **Admin mints an upload link** → purpose is required at mint time
   (task creation vs brokering); the recipient cannot tell which was chosen.
9. **Upload** → the bundle ingests.
10. **Promote to a task.** If the bundle declared no specialty — which every
    hospital-portal upload does, because specialty is a property of the data and
    is not asked of hospital IT — the row shows **Specialty not set** with a
    picker, and **Promote is disabled until you choose one**. Choose it, promote.

### The one thing to say out loud at step 10

Ingest refuses to guess a specialty. A wrong one routes the case to the wrong
physician pool and mislabels it in the export, invisibly, and neither is
recoverable once the bundle ships. Refusing to promote is the product working.

---

## 3 · κ will read `null`, and that is the pitch

`ASCLEPIUS_KAPPA_MIN_N` is `30`. Below 30 double-labeled observations the quality
report prints *"kappa is not reportable below 30"* rather than a number.

**Say it before they read it.** "We return nothing rather than a number nobody
should trust" is a much stronger line delivered than discovered.

If you would rather show a real κ with its confidence interval, seed 30+
double-labeled observations. Do **not** lower the threshold — a κ computed on
n=4 is the exact thing the floor exists to prevent, and a partner who asks how
it was computed will find that out.

---

## 4 · Known-good demo accounts

- **Sandbox contributor** (`mockadmin`) — provisioned on every boot, tiered
  `labeler`, submissions hard-excluded from every export. Credentials are in the
  admin's Demo Credentials section.
- **Bootstrap admin** — `ASCLEPIUS_ADMIN_EMAIL` / `ASCLEPIUS_ADMIN_PASSWORD`.
  In production these must be set explicitly; no default credentials are seeded.

---

## 5 · If something looks wrong on stage

| Symptom | Almost certainly |
|---|---|
| Sign-in "silently fails" on the provider portal | Not HTTPS. §1. |
| Reviewer earns nothing after a full adjudication | `ASCLEPIUS_TR_MIN_SECONDS` still `1200`. §1. |
| Promote button is greyed out | Specialty not set on the upload. The reason and the picker are in the row. |
| A physician sees "This account cannot draw cases right now" | No tier assigned — approve them in the verification queue. |
| Quality report shows no κ | Fewer than 30 double-labeled observations. §3. |
