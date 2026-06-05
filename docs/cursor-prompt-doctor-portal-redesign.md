# Cursor Prompt — Doctor Portal UX + AI Security & Compliance Redesign

> Paste everything below the line into Cursor. It is written against the current
> codebase (FastAPI backend + static `frontend/*.html/js`, no build step) and
> references exact files, functions, element IDs, and line anchors so the change
> can be made in one pass.

---

You are working in the Archangel/CareGuide repo: a Python FastAPI backend
(`backend/`) serving static HTML/JS from `frontend/`. **No build step, no
database** — data lives in memory and in `team_store`. The doctor portal is the
single large file `frontend/doctor.html` (HTML + CSS + inline JS). The admin
console is `frontend/admin.html`. Make minimal, surgical edits that match the
existing code style (vanilla JS, `byId()` helpers, template-string rendering,
inline `<style>` blocks). Do **not** introduce frameworks or a build step.

Implement all of the following. Each item lists the exact current location and
the desired end state.

## Auth context you must respect
- The **doctor portal** authenticates with a **tenant-staff JWT** (see
  `backend/routers/tenant_portal.py` → `decode_tenant_staff_token`). Roles are
  `surgeon` (TEAM surgeon), `rn_coordinator` (RN care coordinator), and `np_pa`.
  The token carries `itd` (is_team_director) and `slug` (tenant slug).
- The **admin console** (`admin.html`) authenticates with an **admin token**
  (`_verify_token` in `backend/routers/admin.py`). Doctor-portal pages **cannot**
  call `/admin/*` endpoints — those require the admin token. Any admin data you
  surface in the doctor portal must go through **new tenant-scoped endpoints**
  (see the Backend section).

---

## 1. Patient roster — "Edit" button

File: `frontend/doctor.html`, `renderRoster()` (~line 3011), row template ~line 3031.

- Change the roster row button label from `Edit` to **`Edit patient details`**
  (the `<button class="btn-intraop edit-patient" …>Edit</button>`).
- Keep its click handler (`openEditPatientModal`) unchanged.

## 2. Remove "Switch to post-op" from the roster rows

File: `frontend/doctor.html`, `intraopCtaHtml(p)` (lines ~3055–3069).

- For **pre-op** rows this function currently returns
  `Switch to post-op` (line ~3068). Remove that button from the roster entirely —
  pre-op rows should render **no** post-op CTA in the roster.
- The "Switch to Post-Op" action must remain available **only** from the patient
  detail panel, where it already exists as `#switchToPostOpBtn` (line ~1675) and
  `#switchToPostOpBtn2` (line ~1701), beside "Revise Prep Notes" and "View Intake
  Form". Do not move or duplicate those — just confirm they stay wired to the
  existing `/api/episodes/{id}/switch-to-postop` flow.
- Leave the `intra_op` "Open intra-op form" branch behavior as-is unless it
  depends on the removed pre-op button (it does not).

## 3. Post-op roster rows — remove "Recompute now" and the TIER_3 marker

File: `frontend/doctor.html`, `intraopCtaHtml(p)` post-op branch (lines ~3057–3063).

- For **post-op** rows, remove BOTH:
  - the `Recompute now` button (`<button … data-action="postop-retier">`), and
  - the `postop-tier-badge` chip that renders the raw tier string (e.g.
    `TIER_3`) — this is the "weird TIER_3 marker" in the screenshot (line ~3061).
- Post-op rows should show the tier through the **titled roster column** added in
  item 4 (a clean labeled chip), not a floating raw `TIER_3` badge stacked over a
  button. The post-op CTA cell should be empty (or show nothing) in the roster.
- You may leave `handleIntraopCtaClick`'s `postop-retier` case in place (dead
  code is fine) or remove it; do not break the `switch`/`open` cases.

## 4. Roster column titles + redesign

File: `frontend/doctor.html`. The roster is a CSS grid: `.roster-list` →
`.roster-row` with `grid-template-columns: 1.1fr 1fr 1.2fr .7fr .9fr 1.1fr`
(CSS ~line 214). Rows are built in `renderRoster()` (~line 3013). **There is
currently no header row.** Today each row packs several unlabeled markers into
the name cell via `metaHtml` (episode pill = pre/post-op, tier chip, TEAM badge).

Redesign so every column and every marker has a visible **title/header**:

- Add a sticky header row at the top of `.roster-list` (or just above it) that
  aligns to the same grid columns, with these titles:
  1. **Name**
  2. **Surgical Stage** (Pre-Op / Post-Op)
  3. **Risk Tier** (Tier 1 / Tier 2 / Tier 3 — replaces the floating badge)
  4. **Phone**
  5. **Email**
  6. **Episode Day**
  7. **Intake Status**
  8. **Actions**
- Pull the **Surgical Stage** pill and the **Risk Tier** chip out of the name
  cell into their own titled columns. Keep the TEAM badge near the name or give
  it its own subtle column — your call, but it must read cleanly.
- Each marker should read as **Title (label above) + value below**, e.g.:
  - `Name`: Linda Whitefield
  - `Surgical Stage`: Pre-Op or Post-Op
  - `Risk Tier`: Tier 1 / Tier 2 / Tier 3
  - `Intake Status`: the existing intake-status pill
- Update `grid-template-columns` and the responsive `@media` rule (~line 1299)
  so the new columns fit without overflow. Keep it scannable for 50+ rows.
- Preserve existing helpers (`episodePillHtml`, `tierChipClass`, `tierChipLabel`,
  `renderTeamBadge`, `intakeStatusPillHtml`). The tier chip should reuse the
  `roster-tier-chip` styles (CSS ~line 1259) and the `tier3` red left-border
  emphasis may stay, but there must be **no raw `TIER_3` text badge**.

## 5. Move the self-flag alert out of the top banner into Notifications

Files: `frontend/doctor.html` — banner element `#triageAlertBanner` (line ~1364),
banner logic in `loadPatients()` (lines ~2875–2895), notification bell
`#doctorNotifBell` (line ~1359) + `setupNotificationBell()` (line ~5372) +
`loadIntakeNotifications()` (line ~2977).

- **Remove the top banner entirely.** Delete the `#triageAlertBanner` element and
  all code that sets `.style.display`/`.innerHTML` on it (both the
  `rn_coordinator` and `surgeon` branches, and the non-triage reset branch). No
  full-width banner should ever appear at the top of the roster.
- Instead, surface the `hasActiveSelfFlag` count **inside the Notifications
  bell** that sits to the **left of "+ Add Patient"**:
  - Fold the self-flag patients into the notifications shown when the bell is
    clicked (the `setupNotificationBell()` panel). Render an entry like
    `N patient(s) have an active "I need help" self-flag — review the roster.`
    with the affected patient names, ideally linking/opening their rows.
  - Include self-flag count in the bell's badge count
    (`Notifications: ${count}`) alongside `intakeNotifications.length`.
  - Keep the surgeon-vs-RN nuance if you like (RN owns resolution), but it must
    live in the notifications panel, never as a banner.

## 6. Compliance tab → "AI Security and Compliance" (the big one)

This unifies the admin AI-compliance views into the doctor portal for **every
provider** (surgeon AND rn_coordinator), and merges the surgeon-only Audit Log
into it.

### 6a. Rename + restructure the tab
File: `frontend/doctor.html`.
- Sidebar tab button (line ~1340): change label `Compliance` →
  **`AI Security and Compliance`** (keep `data-tab="compliance"` or rename
  consistently across the button + `#tab-compliance` section + `setupTabs`).
- Page title (line ~1399): `Compliance` → **`AI Security and Compliance`**.
- **Delete the separate `Audit Log` sidebar tab** (`#tab-btn-audit`, line ~1342)
  and its standalone `#tab-audit` section (lines ~1492–1515). The audit log moves
  *into* this tab as a sub-section (6c).

### 6b. Add Grounding Checker + AI Call Log sub-sections
Port the two admin views from `frontend/admin.html` into the
`AI Security and Compliance` tab, mirroring the admin layout (admin divides them
into sections under an "AI Security & Compliance" nav group, line ~455):

- **Grounding Checker** — port the markup from admin `#tab-grounding`
  (lines ~853–885): KPI grid (`gk-total/pass/review/block/...`), filters
  (verdict/track/search), the reports table, and the inspector-recall banner.
  Port the JS that loads it (admin.html `loadGrounding*` functions that call
  `/admin/grounding/stats`, `/admin/grounding/inspector-recall`,
  `/admin/grounding/reports`, `/admin/grounding/reports/{id}` — found via
  `fetch('/admin/grounding/...')` around admin.html lines ~2156–2240). Also port
  the grounding drawer overlay (`#grounding-drawer-overlay`, line ~922) and the
  associated CSS (admin.html lines ~343–377).
- **AI Call Log** — port admin `#tab-ai-call-log` (lines ~887–920): KPI grid
  (`ack-total/input/output/models`), per-role table, filters, calls table.
  Port the JS calling `/admin/ai-calls/stats`, `/admin/ai-calls`,
  `/admin/ai-calls/prompts` (admin.html lines ~2289–2327).
- **Re-point all these `fetch('/admin/...')` calls to the new tenant-scoped
  endpoints** (see Backend), using the doctor portal's existing authed fetch
  helper `apiJson(...)` (which already attaches the tenant-staff Bearer token),
  **not** `authHeaders()`/`adminToken` (that's admin-only and won't exist here).
- Use the doctor portal's tenant slug (from the stored profile,
  `PROFILE_KEY` → `tenantSlug`, see `loadAuditLog()` line ~2737 for the pattern).
- Lazy-load each sub-section's data when the tab is first opened (extend
  `setupTabs()` ~line 2722, which already special-cases the `audit` tab).
- **Decision to confirm with me:** the existing PCP-referral / beneficiary
  table currently in the Compliance tab (`#complianceBody`, `renderCompliance()`
  ~line 3953). Keep it as a fourth labeled sub-section ("PCP Referral
  Compliance") within this tab rather than deleting it, unless I say otherwise.

### 6c. Audit Log — make it a sub-section AND available to the RN
- Move the audit-log table (currently `#tab-audit` / `#auditLogBody`, lines
  ~1492–1515, loaded by `loadAuditLog()` ~line 2736) into the
  `AI Security and Compliance` tab as a third sub-section titled **"Audit Log"**,
  designed to match the Grounding/AI-Call sections (same card/`data-section`
  styling).
- It must render for **both** `surgeon` and `rn_coordinator`. Remove the
  surgeon/`is_team_director`-only gating in the UI (lines ~4081–4085 set
  `#tab-btn-audit` visibility from `profile.isTeamDirector`). Since the tab is
  now always present for providers, just load the audit data whenever the tab
  opens.
- Backend gate must change too (see Backend 7c) — today
  `/api/tenant/{slug}/audit-log` returns 403 unless `is_team_director`.

---

## 7. Backend changes

File: `backend/routers/tenant_portal.py` (tenant-staff-authed, `/api/tenant`
prefix). Reuse `team_store` methods already used by the admin endpoints in
`backend/routers/admin.py`.

### 7a. Tenant-scoped Grounding endpoints
Add, mirroring `admin.py` `/grounding/*` (lines ~415–486) but authed with
`decode_tenant_staff_token` (any provider role for this tenant, like the audit
endpoint pattern at `tenant_portal.py:76`):
- `GET /api/tenant/{slug}/grounding/stats?window_days=30` →
  `team_store.grounding_summary_stats(...)`
- `GET /api/tenant/{slug}/grounding/inspector-recall` →
  `team_store.get_latest_inspector_recall()`
- `GET /api/tenant/{slug}/grounding/reports` (verdict/track/prompt_version/since/
  limit filters) → `team_store.list_grounding_reports(...)` (enrich with
  `patient_name` like admin does)
- `GET /api/tenant/{slug}/grounding/reports/{report_id}` →
  `team_store.get_grounding_report(report_id)`

### 7b. Tenant-scoped AI Call Log endpoints
Mirror `admin.py` `/ai-calls/*` (lines ~489–542):
- `GET /api/tenant/{slug}/ai-calls/stats?window_days=30` →
  `team_store.llm_call_stats(...)`
- `GET /api/tenant/{slug}/ai-calls` (role/prompt_id/prompt_version/since/limit) →
  `team_store.list_llm_calls(...)`
- `GET /api/tenant/{slug}/ai-calls/prompts` → same prompt registry payload as
  admin (`prompts.registry`).

> Scoping note: confirm whether grounding/ai-call data should be filtered to the
> tenant/health-system, or is global demo data. The admin store methods appear
> global; for the demo, returning the same data is acceptable, but add a `# TODO`
> if per-tenant scoping is later required.

### 7c. Open the Audit Log to the RN
`tenant_portal.py` `tenant_audit_log` (line ~76): remove the
`if not bool(td.get("itd")): raise 403` check (lines ~84–85) so any authenticated
provider for the tenant (surgeon, rn_coordinator) can read the sign-in audit log.
Keep the slug/token validation.

---

## 8. Validation
- `cd backend && python3 -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload`
- Sign in to the doctor portal as both a **surgeon** and an **RN coordinator**;
  verify: roster button reads "Edit patient details", no roster "Switch to
  post-op", no post-op "Recompute now"/"TIER_3" badge, roster has titled
  columns, no top banner, self-flag count appears in the Notifications bell, and
  the **AI Security and Compliance** tab shows Grounding Checker + AI Call Log +
  Audit Log for **both** roles.
- Run `cd backend && python3 -m pytest tests/ -q` and keep
  `tests/test_admin_ai_compliance.py` green; add a small test that the new
  tenant-scoped grounding/ai-calls/audit endpoints return 200 for an authed RN.
- Keep all edits in `frontend/doctor.html`, `frontend/admin.html` (only if you
  factor shared bits — otherwise leave admin untouched), and
  `backend/routers/tenant_portal.py`. Do not alter the admin auth model.
