# TEAM Eligibility v0.3 — Scaling fixes + Pre-Op detail UX

| Field | Value |
|---|---|
| Doc version | 0.3 (delta on top of v0.2) |
| Owner | Tej Patel |
| Status | Build-ready |
| Audience | Cursor |
| Depends on | TEAM Eligibility v0.2 (already shipped — see `backend/eligibility/`, `backend/routers/eligibility.py`, `frontend/doctor.html`) |

This PRD is a **delta** — fix only what's listed below. Don't refactor anything else, don't rename modules, don't change the prompt text in `backend/prompts/eligibility.py`. The pipeline shape (parse → segments → per-patient extract → evaluate) is correct; the bugs below prevent it from scaling and the UI from rendering the right buttons.

---

## 0. What's broken (root causes, verified in code)

| # | Symptom | Root cause | File / line |
|---|---|---|---|
| 1 | Pre-op detail shows **all 7 buttons at once** (initial 3 + confirmed 4) | `.actions-row { display: flex }` (specificity 0,1,0) overrides UA `[hidden]{display:none}` (specificity 0,1,0 but author CSS wins). The `hidden` HTML attribute is silently dropped. | `frontend/doctor.html:361` |
| 2 | "Revert to AI extract" button must go | Currently rendered + wired | `frontend/doctor.html:1257`, handler at `2915` |
| 3 | "Switch to Post-Op" rendered twice | Two separate buttons (`switchToPostOpBtn` and `switchToPostOpBtn2`), both visible because of bug #1 | `frontend/doctor.html:1246`, `1273`, handlers `2979-2980` |
| 4 | INELIGIBLE badge has a `title=` tooltip but uses long phrasing | Labels in `_TEAM_FAIL_LABELS` are full sentences; native `title` also has 1-2s browser delay | `backend/main.py:1641-1648` |
| 5 | 50-page PDF with 50 patients silently drops most of them | `extract_patient_segments` is hard-capped at 24,000 chars at two call sites. A 50-page PDF averages ~2,500 chars/page → only the first ~10 pages reach the LLM. | `backend/eligibility/pipeline.py:459`, `extract.py` is fine — the cap is at the call site |
| 6 | Even when all 50 segments ARE detected, the batch takes 12+ minutes | Per-split processing is sequential (`for split in splits: await _process_batch_split(...)`) | `backend/eligibility/pipeline.py:756` |
| 7 | Per-patient eligibility extraction can hit Anthropic rate limits | Each patient fires its own extraction via `asyncio.create_task` with **no concurrency cap** — 50 patients = 50 simultaneous LLM calls | `backend/eligibility/pipeline.py:648` |
| 8 | Long single-patient PDFs are also truncated | Same 24,000-char cap on the per-segment fast path | `backend/eligibility/pipeline.py:459` (single path) |

Fix all 8.

---

## 1. UI fixes (`frontend/doctor.html`)

### 1.1 Make `[hidden]` win against `display: flex` — single-line fix

**Why:** This is the entire reason both action rows render at once. Adding one global rule fixes it everywhere `hidden` is used in the file.

**Edit:** add to the `<style>` block. Place it near the top of the stylesheet (before `.actions-row { display: flex; ... }` is fine — CSS specificity, not order, controls this).

```css
[hidden] { display: none !important; }
```

That's it for bug #1. After this lands, `applyPrepState("initial")` correctly hides `preopActionsConfirmed` because the `hidden` HTML attribute the JS sets on line 2795 actually takes effect.

### 1.2 Remove "Revert to AI extract" button + handler

**Markup** at `frontend/doctor.html:1256-1261` — current:

```html
<div class="inline-confirm-actions">
  <button class="btn" id="preopRevertBtn" type="button">Revert to AI extract</button>
  <button class="btn primary" id="preopConfirmGenerateBtn" type="button">
    Confirm &amp; Generate Preparation Materials
  </button>
</div>
```

Replace with:

```html
<div class="inline-confirm-actions">
  <button class="btn primary" id="preopConfirmGenerateBtn" type="button">
    Confirm &amp; Generate Preparation Materials
  </button>
</div>
```

**Handler** at `frontend/doctor.html:2915` — delete the entire `preopRevertBtn` event listener block (the four lines starting `byId("preopRevertBtn").addEventListener(...)`).

**Apply the same removal to the post-op confirm panel** if it has a parallel `postopRevertBtn` (do a `grep -n "postopRevertBtn" frontend/doctor.html` and remove both markup and handler if present, for consistency — same UX rule).

### 1.3 Pre-op detail buttons spec — confirm post-fix behavior

After 1.1 + 1.2 the state machine in `applyPrepState()` (`doctor.html:2792-2802`) already produces the correct UX. Document the intended behavior in a code comment above that function so it's not regressed:

```js
/**
 * Pre-op detail panel state machine.
 *
 * INVARIANT: exactly one of these three blocks is ever visible:
 *
 *   "initial"   → preopActionsInitial   = [View Intake Form, Switch to Post-Op, Revise Prep Notes]
 *   "reviewing" → preopConfirmPanel     = [textarea + Confirm & Generate]
 *   "confirmed" → preopActionsConfirmed = [View Preparation Materials, View Intake Form,
 *                                           Send Preparation Materials, Switch to Post-Op]
 *
 * The transition is one-way per session:
 *   initial → reviewing → confirmed
 *
 * "View Intake Form" appears in both initial and confirmed states (by design — the doctor
 * can re-open the intake form at any time). It is NOT duplicated within a single visible
 * state. Treat the two button IDs (#viewIntakeFormBtn and #viewIntakeFormBtn2) as the
 * same logical button rendered in two different states.
 *
 * "Switch to Post-Op" likewise appears in both initial and confirmed states. Same logic
 * applies — same handler bound to both IDs at lines 2979-2980. Don't add a third copy.
 *
 * The state is persisted server-side via GET /api/patient/:id/preop-notes (resp.source:
 * "ai" → initial, "confirmed" → confirmed) so refreshing the modal restores the state.
 */
```

No additional code change is needed for buttons — leave the two-row HTML and the dual handlers (lines 2979-2980) intact.

### 1.4 Concise INELIGIBLE hover tooltip

**Two-part change:**

#### Part A — shorten the labels (`backend/main.py:1641-1648`)

Replace the `_TEAM_FAIL_LABELS` dict with:

```python
_TEAM_FAIL_LABELS = {
    "partA_active":     "Part A inactive",
    "partB_active":     "Part B inactive",
    "not_ma":           "Medicare Advantage",
    "medicare_primary": "Medicare not primary",
    "not_esrd_basis":   "ESRD-basis entitlement",
    "not_umwa":         "UMWA Health Plan",
}
```

These match the user's "Not MA" example: 1-3 words, no leading "Reason:" verbiage. The display order in `_TEAM_FAIL_ORDER` (lines 1650-1657) stays as-is.

#### Part B — replace native `title=` with a styled CSS tooltip (`frontend/doctor.html`)

Native `title` has a 1-2s browser delay and inconsistent styling. Replace with a CSS-only tooltip that appears immediately on hover. Keep `title` as accessibility fallback.

**CSS** — add to the `<style>` block (anywhere after the existing `.badge-team-*` rules at line 800-809):

```css
.badge-team-ineligible[data-reason] { position: relative; cursor: help; }
.badge-team-ineligible[data-reason]:hover::after,
.badge-team-ineligible[data-reason]:focus-visible::after {
  content: attr(data-reason);
  position: absolute;
  bottom: calc(100% + 6px);
  left: 50%;
  transform: translateX(-50%);
  white-space: nowrap;
  background: #1f2937;
  color: #fff;
  font-size: 11px;
  font-weight: 500;
  padding: 4px 8px;
  border-radius: 4px;
  pointer-events: none;
  z-index: 1000;
  box-shadow: 0 2px 8px rgba(0,0,0,0.15);
}
.badge-team-ineligible[data-reason]:hover::before,
.badge-team-ineligible[data-reason]:focus-visible::before {
  content: "";
  position: absolute;
  bottom: 100%;
  left: 50%;
  transform: translateX(-50%);
  border: 4px solid transparent;
  border-top-color: #1f2937;
  pointer-events: none;
  z-index: 1000;
}
```

**JS** — update `renderTeamBadge()` at `frontend/doctor.html:2014-2029`:

```js
function renderTeamBadge(patient) {
  const map = {
    ELIGIBLE:        ["badge-team-eligible",   "✓ TEAM ELIGIBLE"],
    INELIGIBLE:      ["badge-team-ineligible", "✗ NOT TEAM ELIGIBLE"],
    BLOCKED_UNKNOWN: ["badge-team-pending",    "⚠ Eligibility review needed"],
    PENDING:         ["badge-team-pending",    "… Eligibility pending"],
  };
  const status = patient?.eligibilityStatus || patient?.eligibility_status;
  const entry = map[status];
  if (!entry) return "";
  const failingRule = patient?.eligibilityFailingRule || patient?.eligibility_failing_rule;
  if (status === "INELIGIBLE" && failingRule) {
    return `<span class="${entry[0]}" data-reason="${esc(failingRule)}" title="${esc(failingRule)}" tabindex="0">${esc(entry[1])}</span>`;
  }
  return `<span class="${entry[0]}" title="${esc(entry[1])}">${esc(entry[1])}</span>`;
}
```

**Result:** hovering "✗ NOT TEAM ELIGIBLE" instantly shows a small dark pill with "Medicare Advantage" / "ESRD-basis entitlement" / "UMWA Health Plan" / "Medicare not primary" / "Part A inactive" / "Part B inactive". Keyboard users get the same tooltip on focus.

---

## 2. Scaling fixes (`backend/eligibility/pipeline.py`)

A 50-page PDF with 50 distinct patient sections must work end-to-end without silent data loss, without hitting Anthropic rate limits, and within ~3-4 minutes wall-clock on a single batch.

### 2.1 Remove the 24,000-char hard cap; chunk-then-merge instead

**Current (broken)** — `pipeline.py:459`:

```python
result = await extract_patient_segments(llm_text[:24000])
```

This silently truncates. A 50-page LEJR/CABG export is ~125,000 chars; only ~10 patients reach the model.

**Replace** the single-call segmentation with a chunk-and-merge helper. Add this function to `pipeline.py` above `_segments_extract_and_fanout`:

```python
# Conservative budget — Sonnet 4.6 handles 200K input tokens, but we want a generous
# safety margin and to keep individual calls well under 60s. ~60,000 chars ≈ 18,000 tokens.
SEGMENT_CHUNK_CHARS = 60_000
# When chunking is needed, overlap each chunk by this many chars so a patient section
# straddling a boundary is fully visible to at least one chunk.
SEGMENT_CHUNK_OVERLAP = 4_000


def _chunk_for_segmentation(text: str) -> List[str]:
    """Break ``text`` into overlapping chunks for the segments LLM call.

    Returns a 1-element list when text fits in a single call.
    """
    if len(text) <= SEGMENT_CHUNK_CHARS:
        return [text]
    chunks: List[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + SEGMENT_CHUNK_CHARS, n)
        chunks.append(text[start:end])
        if end >= n:
            break
        start = end - SEGMENT_CHUNK_OVERLAP
    return chunks


def _dedupe_segments(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Dedupe patients across overlapping chunks.

    Primary key: MBI (case-insensitive, whitespace-stripped). Fallback key when MBI
    is absent: (lastName, firstName, dob). When two records collide, prefer the one
    with HIGH confidence > MEDIUM > LOW; on a tie, prefer the one with a non-null
    sectionAnchor.
    """
    by_key: Dict[str, Dict[str, Any]] = {}
    conf_rank = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
    for seg in segments:
        mbi = (seg.get("mbi") or "").strip().upper()
        if mbi:
            key = f"mbi:{mbi}"
        else:
            ln = (seg.get("lastName") or "").strip().lower()
            fn = (seg.get("firstName") or "").strip().lower()
            dob = (seg.get("dob") or "").strip()
            if not (ln or fn or dob):
                continue  # nothing to identify; drop
            key = f"name:{ln}|{fn}|{dob}"
        prev = by_key.get(key)
        if prev is None:
            by_key[key] = seg
            continue
        # Same patient — keep the better record
        prev_score = (
            conf_rank.get((prev.get("confidence") or "").upper(), 0),
            1 if prev.get("sectionAnchor") else 0,
        )
        new_score = (
            conf_rank.get((seg.get("confidence") or "").upper(), 0),
            1 if seg.get("sectionAnchor") else 0,
        )
        if new_score > prev_score:
            by_key[key] = seg
    return list(by_key.values())


async def _segment_document(llm_text: str) -> List[Dict[str, Any]]:
    """Run patient segmentation across (possibly multiple) chunks of llm_text and
    return a deduped list of patient segments."""
    chunks = _chunk_for_segmentation(llm_text)
    if len(chunks) == 1:
        result = await extract_patient_segments(chunks[0])
        return (result.get("extracted") or {}).get("patients") or []

    # Bounded concurrency for chunked segmentation so we don't fan out 20 calls at once
    sem = asyncio.Semaphore(SEGMENT_CHUNK_CONCURRENCY)

    async def _one(chunk: str) -> List[Dict[str, Any]]:
        async with sem:
            try:
                r = await extract_patient_segments(chunk)
                return (r.get("extracted") or {}).get("patients") or []
            except Exception as e:  # noqa: BLE001
                log.warning("Segment chunk failed (skipping): %s", e)
                return []

    chunk_results = await asyncio.gather(*[_one(c) for c in chunks])
    flat = [seg for sublist in chunk_results for seg in sublist]
    return _dedupe_segments(flat)
```

Add this constant near the top of the module (next to `MBI_RE`):

```python
SEGMENT_CHUNK_CONCURRENCY = 4   # bounded fan-out for segmentation chunks
PATIENT_EXTRACT_CONCURRENCY = 5  # bounded fan-out for per-patient eligibility extraction
```

**Then update the call site** at `pipeline.py:458-460`:

```python
# OLD:
try:
    result = await extract_patient_segments(llm_text[:24000])
    segments: List[Dict[str, Any]] = (result.get("extracted") or {}).get("patients") or []

# NEW:
try:
    segments = await _segment_document(llm_text)
```

### 2.2 Bounded-concurrency split processing

**Current (slow)** — `pipeline.py:756`:

```python
for split in splits:
    await _process_batch_split(split, hs_id, actor, app, rec)
```

Sequential. 50 splits × ~10s LLM segmentation = 8+ minutes.

**Replace** with bounded-concurrent processing:

```python
SPLIT_CONCURRENCY = 4  # constant near top of module

# In run_batch():
sem = asyncio.Semaphore(SPLIT_CONCURRENCY)

async def _bounded(split):
    async with sem:
        await _process_batch_split(split, hs_id, actor, app, rec)

await asyncio.gather(*[_bounded(s) for s in splits])
```

Use `asyncio.gather` (not `create_task` fire-and-forget) so `run_batch` waits for completion before emitting the `done` event.

### 2.3 Cap per-patient eligibility extraction concurrency

**Current** — `pipeline.py:648`:

```python
asyncio.create_task(
    _run_patient_in_batch(check_id, store_dict[pid], [rec_doc], "", surgery_date, batch_rec)
)
```

Unbounded. 50 patients = 50 concurrent Anthropic calls.

**Add** a module-level semaphore and wrap the dispatch:

```python
# Near top of module, after the constants block:
_extract_sem: Optional[asyncio.Semaphore] = None

def _patient_extract_semaphore() -> asyncio.Semaphore:
    """Lazy-initialized so the semaphore binds to the running loop."""
    global _extract_sem
    if _extract_sem is None:
        _extract_sem = asyncio.Semaphore(PATIENT_EXTRACT_CONCURRENCY)
    return _extract_sem
```

**Update** `_run_patient_in_batch` (around `pipeline.py:674`) to acquire the semaphore:

```python
async def _run_patient_in_batch(check_id, patient, docs, notes, surgery_date, batch_rec):
    async with _patient_extract_semaphore():
        await run_pipeline(check_id, patient, docs, notes, surgery_date)
    rec = store.get_check(check_id) or {}
    await _emit(
        batch_rec,
        "patient_done",
        {
            "patientId": rec.get("patient_id"),
            "check_id": check_id,
            "overallVerdict": rec.get("overall_verdict"),
            "status": rec.get("status"),
        },
    )
```

The dispatch at line 648 stays as `asyncio.create_task(...)` — the semaphore inside `_run_patient_in_batch` is what bounds concurrency. Keep the dispatch async so the surrounding loop in `_segments_extract_and_fanout` continues registering patients while extractions run.

### 2.4 Backpressure on the SSE ring buffer for large batches

`store.ring_buffer()` defaults — confirm it's at least 500 entries (one batch with 50 patients emits ~5 events per patient = 250 events plus parent batch events). If `ring_buffer()` is hard-coded to a smaller number, raise it. Don't change the queue size — `asyncio.QueueFull` is already handled with a drop + log on line 47.

Quick check before editing:
```bash
grep -n "ring_buffer\|maxlen" backend/eligibility/store.py
```
If the default is < 500, set it to `512`. Otherwise leave it.

### 2.5 Don't re-truncate inside per-patient extraction

After segmentation produces a per-patient slice, the slice is passed to `extract_eligibility` via `_register_one_segment_and_enqueue` → `run_pipeline`. The eligibility extraction itself does not have a hard char cap (it sends the parsed-doc text via `_build_user_content` in `extract.py:175`), but the slice is `text` from `_slice_by_anchors` — which can be the WHOLE document text when an anchor isn't found (`pipeline.py:421-423`). For a 50-patient document, falling back to "the whole document" for one patient defeats segmentation.

**Fix** the fallback in `_slice_by_anchors` (`pipeline.py:421-423`):

```python
# OLD:
if not found:
    return [(seg, text) for seg in segments]

# NEW:
if not found:
    # No anchors located. Best-effort: split the document into roughly equal slices,
    # one per segment, in document order. Better than handing every patient the
    # entire document (which inflates LLM cost and can cause cross-patient bleed).
    if not segments:
        return []
    span = max(1, len(text) // len(segments))
    return [
        (seg, text[i * span : min((i + 1) * span + SEGMENT_CHUNK_OVERLAP, len(text))])
        for i, seg in enumerate(segments)
    ]
```

Tradeoff: when anchors are completely missing (rare — Sonnet is good at producing them), per-patient slices are approximate but bounded. Better than the current behavior.

---

## 3. Tests

Add to `backend/tests/test_eligibility_pipeline.py`:

### 3.1 Chunked segmentation

```python
async def test_segment_document_single_chunk(monkeypatch):
    """Documents under SEGMENT_CHUNK_CHARS go through one LLM call."""
    calls = []
    async def fake_seg(text):
        calls.append(len(text))
        return {"extracted": {"patients": [{"mbi": "1EG4TE5MK73", "confidence": "HIGH"}]}}
    monkeypatch.setattr("eligibility.pipeline.extract_patient_segments", fake_seg)
    from eligibility.pipeline import _segment_document
    result = await _segment_document("x" * 50_000)
    assert len(calls) == 1
    assert len(result) == 1


async def test_segment_document_chunks_long_doc(monkeypatch):
    """Documents exceeding SEGMENT_CHUNK_CHARS get chunked with overlap."""
    calls = []
    async def fake_seg(text):
        calls.append(len(text))
        return {"extracted": {"patients": []}}
    monkeypatch.setattr("eligibility.pipeline.extract_patient_segments", fake_seg)
    from eligibility.pipeline import _segment_document, SEGMENT_CHUNK_CHARS
    await _segment_document("x" * (SEGMENT_CHUNK_CHARS * 3))
    assert len(calls) >= 3  # at least 3 chunks for 3x text


async def test_segment_document_dedupes_overlapping_patients(monkeypatch):
    """A patient appearing in two overlapping chunks shows up once."""
    chunk_calls = [0]
    async def fake_seg(text):
        chunk_calls[0] += 1
        # Both chunks return the same patient — must dedupe to 1
        return {"extracted": {"patients": [
            {"mbi": "1EG4TE5MK73", "lastName": "Doe", "confidence": "HIGH"},
        ]}}
    monkeypatch.setattr("eligibility.pipeline.extract_patient_segments", fake_seg)
    from eligibility.pipeline import _segment_document, SEGMENT_CHUNK_CHARS
    result = await _segment_document("x" * (SEGMENT_CHUNK_CHARS * 2))
    assert len(result) == 1
```

### 3.2 50-patient stress test (synthetic)

```python
async def test_batch_50_patients_no_truncation(monkeypatch, tmp_path):
    """End-to-end: 50 distinct patient sections in one big synthetic PDF text
    must produce 50 patient records with no silent drops."""
    # Build synthetic doc with 50 patient sections, each ~3000 chars
    sections = []
    for i in range(50):
        sections.append(
            f"=== PATIENT {i+1:02d} ===\n"
            f"Name: Patient {i+1:02d}\n"
            f"MBI: 1EG4TE5MK{i:02d}\n"
            f"DOB: 1950-01-01\n"
            + ("Lorem ipsum " * 200) + "\n"
        )
    big_text = "\n".join(sections)
    assert len(big_text) > 100_000  # bigger than the old 24k cap

    # Stub the segments extractor to return one patient per section
    async def fake_seg(text):
        # naive: count "=== PATIENT" markers in this chunk
        import re
        ids = re.findall(r"=== PATIENT (\d+) ===", text)
        return {"extracted": {"patients": [
            {
                "firstName": f"Patient",
                "lastName": str(int(i)),
                "mbi": f"1EG4TE5MK{int(i)-1:02d}",
                "sectionAnchor": f"=== PATIENT {i} ===",
                "confidence": "HIGH",
            } for i in ids
        ]}}
    monkeypatch.setattr("eligibility.pipeline.extract_patient_segments", fake_seg)

    from eligibility.pipeline import _segment_document
    result = await _segment_document(big_text)
    assert len(result) == 50
```

### 3.3 Bounded split concurrency

Add an assertion that the split semaphore caps concurrent segments calls at `SPLIT_CONCURRENCY` (use a counter incremented inside the stub).

### 3.4 UI smoke test (manual checklist — add to `docs/`)

After 1.1 lands, verify:
- Open a pre-op patient before any prep notes are confirmed → exactly 3 buttons render.
- Click Revise Prep Notes → editor visible, 3 buttons hidden, no "Revert to AI extract" button.
- Click Confirm & Generate → spinner, then exactly 4 buttons (View Preparation Materials, View Intake Form, Send Preparation Materials, Switch to Post-Op).
- Refresh modal → returns to confirmed state (4 buttons).
- Hover ✗ NOT TEAM ELIGIBLE → small dark tooltip appears immediately with concise reason ("Medicare Advantage", "ESRD-basis entitlement", etc.).

---

## 4. Acceptance criteria

### UI
- [ ] AC-U1 — `[hidden]{display:none !important}` rule present in `frontend/doctor.html`.
- [ ] AC-U2 — `preopRevertBtn` button + handler removed (also `postopRevertBtn` if it exists).
- [ ] AC-U3 — Pre-op detail in initial state shows ONLY: View Intake Form, Switch to Post-Op, Revise Prep Notes.
- [ ] AC-U4 — Pre-op detail in confirmed state shows ONLY: View Preparation Materials, View Intake Form, Send Preparation Materials, Switch to Post-Op.
- [ ] AC-U5 — Pre-op detail in reviewing state shows the textarea + Confirm & Generate (no Revert).
- [ ] AC-U6 — `_TEAM_FAIL_LABELS` updated to concise 1-3 word labels.
- [ ] AC-U7 — Hovering `.badge-team-ineligible` displays a custom CSS tooltip immediately (no native browser delay) with the failing-rule label.
- [ ] AC-U8 — Tooltip is keyboard-focusable (`tabindex="0"`, appears on `:focus-visible`).

### Scaling
- [ ] AC-S1 — `_segment_document(text)` with `len(text) > SEGMENT_CHUNK_CHARS` issues multiple LLM calls and dedupes results by MBI.
- [ ] AC-S2 — A synthetic 50-patient document (>100,000 chars) yields 50 patient records (no silent drops).
- [ ] AC-S3 — `run_batch` processes splits concurrently, capped at `SPLIT_CONCURRENCY = 4`.
- [ ] AC-S4 — Per-patient eligibility extraction is capped at `PATIENT_EXTRACT_CONCURRENCY = 5` via `_patient_extract_semaphore()`.
- [ ] AC-S5 — `_slice_by_anchors` fallback when anchors are missing splits the doc into per-segment slices (no "give every patient the entire document" behavior).
- [ ] AC-S6 — Existing tests in `backend/tests/test_eligibility_*.py` still pass.
- [ ] AC-S7 — A 50-patient synthetic batch finishes within ~3-4 minutes locally (rough — depends on Anthropic latency; document the observed wall-clock in the PR description).

---

## 5. Out of scope (do NOT do)

- Don't re-architect the SSE layer.
- Don't change the prompts in `backend/prompts/eligibility.py`.
- Don't introduce a real DB — in-memory `store.py` stays.
- Don't add new dependencies. Everything above uses `asyncio` from stdlib.
- Don't rename modules.
- Don't add wound-photo, surveys, intake, triage, or any non-eligibility feature.

---

## 6. PR description template

```
## TEAM Eligibility v0.3 — scaling + pre-op detail UX

### Fixes
- 50-patient PDFs no longer silently truncate (chunked segmentation + dedup).
- Group-batch processing now bounded-concurrent (4 splits, 5 per-patient extracts).
- Pre-op detail buttons render correctly per state machine ([hidden] CSS fix).
- "Revert to AI extract" removed.
- Concise hover tooltip on ✗ NOT TEAM ELIGIBLE (e.g., "Medicare Advantage").

### Verified
- Synthetic 50-patient batch: 50 records detected, no drops (test_batch_50_patients_no_truncation).
- Wall-clock on 50-patient batch: <observed> seconds (was: 12+ min sequential / silent truncation).
- Existing eligibility tests pass.
- Manual: UI smoke test from §4 of the v0.3 PRD all green.
```

*End of v0.3 delta PRD.*
