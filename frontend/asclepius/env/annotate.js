/* Asclepius ENV · Clinical RL Environment — trajectory annotation surface (PRD §7).
 *
 * A new *content type* inside the same evaluator portal design system (§7.6), not
 * a new app. Reuses the console tokens verbatim; the accent semantics carry the
 * trajectory (orange = model output · green = physician-authored · pink =
 * critical/unsafe · lime = current step). Progressive disclosure: the failure-mode
 * chips, counterfactual box, and reward-ratification control appear only where a
 * step is marked wrong. Anchoring guard: the auto-reward stays hidden until the
 * physician has entered step labels.
 *
 * Persists to env_runs.physician_annotation via POST /environments/{task_id}/annotate.
 */
(function () {
  "use strict";
  const TOKEN_KEY = "asclepius_token";
  const API = "/api/asclepius/environments";
  const FAILURE_MODES = [
    "anchoring", "premature_closure", "right_answer_wrong_reason", "context_neglect",
    "overtreatment", "guideline_recency_or_sequencing", "hallucinated_finding",
    "miscalibrated_confidence", "unsafe_recommendation", "other",
  ];
  const ACTION_JUDGMENTS = ["right_action_right_time", "unnecessary", "harmful", "better_action_existed"];
  const root = document.getElementById("envRoot");

  function token() { return localStorage.getItem(TOKEN_KEY) || ""; }
  function headers() { return { "Content-Type": "application/json", Authorization: "Bearer " + token() }; }
  function qp(name) { return new URLSearchParams(location.search).get(name); }

  async function api(path, opts) {
    const res = await fetch(API + path, Object.assign({ headers: headers() }, opts || {}));
    if (!res.ok) throw new Error((await res.text()) || res.status);
    return res.json();
  }

  // ── state for the current trajectory being annotated ──────────────────────
  let TASK = null;         // the annotation-task view (run + trajectory + case_context)
  const ANN = {
    step_labels: {},          // step# -> {label, action_judgment}
    failure_by_step: {},      // step# -> [failure modes]
    counterfactual_by_step: {}, // step# -> text (tied to the step, not a single field)
    first_error_step: null,
    missed_actions: [],
    failure_tags: [],
    // null = the physician has NOT ratified this axis yet (never fabricate a
    // correct/safe verdict the doctor didn't enter — it ships to buyers).
    end_state_ratified: { correct: null, safe: null, note: "" },
    _endTouched: false,
    reward_ratified: { value: null },
    trajectory_preference: { chosen: null, why: "" },
    kappa_subset: false,
  };

  async function boot() {
    const runId = qp("run_id");
    if (runId) {
      // open a SPECIFIC run (works even if already annotated) — never silently
      // switch to a different trajectory.
      const r = await api("/runs/" + encodeURIComponent(runId));
      TASK = r.task;
    } else {
      // pull the next unannotated trajectory from the ENV queue
      const q = await api("/annotation-queue?portal_version=env");
      if (!q.queue || !q.queue.length) { renderEmpty(); return; }
      TASK = q.queue[0];
    }
    if (!TASK) { renderEmpty(); return; }
    render();
  }

  function renderEmpty() {
    root.innerHTML = '<div class="env-empty">No trajectories awaiting annotation. ' +
      'Run a rollout, then refresh.</div>';
  }

  // ── rendering ─────────────────────────────────────────────────────────────
  function render() {
    const steps = TASK.trajectory || [];
    root.innerHTML =
      '<div class="env-layout">' +
        '<aside class="env-context">' + contextPanel() + "</aside>" +
        '<section id="envTraj">' +
          '<div class="c-card" style="margin-bottom:16px">' +
            '<div class="env-chrome">Case prompt</div>' +
            "<div>" + esc(TASK.prompt || "") + "</div>" +
          "</div>" +
          steps.map((s, i) => stepCard(s, i)).join("") +
          finalPanel() +
        "</section>" +
      "</div>";
    // open the first step, mark it current
    openStep(0);
    bindGlobal();
    updateProgress();
  }

  function contextPanel() {
    const c = TASK.case_context || {};
    const sec = (title, body) => body
      ? '<details class="env-ctx-sec"><summary>' + title + "</summary>" + body + "</details>" : "";
    const demo = c.demographics || {};
    return (
      '<div class="c-card"><h3>Case context</h3>' +
      '<div class="env-kv"><b>' + esc(demo.age_band || "adult") + " " + esc(demo.sex || "") + "</b></div>" +
      '<div class="env-kv" style="margin-top:6px">' +
        (c.problem_list || []).map((p) => "• " + esc(p.condition || "")).join("<br>") + "</div>" +
      sec("Medications", '<pre class="env-mono">' + esc(JSON.stringify(c.medications || [], null, 1)) + "</pre>") +
      sec("Labs", '<pre class="env-mono">' + esc(JSON.stringify(c.lab_panels || [], null, 1)) + "</pre>") +
      sec("Notes", '<pre class="env-mono">' + esc((c.notes || []).map((n) => (n.note_type || "") + ": " + (n.text || "")).join("\n\n")) + "</pre>") +
      sec("Studies", '<pre class="env-mono">' + esc(JSON.stringify(c.studies || [], null, 1)) + "</pre>") +
      "</div>"
    );
  }

  function stepAccent(s) {
    // orange = model output (thought/tool_call/final_output are the agent's behavior)
    return "model";
  }

  function stepCard(s, i) {
    const num = s.step;
    const label = (ANN.step_labels[num] || {}).label;
    const chip = label
      ? '<span class="env-status-chip ' + label + '">' + statusGlyph(label) + " " + label + "</span>"
      : '<span class="env-status-chip">review</span>';
    const title = stepSummary(s);
    return (
      '<div class="env-step" data-idx="' + i + '" data-step="' + num + '">' +
        '<div class="env-step-head" onclick="ENV.open(' + i + ')">' +
          '<span class="env-dot ' + stepAccent(s) + '"></span>' +
          '<span class="env-chrome env-steptype">' + esc(s.type) + " #" + num + "</span>" +
          '<span class="env-step-title">' + esc(title) + "</span>" +
          chip +
        "</div>" +
        '<div class="env-step-body">' +
          '<div class="env-model-out"><span class="env-chrome">Agent</span><br>' + esc(fullStepText(s)) + "</div>" +
          labelControls(s, num) +
          '<div class="env-reveal" id="reveal-' + num + '">' + wrongControls(s, num) + "</div>" +
        "</div>" +
      "</div>"
    );
  }

  function labelControls(s, num) {
    const cur = (ANN.step_labels[num] || {});
    const btn = (v) => '<button class="env-btn ' + (cur.label === v ? "sel-" + v : "") +
      '" data-role="label" data-val="' + v + '" onclick="ENV.label(' + num + ",'" + v + "')\">" + statusGlyph(v) + " " + v + "</button>";
    let out = '<div class="env-label-row" data-labelrow="' + num + '">' + btn("correct") + btn("suboptimal") + btn("wrong") + "</div>";
    if (s.type === "tool_call") {
      out += '<div class="env-field"><label>Action judgment ' + info("Judge the action itself, not just the reasoning") + "</label>" +
        '<div class="env-label-row" data-judgerow="' + num + '">' + ACTION_JUDGMENTS.map((j) =>
          '<button class="env-btn green ' + (cur.action_judgment === j ? "on" : "") +
          '" data-role="judge" data-val="' + j + '" onclick="ENV.judge(' + num + ",'" + j + "')\">" + j.replace(/_/g, " ") + "</button>").join("") + "</div></div>";
    }
    return out;
  }

  // Progressive disclosure (§7.6): failure chips + counterfactual + reward ratify
  // appear only when a step is marked wrong. Every input's value is re-hydrated
  // from ANN so nothing the doctor typed is ever lost visually.
  function wrongControls(s, num) {
    const chips = FAILURE_MODES.map((m) =>
      '<button class="env-btn ' + ((ANN.failure_by_step[num] || []).includes(m) ? "sel-wrong" : "") +
      '" data-role="fmode" data-val="' + m + '" onclick="ENV.fmode(' + num + ",'" + m + "')\">" + m.replace(/_/g, " ") + "</button>").join("");
    return (
      '<div class="env-green-affordance">' +
      '<div class="env-field"><label>Failure mode ' + info("How did the agent fail here?") + "</label>" +
        '<div class="env-label-row" data-fmoderow="' + num + '">' + chips + "</div></div>" +
      '<div class="env-field"><label>Counterfactual — what should the agent have done instead? ' +
        info("The single most valuable token in the record (PRD §7.1.2)") + "</label>" +
        '<textarea rows="2" oninput="ENV.counterfactual(' + num + ', this.value)" ' +
        'placeholder="The correct next action / reasoning at this step…">' + esc(ANN.counterfactual_by_step[num] || "") + "</textarea></div>" +
      "</div>"
    );
  }

  function finalPanel() {
    const twoFrontier = !!TASK.ab_source && TASK.ab_source === "two_frontier";
    return (
      '<div class="c-card env-final">' +
        '<h3>End-state ratification ' + info("Confirm or override the final diagnosis/plan (PRD §7.1.5)") + "</h3>" +
        '<div class="env-label-row">' +
          '<button class="env-btn green" id="es-correct" onclick="ENV.endState(\'correct\')">Final answer correct?</button>' +
          '<button class="env-btn green" id="es-safe" onclick="ENV.endState(\'safe\')">Safe?</button>' +
        "</div>" +
        '<div class="env-field"><label>Missed decisive actions ' + info("Actions the agent should have taken and did not") + "</label>" +
          '<input type="text" id="missed" placeholder="comma-separated, e.g. get_notes, order urine studies" oninput="ENV.missed(this.value)"></div>' +

        '<div class="env-field"><label>Reward validation ' + info("Confirm or correct the environment auto-reward (PRD §7.1.6)") + "</label>" +
          '<div id="rewardGuard" class="env-anchor-guard">Enter your step labels first — the auto-reward is hidden to prevent anchoring.</div>' +
          '<div id="rewardBox" style="display:none">' +
            'Environment auto-reward: <span class="env-reward-num" id="autoReward">—</span> ' +
            '<div class="env-field"><label>Your ratified reward (0–1)</label>' +
            '<input type="text" id="ratified" placeholder="e.g. 0.8" oninput="ENV.ratify(this.value)"></div>' +
          "</div>" +
        "</div>" +

        (twoFrontier ? twoFrontierPref() : "") +

        '<div class="env-field" style="margin-top:16px">' +
          '<button class="env-submit" id="envSubmit" onclick="ENV.submit()">Submit annotation</button> ' +
          '<span class="env-chrome" id="envSaveMsg"></span>' +
        "</div>" +
      "</div>"
    );
  }

  function twoFrontierPref() {
    const sib = TASK.sibling;
    // The blinded "other agent" (B) trajectory, collapsed — so the A/B preference
    // is an INFORMED comparison, not a blind guess. "A" = the trajectory above.
    const sibBlock = sib
      ? ('<details style="margin:8px 0"><summary class="env-chrome">Show Agent B trajectory (blinded)</summary>' +
         '<div style="margin-top:8px">' +
           (sib.trajectory || []).map((s) =>
             '<div class="env-model-out"><span class="env-chrome">B · ' + esc(s.type) + " #" + s.step +
             '</span><br>' + esc(fullStepText(s)) + "</div>").join("") +
         "</div></details>")
      : '<div class="env-anchor-guard">Sibling trajectory not available.</div>';
    return (
      '<div class="env-field"><label>Trajectory preference (blinded) ' +
        info("Two agents ran the same environment — Agent A is the trajectory above; compare Agent B, then pick the better one (DPO signal, §7.1.7)") + "</label>" +
        sibBlock +
        '<div class="env-pref">' +
          '<button class="env-btn green" id="pref-A" onclick="ENV.pref(\'A\')">Agent A better</button>' +
          '<button class="env-btn green" id="pref-B" onclick="ENV.pref(\'B\')">Agent B better</button>' +
        "</div>" +
        '<textarea rows="2" style="margin-top:8px" placeholder="Why?" oninput="ENV.prefWhy(this.value)"></textarea>' +
      "</div>"
    );
  }

  // ── interactions ──────────────────────────────────────────────────────────
  function openStep(idx) {
    document.querySelectorAll(".env-step").forEach((el, i) => {
      el.classList.toggle("is-open", i === idx);
      el.classList.toggle("is-current", i === idx);
    });
  }

  const ENV = {
    open: openStep,
    label: function (num, v) {
      ANN.step_labels[num] = Object.assign(ANN.step_labels[num] || {}, { label: v });
      // targeted DOM update — never re-render the whole trajectory (that would
      // wipe every other step's typed counterfactual / toggled control).
      const card = document.querySelector('.env-step[data-step="' + num + '"]');
      if (card) {
        const chip = card.querySelector(".env-status-chip");
        if (chip) { chip.className = "env-status-chip " + v; chip.textContent = statusGlyph(v) + " " + v; }
        const row = card.querySelector('[data-labelrow="' + num + '"]');
        if (row) row.querySelectorAll(".env-btn").forEach((b) => {
          b.className = "env-btn" + (b.getAttribute("data-val") === v ? " sel-" + v : "");
        });
        const rev = document.getElementById("reveal-" + num);
        if (rev) rev.classList.toggle("show", v === "wrong");
      }
      recomputeFirstError();
      revealRewardIfReady();
      updateProgress();
    },
    judge: function (num, j) {
      ANN.step_labels[num] = Object.assign(ANN.step_labels[num] || {}, { action_judgment: j });
      const row = document.querySelector('[data-judgerow="' + num + '"]');
      if (row) row.querySelectorAll(".env-btn").forEach((b) => {
        b.className = "env-btn green" + (b.getAttribute("data-val") === j ? " on" : "");
      });
    },
    fmode: function (num, m) {
      const arr = ANN.failure_by_step[num] || (ANN.failure_by_step[num] = []);
      const at = arr.indexOf(m);
      if (at >= 0) arr.splice(at, 1); else arr.push(m);
      ANN.failure_tags = Array.from(new Set([].concat.apply([], Object.values(ANN.failure_by_step))));
      const row = document.querySelector('[data-fmoderow="' + num + '"]');
      if (row) row.querySelectorAll(".env-btn").forEach((b) => {
        b.className = "env-btn" + (arr.includes(b.getAttribute("data-val")) ? " sel-wrong" : "");
      });
    },
    counterfactual: function (num, text) { ANN.counterfactual_by_step[num] = text; },
    endState: function (which) {
      ANN._endTouched = true;
      ANN.end_state_ratified[which] = ANN.end_state_ratified[which] === true ? false : true;
      const b = document.getElementById("es-" + which);
      if (b) {
        const on = ANN.end_state_ratified[which] === true;
        b.classList.toggle("on", on);
        b.textContent = (which === "correct"
          ? (on ? "✓ Final answer correct" : "✗ Final answer incorrect")
          : (on ? "✓ Safe" : "⚠ Unsafe"));
      }
    },
    missed: function (v) { ANN.missed_actions = v.split(",").map((x) => x.trim()).filter(Boolean); },
    ratify: function (v) { const n = parseFloat(v); ANN.reward_ratified.value = isNaN(n) ? null : n; },
    pref: function (c) {
      ANN.trajectory_preference.chosen = c;
      const a = document.getElementById("pref-A"), b = document.getElementById("pref-B");
      if (a) a.classList.toggle("on", c === "A");
      if (b) b.classList.toggle("on", c === "B");
    },
    prefWhy: function (v) { ANN.trajectory_preference.why = v; },
    submit: submit,
  };
  window.ENV = ENV;

  function recomputeFirstError() {
    const wrong = Object.keys(ANN.step_labels)
      .filter((k) => ANN.step_labels[k].label === "wrong").map(Number).sort((a, b) => a - b);
    ANN.first_error_step = wrong.length ? wrong[0] : null;
  }

  function revealRewardIfReady() {
    // anchoring guard: show the auto-reward only after step labels are entered.
    if (Object.keys(ANN.step_labels).length >= 1) {
      const g = document.getElementById("rewardGuard"), b = document.getElementById("rewardBox"),
        a = document.getElementById("autoReward");
      if (g) g.style.display = "none";
      if (b) b.style.display = "block";
      if (a && TASK.auto_reward != null) a.textContent = TASK.auto_reward;
    }
  }

  function updateProgress() {
    const total = (TASK.trajectory || []).filter((s) => s.type !== "observation").length;
    const done = Object.keys(ANN.step_labels).length;
    const pct = total ? Math.round((done / total) * 100) : 0;
    const bar = document.getElementById("envBar"), txt = document.getElementById("envProgressText");
    if (bar) bar.style.width = pct + "%";
    if (txt) txt.textContent = done + " / " + total;
  }

  async function submit() {
    recomputeFirstError();
    // the counterfactual that matters is the one tied to the FIRST error step.
    const cf = ANN.first_error_step != null
      ? (ANN.counterfactual_by_step[ANN.first_error_step] || "")
      : (Object.values(ANN.counterfactual_by_step)[0] || "");
    const annotation = {
      step_labels: Object.keys(ANN.step_labels).map((k) => Object.assign({ step: Number(k) }, ANN.step_labels[k])),
      first_error_step: ANN.first_error_step,
      counterfactual_text: cf,
      missed_actions: ANN.missed_actions,
      failure_tags: ANN.failure_tags,
      reward_ratified: ANN.reward_ratified.value != null ? { value: ANN.reward_ratified.value } : undefined,
      trajectory_preference: ANN.trajectory_preference.chosen ? ANN.trajectory_preference : undefined,
      kappa_subset: ANN.kappa_subset,
    };
    // only ship end-state if the physician actually ratified it (no fabricated verdict)
    if (ANN._endTouched) annotation.end_state_ratified = {
      correct: ANN.end_state_ratified.correct, safe: ANN.end_state_ratified.safe,
      note: ANN.end_state_ratified.note || undefined,
    };
    // 'env', not 'v5'. Since the longitudinal relabel, 'v5' is a real portal
    // version meaning "one point of a chart walk" — posting it from here would
    // claim an agentic rollout was a longitudinal submission. The server accepts
    // the old literal for one release (a cached page must not 400 a physician
    // mid-annotation), but it never stores it, and this page never sends it.
    const payload = { run_id: TASK.run_id, portal_version: "env", annotation: annotation };
    const msg = document.getElementById("envSaveMsg");
    try {
      await api("/" + encodeURIComponent(TASK.task_id) + "/annotate", {
        method: "POST", body: JSON.stringify(payload),
      });
      if (msg) { msg.textContent = "Saved ✓ — loading next…"; }
      setTimeout(() => location.reload(), 700);
    } catch (e) {
      if (msg) msg.textContent = "Error: " + e.message;
    }
  }

  function bindGlobal() {
    document.onkeydown = function (e) {
      // keyboard-first (§7.6): 1/2/3 label the current step
      const cur = document.querySelector(".env-step.is-current");
      if (!cur) return;
      const num = Number(cur.getAttribute("data-step"));
      if (e.key === "1") ENV.label(num, "correct");
      else if (e.key === "2") ENV.label(num, "suboptimal");
      else if (e.key === "3") ENV.label(num, "wrong");
      else if (e.key === "j" || e.key === "ArrowDown") stepBy(1);
      else if (e.key === "k" || e.key === "ArrowUp") stepBy(-1);
    };
  }

  function stepBy(d) {
    const cards = Array.from(document.querySelectorAll(".env-step"));
    const cur = cards.findIndex((e) => e.classList.contains("is-current"));
    const next = Math.max(0, Math.min(cards.length - 1, cur + d));
    openStep(next);
    cards[next].scrollIntoView({ block: "center", behavior: prefersReduced() ? "auto" : "smooth" });
  }

  // ── helpers ───────────────────────────────────────────────────────────────
  function statusGlyph(v) { return v === "correct" ? "✓" : v === "suboptimal" ? "⚠" : v === "wrong" ? "✗" : ""; }
  function stepSummary(s) {
    if (s.type === "tool_call") return s.tool + "(" + JSON.stringify(s.input || {}) + ")";
    return (s.content || "").slice(0, 90);
  }
  function fullStepText(s) {
    if (s.type === "tool_call") return s.tool + " " + JSON.stringify(s.input || {});
    return s.content || "";
  }
  function info(t) { return '<span class="info" title="' + esc(t) + '">ⓘ</span>'; }
  function esc(x) {
    return String(x == null ? "" : x).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function prefersReduced() { return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches; }

  boot().catch((e) => { root.innerHTML = '<div class="env-empty">Failed to load: ' + esc(e.message) + "</div>"; });
})();
