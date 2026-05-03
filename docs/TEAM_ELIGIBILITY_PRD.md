# PRD — TEAM Eligibility Determination + Doctor Portal Workflow Redesign

| Field | Value |
|---|---|
| Feature | TEAM Eligibility Determination + Add-Patient / Detail-View Redesign |
| Document version | 0.2 |
| Owner | Tej Patel |
| Status | Build-ready |
| Last updated | 2026-05-03 |
| Target user | Pre-op nurse coordinator / Surgeon |
| Implementation | Existing Archangel Health stack — **FastAPI (Python) backend + static HTML/CSS/JS frontend** (no build step, no DB) |
| Audience | Claude Code / Cursor |

---

## 0. Stack & file map (read this first)

This is **not** a Next.js project. All work lands in the existing repo:

```
/backend                       # FastAPI app (single service)
  main.py                      # in-memory PATIENTS, /api/process-patient, dashboard routes
  team_store.py                # in-memory episode store (ensure_episode, list_active_episodes)
  pipeline/
    ingest.py                  # PDF/text ingest (existing)
    extract.py                 # field extraction (existing)
    classify.py                # classification (existing)
    generate.py                # discharge / prep material generation (existing)
  routers/
    tenant_portal.py           # tenant-scoped patient endpoints
    admin.py
    onboarding.py
    internal.py
  prompts/                     # existing prompt templates
  intake_section_prompts/      # existing
  integrations/                # external services (anthropic, twilio, sendgrid, etc.)

/frontend                      # vanilla HTML/CSS/JS (mounted at /static)
  doctor.html                  # ← all UI changes land here (Add Patient modal at L1044,
                               #   pre-op detail modal at L955, timeline/post-op modal at L910)
  styles.css                   # ← styles for new components
  app.js
  pre-op.html, pre-op.js       # patient-side pre-op view
  index.html, upload.html      # patient-side post-op
```

**No new framework, no new build step, no new database.** Persistence stays where it is today (in-memory `PATIENTS` list in `backend/main.py` + `team_store`). Add new modules under `backend/eligibility/` (mirrors existing `backend/pipeline/`).

**Files to create:**

```
/backend/eligibility/
  __init__.py
  parse_x12.py                 # X12 271 parser
  parse_pdf.py                 # PDF / OCR (reuse pipeline/ingest.py if possible)
  parse_csv.py                 # CSV header-alias resolver
  extract.py                   # Anthropic call + tool-schema, system prompt
  evaluate.py                  # deterministic verdict logic for the 6 checks
  pipeline.py                  # parse → extract → evaluate orchestrator
  store.py                     # in-memory eligibility check store + audit log
/backend/routers/
  eligibility.py               # all /api/eligibility-* endpoints (mounted from main.py)
```

UI work is additive edits to `frontend/doctor.html` + `frontend/styles.css` (no JS framework). Use the existing `byId()`, `apiJson()`, `showToast()`, `.modal-overlay/.modal/.panel/.btn/.btn.primary` patterns — do **not** introduce React/Vue/shadcn.

---

## 1. Scope (read this second)

This PRD covers two intertwined tracks:

**Track A — TEAM Eligibility Determination**
1. Coordinator clicks **"Add Patient"**
2. Coordinator chooses **Single** or **Group** intake
3. For Single: coordinator uploads one or more **Relevant Patient Files** (X12 271, PDF eligibility report, CSV export, surgical prep notes, etc.) and the system runs the six TEAM checks
4. For Group: coordinator drops a bundle of patient files; the system fans out, creates one pre-op patient per detected file, and runs eligibility per patient
5. Verdict is shown inline on the patient detail view as a **"TEAM eligible"** badge next to the patient's name

**Track B — Doctor portal detail-view simplification**
- **Post-op detail** collapses to a single primary action: **"Confirm Post-Op Notes"**, which opens an in-page panel with the AI-parsed notes, lets the surgeon confirm/edit, then auto-generates discharge materials and swaps the panel for three buttons: **View Discharge Materials**, **Send Discharge Materials**, **Edit Post-Op Notes**.
- **Pre-op detail** mirrors the same pattern: initially three buttons — **View Intake Form**, **Switch to Post-Op**, **Revise Prep Notes** (Revise Prep Notes opens the same confirm panel). After prep materials are generated: **View Preparation Materials**, **View Intake Form**, **Send Preparation Materials**, **Switch to Post-Op**.

Out of scope: PAM, triage, telehealth, EHR integration, scheduling.

**Note on terminology:** The system accepts *eligibility documents* — Medicare eligibility artifacts produced by clearinghouses, MACs, or EHR portals — alongside any other clinical files (surgical prep notes, intake notes). The user-facing label is **"Relevant Patient Files"**; the technical eligibility scope is the three formats below (X12 271, PDF, CSV). All other uploaded files are stored against the patient and treated as context.

---

## 2. The six TEAM eligibility checks

| # | ID | Check | Pass condition |
|---|---|---|---|
| 1 | `partA_active` | Part A active | Coverage active on surgery date |
| 2 | `partB_active` | Part B active | Coverage active on surgery date |
| 3 | `not_ma` | Original Medicare (not MA) | No Medicare Advantage plan enrollment on surgery date |
| 4 | `medicare_primary` | Medicare primary payer | No other primary payer ahead of Medicare |
| 5 | `not_esrd_basis` | Not ESRD-basis | Eligibility basis is age or disability, not End-Stage Renal Disease |
| 6 | `not_umwa` | Not UMWA | Patient is not enrolled in United Mine Workers of America Health Plan |

A patient is **TEAM-eligible** iff all six = PASS.

---

## 3. Add Patient flow — top-level

The existing Add Patient modal (`#addPatientModal` at `frontend/doctor.html:1044`) currently shows a Pre-Op vs Post-Op chooser as Step 1. **Replace that chooser with Single vs Group.** Episode type (pre-op vs post-op) is now derived implicitly: Group uploads always create pre-op patients; Single uploads ask for episode type *after* file ingest if it can't be inferred from the documents.

```
Click "Add Patient"
        │
        ▼
┌───────────────────────────┐
│  Step 0: Single vs Group  │
└───────────┬───────────────┘
            │
   ┌────────┴────────┐
   ▼                 ▼
┌─────────┐    ┌─────────────┐
│ SINGLE  │    │   GROUP     │
└────┬────┘    └──────┬──────┘
     │                │
     ▼                ▼
 §4 Single flow   §5 Group flow
```

### 3.1 Step 0 UI

Replace the contents of `#episodeTypeStep` with:

```html
<div class="episode-step" id="intakeModeStep">
  <div class="episode-step-title">Step 1 — Choose Intake Mode</div>
  <div class="episode-choice-grid">
    <button type="button" class="episode-choice" id="chooseSingleBtn">
      <h4>Single Patient</h4>
      <p>Add one patient and upload their relevant files (eligibility, prep notes, post-op notes).</p>
    </button>
    <button type="button" class="episode-choice" id="chooseGroupBtn">
      <h4>Group Upload</h4>
      <p>Drop a bundle of patient files. Patients are auto-created and queued as pre-op.</p>
    </button>
  </div>
</div>
```

Keyboard shortcut `n` still opens the modal with focus on Single. Cancel discards drafts.

---

## 4. Single-patient flow

### 4.1 Patient identity form (Step 1 within Single)

Same fields as v0.1, **but** all optional except name (the system can backfill MBI, DOB, and surgery date from uploaded eligibility docs in the next step):

| Field | Required | Validation |
|---|---|---|
| Patient Name | yes | 1–120 chars |
| Phone | no | E.164-ish |
| Email | no | RFC 5322 light |
| MBI | **no, but recommended** | Regex `^[1-9][A-Z][A-Z0-9][0-9][A-Z][A-Z0-9][0-9][A-Z][A-Z][0-9]{2}$` if provided |
| DOB | no | ≤ today − 18y; soft warning if < 65 |
| Scheduled Surgery Date | no | ±180 days; required before eligibility check runs (can be filled from documents) |
| Anchor procedure | no | Enum LEJR / HIP_FEMUR / SPINAL_FUSION / CABG / MAJOR_BOWEL — default empty, surfaced in a chip |

If MBI / DOB / surgery date are missing after document parsing, the system blocks the Determine step until the coordinator fills them inline.

### 4.2 Relevant Patient Files (Step 2 within Single)

**This is the rename.** The `#notesLabel` (currently "Patient-Specific Surgical Preparation Notes *" / "Discharge Notes *" at `doctor.html:2257`) becomes:

> **Relevant Patient Files**
> Drop eligibility documents (X12 271, PDF, CSV) and any other clinical files (prep notes, post-op notes, intake forms). The system will route eligibility files through the TEAM check and attach the rest to the patient record.

#### 4.2.1 Drop zone

Replace the single `#uploadArea` + textarea pattern with a multi-file drop zone (still using `.upload-area` / `.upload-content` classes, just looping):

```html
<div class="form-group">
  <label>Relevant Patient Files</label>
  <div class="upload-area" id="filesDrop">
    <div class="upload-content">
      <span class="upload-icon">📄</span>
      <p class="upload-text">
        Drop files here, or <label for="filesInput" class="upload-link">browse</label>
      </p>
      <span class="upload-hint">
        Eligibility: X12 271 (.x12/.271/.edi/.txt ≤5MB), PDF (≤25MB), CSV (≤10MB).
        Other: any PDF or text up to 25MB.
      </span>
    </div>
    <input type="file" id="filesInput" multiple
           accept=".pdf,.x12,.271,.edi,.txt,.csv,.tsv" style="display:none;" />
  </div>
  <ul class="file-list" id="fileList"></ul>
  <div class="or-divider"><span>or paste notes below</span></div>
  <textarea id="freeformNotes"
    placeholder="Optional — paste any additional notes, prep/discharge text, or context..."></textarea>
</div>
```

Each row in `#fileList` shows: filename, format badge (`X12_271` / `PDF` / `CSV` / `OTHER`), size, status pill (`queued` → `uploading %` → `validated` → `error`), and an `×` remove button.

#### 4.2.2 Accepted file types (eligibility detection)

| Format | Extensions | Detection | Max size |
|---|---|---|---|
| X12 271 | `.x12`, `.271`, `.edi`, `.txt` | First non-whitespace chars `ISA*` | 5 MB |
| PDF | `.pdf` | First 4 bytes `%PDF` | 25 MB |
| CSV | `.csv`, `.tsv` | UTF-8 + header row + ≥1 comma/tab | 10 MB |
| OTHER | any | falls through; stored against patient, **not** routed to eligibility extractor | 25 MB |

Client-side validation pipeline per file: extension check → size check → 1KB magic-byte sniff → PHI bumper (warn if patient last name not detected in file content for X12/PDF; soft warning only).

#### 4.2.3 Upload endpoint

`POST /api/eligibility-documents` (multipart). Server:
- Stores file under `<UPLOAD_DIR>/eligibility/{patientId}/{uuid}.{ext}` (start with local disk; S3 is a follow-up — there is no S3 wired today).
- Computes SHA256 + size.
- Returns `{ id, filename, format, sizeBytes, sha256, status }`.

`DELETE /api/eligibility-documents/{id}` removes the file and the record.

#### 4.2.4 Submit

Primary button: **"Determine Eligibility & Generate Resources"** (enabled when at least one file has `validated` status **OR** the freeform textarea is non-empty).

### 4.3 Determine step (Step 3 within Single)

Identical pipeline to v0.1: parse → extract → evaluate, streamed via Server-Sent Events to the modal. Stage strip: `Parsing` → `Extracting` → `Evaluating` → `Done`.

Result panel inside the modal:

- **Top banner**: large pill — 🟢 ELIGIBLE / 🔴 INELIGIBLE / 🟡 REVIEW NEEDED.
- **Six rows** (one per check) using existing `.panel` styling. Each row: check name, verdict pill, plain-language interpretation, "Show source" disclosure (verbatim ≤200-char excerpt), and "Override" button (UNKNOWN/FAIL only).
- **Bottom action bar**: `Re-run Extraction` (secondary), `Save as TEAM Episode` (primary, enabled iff overall = ELIGIBLE or all UNKNOWNs overridden to PASS), `Save as Standard Episode` (alternate path), `Cancel`.

After save, the modal closes and the new patient appears in `#rosterList` with the `TEAM eligible` badge applied (see §6.1).

### 4.4 Acceptance criteria — Single

- **AC-S1** Pressing `n` on `/patients` opens the modal focused on the Single tile.
- **AC-S2** Choosing Single advances to the identity form; Patient Name is the only required field.
- **AC-S3** Drop zone accepts any mix of supported formats; each file is validated client-side before upload.
- **AC-S4** PDF >25MB rejected client-side with "PDF exceeds 25MB limit".
- **AC-S5** "Determine Eligibility" runs the pipeline; SSE stream advances the stage strip.
- **AC-S6** All six rows render within 15s for a clean X12 271; sourceExcerpt populated per row.
- **AC-S7** UNKNOWN on any check disables "Save as TEAM Episode" until override or re-run resolves it.
- **AC-S8** On save, the patient appears on the roster with the `TEAM eligible` badge iff overall verdict is ELIGIBLE.
- **AC-S9** Cancel discards the draft patient + uploaded files (hard delete).

---

## 5. Group-upload flow

### 5.1 Goal

Doctor drops a bundle of patient files (a folder of PDFs, a multi-record CSV, multiple X12 271s, etc.). System fans out to **one patient per detected identity**, queues them on the dashboard, marks each as **pre-op**, and runs the eligibility pipeline per patient.

### 5.2 Step 1 within Group — Drop bundle

```html
<div class="episode-step" id="groupDropStep">
  <div class="episode-step-title">Step 1 — Drop Patient Files</div>
  <div class="upload-area" id="groupDrop">
    <div class="upload-content">
      <span class="upload-icon">📁</span>
      <p class="upload-text">
        Drop multiple files (or a zipped folder) — one or more patients per file is OK.
      </p>
      <span class="upload-hint">
        PDFs, X12 271, CSV exports. Up to 50 files / 200 MB per batch.
      </span>
    </div>
    <input type="file" id="groupInput" multiple
           accept=".pdf,.x12,.271,.edi,.txt,.csv,.tsv,.zip" style="display:none;" />
  </div>
  <ul class="file-list" id="groupFileList"></ul>
</div>
```

### 5.3 Identity fan-out

Server endpoint `POST /api/eligibility-batches` accepts multipart with multiple files. Pipeline per batch:

1. **Sniff & split** — for each file: detect format. CSVs: each row may be a separate patient — split on `mbi` column. X12: each `ST*270*` / `ST*271*` envelope is one patient. PDFs: one PDF = one patient (filename heuristics for grouping multi-page exports).
2. **Identity extract** — call Claude with a small tool-use prompt to pull `{ firstName, lastName, dob, mbi, surgeryDate?, anchorProcedure? }` from each split. Confidence score per identity.
3. **Patient draft create** — for each high-confidence identity (`HIGH` or `MEDIUM`): create a `Patient` record with `pipeline_type = "pre_op"` (always pre-op for group uploads), `eligibilityStatus = 'PENDING'`, attach the source file(s).
4. **Low-confidence pile** — surface in a "Needs review" list at the bottom of the Group modal: coordinator can edit the parsed fields or discard.

### 5.4 Eligibility per patient

After identity fan-out, the server queues an eligibility check per created patient. Status is visible from the dashboard: `Eligibility: Pending` chip on the row until the per-patient pipeline finishes, then it flips to `TEAM eligible` / `Ineligible` / `Review needed`.

### 5.5 Group results screen

The Group modal advances to a summary view:

- **Created**: N patients (table with name, DOB, MBI, surgery date, eligibility status chip)
- **Needs review**: M files (each editable inline)
- **Errors**: K files (e.g., corrupted PDF, unparseable CSV row)

Primary button: **"Done — Open Dashboard"** (closes modal; roster auto-refreshes; all newly created patients are flagged pre-op).

### 5.6 Acceptance criteria — Group

- **AC-G1** Choosing Group advances to the bundle drop zone.
- **AC-G2** Dropping 10 distinct patient files creates 10 draft patients, each appearing in `#rosterList` as pre-op.
- **AC-G3** Every group-created patient has `pipeline_type === 'pre_op'` regardless of the source document content.
- **AC-G4** Patients with low-confidence identity extraction land in "Needs review" and are NOT auto-added to the roster until the coordinator confirms.
- **AC-G5** Eligibility check status chip on each roster row updates in real time as the per-patient pipeline finishes.
- **AC-G6** A single CSV with 25 rows (one patient per row) creates 25 draft patients.
- **AC-G7** A corrupted file lands in "Errors" with a human-readable cause; it does not block other files in the batch.

---

## 6. Patient detail-view redesign

### 6.1 "TEAM eligible" badge

Next to the patient's name everywhere it appears (roster row, timeline modal `#timelineHeading`, pre-op detail modal `#preopDetailHeading`), render a pill:

```html
<span class="badge-team-eligible" title="All six TEAM checks passed">
  ✓ TEAM eligible
</span>
```

CSS (add to `frontend/styles.css`):

```css
.badge-team-eligible {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 2px 10px; margin-left: 10px;
  font-size: 12px; font-weight: 600; line-height: 1.6;
  color: #0f5132; background: #d1e7dd; border: 1px solid #badbcc;
  border-radius: 999px; vertical-align: middle;
}
.badge-team-ineligible { /* red variant */ color: #842029; background: #f8d7da; border-color: #f5c2c7; }
.badge-team-pending    { /* yellow variant */ color: #664d03; background: #fff3cd; border-color: #ffecb5; }
```

Render rule: read `patient.eligibilityStatus`:
- `ELIGIBLE` → green `✓ TEAM eligible`
- `INELIGIBLE` → red `✗ Not TEAM eligible`
- `BLOCKED_UNKNOWN` → yellow `⚠ Eligibility review needed`
- `PENDING` → yellow `… Eligibility pending`
- `DRAFT` / unset → no badge

### 6.2 Post-Op detail (timeline modal `#timelineModal` — `doctor.html:910`)

#### 6.2.1 Initial state — one button only

Replace the current Discharge Materials panel (`doctor.html:927-935`, two buttons `viewMaterialsBtn` + `sendMaterialsBtn`) with a **single primary button** until the surgeon has confirmed:

```html
<div class="panel" id="postopMaterialsPanel">
  <div class="panel-head"><span>Discharge Materials</span></div>
  <div class="panel-body">
    <p class="panel-note">
      A lot of source data is being parsed and analyzed. Confirm the post-op notes
      to generate discharge materials.
    </p>
    <div class="actions-row" id="postopActionsInitial">
      <button class="btn primary" id="confirmPostOpNotesBtn">Confirm Post-Op Notes</button>
    </div>

    <!-- Inline confirm panel — same page, no new modal -->
    <div class="inline-confirm-panel" id="postopConfirmPanel" hidden>
      <div class="inline-confirm-head">
        <h4>Review extracted post-op notes</h4>
        <button class="link-btn" id="postopCancelEdit">Cancel</button>
      </div>
      <textarea id="postopNotesEditor" rows="14"></textarea>
      <div class="inline-confirm-actions">
        <button class="btn" id="postopRevertBtn">Revert to AI extract</button>
        <button class="btn primary" id="postopConfirmGenerateBtn">
          Confirm & Generate Discharge Materials
        </button>
      </div>
      <div class="inline-progress" id="postopGenProgress" hidden>
        <div class="spinner-sm"></div>
        <span id="postopGenProgressLabel">Generating discharge materials…</span>
      </div>
    </div>

    <!-- Post-confirmation state — three buttons -->
    <div class="actions-row" id="postopActionsConfirmed" hidden>
      <button class="btn"        id="viewMaterialsBtn">View Discharge Materials</button>
      <button class="btn primary" id="sendMaterialsBtn">Send Discharge Materials</button>
      <button class="btn"        id="editPostOpNotesBtn">Edit Post-Op Notes</button>
    </div>
  </div>
</div>
```

#### 6.2.2 Behavior

1. Surgeon clicks **Confirm Post-Op Notes** → `#postopActionsInitial` hides, `#postopConfirmPanel` reveals (same modal, in-place — no new dialog).
2. `#postopNotesEditor` is pre-filled with the AI-extracted post-op notes (`GET /api/patient/:id/postop-notes` — new endpoint that returns the latest extracted text).
3. Surgeon edits if needed → clicks **Confirm & Generate Discharge Materials**:
   - `POST /api/patient/:id/postop-notes/confirm` with the final text body.
   - Server kicks off discharge-material generation (existing `pipeline/generate.py` path).
   - Inline `#postopGenProgress` strip animates while generation runs (typically 5–20s).
4. On success: `#postopConfirmPanel` hides, `#postopActionsConfirmed` reveals with the three buttons.
5. **Edit Post-Op Notes** re-opens `#postopConfirmPanel` (no destructive reset; a new confirm regenerates materials).

#### 6.2.3 Acceptance criteria — Post-Op

- **AC-PO1** When the timeline modal opens for a post-op patient with no confirmed notes, only the **Confirm Post-Op Notes** button is visible inside Discharge Materials.
- **AC-PO2** Clicking it reveals an inline editor pre-filled with AI-parsed text on the same page (no new modal opens).
- **AC-PO3** Clicking **Confirm & Generate** triggers material generation; the inline progress strip appears.
- **AC-PO4** When generation completes, the three buttons (View / Send / Edit) appear and the editor is hidden.
- **AC-PO5** **Edit Post-Op Notes** re-opens the editor with the last confirmed text.
- **AC-PO6** **Send Discharge Materials** behaves exactly as today (existing `sendMaterialsBtn` handler at `doctor.html:2204`).

### 6.3 Pre-Op detail (`#preopDetailModal` — `doctor.html:955`)

#### 6.3.1 Initial state — three buttons

Replace the current four-button row (`doctor.html:980-984`: View Prep Materials, View Intake Form, Send Prep Materials, Switch to Post-Op) with two states.

**Initial (no confirmed prep notes yet):**

```html
<div class="panel" id="preopMaterialsPanel" style="margin-bottom:0;">
  <div class="panel-head"><span>Prep Materials</span></div>
  <div class="panel-body">
    <p class="panel-note">
      Review the intake form, revise prep notes to generate preparation materials,
      or move this patient into the post-op flow.
    </p>
    <div class="actions-row" id="preopActionsInitial">
      <button class="btn"        id="viewIntakeFormBtn">View Intake Form</button>
      <button class="btn"        id="switchToPostOpBtn">Switch to Post-Op</button>
      <button class="btn primary" id="revisePrepNotesBtn">Revise Prep Notes</button>
    </div>

    <!-- Inline confirm panel — same shape as post-op -->
    <div class="inline-confirm-panel" id="preopConfirmPanel" hidden>
      <div class="inline-confirm-head">
        <h4>Review extracted prep notes</h4>
        <button class="link-btn" id="preopCancelEdit">Cancel</button>
      </div>
      <textarea id="preopNotesEditor" rows="14"></textarea>
      <div class="inline-confirm-actions">
        <button class="btn" id="preopRevertBtn">Revert to AI extract</button>
        <button class="btn primary" id="preopConfirmGenerateBtn">
          Confirm & Generate Preparation Materials
        </button>
      </div>
      <div class="inline-progress" id="preopGenProgress" hidden>
        <div class="spinner-sm"></div>
        <span id="preopGenProgressLabel">Generating preparation materials…</span>
      </div>
    </div>

    <!-- Post-confirmation state — four buttons -->
    <div class="actions-row" id="preopActionsConfirmed" hidden>
      <button class="btn"        id="viewPrepMaterialsBtn">View Preparation Materials</button>
      <button class="btn"        id="viewIntakeFormBtn2">View Intake Form</button>
      <button class="btn primary" id="sendPrepMaterialsBtn">Send Preparation Materials</button>
      <button class="btn"        id="switchToPostOpBtn2">Switch to Post-Op</button>
    </div>
  </div>
</div>
```

#### 6.3.2 Behavior

1. Coordinator clicks **Revise Prep Notes** → `#preopActionsInitial` hides, `#preopConfirmPanel` reveals on the same page (mirrors post-op).
2. Editor is pre-filled with the AI-extracted prep text (`GET /api/patient/:id/preop-notes`).
3. Coordinator edits and clicks **Confirm & Generate Preparation Materials**:
   - `POST /api/patient/:id/preop-notes/confirm` with the final text.
   - Server runs prep-material generation (existing path — preop_resource at `main.py:565`).
4. On success: `#preopConfirmPanel` hides, `#preopActionsConfirmed` reveals with **View Preparation Materials**, **View Intake Form**, **Send Preparation Materials**, **Switch to Post-Op**.
5. **View Intake Form** in either state opens `#doctorIntakeModal` as it does today.
6. **Switch to Post-Op** in either state runs the existing handler at `doctor.html:2234` (opens Add Patient pre-filled, post-op mode).

#### 6.3.3 Group-upload interaction

Patients created via Group upload (§5) land here in the **initial** state with the AI-extracted prep notes already populated from the bundled file. The coordinator's first action is typically Revise Prep Notes → Confirm.

#### 6.3.4 Acceptance criteria — Pre-Op

- **AC-PR1** Opening a pre-op patient (no confirmed prep notes) shows exactly three buttons: View Intake Form, Switch to Post-Op, Revise Prep Notes.
- **AC-PR2** **Revise Prep Notes** opens the inline editor on the same page (no new modal).
- **AC-PR3** **Confirm & Generate Preparation Materials** triggers material generation; progress strip appears.
- **AC-PR4** After generation, the four-button state shows (View Preparation Materials, View Intake Form, Send Preparation Materials, Switch to Post-Op).
- **AC-PR5** Patients created via Group upload appear in the initial three-button state and respect the same flow.
- **AC-PR6** TEAM-eligible badge is rendered next to the patient's name in `#preopDetailHeading` per §6.1.

---

## 7. Pipeline internals (server side)

### 7.1 Parse layer — `backend/eligibility/parse_*.py`

#### `parse_x12.py` (Python port of v0.1 spec)

```python
# backend/eligibility/parse_x12.py
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class Benefit:
    eb01: str = ""           # status code: 1=Active, 6=Inactive, L=Primary, ...
    eb03: str = ""           # service type: MA=Part A, MB=Part B, 30=Health Plan
    plan_begin: Optional[str] = None    # DTP*346
    plan_end: Optional[str] = None      # DTP*347
    payer_name: str = ""                # NM1*PR
    contract_id: str = ""               # REF*18
    industry_codes: list = field(default_factory=list)  # III
    messages: list = field(default_factory=list)        # MSG

@dataclass
class X12_271_AST:
    subscriber: dict = field(default_factory=dict)
    benefits: list = field(default_factory=list)
    msp: dict = field(default_factory=dict)
    messages: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    raw: str = ""

def parse_x12_271(raw: str) -> X12_271_AST:
    if not raw or "ISA" not in raw[:106]:
        raise ValueError("Not a valid X12 271 envelope")
    segment_delim = raw[105]
    element_delim = raw[3]
    segments = [s for s in raw.split(segment_delim) if s.strip()]
    ast = X12_271_AST(raw=raw)
    current: Optional[Benefit] = None
    for seg in segments:
        fields_ = seg.split(element_delim)
        tag = fields_[0]
        if tag == "NM1" and len(fields_) > 1 and fields_[1] == "IL":
            ast.subscriber = _parse_subscriber(fields_)
        elif tag == "EB":
            current = Benefit(
                eb01=fields_[1] if len(fields_) > 1 else "",
                eb03=fields_[3] if len(fields_) > 3 else "",
            )
            ast.benefits.append(current)
        elif tag == "DTP" and current and len(fields_) > 3:
            qual = fields_[1]
            if qual == "346": current.plan_begin = fields_[3]
            elif qual == "347": current.plan_end = fields_[3]
        elif tag == "REF" and current and len(fields_) > 2 and fields_[1] == "18":
            current.contract_id = fields_[2]
        elif tag == "MSG" and len(fields_) > 1:
            (current.messages if current else ast.messages).append(fields_[1])
        elif tag == "AAA":
            ast.errors.append(_parse_aaa(fields_))
    return ast
```

Reference fields for the six checks (unchanged from v0.1): EB03 service-type codes, EB01 status codes, DTP\*346/347 dates, NM1\*PR payer names, REF\*18 contract IDs, MSG text.

#### `parse_pdf.py`

Reuse `backend/pipeline/ingest.py` if it already does PDF text extraction; otherwise wrap `pdfminer.six` (already a likely dep — check `requirements.txt`). OCR fallback via `pytesseract` only if `extracted_text_per_page < 50` chars on average. Keep dependency footprint small — both are pure-Python or have small wheels.

#### `parse_csv.py`

`pandas`-free implementation using stdlib `csv` + a Levenshtein header-aliaser. Column aliases (same shape as v0.1):

```python
COLUMN_ALIASES = {
    "partA_eff":      ["part_a_effective", "medicare_a_start", "parta_eff_dt", "part a eff", "medA_start"],
    "partA_term":     ["part_a_term", "medicare_a_end", "parta_end"],
    "partB_eff":      ["part_b_effective", "medicare_b_start", "partb_eff_dt", "part b eff"],
    "partB_term":     ["part_b_term", "medicare_b_end"],
    "ma_plan_id":     ["ma_plan", "maplanid", "ma_contract", "mapd_plan"],
    "msp_indicator":  ["msp", "secondary_payer", "primary_payer"],
    "esrd_indicator": ["esrd", "esrd_basis"],
    "umwa_indicator": ["umwa"],
}
```

If header similarity < 0.8, fall through and pass the raw CSV text to the LLM extractor.

### 7.2 Extract layer — `backend/eligibility/extract.py`

Use the existing Anthropic client wiring (`backend/integrations/`). Tool schema (Python dict matches v0.1 JSON):

```python
EXTRACT_TOOL = {
    "name": "extract_team_eligibility",
    "description": "Extract Medicare eligibility fields needed to determine TEAM eligibility.",
    "input_schema": {
        "type": "object",
        "properties": {
            "partA": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["ACTIVE", "INACTIVE", "UNKNOWN"]},
                    "effectiveDate": {"type": "string", "nullable": True},
                    "terminationDate": {"type": "string", "nullable": True},
                    "sourceExcerpt": {"type": "string"},
                },
                "required": ["status", "sourceExcerpt"],
            },
            # partB: same shape
            # medicareAdvantage: { enrolled: YES|NO|UNKNOWN, contractId, planName, sourceExcerpt }
            # medicarePrimary:   { isPrimary: YES|NO|UNKNOWN, secondaryReason, sourceExcerpt }
            # esrdBasis:         { isESRDBasis: YES|NO|UNKNOWN, sourceExcerpt }
            # umwa:              { isUMWA: YES|NO|UNKNOWN, sourceExcerpt }
            "overallConfidence": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
        },
        "required": ["partA", "partB", "medicareAdvantage", "medicarePrimary",
                     "esrdBasis", "umwa", "overallConfidence"],
    },
}
```

System prompt — verbatim from v0.1 §6.4 (the Medicare interpretation rules for MA detection, MSP, ESRD-basis vs comorbidity, UMWA, and the date arithmetic). Templated `{{SURGERY_DATE}}` is interpolated server-side.

API call:

```python
import anthropic
client = anthropic.Anthropic()  # picks up ANTHROPIC_API_KEY

def extract_eligibility(parsed_docs: list, surgery_date: str) -> dict:
    user_content = format_parsed_docs_for_llm(parsed_docs)
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        tools=[EXTRACT_TOOL],
        tool_choice={"type": "tool", "name": "extract_team_eligibility"},
        system=build_system_prompt(surgery_date),
        messages=[{"role": "user", "content": user_content}],
    )
    tool_use = next((c for c in resp.content if c.type == "tool_use"), None)
    if not tool_use:
        raise RuntimeError("No tool_use in response")
    return {"extracted": tool_use.input, "request_id": resp.id}
```

### 7.3 Evaluate layer — `backend/eligibility/evaluate.py`

```python
from datetime import date

Verdict = str  # "PASS" | "FAIL" | "UNKNOWN"

def coverage_active_on(c: dict, on: date) -> Verdict:
    status = c.get("status")
    if status == "INACTIVE": return "FAIL"
    if status == "UNKNOWN":  return "UNKNOWN"
    eff = _parse_date(c.get("effectiveDate"))
    term = _parse_date(c.get("terminationDate"))
    if eff and eff > on:   return "FAIL"
    if term and term < on: return "FAIL"
    return "PASS"

def evaluate(e: dict, surgery_date: date) -> dict:
    return {
        "partA_active":     coverage_active_on(e["partA"], surgery_date),
        "partB_active":     coverage_active_on(e["partB"], surgery_date),
        "not_ma":
            "PASS" if e["medicareAdvantage"]["enrolled"] == "NO"
            else "FAIL" if e["medicareAdvantage"]["enrolled"] == "YES"
            else "UNKNOWN",
        "medicare_primary":
            "PASS" if e["medicarePrimary"]["isPrimary"] == "YES"
            else "FAIL" if e["medicarePrimary"]["isPrimary"] == "NO"
            else "UNKNOWN",
        "not_esrd_basis":
            "PASS" if e["esrdBasis"]["isESRDBasis"] == "NO"
            else "FAIL" if e["esrdBasis"]["isESRDBasis"] == "YES"
            else "UNKNOWN",
        "not_umwa":
            "PASS" if e["umwa"]["isUMWA"] == "NO"
            else "FAIL" if e["umwa"]["isUMWA"] == "YES"
            else "UNKNOWN",
    }

def overall_verdict(r: dict) -> str:
    vs = list(r.values())
    if all(v == "PASS" for v in vs): return "ELIGIBLE"
    if any(v == "FAIL" for v in vs): return "INELIGIBLE"
    return "BLOCKED_UNKNOWN"
```

### 7.4 Pipeline orchestrator — `backend/eligibility/pipeline.py`

Async coroutine that emits status events through a per-check `asyncio.Queue` consumed by the SSE endpoint:

```
async def run_pipeline(check_id, patient, document_records):
    emit("PARSING")
    parsed = [parse_router(d) for d in document_records]
    emit("EXTRACTING")
    result = extract_eligibility(parsed, surgery_date=patient["surgeryDate"])
    emit("EVALUATING")
    verdicts = evaluate(result["extracted"], patient["surgeryDate"])
    overall = overall_verdict(verdicts)
    persist_check(check_id, parsed, result, verdicts, overall)
    emit("DONE", payload={"verdicts": verdicts, "overallVerdict": overall, ...})
```

### 7.5 Storage — `backend/eligibility/store.py`

In-memory dicts mirroring the existing `team_store` style:

```python
ELIGIBILITY_CHECKS: dict[str, dict] = {}   # id -> check record
ELIGIBILITY_DOCS:   dict[str, dict] = {}   # id -> {patientId, filename, format, ...}
AUDIT_LOG: list[dict] = []                 # action, actor, before, after, ts
```

Each `Patient` row in `backend/main.py` PATIENTS list gains:
- `eligibility_status: str`  (`DRAFT` | `PENDING` | `ELIGIBLE` | `INELIGIBLE` | `BLOCKED_UNKNOWN`)
- `eligibility_check_id: Optional[str]`
- `relevant_files: list[str]`  (eligibility doc IDs)

---

## 8. API contracts (FastAPI routes)

All routes live in `backend/routers/eligibility.py` and are mounted from `main.py` with the existing auth dep.

| Method & Path | Purpose |
|---|---|
| `POST /api/eligibility-documents` | Multipart upload — `{ patientId, file }` → `{ id, filename, format, sizeBytes, sha256 }` |
| `DELETE /api/eligibility-documents/{id}` | Remove uploaded file |
| `POST /api/eligibility-checks` | `{ patientId, documentIds[] }` → `{ id, status: 'PARSING' }` (202) |
| `GET /api/eligibility-checks/{id}` | Full check record |
| `GET /api/eligibility-checks/{id}/stream` | **SSE** — `event: status` and `event: result` |
| `POST /api/eligibility-checks/{id}/override` | `{ field, to: 'PASS', reason }` |
| `POST /api/eligibility-checks/{id}/rerun` | Re-run extraction without re-uploading |
| `POST /api/eligibility-checks/{id}/finalize` | `{ decision: 'SAVE_AS_TEAM' \| 'SAVE_AS_STANDARD' }` |
| `POST /api/eligibility-batches` | Group upload — multipart with multiple files; returns `{ batchId, created[], needsReview[], errors[] }` |
| `GET /api/eligibility-batches/{id}/stream` | SSE of per-patient pipeline progress in a batch |
| `POST /api/patient/{id}/postop-notes/confirm` | Body: `{ text }` → triggers discharge generation, returns generation status |
| `GET /api/patient/{id}/postop-notes` | Latest AI-extracted post-op notes text |
| `POST /api/patient/{id}/preop-notes/confirm` | Body: `{ text }` → triggers prep generation |
| `GET /api/patient/{id}/preop-notes` | Latest AI-extracted prep notes text |

SSE event format (existing pattern in repo):

```
event: status
data: {"stage": "EXTRACTING"}

event: result
data: {"verdicts": {...}, "overallVerdict": "ELIGIBLE", "extractedFields": {...}}
```

---

## 9. Frontend wiring (`frontend/doctor.html` + `app.js`)

Use the existing patterns. **No new framework.** All new JS goes inside the existing `<script>` block in `doctor.html`.

### 9.1 New helpers to add

```js
// EventSource wrapper for SSE consumption
function streamEligibility(checkId, onStatus, onResult, onError) {
  const es = new EventSource(`${API}/api/eligibility-checks/${checkId}/stream`);
  es.addEventListener('status', (e) => onStatus(JSON.parse(e.data)));
  es.addEventListener('result', (e) => { onResult(JSON.parse(e.data)); es.close(); });
  es.onerror = (e) => { onError?.(e); es.close(); };
  return es;
}

function renderTeamBadge(patient) {
  const map = {
    ELIGIBLE:        ['badge-team-eligible',   '✓ TEAM eligible'],
    INELIGIBLE:      ['badge-team-ineligible', '✗ Not TEAM eligible'],
    BLOCKED_UNKNOWN: ['badge-team-pending',    '⚠ Eligibility review needed'],
    PENDING:         ['badge-team-pending',    '… Eligibility pending'],
  };
  const entry = map[patient.eligibility_status];
  if (!entry) return '';
  return `<span class="${entry[0]}" title="${entry[1]}">${entry[1]}</span>`;
}
```

### 9.2 Wire-up checklist

| Element | Old handler | New behavior |
|---|---|---|
| `#addPatientBtn` | opens modal at episode-type chooser | opens modal at intake-mode chooser (Single / Group) |
| `#chooseSingleBtn` | (new) | shows `#patientFormFields` (as today), then `#filesDrop` Step 2 |
| `#chooseGroupBtn` | (new) | shows `#groupDropStep` |
| `#confirmPostOpNotesBtn` | (new) | shows `#postopConfirmPanel` with editor pre-filled from `GET /api/patient/:id/postop-notes` |
| `#postopConfirmGenerateBtn` | (new) | `POST .../postop-notes/confirm` → on success swap to `#postopActionsConfirmed` |
| `#editPostOpNotesBtn` | (new) | re-show `#postopConfirmPanel` |
| `#viewMaterialsBtn` | existing handler | unchanged |
| `#sendMaterialsBtn` | `doctor.html:2204` | unchanged |
| `#revisePrepNotesBtn` | (new) | shows `#preopConfirmPanel` with editor pre-filled from `GET /api/patient/:id/preop-notes` |
| `#preopConfirmGenerateBtn` | (new) | `POST .../preop-notes/confirm` → on success swap to `#preopActionsConfirmed` |
| `#switchToPostOpBtn` / `#switchToPostOpBtn2` | existing at `doctor.html:2234` | unchanged |
| `#viewPrepMaterialsBtn` | existing | unchanged |
| `#sendPrepMaterialsBtn` | existing | unchanged |
| `#viewIntakeFormBtn` / `#viewIntakeFormBtn2` | existing | unchanged |

### 9.3 Patient roster row (`renderRoster()` in `doctor.html`)

Append the badge after the name span:

```js
nameCell.innerHTML = `${esc(p.name)} ${renderTeamBadge(p)}`;
```

### 9.4 Modal headings

In `openTimelineModal(p)`:
```js
byId('timelineHeading').innerHTML = `${esc(p.name)} ${renderTeamBadge(p)} — Episode Timeline`;
```

In `openPreopDetailModal(p)`:
```js
byId('preopDetailHeading').innerHTML = `${esc(p.name)} ${renderTeamBadge(p)} — Pre-Op Preparation Detail`;
```

---

## 10. Cross-cutting requirements

### 10.1 Audit (writes to `AUDIT_LOG` in `eligibility/store.py`)

| Action | When |
|---|---|
| `patient_created`               | POST /api/patients (existing path) |
| `document_uploaded`             | POST /api/eligibility-documents |
| `document_deleted`              | DELETE /api/eligibility-documents/:id |
| `eligibility_check_started`    | POST /api/eligibility-checks |
| `eligibility_check_completed`  | pipeline DONE |
| `eligibility_override`          | POST /api/eligibility-checks/:id/override |
| `eligibility_finalized`         | POST /api/eligibility-checks/:id/finalize |
| `postop_notes_confirmed`        | POST /api/patient/:id/postop-notes/confirm |
| `preop_notes_confirmed`         | POST /api/patient/:id/preop-notes/confirm |
| `discharge_materials_generated` | server-side after notes confirm |
| `prep_materials_generated`      | server-side after notes confirm |

### 10.2 Security

- All new routes wrap with the existing `Depends(auth)` pattern from `backend/auth.py`.
- Files saved to local disk (not S3) — directory mode `0o700`, filenames are UUIDs only.
- LLM call: PHI in prompts. Anthropic BAA required for production. **For prototype, use synthetic test data only.** Add a `[ELIGIBILITY] Sending {N} chars to Anthropic` log line for every call so this is never invisible.
- Rate limit: 30 eligibility checks per coordinator per hour (in-memory token bucket).
- AV scan is a follow-up (no `clamav` wired today). Track but do not block v0.1.

### 10.3 Telemetry (log to stdout for now; no analytics service wired)

- `eligibility.duration_ms` per stage
- `eligibility.unknown_rate` per check field
- `eligibility.override_rate` per check field
- `eligibility.llm.input_tokens` / `output_tokens` / `cost_usd` per call

### 10.4 Accessibility

- All form inputs have `<label>`s.
- Drop zones support keyboard via `<label for=…>` browse fallback.
- Verdict pills include text, not color alone.
- Modal traps focus, Esc closes, Enter submits the active panel.

---

## 11. Edge cases (enumerated)

1. **271 returns AAA segment** ("Unable to Respond") — parser surfaces a parse error; UI: "Eligibility document was not actionable. Obtain a fresh check from your clearinghouse."
2. **Password-protected PDF** — block at upload with "PDF is password-protected. Provide an unlocked version."
3. **Scanned PDF, low OCR quality** — proceed but flag `overallConfidence: LOW`; UI: "OCR was used; please verify extracted fields."
4. **CSV missing required columns** — fall through to LLM; if still mostly UNKNOWN, surface "This CSV does not contain enough Medicare detail."
5. **Conflicting files** (271 says active, PDF says inactive) — surface conflict at the affected row; block verdict resolution until coordinator picks authoritative source.
6. **Surgery date in the past** — re-run check with the historical surgery date; warn "This check is for a past surgery date."
7. **MA contract ID present but plan name says "Original Medicare"** — LLM marks MA enrolled YES with explanation; surface conflict.
8. **Termination date == surgery date** — coverage active *through* term date; PASS unless explicitly stated otherwise.
9. **Patient < 65 (early Medicare via disability)** — soft warning at Step 1; eligibility check proceeds; ESRD-basis check still applies.
10. **LLM rate limit / API error** — exponential backoff (3 attempts); on final failure surface "Retry" button → `/rerun`.
11. **Server crashes mid-pipeline** — re-running creates a new EligibilityCheck; prior preserved.
12. **Browser closed during pipeline** — pipeline continues server-side; on return, dashboard shows in-progress chip; clicking opens streaming view.
13. **SSE disconnect** — client retries with last `event-id`; server replays missed events.
14. **Two coordinators add same MBI simultaneously** — uniqueness check returns the existing patient ID; loser sees "Patient was just created — view existing?"
15. **File mid-upload, browser refreshed** — local file is orphaned; cleanup job sweeps the upload dir nightly.
16. **Override applied, then re-run** — re-run preserves overrides per field unless explicitly cleared; UI surfaces "Previous overrides preserved" notice.
17. **Coordinator overrides FAIL → PASS** — allowed only with elevated permission role; reason text required; flagged in audit.
18. **Group upload with one corrupt file** — that file lands in "Errors"; the rest of the batch processes normally.
19. **Group upload with two files for same patient** (e.g., 271 + PDF for the same MBI) — auto-merge into one patient record; both files attached.
20. **Confirm Post-Op Notes with empty editor** — block with inline error "Notes cannot be empty before generating discharge materials."
21. **Generation failure after Confirm** — three-button state does NOT appear; inline error shown above the editor with "Retry" button (does not require re-confirming).

---

## 12. Build order

1. **`backend/eligibility/` skeleton** — empty modules + `routers/eligibility.py` mounted in `main.py`.
2. **Patient model fields** — add `eligibility_status`, `eligibility_check_id`, `relevant_files` to the in-memory PATIENTS row.
3. **Doc upload endpoint** — `POST /api/eligibility-documents` writing to local disk with SHA256.
4. **`parse_x12.py`** + unit tests on synthetic ISA envelopes.
5. **`parse_pdf.py`** + `parse_csv.py`.
6. **`extract.py`** — Anthropic client, tool schema, system prompt; test against 5 hand-labeled fixtures.
7. **`evaluate.py`** with full unit coverage of the 6 checks + edge cases (term==surgery, etc.).
8. **`pipeline.py`** orchestrator with status queue.
9. **SSE endpoint** + override/rerun/finalize endpoints.
10. **`frontend/doctor.html` Add Patient redesign** — Single/Group chooser; rename to Relevant Patient Files; multi-file drop zone.
11. **Determine step UI** — six rows + source disclosure + override modal.
12. **Group flow** — `POST /api/eligibility-batches` + summary screen.
13. **TEAM eligible badge** — `renderTeamBadge()` everywhere a patient name appears.
14. **Post-Op detail redesign** — single Confirm button → inline editor → three-button state.
15. **Pre-Op detail redesign** — three-button initial → inline editor → four-button state.
16. **Audit log writes** from every mutating endpoint.
17. **Edge cases & polish** — work through §11.
18. **Validation set** — 50 documents (5 X12 / 35 PDF / 10 CSV), measure first-pass accuracy ≥95%.

Each step is independently shippable and testable with the existing dev workflow:
```
cd backend && python3 -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```
Open `http://localhost:8000/static/doctor.html` (after wiring an authed session) — no `npm install`, no build step.

---

## 13. Out of scope

- Pre-op intake form authoring (PAM, social determinants, etc.)
- Triage tier assignment
- Telehealth, claim generation, or scheduling
- EHR integration (FHIR/HL7) — uploads remain manual artifacts
- Surgeon/hospital management UI
- Multi-language (English only for v0.1)
- Recurring re-checks (one-and-done; freshness reminders deferred)
- Migration to a real DB / S3 (in-memory + local disk for v0.1; durability is a follow-up)

---

## 14. Glossary

- **MBI** — Medicare Beneficiary Identifier (11-character ID)
- **X12 271** — Healthcare Eligibility Benefit Response (ASC X12N transaction set)
- **MA / Part C** — Medicare Advantage
- **MSP** — Medicare Secondary Payer
- **ESRD** — End-Stage Renal Disease
- **UMWA** — United Mine Workers of America Health Plan
- **AST** — Abstract Syntax Tree (parsed X12 segments)
- **SSE** — Server-Sent Events
- **TEAM** — Transforming Episode Accountability Model

---

*End of PRD v0.2 — TEAM Eligibility Determination + Doctor Portal Workflow Redesign*
