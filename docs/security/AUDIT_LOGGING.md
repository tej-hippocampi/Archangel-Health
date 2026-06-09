# Audit Logging (PRD-5)

HIPAA **§164.312(b)** requires recording and examining activity in systems that
contain ePHI; **§164.316(b)(2)** requires retaining that documentation for **six
years**. We implement a tamper-evident, append-only audit trail.

## What is recorded

A pure-ASGI middleware (`backend/audit/middleware.py`, installed as the innermost
layer) records one event for every request to an ePHI-bearing surface:

- `/patient/*`, `/doctor/patient/*` (page loads)
- `/api/patient*`, `/api/patients*`, `/api/episodes/*`, `/api/escalations*`,
  `/api/intake-forms*`, `/api/eligibility*`, `/api/digital-care-companion*`,
  `/api/avatar/chat`, `/api/pre-op/intake*`, `/admin/*`

Auth, onboarding, health, docs, and static assets are intentionally **not** audited
(no PHI). Each event stores **minimum-necessary** metadata only:

| Field | Example |
|---|---|
| `ts` | UTC timestamp |
| `actor_type` / `actor_id` | `patient`/`<id>`, `staff:surgeon`/`<email>`, `admin`, `anonymous` |
| `action` | HTTP method |
| `resource` | request path |
| `patient_id` | extracted from the path when present |
| `outcome` | `success` / `denied` / `error` (from the status code) |
| `source_ip`, `user_agent` | caller IP + UA |

**Request and response bodies are never read or stored** — no clinical content,
no message text, no PHI beyond the patient id already present in the URL.

## Tamper-evidence (hash chain)

Each row stores `prev_hash` and `row_hash = sha256(prev_hash + canonical(row))`,
so the rows form a chain. Altering or deleting any row breaks every hash after it.

- `GET /admin/audit/verify` recomputes the chain end-to-end and returns
  `{ok, count, broken_at_id}`.
- `GET /admin/audit/events?patient_id=&actor_id=&since=&limit=` reads the trail.

Backed by the same SQLite DB as `TeamStore` (`TEAM_DB_PATH`).

## Retention & production hardening

- **6-year retention is a floor.** We never auto-delete (deleting would break the
  chain). Archive/rotate at the ops layer if needed, preserving the chain.
- In production, point `TEAM_DB_PATH` at a **WORM / object-lock-backed volume**, or
  ship `audit_events` to an append-only sink (e.g. CloudWatch Logs with a retention
  lock / S3 Object Lock), so the trail is immutable at the storage layer too — the
  hash chain proves integrity; object-lock prevents deletion.

## Still open (follow-ups)

- Migrate the in-memory eligibility audit (`eligibility/store.py`) into this store.
- PHI-safe application logging: replace the remaining PHI-bearing `print()` calls
  with redacted structured logging.
