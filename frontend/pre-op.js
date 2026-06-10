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

const SECTION_NUM_TO_KEY = {
  1: "section1_demographics",
  2: "section2_surgicalInfo",
  3: "section3_medicalHistory",
  4: "section4_surgicalAnesthesiaHistory",
  5: "section5_medicationsAllergies",
  6: "section6_socialHistory",
  7: "section7_familyHistory",
  8: "section8_reviewOfSystems",
  9: "section9_functionalAssessment",
  10: "section10_dayOfSurgeryReadiness",
  11: "section11_acknowledgments",
};

const SECTION_TITLES = [
  "",
  "Demographics",
  "Surgical Info",
  "Medical History",
  "Surgical & Anesthesia History",
  "Medications & Allergies",
  "Social History",
  "Family History",
  "Review of Systems",
  "Functional Assessment",
  "Day of Surgery Readiness",
  "Information Accurate",
];

/** [fieldKey, display label] — matches backend intake schema section 2. */
const SECTION2_DISPLAY_ORDER = [
  ["scheduledProcedure", "Scheduled procedure"],
  ["procedureCPTCodes", "CPT code(s)"],
  ["surgicalSite", "Surgical site"],
  ["laterality", "Laterality"],
  ["surgeonName", "Surgeon"],
  ["anesthesiologist", "Anesthesiologist"],
  ["scheduledDateTime", "Scheduled date / time"],
  ["facilityLocation", "Facility / location"],
  ["procedureType", "Procedure type"],
  ["estimatedDuration", "Estimated duration"],
  ["preOpDiagnosis", "Pre-op diagnosis"],
];

const SECTION_BLURBS = {
  1: "Confirm your contact and insurance details.",
  2: "Review the surgery plan from your care team.",
  3: "Past and current medical conditions.",
  4: "Prior operations and anesthesia experiences.",
  5: "Medicines, supplements, and allergies.",
  6: "Tobacco, alcohol, home support, and daily life.",
  7: "Conditions that run in your family.",
  8: "Symptoms by body system.",
  9: "Mobility, falls, and advance care wishes.",
  10: "Ride home, fasting, and day-of instructions.",
  11: "Confirm your information is accurate.",
};

/** Shown under the section title during the interview (UX only; model uses per-section MD in backend). */
const SECTION_FOCUS_LINES = {
  3: "We’ll cover conditions like diabetes, heart or lung disease, bleeding or clotting problems, cancer history, and similar topics.",
  4: "We’ll cover prior surgeries, types of anesthesia you’ve had, nausea after anesthesia, and family reactions to anesthesia.",
  5: "We’ll cover prescription medicines, blood thinners, insulin, over-the-counter drugs, supplements, and allergies.",
  6: "We’ll cover tobacco, alcohol, other substances, work, exercise, who lives with you, and help after surgery.",
  7: "We’ll cover heart disease, diabetes, cancer, bleeding disorders, anesthesia problems, and sudden cardiac death in relatives.",
  8: "We’ll do a quick pass over common symptoms by body system (energy, heart, lungs, nerves, stomach, and more).",
  9: "We’ll cover activity level, falls, memory or confusion, and advance directives or a healthcare proxy if you have one.",
  10: "We’ll cover your ride home, who stays with you after surgery, NPO (fasting) rules, and instructions from your team.",
};

let pearStarted = false;
let intakeFormId = null;
let intakeForm = null;
let intakeStatus = "NOT_STARTED";
let interviewState = {
  activeSection: 1,
  completedSections: [],
  messagesBySection: {},
  sectionInterviewComplete: {},
  firstPassComplete: false,
};

function assignInterviewState(raw) {
  interviewState = raw && typeof raw === "object" ? { ...raw } : {};
  if (!interviewState.activeSection) interviewState.activeSection = 1;
  if (!interviewState.messagesBySection) interviewState.messagesBySection = {};
  interviewState.completedSections = normalizeCompletedSections(interviewState.completedSections);
  if (!interviewState.sectionInterviewComplete || typeof interviewState.sectionInterviewComplete !== "object") {
    interviewState.sectionInterviewComplete = {};
  }
  const sic = { ...interviewState.sectionInterviewComplete };
  const cs = interviewState.completedSections;
  if (Object.keys(sic).length === 0 && cs.length) {
    cs.forEach((n) => {
      if (n >= 3 && n <= 10) sic[String(n)] = true;
    });
  }
  interviewState.sectionInterviewComplete = sic;
  if (interviewState.firstPassComplete == null) {
    interviewState.firstPassComplete =
      cs.length >= 11 && [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11].every((i) => cs.includes(i));
  }
}

function sectionInterviewCompleteFor(n) {
  return !!(interviewState.sectionInterviewComplete || {})[String(n)];
}

function firstPassCompleteBool() {
  return !!interviewState.firstPassComplete;
}
let activeSectionNum = 1;
let audioPlayer = null;
let preopIsPlaying = false;
let preopWatchedLogged = false;

function esc(str) {
  return String(str ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function formatApiDetail(detail) {
  if (detail == null) return "";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (typeof item === "string") return item;
        if (item && typeof item === "object" && item.msg) return String(item.msg);
        try {
          return JSON.stringify(item);
        } catch (_e) {
          return String(item);
        }
      })
      .join("; ");
  }
  if (typeof detail === "object" && detail.msg) return String(detail.msg);
  try {
    return JSON.stringify(detail);
  } catch (_e) {
    return `Request failed`;
  }
}

async function apiJson(path, options = {}) {
  const res = await fetch(`${API}${path}`, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(formatApiDetail(data.detail) || `Request failed: ${res.status}`);
  return data;
}

function logPatientEvent(eventType, payload = {}) {
  return apiJson(`/api/patient/${encodeURIComponent(PATIENT.id)}/events`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ event_type: eventType, payload }),
  }).catch(() => {});
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

function normalizeCompletedSections(cs) {
  return (cs || [])
    .map((x) => Number(x))
    .filter((x) => !Number.isNaN(x) && x >= 1 && x <= 11);
}

function sectionCompleted(n) {
  return normalizeCompletedSections(interviewState.completedSections).includes(n);
}

function sectionUnlocked(n) {
  if (firstPassCompleteBool()) return true;
  if (n === 1) return true;
  return sectionCompleted(n - 1);
}

function allSectionsComplete() {
  for (let i = 1; i <= 11; i += 1) {
    if (!sectionCompleted(i)) return false;
  }
  return true;
}

function appendMessage(role, text) {
  const wrap = document.getElementById("pearChat");
  const msg = document.createElement("div");
  msg.className = `preop-msg ${role === "patient" ? "patient" : "assistant"}`;
  msg.innerHTML = `
    <div class="preop-bubble">${esc(text)}</div>
    ${role !== "patient" ? `<div class="preop-disclaimer">${esc(DISCLAIMER)}</div>` : ""}
  `;
  wrap.appendChild(msg);
  wrap.scrollTop = wrap.scrollHeight;
}

function clearChat() {
  const wrap = document.getElementById("pearChat");
  wrap.innerHTML = "";
}

function chatHistoryForApi() {
  const wrap = document.getElementById("pearChat");
  const rows = [];
  wrap.querySelectorAll(".preop-msg").forEach((el) => {
    const isPat = el.classList.contains("patient");
    const bubble = el.querySelector(".preop-bubble");
    if (!bubble) return;
    rows.push({
      role: isPat ? "user" : "assistant",
      content: bubble.textContent.trim(),
    });
  });
  return rows;
}

function renderStepper() {
  const el = document.getElementById("intakeStepper");
  if (!el) return;
  el.style.display = "none";
  el.innerHTML = "";
}

function selectSection(n) {
  if (!sectionUnlocked(n)) return;
  activeSectionNum = n;
  interviewState.activeSection = n;
  renderNav();
  renderSectionPane();
}

function sectionStatusText(n) {
  if (sectionCompleted(n)) return "Completed";
  if (n >= 3 && n <= 10 && sectionInterviewCompleteFor(n) && !sectionCompleted(n)) return "Review needed";
  if (n >= 3 && n <= 10 && activeSectionNum === n && !sectionInterviewCompleteFor(n)) return "Interview";
  if (!sectionUnlocked(n)) return "Locked";
  return "Not started";
}

function renderSidebar() {
  const el = document.getElementById("intakeSidebar");
  if (!el) return;
  el.innerHTML = `<h3>Sections</h3>`;
  for (let n = 1; n <= 11; n += 1) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "intake-nav-item";
    btn.setAttribute("aria-label", `Section ${n} ${SECTION_TITLES[n]}, ${sectionStatusText(n)}`);
    if (activeSectionNum === n) btn.classList.add("active");
    if (sectionCompleted(n) && activeSectionNum !== n) btn.classList.add("done");
    if (!sectionUnlocked(n)) btn.classList.add("locked");
    if (sectionUnlocked(n)) {
      btn.addEventListener("click", () => selectSection(n));
    }
    btn.innerHTML = `
      <span class="nav-title">${n}. ${esc(SECTION_TITLES[n])}</span>
      <span class="nav-sub">${esc(SECTION_BLURBS[n] || "")}</span>
      <span class="nav-status">${esc(sectionStatusText(n))}</span>
    `;
    el.appendChild(btn);
  }
}

function renderNav() {
  renderStepper();
  renderSidebar();
}

function fieldSourceCaption(payload) {
  const base = sourceLabel(payload.source);
  if (payload.source === "patient_edited" && payload.editedAt) {
    try {
      return `${base} · ${new Date(payload.editedAt).toLocaleString()}`;
    } catch (_e) {
      return base;
    }
  }
  return base;
}

function renderSectionPreviewOnly(sectionKey) {
  const body = document.getElementById("intakeBody");
  const preview = document.getElementById("intakePreview");
  const head = document.getElementById("intakePreviewHead");
  if (!body || !preview) return;
  body.innerHTML = "";
  preview.style.display = "block";
  if (head) head.textContent = "This section on your form";

  const fd = intakeForm?.form_data || {};
  const secData = fd[sectionKey] || {};
  let keys;
  if (sectionKey === "section2_surgicalInfo") {
    keys = SECTION2_DISPLAY_ORDER.map((r) => r[0]);
  } else {
    keys = Object.keys(secData).filter((fieldKey) => {
      const p = secData[fieldKey];
      return p && typeof p === "object" && Object.prototype.hasOwnProperty.call(p, "value");
    });
  }

  const grid = document.createElement("div");
  grid.className = "intake-grid";

  const labelFor = (fieldKey) => {
    if (sectionKey === "section2_surgicalInfo") {
      const row = SECTION2_DISPLAY_ORDER.find((r) => r[0] === fieldKey);
      return row ? row[1] : fieldKey.replace(/([A-Z])/g, " $1");
    }
    return fieldKey.replace(/([A-Z])/g, " $1");
  };

  keys.forEach((fieldKey) => {
    let payload = secData[fieldKey];
    if (!payload || typeof payload !== "object") {
      payload = {
        value: fieldKey === "procedureCPTCodes" ? [] : "",
        source: "not_obtained",
      };
    }
    if (!Object.prototype.hasOwnProperty.call(payload, "value")) return;
    const card = document.createElement("div");
    card.className = "field";
    if (payload.source === "not_obtained") {
      card.style.borderStyle = "dashed";
      card.style.background = "#f8fafc";
    }
    const label = labelFor(fieldKey);
    const value = fieldDisplayValue(payload.value);
    const displayVal =
      payload.source === "not_obtained" && !value
        ? "We didn't cover this — please fill in if you can"
        : value || "—";
    const ro = IS_DOCTOR_VIEW;
    card.innerHTML = `
      <div class="label">${esc(label)}</div>
      <div class="value">${esc(displayVal)}</div>
      <div class="field-source" style="margin-top:6px;font-size:12px;color:#64748b;text-align:right;">${esc(fieldSourceCaption(payload))}</div>
      <input class="edit-input" value="${esc(value)}" style="display:${ro ? "none" : "block"};margin-top:8px;" ${ro ? "disabled" : ""} />
    `;
    const input = card.querySelector(".edit-input");
    if (!ro) {
      input.addEventListener("blur", async () => {
        const nextValue = normalizeInputValue(input.value, payload.value);
        try {
          await patchField(sectionKey, fieldKey, nextValue);
          const p2 = intakeForm?.form_data?.[sectionKey]?.[fieldKey];
          card.querySelector(".value").textContent =
            fieldDisplayValue(p2?.value) ||
            (p2?.source === "not_obtained" ? "We didn't cover this — please fill in if you can" : "—");
          const cap = card.querySelector(".field-source");
          if (cap && p2) cap.textContent = fieldSourceCaption(p2);
        } catch (_e) {
          /* retry */
        }
      });
    }
    grid.appendChild(card);
  });
  body.appendChild(grid);
}

function sourceLabel(source) {
  const map = {
    interview: "From your interview",
    patient_record: "From your medical record",
    prep_document: "From your doctor's plan",
    patient_edited: "Edited by you",
    doctor: "Care team",
    health_system: "Hospital / organization",
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
    payload.editedAt = new Date().toISOString();
  }
}

function renderDemographicsPane() {
  const pane = document.getElementById("intakeSectionPane");
  const s1 = intakeForm?.form_data?.section1_demographics || {};
  const field = (key, label, type = "text") => {
    const node = s1[key] || {};
    const val = fieldDisplayValue(node.value);
    return `<div class="demo-field">
      <label for="demo_${esc(key)}">${esc(label)}</label>
      ${type === "textarea" ? `<textarea id="demo_${esc(key)}">${esc(val)}</textarea>` : `<input id="demo_${esc(key)}" type="${type}" value="${esc(val)}" />`}
    </div>`;
  };
  pane.innerHTML = `
    <p style="font-size:15px;color:#334155;line-height:1.5;margin-top:0;">
      Hey ${esc(PATIENT.name || "there")}, thank you for taking the time for your intake interview. Your answers go to your surgeon to help prepare you for surgery.
      We are starting with <strong>Section 1: Demographics</strong>. Please confirm or update the information below, then save and continue.
    </p>
    <div class="demo-form-grid">
      ${field("fullLegalName", "Full legal name")}
      ${field("preferredName", "Preferred name")}
      ${field("dateOfBirth", "Date of birth")}
      ${field("sexAssignedAtBirth", "Sex assigned at birth")}
      ${field("genderIdentity", "Gender identity")}
      ${field("ethnicity", "Ethnicity / race")}
      ${field("primaryLanguage", "Primary language")}
      ${field("interpreterNeeded", "Interpreter needed? (yes/no)", "text")}
      ${field("address", "Address", "textarea")}
      ${field("phonePrimary", "Primary phone")}
      ${field("phoneEmergency", "Alternate / emergency phone")}
      ${field("email", "Email", "email")}
      ${field("emergencyContactName", "Emergency contact name")}
      ${field("emergencyContactRelationship", "Relationship to you")}
      ${field("emergencyContactPhone", "Emergency contact phone")}
      ${field("insuranceProvider", "Insurance provider")}
      ${field("insurancePolicyNumber", "Policy number")}
      ${field("insuranceGroupNumber", "Group number")}
      ${field("referringPhysician", "Referring physician")}
    </div>
    <div style="margin-top:14px;">
      <button type="button" class="preop-btn primary" id="saveDemographicsBtn">Confirm &amp; continue to Section 2</button>
    </div>
  `;
  document.getElementById("saveDemographicsBtn").addEventListener("click", async () => {
    const keys = Object.keys(s1);
    for (const key of keys) {
      const el = document.getElementById(`demo_${key}`);
      if (!el) continue;
      const raw = el.value.trim();
      const orig = (s1[key] || {}).value;
      const next = normalizeInputValue(raw, orig);
      if (String(next) !== String(orig ?? "")) {
        await patchField("section1_demographics", key, next);
      }
    }
    await apiJson(`/api/intake-forms/${encodeURIComponent(intakeFormId)}/interview/complete-section`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ section: 1 }),
    });
    const refreshed = await apiJson(`/api/intake-forms/latest/${encodeURIComponent(PATIENT.id)}`);
    intakeForm = refreshed.intake_form;
    assignInterviewState(intakeForm.interview_state || interviewState);
    selectSection(2);
  });
}

function renderSurgicalInfoPane() {
  const pane = document.getElementById("intakeSectionPane");
  pane.innerHTML = `
    <p style="font-size:16px;color:#334155;line-height:1.55;margin-top:0;">
      <strong>Section 2: Surgical information</strong> — below is what we have from your doctor's plan and hospital records.
      Please read every field. If anything looks wrong, you can edit it. When everything looks right, confirm to continue.
    </p>
  `;
  const preview = document.getElementById("intakePreview");
  const actions = document.getElementById("intakeFormActionsHost");
  if (preview) preview.style.display = "block";
  renderSectionPreviewOnly("section2_surgicalInfo");
  if (actions) {
    actions.innerHTML = "";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "preop-btn primary";
    btn.id = "confirmSection2Btn";
    btn.textContent = "Confirm & continue to Section 3";
    btn.addEventListener("click", async () => {
      try {
        await apiJson(`/api/intake-forms/${encodeURIComponent(intakeFormId)}/interview/complete-section`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ section: 2 }),
        });
        const refreshed = await apiJson(`/api/intake-forms/latest/${encodeURIComponent(PATIENT.id)}`);
        intakeForm = refreshed.intake_form;
        assignInterviewState(intakeForm.interview_state || interviewState);
        selectSection(3);
      } catch (e) {
        window.alert(e.message || "Could not continue.");
      }
    });
    actions.appendChild(btn);
  }
}

function scrollInterviewComposerIntoView() {
  requestAnimationFrame(() => {
    const card = document.querySelector(".intake-chat-card");
    const input = document.getElementById("pearInput");
    if (card) {
      card.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
    if (input) {
      input.scrollIntoView({ block: "end", behavior: "smooth" });
      if (!IS_DOCTOR_VIEW) {
        try {
          input.focus({ preventScroll: false });
        } catch (_e) {
          input.focus();
        }
      }
    }
  });
}

function sectionChatIntro(n) {
  const name = PATIENT.name || "there";
  const lines = {
    3: `Hi ${name}, in this section I'll ask about your medical history. Let's start — do you have any ongoing medical conditions like diabetes, high blood pressure, heart disease, or lung problems?`,
    4: `Now I'd like to ask about past surgeries and anesthesia. Have you ever had any surgical procedures before?`,
    5: `In this section we're asking about your medications and allergies. Let's start — are you currently taking any prescription medications?`,
    6: `Now I have a few questions about your daily life. Our first question is — do you currently smoke or use any tobacco products, including vaping?`,
    7: `In this section we're asking about your family's health history. Does anyone in your immediate family — parents, siblings, or children — have heart disease, diabetes, or cancer?`,
    8: `Now I'd like to do a quick check on how you've been feeling lately. Have you had any recent changes in your energy level, unexplained weight changes, or fevers?`,
    9: `In this section we're asking about your day-to-day function. How would you describe your typical activity level — can you walk up a flight of stairs without getting short of breath?`,
    10: `Let's make sure you're all set for the day of surgery. Do you have a ride arranged to and from the hospital?`,
  };
  return lines[n] || "Reply in the text box below when you're ready.";
}

function renderAcknowledgementsPane() {
  const pane = document.getElementById("intakeSectionPane");
  const s11 = intakeForm?.form_data?.section11_acknowledgments || {};
  const ia = (s11.informationAccurate || {}).value === true;
  pane.innerHTML = `
    <h3 style="margin-top:0;">Section 11: Confirm your information</h3>
    <p style="color:#475569;font-size:14px;">Please review and confirm the acknowledgements below.</p>
    <label style="display:flex;gap:10px;align-items:flex-start;font-size:14px;color:#0f172a;margin:14px 0;">
      <input type="checkbox" id="ackAccurate" ${ia ? "checked" : ""} style="margin-top:3px;" />
      <span>I confirm that the information I provided is accurate to the best of my knowledge.</span>
    </label>
    <button type="button" class="preop-btn primary" id="saveAckBtn">Confirm &amp; finish this section</button>
  `;
  document.getElementById("intakePreview").style.display = "block";
  const ackActions = document.getElementById("intakeFormActionsHost");
  if (ackActions) ackActions.innerHTML = "";
  renderSectionPreviewOnly("section11_acknowledgments");
  document.getElementById("saveAckBtn").addEventListener("click", async () => {
    const ok = document.getElementById("ackAccurate").checked;
    if (!ok) {
      window.alert("Please confirm that your information is accurate to continue.");
      return;
    }
    await patchField("section11_acknowledgments", "informationAccurate", true);
    await apiJson(`/api/intake-forms/${encodeURIComponent(intakeFormId)}/interview/complete-section`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        section: 11,
        acknowledgements: { informationAccurate: true, understandsEditRights: true },
      }),
    });
    const refreshed = await apiJson(`/api/intake-forms/latest/${encodeURIComponent(PATIENT.id)}`);
    intakeForm = refreshed.intake_form;
    assignInterviewState(intakeForm.interview_state || interviewState);
    renderNav();
    renderSectionPane();
    if (allSectionsComplete()) {
      const ga = document.getElementById("intakeGlobalActions");
      if (ga) ga.style.display = "block";
      document.getElementById("finalizeInterviewBtn").style.display = "inline-block";
      document.getElementById("saveFormBtn").style.display = "none";
      document.getElementById("submitFormBtn").style.display = "none";
    }
  });
}

function setupForceCompleteButton() {
  const host = document.getElementById("intakeForceDoneHost");
  if (!host) return;
  host.innerHTML = "";
  const fc = document.createElement("button");
  fc.type = "button";
  fc.className = "preop-btn";
  fc.textContent = "End interview early and review form";
  fc.addEventListener("click", async () => {
    if (!window.confirm("We'll save what we have so far and move you to the form review. You can edit fields after. Continue?")) return;
    try {
      await apiJson(`/api/intake-forms/${encodeURIComponent(intakeFormId)}/interview/complete-section`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ section: activeSectionNum, forceComplete: true }),
      });
      const refreshed = await apiJson(`/api/intake-forms/latest/${encodeURIComponent(PATIENT.id)}`);
      intakeForm = refreshed.intake_form;
      assignInterviewState(intakeForm.interview_state || interviewState);
      renderNav();
      renderSectionPane();
    } catch (e) {
      window.alert(e.message || "Could not end the interview for this section.");
    }
  });
  host.appendChild(fc);
}

async function confirmActiveSectionReview() {
  try {
    await apiJson(`/api/intake-forms/${encodeURIComponent(intakeFormId)}/interview/complete-section`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ section: activeSectionNum, confirmReview: true }),
    });
    const refreshed = await apiJson(`/api/intake-forms/latest/${encodeURIComponent(PATIENT.id)}`);
    intakeForm = refreshed.intake_form;
    assignInterviewState(intakeForm.interview_state || interviewState);
    renderNav();
    if (activeSectionNum < 11) selectSection(activeSectionNum + 1);
    else renderSectionPane();
  } catch (e) {
    window.alert(e.message || "Could not confirm this section.");
  }
}

function renderSectionPane() {
  const chatRow = document.getElementById("intakeChatRow");
  const preview = document.getElementById("intakePreview");
  const phaseHdr = document.getElementById("intakePhaseHeader");
  const actionsHost = document.getElementById("intakeFormActionsHost");
  if (chatRow) chatRow.style.display = "none";
  if (preview) preview.style.display = "none";
  if (phaseHdr) {
    phaseHdr.style.display = "none";
    phaseHdr.innerHTML = "";
  }
  if (actionsHost) actionsHost.innerHTML = "";
  const globalActions = document.getElementById("intakeGlobalActions");
  if (globalActions) globalActions.style.display = "none";
  document.getElementById("intakeSectionPane").innerHTML = "";

  if (activeSectionNum === 1) {
    renderDemographicsPane();
    renderNav();
    return;
  }
  if (activeSectionNum === 2) {
    renderSurgicalInfoPane();
    renderNav();
    return;
  }
  if (activeSectionNum >= 3 && activeSectionNum <= 10) {
    const sic = sectionInterviewCompleteFor(activeSectionNum);
    const done = sectionCompleted(activeSectionNum);

    if (!sic) {
      if (phaseHdr) {
        phaseHdr.style.display = "block";
        phaseHdr.innerHTML = `<strong>Section ${activeSectionNum}: ${esc(SECTION_TITLES[activeSectionNum])}</strong>`;
      }
      chatRow.style.display = "block";
      clearChat();
      const saved = (interviewState.messagesBySection || {})[String(activeSectionNum)] || [];
      saved.forEach((m) => {
        const role = m.role === "patient" ? "patient" : "assistant";
        appendMessage(role, m.text || "");
      });
      if (!saved.length) {
        appendMessage("assistant", sectionChatIntro(activeSectionNum));
      }
      setupForceCompleteButton();
      renderNav();
      scrollInterviewComposerIntoView();
      return;
    }

    if (phaseHdr) {
      phaseHdr.style.display = "block";
      phaseHdr.innerHTML = `<strong>Review: ${esc(SECTION_TITLES[activeSectionNum])}</strong><br/><span style="font-weight:400;">Check each field. Edit anything that needs an update, then confirm to continue.</span>`;
    }
    preview.style.display = "block";
    renderSectionPreviewOnly(SECTION_NUM_TO_KEY[activeSectionNum]);
    if (!done) {
      const nextNum = activeSectionNum + 1;
      const nextTitle = SECTION_TITLES[nextNum] || "";
      const confirmBtn = document.createElement("button");
      confirmBtn.type = "button";
      confirmBtn.className = "preop-btn primary";
      confirmBtn.style.width = "100%";
      confirmBtn.textContent = `Confirm & continue to Section ${nextNum}${nextTitle ? ": " + nextTitle : ""}`;
      confirmBtn.addEventListener("click", confirmActiveSectionReview);
      actionsHost.appendChild(confirmBtn);
    } else {
      const p = document.createElement("p");
      p.style.margin = "0";
      p.style.fontSize = "14px";
      p.style.color = "#64748b";
      p.textContent = "You have confirmed this section. You can still edit fields above if something changes.";
      actionsHost.appendChild(p);
    }
    renderNav();
    return;
  }
  if (activeSectionNum === 11) {
    renderAcknowledgementsPane();
  }
  renderNav();
}

async function submitSectionChat() {
  const input = document.getElementById("pearInput");
  const val = (input.value || "").trim();
  if (!val || activeSectionNum < 3 || activeSectionNum > 10) return;
  const priorHistory = chatHistoryForApi();
  appendMessage("patient", val);
  input.value = "";
  const typing = document.getElementById("pearTyping");
  const proc = document.getElementById("intakeProcessing");
  if (typing) typing.style.display = "block";
  if (proc) proc.style.display = "none";
  try {
    const data = await apiJson(`/api/intake-forms/${encodeURIComponent(intakeFormId)}/interview/section-message`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        section: activeSectionNum,
        message: val,
        conversationHistory: priorHistory,
      }),
    });
    assignInterviewState(data.interviewState || interviewState);
    intakeForm.form_data = data.formData || intakeForm.form_data;
    appendMessage("assistant", data.reply || "Thanks.");
    if (typing) typing.style.display = "none";
    if (data.sectionComplete) {
      if (proc) proc.style.display = "block";
      appendMessage(
        "assistant",
        "That completes the questions for this section. Next you'll review the form — check each answer, edit if needed, then tap Confirm and continue.",
      );
      renderNav();
      setTimeout(() => {
        if (proc) proc.style.display = "none";
        renderSectionPane();
      }, 900);
    } else {
      renderNav();
    }
  } catch (e) {
    if (typing) typing.style.display = "none";
    appendMessage("assistant", e.message || "Something went wrong. Please try again.");
  }
}

function renderIntakeForm() {
  if (!intakeForm) return;
  const box = document.getElementById("intakePreview");
  const body = document.getElementById("intakeBody");
  body.innerHTML = "";
  box.classList.add("active");
  const cfMap = new Map();
  (intakeForm.conflicts || []).forEach((c) => {
    if (c.field) cfMap.set(c.field, c);
  });
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
  Object.entries(intakeForm.form_data || {})
    .filter(([k]) => /^section\d+_/i.test(k))
    .forEach(([sectionName, fields]) => {
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
        const value = fieldDisplayValue(payload.value);
        const ro = IS_DOCTOR_VIEW;
        const srcLine = fieldSourceCaption(payload);
        card.innerHTML = `
        <div class="label">${esc(label)}</div>
        <div class="value">${esc(value || (payload.source === "not_obtained" ? "We didn't cover this — please fill in if you can, or your care team will follow up." : "—"))}</div>
        <div class="field-source" style="margin-top:6px;font-size:12px;color:#64748b;text-align:right;">${esc(srcLine)}</div>
        <input class="edit-input" value="${esc(value)}" style="display:${ro ? "none" : "block"};margin-top:8px;" />
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
        if (ro) {
          input.disabled = true;
        }
        input.addEventListener("blur", async () => {
          if (IS_DOCTOR_VIEW) return;
          const nextValue = normalizeInputValue(input.value, payload.value);
          try {
            await patchField(sectionName, fieldKey, nextValue);
            card.querySelector(".value").textContent = fieldDisplayValue(nextValue) || "—";
            const p2 = intakeForm?.form_data?.[sectionName]?.[fieldKey];
            const cap = card.querySelector(".field-source");
            if (cap && p2) cap.textContent = fieldSourceCaption(p2);
          } catch (_e) {
            /* */
          }
        });
        grid.appendChild(card);
      });
      sectionWrap.appendChild(grid);
      body.appendChild(sectionWrap);
    });
}

function buildFullTranscriptFromState() {
  const by = interviewState.messagesBySection || {};
  const out = [];
  Object.keys(by)
    .sort((a, b) => Number(a) - Number(b))
    .forEach((k) => {
      (by[k] || []).forEach((m) => {
        out.push({
          role: m.role === "patient" ? "patient" : "bot",
          text: m.text || "",
          timestamp: m.timestamp || "",
        });
      });
    });
  return out;
}

async function loadLatestIntakeForm() {
  try {
    const data = await apiJson(`/api/intake-forms/latest/${encodeURIComponent(PATIENT.id)}`);
    intakeForm = data.intake_form || null;
    if (intakeForm) {
      intakeFormId = intakeForm.id;
      assignInterviewState(
        intakeForm.interview_state || {
          activeSection: 1,
          completedSections: [],
          messagesBySection: {},
          sectionInterviewComplete: {},
          firstPassComplete: false,
        },
      );
      activeSectionNum = interviewState.activeSection || 1;
      setStatus(intakeForm.status || "NOT_STARTED");
      if (["INTERVIEW_COMPLETE", "SUBMITTED", "UPDATED"].includes(intakeForm.status || "")) {
        document.getElementById("intakePreview").style.display = "block";
        renderIntakeForm();
      }
      if (intakeForm.status === "INTERVIEW_IN_PROGRESS" && allSectionsComplete()) {
        const fin = document.getElementById("finalizeInterviewBtn");
        if (fin) fin.style.display = "inline-block";
      }
      if (intakeForm.status === "INTERVIEW_IN_PROGRESS" && document.getElementById("pearShell")?.classList.contains("active")) {
        renderNav();
      }
      return;
    }
  } catch (_e) {
    /* none */
  }
  intakeForm = null;
  intakeFormId = null;
  assignInterviewState({
    activeSection: 1,
    completedSections: [],
    messagesBySection: {},
    sectionInterviewComplete: {},
    firstPassComplete: false,
  });
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
  } else if (battlecardContainer) {
    battlecardContainer.innerHTML = `
        <div class="battlecard-fallback">
          <div class="battlecard-fallback-item">Medication holds and stop dates reviewed</div>
          <div class="battlecard-fallback-item">NPO and fasting instructions confirmed</div>
          <div class="battlecard-fallback-item">Transport and caregiver plan confirmed</div>
          <div class="battlecard-fallback-item">Consent forms reviewed</div>
        </div>
      `;
  }
  void loadPreopAudio(preop);
}

const preopTeachback = {
  sessionId: null,
  questions: [],
  index: 0,
};

function highlightTeachbackAnchor(container, anchorId) {
  if (!container || !anchorId) return;
  const target = container.querySelector(`#${CSS.escape(anchorId)}`);
  if (!target) return;
  target.classList.remove("teachback-highlight");
  target.scrollIntoView({ behavior: "smooth", block: "center" });
  target.classList.add("teachback-highlight");
  window.setTimeout(() => target.classList.remove("teachback-highlight"), 2300);
}

function renderPreopTeachbackQuestion() {
  const qa = document.getElementById("preopTeachbackQA");
  const progress = document.getElementById("preopTeachbackProgress");
  const question = document.getElementById("preopTeachbackQuestion");
  const answer = document.getElementById("preopTeachbackAnswer");
  const callout = document.getElementById("preopTeachbackCallout");
  if (!qa || !progress || !question || !answer || !callout) return;
  const q = preopTeachback.questions[preopTeachback.index];
  if (!q) {
    qa.style.display = "none";
    return;
  }
  qa.style.display = "grid";
  progress.textContent = `Question ${preopTeachback.index + 1} of ${preopTeachback.questions.length}`;
  const progressFill = document.getElementById("preopTeachbackProgressFill");
  if (progressFill) {
    const pct = preopTeachback.questions.length
      ? ((preopTeachback.index + 1) / preopTeachback.questions.length) * 100
      : 0;
    progressFill.style.width = `${pct}%`;
  }
  question.textContent = q.question || "";
  answer.value = "";
  callout.style.display = "none";
  callout.textContent = "";
}

function showPreopTeachbackAdvisory() {
  const intro = document.getElementById("preopTeachbackIntro");
  const advisory = document.getElementById("preopTeachbackAdvisory");
  const status = document.getElementById("preopTeachbackStatus");
  if (intro) intro.style.display = "none";
  if (advisory) advisory.style.display = "grid";
  if (status) status.textContent = "";
}

function hidePreopTeachbackAdvisory() {
  const intro = document.getElementById("preopTeachbackIntro");
  const advisory = document.getElementById("preopTeachbackAdvisory");
  const startBtn = document.getElementById("preopTeachbackStartBtn");
  if (advisory) advisory.style.display = "none";
  if (intro) intro.style.display = "";
  if (startBtn) startBtn.disabled = false;
}

async function startPreopTeachback() {
  const status = document.getElementById("preopTeachbackStatus");
  const startBtn = document.getElementById("preopTeachbackStartBtn");
  const intro = document.getElementById("preopTeachbackIntro");
  const advisory = document.getElementById("preopTeachbackAdvisory");
  if (!status || !startBtn) return;
  if (advisory) advisory.style.display = "none";
  if (intro) intro.style.display = "none";
  startBtn.disabled = true;
  status.textContent = "Starting teach-back...";
  try {
    const data = await apiJson(`/api/episodes/${encodeURIComponent(PATIENT.id)}/teachback/pre_op/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    preopTeachback.sessionId = data.session_id;
    preopTeachback.questions = Array.isArray(data.questions) ? data.questions : [];
    preopTeachback.index = 0;
    if (data.battlecard_html && document.getElementById("preopBattlecardContainer")) {
      document.getElementById("preopBattlecardContainer").innerHTML = data.battlecard_html;
    }
    status.textContent = "Answer in your own words. You can tap \"I'm not sure\" if needed.";
    renderPreopTeachbackQuestion();
  } catch (err) {
    status.textContent = err.message || "Teach-back is not available yet.";
    if (intro) intro.style.display = "";
    startBtn.disabled = false;
  }
}

async function submitPreopTeachbackAnswer(value) {
  const status = document.getElementById("preopTeachbackStatus");
  const submitBtn = document.getElementById("preopTeachbackSubmitBtn");
  const unsureBtn = document.getElementById("preopTeachbackUnsureBtn");
  const callout = document.getElementById("preopTeachbackCallout");
  const battlecard = document.getElementById("preopBattlecardContainer");
  const q = preopTeachback.questions[preopTeachback.index];
  if (!q || !status || !submitBtn || !unsureBtn || !callout) return;

  submitBtn.disabled = true;
  unsureBtn.disabled = true;
  status.textContent = "Checking your answer...";
  try {
    const data = await apiJson(`/api/episodes/${encodeURIComponent(PATIENT.id)}/teachback/pre_op/answer`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: preopTeachback.sessionId,
        question_id: q.id,
        answer: value,
      }),
    });

    if (data.retry) {
      const locate = data.locate || {};
      status.textContent = "Let's review this part together, then try once more.";
      callout.style.display = "block";
      callout.textContent = locate.transcript_quote || "Review the highlighted section, then answer again.";
      highlightTeachbackAnchor(battlecard, locate.battlecard_anchor);
      return;
    }

    if (data.completed) {
      const aggregate = (data.results || {}).aggregate || {};
      status.textContent = aggregate.failed_red_flag
        ? "Thanks. A care team member should check in with you."
        : "Teach-back complete. Thank you.";
      document.getElementById("preopTeachbackQA").style.display = "none";
      return;
    }

    preopTeachback.index += 1;
    status.textContent = "Recorded. Next question:";
    renderPreopTeachbackQuestion();
  } catch (err) {
    status.textContent = err.message || "Could not submit answer.";
  } finally {
    submitBtn.disabled = false;
    unsureBtn.disabled = false;
  }
}

function setupPreopTeachback() {
  const startBtn = document.getElementById("preopTeachbackStartBtn");
  const continueBtn = document.getElementById("preopTeachbackContinueBtn");
  const backBtn = document.getElementById("preopTeachbackBackBtn");
  const submitBtn = document.getElementById("preopTeachbackSubmitBtn");
  const unsureBtn = document.getElementById("preopTeachbackUnsureBtn");
  const answer = document.getElementById("preopTeachbackAnswer");
  if (!startBtn || !submitBtn || !unsureBtn || !answer) return;
  startBtn.addEventListener("click", () => {
    if (continueBtn) {
      showPreopTeachbackAdvisory();
    } else {
      void startPreopTeachback();
    }
  });
  if (continueBtn) continueBtn.addEventListener("click", () => void startPreopTeachback());
  if (backBtn) backBtn.addEventListener("click", hidePreopTeachbackAdvisory);
  submitBtn.addEventListener("click", () => void submitPreopTeachbackAnswer((answer.value || "").trim()));
  unsureBtn.addEventListener("click", () => void submitPreopTeachbackAnswer("I'm not sure"));
}

async function loadPreopAudio(preop) {
  const btn = document.getElementById("preopPlayPauseBtn");
  if (preop.voice_audio_url) {
    initPreopAudio(preop.voice_audio_url);
    return;
  }
  if (!(preop.voice_script || "").trim()) {
    if (btn) {
      btn.textContent = "⚠ Audio unavailable";
      btn.disabled = true;
    }
    return;
  }
  if (btn) {
    btn.textContent = "Loading audio…";
    btn.disabled = true;
  }
  try {
    const data = await apiJson(`/api/patient/${encodeURIComponent(PATIENT.id)}/preop-audio`);
    if (data.audio_url) {
      if (btn) btn.disabled = false;
      initPreopAudio(data.audio_url);
      return;
    }
  } catch (_e) {
    // Fall through to unavailable state.
  }
  if (btn) {
    btn.textContent = "⚠ Audio unavailable";
    btn.disabled = true;
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
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) close();
  });
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
    const finBtn = document.getElementById("finalizeInterviewBtn");
    if (sendBtn) sendBtn.style.display = "none";
    if (input) input.style.display = "none";
    if (submitBtn) submitBtn.style.display = "none";
    if (saveBtn) saveBtn.style.display = "none";
    if (finBtn) finBtn.style.display = "none";
  }
  const postTab = document.getElementById("postOperationTab");
  if (postTab) postTab.href = `/patient/${PATIENT.id}`;
  const preTab = document.getElementById("preOperationTab");
  if (preTab) preTab.href = `/patient/${PATIENT.id}/pre-op`;
  const companionBtn = document.getElementById("preopCompanionBtn");
  if (companionBtn) companionBtn.href = `/patient/${PATIENT.id}/digital-care-companion`;

  bindSpeedControls();
  loadPreopResources();
  setupPreopTeachback();
  setupNotifyCareTeam();
  loadLatestIntakeForm();

  document.getElementById("startPearBtn").addEventListener("click", async () => {
    document.getElementById("pearShell").classList.add("active");
    if (["INTERVIEW_COMPLETE", "SUBMITTED", "UPDATED"].includes(intakeStatus) && intakeForm) {
      document.getElementById("intakeGlobalActions").style.display = "block";
      document.getElementById("intakePreview").style.display = "block";
      renderIntakeForm();
      return;
    }
    if (!pearStarted) {
      pearStarted = true;
      try {
        if (!intakeFormId) {
          const started = await apiJson("/api/intake-forms/start-interview", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ patientId: PATIENT.id, surgeryId: PATIENT.id }),
          });
          intakeFormId = started.intakeFormId;
          logPatientEvent("intake_started", {});
        }
        const refreshed = await apiJson(`/api/intake-forms/latest/${encodeURIComponent(PATIENT.id)}`);
        intakeForm = refreshed.intake_form;
        assignInterviewState(intakeForm.interview_state || interviewState);
        setStatus(intakeForm.status || "INTERVIEW_IN_PROGRESS");
        document.getElementById("endInterviewBtn").style.display = "inline-block";
      } catch (_e) {
        pearStarted = false;
        const cr = document.getElementById("intakeChatRow");
        if (cr) cr.style.display = "block";
        const wrap = document.getElementById("pearChat");
        if (wrap) wrap.innerHTML = "";
        appendMessage("assistant", "We could not start the intake form. Please refresh and try again.");
        return;
      }
    }
    for (let i = 1; i <= 11; i += 1) {
      if (!sectionCompleted(i)) {
        activeSectionNum = i;
        break;
      }
    }
    renderSectionPane();
  });

  document.getElementById("pearSendBtn").addEventListener("click", submitSectionChat);
  document.getElementById("pearInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submitSectionChat();
    }
  });

  document.getElementById("endInterviewBtn").addEventListener("click", () => {
    const ok = window.confirm("You can return any time to continue. Your progress is saved.");
    if (!ok) return;
    document.getElementById("endInterviewBtn").style.display = "none";
    setStatus("INTERVIEW_IN_PROGRESS");
  });

  document.getElementById("finalizeInterviewBtn").addEventListener("click", async () => {
    if (!intakeFormId || !allSectionsComplete()) return;
    const fp = document.getElementById("finalizeProcessing");
    if (fp) fp.style.display = "block";
    try {
      const completed = await apiJson(`/api/intake-forms/${encodeURIComponent(intakeFormId)}/complete-interview`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          transcript: buildFullTranscriptFromState(),
          duration: Object.values(interviewState.messagesBySection || {}).reduce((a, arr) => a + (arr || []).length, 0) * 20,
        }),
      });
      intakeForm = { ...intakeForm, form_data: completed.formData, red_flags: completed.redFlags, conflicts: completed.conflicts };
      setStatus("INTERVIEW_COMPLETE");
      logPatientEvent("intake_completed", {});
      document.getElementById("finalizeInterviewBtn").style.display = "none";
      document.getElementById("intakePreview").style.display = "block";
      renderIntakeForm();
    } catch (e) {
      window.alert(e.message || "Could not finalize the interview.");
    } finally {
      if (fp) fp.style.display = "none";
    }
  });

  document.getElementById("saveFormBtn").addEventListener("click", async () => {
    try {
      await loadLatestIntakeForm();
      if (intakeForm && intakeStatus === "INTERVIEW_COMPLETE") renderIntakeForm();
    } catch (_e) {
      /* */
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
      await loadLatestIntakeForm();
    } catch (_e) {
      window.alert("Could not submit the form right now.");
    }
  });
});
