# The buyer CRM is retired — the delivery rail is not

_Export & Approval PRD §5._

## What was removed

| Removed | Where it lived |
|---|---|
| `POST /api/asclepius/buyers` | `routers/asclepius.py` |
| `GET /api/asclepius/buyers` | `routers/asclepius.py` |
| `POST /api/asclepius/buyer-requests` | `routers/asclepius.py` |
| `GET /api/asclepius/buyer-requests` | `routers/asclepius.py` |
| `GET /api/asclepius/buyer-requests/{id}` | `routers/asclepius.py` |
| `POST /api/asclepius/buyer-requests/{id}/status` | `routers/asclepius.py` |
| `POST /api/asclepius/buyer-requests/{id}/batch` | `routers/asclepius.py` |
| `renderAdminBuyers` (the "Buyers & Requests" tab) | `frontend/asclepius/asclepius.js` |

All of them now return **404**. That is the intended state, and
`tests/test_export_approval_prd.py` asserts it.

Why: the CRM was a second, parallel place to describe a sale, kept up to date by
nobody, sitting in the same tab as the thing people actually came to that tab to
do. Export is now one page — pick a scope, see exactly what ships and what does
not, build the bundle — and the one CRM feature that carried weight ("send this
to a buyer") is attached to the export itself.

## What was NOT removed

**The tables `buyers` and `buyer_requests` still exist and still hold every
row.** They are never dropped, and nothing deletes from them:

* they are the historical record of real conversations with real counterparties;
* `tasks.buyer_request_id` still points into `buyer_requests`, so tasks created
  for a past request keep their provenance, and every record packaged from those
  tasks still carries `buyer_request_id` in its payload;
* `build_export(..., buyer_request_id=…)` still filters on it, so a past
  request's batch can still be cut.

Read them with SQL when you need them:

```sql
SELECT * FROM buyers ORDER BY created_at DESC;
SELECT * FROM buyer_requests ORDER BY created_at DESC;
```

The **delivery rail is fully supported and unchanged**:

* `buyer_accounts`, `buyer_deliveries` — tables
* `routers/asclepius_buyer.py`, `frontend/buyer/` — the buyer portal
* `POST /api/asclepius/admin/buyer-deliveries` — the organization-scoped send
* `deliver_existing_export()` — new: drops an already-built export into a
  buyer's workspace. This is what the Export tab's **Export + send to ▾** calls,
  so the bundle a buyer receives is the exact bundle the operator previewed,
  not a second one rebuilt from different filters.

## If you need the CRM back

Nothing was destroyed, so it is a revert of the endpoint block in
`routers/asclepius.py` and the `renderAdminBuyers` call in the Export subnav.
Prefer not to: the reason it went is that a second place to record a deal is a
second place for it to be wrong.
