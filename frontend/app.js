/* ═══════════════════════════════════════════════════════════
   Archangel Health Patient Dashboard — app.js
   All resource data is injected into window.__PATIENT__ by the server.
   No secondary API calls needed for battlecards or audio.
   ═══════════════════════════════════════════════════════════ */

const PATIENT = window.__PATIENT__ || {
  id:           'maria_001',
  name:         'Maria L.',
  firstName:    'Maria',
  procedure:    'Lumpectomy + Sentinel Node Biopsy',
  pipelineType: 'post_op',
  visitDate:    'Jan 30, 2026',
  audioUrl:     null,
  tavusUrl:     null,
  phoneTeam:    '',
  hasResources: false,
  resources:    null,
};

document.addEventListener('DOMContentLoaded', () => {
  initPatientInfo();
  initOverlays();
  initChat();
  initSuggestedQuestions();
  initFooterActions();
  initQuestionsButton();
  loadResources();
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
  const overlay = document.getElementById('callTeamOverlay');
  if (callBtn && overlay) {
    callBtn.addEventListener('click', function () {
      const phoneDisplay = document.getElementById('callTeamPhoneDisplay');
      const telLink = document.getElementById('callTeamTelLink');
      const phone = PATIENT.phoneTeam || '';
      if (phone) {
        phoneDisplay.textContent = phone;
        telLink.href = 'tel:' + phone.replace(/\s/g, '');
        telLink.style.display = '';
      } else {
        phoneDisplay.textContent = 'No office phone on file. Please contact your care team via your discharge paperwork.';
        telLink.style.display = 'none';
      }
      overlay.classList.add('is-open');
      overlay.setAttribute('aria-hidden', 'false');
      document.body.style.overflow = 'hidden';
    });
  }
  document.getElementById('callTeamClose')?.addEventListener('click', function () {
    const o = document.getElementById('callTeamOverlay');
    if (o) { o.classList.remove('is-open'); o.setAttribute('aria-hidden', 'true'); document.body.style.overflow = ''; }
  });
  document.getElementById('callTeamOverlay')?.addEventListener('click', function (e) {
    if (e.target === this) {
      this.classList.remove('is-open');
      this.setAttribute('aria-hidden', 'true');
      document.body.style.overflow = '';
    }
  });

  if (PATIENT.doctorView) {
    const backBtn = document.getElementById('backToRoster');
    if (backBtn) backBtn.style.display = 'inline';
  }

  const preTab = document.getElementById('preOperationTab');
  const postTab = document.getElementById('postOperationTab');
  if (preTab) preTab.href = `/patient/${PATIENT.id}/pre-op`;
  if (postTab) postTab.href = `/patient/${PATIENT.id}`;
  if (preTab && postTab) {
    preTab.classList.remove('active');
    postTab.classList.add('active');
  }
}

// ─── Overlays (click Diagnosis or Treatment → full-screen that section only) ─
function initOverlays() {
  document.querySelectorAll('.resource-card[data-overlay]').forEach(btn => {
    btn.addEventListener('click', () => {
      const overlayId = `overlay-${btn.dataset.overlay}`;
      const overlay = document.getElementById(overlayId);
      if (overlay) {
        overlay.classList.add('is-open');
        overlay.setAttribute('aria-hidden', 'false');
        document.body.style.overflow = 'hidden';
      }
    });
  });

  function closeOverlay(overlayEl) {
    if (!overlayEl) return;
    overlayEl.classList.remove('is-open');
    overlayEl.setAttribute('aria-hidden', 'true');
    document.body.style.overflow = '';
  }

  document.getElementById('backDiagnosis')?.addEventListener('click', () => {
    closeOverlay(document.getElementById('overlay-diagnosis'));
  });
  document.getElementById('backTreatment')?.addEventListener('click', () => {
    closeOverlay(document.getElementById('overlay-treatment'));
  });

  const voiceLinkDiag = document.getElementById('voiceLinkDiagnosis');
  const voiceLinkTreat = document.getElementById('voiceLinkTreatment');
  if (voiceLinkDiag) voiceLinkDiag.href = `/patient/${PATIENT.id}/digital-care-companion`;
  if (voiceLinkTreat) voiceLinkTreat.href = `/patient/${PATIENT.id}/digital-care-companion`;
}

function initQuestionsButton() {
  document.getElementById('questionsBtn')?.addEventListener('click', goToVoiceGuide);
  const voiceBtn = document.getElementById('voiceAvatarBtn');
  if (voiceBtn) voiceBtn.href = `/patient/${PATIENT.id}/digital-care-companion`;
  const preOpBtn = document.getElementById('openPreOpBtn');
  if (preOpBtn) preOpBtn.href = `/patient/${PATIENT.id}/pre-op`;
}

async function trackPatientEvent(eventType, payload = {}) {
  if (!PATIENT?.id) return;
  try {
    await fetch(`${window.location.origin}/api/patient/${PATIENT.id}/events`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ event_type: eventType, payload }),
    });
  } catch (_err) {
    // Non-blocking analytics/event call.
  }
}

function goToVoiceGuide() {
  window.location.href = `/patient/${PATIENT.id}/digital-care-companion`;
}

function showCareGuide() {
  const section = document.getElementById('careGuideSection');
  if (section) {
    section.style.display = 'block';
    section.scrollIntoView({ behavior: 'smooth', block: 'start' });
    setTimeout(() => document.getElementById('chatInput')?.focus(), 600);
  }
}

// ─── Load Resources (from injected data, no API calls) ────────
function loadResources() {
  const res = PATIENT.resources;

  if (res && res.diagnosis) {
    const dc = document.getElementById('diagBattlecardContainer');
    if (dc) dc.innerHTML = res.diagnosis.battlecard_html || noDataMsg('diagnosis battlecard');
    if (res.diagnosis.voice_audio_url) {
      initAudioPlayer('diag', res.diagnosis.voice_audio_url);
    }
  } else {
    const dc = document.getElementById('diagBattlecardContainer');
    if (dc) dc.innerHTML = getDemoDiagnosisBattlecard();
  }

  if (res && res.treatment) {
    const tc = document.getElementById('treatBattlecardContainer');
    if (tc) tc.innerHTML = res.treatment.battlecard_html || noDataMsg('treatment battlecard');
    if (res.treatment.voice_audio_url) {
      initAudioPlayer('treat', res.treatment.voice_audio_url);
    }
  } else {
    const tc = document.getElementById('treatBattlecardContainer');
    if (tc) tc.innerHTML = getDemoTreatmentBattlecard();
  }
}

function noDataMsg(name) {
  return `<p style="padding:24px;color:#6B7280;">No ${name} available yet.</p>`;
}

// ─── Speed Control ────────────────────────────────────────────
const audioPlayers = {};
const watchedEventSent = new Set();

document.addEventListener('click', (e) => {
  const btn = e.target.closest('.speed-btn');
  if (!btn) return;
  const control = btn.closest('.speed-control');
  const playerPrefix = control.dataset.player;
  const speed = parseFloat(btn.dataset.speed);

  control.querySelectorAll('.speed-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');

  if (audioPlayers[playerPrefix]) {
    audioPlayers[playerPrefix].playbackRate = speed;
  }
});

// ─── Audio Player ─────────────────────────────────────────────
function initAudioPlayer(prefix, audioUrl) {
  const playPauseBtn = document.getElementById(`${prefix}PlayPauseBtn`);
  const playBtnLarge = document.getElementById(`${prefix}PlayBtnLarge`);
  const overlay      = document.getElementById(`${prefix}VideoOverlay`);
  const progressFill = document.getElementById(`${prefix}ProgressFill`);
  const timeDisplay  = document.getElementById(`${prefix}TimeDisplay`);
  const progressBar  = document.getElementById(`${prefix}ProgressBar`);

  const audio = new Audio(audioUrl);
  audioPlayers[prefix] = audio;
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
  audio.addEventListener('ended', () => setPlay(false));
  audio.addEventListener('error', () => {
    if (playPauseBtn) playPauseBtn.textContent = '⚠ Audio unavailable';
    if (playPauseBtn) playPauseBtn.disabled = true;
  });

  function toggle() { isPlaying ? doPause() : doPlay(); }
  function doPlay() {
    audio.play();
    setPlay(true);
    if (overlay) overlay.style.opacity = '0';
    if (!watchedEventSent.has(prefix)) {
      watchedEventSent.add(prefix);
      const ev = prefix === 'diag' ? 'diagnosis_video_watched' : 'treatment_video_watched';
      trackPatientEvent(ev, { source: 'patient_dashboard' });
    }
  }
  function doPause() { audio.pause(); setPlay(false); if (overlay) overlay.style.opacity = '1'; }
  function setPlay(val) {
    isPlaying = val;
    if (playPauseBtn) playPauseBtn.textContent = val ? '⏸ Pause' : '▶ Play';
    if (playBtnLarge) playBtnLarge.textContent = val ? '⏸' : '▶';
  }

  [playBtnLarge, playPauseBtn].forEach(el => el?.addEventListener('click', toggle));
  progressBar?.addEventListener('click', e => {
    const rect = progressBar.getBoundingClientRect();
    audio.currentTime = ((e.clientX - rect.left) / rect.width) * audio.duration;
  });
}

function fmtTime(sec) {
  if (isNaN(sec)) return '0:00';
  return `${Math.floor(sec / 60)}:${String(Math.floor(sec % 60)).padStart(2, '0')}`;
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
  document.getElementById('sendBtn')?.addEventListener('click', sendMessage);
  document.getElementById('chatInput')?.addEventListener('keypress', e => {
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
    const res = await fetch(`${window.location.origin}/api/digital-care-companion/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        patient_id: PATIENT.id,
        message: text,
        conversation_history: getChatHistory(),
      }),
    });

    const data = await res.json();
    typingEl.remove();

    if (!res.ok) {
      appendMessage(`Error (${res.status}): ${data.detail || 'Unknown error'}. Check your server terminal for details.`, 'error');
      return;
    }

    if (data.response && data.response.includes('technical issue')) {
      appendMessage(data.response + '\n\n⚠️ Check your server terminal — the actual error is printed there.', 'assistant');
    } else {
      appendMessage(data.response, 'assistant');
      if (data.escalation && data.escalation.requires_consent) {
        appendConsentPrompt(data.escalation.escalation_id);
      }
    }
  } catch (err) {
    typingEl.remove();
    appendMessage(`Connection error: ${err.message}. Is the server running?`, 'error');
  }
}

function appendConsentPrompt(escalationId) {
  const messages = document.getElementById('chatMessages');
  const wrap = document.createElement('div');
  wrap.className = 'message assistant-message';
  wrap.innerHTML = `
    <div class="message-bubble">
      <div style="margin-bottom:8px;">Share this with your care navigator?</div>
      <div style="display:flex;gap:8px;">
        <button type="button" data-consent="yes" style="padding:6px 10px;border:1px solid #d1d5db;border-radius:8px;background:#fff;cursor:pointer;">Yes</button>
        <button type="button" data-consent="no" style="padding:6px 10px;border:1px solid #d1d5db;border-radius:8px;background:#fff;cursor:pointer;">No</button>
      </div>
    </div>
    <span class="message-time">${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>
  `;
  wrap.querySelectorAll('[data-consent]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const consent = btn.getAttribute('data-consent');
      try {
        await fetch(`${window.location.origin}/api/escalations/consent`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ escalation_id: escalationId, consent }),
        });
        appendMessage(consent === 'yes' ? 'Consent shared. A navigator will follow up.' : 'Understood. I will not share this right now.', 'assistant');
      } catch (_e) {
        appendMessage('Could not save consent right now. Please try again.', 'error');
      }
      wrap.querySelectorAll('button').forEach((b) => { b.disabled = true; });
    });
  });
  messages.appendChild(wrap);
  messages.scrollTop = messages.scrollHeight;
}

function appendMessage(text, role) {
  const messages = document.getElementById('chatMessages');
  const el = document.createElement('div');
  el.className = `message ${role === 'error' ? 'error' : role}-message`;
  const time = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

  if (role === 'error') {
    el.innerHTML = `<div class="message-bubble" style="background:#FEF2F2;border:1px solid #FECACA;color:#991B1B;">${escapeHtml(text)}</div><span class="message-time">${time}</span>`;
  } else {
    el.innerHTML = `<div class="message-bubble">${escapeHtml(text)}</div><span class="message-time">${time}</span>`;
  }
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
  return Array.from(document.querySelectorAll('#chatMessages .message:not(.typing-indicator):not(.error-message)'))
    .slice(1)
    .map(msg => ({
      role: msg.classList.contains('patient-message') ? 'user' : 'assistant',
      content: msg.querySelector('.message-bubble').textContent.trim(),
    }));
}

function initFooterActions() {
  document.getElementById('openAvatarBtn')?.addEventListener('click', goToVoiceGuide);
}

function setText(id, val) {
  const el = document.getElementById(id);
  if (el && val) el.textContent = val;
}

function escapeHtml(str) {
  const d = document.createElement('div');
  d.appendChild(document.createTextNode(str));
  return d.innerHTML;
}

// ─── Demo Battlecards (fallback for demo patient) ─────────────

function getDemoDiagnosisBattlecard() {
  return `<div style="font-family:'Inter',sans-serif;padding:24px;max-width:820px;margin:0 auto;color:#1F2937;font-size:14px;line-height:1.55"><div style="background:linear-gradient(135deg,#0F172A,#1E3A5F);color:#fff;padding:20px 24px;border-radius:10px;margin-bottom:20px"><h2 style="font-size:18px;font-weight:700;margin-bottom:4px">Understanding Your Diagnosis</h2><p style="font-size:13px;opacity:.8">Lumpectomy + Sentinel Node Biopsy</p></div><div style="margin-bottom:20px"><h3 style="font-size:15px;font-weight:700;margin-bottom:10px">🔍 Your Diagnoses</h3><div style="background:#F9FAFB;border-left:4px solid #2563EB;border-radius:8px;padding:14px 18px;margin-bottom:10px"><div style="font-weight:700;margin-bottom:4px">Triple-Negative Breast Cancer</div><div style="font-size:13px;color:#4B5563">A type of breast cancer where the cells don't have three common receptors.</div></div><div style="background:#F9FAFB;border-left:4px solid #7C3AED;border-radius:8px;padding:14px 18px"><div style="font-weight:700;margin-bottom:4px">BRCA1 Mutation (Germline)</div><div style="font-size:13px;color:#4B5563">A genetic change you were born with — not caused by anything you did.</div></div></div></div>`;
}

function getDemoTreatmentBattlecard() {
  return `<div style="font-family:'Inter',sans-serif;padding:24px;max-width:820px;margin:0 auto;color:#1F2937;font-size:14px;line-height:1.55"><div style="background:linear-gradient(135deg,#1A3C8F,#2563EB);color:#fff;padding:20px 24px;border-radius:10px;margin-bottom:20px"><h2 style="font-size:18px;font-weight:700;margin-bottom:4px">Your Treatment & Recovery Action Card</h2><p style="font-size:13px;opacity:.8">Post-Surgery Discharge</p></div><div style="background:#FFF7ED;border:1px solid #FED7AA;border-left:4px solid #F97316;border-radius:8px;padding:16px 20px;margin-bottom:20px"><h3 style="font-size:14px;font-weight:700;color:#92400E;margin-bottom:6px">⭐ The ONE Most Important Thing</h3><p style="font-size:14px;color:#78350F">Take <strong>olaparib 300mg twice daily</strong>, 12 hours apart, every single day.</p></div><div style="background:#FFFBEB;border:1px solid #FCD34D;border-radius:8px;padding:14px 18px;margin-bottom:14px"><h4 style="color:#92400E;font-size:13px;font-weight:700;margin-bottom:8px">⚠ Call Your Doctor If</h4><ul style="font-size:13px;color:#78350F;margin-left:16px"><li>Fever of 100.4°F or higher</li><li>Vomiting that won't stop</li></ul></div></div>`;
}
