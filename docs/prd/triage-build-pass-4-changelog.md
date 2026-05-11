# Triage Suite — Pass 4 Changelog

Pass 4 consolidates the **staff role model** (five tokens), enforces a **4-seat surgical pod cap** on onboarding, adds **per-route RBAC** across all four triage routers (`backend/auth_roles.py`), and replaces the intra-op form lifecycle with **RN drafts → surgeon reviews and locks** (`READY_FOR_SURGEON_REVIEW`, mark-ready / recall, surgeon-only lock). It surfaces a **Forms awaiting your review** queue for surgeons on `frontend/doctor.html`, adds **NP/PA read-only** UX hooks, and versions the five triage PRDs under `docs/prd/`.

## Role + schema migration

- **`team_members.is_team_director`** (SQLite, idempotent `ALTER`). Legacy rows migrate: `director` → `surgeon` + `is_team_director=1`; `doctor` → `surgeon`; `nurse` → `rn_coordinator` (guarded migration `team_members_roles_v4`).
- **JWT:** tenant staff tokens carry **`itd`** (director flag). Landing JWTs still only carry `sub`; role is resolved from the in-memory user store / profile.
- **`intraop_forms`:** `draft_completed_by`, `draft_completed_at`. Status enum **`READY_FOR_LOCK` → `READY_FOR_SURGEON_REVIEW`** (one-shot `UPDATE` on startup migration).

**Migration counts** are not logged automatically; on upgrade, expect `UPDATE intraop_forms … WHERE status='READY_FOR_LOCK'` to match any legacy rows (often **0** on fresh installs).

## Pod cap (onboarding)

- API: `POST …/onboarding/.../members` enforces **1 RN coordinator**, **2 NP/PA**, rejects extra **surgeon** rows (director is the only surgeon), max **3 non-director** members.
- Landing wizard: `Step4YourTeam` counters + disabled roles when caps hit.

## RBAC (`auth_roles.py`)

- `require_roles(staff, allowed)` — **401** if no Bearer, **403** if role not in set.
- `require_patient_session(staff)` — **403** if a staff Bearer hits a patient-only endpoint.
- **WRITE_CLINICAL** = `{surgeon, rn_coordinator}`; **ALL_CLINICAL** includes `np_pa` for reads.
- **NP/PA** blocked from **initial-tier/compute**, **preop-retier/compute**, and other writes per PRD §3.

## Intra-op workflow

- **PATCH:** RN edits in `NEW` / `IN_PROGRESS` / `REOPENED`; surgeon edits only in **`READY_FOR_SURGEON_REVIEW`**.
- **POST** `mark-ready-for-review` (RN): validation, status + draft metadata, **`escalations`** `intraop:ready_for_review`, event `INTRAOP_FORM_READY_FOR_REVIEW`.
- **POST** `recall` (RN): `READY_FOR_SURGEON_REVIEW` → `IN_PROGRESS`, escalation `intraop:draft_recalled`, event `INTRAOP_FORM_RECALLED`.
- **POST** `lock` (surgeon): requires **`READY_FOR_SURGEON_REVIEW`**; `IN_PROGRESS` → **409** with RN handoff message.
- **POST** `reopen`: **`X-Admin-Token`** OR **locking surgeon** Bearer (`surgeon_locked_by` email match).
- **GET** `/api/intraop-forms?status=READY_FOR_SURGEON_REVIEW` (surgeon, tenant-filtered): dashboard queue.

## Frontend

- **`frontend/intraop-form.html`:** Role-aware banners, **Send to surgeon for review**, **Recall draft**, surgeon **Lock**, **Reopen** (admin token vs surgeon session). Uses `archangel_doctor_auth_token` + `archangel_doctor_profile_ui_v2` for landing role when JWT has no `role` claim.
- **`frontend/doctor.html`:** Review queue panel; **Tier 3** roster emphasis; **`data-readonly-role="np_pa"`** on `body` + disabled intra-op / add-patient CTAs for NP/PA.

## PRDs

- Copied from `~/Downloads/` into **`docs/prd/`**: `initial-triage-v1.md`, `preop-retier-v1.md`, `intraop-reassessment-v1.md`, `postop-scoring-v1.md`, `README.md`.
- **`intraop-reassessment-v1.md`** updated for pass-4 workflow (§3.2–3.3, §4.3 origins, §7 states, §10 APIs, §12 permissions + implementation note).
- **`README.md`**: §3.5 intra-op escalation note; §3.7 pod cap / role consolidation footnote.

## Tests

- `backend/tests/test_role_migration.py`
- `backend/tests/test_onboarding_team_caps.py`
- `backend/tests/test_role_authorization.py`
- `backend/tests/test_intraop_workflow_rn_drafts_surgeon_locks.py`
- Updated: `test_intraop_router.py`, `test_intraop_lifecycle.py`, `_role_auth.py` (per-email landing tokens)

**Verification:** `cd backend && python3 -m pytest tests/ -q` → **623 passed** (2026-05-10).

## Follow-ups for v5

- Persisted **versioned tuning store** (`tuning.json` per stage) wired to admin POST surfaces.
- **Wound-photo** pipeline restoration + nurse review export.
- **RPM** / device streams.
- **Email/SMS** to surgeon supervisor on intra-op overdue (today: `escalations` row only for ready-for-review).
- **Multi-surgeon pods** (beyond director-as-single-surgeon).
