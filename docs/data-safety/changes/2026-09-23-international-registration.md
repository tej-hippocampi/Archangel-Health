# International registration selection — 23 September 2026

The first registration screen displayed “Outside the US” as a disabled
placeholder. An applicant could not select it, and the identity store also
treated an explicit empty state as an instruction to retain an earlier US
state. Saved review credentials and CV suggestions could restore that state.

## Scope and invariants

- The registration selector now includes an enabled empty-value answer and
  labels the field optional. Other selectors retain their disabled placeholders.
- `license_state` omitted or null preserves existing data. An explicit empty
  string clears the US state. Existing US state normalization remains.
- Writes affect the current realm's team SQLite database only:
  `health_systems.director_license_state` and existing director draft
  `asclepius_people.credentials_json.licenseState`, its `cvManualFields`
  provenance marker, and the ordinary update timestamps. Existing identity
  edits and password/OTP rules continue to apply.
- Identity and draft synchronization share one transaction. The write lock
  precedes reading credentials so concurrent CV updates cannot be overwritten.
  Malformed stored credentials fail visibly and roll back; they are never
  replaced by an empty object. Unrelated credential keys and original CV parse
  values remain unchanged. Later review edits retain precedence on reload.
- No migration, bulk rewrite, deletion, account approval, real email, financial
  operation, blob write, encryption key change, or patient-data operation is
  involved. Production and sandbox use the same realm-aware store method.

## Preservation and recovery evidence

`test_international_selection_clears_a_saved_us_state_and_survives_reload`
runs both with and without an existing review draft. It takes a consistent
SQLite backup of the nonempty fixture, inventories every table and field,
then exercises omitted/null requests, repeated explicit clearing, and session
reload. The after inventory permits only the intentional columns listed above;
every other field and row identity must remain. The saved credential object is
also compared directly, including CV suggestions, original parse, and other
manual fields. A separate restore from the backup must match the full before
inventory without exemptions. Fixtures and backups live in pytest temporary
directories; they are not production snapshots.

`test_invalid_saved_credentials_roll_back_an_identity_correction` verifies
that malformed original JSON and the prior identity/state survive a failed
update. Existing CV lifecycle tests cover concurrent worker merges. This
change introduces no file receipt or external service operation, so disk-full
upload and provider-delivery experiments are outside its scope.

Frontend tests exercise selectable international answers, password validation,
the outgoing empty value, saved review/CV hydration, and UK registration fields.
The existing real-browser test exercises the built landing app with isolated
API stores at desktop and mobile widths: US state → Outside the US → save →
reload → email verification → UK/India credential review. Network requests are
intercepted and no test mail is delivered.

## Release and rollback

Run the onboarding, international signup, credentialing and CV lifecycle tests,
frontend suite/build, browser gate, SQL gate, route baseline, import scan,
merge-readiness, and independent review before release. No production store
has been inspected, backed up, or changed by this patch; fixture evidence does
not establish live backup health. Verify current platform backup/restore
readiness before deployment. Rollback reverts application code; do not restore
old databases over newer applications. There is no schema migration to undo.
