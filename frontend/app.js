/* ═══════════════════════════════════════════════════════════
   CareGuide Patient Dashboard — app.js
   ═══════════════════════════════════════════════════════════ */

// ─── Config ──────────────────────────────────────────────────
const API_BASE = window.location.hostname === 'localhost'
  ? 'http://localhost:8000'
  : '';

// Patient context is injected by the server at page load.
// For demo purposes, we fall back to this static object.
const PATIENT = window.__PATIENT__ || {
  id:            '00877123',
  name:          'Maria L.',
  firstName:     'Maria',
  procedure:     'Lumpectomy + Sentinel Node Biopsy',
  pipelineType:  'post_op',   // 'pre_op' | 'post_op'
  visitDate:     'Jan 30, 2026',
  audioUrl:      null,         // Set after ElevenLabs synthesis
  tavusUrl:      null,         // Set after Tavus conversation created
  phoneTeam:     '555-867-5309',
};

// ─── Boot ─────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  initPatientInfo();
  initTabs();
  initVideoPlayer();
  initChat();
  initSuggestedQuestions();
  initFooterActions();
  initQuestionsButton();
  loadBattlecard();
  loadTavusAvatar();
  loadDischargeInstructions();
});

// ─── Patient Info ─────────────────────────────────────────────
function initPatientInfo() {
  setText('patientName',   PATIENT.name);
  setText('visitDate',     PATIENT.visitDate);
  setText('cardSubtitle',  PATIENT.procedure);

  if (PATIENT.pipelineType === 'pre_op') {
    setText('cardTitle', 'Pre-Surgery Preparation Plan');
  }

  const callBtn = document.getElementById('callTeamBtn');
  if (callBtn && PATIENT.phoneTeam) {
    callBtn.href = `tel:${PATIENT.phoneTeam}`;
  }
}

// ─── Tabs ─────────────────────────────────────────────────────
function initTabs() {
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const target = btn.dataset.tab;

      document.querySelectorAll('.tab-btn').forEach(b => {
        b.classList.remove('active');
        b.setAttribute('aria-selected', 'false');
      });
      document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));

      btn.classList.add('active');
      btn.setAttribute('aria-selected', 'true');
      document.getElementById(`tab-${target}`).classList.add('active');
    });
  });
}

// ─── "Any further questions?" Button ──────────────────────────
function initQuestionsButton() {
  const btn = document.getElementById('questionsBtn');
  btn?.addEventListener('click', showCareGuide);
}

function showCareGuide() {
  const section = document.getElementById('careGuideSection');
  if (section) {
    section.style.display = 'block';
    section.scrollIntoView({ behavior: 'smooth', block: 'start' });
    setTimeout(() => document.getElementById('chatInput')?.focus(), 600);
  }
}

// ─── Video Player ─────────────────────────────────────────────
function initVideoPlayer() {
  const playPauseBtn   = document.getElementById('playPauseBtn');
  const playBtnLarge   = document.getElementById('playBtnLarge');
  const videoOverlay   = document.getElementById('videoOverlay');
  const progressFill   = document.getElementById('progressFill');
  const timeDisplay    = document.getElementById('timeDisplay');
  const progressBar    = document.getElementById('progressBar');

  let audio     = null;
  let isPlaying = false;
  let duration  = 270; // 4:30 default for demo

  if (PATIENT.audioUrl) {
    audio = new Audio(PATIENT.audioUrl);
    audio.addEventListener('loadedmetadata', () => { duration = audio.duration; });
    audio.addEventListener('timeupdate',     updateProgress);
    audio.addEventListener('ended',          () => setPlaying(false));
  }

  [playBtnLarge, playPauseBtn].forEach(el => el?.addEventListener('click', togglePlay));

  progressBar?.addEventListener('click', e => {
    if (!audio) return;
    const rect = progressBar.getBoundingClientRect();
    const pct  = (e.clientX - rect.left) / rect.width;
    audio.currentTime = pct * audio.duration;
  });

  function togglePlay() {
    isPlaying ? pause() : play();
  }

  function play() {
    audio?.play();
    setPlaying(true);
    if (videoOverlay) videoOverlay.style.opacity = '0';
  }

  function pause() {
    audio?.pause();
    setPlaying(false);
    if (videoOverlay) videoOverlay.style.opacity = '1';
  }

  function setPlaying(val) {
    isPlaying = val;
    if (playPauseBtn) playPauseBtn.textContent = val ? '⏸ Pause' : '▶ Play';
    if (playBtnLarge) playBtnLarge.textContent  = val ? '⏸' : '▶';
  }

  function updateProgress() {
    if (!audio || isNaN(audio.duration)) return;
    const pct = (audio.currentTime / audio.duration) * 100;
    if (progressFill) progressFill.style.width = `${pct}%`;
    if (timeDisplay)  timeDisplay.textContent  =
      `${fmtTime(audio.currentTime)} / ${fmtTime(audio.duration)}`;
  }
}

function fmtTime(sec) {
  if (isNaN(sec)) return '0:00';
  const m = Math.floor(sec / 60);
  const s = String(Math.floor(sec % 60)).padStart(2, '0');
  return `${m}:${s}`;
}

// ─── Battlecard ───────────────────────────────────────────────
async function loadBattlecard() {
  const container = document.getElementById('battlecardContainer');
  if (!container) return;

  try {
    const res = await fetch(`${API_BASE}/api/patient/${PATIENT.id}/battlecard`);
    if (!res.ok) throw new Error('Not found');
    const data = await res.json();
    if (!data.html) throw new Error('Empty battlecard');
    container.innerHTML = data.html;
  } catch {
    container.innerHTML = getDemoBattlecard();
  }
}

// ─── Discharge Instructions ───────────────────────────────────
async function loadDischargeInstructions() {
  const container = document.getElementById('dischargeContainer');
  if (!container) return;

  try {
    const res = await fetch(`${API_BASE}/api/patient/${PATIENT.id}/discharge`);
    if (!res.ok) throw new Error('Not found');
    const data = await res.json();
    container.innerHTML = renderDischargeInstructions(data.structured_data, data.voice_script);
  } catch {
    container.innerHTML = '<p style="padding:24px;color:#6B7280;">Discharge instructions are not yet available.</p>';
  }
}

function renderDischargeInstructions(sd, voiceScript) {
  const medsHtml = (sd.medications || []).map(m => `
    <div class="di-med-card">
      <div class="di-med-name">${esc(m.name)}</div>
      <div class="di-med-detail">${esc(m.dose)} · ${esc(m.route)} · ${esc(m.frequency)}</div>
      ${m.notes ? `<div class="di-med-notes">${esc(m.notes)}</div>` : ''}
    </div>
  `).join('');

  const redFlagsHtml = (sd.red_flags || []).map(f =>
    `<li>${esc(f)}</li>`
  ).join('');

  const normalHtml = (sd.normal_symptoms || []).map(s =>
    `<li>${esc(s)}</li>`
  ).join('');

  const diagnosesHtml = (sd.key_diagnoses || []).map(d =>
    `<li>${esc(d)}</li>`
  ).join('');

  const followUp = sd.follow_up || {};

  return `
<div class="di">
  <div class="di-header">
    <h2>Discharge Instructions</h2>
    <p>${esc(sd.patient_name || '')} · ${esc(sd.procedure_name || '')}</p>
  </div>

  <div class="di-section">
    <h3>📋 Diagnoses</h3>
    <ul>${diagnosesHtml}</ul>
  </div>

  <div class="di-section">
    <h3>⚠️ Allergies</h3>
    <p>${esc((sd.allergies || []).join(', ') || 'None reported')}</p>
  </div>

  <div class="di-section">
    <h3>💊 Medications</h3>
    ${medsHtml}
  </div>

  <div class="di-section">
    <h3>📝 Post-Operative Instructions</h3>
    <p>${esc(sd.post_op_instructions || sd.pre_op_instructions || '')}</p>
  </div>

  <div class="di-section di-red-flags">
    <h3>🚨 Warning Signs — When to Call</h3>
    <ul>${redFlagsHtml}</ul>
  </div>

  <div class="di-section di-normal">
    <h3>ℹ️ What's Normal After Surgery</h3>
    <ul>${normalHtml}</ul>
  </div>

  <div class="di-section">
    <h3>📅 Follow-Up Appointment</h3>
    <p><strong>${esc(followUp.date || 'TBD')}</strong> — ${esc(followUp.provider || '')}</p>
    ${followUp.notes ? `<p class="di-followup-notes">${esc(followUp.notes)}</p>` : ''}
  </div>

  <div class="di-section di-primary">
    <h3>⭐ Primary Concern</h3>
    <p>${esc(sd.primary_concern || '')}</p>
  </div>

  ${voiceScript ? `
  <div class="di-section">
    <h3>🎧 Your Recovery Message</h3>
    <p class="di-voice-script">${esc(voiceScript)}</p>
  </div>` : ''}
</div>`;
}

function esc(str) {
  const d = document.createElement('div');
  d.appendChild(document.createTextNode(str || ''));
  return d.innerHTML;
}

// ─── Tavus Avatar ─────────────────────────────────────────────
function loadTavusAvatar() {
  const container = document.getElementById('tavusContainer');
  if (!container || !PATIENT.tavusUrl) return;

  const iframe = document.createElement('iframe');
  iframe.src   = PATIENT.tavusUrl;
  iframe.allow = 'camera; microphone; autoplay';
  iframe.allowFullscreen = true;
  container.innerHTML = '';
  container.appendChild(iframe);
}

// ─── Suggested Questions ──────────────────────────────────────
function initSuggestedQuestions() {
  document.querySelectorAll('.suggestion-chip').forEach(chip => {
    chip.addEventListener('click', () => {
      const q = chip.dataset.q;
      document.getElementById('chatInput').value = q;
      sendMessage();
      document.getElementById('suggestedQuestions').style.display = 'none';
    });
  });
}

// ─── Chat ──────────────────────────────────────────────────────
function initChat() {
  const input   = document.getElementById('chatInput');
  const sendBtn = document.getElementById('sendBtn');

  sendBtn?.addEventListener('click', sendMessage);
  input?.addEventListener('keypress', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });
}

async function sendMessage() {
  const input = document.getElementById('chatInput');
  const text  = input?.value.trim();
  if (!text) return;

  input.value = '';
  document.getElementById('suggestedQuestions').style.display = 'none';

  appendMessage(text, 'patient');
  const typingEl = showTyping();

  try {
    const res = await fetch(`${API_BASE}/api/avatar/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        patient_id:           PATIENT.id,
        message:              text,
        conversation_history: getChatHistory(),
      }),
    });

    if (!res.ok) throw new Error('API error');
    const data = await res.json();
    typingEl.remove();
    appendMessage(data.response, 'assistant');
  } catch {
    typingEl.remove();
    appendMessage(
      "I'm having trouble connecting right now. For urgent questions, please call your care team.",
      'assistant',
    );
  }
}

function appendMessage(text, role) {
  const messages = document.getElementById('chatMessages');
  const el       = document.createElement('div');
  el.className   = `message ${role}-message`;

  const time = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  el.innerHTML = `
    <div class="message-bubble">${escapeHtml(text)}</div>
    <span class="message-time">${time}</span>
  `;
  messages.appendChild(el);
  messages.scrollTop = messages.scrollHeight;
}

function showTyping() {
  const messages = document.getElementById('chatMessages');
  const el       = document.createElement('div');
  el.className   = 'message assistant-message typing-indicator';
  el.innerHTML   = `
    <div class="message-bubble">
      <span class="typing-dot"></span>
      <span class="typing-dot"></span>
      <span class="typing-dot"></span>
    </div>`;
  messages.appendChild(el);
  messages.scrollTop = messages.scrollHeight;
  return el;
}

function getChatHistory() {
  return Array.from(
    document.querySelectorAll('#chatMessages .message:not(.typing-indicator)'),
  )
    .slice(1) // skip initial greeting
    .map(msg => ({
      role:    msg.classList.contains('patient-message') ? 'user' : 'assistant',
      content: msg.querySelector('.message-bubble').textContent.trim(),
    }));
}

// ─── Footer ───────────────────────────────────────────────────
function initFooterActions() {
  document.getElementById('openAvatarBtn')?.addEventListener('click', showCareGuide);
}

// ─── Helpers ──────────────────────────────────────────────────
function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

function escapeHtml(str) {
  const d = document.createElement('div');
  d.appendChild(document.createTextNode(str));
  return d.innerHTML;
}

// ─── Demo Battlecard (embedded fallback) ──────────────────────
function getDemoBattlecard() {
  return `
<style>
  .bc{font-family:'Inter',sans-serif;padding:24px;max-width:820px;margin:0 auto;color:#1F2937;font-size:14px;line-height:1.55}
  .bc-head{background:linear-gradient(135deg,#1A3C8F,#2563EB);color:#fff;padding:20px 24px;border-radius:10px;margin-bottom:20px}
  .bc-head h1{font-size:18px;font-weight:700;margin-bottom:4px}
  .bc-head p{font-size:13px;opacity:.8}
  .bc-head .bc-patient{margin-top:10px;background:rgba(255,255,255,.15);padding:8px 14px;border-radius:6px;font-size:13px;font-weight:500}
  .bc-priority{background:#FFF7ED;border:1px solid #FED7AA;border-left:4px solid #F97316;border-radius:8px;padding:16px 20px;margin-bottom:20px}
  .bc-priority h2{font-size:14px;font-weight:700;color:#92400E;margin-bottom:6px}
  .bc-priority p{font-size:14px;color:#78350F}
  .bc-section{margin-bottom:20px}
  .bc-section h3{font-size:14px;font-weight:700;color:#1F2937;margin-bottom:12px;display:flex;align-items:center;gap:8px}
  .med-card{background:#F9FAFB;border-radius:8px;border-left:4px solid #2563EB;padding:12px 16px;margin-bottom:10px}
  .med-card.new{border-left-color:#7C3AED}
  .med-card.support{border-left-color:#10B981}
  .med-name{font-weight:700;font-size:14px;color:#1F2937;margin-bottom:2px}
  .med-dose{font-size:13px;color:#4B5563;margin-bottom:4px}
  .med-tag{display:inline-block;font-size:11px;font-weight:600;padding:2px 8px;border-radius:10px;margin-right:6px}
  .tag-new{background:#EDE9FE;color:#5B21B6}
  .tag-support{background:#D1FAE5;color:#065F46}
  .med-warn{font-size:12px;color:#B45309;margin-top:6px;font-weight:500}
  .timeline{position:relative;padding-left:28px}
  .timeline::before{content:'';position:absolute;left:10px;top:6px;bottom:6px;width:2px;background:#E5E7EB}
  .tl-item{position:relative;margin-bottom:16px}
  .tl-dot{position:absolute;left:-28px;top:2px;width:20px;height:20px;background:#2563EB;border-radius:50%;display:flex;align-items:center;justify-content:center;color:#fff;font-size:10px;font-weight:700;border:2px solid #fff;box-shadow:0 0 0 2px #2563EB}
  .tl-label{font-weight:700;font-size:13px;color:#2563EB;margin-bottom:3px}
  .tl-content{font-size:13px;color:#374151}
  .do-dont{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  .can-do{background:#F0FDF4;border:1px solid #BBF7D0;border-radius:8px;padding:14px}
  .cant-do{background:#FFF1F2;border:1px solid #FECDD3;border-radius:8px;padding:14px}
  .can-do h4{color:#065F46;font-size:13px;font-weight:700;margin-bottom:8px}
  .cant-do h4{color:#9F1239;font-size:13px;font-weight:700;margin-bottom:8px}
  .can-do li,.cant-do li{font-size:13px;margin-left:16px;margin-bottom:4px}
  .can-do li{color:#14532D} .cant-do li{color:#881337}
  .normal-box{background:#EFF6FF;border:1px solid #BFDBFE;border-radius:8px;padding:14px;margin-bottom:14px}
  .normal-box h4{color:#1D4ED8;font-size:13px;font-weight:700;margin-bottom:8px}
  .normal-box li{font-size:13px;color:#1E40AF;margin-left:16px;margin-bottom:3px}
  .call-box{background:#FFFBEB;border:1px solid #FCD34D;border-radius:8px;padding:14px;margin-bottom:14px}
  .call-box h4{color:#92400E;font-size:13px;font-weight:700;margin-bottom:8px}
  .call-box li{font-size:13px;color:#78350F;margin-left:16px;margin-bottom:3px}
  .er-box{background:#FEF2F2;border:1px solid #FECACA;border-radius:8px;padding:14px;margin-bottom:14px}
  .er-box h4{color:#7F1D1D;font-size:13px;font-weight:700;margin-bottom:8px}
  .er-box li{font-size:13px;color:#991B1B;margin-left:16px;margin-bottom:3px}
  .contact-box{background:#F9FAFB;border:1px solid #E5E7EB;border-radius:8px;padding:16px;text-align:center}
  .contact-box p{font-size:13px;color:#6B7280;margin-bottom:6px}
  .contact-box strong{font-size:15px;color:#1F2937}
</style>

<div class="bc">
  <div class="bc-head">
    <h1>Adjuvant Therapy After Lumpectomy</h1>
    <p>Post-Surgery Discharge Quick Action Card</p>
    <div class="bc-patient">Maria L. · MRN 00877123 · Jan 30, 2026</div>
  </div>

  <div class="bc-priority">
    <h2>⭐ The ONE Most Important Thing</h2>
    <p>
      Take <strong>olaparib exactly as prescribed</strong> — 300 mg in the morning and 300 mg in the evening,
      every day, about 12 hours apart. This medication is your protection against cancer returning.
      Do <strong>not</strong> double-up on a missed dose.
    </p>
  </div>

  <div class="bc-section">
    <h3>💊 Your Medications — Take Exactly As Prescribed</h3>
    <div class="med-card new">
      <div class="med-name">Olaparib <span class="med-tag tag-new">NEW</span></div>
      <div class="med-dose">300 mg · by mouth · twice daily (morning + evening, ~12 hrs apart)</div>
      <div class="med-warn">⚠ Take on a schedule — same times every day · Do NOT double up if you miss a dose</div>
    </div>
    <div class="med-card support">
      <div class="med-name">Ondansetron <span class="med-tag tag-support">FOR NAUSEA</span></div>
      <div class="med-dose">8 mg · by mouth · every 8 hours as needed</div>
    </div>
    <div class="med-card support">
      <div class="med-name">Loperamide <span class="med-tag tag-support">FOR DIARRHEA</span></div>
      <div class="med-dose">2 mg after first loose stool, then 2 mg after each loose stool · max 8 mg/day</div>
    </div>
  </div>

  <div class="bc-section">
    <h3>📅 What Happens Next</h3>
    <div class="timeline">
      <div class="tl-item">
        <div class="tl-dot">1</div>
        <div class="tl-label">Today</div>
        <div class="tl-content">Start olaparib 300 mg AM + 300 mg PM · take supportive meds if needed</div>
      </div>
      <div class="tl-item">
        <div class="tl-dot">2</div>
        <div class="tl-label">Every 4 Weeks (×3 months)</div>
        <div class="tl-content">CBC + CMP blood work to monitor your counts</div>
      </div>
      <div class="tl-item">
        <div class="tl-dot">3</div>
        <div class="tl-label">Week 4 Follow-Up</div>
        <div class="tl-content">Oncology clinic appointment — call earlier if symptoms arise</div>
      </div>
      <div class="tl-item">
        <div class="tl-dot">✓</div>
        <div class="tl-label">12 Months (if tolerated)</div>
        <div class="tl-content">Planned end of olaparib therapy — reassess with your team</div>
      </div>
    </div>
  </div>

  <div class="bc-section">
    <h3>✓ What You Can / Can't Do</h3>
    <div class="do-dont">
      <div class="can-do">
        <h4>✓ You CAN</h4>
        <ul>
          <li>Take olaparib with or without food</li>
          <li>Use ondansetron when nausea hits</li>
          <li>Call your team any time — day or night</li>
          <li>Share this card with your family</li>
        </ul>
      </div>
      <div class="cant-do">
        <h4>✗ Do NOT</h4>
        <ul>
          <li>Double-up on a missed dose</li>
          <li>Start new supplements without oncology approval</li>
          <li>Skip contraception during therapy</li>
          <li>Wait if you have a fever ≥ 100.4°F</li>
        </ul>
      </div>
    </div>
  </div>

  <div class="bc-section">
    <h3>🩺 Know Your Symptoms</h3>
    <div class="normal-box">
      <h4>ℹ What's Normal After Surgery</h4>
      <ul>
        <li>Mild fatigue (your Hgb is slightly low — 10.6 — so this is expected)</li>
        <li>Occasional mild nausea · occasional loose stools</li>
        <li>Minor tenderness at the surgical site</li>
      </ul>
    </div>
    <div class="call-box">
      <h4>⚠ Call Your Doctor Today If</h4>
      <ul>
        <li>Fever of 100.4°F (38°C) or higher</li>
        <li>Vomiting that won't stop · diarrhea that doesn't improve</li>
        <li>New bleeding or easy bruising</li>
        <li>Worsening fatigue, lightheadedness, or racing heartbeat</li>
      </ul>
    </div>
    <div class="er-box">
      <h4>🚨 Go to ER Immediately If</h4>
      <ul>
        <li>Shortness of breath or chest pain</li>
        <li>Severe dizziness, weakness, or feeling faint</li>
        <li>Can't keep any fluids down</li>
        <li>Something feels severe or scary — don't wait</li>
      </ul>
    </div>
  </div>

  <div class="contact-box">
    <p>📞 You are <strong>never</strong> bothering us. Calling early prevents bigger problems.</p>
    <p style="margin-top:8px">Call your oncology team any time — including nights and weekends.</p>
    <p style="margin-top:10px;font-size:12px;color:#9CA3AF">Tell them you are on cancer treatment when calling the ER.</p>
  </div>
</div>`;
}
