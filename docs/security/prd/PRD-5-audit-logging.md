# PRD-5: Tamper-evident Audit Logging for all ePHI access

§164.312(b) Audit Controls (Required) + §164.316(b)(2) 6-year retention.

## Context
The only structured audit log is `eligibility/store.py` `AUDIT_LOG`: an in-memory
list, FIFO-capped at 10k, wiped on restart, eligibility-only. PHI access via patient
dashboards, discharge, chat, and admin views is NOT audited. ~14 `print()`
statements log PHI (e.g., `main.py:4104` logs `patient_id` + chat context).

## Goal
Append-only, persistent audit trail of every ePHI access/mutation, retained 6
years, queryable by admin; remove PHI from application logs.

## Implementation
1. New `backend/audit/audit_log.py` backed by a dedicated SQLite table
   `audit_events` (append-only): `id` (PK autoincrement), `ts` (UTC ISO),
   `actor_type` (patient|staff|admin|system), `actor_id`, `action`, `resource_type`,
   `patient_id`, `source_ip`, `user_agent`, `outcome` (success|denied|error),
   `detail_json`, `prev_hash`, `row_hash`. Compute
   `row_hash = sha256(prev_hash + canonical(row))` for a tamper-evident chain. Expose
   NO UPDATE/DELETE methods.
2. `record(event)` helper + a FastAPI middleware/dependency that auto-logs every
   request to a `/patient`, `/api/patient`, or `/admin` path: actor (from
   PRD-1/PRD-3 context), `patient_id`, `action = method + route`, `outcome`, IP, UA.
   Do NOT store message bodies / clinical content — store IDs + action only
   (minimum necessary).
3. Migrate the eligibility audit hooks to the new store (keep the existing API for
   back-compat, but persist).
4. Retention: documented 6-year retention; `GET /admin/audit/events?limit=&patient_id=&since=`
   (admin-auth) and `GET /admin/audit/verify` that recomputes the hash chain and
   reports any break.
5. PHI-safe logging: replace PHI-bearing `print()` calls (grep
   `print\(.*patient|name|message`) with `safe_log()` that redacts/hashes IDs. Route
   through Python `logging` with a redaction filter.
6. Production note: in cloud, point the SQLite file at an object-lock / WORM-backed
   volume, or ship events to an append-only sink (e.g., CloudWatch Logs with
   retention lock). Document in `docs/security/AUDIT_LOGGING.md`.

## Acceptance criteria
- Accessing `/api/patient/{id}/discharge` writes one `audit_events` row with correct
  actor + outcome.
- A denied (404) access also writes a row with `outcome=denied`.
- Tampering with a row is detected by `/admin/audit/verify` (mutate a row in a temp
  DB and assert the chain breaks).
- No PHI in stdout logs (test scans captured logs for a known patient name/MRN).
- Tests in `backend/tests/test_audit_log.py`.
