# Pre-Op Intake Bot Phase 1 QA Checklist

## Feature flag
- Confirm backend env has `ENABLE_PREOP_INTAKE_BOT_V2=1`.
- Restart backend and verify `POST /api/intake-forms/start-interview` returns `200`.

## Patient flow
- Open a pre-op patient page at `/patient/{id}/pre-op`.
- Verify intake card status starts as `Not Started`.
- Start interview, answer at least 4 prompts, and complete interview.
- Verify `Processing your answers...` appears, then structured form renders.
- Confirm red flags (if present) show patient-friendly banner text.
- Edit multiple fields and click out of each input; refresh page and verify edits persisted.
- Click `Submit Form`; confirm status changes to `Submitted`.
- Edit one more field after submit; confirm status changes to `Updated`.

## Doctor flow
- Open doctor portal `/`.
- In patient roster, verify intake status pill appears per patient row.
- Open a pre-op patient detail and click `View Intake Form`.
- Verify form is read-only, includes source labels, conflicts, red flags, and edit history entries.
- Click notification bell and verify unread intake notifications appear.
- Mark a notification as read and verify unread count decreases.

## API/permissions checks
- As doctor-authenticated session, try `PATCH /api/intake-forms/{id}` and verify `403`.
- Verify patient intake fetch endpoint works: `GET /api/intake-forms/latest/{patient_id}`.
- Verify edit history endpoint returns entries after at least one patient edit.

## Backward compatibility checks
- Existing pre-op resources (audio, battlecard, notify care team) still function.
- Existing doctor roster and escalation tabs still load without JavaScript errors.

