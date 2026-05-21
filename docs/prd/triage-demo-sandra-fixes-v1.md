# Cursor PRD — Fix Sandra Reyes "Why This Tier" Bullets

> **Model note:** Written for Composer 2 Fast. Exact files, exact values. Do not refactor anything outside what is named below. Apply the same deliberate, data-first care used for the Patricia Alvarez fix.

## Goal

On Sandra Reyes' detail page, clicking her **Tier 3** pill shows the `Initial: Tier 1 → Current: Tier 3` diagram (this is correct, keep it) followed by bullet points. Right now it shows **three** bullets, two of which are wrong:

- ✅ Intra-op event: BP instability requiring vasopressors
- ❌ Tier 3 patient silent across all channels for ≥24 h
- ❌ Any patient silent across all channels for ≥72 h

It must show **exactly these two bullets, and nothing else:**

1. **Intra-op event: BP instability requiring vasopressors**
2. **Patient scored RED on Day 7 survey.**

## Why it's broken (root cause, verified)

The card calls `GET /api/episodes/{id}/triage-explain` (`backend/routers/triage_explain.py`). For TRIAGEDM patients it builds the bullets from `_top_contributing_reasons()` (line ~49), which merges reasons from:

```python
ts.list_preop_retier_events(...)      # none for Sandra
ts.list_postop_retier_events(..., limit=5)   # <-- the problem
ts.list_intraop_reassessments(...)    # gives the BP bullet (correct)
```

then dedupes by code and sorts by weight, taking the top 3.

Sandra is many days into her episode and has been silent since her last seeded check-in. The live post-op re-tier has written **newer `postop_retier_events` whose reasons are LOST_CONTACT contributors** ("…silent across all channels for ≥24 h / ≥72 h"). Those have high weights, so they win the sort and displace the seeded `DAY7_RED_SURVEY` reason. The intra-op BP reason (weight 6) survives. Result: 1 right bullet + 2 lost-contact bullets.

Re-seeding alone will **not** fix this — the live recompute keeps regenerating lost-contact events. The fix must make the demo explanation **curated and deterministic**, immune to live recompute. This mirrors how we curated Patricia's data deliberately.

## The fix (two small, surgical changes)

### Change 1 — Add a curated explain list to Sandra's seed

File: **`backend/triage_demo_seed.py`**.

(a) In `triage_patient_blueprint()`, on the **`triage_sandra_reyes`** row (around line 186), add one key — exactly these two reasons, in this order:

```python
"explain_reasons": [
    {"kind": "SOFT", "code": "INTRAOP_BP_VASOPRESSOR",
     "label": "Intra-op event: BP instability requiring vasopressors", "weight": 6},
    {"kind": "SOFT", "code": "DAY7_RED_SURVEY",
     "label": "Patient scored RED on Day 7 survey.", "weight": 5},
],
```

(b) In `build_patient_blob(...)`, after the blob dict is constructed, propagate it onto the patient blob so the reader can find it:

```python
if row.get("explain_reasons"):
    blob["triage_explain_reasons"] = list(row["explain_reasons"])
```

(c) For timeline consistency, update Sandra's seeded `save_postop_retier_event(...)` in `_seed_sandra_reyes_clinical(...)` (around line 758) so its `reasons=[...]` is the **single** Day-7 reason with the new wording, and **remove the `DAY6_WOUND_PHOTO` reason entirely**:

```python
reasons=[
    {"kind": "SOFT", "code": "DAY7_RED_SURVEY",
     "label": "Patient scored RED on Day 7 survey.", "weight": 5},
],
```

Leave the intra-op reassessment block (`INTRAOP_BP_VASOPRESSOR`) exactly as it is.

### Change 2 — Make `triage-explain` prefer the curated list for TRIAGEDM

File: **`backend/routers/triage_explain.py`**, inside `get_triage_explain(...)`, in the `if is_triage:` branch (around line 105). Before computing `ev_reasons`, return the curated list verbatim when present:

```python
if is_triage:
    curated = patient.get("triage_explain_reasons")
    if curated:
        reasons = list(curated)            # exact, ordered, recompute-proof
    else:
        ev_reasons = _top_contributing_reasons(patient=patient, ts=ts, episode_id=episode_id, limit=3)
        # ...existing fallback logic unchanged...
```

This guarantees Sandra shows exactly her two curated bullets and is immune to live lost-contact recomputes. Patricia and every other patient have no `triage_explain_reasons`, so they fall through to the **unchanged** existing logic — their behavior does not change.

> The frontend already slices `(ex.reasons || []).slice(0, 3)` and renders one `<li>` per reason (`frontend/doctor.html` ~line 1845). With a curated list of two, it renders exactly two bullets. **No frontend change is needed.**

## Do-not-break checklist

- Keep the `Initial: Tier 1 → Current: Tier 3` diagram, the Tier 3 pill, TEAM-eligible badge, episode stats, and "Generate post-op resources" button — all correct.
- Do **not** change the intra-op reassessment reason or Sandra's tier values.
- Do **not** alter `_top_contributing_reasons` itself or any other patient's reasons — only add the `curated` short-circuit ahead of it.
- Do **not** touch CDRSNAI1 or `manan.vyas@cedarssinai.com`.
- All edits are confined to `backend/triage_demo_seed.py` (Sandra's row + `build_patient_blob` propagation + her seeded post-op event) and the single `curated` check in `backend/routers/triage_explain.py`.

## Verify before declaring done

1. Reseed with `DEMO_SEED_STRATEGY=reset` so Sandra's stale post-op events clear.
2. Open Sandra Reyes → click the **Tier 3** pill. Confirm the diagram is unchanged and exactly **two** bullets appear, in this order:
   - Intra-op event: BP instability requiring vasopressors
   - Patient scored RED on Day 7 survey.
3. Confirm no "silent across all channels" bullets appear, even after leaving the page open (live recompute must not change the displayed bullets).
4. Confirm Patricia Alvarez's bullets are unchanged (still her three: T-96 RED, intake BMI/smoker, PAM LOW).
5. Run `python3 -m pytest backend/tests/test_triage_demo.py -q` and report results.

If any step fails, stop and report which one — do not invent alternative reason codes or weights.
