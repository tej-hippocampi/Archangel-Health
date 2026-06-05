# PRD-1: Patient PHI Access Control — close the unauthenticated IDOR

## 1. Problem & threat
All patient-facing routes funnel their access check through
`_assert_staff_can_access_patient(patient_id, staff)` in `backend/main.py`
(~line 153). That helper RETURNS EARLY when `staff is None`:

```python
def _assert_staff_can_access_patient(patient_id, staff):
    if staff is None or staff.source != "tenant" or not staff.tenant_id:
        return            # <-- no token => access GRANTED
```

Because patient IDs are guessable (`maria_001`, `demo_thenuk_001`,
`demo_tej_patel_001`), an unauthenticated attacker can read full PHI and chat
with an AI preloaded with a patient's clinical record. The clinic_code +
resource_code pair is only used by `/api/patient/by-codes` to LOOK UP the id;
it is never enforced on the resource. This violates 45 CFR §164.312(a) (access
control), §164.312(d) (authentication), and §164.502(b) (minimum necessary).

Note the codebase has TWO gates:
- `_assert_staff_can_access_patient` — LENIENT (the bug; on patient-or-staff routes)
- `_assert_clinical_staff_can_access_patient` → `assert_staff_patient_scope` in
  `staff_context.py` (STRICT — already raises 401 when staff is None; leave it)

## 2. Goal / definition of done
Every route that exposes or mutates a specific patient's PHI must require ONE of:
- (a) a valid CLINICAL STAFF token already scoped to that patient's health system, OR
- (b) a valid PATIENT SESSION bound to that exact `patient_id`.

Anonymous or wrong-patient access returns **404** (never 403 — avoid id
enumeration). No change to the legitimate patient UX: patients still enter 2 codes.

## 3. Design overview (cross-origin–safe, minimal JS churn)
Codes are entered in the landing app (`landing/`, a different origin from the
backend — see `landing/src/lib/auth-api.ts:262`). So we do NOT set a session
cookie on the cross-origin `by-codes` fetch. Instead:

1. `GET /api/patient/by-codes` (main.py ~1838) validates the two codes (as today),
   then MINTS a short-lived (5 min), single-use **entry token** bound to the
   `patient_id` and returns `dashboard_url` with it as a query param:
   `{BASE_URL}/patient/{pid}?k=<entry_token>` (or `.../pre-op?k=...`).
2. The browser does a top-level NAVIGATION to that backend-origin URL
   (first-party). The page route (`GET /patient/{id}` etc.) sees `?k=`, validates
   + consumes the entry token, and sets an **HttpOnly, Secure, SameSite=Lax**
   cookie `pt_session` (signed JWT, 8h) on the backend origin, then 302-redirects
   to the same path WITHOUT `?k=` (so the token never lingers in history/referrer)
   and serves the page.
3. All subsequent same-origin calls from `app.js` / `pre-op.js` /
   `voice-avatar.html` automatically send the `pt_session` cookie (default `fetch`
   credentials are same-origin), so NO Authorization-header changes are needed in
   the patient JS. The API routes enforce the cookie.

Staff continue to use their existing `Authorization: Bearer` tokens unchanged.

## 4. New module: `backend/patient_session.py`
Reuse the existing jose/jwt + `AUTH_SECRET` setup (see `auth.py` / `tenant_jwt.py`).

```python
AUTH_SECRET = os.getenv("AUTH_SECRET", "change-me-in-production-elysium")
ALGORITHM = "HS256"
PATIENT_SESSION_TTL_MIN = 8 * 60
ENTRY_TOKEN_TTL_MIN = 5

@dataclass
class PatientSession:
    patient_id: str
    health_system_id: Optional[str]
    jti: str

def create_entry_token(patient_id, health_system_id) -> str:
    # JWT: {typ:"patient_entry", pid, tid, jti:uuid4, exp: now+5m}

def consume_entry_token(token) -> Optional[PatientSession]:
    # decode + verify typ=="patient_entry"; reject if jti already consumed
    # (single-use). Mark jti consumed. Return PatientSession or None.

def create_patient_session(patient_id, health_system_id) -> str:
    # JWT: {typ:"patient", pid, tid, jti:uuid4, exp: now+8h}

def decode_patient_session(token) -> Optional[PatientSession]:
    # decode + verify typ=="patient"; reject if jti revoked. Return or None.
```

Single-use / revocation store: prefer a small table in `team.db` (TeamStore) so it
survives restarts: `consumed_entry_jti(jti, exp)` and `revoked_patient_jti(jti)`.
Add `TeamStore.mark_entry_jti_consumed(jti)`, `is_entry_jti_consumed(jti)`,
`revoke_patient_jti(jti)`, `is_patient_jti_revoked(jti)`. GC expired rows
opportunistically. (An in-process TTL set is acceptable for v1 since the store is
single-process, but the DB version is preferred and matches PRD-3/PRD-5.)

Cookie attributes (FastAPI `Response.set_cookie`):
```python
key="pt_session", httponly=True, samesite="lax",
secure=(os.getenv("ENV") == "production"),
max_age=PATIENT_SESSION_TTL_MIN * 60, path="/"
```

## 5. New dependency + unified access helper (`backend/main.py`, near line 153)

```python
from fastapi import Request
from patient_session import decode_patient_session, PatientSession

def get_patient_session(request: Request) -> Optional[PatientSession]:
    tok = request.cookies.get("pt_session")
    if not tok:
        return None
    return decode_patient_session(tok)

def assert_patient_or_staff_access(
    patient_id: str, *, staff: Optional[StaffContext],
    patient_session: Optional[PatientSession],
) -> None:
    if patient_id not in _patient_store:
        raise HTTPException(404, "Patient not found")
    # Staff path: keep existing tenant scoping (strict — no early grant on None)
    if staff is not None:
        d = _patient_store.get(patient_id) or {}
        hs = str(d.get("health_system_id") or "")
        if staff.source == "tenant":
            if hs and (not staff.tenant_id or hs != str(staff.tenant_id)):
                raise HTTPException(404, "Patient not found")
        elif staff.source == "landing" and hs and hs != DEMO_HEALTH_SYSTEM_ID:
            raise HTTPException(404, "Patient not found")
        return
    # Patient path
    if patient_session is not None and patient_session.patient_id == patient_id:
        return
    raise HTTPException(404, "Patient not found")
```

IMPORTANT: do NOT keep the old early-return behavior. Replace all call sites of
`_assert_staff_can_access_patient` with `assert_patient_or_staff_access` (preferred),
or make the old function raise instead of returning when no principal is present.

## 6. Route inventory — exactly what to change
For each Group A/A2 route, add
`patient_session: Optional[PatientSession] = Depends(get_patient_session)` and
replace `_assert_staff_can_access_patient(pid, staff)` with
`assert_patient_or_staff_access(pid, staff=staff, patient_session=patient_session)`.

**GROUP A — patient-or-staff (patient MUST reach with their session):**
- `GET /patient/{patient_id}` (~3989) — also sets cookie via `?k=` (see §7)
- `GET /patient/{patient_id}/pre-op` (~3961) — also sets cookie via `?k=`
- `GET /patient/{patient_id}/digital-care-companion` (~3933) — also sets cookie via `?k=`
- `GET /patient/{patient_id}/voice` (~3934) — also sets cookie via `?k=`
- `GET /api/patient/{patient_id}/config` (~3895)
- `GET /api/patient/{patient_id}/discharge` (~3918)
- `GET /api/patient/{patient_id}/battlecard` (~3884)
- `GET /api/patient/{patient_id}/audio` (~3846)
- `GET /api/patient/{patient_id}/resources` (~3734)
- `POST /api/patient/{patient_id}/events` (~2178)
- `POST /api/digital-care-companion/chat` (~4055) — keys on `req.patient_id`
- `POST /api/avatar/chat` (~4056) — keys on `req.patient_id`

**GROUP A2 — patient-facing but currently UNAUTHENTICATED (no staff dep at all):**
Take only `patient_id` in the body. Require patient-session bound to that id OR
scoped staff.
- `POST /api/pre-op/intake/start` (~4813)
- `POST /api/pre-op/intake/answer` (~4835)
- `POST /api/pre-op/intake/submit` (~4875)

**GROUP B — STAFF-ONLY routes currently using the LENIENT gate → TIGHTEN to the
strict `_assert_clinical_staff_can_access_patient` (reject patient sessions):**
- `POST /api/intake-forms/start-interview` (~4165)
- `POST /api/intake-forms/{id}/interview/section-message` (~4258)
- `POST /api/intake-forms/{id}/interview/complete-section` (~4364)
- `POST /api/intake-forms/{id}/interview/reset-section` (~4503)
- `POST /api/intake-forms/{id}/complete-interview` (~4553)
- `GET /api/intake-forms/{id}` and `/latest/{patient_id}` (~4638, ~4658)
- `GET/POST /api/patients/{id}/preop-window/*` (~3016, ~3031)
- any other lenient-gate call site that is a clinician action. When unsure, default
  to STAFF-ONLY — patients never need these.

**GROUP C — already code-validated, no staff context (leave logic; MUST be
rate-limited per PRD-2; MAY mint a `pt_session` on success):**
- `GET /survey`, `POST /api/survey/submit` (~2757, ~2830)
- `GET/POST /api/preop-survey/*` (~2925, ~2944)

**GROUP D — already strict (`_assert_clinical_staff_can_access_patient`): NO CHANGE.**
(discharge-materials, pcp-referral, `PATCH /api/patient/{id}`, `/timeline`,
`/preop-audio`, `escalations/*`, `doctor/patient/{id}`, `latest-intake`, etc.)

## 7. Page routes that mint the cookie (Group A page handlers)
For `GET /patient/{id}`, `/pre-op`, `/digital-care-companion`, `/voice`:
1. Read `k: Optional[str] = None` query param.
2. If a valid `pt_session` cookie already matches pid → serve.
3. Else if `k` present: `sess = consume_entry_token(k)`; if valid and
   `sess.patient_id == pid` → return a `RedirectResponse` to the same path (no
   `?k=`) with `response.set_cookie("pt_session", create_patient_session(...))`.
   (Browser re-requests with the cookie → step 2 passes.)
4. Else if scoped staff → serve (staff previewing).
5. Else → 404.

## 8. `by-codes` change (main.py ~1838)
After a successful code match:
```python
entry = create_entry_token(pid, d.get("health_system_id"))
dashboard_path = f"/patient/{pid}/pre-op" if is_preop else f"/patient/{pid}"
return {"patient_id": pid, "dashboard_url": f"{base_url}{dashboard_path}?k={entry}"}
```
Rate-limit this endpoint to 10/min/IP (PRD-2). Keep the generic 404 — never reveal
whether the clinic_code or the resource_code was wrong.

## 9. Frontend changes
- `landing/src/lib/auth-api.ts` (~262): no logic change — it already consumes
  `dashboard_url` and navigates; the `?k=` is now included. Verify the caller uses
  `window.location.href = dashboard_url` (top-level nav, NOT a fetch).
- `frontend/app.js`, `frontend/pre-op.js`, `frontend/voice-avatar.html`,
  `frontend/postop.js`: no auth-header changes (same-origin cookie auto-sent).
  Confirm none use `credentials: 'omit'`; if any call the backend cross-origin, add
  `credentials: 'include'`.
- Add a friendly "session expired — please re-open your link or re-enter your
  codes" state when an `/api/patient/*` call returns 404/401 after the 8h TTL.

## 10. Logout / expiry
- `POST /api/patient/logout`: clears `pt_session` and revokes its jti.
- Sessions expire after 8h. Patients re-enter codes (still valid) for a fresh token.

## 11. Test plan (`backend/tests/test_patient_access_control.py`)
1. No cookie/token: `GET /api/patient/maria_001/discharge` → 404.
2. No cookie: `GET /patient/maria_001` (no `?k=`) → 404.
3. Happy path: `/api/patient/by-codes` with valid demo codes → `dashboard_url`
   contains `?k=`; extract token; `GET /patient/{id}?k=...` → 302 + Set-Cookie
   `pt_session`; follow with cookie → 200.
4. Wrong-patient session: mint session for A, call `/api/patient/{B}/discharge`
   with A's cookie → 404.
5. Single-use entry token: consume `k` twice → second use rejected.
6. Expired session: forge `pt_session` with past `exp` → 404.
7. Staff still works: tenant staff bearer for their own patient → 200; cross-tenant
   → 404.
8. Group B tightening: anonymous `POST /api/intake-forms/start-interview` → 401.
9. Chat: anonymous `POST /api/digital-care-companion/chat` → 404; with valid
   patient session for that pid → 200.
10. Enumeration guard: a route-walker test that hits EVERY registered route matching
    `^/(api/)?patient` and `^/patient/` with no auth and asserts the status is in
    {401, 404, 422} (never 200 with PHI). Enumerate via `app.routes` so new routes
    are covered automatically.

Run full suite: `cd backend && python3 -m pytest tests/ -q` (all green).

## 12. Rollout / safety
- Feature flag `ENFORCE_PATIENT_AUTH` (default `"1"`). When `"0"`, log a WARNING and
  fall back to legacy behavior — for a brief staged rollout only; remove the flag
  once verified. Demo mode (`DEMO_MODE=1`) must still pass the route-walker test, so
  seed a demo `pt_session` helper for demos rather than disabling auth.
- Update `.env.example`: document `ENV`, `ENFORCE_PATIENT_AUTH`.

## 13. Out of scope
- Patient passwords / OTP login (codes remain the credential).
- Encryption at rest, audit logging (PRD-5, PRD-6).
