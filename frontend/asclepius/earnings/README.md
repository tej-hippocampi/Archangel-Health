# Asclepius — Earnings (payout tracking) surface

A physician-facing **Earnings** page for the Asclepius Expert Evaluation Portal: what a clinician has earned, what's pending, when it pays out, and a per-case history. Built on the existing "console" design system so it drops into the product.

> **Status: preview with demo data.** Dr. Maya Rao, the dollar figures, and the case rows are placeholders. No live data, no backend calls.

## Look at it (no setup)

Open **`earnings-preview.html`** in any browser (double-click it). It's fully self-contained — the design system and fonts are inlined, so it works offline and needs no build step.

## What's on the page

- **Next payout** — the one number that matters, big and up front: amount, date, where it's going, and the clinician's approval rate.
- **Recent work** — a plain list of recent cases with the pay and status of each (Awaiting review / Approved / Paid).
- **Payment method & tax** — tucked behind one click: Stripe method, W-9 status, year-to-date total, and the $2,000 1099-NEC note.

Design intent: *one clear idea per screen, detail hidden until asked for.* This deliberately shows less than a full dashboard — the payout pipeline, quality metrics, filters, etc. can be layered back in behind disclosures once the basics are validated.

## Files

| File | What it is |
|---|---|
| `earnings-preview.html` | Self-contained preview. Open this to see it. |
| `src/earnings.view.html` | The markup fragment — render inside the product's `<main id="ascRoot">`. |
| `src/earnings.css` | The new styles only (prefix `.px-`). Every value is a `_tokens.css` variable. |
| `src/earnings.js` | View behavior (`pxEarnings.init(root)`); self-inits if the markup is already present. |

## Integrating into the product (`frontend/asclepius/`)

The new files depend on the console design system that already ships with the product — `clinical-fonts.css`, `_tokens.css`, `_base.css` — so integration is additive:

1. Copy `src/earnings.css` and `src/earnings.js` into `frontend/asclepius/`.
2. Load them after the existing stylesheet / script (order: `clinical-fonts.css` → `_tokens.css` → `_base.css` → `asclepius.css` → `earnings.css`).
3. Render the `earnings.view.html` markup into `#ascRoot` (e.g. add an "Earnings" entry to `renderHeader()`'s nav and a `renderEarnings()` branch, following the existing `setRoot()` pattern), then call `pxEarnings.init(ascRoot)`.
4. Replace the demo data: the hero amount/date, the recent-work rows, and the tax block are the wiring points for real payout data (Stripe balance/transfers + the submission ledger).

No existing files need to be edited to preview or evaluate this; wiring it into the live nav is the only product change.

## Notes for review

- **Single-theme (light)** on purpose — matches the product's committed console aesthetic.
- **`reward` is intentionally avoided for money** — in `asclepius.js` `step_reward` already means the RLHF step label. Money here is *pay / earned / payout*.
- Semantic dots follow the product's meanings: green = earned/approved, orange = pending/awaiting, pink = attention.
