const PATIENT = window.__PATIENT__ || {
  id: "demo_preop_001",
  name: "Maria L.",
  procedure: "Lumbar Fusion",
  phoneTeam: "",
  preop_resource: null,
};

const DISCLAIMER =
  "This was created with AI assistance. AI can make mistakes. This is not medical advice or a diagnosis. Always talk to a doctor about your health. If this is an emergency, call 911.";

const API = window.location.origin;
const conversation = [];
let pearStarted = false;
let prefillData = null;
let audioPlayer = null;
let preopIsPlaying = false;
let preopWatchedLogged = false;

function esc(str) {
  return String(str ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

async function apiJson(path, options = {}) {
  const res = await fetch(`${API}${path}`, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Request failed: ${res.status}`);
  return data;
}

function appendMessage(role, text) {
  const wrap = document.getElementById("pearChat");
  const msg = document.createElement("div");
  msg.className = `preop-msg ${role}`;
  msg.innerHTML = `
    <div class="preop-bubble">${esc(text)}</div>
    ${role === "assistant" ? `<div class="preop-disclaimer">${esc(DISCLAIMER)}</div>` : ""}
  `;
  wrap.appendChild(msg);
  wrap.scrollTop = wrap.scrollHeight;
}

async function submitAnswer() {
  const input = document.getElementById("pearInput");
  const val = (input.value || "").trim();
  if (!val) return;
  appendMessage("patient", val);
  conversation.push({ role: "user", content: val });
  input.value = "";
  try {
    const data = await apiJson("/api/pre-op/intake/answer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        patient_id: PATIENT.id,
        message: val,
        conversation_history: conversation,
      }),
    });
    const reply = data.response || "Thanks. I captured that.";
    appendMessage("assistant", reply);
    conversation.push({ role: "assistant", content: reply });
    if (data.interview_complete && data.prefill_form) {
      prefillData = data.prefill_form;
      showIntakePreview();
    }
  } catch (_e) {
    appendMessage("assistant", "I had trouble saving that answer. Please try again.");
  }
}

function createEditableField(label, value) {
  const div = document.createElement("div");
  div.className = "field";
  div.innerHTML = `
    <div class="label">${esc(label)}</div>
    <div class="value">${esc(value)}</div>
    <input class="edit-input" value="${esc(value)}" />
    <div class="edit-row">
      <button type="button" class="preop-btn edit-btn">Edit</button>
      <button type="button" class="preop-btn save-btn" style="display:none;">Save</button>
    </div>
  `;
  const editBtn = div.querySelector(".edit-btn");
  const saveBtn = div.querySelector(".save-btn");
  const valueEl = div.querySelector(".value");
  const inputEl = div.querySelector(".edit-input");

  editBtn.addEventListener("click", () => {
    div.classList.add("editing");
    editBtn.style.display = "none";
    saveBtn.style.display = "inline-block";
    inputEl.focus();
  });
  saveBtn.addEventListener("click", () => {
    div.classList.remove("editing");
    valueEl.textContent = inputEl.value;
    saveBtn.style.display = "none";
    editBtn.style.display = "inline-block";
  });
  return div;
}

function createChecklistSection(title, items = []) {
  const section = document.createElement("div");
  section.innerHTML = `<div class="section-title">${esc(title)}</div>`;
  const grid = document.createElement("div");
  grid.className = "intake-grid";
  items.forEach((item) => {
    if (typeof item === "string") {
      grid.appendChild(createEditableField(item, "Yes | Comments:"));
      return;
    }
    const label = item.label || item.item || "Checklist item";
    const value = `${item.status || "Yes"} | Comments: ${item.comments || ""}`;
    grid.appendChild(createEditableField(label, value));
  });
  section.appendChild(grid);
  return section;
}

function showIntakePreview() {
  if (!prefillData) return;
  const box = document.getElementById("intakePreview");
  const body = document.getElementById("intakeBody");
  body.innerHTML = "";

  const header = document.createElement("div");
  header.innerHTML = `<div class="section-title">Patient + Surgery Header</div>`;
  const headerGrid = document.createElement("div");
  headerGrid.className = "intake-grid";
  Object.entries(prefillData.header || {}).forEach(([k, v]) => {
    headerGrid.appendChild(createEditableField(k, v));
  });
  header.appendChild(headerGrid);
  body.appendChild(header);

  body.appendChild(createChecklistSection("Pre-Op Testing Acknowledgment", prefillData.preOpTesting));
  body.appendChild(createChecklistSection("Medication Instructions Acknowledged", prefillData.medicationInstructions));
  body.appendChild(createChecklistSection("Day-of-Surgery Prep", prefillData.dayOfSurgery));
  body.appendChild(createChecklistSection("Home Preparation Confirmed", prefillData.homePreparation));
  body.appendChild(createChecklistSection("Consent Forms", prefillData.consentForms));

  const finalReview = document.createElement("div");
  finalReview.innerHTML = `<div class="section-title">Final Review</div>`;
  const finalGrid = document.createElement("div");
  finalGrid.className = "intake-grid";
  Object.entries(prefillData.finalReview || {}).forEach(([k, v]) => {
    finalGrid.appendChild(createEditableField(k, v));
  });
  finalReview.appendChild(finalGrid);
  body.appendChild(finalReview);

  box.classList.add("active");
}

function fmtTime(sec) {
  if (Number.isNaN(sec)) return "0:00";
  return `${Math.floor(sec / 60)}:${String(Math.floor(sec % 60)).padStart(2, "0")}`;
}

function bindSpeedControls() {
  document.querySelectorAll('.speed-control[data-player="preop"] .speed-btn').forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll('.speed-control[data-player="preop"] .speed-btn').forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      if (audioPlayer) {
        const speed = Number(btn.dataset.speed || "1");
        audioPlayer.playbackRate = speed;
      }
    });
  });
}

function initPreopAudio(audioUrl) {
  if (!audioUrl) return;
  const playBtn = document.getElementById("preopPlayPauseBtn");
  const progressFill = document.getElementById("preopProgressFill");
  const progressBar = document.getElementById("preopProgressBar");
  const timeDisplay = document.getElementById("preopTimeDisplay");
  audioPlayer = new Audio(audioUrl);

  const setPlayState = (isPlaying) => {
    preopIsPlaying = isPlaying;
    playBtn.textContent = isPlaying ? "⏸ Pause" : "▶ Play";
  };

  audioPlayer.addEventListener("loadedmetadata", () => {
    timeDisplay.textContent = `0:00 / ${fmtTime(audioPlayer.duration)}`;
  });
  audioPlayer.addEventListener("timeupdate", () => {
    if (!audioPlayer.duration) return;
    const pct = (audioPlayer.currentTime / audioPlayer.duration) * 100;
    progressFill.style.width = `${pct}%`;
    timeDisplay.textContent = `${fmtTime(audioPlayer.currentTime)} / ${fmtTime(audioPlayer.duration)}`;
  });
  audioPlayer.addEventListener("ended", () => setPlayState(false));
  playBtn.addEventListener("click", () => {
    if (preopIsPlaying) {
      audioPlayer.pause();
      setPlayState(false);
    } else {
      audioPlayer.play();
      setPlayState(true);
      if (!preopWatchedLogged) {
        preopWatchedLogged = true;
        apiJson(`/api/patient/${encodeURIComponent(PATIENT.id)}/events`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ event_type: "preop_video_watched", payload: { source: "preop_page" } }),
        }).catch(() => {});
      }
    }
  });
  progressBar.addEventListener("click", (e) => {
    const rect = progressBar.getBoundingClientRect();
    const ratio = (e.clientX - rect.left) / rect.width;
    audioPlayer.currentTime = ratio * (audioPlayer.duration || 0);
  });
}

function loadPreopResources() {
  const battlecardContainer = document.getElementById("preopBattlecardContainer");
  const preop = PATIENT.preop_resource || {};
  const battlecardHtml = preop.battlecard_html || "";
  if (battlecardHtml.trim().startsWith("<") && battlecardContainer) {
    battlecardContainer.innerHTML = battlecardHtml;
  } else if (battlecardHtml.trim() && battlecardContainer) {
    const rows = battlecardHtml.split(/\n+/).filter(Boolean);
    battlecardContainer.innerHTML = `
      <div class="battlecard-fallback">
        ${rows.map((row) => `<div class="battlecard-fallback-item">${esc(row)}</div>`).join("")}
      </div>
    `;
  } else {
    if (battlecardContainer) {
      battlecardContainer.innerHTML = `
        <div class="battlecard-fallback">
          <div class="battlecard-fallback-item">Medication holds and stop dates reviewed</div>
          <div class="battlecard-fallback-item">NPO and fasting instructions confirmed</div>
          <div class="battlecard-fallback-item">Transport and caregiver plan confirmed</div>
          <div class="battlecard-fallback-item">Consent forms reviewed</div>
        </div>
      `;
    }
  }
  if (preop.voice_audio_url) {
    initPreopAudio(preop.voice_audio_url);
  } else {
    const btn = document.getElementById("preopPlayPauseBtn");
    if (btn) {
      btn.textContent = "⚠ Audio unavailable";
      btn.disabled = true;
    }
  }
}

function setupNotifyCareTeam() {
  const overlay = document.getElementById("notifyOverlay");
  const openBtn = document.getElementById("notifyCareTeamBtn");
  const cancelBtn = document.getElementById("notifyCancelBtn");
  const submitBtn = document.getElementById("notifySubmitBtn");
  const noteInput = document.getElementById("notifyNoteInput");
  const statusEl = document.getElementById("notifyStatus");
  const close = () => {
    overlay.classList.remove("active");
    statusEl.textContent = "";
    noteInput.value = "";
  };
  openBtn.addEventListener("click", () => overlay.classList.add("active"));
  cancelBtn.addEventListener("click", close);
  overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
  submitBtn.addEventListener("click", async () => {
    const note = noteInput.value.trim();
    if (!note) {
      statusEl.textContent = "Please add a note before sending.";
      return;
    }
    submitBtn.disabled = true;
    statusEl.textContent = "Sending...";
    try {
      const data = await apiJson("/api/pre-op/notify-care-team", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ patient_id: PATIENT.id, message: note }),
      });
      statusEl.textContent = data.response || "Notification sent to your care team.";
      setTimeout(close, 1200);
    } catch (e) {
      statusEl.textContent = e.message || "Could not send notification.";
    } finally {
      submitBtn.disabled = false;
    }
  });
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("patientName").textContent = PATIENT.name || "Patient";
  document.getElementById("patientProcedure").textContent = PATIENT.procedure ? `Procedure: ${PATIENT.procedure}` : "Pre-Op Preparation";
  const back = document.getElementById("backToDashboard");
  if (back) back.href = `/patient/${PATIENT.id}`;
  const postTab = document.getElementById("postOperationTab");
  if (postTab) postTab.href = `/patient/${PATIENT.id}`;
  const preTab = document.getElementById("preOperationTab");
  if (preTab) preTab.href = `/patient/${PATIENT.id}/pre-op`;
  const companionBtn = document.getElementById("preopCompanionBtn");
  if (companionBtn) companionBtn.href = `/patient/${PATIENT.id}/digital-care-companion`;

  bindSpeedControls();
  loadPreopResources();
  setupNotifyCareTeam();

  document.getElementById("startPearBtn").addEventListener("click", async () => {
    document.getElementById("pearShell").classList.add("active");
    if (!pearStarted) {
      pearStarted = true;
      try {
        const data = await apiJson("/api/pre-op/intake/start", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ patient_id: PATIENT.id }),
        });
        const starter = data.response || "Hi, I am your pre-op Digital Care Companion. I will ask one question at a time using the PEAR framework.";
        appendMessage("assistant", starter);
        conversation.push({ role: "assistant", content: starter });
      } catch (_e) {
        appendMessage("assistant", "Hi, I am your pre-op Digital Care Companion. Please tell me when your symptoms started and if they are changing.");
      }
    }
    document.getElementById("pearInput").focus();
  });

  document.getElementById("pearSendBtn").addEventListener("click", submitAnswer);
  document.getElementById("pearInput").addEventListener("keypress", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      submitAnswer();
    }
  });

  document.getElementById("submitFormBtn").addEventListener("click", () => {
    const edited = {};
    document.querySelectorAll("#intakeBody .field").forEach((field) => {
      const key = field.querySelector(".label")?.textContent || "";
      const val = field.querySelector(".value")?.textContent || field.querySelector(".edit-input")?.value || "";
      if (key) edited[key] = val;
    });
    apiJson("/api/pre-op/intake/submit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ patient_id: PATIENT.id, form_data: edited }),
    }).then(() => {
      appendMessage("assistant", "Form submitted. Your care team can now review it in the doctor portal.");
    }).catch(() => {
      appendMessage("assistant", "I could not submit the form right now. Please try again.");
    });
  });
});
