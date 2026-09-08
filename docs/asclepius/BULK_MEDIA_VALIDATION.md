# Bulk media implementation validation

Local validation, 7–8 September 2026. Synthetic fixtures, isolated temporary
databases, fake model transport and dev-mode email. No production originals,
bucket objects or deployment variables were changed.

## Results

- Final focused regression run: **128 passed**. Includes 35 bulk-media tests plus
  existing upload, provider, durability and purpose-isolation coverage.
- Full backend run on the rebased checkout: **6,392 passed, 1 failed, 2 skipped**
  in 490.98 seconds. The one failure was the existing admin masthead resize test.
  It was independently reproduced in a detached, unchanged `origin/main`
  checkout at `cdf99f827`; no header code was changed here. The Linux CI full
  suite remains a merge gate; this local failure is not represented as a pass.
- Real headless Chrome behavior tests: **4 passed**. Signature-expiry retries,
  browser-close/IndexedDB resume, changed-file detection, cancellation and
  reselecting the same file. Uses real File/Worker/WebCrypto/IndexedDB with
  intercepted HTTP, not AWS.
- PRD citation audit: **22 citations and required sections passed**.
- Updated route snapshot: **666 routes**, with no removed existing routes.
- Dangling-import scan: **669 files, no dangling imports**.
- Data inventory: no local application database existed; the before/after
  inventory is empty. Tests separately verify additive media migrations preserve
  rows and legacy original retention for brokering/storage/unset/unknown policy.
- Two calendar tests failed identically on unchanged main because event queries
  used today's clock while the scheduler used 2 September. Their clocks are now
  aligned in the tests; no community production code changed.

## What the new tests establish

1 TiB and 100 GiB metadata admission without buffering media; valid multipart
planning; atomic concurrent quota reservations and resume at zero allowance;
cross-organization/account/realm isolation; a real 100,000-row paginated catalog;
missing, reordered and malformed part rejection; version/size validation;
whole-object hashes; uncertain-completion recovery; lease fencing; cancellation
and expiry races; no clinical processing; held corruption; permanent-error
handling and audited admin retry; immutable approved sources; small-source copy
and restart; provider-side CLI resume and credential separation; 90-day active
MPU preservation during orphan cleanup; review and buyer-bound manifests.

S3 SDK contract tests use botocore Stubber. Service fault injection uses a test
storage double. The same bulk test module runs against real PostgreSQL in its
dedicated CI job. Local SQLite results do not substitute for that job. An attempted
Moto smoke test was not accepted as evidence: Moto's ListParts response omitted
SHA-256 checksums required by the adapter, so the adapter correctly refused it.

## Independent auditor report

No remaining merge-blocking findings in the disabled-by-default implementation.
The auditor independently verified the expiry/signature race fix, small-source
copy parameters, bounded retries, lease fencing, preserved originals/reservations,
admin-only audited retry, pinned source identity, and the final retention change.
An independent run passed the then-current 32 bulk tests; the later four-policy
retention coverage raises that module to 35 tests. PRD and merge-readiness checks
passed. Full-suite failure resolution and PostgreSQL/browser CI remain merge
gates. Real S3/CORS/KMS, recovery, and capacity tests remain enablement gates.

## Not established

No real S3 or PostgreSQL production deployment was tested locally. No 100 GiB or
1 TiB object was transferred over a real network; no multi-day or 20-provider
benchmark was run. No 100–1,000-hour production-capacity claim follows from these
tests. Follow [the operations runbook](BULK_MEDIA_OPERATIONS.md) before enabling.

Reproduce focused backend checks with `python -m pytest tests/test_bulk_media.py`
from `backend`. Run `node frontend/tests/media-upload.cjs` with Playwright on
the Node module path for browser behavior. CI provisions PostgreSQL and Chromium
and runs these checks on every PR.
