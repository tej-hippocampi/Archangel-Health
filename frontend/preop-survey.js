(function () {
  const API = window.location.origin;
  const params = new URLSearchParams(window.location.search);
  const WINDOW = (params.get("window") || "").toLowerCase();
  const PATIENT_ID = (params.get("patient") || "").trim();
  const root = document.getElementById("surveyRoot");

  const SYMPTOM_LABELS = {
    fever_chills_cough: "Fever, chills, or cough",
    chest_pain_sob: "Chest pain or shortness of breath",
    rash_cut_wound: "New rash, cut, or wound",
    bleeding_bruising: "Unusual bleeding or bruising",
  };

  function esc(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function apiJson(path, options = {}) {
    const res = await fetch(`${API}${path}`, options);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `Request failed: ${res.status}`);
    return data;
  }

  async function logOpened() {
    try {
      await apiJson(`/api/patient/${encodeURIComponent(PATIENT_ID)}/events`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ event_type: "preop_survey_opened", payload: { window: WINDOW } }),
      });
    } catch (_e) {
      /* non-fatal */
    }
  }

  function renderQuestion(q, idx) {
    const id = q.id;
    const name = `q_${esc(id)}`;
    const t = esc(q.text || id);
    const type = q.type || "choice";
    const opts = q.options || [];

    if (type === "symptom_screen") {
      const rows = opts
        .map((k) => {
          const lab = SYMPTOM_LABELS[k] || k;
          return `<div class="symptom-row"><span>${esc(lab)}</span>
            <select name="${esc(id)}_${esc(k)}" data-symptom-key="${esc(k)}" class="symptom-select">
              <option value="">—</option>
              <option value="No">No</option>
              <option value="Yes">Yes</option>
            </select></div>`;
        })
        .join("");
      return `<div class="q-block" data-qid="${esc(id)}" data-qtype="symptom_screen">
        <label class="qtext">${t}</label>${rows}</div>`;
    }

    if (type === "datetime") {
      return `<div class="q-block" data-qid="${esc(id)}" data-qtype="datetime">
        <label class="qtext" for="${name}">${t}</label>
        <input type="datetime-local" id="${name}" name="${name}" style="width:100%;max-width:340px;padding:8px;border:1px solid #cbd5e1;border-radius:8px;font-size:14px;" />
      </div>`;
    }

    if (type === "vas_anxiety" || type === "readiness_1_10") {
      const inner = opts
        .map((v) => {
          const val = esc(v);
          return `<label class="pick"><input type="radio" name="${name}" value="${val}" /> ${val}</label>`;
        })
        .join("");
      return `<div class="q-block" data-qid="${esc(id)}" data-qtype="${esc(type)}">
        <span class="qtext">${t}</span>
        <div class="q-opts" style="margin-top:8px;">${inner}</div></div>`;
    }

    const inner = opts
      .map((v) => {
        const val = esc(v);
        return `<label class="pick"><input type="radio" name="${name}" value="${val}" /> ${val}</label>`;
      })
      .join("");
    return `<div class="q-block" data-qid="${esc(id)}" data-qtype="${esc(type)}">
      <span class="qtext">${t}</span>
      <div class="q-opts" style="margin-top:8px;">${inner}</div></div>`;
  }

  function collectAnswers(questions) {
    const answers = [];
    for (const q of questions) {
      const id = q.id;
      const el = root.querySelector(`[data-qid="${id}"]`);
      if (!el) continue;
      const type = el.getAttribute("data-qtype") || "";
      if (type === "symptom_screen") {
        const obj = {};
        el.querySelectorAll("[data-symptom-key]").forEach((sel) => {
          const k = sel.getAttribute("data-symptom-key");
          if (k) obj[k] = sel.value || "";
        });
        const missing = (q.options || []).some((k) => !obj[k]);
        if (missing) return { error: "Please answer every symptom line.", answers: null };
        answers.push({ id, response: JSON.stringify(obj) });
        continue;
      }
      if (type === "datetime") {
        const inp = el.querySelector("input[type='datetime-local']");
        const v = (inp && inp.value) || "";
        if (!v) return { error: "Please complete all questions.", answers: null };
        answers.push({ id, response: v });
        continue;
      }
      const picked = el.querySelector("input:checked");
      if (!picked || !picked.value) return { error: "Please complete all questions.", answers: null };
      answers.push({ id, response: picked.value });
    }
    return { error: null, answers };
  }

  async function init() {
    if (!WINDOW || !["t96", "t48", "t24"].includes(WINDOW) || !PATIENT_ID) {
      root.innerHTML = `<p class="status-msg err">Missing or invalid link. Use ?window=t96|t48|t24&amp;patient=&lt;id&gt;.</p>`;
      return;
    }

    let data;
    try {
      data = await apiJson(
        `/api/preop-survey/questions?window=${encodeURIComponent(WINDOW)}&patient_id=${encodeURIComponent(PATIENT_ID)}`
      );
    } catch (e) {
      root.innerHTML = `<p class="status-msg err">${esc(e.message)}</p>`;
      return;
    }

    const questions = data.questions || [];
    const title =
      WINDOW === "t96" ? "T-96 Readiness (4 days before)" : WINDOW === "t48" ? "T-48 Check-in (2 days before)" : "T-24 Final check (day before)";

    root.innerHTML = `
      <div class="survey-head">
        <h1>${esc(title)}</h1>
        <p>Short check-in for your care team. This is not a substitute for calling your surgeon if something feels urgent.</p>
      </div>
      <form id="preopSurveyForm">
        ${questions.map((q, i) => renderQuestion(q, i)).join("")}
        <div class="survey-actions">
          <button type="submit" class="btn-primary" id="submitBtn">Submit</button>
          <span class="status-msg" id="statusMsg"></span>
        </div>
      </form>
    `;

    await logOpened();

    document.getElementById("preopSurveyForm").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const status = document.getElementById("statusMsg");
      const btn = document.getElementById("submitBtn");
      const { error, answers } = collectAnswers(questions);
      if (error) {
        status.textContent = error;
        status.className = "status-msg err";
        return;
      }
      btn.disabled = true;
      status.textContent = "Submitting…";
      status.className = "status-msg";
      try {
        const res = await apiJson("/api/preop-survey/submit", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ patient_id: PATIENT_ID, window: WINDOW, answers }),
        });
        status.textContent = `Thank you. Your responses were recorded (readiness signal: ${res.tier || "—"}).`;
        status.className = "status-msg ok";
      } catch (e) {
        status.textContent = e.message || "Could not submit.";
        status.className = "status-msg err";
        btn.disabled = false;
      }
    });
  }

  init();
})();
