/**
 * Patient-facing post-op recovery tracking (PRD §13).
 *
 * Renders the daily check-in, D-X survey, med-adherence, video-event,
 * and patient-self-flag entry points. The card is hidden until
 * `/api/episodes/{id}/postop` confirms `phase==post_op` and a
 * `discharge_at` is on file. We never display the tier or score values
 * to the patient (PRD §13 invariant).
 */

(function () {
  "use strict";

  // ─── Patient id resolver ───────────────────────────────────────────────
  function resolvePatientId() {
    const m = (location.pathname || "").match(/^\/patient\/([^/?#]+)/);
    if (m && m[1]) return decodeURIComponent(m[1]);
    return null;
  }

  function authHeaders() {
    const t = (window.localStorage && localStorage.getItem("access_token")) || null;
    return t ? { Authorization: "Bearer " + t } : {};
  }

  async function fetchJSON(url, options) {
    const opts = options || {};
    const headers = Object.assign(
      { "Content-Type": "application/json" },
      authHeaders(),
      opts.headers || {}
    );
    const r = await fetch(url, Object.assign({}, opts, { headers }));
    if (!r.ok) {
      let detail = "";
      try { const j = await r.json(); detail = j.detail || JSON.stringify(j); } catch (_e) {}
      throw new Error(`${r.status} ${r.statusText} ${detail}`.trim());
    }
    return r.json();
  }

  // ─── Boot: only show the card when patient is post-op ──────────────────
  let patientId = null;
  let postopState = null;

  async function boot() {
    patientId = resolvePatientId();
    if (!patientId) return;
    try {
      postopState = await fetchJSON(`/api/episodes/${encodeURIComponent(patientId)}/postop`);
    } catch (_e) {
      return;
    }

    const card = document.getElementById("postopCard");
    if (!card) return;
    if (postopState.dischargeAt) {
      card.style.display = "block";
      maybeShowSurveyTile();
    }
    wireTiles();
  }

  function maybeShowSurveyTile() {
    const tile = document.getElementById("postopSurveyTile");
    const titleEl = document.getElementById("postopSurveyTitle");
    if (!tile || !postopState || !postopState.dischargeAt) return;
    const day = episodeDay(postopState.dischargeAt);
    if (day === 7 || day === 14 || day === 30) {
      tile.style.display = "";
      tile.dataset.surveyDay = String(day);
      if (titleEl) titleEl.textContent = `Day ${day} recovery survey`;
      const overlayTitle = document.getElementById("postopSurveyOverlayTitle");
      if (overlayTitle) overlayTitle.textContent = `Day ${day} recovery survey`;
    }
  }

  function episodeDay(dischargeAtIso) {
    try {
      const t = new Date(dischargeAtIso);
      const days = Math.floor((Date.now() - t.getTime()) / (1000 * 60 * 60 * 24)) + 1;
      return Math.max(days, 1);
    } catch (_e) {
      return 1;
    }
  }

  // ─── Tile wiring ───────────────────────────────────────────────────────
  function wireTiles() {
    const checkinTile = document.getElementById("postopCheckinTile");
    if (checkinTile) checkinTile.addEventListener("click", () => openOverlay("checkin"));

    const surveyTile = document.getElementById("postopSurveyTile");
    if (surveyTile) surveyTile.addEventListener("click", () => openSurveyOverlay(parseInt(surveyTile.dataset.surveyDay, 10)));

    const medTile = document.getElementById("postopMedTile");
    if (medTile) medTile.addEventListener("click", () => openOverlay("med"));

    const videosTile = document.getElementById("postopVideosTile");
    if (videosTile) videosTile.addEventListener("click", openVideos);

    const flagTile = document.getElementById("postopSelfFlagTile");
    if (flagTile) flagTile.addEventListener("click", () => openOverlay("self-flag"));

    document.getElementById("postopCheckinClose")?.addEventListener("click", () => closeOverlay("checkin"));
    document.getElementById("postopSurveyClose")?.addEventListener("click", () => closeOverlay("survey"));
    document.getElementById("postopMedClose")?.addEventListener("click", () => closeOverlay("med"));
    document.getElementById("postopSelfFlagClose")?.addEventListener("click", () => closeOverlay("self-flag"));

    wireCheckinForm();
    wireMedAdherence();
    wireSelfFlag();
    document.getElementById("postopSurveySubmit")?.addEventListener("click", submitSurvey);
  }

  function openOverlay(name) {
    const el = document.getElementById(`overlay-postop-${name}`);
    if (el) {
      el.classList.add("postop-overlay-open");
      el.setAttribute("aria-hidden", "false");
    }
  }

  function closeOverlay(name) {
    const el = document.getElementById(`overlay-postop-${name}`);
    if (el) {
      el.classList.remove("postop-overlay-open");
      el.setAttribute("aria-hidden", "true");
    }
  }

  // ─── Daily check-in ────────────────────────────────────────────────────
  function wireCheckinForm() {
    const slider = document.getElementById("checkinPainNrs");
    const sliderVal = document.getElementById("checkinPainNrsValue");
    if (slider && sliderVal) {
      slider.addEventListener("input", () => { sliderVal.textContent = slider.value; });
    }

    document.querySelectorAll('#postopCheckinForm [data-field]').forEach((group) => {
      const isMulti = group.classList.contains("postop-chip-group");
      group.querySelectorAll(isMulti ? ".postop-chip" : ".postop-pill").forEach((btn) => {
        btn.addEventListener("click", () => {
          if (isMulti) {
            btn.classList.toggle("active");
          } else {
            group.querySelectorAll(".postop-pill").forEach((b) => b.classList.remove("active"));
            btn.classList.add("active");
          }
        });
      });
    });

    document.getElementById("postopCheckinForm")?.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const form = ev.target;
      const status = document.getElementById("postopCheckinStatus");
      const submitBtn = document.getElementById("postopCheckinSubmit");
      try {
        const answers = collectCheckinAnswers();
        if (!answers) {
          status.textContent = "Please answer every question before submitting.";
          return;
        }
        if (submitBtn) { submitBtn.disabled = true; }
        status.textContent = "Submitting…";
        await fetchJSON(`/api/episodes/${encodeURIComponent(patientId)}/postop/checkin`, {
          method: "POST",
          body: JSON.stringify({ answers }),
        });
        status.textContent = "Thanks — your check-in was sent to your care team.";
        setTimeout(() => closeOverlay("checkin"), 1100);
      } catch (e) {
        status.textContent = "Something went wrong: " + (e.message || "please try again.");
      } finally {
        if (submitBtn) { submitBtn.disabled = false; }
      }
    });
  }

  function collectCheckinAnswers() {
    const single = (field) => {
      const el = document.querySelector(`[data-field="${field}"] .postop-pill.active`);
      return el ? el.dataset.value : null;
    };
    const multi = (field) => Array.from(
      document.querySelectorAll(`[data-field="${field}"] .postop-chip.active`)
    ).map((b) => b.dataset.value);

    const out = {
      pain_nrs: parseInt(document.getElementById("checkinPainNrs").value, 10),
      pain_trajectory: single("pain_trajectory"),
      fever: single("fever"),
      incision_change: single("incision_change"),
      incision_flags: multi("incision_flags"),
      nausea: single("nausea"),
      eating_drinking: single("eating_drinking"),
      red_flag_symptoms: multi("red_flag_symptoms"),
      walking: single("walking"),
      worry_level: single("worry_level"),
      free_text: (document.getElementById("checkinFreeText")?.value || "") || null,
    };
    const required = [
      "pain_trajectory", "fever", "incision_change",
      "nausea", "eating_drinking", "walking", "worry_level",
    ];
    for (const k of required) {
      if (!out[k]) return null;
    }
    return out;
  }

  // ─── Med adherence ─────────────────────────────────────────────────────
  function wireMedAdherence() {
    document.querySelectorAll('[data-field="med_adherence_response"] .postop-pill').forEach((btn) => {
      btn.addEventListener("click", async () => {
        const status = document.getElementById("postopMedStatus");
        try {
          status.textContent = "Sending…";
          await fetchJSON(`/api/episodes/${encodeURIComponent(patientId)}/postop/med-adherence`, {
            method: "POST",
            body: JSON.stringify({ response: btn.dataset.value }),
          });
          status.textContent = "Thanks!";
          setTimeout(() => closeOverlay("med"), 800);
        } catch (e) {
          status.textContent = "Something went wrong: " + (e.message || "please try again.");
        }
      });
    });
  }

  // ─── Self-flag ─────────────────────────────────────────────────────────
  function wireSelfFlag() {
    document.getElementById("postopSelfFlagSubmit")?.addEventListener("click", async () => {
      const status = document.getElementById("postopSelfFlagStatus");
      const text = document.getElementById("selfFlagFreeText")?.value || "";
      try {
        status.textContent = "Alerting your care team…";
        await fetchJSON(`/api/episodes/${encodeURIComponent(patientId)}/postop/self-flag`, {
          method: "POST",
          body: JSON.stringify({ free_text: text || null }),
        });
        status.textContent = "Care team notified. Thank you.";
        const toast = document.createElement("div");
        toast.textContent = "Care team notified.";
        toast.setAttribute("role", "status");
        Object.assign(toast.style, {
          position: "fixed",
          bottom: "24px",
          left: "50%",
          transform: "translateX(-50%)",
          background: "#0f766e",
          color: "#fff",
          padding: "10px 18px",
          borderRadius: "10px",
          fontWeight: "600",
          fontSize: "14px",
          zIndex: "9999",
          boxShadow: "0 4px 20px rgba(0,0,0,.15)",
        });
        document.body.appendChild(toast);
        setTimeout(() => toast.remove(), 2200);
        setTimeout(() => closeOverlay("self-flag"), 400);
      } catch (e) {
        status.textContent = "Something went wrong: " + (e.message || "please try again.");
      }
    });
  }

  // ─── Videos: forward to existing diagnosis/treatment overlay,
  //     emit `postop_video_event` for each play. ───────────────────────────
  function openVideos() {
    const diagBtn = document.getElementById("cardTreatment") || document.getElementById("cardDiagnosis");
    if (diagBtn) diagBtn.click();
    instrumentVideoPlayers();
  }

  let videoSession = null;
  function instrumentVideoPlayers() {
    const players = [
      { btn: document.getElementById("diagPlayPauseBtn"), kind: "DIAGNOSIS_TREATMENT" },
      { btn: document.getElementById("treatPlayPauseBtn"), kind: "RED_FLAG" },
    ];
    players.forEach((p) => {
      if (!p.btn || p.btn.dataset.postopWired === "1") return;
      p.btn.dataset.postopWired = "1";
      p.btn.addEventListener("click", () => {
        const sid = videoSession || `s_${Date.now()}_${Math.floor(Math.random() * 1e6)}`;
        videoSession = sid;
        fetchJSON(`/api/episodes/${encodeURIComponent(patientId)}/postop/video-event`, {
          method: "POST",
          body: JSON.stringify({
            video_kind: p.kind,
            event_type: "PLAYED",
            session_id: sid,
          }),
        }).catch(() => {});
      });
    });
  }

  // ─── D-X survey rendering (lightweight; sliders + pills) ───────────────
  let _surveyDay = null;
  let _surveyAnswers = {};

  function openSurveyOverlay(day) {
    if (!day) return;
    _surveyDay = day;
    _surveyAnswers = { section_a: {}, section_b: {}, section_c: {}, section_d: {} };
    renderSurveySections(day);
    openOverlay("survey");
  }

  function renderSurveySections(day) {
    const root = document.getElementById("postopSurveySections");
    if (!root) return;
    root.innerHTML = "";

    const buildSlider = (sectionKey, fieldKey, label, min, max, def) => {
      const wrap = document.createElement("div");
      wrap.className = "postop-form-section";
      wrap.innerHTML = `
        <label class="postop-form-label">${label}</label>
        <input type="range" min="${min}" max="${max}" value="${def}" class="postop-slider" />
        <span class="postop-slider-value">${def}</span>
      `;
      const slider = wrap.querySelector("input");
      const val = wrap.querySelector(".postop-slider-value");
      slider.addEventListener("input", () => {
        val.textContent = slider.value;
        const num = parseInt(slider.value, 10);
        _surveyAnswers[sectionKey] = _surveyAnswers[sectionKey] || {};
        _surveyAnswers[sectionKey][fieldKey] = num;
      });
      _surveyAnswers[sectionKey][fieldKey] = parseInt(def, 10);
      return wrap;
    };

    // Section A — pain & symptoms
    const heading = (txt) => {
      const h = document.createElement("h3");
      h.className = "postop-form-section-heading";
      h.textContent = txt;
      return h;
    };
    root.appendChild(heading("How are you feeling?"));
    root.appendChild(buildSlider("section_a", "pain_nrs", "Pain right now (0–10)", 0, 10, 2));

    // Section B — function (procedure-family agnostic, 0..100 self-rating)
    root.appendChild(heading("How is your function?"));
    root.appendChild(buildSlider("section_b", "function", "Daily function (0=worst, 100=best)", 0, 100, 70));
    root.appendChild(buildSlider("section_b", "stiffness", "Stiffness (0=worst, 100=best)", 0, 100, 70));

    // Section C — engagement
    root.appendChild(heading("Engagement & adherence"));
    root.appendChild(buildSlider("section_c", "pt_adherence_pct", "Physical therapy adherence (%)", 0, 100, 80));
    root.appendChild(buildSlider("section_c", "appointments_attended_pct", "Follow-up appointments attended (%)", 0, 100, 80));

    // Section D — recovery confidence
    root.appendChild(heading("Recovery confidence"));
    root.appendChild(buildSlider("section_d", "readiness_0_10", "How ready do you feel for the next stage? (0–10)", 0, 10, 7));
  }

  async function submitSurvey() {
    const status = document.getElementById("postopSurveyStatus");
    if (!_surveyDay) return;
    try {
      status.textContent = "Submitting…";
      await fetchJSON(`/api/episodes/${encodeURIComponent(patientId)}/postop/survey/${_surveyDay}`, {
        method: "POST",
        body: JSON.stringify({ answers: _surveyAnswers }),
      });
      status.textContent = "Thanks — your survey was sent to your care team.";
      setTimeout(() => closeOverlay("survey"), 1100);
    } catch (e) {
      status.textContent = "Something went wrong: " + (e.message || "please try again.");
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
