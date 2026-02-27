/* ═══════════════════════════════════════════════════════════
   CareGuide Patient Dashboard — app.js
   Two-resource mode: Diagnosis + Treatment tabs
   ═══════════════════════════════════════════════════════════ */

const API_BASE = window.location.origin;

const PATIENT = window.__PATIENT__ || {
  id:           'maria_001',
  name:         'Maria L.',
  firstName:    'Maria',
  procedure:    'Lumpectomy + Sentinel Node Biopsy',
  pipelineType: 'post_op',
  visitDate:    'Jan 30, 2026',
  audioUrl:     null,
  tavusUrl:     null,
  phoneTeam:    '555-867-5309',
  hasResources: false,
};

// ─── Boot ─────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  initPatientInfo();
  initTabs();
  initChat();
  initSuggestedQuestions();
  initFooterActions();
  initQuestionsButton();
  loadResources();
  loadTavusAvatar();
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
  if (callBtn && PATIENT.phoneTeam) callBtn.href = `tel:${PATIENT.phoneTeam}`;
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
  document.getElementById('questionsBtn')?.addEventListener('click', showCareGuide);
}

function showCareGuide() {
  const section = document.getElementById('careGuideSection');
  if (section) {
    section.style.display = 'block';
    section.scrollIntoView({ behavior: 'smooth', block: 'start' });
    setTimeout(() => document.getElementById('chatInput')?.focus(), 600);
  }
}

// ─── Load Resources ───────────────────────────────────────────
async function loadResources() {
  try {
    const res = await fetch(`${API_BASE}/api/patient/${PATIENT.id}/resources`);
    if (!res.ok) throw new Error('No resources');
    const data = await res.json();

    // Diagnosis
    if (data.diagnosis) {
      const dc = document.getElementById('diagBattlecardContainer');
      if (dc) dc.innerHTML = data.diagnosis.battlecard_html || '<p style="padding:24px;color:#6B7280;">No battlecard available.</p>';
      if (data.diagnosis.voice_audio_url) {
        initAudioPlayer('diag', data.diagnosis.voice_audio_url);
      }
    }

    // Treatment
    if (data.treatment) {
      const tc = document.getElementById('treatBattlecardContainer');
      if (tc) tc.innerHTML = data.treatment.battlecard_html || '<p style="padding:24px;color:#6B7280;">No battlecard available.</p>';
      if (data.treatment.voice_audio_url) {
        initAudioPlayer('treat', data.treatment.voice_audio_url);
      }
    }

  } catch {
    // No two-resource data — show fallback demo content
    const dc = document.getElementById('diagBattlecardContainer');
    if (dc) dc.innerHTML = getDemoDiagnosisBattlecard();
    const tc = document.getElementById('treatBattlecardContainer');
    if (tc) tc.innerHTML = getDemoTreatmentBattlecard();
  }
}

// ─── Audio Player (reusable for both tabs) ────────────────────
function initAudioPlayer(prefix, audioUrl) {
  const playPauseBtn = document.getElementById(`${prefix}PlayPauseBtn`);
  const playBtnLarge = document.getElementById(`${prefix}PlayBtnLarge`);
  const overlay      = document.getElementById(`${prefix}VideoOverlay`);
  const progressFill = document.getElementById(`${prefix}ProgressFill`);
  const timeDisplay  = document.getElementById(`${prefix}TimeDisplay`);
  const progressBar  = document.getElementById(`${prefix}ProgressBar`);

  const audio = new Audio(audioUrl);
  let isPlaying = false;

  audio.addEventListener('loadedmetadata', () => {
    if (timeDisplay) timeDisplay.textContent = `0:00 / ${fmtTime(audio.duration)}`;
  });
  audio.addEventListener('timeupdate', () => {
    if (isNaN(audio.duration)) return;
    const pct = (audio.currentTime / audio.duration) * 100;
    if (progressFill) progressFill.style.width = `${pct}%`;
    if (timeDisplay) timeDisplay.textContent = `${fmtTime(audio.currentTime)} / ${fmtTime(audio.duration)}`;
  });
  audio.addEventListener('ended', () => setPlaying(false));

  function togglePlay() { isPlaying ? pause() : play(); }
  function play() { audio.play(); setPlaying(true); if (overlay) overlay.style.opacity = '0'; }
  function pause() { audio.pause(); setPlaying(false); if (overlay) overlay.style.opacity = '1'; }
  function setPlaying(val) {
    isPlaying = val;
    if (playPauseBtn) playPauseBtn.textContent = val ? '⏸ Pause' : '▶ Play';
    if (playBtnLarge) playBtnLarge.textContent = val ? '⏸' : '▶';
  }

  [playBtnLarge, playPauseBtn].forEach(el => el?.addEventListener('click', togglePlay));
  progressBar?.addEventListener('click', e => {
    const rect = progressBar.getBoundingClientRect();
    audio.currentTime = ((e.clientX - rect.left) / rect.width) * audio.duration;
  });
}

function fmtTime(sec) {
  if (isNaN(sec)) return '0:00';
  const m = Math.floor(sec / 60);
  const s = String(Math.floor(sec % 60)).padStart(2, '0');
  return `${m}:${s}`;
}

// ─── Tavus Avatar ─────────────────────────────────────────────
function loadTavusAvatar() {
  const container = document.getElementById('tavusContainer');
  if (!container || !PATIENT.tavusUrl) return;
  const iframe = document.createElement('iframe');
  iframe.src = PATIENT.tavusUrl;
  iframe.allow = 'camera; microphone; autoplay';
  iframe.allowFullscreen = true;
  container.innerHTML = '';
  container.appendChild(iframe);
}

// ─── Suggested Questions ──────────────────────────────────────
function initSuggestedQuestions() {
  document.querySelectorAll('.suggestion-chip').forEach(chip => {
    chip.addEventListener('click', () => {
      document.getElementById('chatInput').value = chip.dataset.q;
      sendMessage();
      document.getElementById('suggestedQuestions').style.display = 'none';
    });
  });
}

// ─── Chat ──────────────────────────────────────────────────────
function initChat() {
  const input = document.getElementById('chatInput');
  const sendBtn = document.getElementById('sendBtn');
  sendBtn?.addEventListener('click', sendMessage);
  input?.addEventListener('keypress', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });
}

async function sendMessage() {
  const input = document.getElementById('chatInput');
  const text = input?.value.trim();
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
        patient_id: PATIENT.id,
        message: text,
        conversation_history: getChatHistory(),
      }),
    });
    if (!res.ok) throw new Error('API error');
    const data = await res.json();
    typingEl.remove();
    appendMessage(data.response, 'assistant');
  } catch {
    typingEl.remove();
    appendMessage("I'm having trouble connecting right now. For urgent questions, please call your care team.", 'assistant');
  }
}

function appendMessage(text, role) {
  const messages = document.getElementById('chatMessages');
  const el = document.createElement('div');
  el.className = `message ${role}-message`;
  const time = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  el.innerHTML = `<div class="message-bubble">${escapeHtml(text)}</div><span class="message-time">${time}</span>`;
  messages.appendChild(el);
  messages.scrollTop = messages.scrollHeight;
}

function showTyping() {
  const messages = document.getElementById('chatMessages');
  const el = document.createElement('div');
  el.className = 'message assistant-message typing-indicator';
  el.innerHTML = '<div class="message-bubble"><span class="typing-dot"></span><span class="typing-dot"></span><span class="typing-dot"></span></div>';
  messages.appendChild(el);
  messages.scrollTop = messages.scrollHeight;
  return el;
}

function getChatHistory() {
  return Array.from(document.querySelectorAll('#chatMessages .message:not(.typing-indicator)'))
    .slice(1)
    .map(msg => ({
      role: msg.classList.contains('patient-message') ? 'user' : 'assistant',
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

// ─── Demo Battlecards (fallback when no resources generated) ──

function getDemoDiagnosisBattlecard() {
  return `
<div style="font-family:'Inter',sans-serif;padding:24px;max-width:820px;margin:0 auto;color:#1F2937;font-size:14px;line-height:1.55">
  <div style="background:linear-gradient(135deg,#0F172A,#1E3A5F);color:#fff;padding:20px 24px;border-radius:10px;margin-bottom:20px">
    <h2 style="font-size:18px;font-weight:700;margin-bottom:4px">Understanding Your Diagnosis</h2>
    <p style="font-size:13px;opacity:.8">Lumpectomy + Sentinel Node Biopsy</p>
    <div style="margin-top:10px;background:rgba(255,255,255,.15);padding:8px 14px;border-radius:6px;font-size:13px">Maria L. · Jan 30, 2026</div>
  </div>
  <div style="margin-bottom:20px">
    <h3 style="font-size:15px;font-weight:700;margin-bottom:10px">🔍 Your Diagnoses</h3>
    <div style="background:#F9FAFB;border-left:4px solid #2563EB;border-radius:8px;padding:14px 18px;margin-bottom:10px">
      <div style="font-weight:700;margin-bottom:4px">Triple-Negative Breast Cancer</div>
      <div style="font-size:13px;color:#4B5563">A type of breast cancer where the cells don't have three common receptors. This means certain hormone therapies won't work, but other treatments are very effective.</div>
    </div>
    <div style="background:#F9FAFB;border-left:4px solid #7C3AED;border-radius:8px;padding:14px 18px;margin-bottom:10px">
      <div style="font-weight:700;margin-bottom:4px">BRCA1 Mutation (Germline)</div>
      <div style="font-size:13px;color:#4B5563">A genetic change you were born with — not caused by anything you did. This mutation is why olaparib is part of your treatment plan, as it specifically targets cells with this mutation.</div>
    </div>
  </div>
  <div style="margin-bottom:20px">
    <h3 style="font-size:15px;font-weight:700;margin-bottom:10px">🏥 What Was Done</h3>
    <p style="font-size:14px;color:#374151;line-height:1.6">A lumpectomy removed the tumor and a small area of surrounding tissue. A sentinel node biopsy checked the nearest lymph nodes to see if cancer had spread. The goal: remove the cancer while preserving as much of your breast as possible.</p>
  </div>
  <div style="background:#EFF6FF;border:1px solid #BFDBFE;border-radius:8px;padding:16px 20px">
    <h3 style="font-size:14px;font-weight:700;color:#1D4ED8;margin-bottom:8px">❓ Questions for Your Next Visit</h3>
    <ul style="font-size:13px;color:#1E40AF;margin-left:16px">
      <li style="margin-bottom:4px">How did the sentinel node biopsy results look?</li>
      <li style="margin-bottom:4px">What does BRCA1 mean for my family members?</li>
      <li style="margin-bottom:4px">How will we know if olaparib is working?</li>
    </ul>
  </div>
</div>`;
}

function getDemoTreatmentBattlecard() {
  return `
<div style="font-family:'Inter',sans-serif;padding:24px;max-width:820px;margin:0 auto;color:#1F2937;font-size:14px;line-height:1.55">
  <div style="background:linear-gradient(135deg,#1A3C8F,#2563EB);color:#fff;padding:20px 24px;border-radius:10px;margin-bottom:20px">
    <h2 style="font-size:18px;font-weight:700;margin-bottom:4px">Your Treatment & Recovery Action Card</h2>
    <p style="font-size:13px;opacity:.8">Post-Surgery Discharge</p>
    <div style="margin-top:10px;background:rgba(255,255,255,.15);padding:8px 14px;border-radius:6px;font-size:13px">Maria L. · Lumpectomy · Jan 30, 2026</div>
  </div>
  <div style="background:#FFF7ED;border:1px solid #FED7AA;border-left:4px solid #F97316;border-radius:8px;padding:16px 20px;margin-bottom:20px">
    <h3 style="font-size:14px;font-weight:700;color:#92400E;margin-bottom:6px">⭐ The ONE Most Important Thing</h3>
    <p style="font-size:14px;color:#78350F">Take <strong>olaparib 300mg twice daily</strong>, 12 hours apart, every single day. Never double up on a missed dose.</p>
  </div>
  <div style="margin-bottom:20px">
    <h3 style="font-size:15px;font-weight:700;margin-bottom:12px">💊 Your Medications</h3>
    <div style="background:#F9FAFB;border-radius:8px;border-left:4px solid #7C3AED;padding:12px 16px;margin-bottom:10px">
      <div style="font-weight:700">Olaparib <span style="background:#EDE9FE;color:#5B21B6;font-size:11px;padding:2px 8px;border-radius:10px;font-weight:600">CONTINUE</span></div>
      <div style="font-size:13px;color:#4B5563">300mg · oral · twice daily (12 hrs apart)</div>
      <div style="font-size:12px;color:#B45309;margin-top:6px;font-weight:500">⚠ If you miss a dose, skip it. Never double up.</div>
    </div>
    <div style="background:#F9FAFB;border-radius:8px;border-left:4px solid #10B981;padding:12px 16px;margin-bottom:10px">
      <div style="font-weight:700">Ondansetron <span style="background:#D1FAE5;color:#065F46;font-size:11px;padding:2px 8px;border-radius:10px;font-weight:600">FOR NAUSEA</span></div>
      <div style="font-size:13px;color:#4B5563">8mg · oral · every 8 hours as needed</div>
    </div>
    <div style="background:#F9FAFB;border-radius:8px;border-left:4px solid #10B981;padding:12px 16px;margin-bottom:10px">
      <div style="font-weight:700">Loperamide <span style="background:#D1FAE5;color:#065F46;font-size:11px;padding:2px 8px;border-radius:10px;font-weight:600">FOR DIARRHEA</span></div>
      <div style="font-size:13px;color:#4B5563">2mg after first loose stool, max 8mg/day</div>
    </div>
  </div>
  <div style="background:#FFFBEB;border:1px solid #FCD34D;border-radius:8px;padding:14px 18px;margin-bottom:14px">
    <h4 style="color:#92400E;font-size:13px;font-weight:700;margin-bottom:8px">⚠ Call Your Doctor Today If</h4>
    <ul style="font-size:13px;color:#78350F;margin-left:16px"><li style="margin-bottom:3px">Fever of 100.4°F or higher</li><li style="margin-bottom:3px">Vomiting that won't stop</li><li style="margin-bottom:3px">New bleeding or easy bruising</li><li>Worsening fatigue or racing heartbeat</li></ul>
  </div>
  <div style="background:#FEF2F2;border:1px solid #FECACA;border-radius:8px;padding:14px 18px;margin-bottom:14px">
    <h4 style="color:#7F1D1D;font-size:13px;font-weight:700;margin-bottom:8px">🚨 Go to ER Immediately If</h4>
    <ul style="font-size:13px;color:#991B1B;margin-left:16px"><li style="margin-bottom:3px">Shortness of breath or chest pain</li><li style="margin-bottom:3px">Severe dizziness or feeling faint</li><li>Can't keep any fluids down</li></ul>
  </div>
</div>`;
}
