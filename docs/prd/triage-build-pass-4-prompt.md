# Cursor Fix Prompt — Triage Build Pass 4 (Role Model + Permissions)

Paste the section below into Cursor verbatim. It is self-contained.

---

## Prompt to Cursor

This pass consolidates the role model, enforces permissions across the four triage routers, caps team size at the surgical pod level, and changes the intra-op workflow from "surgeon-only filler" to "RN drafts → surgeon reviews and locks." Source PRDs remain `docs/prd/initial-triage-v1.md`, `preop-retier-v1.md`, `intraop-reassessment-v1.md`, `postop-scoring-v1.md`, with `docs/prd/README.md` as the integration map. The intra-op workflow change supersedes Intra-Op Reassessment v1.0 §3.2–§3.3 and §4.3 (single-surgeon model). Update the PRD's "filler" language to match the new workflow as part of this pass.

After each section, run `cd backend && python3 -m pytest tests/ -q` and ensure the pass count is at or above the post-pass-3 baseline.

### 1. Role consolidation — five roles total, no others

The system has exactly **five** roles. Remove any references in code, comments, tests, or PRDs to roles outside this set. Specifically: delete `anesthesia provider` and `clinical operations lead` from any role check, label map, or PRD prose.

| Role | Token (DB-stored) | Where they sign in |
|---|---|---|
| System admin | `system_admin` | `routers/admin.py` admin token |
| Surgeon | `surgeon` | tenant JWT |
| RN care coordinator | `rn_coordinator` | tenant JWT |
| NP / PA | `np_pa` | tenant JWT |
| Patient | `patient` (implicit) | patient session in `_patient_store` |

#### 1.1 Migrate existing role tokens

Existing rows in `team_members` use `doctor`, `nurse`, and `director`. Migrate as follows:

- `role='doctor'` → `role='surgeon'`.
- `role='nurse'` → `role='rn_coordinator'`.
- `role='director'` → `role='surgeon'` AND set a new `is_team_director=1` flag.

Add `is_team_director INTEGER NOT NULL DEFAULT 0` to `team_members`. The director-creates-the-team flow stays — the *role* becomes `surgeon`, the *director-ness* becomes a separate boolean. Run the migration as a one-shot in `team_store.py::_init_schema` guarded by a `try/except` that detects whether the column already exists.

#### 1.2 Update every role-check site

Update every site that compares `role == "doctor"` or similar. The audit list — confirm and fix each:

- `backend/staff_context.py:35` — `role=str(td.get("role") or "doctor")` → default to `"surgeon"`.
- `backend/staff_context.py:51` — same default change.
- `backend/auth.py:107` — `u.setdefault("role", "doctor")` → `"surgeon"`.
- `backend/auth.py:137` — `register_user(..., role: str = "doctor")` → `"surgeon"`.
- `backend/auth.py:207, 236` — `users[key].get("role") != "doctor"` → `!= "surgeon"`.
- `backend/routers/onboarding.py:22-23` — `_ROLE_LABELS` map: replace with `{"surgeon": "Surgeon", "rn_coordinator": "RN Care Coordinator", "np_pa": "NP / PA"}`.
- `backend/routers/onboarding.py:250` — accept the three new tokens, reject everything else.
- `backend/routers/tenant_portal.py:33, 38, 81` — `role default "doctor"` → `"surgeon"`; `role.lower() != "director"` → `is_team_director != 1`.
- `backend/main.py:157` — `role in {"doctor", "director", "nurse"}` → `role in {"surgeon", "rn_coordinator", "np_pa"}`.
- `backend/main.py:478` — hard-coded `"role": "doctor"` → `"surgeon"`.
- `backend/main.py:1482-1483` — `role == "director"` → use the new `is_team_director` flag.
- `backend/main.py:2463, 2610` — `user.role == "doctor"` → `user.role == "surgeon"`.
- `backend/main.py:3650` — `payload["source"] = "doctor"` (intake source label, not auth) → leave as `"doctor"` only if it's a clinical-source label, otherwise migrate to `"surgeon"`. Verify in context before changing.
- `frontend/doctor.html:2159` — `(profile.typeOfDoctor || "").toLowerCase().includes("director")` → check new `profile.isTeamDirector` flag instead.

The intent: after this section, no `"doctor"`, `"nurse"`, or `"director"` token survives as a *role* in the codebase. They may still appear as labels for clinical-source provenance (e.g., `payload["source"] = "doctor"` in intake form parser, which means "this field came from a doctor's note") — those are fine to keep but add a comment clarifying it is a source label, not a role token.

### 2. Surgical pod cap — exactly four people per team

The director creates the team during onboarding and must end up with a pod of exactly four members: themselves (the surgeon) + 1 RN coordinator + 2 NP/PA. Enforce both at the API and UI levels.

#### 2.1 API enforcement in `routers/onboarding.py::add_team_member`

Before inserting a new `team_members` row, count existing non-director members by role and reject if any cap would be exceeded:

```python
existing = ts.list_team_members(row["id"])
non_director = [m for m in existing if not m.get("is_team_director")]
if role == "rn_coordinator" and any(m.get("role") == "rn_coordinator" for m in non_director):
    raise HTTPException(status_code=409, detail="Team already has an RN care coordinator (cap: 1).")
if role == "np_pa" and sum(1 for m in non_director if m.get("role") == "np_pa") >= 2:
    raise HTTPException(status_code=409, detail="Team already has 2 NP/PAs (cap: 2).")
if role == "surgeon":
    raise HTTPException(status_code=409, detail="The team director is the only surgeon on the pod.")
if len(non_director) >= 3:
    raise HTTPException(status_code=409, detail="Team is full (cap: 4 including director).")
```

A non-director surgeon is rejected entirely — the director slot is the surgeon slot. If product later wants multi-surgeon pods, that's a separate change.

#### 2.2 Director auto-becomes surgeon at `/finish`

In `routers/onboarding.py::finish_onboarding`, when the director's `team_members` row is finalized, set `role='surgeon'` and `is_team_director=1`. Verify the helper `complete_onboarding_finalize` in `team_store.py` also writes both fields.

#### 2.3 Wizard UI updates

In whichever frontend page hosts the onboarding wizard (it consumes `_ROLE_LABELS` and posts to `/api/onboarding/add-team-member`):

- Replace the role dropdown options with: `RN Care Coordinator`, `NP / PA`. Surgeon is not selectable (already the director).
- Show a live counter: "Team: 1 / 4 — director (surgeon)" → "Team: 2 / 4" as members are added.
- After 4 total (1 director + 1 RN + 2 NP/PA), disable the "Add member" button and surface a friendly "Team is complete" state.
- Update the completion email count language accordingly (`onboarding_emails.build_complete_email`).

#### 2.4 Tests

Add `tests/test_onboarding_team_caps.py`:

1. Director creates team; adds 1 `rn_coordinator` — succeeds.
2. Adds a second `rn_coordinator` — 409.
3. Adds 2 `np_pa` — succeeds.
4. Adds a third `np_pa` — 409.
5. Adds a `surgeon` (non-director) — 409.
6. Director's own row in `team_members` has `role='surgeon'` and `is_team_director=1`.

### 3. Route-level permission enforcement

Pass 3 noted that triage endpoints accept any authenticated staff. Tighten this. Add a small helper module `backend/auth_roles.py`:

```python
def require_roles(staff: StaffContext, allowed: set[str]) -> None:
    if staff is None or staff.role not in allowed:
        raise HTTPException(status_code=403, detail="Insufficient role.")
```

Apply across the four triage routers using these allow-lists. Read endpoints accept the full clinical set (RN + Surgeon + NP/PA) so NP/PA can read everything; write endpoints exclude NP/PA.

#### 3.1 `routers/initial_tier.py`

| Endpoint | Allowed roles |
|---|---|
| `POST /api/triage/initial-tier/compute` | `{rn_coordinator, surgeon, np_pa}` (preview is harmless) |
| `POST /api/episodes/{id}/initial-tier` | `{rn_coordinator, surgeon}` |
| `POST /api/episodes/{id}/initial-tier/override` | `{rn_coordinator, surgeon}` |
| `GET  /api/triage/tuning/initial-tier/current` | `{system_admin, surgeon, rn_coordinator, np_pa}` |
| `POST /api/triage/tuning/initial-tier` | `{system_admin}` |

#### 3.2 `routers/preop_retier.py`

| Endpoint | Allowed roles |
|---|---|
| `POST /api/triage/preop-retier/compute` | `{rn_coordinator, surgeon, np_pa}` |
| `POST /api/episodes/{id}/preop-retier/run` | `{rn_coordinator, surgeon}` |
| `POST /api/episodes/{id}/pam` | patient-session only (no staff token; existing intake submission auth) |
| `POST /api/events/preop-video` | patient-session only |
| `POST /api/events/battlecard` | patient-session only |
| `GET  /api/triage/tuning/preop-retier/current` | `{system_admin, surgeon, rn_coordinator, np_pa}` |
| `POST /api/triage/tuning/preop-retier` | `{system_admin}` |

#### 3.3 `routers/intraop.py`

The workflow change in §4 below changes which role can do what. After §4 is applied:

| Endpoint | Allowed roles |
|---|---|
| `POST /api/episodes/{id}/intraop-form` (create) | `{rn_coordinator}` |
| `GET  /api/episodes/{id}/intraop-form` | `{rn_coordinator, surgeon, np_pa}` |
| `PATCH /api/episodes/{id}/intraop-form` (autosave during draft) | `{rn_coordinator}` while status is `IN_PROGRESS` or `DRAFT_PENDING_REVIEW`; `{surgeon}` while status is `READY_FOR_SURGEON_REVIEW` |
| `POST /api/episodes/{id}/intraop-form/mark-ready-for-review` (NEW; see §4) | `{rn_coordinator}` |
| `POST /api/episodes/{id}/intraop-form/lock` | `{surgeon}` |
| `POST /api/episodes/{id}/intraop-form/pdf` (upload + extraction) | `{rn_coordinator}` |
| `GET  /api/intraop-extractions/{id}` | `{rn_coordinator, surgeon, np_pa}` |
| `POST /api/episodes/{id}/intraop-form/reopen` | `{system_admin, surgeon}` (admin or the locking surgeon) |

#### 3.4 `routers/postop.py`

| Endpoint | Allowed roles |
|---|---|
| Patient-submitted: daily-checkin, surveys, med adherence, video event, self-flag, wound photo (when restored) | patient-session only |
| `POST /api/episodes/{id}/postop-retier/run` | `{rn_coordinator, surgeon}` |
| Alert review/resolve/defer (existing per Triage Tracking §10) | `{rn_coordinator}` |
| Wound-photo nurse review (deferred to a later pass) | `{rn_coordinator}` when restored |
| `GET  /api/triage/tuning/postop/current` | `{system_admin, surgeon, rn_coordinator, np_pa}` |
| `POST /api/triage/tuning/postop` | `{system_admin}` |

#### 3.5 Tests

Add `tests/test_role_authorization.py`:

1. NP/PA token returns 403 on every write endpoint above.
2. NP/PA token returns 200 on every read endpoint above.
3. Surgeon token returns 200 on `lock` and 403 on `mark-ready-for-review`.
4. RN token returns 200 on `mark-ready-for-review` and 403 on `lock`.
5. System admin token returns 200 on tuning POST; everyone else 403.
6. Anonymous (no token) returns 401 across the board.

### 4. Intra-op workflow change — RN drafts, surgeon reviews and locks

This supersedes Intra-Op Reassessment PRD §3.2–§3.3 and §4.3. Update the PRD prose alongside the code.

#### 4.1 New form lifecycle

```
[NEW]                          ← created when episode reaches OR_ENDED
   │ RN clicks "Draft intra-op form"
   ▼
[IN_PROGRESS]                  ← RN is actively filling; autosave on
   │ all required fields filled (PDF extraction OK, manual OK, mixed OK)
   │ RN clicks "Send to surgeon for review"
   ▼
[READY_FOR_SURGEON_REVIEW]     ← out of RN hands; surgeon notification sent
   │ surgeon opens the form
   │ surgeon may edit any field; surgeon clicks "Lock & switch to post-op"
   ▼
[LOCKED]                       ← reassessment fires; episode → POST_OP
   │ (rare) admin or locking surgeon reopens with reason
   ▼
[REOPENED]                     ← back to READY_FOR_SURGEON_REVIEW (not IN_PROGRESS)
```

The state machine has one new state: `READY_FOR_SURGEON_REVIEW`. RN cannot edit while in that state; surgeon can.

#### 4.2 `mark-ready-for-review` endpoint

Add `POST /api/episodes/{episode_id}/intraop-form/mark-ready-for-review` to `routers/intraop.py`. Behaviour:

1. Auth: requires `rn_coordinator`.
2. Validates that all 11 required universal fields (`docs/prd/intraop-reassessment-v1.md` §4.1) are present and confirmed (including LOW-confidence PDF extractions).
3. Sets `intraop_form.status = 'READY_FOR_SURGEON_REVIEW'` and `intraop_form.draft_completed_by = rn_user_id`, `draft_completed_at = now`.
4. Sends the surgeon a notification — see §4.4.
5. Writes a `TriageEvent` of type `INTRAOP_FORM_READY_FOR_REVIEW`.
6. Returns the form snapshot.

#### 4.3 Lock endpoint changes

Update `POST /api/episodes/{id}/intraop-form/lock`:

1. Auth: requires `surgeon`.
2. Validates `status == 'READY_FOR_SURGEON_REVIEW'`. Reject with 409 if status is `IN_PROGRESS` (surgeon must wait for RN to finish drafting).
3. Records `surgeon_locked_by`, `surgeon_locked_at`, fires the existing reassessment, transitions episode to `POST_OP`. Conservative-default cron behaviour from the original PRD §7.4 stays.

If a draft sits in `READY_FOR_SURGEON_REVIEW` for 4 hours without surgeon action, the existing overdue cron escalates it (page on-call surgeon AND surgeon supervisor email) — leave that wiring in place.

#### 4.4 Surgeon notification

Reuse existing notification infrastructure (the same channels that page the surgeon for `ESCALATED_TO_SURGEON` resolutions). Payload:

```
"{Patient name}, {Procedure family}, Day 0 post-op intra-op form is ready for your review and lock.
Drafted by RN {RN name}. Open in dashboard."
```

Send via the existing email + SMS + in-app channels. Add a new `notification_kind: 'INTRAOP_FORM_READY_FOR_REVIEW'` to whatever notification taxonomy exists.

#### 4.5 UI changes

`frontend/intraop-form.html` (or wherever the form lives):

- **RN view (status `IN_PROGRESS`):** all fields editable; PDF upload visible; "Save draft" autosave; bottom CTA reads "Send to surgeon for review" (disabled until all 11 required fields filled).
- **RN view (status `READY_FOR_SURGEON_REVIEW`):** read-only; banner says "Awaiting surgeon review. Sent {time} by you." with a link to "Recall draft" that flips back to `IN_PROGRESS` and notifies the surgeon the recall happened.
- **Surgeon view (status `READY_FOR_SURGEON_REVIEW`):** all fields editable; banner says "Drafted by RN {name} on {time}. Review, edit if needed, then lock."; bottom CTA reads "Lock & switch to post-op".
- **Surgeon view (status `LOCKED`):** read-only; banner shows lock metadata.
- **NP / PA view (any status):** read-only at every state; no CTAs visible.

#### 4.6 Doctor dashboard surgeon roster

In `frontend/doctor.html`:

- Surgeon login sees the full patient roster scoped to their pod (existing tenant filter).
- Patients with `current_tier == 'TIER_3'` get a clear visual marker on the row (red left-border + small "Tier 3" chip). Same convention as the queue tier card. Existing color tokens already exist for this.
- A new "Forms awaiting your review" section at the top of the surgeon's home shows any `intraop_forms` rows with `status='READY_FOR_SURGEON_REVIEW'` for patients in their pod. Click → opens the intra-op form in surgeon-review mode.

#### 4.7 NP / PA read-only dashboard

NP/PA login lands on the same dashboard URL as the RN coordinator. Implementation:

- All write endpoints already 403 per §3 — that's the auth backstop.
- All buttons / forms / modals on every page must check `currentStaffRole` (already exposed on the dashboard via the staff JWT) and either hide the action affordance or render it disabled with a tooltip "Read-only access".
- This applies to: alert claim/resolve/defer; tier override; intra-op draft + lock; admin tuning; "Recompute now"; wound-photo review (when that pipeline is restored); every "Send", "Submit", "Save", "Lock", "Override" button.
- Add a global `data-readonly-role="np_pa"` style hook in `frontend/styles.css` that visually grays disabled actions.

#### 4.8 Tests

Add `tests/test_intraop_workflow_rn_drafts_surgeon_locks.py`:

1. RN creates form, fills all 11 fields, calls `mark-ready-for-review`. Status moves to `READY_FOR_SURGEON_REVIEW`. Surgeon notification fired.
2. RN attempts `lock` — 403.
3. RN attempts to `PATCH` the form while status is `READY_FOR_SURGEON_REVIEW` — 409.
4. Surgeon attempts `mark-ready-for-review` — 403.
5. Surgeon attempts `lock` while status is `IN_PROGRESS` — 409 with helpful error.
6. Surgeon attempts `lock` while status is `READY_FOR_SURGEON_REVIEW` — 200; reassessment fires; episode is `POST_OP`.
7. Surgeon edits fields then locks — edits persist with `surgeon_locked_by` attribution.
8. RN recalls draft — status moves back to `IN_PROGRESS` and surgeon receives recall notification.
9. Conservative-default cron at OR-end + 24h with status `READY_FOR_SURGEON_REVIEW` — fires the existing overdue page to the surgeon.

### 5. PRD update

Edit `docs/prd/intraop-reassessment-v1.md` to reflect the new workflow:

- §3.2 ("Two ingestion paths"): keep PDF + manual fill; clarify the filler is the **RN**.
- §3.3 ("Lock behavior"): replace single-surgeon model with the `IN_PROGRESS → READY_FOR_SURGEON_REVIEW → LOCKED` lifecycle. The RN sends to surgeon; the surgeon locks.
- §4.3 ("Per-field origin tracking"): add `RN_DRAFT` and `SURGEON_REVIEWED` to the origin enum.
- §7 ("Form lifecycle"): replace the diagram and state list with the new five-state machine.
- §10 ("API contracts"): add the `mark-ready-for-review` endpoint.
- §12 ("Cross-cutting requirements") permissions table: rewrite to match §3.3 above.

Add a top-of-file note: `Updated 2026-MM-DD by pass-4: workflow changed from single-surgeon model to RN-drafts → surgeon-reviews-and-locks.`

Also update `docs/prd/README.md` §3.5 (audit pattern) and §3.7 (out-of-scope) to mention the new workflow + remove anesthesia provider and clinical operations lead from any role mention.

### 6. Out of scope for this pass — do not build

- Wound-photo upload, nurse review, training-data export — still deferred.
- RPM device readings — still deferred.
- Persisted tuning store backed by versioned `tuning.json` — still deferred; the POST tuning admin endpoints stay as no-op stubs gated by `system_admin`.
- Auto co-paging the surgeon on critical post-op events (the user has not asked for this; RN remains the gate).
- Multi-surgeon pods. Cap is enforced at exactly 1 surgeon (the director).
- Role changes for existing patient-session auth. Patients still don't carry roles.

### 7. Definition of done

- `pytest backend/tests/ -q` reports the post-pass-3 baseline plus the new tests added in §1.2 migration check, §2.4, §3.5, §4.8.
- No code or comment in `backend/` references `anesthesia` or `clinical operations lead` as a role. Clinical-source labels in intake parser may keep the literal `"doctor"` string; comment them clearly.
- `team_members` rows from before this pass are migrated cleanly: `doctor` → `surgeon`, `nurse` → `rn_coordinator`, `director` → `surgeon` + `is_team_director=1`.
- Onboarding wizard caps the team at exactly 4 members (1 surgeon-director + 1 RN + 2 NP/PA), enforced at both API and UI.
- Every triage write endpoint returns 403 to NP/PA and to anonymous.
- Surgeon dashboard surfaces the "Forms awaiting your review" list and visually marks Tier 3 patients on the roster.
- NP/PA dashboard renders every action button disabled with a "Read-only access" tooltip.
- Intra-op form moves through `IN_PROGRESS → READY_FOR_SURGEON_REVIEW → LOCKED`; RN owns the draft, surgeon owns the lock; recalled drafts fire surgeon notification.
- `docs/prd/intraop-reassessment-v1.md` has been updated and committed in the same change set.

When done, append to `docs/prd/triage-build-pass-3-changelog.md` (or create `triage-build-pass-4-changelog.md`) summarizing what changed, which tests were added, the role-token migration counts, and any follow-ups for v5.
