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
const IS_DOCTOR_VIEW = new URLSearchParams(window.location.search).get("doctor_view") === "1";
const conversation = [];
let pearStarted = false;
let intakeFormId = null;
let intakeForm = null;
let intakeStatus = "NOT_STARTED";
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

const STATUS_LABELS = {
  NOT_STARTED: "Not Started",
  INTERVIEW_IN_PROGRESS: "In Progress",
  INTERVIEW_COMPLETE: "Interview Complete",
  SUBMITTED: "Submitted",
  UPDATED: "Updated",
};

function statusBadgeTone(status) {
  if (status === "SUBMITTED") return { bg: "#dcfce7", color: "#166534", border: "#86efac" };
  if (status === "UPDATED") return { bg: "#fef9c3", color: "#854d0e", border: "#fde047" };
  if (status === "INTERVIEW_COMPLETE") return { bg: "#eff6ff", color: "#1d4ed8", border: "#bfdbfe" };
  if (status === "INTERVIEW_IN_PROGRESS") return { bg: "#ffedd5", color: "#9a3412", border: "#fdba74" };
  return { bg: "#f3f4f6", color: "#374151", border: "#d1d5db" };
}

function setStatus(status) {
  intakeStatus = status || "NOT_STARTED";
  const badge = document.getElementById("intakeStatusBadge");
  const tone = statusBadgeTone(intakeStatus);
  badge.textContent = STATUS_LABELS[intakeStatus] || intakeStatus;
  badge.style.background = tone.bg;
  badge.style.color = tone.color;
  badge.style.borderColor = tone.border;
  const startBtn = document.getElementById("startPearBtn");
  if (intakeStatus === "NOT_STARTED") startBtn.textContent = "Start Intake Interview";
  if (intakeStatus === "INTERVIEW_IN_PROGRESS") startBtn.textContent = "Continue Interview";
  if (intakeStatus === "INTERVIEW_COMPLETE") startBtn.textContent = "Review Intake Form";
  if (intakeStatus === "SUBMITTED" || intakeStatus === "UPDATED") startBtn.textContent = "View Intake Form";
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
  conversation.push({ role: "patient", text: val, timestamp: new Date().toISOString() });
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
    conversation.push({ role: "bot", text: reply, timestamp: new Date().toISOString() });
    if (data.interview_complete && intakeFormId) {
      document.getElementById("intakeProcessing").style.display = "block";
      const completed = await apiJson(`/api/intake-forms/${encodeURIComponent(intakeFormId)}/complete-interview`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ transcript: conversation, duration: conversation.length * 12 }),
      });
      intakeStatus = "INTERVIEW_COMPLETE";
      setStatus("INTERVIEW_COMPLETE");
      intakeForm = {
        id: intakeFormId,
        form_data: completed.formData || {},
        red_flags: completed.redFlags || [],
        conflicts: completed.conflicts || [],
        status: "INTERVIEW_COMPLETE",
      };
      document.getElementById("intakeProcessing").style.display = "none";
      renderIntakeForm();
      document.getElementById("endInterviewBtn").style.display = "none";
    }
  } catch (_e) {
    appendMessage("assistant", "I had trouble saving that answer. Please try again.");
    conversation.push({ role: "bot", text: "I had trouble saving that answer. Please try again.", timestamp: new Date().toISOString() });
  }
}

function sourceLabel(source) {
  const map = {
    interview: "From your interview",
    patient_record: "From your medical record",
    prep_document: "From prep document",
    patient_edited: "Patient Edited",
    "interview|patient_record": "Interview + medical record",
    "patient_record|interview": "Medical record + interview",
    not_obtained: "NOT OBTAINED",
    calculated: "Calculated",
    system: "System",
    patient: "Patient",
  };
  return map[source] || source || "Unknown source";
}

function fieldDisplayValue(value) {
  if (Array.isArray(value)) return value.join(", ");
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (value == null) return "";
  return String(value);
}

function normalizeInputValue(raw, original) {
  const text = String(raw ?? "").trim();
  if (Array.isArray(original)) {
    return text ? text.split(",").map((s) => s.trim()).filter(Boolean) : [];
  }
  if (typeof original === "boolean" || original === null) {
    if (!text) return null;
    if (/^(yes|true|y|1)$/i.test(text)) return true;
    if (/^(no|false|n|0)$/i.test(text)) return false;
  }
  return text;
}

async function patchField(section, field, nextValue) {
  if (!intakeFormId) return;
  await apiJson(`/api/intake-forms/${encodeURIComponent(intakeFormId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ section, field, value: nextValue }),
  });
  const payload = intakeForm?.form_data?.[section]?.[field];
  if (payload && typeof payload === "object") {
    payload.value = nextValue;
    payload.source = "patient_edited";
  }
}

function conflictMap(conflicts = []) {
  const out = new Map();
  conflicts.forEach((c) => {
    const key = c.field || "";
    if (key) out.set(key, c);
  });
  return out;
}

function renderIntakeForm() {
  if (!intakeForm) return;
  const box = document.getElementById("intakePreview");
  const body = document.getElementById("intakeBody");
  body.innerHTML = "";
  const cfMap = conflictMap(intakeForm.conflicts || []);
  const redFlags = intakeForm.red_flags || [];
  if (redFlags.length) {
    const alert = document.createElement("div");
    alert.className = "field";
    alert.style.borderColor = "#f59e0b";
    alert.style.background = "#fffbeb";
    alert.innerHTML = `<div class="label" style="color:#92400e;">RED FLAG</div>
      <div class="value" style="color:#92400e;">Your care team has been notified about: ${esc(redFlags.map((r) => r.flag || r).join("; "))}</div>`;
    body.appendChild(alert);
  }
  Object.entries(intakeForm.form_data || {}).forEach(([sectionName, fields]) => {
    const sectionWrap = document.createElement("details");
    sectionWrap.open = true;
    sectionWrap.className = "field";
    sectionWrap.style.background = "#fff";
    sectionWrap.innerHTML = `<summary class="section-title">${esc(sectionName.replace(/^section\d+_/, "").replace(/_/g, " "))}</summary>`;
    const grid = document.createElement("div");
    grid.className = "intake-grid";
    Object.entries(fields || {}).forEach(([fieldKey, payload]) => {
      if (!payload || typeof payload !== "object" || !Object.prototype.hasOwnProperty.call(payload, "value")) return;
      const fullKey = `${sectionName}.${fieldKey}`;
      const card = document.createElement("div");
      card.className = "field";
      const label = fieldKey.replace(/([A-Z])/g, " $1");
      const source = sourceLabel(payload.source);
      const value = fieldDisplayValue(payload.value);
      card.innerHTML = `
        <div class="label">${esc(label)}</div>
        <div class="value">${esc(value || (payload.source === "not_obtained" ? "We didn't cover this — please fill in if you can, or your care team will follow up." : "—"))}</div>
        <div style="margin-top:5px;font-size:11px;color:#64748b;">${esc(source)}</div>
        <input class="edit-input" value="${esc(value)}" style="display:block;margin-top:8px;" />
      `;
      const conflict = cfMap.get(fullKey);
      if (conflict) {
        card.style.borderColor = "#f59e0b";
        card.style.background = "#fffbeb";
        const note = document.createElement("div");
        note.style.marginTop = "6px";
        note.style.fontSize = "12px";
        note.style.color = "#92400e";
        note.textContent = `Record says ${fieldDisplayValue(conflict.recordValue)} but interview says ${fieldDisplayValue(conflict.patientValue)}.`;
        card.appendChild(note);
      }
      const input = card.querySelector(".edit-input");
      if (IS_DOCTOR_VIEW) {
        input.disabled = true;
        input.style.display = "none";
      }
      input.addEventListener("blur", async () => {
        if (IS_DOCTOR_VIEW) return;
        const nextValue = normalizeInputValue(input.value, payload.value);
        try {
          await patchField(sectionName, fieldKey, nextValue);
          card.querySelector(".value").textContent = fieldDisplayValue(nextValue) || "—";
        } catch (_e) {
          // Keep local value if network hiccups; user can retry by blurring again.
        }
      });
      grid.appendChild(card);
    });
    sectionWrap.appendChild(grid);
    body.appendChild(sectionWrap);
  });
  box.classList.add("active");
}

async function loadLatestIntakeForm() {
  try {
    const data = await apiJson(`/api/intake-forms/latest/${encodeURIComponent(PATIENT.id)}`);
    intakeForm = data.intake_form || null;
    if (intakeForm) {
      intakeFormId = intakeForm.id;
      setStatus(intakeForm.status || "NOT_STARTED");
      if (["INTERVIEW_COMPLETE", "SUBMITTED", "UPDATED"].includes(intakeForm.status || "")) {
        renderIntakeForm();
      }
      return;
    }
  } catch (_e) {
    // No form yet.
  }
  intakeForm = null;
  intakeFormId = null;
  setStatus("NOT_STARTED");
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
  if (back) {
    if (IS_DOCTOR_VIEW) {
      back.style.display = "inline";
      back.href = "/";
      back.addEventListener("click", (e) => {
        e.preventDefault();
        window.location.assign("/");
      });
    } else {
      back.style.display = "none";
      back.removeAttribute("href");
    }
  }
  if (IS_DOCTOR_VIEW) {
    const notifyBtn = document.getElementById("notifyCareTeamBtn");
    if (notifyBtn) notifyBtn.style.display = "none";
    const sendBtn = document.getElementById("pearSendBtn");
    const input = document.getElementById("pearInput");
    const submitBtn = document.getElementById("submitFormBtn");
    const saveBtn = document.getElementById("saveFormBtn");
    if (sendBtn) sendBtn.style.display = "none";
    if (input) input.style.display = "none";
    if (submitBtn) submitBtn.style.display = "none";
    if (saveBtn) saveBtn.style.display = "none";
  }
  const postTab = document.getElementById("postOperationTab");
  if (postTab) postTab.href = `/patient/${PATIENT.id}`;
  const preTab = document.getElementById("preOperationTab");
  if (preTab) preTab.href = `/patient/${PATIENT.id}/pre-op`;
  const companionBtn = document.getElementById("preopCompanionBtn");
  if (companionBtn) companionBtn.href = `/patient/${PATIENT.id}/digital-care-companion`;

  bindSpeedControls();
  loadPreopResources();
  setupNotifyCareTeam();
  loadLatestIntakeForm();

  document.getElementById("startPearBtn").addEventListener("click", async () => {
    document.getElementById("pearShell").classList.add("active");
    if (["INTERVIEW_COMPLETE", "SUBMITTED", "UPDATED"].includes(intakeStatus) && intakeForm) {
      renderIntakeForm();
      return;
    }
    if (!pearStarted) {
      pearStarted = true;
      try {
        const started = await apiJson("/api/intake-forms/start-interview", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ patientId: PATIENT.id, surgeryId: PATIENT.id }),
        });
        intakeFormId = started.intakeFormId;
        setStatus("INTERVIEW_IN_PROGRESS");
        document.getElementById("endInterviewBtn").style.display = "inline-block";
        const data = await apiJson("/api/pre-op/intake/start", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ patient_id: PATIENT.id }),
        });
        const starter = data.response || "Hi, I am your pre-op Digital Care Companion. I will ask one question at a time using the PEAR framework.";
        appendMessage("assistant", starter);
        conversation.push({ role: "bot", text: starter, timestamp: new Date().toISOString() });
      } catch (_e) {
        appendMessage("assistant", "Hi, I am your pre-op Digital Care Companion. Please tell me when your symptoms started and if they are changing.");
        conversation.push({ role: "bot", text: "Hi, I am your pre-op Digital Care Companion. Please tell me when your symptoms started and if they are changing.", timestamp: new Date().toISOString() });
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

  document.getElementById("endInterviewBtn").addEventListener("click", () => {
    const ok = window.confirm("Are you sure? Your answers so far will be saved.");
    if (!ok) return;
    document.getElementById("endInterviewBtn").style.display = "none";
    setStatus("INTERVIEW_IN_PROGRESS");
    appendMessage("assistant", "Interview paused. You can continue later from where you left off.");
  });

  document.getElementById("saveFormBtn").addEventListener("click", async () => {
    try {
      await loadLatestIntakeForm();
      appendMessage("assistant", "Your changes were saved.");
    } catch (_e) {
      appendMessage("assistant", "I could not refresh your latest form right now.");
    }
  });

  document.getElementById("submitFormBtn").addEventListener("click", async () => {
    if (!intakeFormId) return;
    try {
      await apiJson(`/api/intake-forms/${encodeURIComponent(intakeFormId)}/submit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      setStatus("SUBMITTED");
      appendMessage("assistant", "Form submitted. Your care team can now review it in the doctor portal.");
      await loadLatestIntakeForm();
    } catch (_e) {
      appendMessage("assistant", "I could not submit the form right now. Please try again.");
    }
  });
});
