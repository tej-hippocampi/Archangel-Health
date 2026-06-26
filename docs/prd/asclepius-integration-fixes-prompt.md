# Cursor Handoff — Asclepius Integration Fixes

Apply the fixes below to integrate the standalone **Asclepius Expert Evaluation
Portal** cleanly. The standalone build (router at `/api/asclepius`, page at
`/asclepius`, `backend/asclepius/` package with `auth.py`/`validation.py`/
`pipeline.py`/`profiles.py`/etc., and `frontend/asclepius/`) is **canonical and
wins** every conflict. Work through the blockers first; they prevent the app from
booting or break trust claims.

---

## 🔴 BLOCKER 1 — Remove the older, conflicting admin-tab Asclepius implementation

An earlier, smaller Asclepius implementation was already committed to this branch
and **collides** with the standalone build. Remove/reconcile it so only the
standalone build remains:

1. **Delete these orphaned files** (superseded by the standalone build; one of
   them — `seed.py` — calls a `store.ingest_submission(...)` method that no longer
   exists and will raise if imported):
   - `backend/asclepius/seed.py`
   - `backend/asclepius/buyer_profiles.py`  ← the standalone build uses a
     `backend/asclepius/buyer_profiles/` **directory**, not a `.py` module. Make
     sure the directory (`default.json`, `TEMPLATE.json`) is what remains.

2. **Ensure the standalone versions overwrite the old ones** for any shared paths
   (`backend/asclepius/__init__.py`, `store.py`, `packaging.py`, `export.py`,
   `backend/routers/asclepius.py`). The old `routers/asclepius.py` used prefix
   `/admin/asclepius`; the canonical one uses **`/api/asclepius`**.

3. **`backend/main.py` — de-duplicate the wiring.** The branch already contains
   (from the old build):
   ```python
   from routers.asclepius import router as asclepius_router   # ~line 62
   app.include_router(asclepius_router)                        # ~line 5862
   ```
   Keep the import and `include_router` **exactly once**. Then make sure the
   standalone additions are present once each: the `/asclepius` HTML page route,
   and the boot-time Asclepius store-init + `seed_default_admin` block.

4. **`frontend/admin.html` — remove the old "Asclepius — Expert Eval Data" tab.**
   It calls `/admin/asclepius/*` endpoints that no longer exist (router moved to
   `/api/asclepius`), so it will 404. The standalone portal at `/asclepius` has
   its own admin UI, so this tab is redundant. Remove:
   - the sidebar nav entry `data-tab="asclepius"` (and its "Asclepius" nav-label),
   - the `<div class="tab-page" id="tab-asclepius">…</div>` block,
   - the lazy-load hook `if (btn.dataset.tab === 'asclepius') { loadAsclepius(); }`,
   - all `loadAsclepius` / `asc-*` JS helpers and event listeners added for it.
   (Leave all non-Asclepius admin functionality untouched.)

**Acceptance:** repo-wide search for `admin/asclepius` returns nothing; the only
Asclepius router prefix is `/api/asclepius`; `python3 -c "import main"` succeeds.

---

## 🔴 BLOCKER 2 — Do NOT wire in the `gold` router (its files don't exist here)

The shared wiring diff also contained "Gold Standard" lines. There is **no
`backend/routers/gold.py` and no `backend/gold/` package** in this repo, so these
lines would crash `main.py` on import and take down the whole backend. Ensure
`backend/main.py` does **not** contain either of these unless/until the Gold
feature actually lands:
```python
from routers.gold import router as gold_router   # REMOVE
app.include_router(gold_router)                   # REMOVE
```
Only `asclepius` wiring should be added from that diff. (The Gold-related
`.env.example` block is harmless documentation and can stay or go.)

**Acceptance:** `grep -rn "routers.gold\|gold_router" backend/main.py` returns
nothing; the app imports cleanly.

---

## 🔴 BLOCKER 3 — Make the PHI scan real (it is currently a silent no-op)

`backend/asclepius/validation.py` does:
```python
from gold.deid import residual_identifiers
```
with an `except` fallback returning `[]`. Because the `gold` package is absent,
**the PHI scan does nothing** — yet every record is stamped `contains_phi: false`
and the datasheet says "residual-identifier scanned." That's a false trust claim
on the exact dimension we sell.

Fix: implement a self-contained baseline scanner **inside
`backend/asclepius/validation.py`** (no `gold` dependency). Replace the
`gold.deid` import/fallback with a local `residual_identifiers(text)` that
regex-scans for at least: email addresses, US phone numbers, SSNs, MRN-like
tokens, and explicit dates (MM/DD/YYYY etc.), returning a list of matched
identifier **kinds** (not the values). Keep the existing call sites and the
`phi:<kinds>` issue format. If the `gold` package is later added, prefer it when
importable, but never fall back to a no-op silently — if no scanner is available,
treat that as a validation failure, not a pass.

**Acceptance:** a submission whose prompt contains `test@example.com` or
`123-45-6789` is flagged (`phi:` issue) and routed to QA; `contains_phi: false`
is only stamped after a scanner actually ran.

---

## 🟠 FIX 4 — Don't seed a default admin password in production

`backend/asclepius/auth.py:seed_default_admin()` creates
`admin@asclepius.local / asclepius-admin-2026` (and a demo evaluator) whenever the
user table is empty — with **no production guard**. A fresh prod boot would have
known default credentials.

Fix: when `ENV == "production"`, refuse to seed the bootstrap admin unless
`ASCLEPIUS_ADMIN_PASSWORD` (and `ASCLEPIUS_ADMIN_EMAIL`) are explicitly set; never
seed the demo evaluator in production (ignore `ASCLEPIUS_SEED_DEMO_EVALUATOR` in
prod). Log a clear warning if seeding is skipped because creds are missing.

---

## 🟡 FIX 5 — Small correctness / polish items

- **`/asclepius` page auth:** confirm it's intentional that the HTML shell is
  served unauthenticated (the JS gates on a token, like `/doctor`). Acceptable —
  just confirm. Remove the unused `request: Request` param on the route handler.
- **Google Fonts / CSP:** `frontend/asclepius/index.html` loads fonts from
  `fonts.googleapis.com`. If the app CSP is strict this is blocked — either add
  the domain to CSP or self-host/drop the webfont (cosmetic).
- **`grounded` on preference records** (`packaging.py`) only reflects the
  rationale anchor; consider also counting `error_tag_anchors` so the premium-tier
  count isn't undercounted.
- **Doctor portal tab placement:** ensure the new top-level "Expert Evaluation"
  tab sits **directly after "Population Analytics"** in the `doctor.html` nav
  order (it should be its own tab, not nested under Population Analytics).

---

## 🟢 Scale notes (no change required for MVP — leave a TODO comment)

- Dedupe check in `pipeline.process_submission` scans all submissions per submit
  (`list_submissions(limit=100000)`); switch to an indexed `dedupe_hash` lookup
  when volume grows (the `idx_sub_dedupe` index already exists).
- `export._flag_counts` and `store.next_task_for_evaluator` similarly do
  full-table scans; fine at pod scale.

---

## Final verification (run after all edits)
```bash
cd backend
python3 -c "import main"                       # clean import, no gold/asclepius errors
python3 -m pytest tests/test_asclepius_*.py -q # asclepius suite passes
grep -rn "admin/asclepius\|routers.gold" backend frontend  # expect: no matches
```
The portal should be reachable at `/asclepius` (own login) and as the "Expert
Evaluation" tab in the doctor portal. No record should reach `export_ready`
without passing auto-validation + the QA gate, and the PHI scan must actually run.
