# One approval, one export tab

_Implementation notes for the Export & Approval PRD._

## The bug, in one paragraph

A physician's submission carried **three statuses that never talked to each
other**:

| System | Field | States | What moved it |
|---|---|---|---|
| Payments ledger | `earnings.status` | accrued → approved / void / paid | a reviewer's verdict, or the 14-day auto-approve |
| QA pipeline | `submissions.status` | submitted → auto_validated → qa_checked → export_ready / needs_qa / rejected | validation + 15% sampling + the QA tab |
| Export gate | `records.status` | mirrors the submission per record type | `update_records_status_for_submission` |

Export reads **only** `records.status IN ('export_ready','exported')`. Payment
approval never touched it. QA approval never touched payment. **No admin action
moved all three.**

Two symptoms fell out of that. The "Pending review" row had no Approve button
because no approve endpoint existed for a labeler earning — only `/release`
(a proposed pay cut) and `/void`. And an export of *All specialties · V4* shipped
a hepatology case instead of the nephrology one you wanted, because the
nephrology records had never become `export_ready` — the export did exactly what
it was told, and the UI never said why.

## What changed

### §1 — The Approve action

`POST /api/asclepius/admin/earnings/{earning_id}/approve` (new, beside
`/release` and `/void`). Policy lives in `payments.approve_earning`; the router
maps refusals to HTTP. One action, three writes, one meaning:

1. the ledger row moves `accrued` → `approved` (compare-and-set — a double-click
   is a 409, not a double-write);
2. the submission and its records move to `export_ready`, but **only from**
   `submitted` / `auto_validated` / `qa_checked` / `needs_qa`. An `exported` case
   is never downgraded; a `rejected` one is Void's business;
3. an audit event, `earning_admin_approved`, carrying `prior_ledger`, `prior_qa`
   and `bypassed_qa_sampling: true` — because it does bypass QA sampling and an
   audit that hides that is not an audit.

`/void` is the mirror: ledger `void` **and** submission + records `rejected`,
from non-terminal states only.

**The convergence rule** (`payments.apply_ledger_decision_to_records`): a record
ships iff `records.status ∈ {export_ready, exported}`, and exactly four events
set it — admin Approve, reviewer accept, the 14-day auto-approve, and the QA tab.
All four also resolve the ledger. `tests/test_export_approval_prd.py::
test_the_four_paths_all_write_both_tables` enumerates them. **A fifth path is a
bug.**

Two of those four are new behavior: the 14-day auto-approve and a reviewer's
accept used to pay a case and leave it permanently unshippable.

### §1.3 — Copyable ids

`copyableId(id)` in `asclepius.js`, exposed on `adminSectionCtx()` and used by
the earnings ledger, Task Routing, the QA queue, the export excluded-list and
export history. The full id is always in the DOM and in the `title`; the copy
button uses `navigator.clipboard` with a hidden-selected-input fallback. The CASE
column carries `min-width` so it never collapses below the id's own width — other
columns shrink first.

### §2 — Export is one page

`Data → Export` has no subnav. Five scopes, one resolver
(`_resolve_case_slice`), which preview **and** bundle both call — so they cannot
disagree.

| Scope | Picker | Resolves to |
|---|---|---|
| Case | id input with a typeahead; accepts several, comma-separated | those cases' records, every labeler, every reviewer |
| Specialty | select | all shippable records in the specialty |
| Version | V1–V5 (V5 keeps its "ships via environments" note) | portal-version match |
| Physician | picked **by name**, shown as `Kalpesh Patel · nephrology · 7 cases` | `annotator_id_hashed` — **the bundle carries the hash only** |
| All | — | every exportable record |

The preview now reports **what is excluded and why**, from the same call:
unapproved submissions (with case id, physician hash, status and a reason), plus
the two exclusions that were always computed and never shown — records the buyer
profile cannot map, and mock/sandbox records.

`[ Approve all N ]` posts once to `POST /admin/export/approve`, which loops the
same `payments.approve_earning` server-side, then re-previews.

Yesterday's export would have read: *"1 case ships. 1 submission on
v4real-v4-neph-001 is awaiting approval and will not ship."*

### §2.3 — Bundle corrections

* **`license`**: `CC-BY-NC-4.0-clinical-eval` → **`archangel-commercial-v1`**.
  NC means non-commercial, on data sold to train commercial models. Records are
  re-stamped **at emit**, like `exported_at`, so the entire back catalogue ships
  under the right terms and **nothing in the `records` table is rewritten**.
* **`batch.json`**: `synthetic_prompt_count` → `model_generated_question_count`,
  with `case_provenance` beside it, so "real case, model-authored question" reads
  as what it is. The old key rides along for one release so a buyer's ingest
  script does not break on the rename.
* **Datasheet**: Composition gains `- Scope: **physician** · nephrology · 7 cases
  (annotator \`3f9a…\`)`. Hash, never name.

### §2.4 — History

`exports.scope_json` records what a bundle WAS. Rows written before it render as
`legacy` — their scope is genuinely unknown and inventing one would be worse.
Nothing is re-generated. The history row also shows delivery status.

### §4 — Migration

`asclepius/export_backfill.py` runs once at boot (and from
`backend/scripts/backfill_export_ready.py`). It finds earnings that are
`approved` or `paid` whose submission is still `submitted` / `auto_validated` /
`qa_checked` with no shippable record — cases we have already paid for that
cannot ship — and moves them to `export_ready`, logging
`records_backfilled_from_ledger`.

Deliberately narrow: `needs_qa` is a pending human decision and a migration does
not get to make it, and a `void` earning's records are **left alone** (a void may
have been a payment decision, not a quality one) and surfaced in the export
preview's excluded list instead.

Idempotent — a second run finds nothing.

### §0 — The no-data-loss contract

`backend/scripts/export_migration_inventory.py` writes
`docs/asclepius/EXPORT_MIGRATION_INVENTORY.md`. Run it `--label before` against
production, deploy, run it `--label after`; the second run exits non-zero if any
count went down or any id set changed (SHA-256 over the sorted ids). Both runs
are pure reads. `test_the_no_data_loss_contract_holds_across_the_migration`
asserts the same thing in CI, and `test_the_contract_catches_a_deleted_row`
proves the check can fail.

**No `DELETE`, no `DROP`, no `ALTER … DROP COLUMN` anywhere in this change.**

### §5 — Buyers

The CRM endpoints and screen are removed; the tables stay. See
[BUYER_CRM_RETIRED.md](BUYER_CRM_RETIRED.md).

## Deploy checklist

1. `ASCLEPIUS_DB_PATH=… python3 backend/scripts/export_migration_inventory.py --label before`
2. Deploy. The backfill sweep runs at boot, off the event loop, and never raises.
3. `ASCLEPIUS_DB_PATH=… python3 backend/scripts/export_migration_inventory.py --label after`
   — it must print `contract holds`.
4. Read the boot log line `asclepius.export_backfill: N case(s) were approved or
   paid but could not ship` — that N is how many cases this change just made
   sellable.

`ASCLEPIUS_LICENSE` still overrides the license string per deployment if a buyer
negotiates their own terms.
