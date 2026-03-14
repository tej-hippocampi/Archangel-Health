# Prompt Lab — Internal Tool Build Spec

Build an internal prompt testing tool for this FastAPI + vanilla JS codebase (Archangel Health / CareGuide).
Do NOT modify any existing files beyond what is specified below.

---

## OVERVIEW

Build a single-page internal tool at `/internal/prompt-lab` that lets the team:

1. Select any prompt (voice scripts, battlecards, avatar) from a dropdown
2. View the current prompt (read-only) and edit a copy side-by-side
3. Select a sample patient fixture and an ElevenLabs voice ID
4. Run "original" and "custom" outputs in parallel for comparison
5. See a 30-second audio preview + rendered battlecard for each
6. SAVE the edited prompt — writes it directly back to the correct `.py` file in the codebase
7. COMMIT & PUSH the change to GitHub from the tool UI

---

## FILES TO CREATE

### 1. `backend/prompts/registry.py`

A dict called `PROMPT_REGISTRY` mapping `prompt_id` → metadata. Import all existing prompt constants:

```python
from .diagnosis import DIAGNOSIS_VOICE_PROMPT, DIAGNOSIS_BATTLECARD_PROMPT
from .treatment import TREATMENT_VOICE_PROMPT, TREATMENT_BATTLECARD_PROMPT
from .preop import PREOP_VOICE_PROMPT, PREOP_BATTLECARD_PROMPT
from .postop import POSTOP_VOICE_PROMPT, POSTOP_BATTLECARD_PROMPT

PROMPT_REGISTRY = {
    "diagnosis_voice": {
        "label": "Diagnosis — Voice Script",
        "content": DIAGNOSIS_VOICE_PROMPT,
        "file": "backend/prompts/diagnosis.py",
        "variable": "DIAGNOSIS_VOICE_PROMPT",
        "type": "voice",
        "paired_battlecard": "diagnosis_battlecard",
    },
    "diagnosis_battlecard": {
        "label": "Diagnosis — Battlecard",
        "content": DIAGNOSIS_BATTLECARD_PROMPT,
        "file": "backend/prompts/diagnosis.py",
        "variable": "DIAGNOSIS_BATTLECARD_PROMPT",
        "type": "battlecard",
        "paired_voice": "diagnosis_voice",
    },
    "treatment_voice": {
        "label": "Treatment — Voice Script",
        "content": TREATMENT_VOICE_PROMPT,
        "file": "backend/prompts/treatment.py",
        "variable": "TREATMENT_VOICE_PROMPT",
        "type": "voice",
        "paired_battlecard": "treatment_battlecard",
    },
    "treatment_battlecard": {
        "label": "Treatment — Battlecard",
        "content": TREATMENT_BATTLECARD_PROMPT,
        "file": "backend/prompts/treatment.py",
        "variable": "TREATMENT_BATTLECARD_PROMPT",
        "type": "battlecard",
        "paired_voice": "treatment_voice",
    },
    "preop_voice": {
        "label": "Pre-Op — Voice Script",
        "content": PREOP_VOICE_PROMPT,
        "file": "backend/prompts/preop.py",
        "variable": "PREOP_VOICE_PROMPT",
        "type": "voice",
        "paired_battlecard": "preop_battlecard",
    },
    "preop_battlecard": {
        "label": "Pre-Op — Battlecard",
        "content": PREOP_BATTLECARD_PROMPT,
        "file": "backend/prompts/preop.py",
        "variable": "PREOP_BATTLECARD_PROMPT",
        "type": "battlecard",
        "paired_voice": "preop_voice",
    },
    "postop_voice": {
        "label": "Post-Op — Voice Script (Legacy)",
        "content": POSTOP_VOICE_PROMPT,
        "file": "backend/prompts/postop.py",
        "variable": "POSTOP_VOICE_PROMPT",
        "type": "voice",
        "paired_battlecard": "postop_battlecard",
    },
    "postop_battlecard": {
        "label": "Post-Op — Battlecard (Legacy)",
        "content": POSTOP_BATTLECARD_PROMPT,
        "file": "backend/prompts/postop.py",
        "variable": "POSTOP_BATTLECARD_PROMPT",
        "type": "battlecard",
        "paired_voice": "postop_voice",
    },
}
```

---

### 2. `backend/routers/internal.py`

FastAPI router with prefix `/internal`. All routes protected by checking
`Authorization: Bearer {INTERNAL_TOOL_SECRET}` header against the env var.
Return 401 if missing or wrong.

#### Sample Patient Fixtures

Hardcode these three fake patient `structured_data` dicts (same shape as what `ExtractionLayer` produces).
These are entirely fake — no real PHI.

```python
SAMPLE_FIXTURES = {
    "cardiac_post_op": {
        "label": "Cardiac Cath — Post-Op Discharge",
        "procedure": "Cardiac Catheterization",
        "data": {
            "patient_name": "James Harrington",
            "procedure_name": "Cardiac Catheterization with Stent Placement",
            "procedure_date": "2025-03-10",
            "procedure_status": "completed",
            "key_diagnoses": ["Coronary Artery Disease", "Single Vessel Disease — LAD"],
            "medications": [
                {"name": "Aspirin", "dose": "81mg", "frequency": "daily", "route": "oral", "status": "new", "notes": "Do not stop without calling cardiologist"},
                {"name": "Clopidogrel", "dose": "75mg", "frequency": "daily", "route": "oral", "status": "new", "notes": "Critical — dual antiplatelet therapy"},
                {"name": "Atorvastatin", "dose": "40mg", "frequency": "nightly", "route": "oral", "status": "new", "notes": ""},
                {"name": "Metoprolol", "dose": "25mg", "frequency": "twice daily", "route": "oral", "status": "new", "notes": ""}
            ],
            "red_flags": [
                "Chest pain or pressure",
                "Bleeding or large bruise at groin/wrist access site",
                "Leg swelling, redness, or warmth",
                "Shortness of breath at rest",
                "Fever above 101°F"
            ],
            "normal_symptoms": [
                "Small bruise at catheter insertion site",
                "Mild soreness at access site for 2-3 days",
                "Fatigue for 24-48 hours"
            ],
            "pre_op_instructions": "",
            "post_op_instructions": "Keep access site dry for 48 hours. No heavy lifting over 10 lbs for 5 days. No driving for 24 hours. Take all medications as prescribed.",
            "diet_instructions": "Low sodium, heart-healthy diet. Limit saturated fats.",
            "activity_restrictions": "No strenuous activity for 5 days. Short walks encouraged starting day 2.",
            "wound_care": "Keep bandage on for 24 hours. Watch for bleeding, swelling, or warmth.",
            "allergies": ["Penicillin"],
            "follow_up": {"date": "2025-03-17", "provider": "Dr. Patel, Cardiology", "notes": "Bring medication list"},
            "note_type": "discharge_note",
            "missing_critical_data": []
        }
    },
    "ortho_post_op": {
        "label": "Knee Replacement — Post-Op Discharge",
        "procedure": "Total Knee Replacement",
        "data": {
            "patient_name": "Sandra Okafor",
            "procedure_name": "Right Total Knee Arthroplasty",
            "procedure_date": "2025-03-08",
            "procedure_status": "completed",
            "key_diagnoses": ["Severe Osteoarthritis — Right Knee", "Chronic Knee Pain"],
            "medications": [
                {"name": "Oxycodone", "dose": "5mg", "frequency": "every 6 hours as needed", "route": "oral", "status": "new", "notes": "Take with food. Do not drive."},
                {"name": "Ibuprofen", "dose": "600mg", "frequency": "every 8 hours with food", "route": "oral", "status": "new", "notes": "Take scheduled, not just as needed"},
                {"name": "Aspirin", "dose": "325mg", "frequency": "daily", "route": "oral", "status": "new", "notes": "Blood clot prevention"},
                {"name": "Enoxaparin", "dose": "40mg", "frequency": "daily injection", "route": "subcutaneous", "status": "new", "notes": "Nurse will teach injection technique"}
            ],
            "red_flags": [
                "Sudden severe calf pain or swelling (possible blood clot)",
                "Knee dramatically more swollen than day before",
                "Fever above 101.5°F",
                "Wound opening, drainage, or foul smell",
                "Numbness or tingling in foot"
            ],
            "normal_symptoms": [
                "Swelling around knee and lower leg for up to 3 months",
                "Bruising from mid-thigh to ankle",
                "Clicking or clunking sounds in knee",
                "Warmth around knee",
                "Difficulty sleeping due to discomfort"
            ],
            "pre_op_instructions": "",
            "post_op_instructions": "Use walker at all times when walking. Do PT exercises 3x daily. Elevate leg above heart level when resting. Ice 20 min every 2 hours.",
            "diet_instructions": "High protein diet to support healing. Stay well hydrated.",
            "activity_restrictions": "No driving until cleared by surgeon. No kneeling. Weight bearing as tolerated with walker.",
            "wound_care": "Keep incision dry for 5 days. Staples removed at 2-week visit.",
            "allergies": ["Sulfa drugs", "Latex"],
            "follow_up": {"date": "2025-03-22", "provider": "Dr. Kim, Orthopedic Surgery", "notes": "Bring list of current medications. PT referral will be sent."},
            "note_type": "discharge_note",
            "missing_critical_data": []
        }
    },
    "general_pre_op": {
        "label": "Appendectomy — Pre-Op Prep",
        "procedure": "Laparoscopic Appendectomy",
        "data": {
            "patient_name": "Marcus Webb",
            "procedure_name": "Laparoscopic Appendectomy",
            "procedure_date": "2025-03-18",
            "procedure_status": "scheduled",
            "key_diagnoses": ["Acute Appendicitis"],
            "medications": [
                {"name": "Metformin", "dose": "500mg", "frequency": "twice daily", "route": "oral", "status": "hold", "notes": "Stop 48 hours before surgery"},
                {"name": "Lisinopril", "dose": "10mg", "frequency": "daily", "route": "oral", "status": "continue", "notes": "Take morning of surgery with sip of water"}
            ],
            "red_flags": [
                "Worsening abdominal pain before surgery date — call ER",
                "Fever above 100.4°F before surgery",
                "Vomiting that prevents taking medications",
                "Any new symptoms overnight"
            ],
            "normal_symptoms": [
                "Mild abdominal discomfort",
                "Anxiety about surgery"
            ],
            "pre_op_instructions": "Nothing to eat after midnight. Clear liquids until 4 hours before surgery. Shower with antibacterial soap the night before and morning of surgery. Arrive 2 hours before scheduled time.",
            "post_op_instructions": "",
            "diet_instructions": "Clear liquids only after midnight. No solid food.",
            "activity_restrictions": "No strenuous activity day before surgery. Arrange driver — you cannot drive yourself home.",
            "wound_care": "",
            "allergies": ["Penicillin", "NSAIDs"],
            "follow_up": {"date": "2025-03-25", "provider": "Dr. Nguyen, General Surgery", "notes": "Post-op wound check"},
            "note_type": "pre_op_note",
            "missing_critical_data": []
        }
    }
}
```

#### Voice Options

```python
VOICE_OPTIONS = [
    {"id": "l4Coq6695JDX9xtLqXDE", "label": "Default Clinical (Current)"},
    {"id": "21m00Tcm4TlvDq8ikWAM", "label": "Rachel — Calm, female"},
    {"id": "AZnzlk1XvdvUeBnXmlld", "label": "Domi — Strong, female"},
    {"id": "EXAVITQu4vr4xnSDxMaL", "label": "Bella — Soft, female"},
    {"id": "ErXwobaYiN019PkySvjV", "label": "Antoni — Well-rounded, male"},
    {"id": "VR6AewLTigWG4xSOukaG", "label": "Arnold — Crisp, male"},
    {"id": "pNInz6obpgDQGcFmaJgB", "label": "Adam — Deep, male"},
    {"id": "yoZ06aMxZJJ28mfd3POQ", "label": "Sam — Raspy, male"},
]
```

#### Endpoints

**`GET /internal/prompts`**
Returns list of all prompts from `PROMPT_REGISTRY`: `[{ id, label, content, type }]`

**`GET /internal/samples`**
Returns list of fixtures: `[{ key, label, procedure }]`

**`GET /internal/voices`**
Returns `VOICE_OPTIONS` list plus a `{ id: "custom", label: "Custom Voice ID..." }` entry at the end.

**`POST /internal/run`**

Body:
```json
{
  "prompt_id": "diagnosis_voice",
  "system_prompt": "...(current or edited)...",
  "sample_key": "cardiac_post_op",
  "preview_words": 75,
  "voice_id": null
}
```

Logic:
1. Pull `structured_data` from `SAMPLE_FIXTURES[sample_key]["data"]`
2. Format it using `GenerationLayer._format_clinical_input()` — import `GenerationLayer` from `backend.pipeline.generate`
3. Call Claude (`claude-sonnet-4-6`) with the provided `system_prompt`
   - If `type == "voice"`: `max_tokens=1500`
   - If `type == "battlecard"`: `max_tokens=8000`
4. If voice prompt: truncate output to `preview_words` words for audio; keep full text for display
5. Synthesize truncated text via `ElevenLabsClient.synthesize_preview()` using provided `voice_id` (or env default if `None`)
   - Save to `/tmp/audio_preview_{sample_key}_{uuid4().hex[:8]}.mp3`
   - Return URL as `/audio/audio_preview_{sample_key}_{...}.mp3`
6. If voice prompt: also generate a battlecard using the **paired** battlecard prompt from registry + the **full** (not truncated) voice script
7. Return:
```json
{
  "voice_script_full": "...",
  "voice_script_preview": "...(first N words)...",
  "audio_url": "/audio/...",
  "battlecard_html": "...",
  "word_count": 75,
  "estimated_seconds": 30,
  "was_truncated": true
}
```
If `type == "battlecard"`: skip audio entirely, return `battlecard_html` only.

**`PATCH /internal/prompts/{prompt_id}`**

Body: `{ "content": "...new prompt string..." }`

Logic:
1. Look up prompt in `PROMPT_REGISTRY` to get `file` path and `variable` name
2. Read the target `.py` file
3. Locate the triple-quoted string assigned to the variable using regex
   - **IMPORTANT**: Read each prompt file before writing to understand exact formatting
   - Handle both `"""` and `'''` style triple-quoted strings
   - Pattern: match `VARIABLE_NAME = """` through the closing `"""`
4. Replace the string content with the new content
5. Write file back
6. Update `PROMPT_REGISTRY[prompt_id]["content"]` in memory immediately
7. Return `{ "success": true, "prompt_id": "...", "file_written": "backend/prompts/diagnosis.py" }`

**`POST /internal/git/push`**

Body: `{ "prompt_id": "diagnosis_voice", "commit_message": "feat(prompts): update diagnosis voice script" }`

Logic:
1. Run `git add backend/prompts/` via `subprocess.run(capture_output=True, text=True)`
2. Run `git commit -m "{commit_message}"`
3. Run `git push origin HEAD`
4. Capture stdout/stderr for each step
5. Return:
```json
{
  "success": true,
  "output": "...combined stdout from all steps...",
  "error": "...stderr if any..."
}
```
If any step fails, return `success: false` with the error so the UI can display it.
Never use `os.system`. Always use `subprocess.run`.

---

### 3. `frontend/prompt-lab.html`

Standalone HTML page. Do NOT import `app.js` or depend on `window.__PATIENT__`.
Link to `styles.css` for base variables and fonts only.
Add a `<style>` block for all tool-specific layout.

#### Auth Gate

On page load: check `localStorage` for `internal_token`.
If missing, show a centered full-screen overlay with a token input field and "Enter" button.
On submit: store token in `localStorage`, reload.
Send `Authorization: Bearer {token}` on every API call.

#### Page Layout

Two-column layout: fixed left config panel (~380px) + scrollable right results area.

**LEFT PANEL — Config:**

- Header: "Prompt Lab" with a small grey `internal` badge
- **Configure section:**
  - Label + Dropdown: "Prompt" — populated from `GET /internal/prompts`, options are `{ value: id, text: label }`
  - Label + Dropdown: "Sample Patient" — populated from `GET /internal/samples`
  - Label + Dropdown: "Voice" — populated from `GET /internal/voices`
  - If "Custom" voice selected: reveal a text input for custom ElevenLabs voice ID
- **Current Prompt section:**
  - Read-only `<textarea>` showing original prompt content
  - Tall (~300px), grey background (`#f5f5f5`), monospace font, `resize: vertical`
- **Edit Prompt section:**
  - Editable `<textarea>` (same height, white background, monospace font, `resize: vertical`)
  - Character count badge below: "1,234 chars" — updates live on `input` event
- **Button row:**
  - "Reset" (grey secondary style) — restores edit textarea to current prompt value
  - "▶ Run Both" (blue primary style) — fires both run calls in parallel

**RIGHT PANEL — Results:**

Two equal-width columns: "Original" (left) and "Custom" (right).

Each column contains:

1. **Column header** — label ("Original" / "Custom") + spinner (hidden unless loading)

2. **Audio section:**
   - Label: "30-second Preview"
   - Word count badge: `~{estimated_seconds}s · {word_count} words`
   - `<audio controls>` element — hidden until `audio_url` is returned, then set `src` and show
   - If prompt type is `battlecard`: show "No audio for battlecard prompts" in grey italic

3. **Battlecard section:**
   - Label: "Battlecard"
   - `<iframe>` (height: 500px, width: 100%, border: none, `srcdoc` attribute) for rendering `battlecard_html`
   - If prompt type is `battlecard`: show the raw text output in a `<pre>` tag with scroll instead
   - If not yet run: show a grey placeholder "Run to see battlecard"

4. **Voice Script Preview section:**
   - Label: "Script Preview (first 75 words)"
   - `<p>` showing `voice_script_preview`
   - Small "See full script ▼" toggle link — reveals `voice_script_full` in a scrollable `<pre>` below

**SAVE SECTION — full width bar below both columns:**

Only visible after at least one successful "Custom" run.

Contents:
- Text: "Save changes to `{file_path}`?"
- Commit message `<input>` pre-filled with `"feat(prompts): update {prompt_label}"`
- "Save to Codebase" button (orange/amber color — distinct from Run)
  - On click: call `PATCH /internal/prompts/{prompt_id}` with current edit textarea content
  - Show inline spinner during request
  - On success: show green "✓ Saved to `backend/prompts/diagnosis.py`"
  - Reveal "Push to GitHub" button (green)
- "Push to GitHub" button (green):
  - On click: call `POST /internal/git/push`
  - Show a terminal-style `<pre>` box below with live output
  - On success: "✓ Pushed successfully. Verify on GitHub."
  - On failure: show error in red

#### UX Details

- "Run Both" fires TWO parallel `fetch()` calls simultaneously — do not chain them, use `Promise.all()`
- Each column has its own independent loading spinner
- All errors shown inline in a red `<p>` tag — never use `alert()`
- Graceful degradation: if ElevenLabs key not set, audio section shows "ElevenLabs not configured" in grey
- On prompt dropdown change: auto-populate both the read-only and edit textareas with the new prompt content

---

## FILES TO MODIFY

### `backend/integrations/elevenlabs.py`

Add `synthesize_preview` method to the existing `ElevenLabsClient` class:

```python
async def synthesize_preview(
    self,
    script: str,
    patient_id: str,
    max_words: int = 75,
    voice_id: Optional[str] = None
) -> Optional[str]:
    """
    Synthesizes only the first max_words words of a script.
    Used by the internal prompt lab to generate cheap 30-second previews.
    """
    # 1. Strip tone markers (same regex as existing synthesize())
    # 2. Split into words, take first max_words
    # 3. Append "..." if truncated
    # 4. Call ElevenLabs API (same endpoint as existing synthesize())
    # 5. Use voice_id if provided, else fall back to self.voice_id
    # 6. Save to /tmp/audio_preview_{patient_id}.mp3
    # 7. Return "/audio/audio_preview_{patient_id}.mp3" or None if no API key
```

### `backend/main.py`

Add these two additions only — do not change anything else:

```python
# At imports section:
from backend.routers.internal import router as internal_router

# After existing router includes:
app.include_router(internal_router)

# New route to serve the HTML page:
@app.get("/internal/prompt-lab", response_class=HTMLResponse)
async def prompt_lab_page():
    with open("frontend/prompt-lab.html") as f:
        return f.read()
```

---

## ENVIRONMENT VARIABLES

Add to `.env.example`:

```
# Internal Prompt Lab
INTERNAL_TOOL_SECRET=change-me-internal-secret
```

---

## HARD CONSTRAINTS

- **No external JS libraries.** Vanilla JS only — no React, Vue, Alpine, or diff libraries.
- **No database.** All state is in-memory + direct file writes.
- **Do not modify any existing prompt `.py` files** during this build. The registry only reads them.
- **File-write regex must be robust.** Before implementing `PATCH /internal/prompts/{prompt_id}`, read each actual prompt file (`diagnosis.py`, `treatment.py`, etc.) to understand the exact triple-quote format used, then write the regex accordingly.
- **Use `subprocess.run` for git commands.** Never `os.system`.
- **Reuse `GenerationLayer._format_clinical_input()`** for formatting fixture data. Import from `backend.pipeline.generate`.
- **The internal router must validate the `INTERNAL_TOOL_SECRET` token on every request** — no exceptions.
- **Audio files from preview runs save to `/tmp/` with a `preview_` prefix** — never overwrite real patient audio.
