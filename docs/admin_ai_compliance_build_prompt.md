# Admin "AI Security & Compliance" — Grounding Checker + AI Call Log (Cursor Build Prompt)

> Paste into Cursor. This builds **only the visibility layer** on top of work that
> already exists in this branch (the grounding checker and the centralized
> model/prompt-logging are already implemented). It adds: read-only backend
> endpoints, one new query method, and a new **"AI Security & Compliance"** section
> in the admin portal with two tabs — **Grounding Checker** (PASS / REVIEW /
> BLOCK) and **AI Call Log** (model + prompt version/sha + tokens + latency, with a
> provenance filter). **Do not re-create anything listed under "Already exists."**

---

## 0. PRE-FLIGHT — fix two existing bugs first (do this before adding features)

These are in the uncommitted changes and must be resolved or the app won't run /
will silently lose data:

1. **Missing module `backend/pipeline/grounding_gate.py`.** `backend/main.py`
   imports `apply_grounding_to_patient` and `audit_and_gate_script` from it
   (line ~53, used ~3053–3503) and that module is the only caller of
   `team_store.save_grounding_report(...)`. **Verify the file exists.** If it does
   not, create it with:
   - `async def audit_and_gate_script(*, patient_id, structured_data, script, track, team_store, regenerate_fn=None) -> Gate` that:
     calls `check_grounding(structured_data, script, track)`; computes
     `compute_accuracy(report)`; on `verdict == "BLOCK"` and `regenerate_fn` given,
     regenerates **once**, re-checks, and marks `regenerated=True`; persists via
     `team_store.save_grounding_report(patient_id=..., track=..., report=report.model_dump(), accuracy=..., script=script, regenerated=...)`; also
     `team_store.log_event(patient_id=patient_id, event_type="grounding_check", payload={**report.model_dump(), "accuracy": ...})`;
     returns a small dataclass `Gate(script, verdict, synthesize: bool, report_id, summary)` where `synthesize = verdict != "BLOCK"`.
   - `def apply_grounding_to_patient(patient_blob: dict, track: str, gate) -> None`
     that writes `requires_clinician_review`, `grounding_summaries[track]`, and
     trims `grounding_pending_tracks`, matching how `main.py` reads them
     (`row["groundingSummaries"]`, `row["groundingPendingTracks"]`,
     `row["requiresClinicianReview"]` at main.py ~2051–2053).
   - Confirm `save_grounding_report` is actually invoked at runtime (set a
     breakpoint / log once) so the new admin tab has rows to show.

2. **Audit logging must never break a real LLM call.** In
   `backend/ai/llm_client.py`, `_record(...)` and `_log(...)` run outside any
   try/except and `_record` imports `prompt_meta` lazily. A bad `prompt_id` or a
   registry import error would raise *after* the model already responded. Wrap the
   record+log in a guard so failures are swallowed (print to stderr) and the call
   still returns:
   ```python
   try:
       rec = _record(role, cfg, prompt_id, purpose, system, messages, resp, t0)
       _log(rec, patient_id, system, messages, resp)
   except Exception as exc:  # logging must never break the call it audits
       import sys; print(f"[llm_client] audit log failed: {exc!r}", file=sys.stderr)
       rec = {"role": role, "model": cfg["model"], "audit_error": repr(exc)}
   return resp, rec
   ```
   Apply to both `call_llm` and `call_llm_sync`. Also: in
   `backend/prompts/registry.py`, the module-level calls `intraop_system_prompt(None)`
   and `INTAKE_SYSTEM_TEMPLATE.format(...)` execute at import — wrap each entry's
   `content` build in a `try/except` that falls back to an empty string so a
   template change can never make the whole registry unimportable.

3. **(Minor, do while you're here)** In `backend/pipeline/grounding_check.py`,
   pass provenance to the judge call: `call_llm(role="grounding_judge",
   prompt_id="grounding_judge", patient_id=patient_id, ...)` and add a
   `grounding_judge` entry to `PROMPT_REGISTRY` (content = `GROUNDING_JUDGE_PROMPT`,
   version `"1.0.0"`). Thread an optional `patient_id` param through
   `check_grounding(...)`. Replace `datetime.utcnow()` in
   `team_store.grounding_summary_stats` with `datetime.now(timezone.utc)`.

---

## 1. Already exists — DO NOT rebuild (call these, don't duplicate)

Backend (`backend/`):
- `ai/model_config.py` — `MODEL_REGISTRY`, `resolve(role)`, `APP_AI_CONFIG_VERSION`.
- `ai/llm_client.py` — `call_llm` / `call_llm_sync`; writes an `event_logs` row with
  `event_type="llm_call"` and `payload` = `{role, model, ai_config_version,
  prompt:{prompt_id,version,sha}, purpose, latency_ms, usage:{input,output},
  anthropic_request_id, input_sha}`.
- `prompts/registry.py` — `PROMPT_REGISTRY` (every entry has `version`),
  `prompt_sha`, `prompt_meta`.
- `pipeline/grounding_check.py` — `build_required_items`, `check_grounding`,
  `compute_accuracy`, `GroundingReport`.
- `team_store.py` — table `grounding_check_reports`; methods
  `save_grounding_report`, `list_grounding_reports(limit,verdict,track,since)`,
  `get_grounding_report(id)`, `grounding_summary_stats(window_days)`,
  `save_inspector_recall_snapshot(...)`; `log_event(patient_id?optional, event_type, payload)`;
  table `event_logs(patient_id, event_type, occurred_at, payload_json, episode_open_date)`.

Admin shell:
- `frontend/admin.html` — static vanilla HTML/CSS/JS. Sidebar uses `.nav-label`
  group headers + `<button class="nav-item" data-tab="X">`; pages are
  `<div class="tab-page" id="tab-X">`; tab switching toggles `.active`. All data
  is loaded with `fetch("/admin/...", {headers:{Authorization:"Bearer "+token}})`.
  Reuse the existing `.data-section`, `table`, `.stat-card`, `.empty-table`,
  `.refresh-btn` styles and the existing token variable.
- `backend/routers/admin.py` — `router = APIRouter(prefix="/admin")`; every
  endpoint takes `authorization: Optional[str] = Header(None)` and calls
  `_verify_token(authorization)` first.

---

## 2. Backend — add to `backend/team_store.py`

The `llm_call` events have no query path yet. Add two read methods (SQLite, follow
the existing `_conn()` + `json.loads(payload_json)` style):

```python
def list_llm_calls(self, *, limit: int = 200, role: Optional[str] = None,
                   prompt_id: Optional[str] = None, prompt_version: Optional[str] = None,
                   since: Optional[str] = None) -> List[Dict[str, Any]]:
    """Recent llm_call events, newest first, flattened for the admin table."""
    clauses = ["event_type = 'llm_call'"]
    params: List[Any] = []
    if since:
        clauses.append("occurred_at >= ?"); params.append(since)
    where = " AND ".join(clauses)
    params.append(limit * 4)  # over-fetch; JSON filters applied in Python
    with self._conn() as conn:
        rows = conn.execute(
            f"SELECT id, patient_id, occurred_at, payload_json FROM event_logs "
            f"WHERE {where} ORDER BY occurred_at DESC LIMIT ?", tuple(params)
        ).fetchall()
    out = []
    for r in rows:
        p = json.loads(r["payload_json"] or "{}")
        pm = p.get("prompt") or {}
        if role and p.get("role") != role: continue
        if prompt_id and pm.get("prompt_id") != prompt_id: continue
        if prompt_version and pm.get("version") != prompt_version: continue
        usage = p.get("usage") or {}
        out.append({
            "id": r["id"], "occurred_at": r["occurred_at"], "patient_id": r["patient_id"],
            "role": p.get("role"), "model": p.get("model"),
            "ai_config_version": p.get("ai_config_version"),
            "prompt_id": pm.get("prompt_id"), "prompt_version": pm.get("version"),
            "prompt_sha": pm.get("sha"), "purpose": p.get("purpose"),
            "latency_ms": p.get("latency_ms"),
            "input_tokens": usage.get("input"), "output_tokens": usage.get("output"),
            "request_id": p.get("anthropic_request_id"),
            "audit_error": p.get("audit_error"),
        })
        if len(out) >= limit: break
    return out

def llm_call_stats(self, *, window_days: int = 30) -> Dict[str, Any]:
    """Per-role/feature cost & latency rollup for the AI Call Log header."""
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime("%Y-%m-%dT%H:%M:%S")
    with self._conn() as conn:
        rows = conn.execute(
            "SELECT payload_json FROM event_logs WHERE event_type='llm_call' AND occurred_at >= ?",
            (cutoff,)).fetchall()
    by_role: Dict[str, Dict[str, Any]] = {}
    total_in = total_out = total_calls = 0
    for r in rows:
        p = json.loads(r["payload_json"] or "{}")
        role = p.get("role") or "unknown"; u = p.get("usage") or {}
        b = by_role.setdefault(role, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "latency_ms_sum": 0})
        b["calls"] += 1
        b["input_tokens"] += u.get("input") or 0
        b["output_tokens"] += u.get("output") or 0
        b["latency_ms_sum"] += p.get("latency_ms") or 0
        total_calls += 1; total_in += u.get("input") or 0; total_out += u.get("output") or 0
    for b in by_role.values():
        b["avg_latency_ms"] = round(b["latency_ms_sum"] / b["calls"]) if b["calls"] else 0
        b.pop("latency_ms_sum")
    return {"window_days": window_days, "total_calls": total_calls,
            "total_input_tokens": total_in, "total_output_tokens": total_out,
            "by_role": by_role,
            "models_in_use": sorted({json.loads(r["payload_json"] or "{}").get("model") for r in rows} - {None})}
```

> Optional cost: if you have per-model $/token, add an estimated-cost field in
> `by_role`. Leave a `# TODO pricing` if you don't — don't hardcode wrong numbers.

---

## 3. Backend — add endpoints to `backend/routers/admin.py`

Mirror the existing style exactly (`authorization: Optional[str] = Header(None)` →
`_verify_token(authorization)`; reach the store via the app singleton the other
endpoints use — `request.app.state.team_store`). Add:

```python
# ── AI Security & Compliance: Grounding ──────────────────────────────
@router.get("/grounding/stats")            # -> team_store.grounding_summary_stats(window_days)
@router.get("/grounding/reports")          # query: verdict, track, since, limit -> list_grounding_reports(...)
@router.get("/grounding/reports/{report_id}")  # -> get_grounding_report(id) (full coverage[]/faithfulness[])
@router.get("/grounding/inspector-recall") # -> latest save_inspector_recall_snapshot row (or {} if none)

# ── AI Security & Compliance: AI Call Log ────────────────────────────
@router.get("/ai-calls/stats")             # -> team_store.llm_call_stats(window_days)
@router.get("/ai-calls")                   # query: role, prompt_id, prompt_version, since, limit -> list_llm_calls(...)
@router.get("/ai-calls/prompts")           # -> from PROMPT_REGISTRY: [{prompt_id, label, version, sha}] for the filter dropdown
```

`/ai-calls/prompts` builds the provenance filter options:
```python
from prompts.registry import PROMPT_REGISTRY, prompt_meta
return [{"prompt_id": pid, "label": e.get("label", pid), **prompt_meta(pid)}
        for pid, e in PROMPT_REGISTRY.items()]
```
All endpoints return JSON; no new auth — they inherit `_verify_token`.

---

## 4. Frontend — add the "AI Security & Compliance" section to `frontend/admin.html`

### 4a. Sidebar nav (after the existing "Triage" group)
```html
<div class="nav-label">AI Security &amp; Compliance</div>
<button class="nav-item" data-tab="grounding"><span class="icon">🛡️</span> Grounding Checker</button>
<button class="nav-item" data-tab="ai-call-log"><span class="icon">🧾</span> AI Call Log</button>
```
These work automatically with the existing `data-tab` switcher — no JS wiring
needed for navigation, just add the two `tab-page` divs and the loaders below.

### 4b. Tab page: Grounding Checker (`<div class="tab-page" id="tab-grounding">`)
- **Page header** "Grounding Checker" + subtitle "Every generated patient script,
  audited for omissions and fabrications before it ships." + `↺ Refresh`.
- **Stat cards** (reuse `.stat-card`) from `/admin/grounding/stats`: Total audited,
  **PASS** (green), **REVIEW** (amber), **BLOCK** (red), Block rate %, Avg coverage
  %, Avg faithfulness %. Add an **Inspector recall** card from
  `/admin/grounding/inspector-recall` with a tooltip: "Catch rate on seeded
  clinical near-misses — this is what makes the numbers above trustworthy."
- **Filter row:** verdict (All/PASS/REVIEW/BLOCK — **default BLOCK first so the
  riskiest surface on top**), track, date range.
- **Reports table** (`.data-section` + `table`) from `/admin/grounding/reports`:
  Time · Patient · Track · **Verdict pill** (color: PASS green / REVIEW amber /
  BLOCK red) · Coverage % · Faithfulness % · Critical failures · Summary. Row click
  → detail.
- **Detail drawer/modal** from `/admin/grounding/reports/{id}`: verdict banner +
  `summary` + `critical_failures` + `model`/`prompt_version`; then **two columns**:
  - *Coverage* — each required item as a row: ✅ COVERED / ⚠️ PARTIAL / ❌ MISSING
    (icon **and** text, not color alone), its severity tag, and the verbatim script
    quote (evidence). This is the "what was included vs omitted" view.
  - *Faithfulness* — each asserted claim: SUPPORTED (green, shows source field) or
    **UNSUPPORTED (red, "fabricated / drifted")**. This is the "what was invented" view.

### 4c. Tab page: AI Call Log (`<div class="tab-page" id="tab-ai-call-log">`)
- **Page header** "AI Call Log" + subtitle "Every Claude call, by feature — model,
  prompt version, tokens, latency. The provenance trail behind every output." +
  `↺ Refresh`.
- **Stat cards** from `/admin/ai-calls/stats`: Total calls (30d), Total input
  tokens, Total output tokens, Models in use (list). Below them, a small
  **per-role/feature table**: Role · Calls · Input tok · Output tok · Avg latency
  (ms) — this is the cost/usage view.
- **Provenance filter row:** Role dropdown; **Prompt dropdown** populated from
  `/admin/ai-calls/prompts` (show `label — version (sha)`); when a prompt is
  chosen, auto-fill a Version field so the operator can answer *"show me every call
  that used prompt X version Y"* — the whole point of the feature. Plus date range.
- **Calls table** from `/admin/ai-calls`: Time · Role/Feature · Model · Prompt id ·
  **Version** · **SHA** (monospace, short) · Input tok · Output tok · Latency (ms) ·
  Request id. Render any `audit_error` row with a small red flag so logging gaps are
  visible. Clicking the SHA filters the table to that prompt+version (quick
  provenance pivot).

### 4d. JS loaders
Add `loadGrounding()`, `loadGroundingReport(id)`, `loadAiCalls()`, plus dropdown
populators, following the existing fetch-with-Bearer pattern already in the file.
Hook them so they fire when their tab becomes active (match how existing tabs
lazy-load, e.g. the Prompt Lab / stats tabs). Keep all styling consistent with the
existing CSS variables and classes — no new framework, no inline color-only status.

---

## 5. Tests (`backend/tests/test_admin_ai_compliance.py`)

Mock auth (`_verify_token`) and seed the store directly:
- Insert two `grounding_check_reports` (one BLOCK, one PASS) → assert
  `/admin/grounding/reports?verdict=BLOCK` returns only the BLOCK; assert
  `/admin/grounding/stats` counts/rates are correct; assert
  `/admin/grounding/reports/{id}` returns full `coverage`/`faithfulness`.
- Insert two `llm_call` events with different `role`/`prompt.version` via
  `log_event` → assert `/admin/ai-calls?role=...` and
  `?prompt_version=...` filter correctly; assert `/admin/ai-calls/stats` rolls up
  tokens per role; assert `/admin/ai-calls/prompts` lists registry prompts with
  `version`+`sha`.
- Assert all new endpoints 401 without a valid token.

---

## Build order
1. §0 pre-flight fixes (grounding_gate.py, audit-log guard, registry import guard).
2. `team_store.list_llm_calls` + `llm_call_stats`.
3. `routers/admin.py` endpoints (grounding + ai-calls).
4. `frontend/admin.html` nav group + two tab pages + JS loaders.
5. `test_admin_ai_compliance.py`; run it + the existing suite. Manually click both
   tabs in the admin portal and confirm rows render and the prompt-version filter
   works.
```
