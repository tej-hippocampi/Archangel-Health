---
name: export-audit
description: Audit an export bundle before it goes to a buyer - license, contributor roster, physician names, answer keys, payment fields, scope and JSONL validity. Use before sending any bundle or sample to a buyer or lab.
---

# /export-audit

The Centaur sample went out as an earnings bundle, under an NC license, with an
eight-row contributor roster that did not match the records. A bundle is the only
artefact a buyer ever sees; everything wrong with it is wrong in public.

## Run it

```bash
python3 backend/scripts/export_audit.py path/to/bundle.zip
```

Exit 2 on any failed assertion, with the specific file and field named.

## What must be true of every bundle

1. **License is not NC.** A non-commercial license on a bundle sold to a lab is
   the one error that cannot be walked back after delivery.
2. **The contributor table lists only annotators present in `records.jsonl`** —
   no more, no fewer. A roster longer than the records is what happened before.
3. **No physician name anywhere.** Contributors appear as ids. Check every file,
   including `batch.json` and any README.
4. **No `answer_key`.** Shipping the key with the eval destroys the eval.
5. **No `amount_cents`, no `earning_id`.** Payment data is ours, not the buyer's,
   and its presence means the bundle was built from the earnings path.
6. **Scope is recorded in `batch.json`** — which specialty, which portal version,
   which date range. A bundle whose scope is not written down cannot be
   reproduced or corrected later.
7. **Every `.jsonl` parses**, line by line. One malformed line makes the buyer's
   loader fail on a file that looks fine.

## If anything fails

Do not patch the zip. Fix the export path that produced it and rebuild — a
hand-edited bundle is unreproducible, and the next one has the same bug.

## Before delivery

Run `/data-inventory` too if the export path ran a migration or a backfill.

## The other half

`export_audit.py` checks what must never be IN a bundle. It cannot check whether
the bundle's prose agrees with its records — the defect class that put seven
errors into the Centaur nephrology sample while this audit passed. Before any
bundle goes out, also run the review in
`docs/asclepius/EXPORT_REVIEW_PROMPT.md`.
