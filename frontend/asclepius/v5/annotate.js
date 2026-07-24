/* Asclepius V5 — trajectory annotation surface (PRD §7).
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
  const root = document.getElementById("v5Root");

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
    step_labels: {},       // step# -> {label, action_judgment}
    failure_by_step: {},   // step# -> [failure modes]
    first_error_step: null,
    counterfactual_text: "",
    missed_actions: [],
    failure_tags: [],
    end_state_ratified: { correct: false, safe: true, note: "" },
    reward_ratified: { value: null },
    trajectory_preference: { chosen: null, why: "" },
    kappa_subset: false,
  };

  async function boot() {
    let runId = qp("run_id");
    if (!runId) {
      // pull the next unannotated trajectory from the V5 queue
      const q = await api("/annotation-queue?portal_version=v5");
      if (!q.queue || !q.queue.length) { renderEmpty(); return; }
      TASK = q.queue[0];
    } else {
      const q = await api("/annotation-queue?portal_version=v5");
      TASK = (q.queue || []).find((t) => t.run_id === runId) || q.queue[0];
      if (!TASK) { renderEmpty(); return; }
    }
    render();
  }

  function renderEmpty() {
    root.innerHTML = '<div class="v5-empty">No trajectories awaiting annotation. ' +
      'Run a rollout, then refresh.</div>';
  }

  // ── rendering ─────────────────────────────────────────────────────────────
  function render() {
    const steps = TASK.trajectory || [];
    root.innerHTML =
      '<div class="v5-layout">' +
        '<aside class="v5-context">' + contextPanel() + "</aside>" +
        '<section id="v5Traj">' +
          '<div class="c-card" style="margin-bottom:16px">' +
            '<div class="v5-chrome">Case prompt</div>' +
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
      ? '<details class="v5-ctx-sec"><summary>' + title + "</summary>" + body + "</details>" : "";
    const demo = c.demographics || {};
    return (
      '<div class="c-card"><h3>Case context</h3>' +
      '<div class="v5-kv"><b>' + esc(demo.age_band || "adult") + " " + esc(demo.sex || "") + "</b></div>" +
      '<div class="v5-kv" style="margin-top:6px">' +
        (c.problem_list || []).map((p) => "• " + esc(p.condition || "")).join("<br>") + "</div>" +
      sec("Medications", '<pre class="v5-mono">' + esc(JSON.stringify(c.medications || [], null, 1)) + "</pre>") +
      sec("Labs", '<pre class="v5-mono">' + esc(JSON.stringify(c.lab_panels || [], null, 1)) + "</pre>") +
      sec("Notes", '<pre class="v5-mono">' + esc((c.notes || []).map((n) => (n.note_type || "") + ": " + (n.text || "")).join("\n\n")) + "</pre>") +
      sec("Studies", '<pre class="v5-mono">' + esc(JSON.stringify(c.studies || [], null, 1)) + "</pre>") +
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
      ? '<span class="v5-status-chip ' + label + '">' + statusGlyph(label) + " " + label + "</span>"
      : '<span class="v5-status-chip">review</span>';
    const title = stepSummary(s);
    return (
      '<div class="v5-step" data-idx="' + i + '" data-step="' + num + '">' +
        '<div class="v5-step-head" onclick="V5.open(' + i + ')">' +
          '<span class="v5-dot ' + stepAccent(s) + '"></span>' +
          '<span class="v5-chrome v5-steptype">' + esc(s.type) + " #" + num + "</span>" +
          '<span class="v5-step-title">' + esc(title) + "</span>" +
          chip +
        "</div>" +
        '<div class="v5-step-body">' +
          '<div class="v5-model-out"><span class="v5-chrome">Agent</span><br>' + esc(fullStepText(s)) + "</div>" +
          labelControls(s, num) +
          '<div class="v5-reveal" id="reveal-' + num + '">' + wrongControls(s, num) + "</div>" +
        "</div>" +
      "</div>"
    );
  }

  function labelControls(s, num) {
    const cur = (ANN.step_labels[num] || {});
    const btn = (v) => '<button class="v5-btn ' + (cur.label === v ? "sel-" + v : "") +
      '" onclick="V5.label(' + num + ",'" + v + "')\">" + statusGlyph(v) + " " + v + "</button>";
    let out = '<div class="v5-label-row">' + btn("correct") + btn("suboptimal") + btn("wrong") + "</div>";
    if (s.type === "tool_call") {
      out += '<div class="v5-field"><label>Action judgment ' + info("Judge the action itself, not just the reasoning") + "</label>" +
        '<div class="v5-label-row">' + ACTION_JUDGMENTS.map((j) =>
          '<button class="v5-btn green ' + (cur.action_judgment === j ? "on" : "") +
          '" onclick="V5.judge(' + num + ",'" + j + "')\">" + j.replace(/_/g, " ") + "</button>").join("") + "</div></div>";
    }
    return out;
  }

  // Progressive disclosure (§7.6): failure chips + counterfactual + reward ratify
  // appear only when a step is marked wrong.
  function wrongControls(s, num) {
    const chips = FAILURE_MODES.map((m) =>
      '<button class="v5-btn ' + ((ANN.failure_by_step[num] || []).includes(m) ? "sel-wrong" : "") +
      '" onclick="V5.fmode(' + num + ",'" + m + "')\">" + m.replace(/_/g, " ") + "</button>").join("");
    return (
      '<div class="v5-green-affordance">' +
      '<div class="v5-field"><label>Failure mode ' + info("How did the agent fail here?") + "</label>" +
        '<div class="v5-label-row">' + chips + "</div></div>" +
      '<div class="v5-field"><label>Counterfactual — what should the agent have done instead? ' +
        info("The single most valuable token in the record (PRD §7.1.2)") + "</label>" +
        '<textarea rows="2" oninput="V5.setFirstError(' + num + ", this.value)\" " +
        'placeholder="The correct next action / reasoning at this step…"></textarea></div>' +
      "</div>"
    );
  }

  function finalPanel() {
    const twoFrontier = !!TASK.ab_source && TASK.ab_source === "two_frontier";
    return (
      '<div class="c-card v5-final">' +
        '<h3>End-state ratification ' + info("Confirm or override the final diagnosis/plan (PRD §7.1.5)") + "</h3>" +
        '<div class="v5-label-row">' +
          '<button class="v5-btn green" id="es-correct" onclick="V5.endState(\'correct\')">✓ Final answer correct</button>' +
          '<button class="v5-btn green on" id="es-safe" onclick="V5.endState(\'safe\')">✓ Safe</button>' +
        "</div>" +
        '<div class="v5-field"><label>Missed decisive actions ' + info("Actions the agent should have taken and did not") + "</label>" +
          '<input type="text" id="missed" placeholder="comma-separated, e.g. get_notes, order urine studies" oninput="V5.missed(this.value)"></div>' +

        '<div class="v5-field"><label>Reward validation ' + info("Confirm or correct the environment auto-reward (PRD §7.1.6)") + "</label>" +
          '<div id="rewardGuard" class="v5-anchor-guard">Enter your step labels first — the auto-reward is hidden to prevent anchoring.</div>' +
          '<div id="rewardBox" style="display:none">' +
            'Environment auto-reward: <span class="v5-reward-num" id="autoReward">—</span> ' +
            '<div class="v5-field"><label>Your ratified reward (0–1)</label>' +
            '<input type="text" id="ratified" placeholder="e.g. 0.8" oninput="V5.ratify(this.value)"></div>' +
          "</div>" +
        "</div>" +

        (twoFrontier ? twoFrontierPref() : "") +

        '<div class="v5-field" style="margin-top:16px">' +
          '<button class="v5-submit" id="v5Submit" onclick="V5.submit()">Submit annotation</button> ' +
          '<span class="v5-chrome" id="v5SaveMsg"></span>' +
        "</div>" +
      "</div>"
    );
  }

  function twoFrontierPref() {
    return (
      '<div class="v5-field"><label>Trajectory preference (blinded) ' +
        info("Two agents ran the same environment — pick the better one (DPO signal, §7.1.7)") + "</label>" +
        '<div class="v5-pref">' +
          '<button class="v5-btn green" onclick="V5.pref(\'A\')">Agent A better</button>' +
          '<button class="v5-btn green" onclick="V5.pref(\'B\')">Agent B better</button>' +
        "</div>" +
        '<textarea rows="2" style="margin-top:8px" placeholder="Why?" oninput="V5.prefWhy(this.value)"></textarea>' +
      "</div>"
    );
  }

  // ── interactions ──────────────────────────────────────────────────────────
  function openStep(idx) {
    document.querySelectorAll(".v5-step").forEach((el, i) => {
      el.classList.toggle("is-open", i === idx);
      el.classList.toggle("is-current", i === idx);
    });
  }

  const V5 = {
    open: openStep,
    label: function (num, v) {
      ANN.step_labels[num] = Object.assign(ANN.step_labels[num] || {}, { label: v });
      // reveal wrong-controls only for a wrong step (progressive disclosure)
      const rev = document.getElementById("reveal-" + num);
      if (rev) rev.classList.toggle("show", v === "wrong");
      // first error = earliest wrong step
      recomputeFirstError();
      rerenderChips();
      revealRewardIfReady();
      updateProgress();
    },
    judge: function (num, j) {
      ANN.step_labels[num] = Object.assign(ANN.step_labels[num] || {}, { action_judgment: j });
      rerenderChips();
    },
    fmode: function (num, m) {
      const arr = ANN.failure_by_step[num] || (ANN.failure_by_step[num] = []);
      const at = arr.indexOf(m);
      if (at >= 0) arr.splice(at, 1); else arr.push(m);
      ANN.failure_tags = Array.from(new Set([].concat.apply([], Object.values(ANN.failure_by_step))));
      rerenderChips();
    },
    setFirstError: function (num, text) {
      if (ANN.first_error_step === num || ANN.first_error_step === null) ANN.counterfactual_text = text;
      else ANN.counterfactual_text = text; // last-edited counterfactual wins
      ANN.first_error_step = ANN.first_error_step || num;
    },
    endState: function (which) {
      if (which === "correct") ANN.end_state_ratified.correct = !ANN.end_state_ratified.correct;
      if (which === "safe") ANN.end_state_ratified.safe = !ANN.end_state_ratified.safe;
      const b = document.getElementById("es-" + which);
      if (b) b.classList.toggle("on");
    },
    missed: function (v) { ANN.missed_actions = v.split(",").map((x) => x.trim()).filter(Boolean); },
    ratify: function (v) { const n = parseFloat(v); ANN.reward_ratified.value = isNaN(n) ? null : n; },
    pref: function (c) {
      ANN.trajectory_preference.chosen = c;
      document.querySelectorAll(".v5-pref .v5-btn").forEach((b) =>
        b.classList.toggle("on", b.textContent.indexOf("Agent " + c) === 0));
    },
    prefWhy: function (v) { ANN.trajectory_preference.why = v; },
    submit: submit,
  };
  window.V5 = V5;

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

  function rerenderChips() {
    // cheap targeted re-render of status chips + selected buttons
    (TASK.trajectory || []).forEach((s) => {
      const card = document.querySelector('.v5-step[data-step="' + s.step + '"]');
      if (!card) return;
      const lbl = (ANN.step_labels[s.step] || {}).label;
      const chip = card.querySelector(".v5-status-chip");
      if (chip) {
        chip.className = "v5-status-chip " + (lbl || "");
        chip.textContent = lbl ? statusGlyph(lbl) + " " + lbl : "review";
      }
      card.querySelectorAll(".v5-label-row .v5-btn").forEach((btn) => {
        btn.className = "v5-btn"; // reset then re-apply below
      });
    });
    // simplest robust approach: full re-render keeps state in ANN
    const scrollY = window.scrollY;
    const openIdx = Array.from(document.querySelectorAll(".v5-step")).findIndex((e) => e.classList.contains("is-open"));
    render();
    if (openIdx >= 0) openStep(openIdx);
    window.scrollTo(0, scrollY);
  }

  function updateProgress() {
    const total = (TASK.trajectory || []).filter((s) => s.type !== "observation").length;
    const done = Object.keys(ANN.step_labels).length;
    const pct = total ? Math.round((done / total) * 100) : 0;
    const bar = document.getElementById("v5Bar"), txt = document.getElementById("v5ProgressText");
    if (bar) bar.style.width = pct + "%";
    if (txt) txt.textContent = done + " / " + total;
  }

  async function submit() {
    const payload = {
      run_id: TASK.run_id,
      portal_version: "v5",
      annotation: {
        step_labels: Object.keys(ANN.step_labels).map((k) => Object.assign({ step: Number(k) }, ANN.step_labels[k])),
        first_error_step: ANN.first_error_step,
        counterfactual_text: ANN.counterfactual_text,
        missed_actions: ANN.missed_actions,
        failure_tags: ANN.failure_tags,
        end_state_ratified: ANN.end_state_ratified,
        reward_ratified: ANN.reward_ratified.value != null ? { value: ANN.reward_ratified.value } : undefined,
        trajectory_preference: ANN.trajectory_preference.chosen ? ANN.trajectory_preference : undefined,
        kappa_subset: ANN.kappa_subset,
      },
    };
    const msg = document.getElementById("v5SaveMsg");
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
      const cur = document.querySelector(".v5-step.is-current");
      if (!cur) return;
      const num = Number(cur.getAttribute("data-step"));
      if (e.key === "1") V5.label(num, "correct");
      else if (e.key === "2") V5.label(num, "suboptimal");
      else if (e.key === "3") V5.label(num, "wrong");
      else if (e.key === "j" || e.key === "ArrowDown") stepBy(1);
      else if (e.key === "k" || e.key === "ArrowUp") stepBy(-1);
    };
  }

  function stepBy(d) {
    const cards = Array.from(document.querySelectorAll(".v5-step"));
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

  boot().catch((e) => { root.innerHTML = '<div class="v5-empty">Failed to load: ' + esc(e.message) + "</div>"; });
})();
