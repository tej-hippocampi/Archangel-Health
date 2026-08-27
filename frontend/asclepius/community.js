/* ═══════════════════════════════════════════════════════════════════════════
   Asclepius Community — Archangel-coded Slack (Community PRD)
   Vanilla SPA, no frameworks, no build step. Auth = the existing Asclepius
   JWT (same origin as the doctor portal; §1 handoff). Every privileged call
   is re-checked server-side — this file is presentation only.
   ═══════════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  const API = '/api/community';
  const TOKEN_KEY = 'asclepius_token';
  const PHI_NOTICE = 'Colleague discussion only. Do not post patient-identifiable information.';

  // ─── State ─────────────────────────────────────────────────────────────────
  const state = {
    token: null,
    me: null,           // my member profile
    isAdmin: false,
    canPost: true,      // false for a view-only account (advisor); set from /me
    channels: [],       // [{slug,name,description,post_policy,unread,mentions}]
    dms: [],            // [{id, peer, unread, last_message_id, last_message_at}]
    active: 'general',  // channel slug OR a dm id ("dm-…") — keys never collide
    msgs: {},           // container key (slug or dm id) -> {list, hasMore, loaded}
    members: [],
    membersById: {},
    online: new Set(),
    memberFilter: '',   // specialty filter for the member directory (§4)
    thread: null,       // {rootId, root, replies}
    sidePanel: null,    // null | 'thread' | 'member'
    sideMember: null,
    typing: {},         // key(channel|thread) -> {name, until}
    ws: null,
    wsOk: false,
    wsRetry: 0,
    pollTimer: null,
    editing: null,      // message id being edited inline
    emojiFor: null,     // message id with open emoji popover
    retention: '',
    // v2.1 social surfaces
    pins: {},           // channel slug -> [pinned message objects]
    bookmarks: {},      // channel slug -> [{id,title,url,added_by}]
    events: { upcoming: [], past: [], pastOpen: false, loadedFor: null },
  };

  const QUICK_EMOJI = ['👍', '✅', '🙌', '❤️', '😂', '🤔', '👀', '🎉'];

  // ─── DOM helpers (same idiom as asclepius.js) ──────────────────────────────
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const k in attrs) {
        const v = attrs[k];
        if (v == null || v === false) continue;
        if (k === 'class') el.className = v;
        else if (k === 'text') el.textContent = v;
        else if (k === 'html') el.innerHTML = v;
        else if (k === 'disabled') { if (v) el.setAttribute('disabled', ''); }
        else if (k.slice(0, 2) === 'on' && typeof v === 'function') {
          el.addEventListener(k.slice(2).toLowerCase(), v);
        } else if (k === 'value') el.value = v;
        else el.setAttribute(k, v);
      }
    }
    append(el, children);
    return el;
  }
  function append(el, children) {
    for (const c of children) {
      if (c == null || c === false) continue;
      if (Array.isArray(c)) append(el, c);
      else if (c instanceof Node) el.appendChild(c);
      else el.appendChild(document.createTextNode(String(c)));
    }
  }
  const root = () => document.getElementById('cmRoot');
  function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }
  function setRoot(node) { const r = root(); clear(r); r.appendChild(node); }

  function toast(msg, kind) {
    const region = document.getElementById('cmToasts');
    const t = h('div', { class: 'cm-toast ' + (kind || 'info'), role: 'status' }, msg);
    region.appendChild(t);
    setTimeout(() => { t.style.opacity = '0'; t.style.transition = 'opacity .3s'; setTimeout(() => t.remove(), 320); },
      kind === 'error' ? 5200 : 3000);
  }

  // ─── API ───────────────────────────────────────────────────────────────────
  async function api(path, opts) {
    opts = opts || {};
    const headers = opts.headers || {};
    if (state.token) headers['Authorization'] = 'Bearer ' + state.token;
    let body = opts.body;
    if (body !== undefined && !opts.isForm) {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(API + path, { method: opts.method || 'GET', headers, body });
    } catch (e) {
      throw { status: 0, detail: null, message: 'Network error — check your connection.' };
    }
    let data = null;
    const ct = res.headers.get('content-type') || '';
    if (ct.indexOf('application/json') !== -1) data = await res.json().catch(() => null);
    if (!res.ok) {
      const detail = data && typeof data === 'object' ? data.detail : null;
      const message = typeof detail === 'string' ? detail
        : (detail && detail.message) ? detail.message : ('Request failed (' + res.status + ')');
      throw { status: res.status, detail, message };
    }
    return data;
  }

  // ─── Formatting: escape-then-transform markdown-lite (§4) ─────────────────
  function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  // Placeholders are NUL-delimited: escaped user text can never collide with
  // them (typing "B0" in a message must not summon a code block or delete
  // text — audit finding). Restoration keeps the original text on a miss.
  const PH = '\u0000';
  function renderInline(t, blocks) {
    // inline code first, extracted to placeholders so markers/URLs inside
    // stay literal (audit finding: transforming in place mangled contents)
    t = t.replace(/`([^`\n]+)`/g, (m, c) => {
      blocks.push('<code>' + c + '</code>');
      return PH + (blocks.length - 1) + PH;
    });
    t = t.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    t = t.replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,!?:;]|$)/g, '$1<em>$2</em>');
    t = t.replace(/(^|[\s(])_([^_\n]+)_(?=[\s).,!?:;]|$)/g, '$1<em>$2</em>');
    // [text](https://url) — http(s) only (escaped input, so quotes are inert)
    t = t.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    // bare URLs
    t = t.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g,
      '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
    return t;
  }
  function renderBody(raw, mentions) {
    // NUL can't be typed, but strip any pasted ones so placeholders stay ours
    const escaped = esc(String(raw || '').replace(/\u0000/g, ''));
    // fenced code blocks
    const blocks = [];
    let text = escaped.replace(/```([\s\S]*?)```/g, (m, code) => {
      blocks.push('<pre><code>' + code.replace(/^\n/, '') + '</code></pre>');
      return PH + (blocks.length - 1) + PH;
    });
    const lines = text.split('\n');
    const out = [];
    let inList = false;
    for (const line of lines) {
      const li = /^\s*[-*]\s+(.*)$/.exec(line);
      if (li) {
        if (!inList) { out.push('<ul>'); inList = true; }
        out.push('<li>' + renderInline(li[1], blocks) + '</li>');
      } else {
        if (inList) { out.push('</ul>'); inList = false; }
        out.push(renderInline(line, blocks));
      }
    }
    if (inList) out.push('</ul>');
    let html = out.join('\n').replace(/\n(<\/?ul>)\n?/g, '$1').replace(/(<\/li>)\n/g, '$1');
    // @mentions — only for members actually recorded on the message
    for (const uid of mentions || []) {
      const m = state.membersById[uid];
      if (!m) continue;
      const needle = '@' + esc(m.display_name);
      html = html.split(needle).join('<span class="cm-mention">' + needle + '</span>');
    }
    // @channel / @here broadcast tokens render as a distinct pill.
    html = html.replace(/(^|[\s(])@(channel|here)\b/gi,
      '$1<span class="cm-broadcast-pill">@$2</span>');
    html = html.replace(/\u0000(\d+)\u0000/g, (m, i) =>
      (blocks[Number(i)] !== undefined ? blocks[Number(i)] : m));
    return html;
  }

  // Server PHI spans are Python code-point indices; JS strings are UTF-16
  // (an emoji counts as 2 units). Convert before slicing so the highlight
  // and the one-tap removal target exactly the flagged characters (audit
  // finding).
  function cpToUtf16(str, cpIdx) {
    let units = 0, cps = 0;
    for (const ch of str) {
      if (cps >= cpIdx) break;
      units += ch.length;
      cps++;
    }
    return units;
  }
  function spansToUtf16(body, spans) {
    return spans.map((sp) => [cpToUtf16(body, sp[0]), cpToUtf16(body, sp[1])]);
  }

  function fmtTime(iso) {
    try {
      const d = new Date(iso);
      return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    } catch (e) { return ''; }
  }
  function dayKey(iso) { return (iso || '').slice(0, 10); }
  function fmtDay(iso) {
    try {
      const d = new Date(iso);
      const today = new Date();
      const yest = new Date(Date.now() - 864e5);
      if (d.toDateString() === today.toDateString()) return 'Today';
      if (d.toDateString() === yest.toDateString()) return 'Yesterday';
      return d.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
    } catch (e) { return ''; }
  }

  // ─── Client-side PHI pre-check (§7.1 — a UX nicety; the server decides) ────
  const PHI_HINTS = [
    [/\b(MRN|MBI|medical record)\b/i, 'an MRN'],
    [/\b\d{3}-\d{2}-\d{4}\b/, 'an SSN'],
    [/\b(DOB|date of birth)\b/i, 'a date of birth'],
    [/\b\d{1,2}\/\d{1,2}\/\d{2,4}\b/, 'an exact date'],
    [/(?:^|\D)\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}(?!\d)/, 'a phone number'],
    [/\b[\w.+-]+@[\w-]+\.[\w.-]+\b/, 'an email address'],
    [/\b\d{7,}\b/, 'a long identifier-like number'],
    [/\b(patient|pt)\.?\s+(name\s*)?(is\s*)?[A-Z][a-z]+\s+[A-Z][a-z]+\b/, 'a patient name'],
  ];
  function phiHint(text) {
    for (const [re, label] of PHI_HINTS) if (re.test(text)) return label;
    return null;
  }

  // ─── Boot ──────────────────────────────────────────────────────────────────
  // Portal handoff (§1): the side panel opens /community?t=<code>. Redeem the
  // single-use code for an Asclepius session, then strip it from the URL so it
  // never lingers in the address bar or gets bookmarked. Falls back silently
  // to any existing same-origin session token.
  async function redeemHandoff() {
    let code = null;
    try { code = new URLSearchParams(location.search).get('t'); } catch (e) { code = null; }
    if (!code) return;
    try { history.replaceState(null, '', location.pathname + location.hash); } catch (e) { /* ignore */ }
    try {
      const res = await fetch(API + '/handoff/redeem', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: code }),
      });
      if (!res.ok) return;
      const d = await res.json().catch(() => null);
      if (d && d.token) {
        state.token = d.token;
        try { localStorage.setItem(TOKEN_KEY, d.token); } catch (e) { /* quota */ }
      }
    } catch (e) { /* network — fall back to the stored session */ }
  }

  async function boot() {
    try { state.token = localStorage.getItem(TOKEN_KEY) || null; } catch (e) { state.token = null; }
    await redeemHandoff();
    if (!state.token) return renderSignedOut();
    try {
      const me = await api('/me');
      state.me = me.member;
      state.isAdmin = !!me.is_admin;
      // Defaults true so an older backend that does not send the field behaves
      // exactly as it did. The server refuses the write either way; this only
      // decides whether we show a box that is going to be refused.
      state.canPost = me.can_post !== false;
      state.retention = me.retention || '';
    } catch (e) {
      if (e.status === 401) return renderSignedOut();
      if (e.status === 403) return renderGate();
      return renderError(e.message);
    }
    await Promise.all([loadChannels(), loadMembers(), loadDms()]);
    const hash = (location.hash || '').replace(/^#/, '');
    if (hash && (state.channels.some((c) => c.slug === hash)
        || state.dms.some((d) => d.id === hash))) state.active = hash;
    renderApp();
    await openChannel(state.active, { force: true });
    connectWs();
    // No force here: returning to the tab while scrolled up in history must
    // not mark unseen messages read (audit finding).
    window.addEventListener('focus', () => markReadIfAtBottom());
  }

  function renderSignedOut() {
    setRoot(h('div', { class: 'cm-gate' },
      h('div', { class: 'cm-gate-card' },
        h('div', { class: 'chrome cm-gate-kicker' }, 'Asclepius · Community'),
        h('h1', { class: 'cm-gate-title' }, 'Sign in through the doctor portal'),
        h('p', { class: 'cm-gate-sub' },
          'The community opens from inside the Asclepius portal. Sign in there, then choose Community from the side panel.'),
        h('div', { class: 'cm-gate-actions' },
          h('a', { class: 'cm-btn cm-btn-primary', href: '/asclepius' }, 'Open the doctor portal')))));
  }
  function renderGate() {
    setRoot(h('div', { class: 'cm-gate' },
      h('div', { class: 'cm-gate-card' },
        h('div', { class: 'chrome cm-gate-kicker' }, 'Asclepius · Community'),
        h('h1', { class: 'cm-gate-title' }, 'Community access is for verified contributors'),
        h('p', { class: 'cm-gate-sub' },
          'This space is reserved for credential-verified contributor physicians. Once your credentials are verified you’ll be able to join the conversation.'),
        h('div', { class: 'cm-gate-actions' },
          h('a', { class: 'cm-btn cm-btn-ghost', href: '/asclepius' }, 'Back to the portal')))));
  }
  function renderError(msg) {
    setRoot(h('div', { class: 'cm-gate' },
      h('div', { class: 'cm-gate-card' },
        h('h1', { class: 'cm-gate-title' }, 'Something went wrong'),
        h('p', { class: 'cm-gate-sub' }, msg || 'Please try again.'),
        h('div', { class: 'cm-gate-actions' },
          h('button', { class: 'cm-btn cm-btn-primary', onClick: () => location.reload() }, 'Retry')))));
  }

  async function loadChannels() {
    const d = await api('/channels');
    state.channels = d.channels || [];
  }
  async function loadDms() {
    const d = await api('/dms');
    state.dms = d.dms || [];
  }
  function isDmKey(key) { return typeof key === 'string' && key.indexOf('dm-') === 0; }
  function activeDm() { return state.dms.find((d) => d.id === state.active) || null; }
  function messagesUrl(key) {
    return isDmKey(key)
      ? '/dms/' + encodeURIComponent(key) + '/messages'
      : '/channels/' + encodeURIComponent(key) + '/messages';
  }
  function readUrl(key) {
    return isDmKey(key)
      ? '/dms/' + encodeURIComponent(key) + '/read'
      : '/channels/' + encodeURIComponent(key) + '/read';
  }
  async function loadMembers() {
    const d = await api('/members');
    state.members = d.members || [];
    state.membersById = {};
    state.online = new Set();
    for (const m of state.members) {
      state.membersById[m.user_id] = m;
      if (m.online) state.online.add(m.user_id);
    }
  }

  // ─── App layout ────────────────────────────────────────────────────────────
  function renderApp() {
    const app = h('div', { class: 'cm-app' },
      h('nav', { class: 'cm-rail', id: 'cmRail', 'aria-label': 'Channels and members' }),
      h('section', { class: 'cm-main' },
        h('header', { class: 'cm-head', id: 'cmHead' }),
        h('div', { class: 'cm-scroll', id: 'cmScroll', role: 'log', 'aria-label': 'Messages' }),
        h('div', { class: 'cm-typing', id: 'cmTyping', 'aria-live': 'polite' }),
        h('div', { class: 'cm-composer-wrap', id: 'cmComposerWrap' })),
      h('aside', { class: 'cm-side', id: 'cmSide', 'aria-label': 'Details panel' }));
    setRoot(app);
    renderRail();
    renderHead();
    renderComposer();
    const scroll = document.getElementById('cmScroll');
    scroll.addEventListener('scroll', onScroll);
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { closeSide(); closeEmoji(); }
    });
    document.addEventListener('click', (e) => {
      if (!e.target.closest('.cm-emoji-pop') && !e.target.closest('[data-emoji-btn]')) closeEmoji();
    });
  }

  function renderRail() {
    const rail = document.getElementById('cmRail');
    if (!rail) return;
    clear(rail);
    rail.appendChild(h('div', { class: 'cm-rail-head' },
      h('div', { class: 'chrome' }, 'Asclepius'),
      h('div', { class: 'cm-rail-title' }, 'Community')));

    const scrollBox = h('div', { class: 'cm-rail-scroll' });

    // channels — core, then threshold-activated specialty channels (the
    // member's own specialty sorts first in its group)
    const chanBtn = (ch) => {
      const unread = ch.unread || 0;
      const isActive = ch.slug === state.active;
      return h('button', {
        class: 'cm-chan' + (isActive ? ' active' : ''),
        'aria-current': isActive ? 'page' : null,
        onClick: () => openChannel(ch.slug),
      },
        h('span', { class: 'cm-chan-hash', 'aria-hidden': 'true' }, '#'),
        h('span', { class: 'cm-chan-name' }, ch.slug),
        ch.post_policy === 'admin' ? h('span', { class: 'cm-chan-lock', title: 'Announcements — Archangel team posts, replies open in threads' }, 'ro') : null,
        unread > 0 && !isActive
          ? h('span', { class: 'cm-chan-unread' }, unread > 99 ? '99+' : String(unread))
          : (unread > 0 ? h('span', { class: 'dot dot-lime', 'aria-label': 'unread' }) : null));
    };
    const coreChans = state.channels.filter(
      (c) => (c.group || 'core') !== 'specialty' && (c.group || 'core') !== 'country');
    const mySpec = ((state.me && state.me.specialty) || '').toLowerCase();
    const specChans = state.channels.filter((c) => c.group === 'specialty')
      .sort((a, b) => ((b.specialty || '').toLowerCase() === mySpec ? 1 : 0)
                    - ((a.specialty || '').toLowerCase() === mySpec ? 1 : 0));
    const chSection = h('div', { class: 'cm-rail-section' },
      h('div', { class: 'cm-rail-label' }, h('span', { class: 'chrome' }, 'Channels')));
    for (const ch of coreChans) chSection.appendChild(chanBtn(ch));
    scrollBox.appendChild(chSection);
    if (specChans.length) {
      const spSection = h('div', { class: 'cm-rail-section' },
        h('div', { class: 'cm-rail-label' }, h('span', { class: 'chrome' }, 'Specialty')));
      for (const ch of specChans) spSection.appendChild(chanBtn(ch));
      scrollBox.appendChild(spSection);
    }

    // Countries. Own room first, same as specialty: a doctor in Riyadh should
    // find Saudi Arabia at the top of the list rather than scrolling past
    // Australia to reach it.
    const myCountry = ((state.me && state.me.country) || '').toUpperCase();
    const countryChans = state.channels.filter((c) => c.group === 'country')
      .sort((a, b) => ((b.country || '').toUpperCase() === myCountry ? 1 : 0)
                    - ((a.country || '').toUpperCase() === myCountry ? 1 : 0));
    if (countryChans.length) {
      const coSection = h('div', { class: 'cm-rail-section' },
        h('div', { class: 'cm-rail-label' }, h('span', { class: 'chrome' }, 'Countries')));
      for (const ch of countryChans) coSection.appendChild(chanBtn(ch));
      scrollBox.appendChild(coSection);
    }

    // direct messages (user-requested extension)
    const dmSection = h('div', { class: 'cm-rail-section' },
      h('div', { class: 'cm-rail-label' },
        h('span', { class: 'chrome' }, 'Direct messages')));
    if (!state.dms.length) {
      dmSection.appendChild(h('div', { class: 'cm-rail-hint' },
        'Open a colleague’s profile to start one.'));
    }
    for (const d of state.dms) {
      const peer = d.peer || {};
      const isActive = d.id === state.active;
      dmSection.appendChild(h('button', {
        class: 'cm-chan cm-dm-row' + (isActive ? ' active' : ''),
        'aria-current': isActive ? 'page' : null,
        onClick: () => openDm(d.id),
      },
        h('span', {
          class: 'cm-presence' + (state.online.has(peer.user_id) ? ' online' : ''),
          'aria-hidden': 'true',
        }),
        h('span', { class: 'cm-chan-name cm-dm-name' }, peer.display_name || 'Former member'),
        d.unread > 0 && !isActive
          ? h('span', { class: 'cm-chan-unread' }, d.unread > 99 ? '99+' : String(d.unread))
          : null));
    }
    scrollBox.appendChild(dmSection);

    // members
    const online = state.members.filter((m) => state.online.has(m.user_id)).length;
    // Specialty filter (PRD §4: member directory with specialty filter)
    const shown = state.members.filter((m) =>
      !state.memberFilter || (m.specialty || '').toLowerCase() === state.memberFilter);
    const specialties = Array.from(new Set(state.members
      .filter((m) => !m.is_staff && m.specialty)
      .map((m) => m.specialty.toLowerCase()))).sort();
    const filterSel = h('select', {
      class: 'cm-member-filter',
      'aria-label': 'Filter members by specialty',
      onChange: (e) => { state.memberFilter = e.target.value; renderRail(); },
    },
      h('option', { value: '' }, 'All specialties'),
      specialties.map((s) => h('option',
        { value: s, selected: state.memberFilter === s ? 'selected' : null },
        s.charAt(0).toUpperCase() + s.slice(1))));

    const mSection = h('div', { class: 'cm-rail-section' },
      h('div', { class: 'cm-rail-label' },
        h('span', { class: 'chrome' },
          'Members (' + (state.memberFilter
            ? shown.length + '/' + state.members.length
            : state.members.length) + ')'),
        h('span', { class: 'chrome' }, online + ' online')),
      specialties.length > 1 ? h('div', { class: 'cm-member-filter-row' }, filterSel) : null);
    for (const m of shown) {
      mSection.appendChild(h('button', {
        class: 'cm-member-row',
        onClick: () => openMember(m.user_id),
      },
        h('span', { class: 'cm-presence' + (state.online.has(m.user_id) ? ' online' : ''), 'aria-hidden': 'true' }),
        h('span', { class: 'cm-member-row-name' }, m.display_name,
          m.verified ? h('span', { class: 'cm-verified', title: 'Credential-verified', style: 'margin-left:6px' }) : null),
        m.is_staff
          ? h('span', { class: 'cm-member-row-spec' }, 'Archangel')
          : (m.specialty ? h('span', { class: 'cm-member-row-spec' }, m.specialty) : null)));
    }
    scrollBox.appendChild(mSection);
    rail.appendChild(scrollBox);

    const me = state.me || {};
    rail.appendChild(h('div', { class: 'cm-rail-foot' },
      h('div', { class: 'cm-rail-me' },
        avatarEl(me, 'small'),
        h('span', {}, me.display_name || '')),
      h('div', { class: 'cm-rail-retention' },
        state.retention || 'Messages are retained indefinitely unless an admin removes them.')));
  }

  function activeChannel() {
    return state.channels.find((c) => c.slug === state.active) || state.channels[0];
  }

  function renderHead() {
    const head = document.getElementById('cmHead');
    if (!head) return;
    clear(head);
    if (isDmKey(state.active)) {
      const d = activeDm();
      const peer = (d && d.peer) || {};
      head.appendChild(h('span', { class: 'cm-head-name' }, peer.display_name || 'Conversation'));
      head.appendChild(h('span', { class: 'cm-head-desc' },
        'Direct messages are between the two of you.'
        + (peer.blurb ? ' ' + peer.blurb : '')));
    } else {
      const ch = activeChannel();
      if (!ch) return;
      head.appendChild(h('span', { class: 'cm-head-name' }, '#' + ch.slug));
      head.appendChild(h('span', { class: 'cm-head-desc' }, ch.description || ''));
    }
    const searchWrap = h('div', { class: 'cm-search' });
    const input = h('input', {
      type: 'search', placeholder: 'Search messages…', 'aria-label': 'Search messages',
      onInput: (e) => onSearchInput(e.target.value, searchWrap),
      onKeydown: (e) => { if (e.key === 'Escape') { e.target.value = ''; closeSearchPop(searchWrap); } },
    });
    searchWrap.appendChild(input);
    head.appendChild(searchWrap);

    // v2.1 channel affordances (not in DMs): pinned-count + bookmark bar.
    if (!isDmKey(state.active)) {
      const slug = state.active;
      const pinCount = (state.pins[slug] || []).length;
      head.appendChild(h('button', {
        class: 'cm-head-btn' + (pinCount ? ' has' : ''), 'aria-label': 'Pinned messages',
        title: 'Pinned messages', onClick: () => openPins(slug),
      }, '📌 ' + (pinCount || '')));
      const bar = renderBookmarkBar(slug);
      if (bar) head.appendChild(bar);
    }
  }

  // ─── Channel / DM open + history ───────────────────────────────────────────
  async function openContainer(key, opts) {
    opts = opts || {};
    if (state.active === key && !opts.force && state.msgs[key] && state.msgs[key].loaded) {
      return;
    }
    state.active = key;
    try { history.replaceState(null, '', '#' + key); } catch (e) { /* ignore */ }
    renderRail();
    renderHead();
    renderComposer();
    if (!state.msgs[key] || opts.force) {
      state.msgs[key] = { list: [], hasMore: false, loaded: false };
      try {
        const d = await api(messagesUrl(key) + '?limit=50');
        state.msgs[key] = { list: d.messages || [], hasMore: !!d.has_more, loaded: true };
      } catch (e) {
        toast(e.message, 'error');
        return;
      }
    }
    // v2.1: channel-scoped side data (pins, bookmarks, and #events cards).
    if (!isDmKey(key)) {
      Promise.all([
        loadPins(key), loadBookmarks(key),
        key === 'events' ? loadEvents() : Promise.resolve(),
      ]).then(() => { if (state.active === key) { renderHead(); renderMessages({}); } })
        .catch(() => { /* transient */ });
    }
    renderMessages({ stickBottom: true });
    markReadIfAtBottom(true);
  }
  function openChannel(slug, opts) { return openContainer(slug, opts); }
  function openDm(dmId, opts) { return openContainer(dmId, opts); }

  async function startDmWith(userId) {
    try {
      const d = await api('/dms', { method: 'POST', body: { user_id: userId } });
      if (!state.dms.some((x) => x.id === d.id)) state.dms.unshift(d);
      closeSide();
      await openDm(d.id, { force: true });
    } catch (e) { toast(e.message, 'error'); }
  }

  let loadingOlder = false;
  async function onScroll() {
    const scroll = document.getElementById('cmScroll');
    const st = state.msgs[state.active];
    if (!scroll || !st || !st.hasMore || loadingOlder) {
      markReadIfAtBottom();
      return;
    }
    if (scroll.scrollTop < 80 && st.list.length) {
      loadingOlder = true;
      const oldest = st.list[0].id;
      const prevHeight = scroll.scrollHeight;
      try {
        const d = await api(messagesUrl(state.active) + '?limit=50&before=' + oldest);
        const seen = new Set(st.list.map((m) => m.id));
        const older = (d.messages || []).filter((m) => !seen.has(m.id));
        st.list = older.concat(st.list);
        st.hasMore = !!d.has_more;
        renderMessages({});
        scroll.scrollTop = scroll.scrollHeight - prevHeight + scroll.scrollTop;
      } catch (e) { /* transient */ }
      loadingOlder = false;
    }
    markReadIfAtBottom();
  }

  const EMPTY_COPY = {
    'general': ['Welcome to #general', 'Open discussion between contributor physicians. Say hello — everyone here is credential-verified.'],
    'task-announcements': ['No announcements yet', 'New task batches, specialty calls, and deadlines from the Archangel team land here. Replies open in threads.'],
    'questions-help': ['No questions yet', 'Ask anything about a case, a rubric, or a payout.'],
  };

  function renderMessages(opts) {
    opts = opts || {};
    const scroll = document.getElementById('cmScroll');
    if (!scroll) return;
    const st = state.msgs[state.active] || { list: [] };
    const atBottom = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 60;
    clear(scroll);
    const inDm = isDmKey(state.active);
    // #events: the pinned event card(s) sit above the message stream. Its own
    // banner carries the empty state, so skip the generic "nothing here" copy.
    if (state.active === 'events') {
      scroll.appendChild(renderEventsBanner());
      if (st.list.length) {
        scroll.appendChild(h('div', { class: 'cm-day-sep' },
          h('span', { class: 'chrome' }, 'Discussion')));
      }
    }
    if (!st.list.length) {
      if (state.active === 'events') return;
      let copy;
      if (inDm) {
        const peer = (activeDm() || {}).peer || {};
        copy = ['A private conversation',
          'This is the beginning of your direct messages with '
          + (peer.display_name || 'this colleague') + '. Colleague discussion only — no PHI.'];
      } else {
        copy = EMPTY_COPY[state.active] || ['Nothing here yet', 'Start the conversation.'];
      }
      scroll.appendChild(h('div', { class: 'cm-empty' },
        h('div', { class: 'cm-empty-title' }, copy[0]),
        h('p', {}, copy[1])));
      return;
    }
    let lastDay = null;
    for (const m of st.list) {
      const day = dayKey(m.created_at);
      if (day !== lastDay) {
        scroll.appendChild(h('div', { class: 'cm-day-sep' },
          h('span', { class: 'chrome' }, fmtDay(m.created_at))));
        lastDay = day;
      }
      scroll.appendChild(messageEl(m, { context: inDm ? 'dm' : 'channel' }));
    }
    if (opts.stickBottom || atBottom) scroll.scrollTop = scroll.scrollHeight;
  }

  // ─── Message element ───────────────────────────────────────────────────────
  function avatarEl(member, size) {
    const acc = (member && member.specialty_accent) || 'green';
    return h('button', {
      class: 'cm-avatar acc-' + acc + (size === 'small' ? '' : '') + (size === 'big' ? ' cm-profile-avatar' : ''),
      style: size === 'small' ? 'width:24px;height:24px;font-size:0.55rem' : null,
      'aria-label': 'Profile: ' + ((member && member.display_name) || 'member'),
      onClick: member && member.user_id ? () => openMember(member.user_id) : null,
    }, (member && member.initials) || '—');
  }

  function specChipEl(author) {
    if (author.is_bot) {
      return h('span', { class: 'cm-bot-badge', title: 'Automated post from the Archangel platform' }, 'APP');
    }
    if (author.is_staff) {
      return h('span', { class: 'cm-spec-chip' },
        h('span', { class: 'dot dot-orange', 'aria-hidden': 'true' }), 'Archangel');
    }
    if (!author.specialty) return null;
    return h('span', { class: 'cm-spec-chip' },
      h('span', { class: 'dot dot-' + (author.specialty_accent || 'green'), 'aria-hidden': 'true' }),
      author.specialty,
      author.years_in_practice != null ? ' · ' + author.years_in_practice + ' yrs' : null);
  }

  /* Link cards under a bot post: what the thing is, where it is, and enough
     of a summary to decide from here whether it is worth the click. Built
     with h() like everything else -- the title and summary come from someone
     else's web page by way of a model, so they are text nodes, never markup.

     No preview images. The CSP is img-src 'self' and loosening it for
     arbitrary news domains, or building a proxy to launder them, is real
     attack surface for decoration. The domain initial does the same job. */
  function cardsEl(cards) {
    if (!cards || !cards.length) return null;
    const wrap = h('div', { class: 'cm-cards' });
    for (const c of cards) {
      if (!c || !c.title) continue;
      const url = String(c.url || '');
      const safe = /^https?:\/\//i.test(url);
      const mono = h('span', { class: 'cm-card-mono' },
        (c.domain || c.title || '?').charAt(0).toUpperCase());
      const title = safe
        ? h('a', { class: 'cm-card-title', href: url, target: '_blank',
                   rel: 'noopener noreferrer' }, c.title)
        : h('span', { class: 'cm-card-title' }, c.title);
      const meta = h('div', { class: 'cm-card-meta' });
      if (c.domain) meta.appendChild(h('span', { class: 'cm-card-domain' }, c.domain));
      if (c.meta) meta.appendChild(h('span', {}, c.meta));
      const col = h('div', { class: 'cm-card-col' }, title);
      if (c.description) {
        col.appendChild(h('div', { class: 'cm-card-desc' }, c.description));
      }
      if (c.meta || c.domain) col.appendChild(meta);
      if (c.prompt) col.appendChild(h('div', { class: 'cm-card-prompt' }, c.prompt));
      wrap.appendChild(h('div', { class: 'cm-card' }, mono, col));
    }
    return wrap.childNodes.length ? wrap : null;
  }

  function messageEl(m, opts) {
    opts = opts || {};
    // DMs have no threads, so the reply affordances hide there like in the
    // thread panel itself.
    const inThread = opts.context === 'thread' || opts.context === 'dm';
    if (m.deleted) {
      return h('div', { class: 'cm-msg', 'data-mid': m.id },
        h('div', { class: 'cm-avatar acc-green', style: 'visibility:hidden' }, ''),
        h('div', { class: 'cm-msg-col' },
          h('div', { class: 'cm-msg-deleted' }, 'Message removed'),
          !inThread && m.reply_count > 0 ? threadTeaser(m) : null));
    }
    const a = m.author || {};
    const mine = state.me && a.user_id === state.me.user_id;
    const canDelete = mine || state.isAdmin;

    const bodyEl = h('div', { class: 'cm-msg-body', html: renderBody(m.body, m.mentions) });

    // A view-only reader keeps the thread affordance, because opening a thread
    // is reading. Reacting and pinning change what everyone else sees, and the
    // server refuses both, so the buttons go rather than fail.
    const canPin = !inThread && !isDmKey(state.active) && state.canPost;
    const actions = h('div', { class: 'cm-msg-actions', role: 'toolbar', 'aria-label': 'Message actions' },
      state.canPost ? h('button', { class: 'cm-act', 'data-emoji-btn': '1', title: 'Add reaction', 'aria-label': 'Add reaction',
        onClick: (e) => { e.stopPropagation(); toggleEmojiPop(m); } }, '😀') : null,
      !inThread ? h('button', { class: 'cm-act', title: 'Reply in thread', 'aria-label': 'Reply in thread',
        onClick: () => openThread(m.id) }, '💬') : null,
      canPin ? h('button', { class: 'cm-act' + (m.pinned ? ' on' : ''),
        title: m.pinned ? 'Unpin' : 'Pin to channel', 'aria-label': m.pinned ? 'Unpin' : 'Pin',
        onClick: () => togglePin(m) }, '📌') : null,
      mine ? h('button', { class: 'cm-act', title: 'Edit', 'aria-label': 'Edit message',
        onClick: () => startEdit(m) }, '✎') : null,
      canDelete ? h('button', { class: 'cm-act', title: mine ? 'Delete' : 'Delete (admin)',
        'aria-label': 'Delete message',
        onClick: () => deleteMessage(m) }, '🗑') : null);

    let kindClass = '';
    if (m.kind === 'digest_news' || m.kind === 'digest_papers') kindClass = ' cm-msg-digest';
    else if (m.kind === 'system_welcome') kindClass = ' cm-msg-welcome';
    else if (m.kind === 'event') kindClass = ' cm-msg-event';
    else if (m.kind === 'poll') kindClass = ' cm-msg-poll';
    if (/(^|[\s(])@(channel|here)\b/i.test(m.body || '')) kindClass += ' cm-msg-broadcast';

    const col = h('div', { class: 'cm-msg-col' },
      h('div', { class: 'cm-msg-head' },
        h('button', { class: 'cm-msg-author', onClick: a.user_id ? () => openMember(a.user_id) : null,
          onMouseenter: (e) => showHoverCard(a, e), onMouseleave: hideHoverCard },
          a.display_name || 'Former member'),
        a.verified ? h('span', { class: 'cm-verified', title: 'Credential-verified' }) : null,
        specChipEl(a),
        m.pinned ? h('span', { class: 'cm-pin-marker', title: 'Pinned' }, '📌') : null,
        h('span', { class: 'cm-msg-time' }, fmtTime(m.created_at),
          m.edited_at ? h('span', { class: 'cm-msg-edited' }, '(edited)') : null)),
      state.editing === m.id ? editBoxEl(m) : bodyEl,
      cardsEl(m.cards),
      m.poll ? pollCardEl(m) : null,
      attachmentsEl(m),
      reactionsEl(m),
      !inThread && m.reply_count > 0 ? threadTeaser(m) : null);
    return h('div', { class: 'cm-msg' + kindClass, 'data-mid': m.id, tabindex: '-1' },
      avatarEl(a), col, actions);
  }

  function threadTeaser(m) {
    return h('button', { class: 'cm-thread-teaser', onClick: () => openThread(m.id) },
      '💬 ' + m.reply_count + (m.reply_count === 1 ? ' reply' : ' replies'));
  }

  // ─── v2.1: polls ───────────────────────────────────────────────────────────
  function pollCardEl(m) {
    const p = m.poll;
    if (!p) return null;
    const total = p.total_votes || 0;
    const mine = state.me && (p.created_by === state.me.user_id);
    const box = h('div', { class: 'cm-poll' + (p.closed ? ' closed' : '') });
    box.appendChild(h('div', { class: 'cm-poll-q' }, p.question));
    for (const opt of p.options || []) {
      const pct = total ? Math.round((opt.votes / total) * 100) : 0;
      const chosen = p.your_vote === opt.id;
      box.appendChild(h('button', {
        class: 'cm-poll-opt' + (chosen ? ' chosen' : ''),
        // A view-only reader sees the result, which is the interesting part,
        // and cannot move it.
        disabled: (p.closed || !state.canPost) ? true : false,
        onClick: (p.closed || !state.canPost) ? null : () => votePoll(p.id, opt.id),
      },
        h('span', { class: 'cm-poll-fill', style: 'width:' + pct + '%' }),
        h('span', { class: 'cm-poll-opt-text' }, (chosen ? '✓ ' : '') + opt.text),
        h('span', { class: 'cm-poll-opt-pct' }, total ? pct + '%' : '')));
    }
    const foot = h('div', { class: 'cm-poll-foot' },
      h('span', {}, total + (total === 1 ? ' vote' : ' votes') + (p.closed ? ' · closed' : '')));
    if ((mine || state.isAdmin) && !p.closed) {
      foot.appendChild(h('button', { class: 'cm-linkbtn', onClick: () => closePoll(p.id) }, 'Close poll'));
    }
    box.appendChild(foot);
    return box;
  }

  async function votePoll(pollId, optionId) {
    try {
      const r = await api('/polls/' + pollId + '/vote', { method: 'POST', body: { option_id: optionId } });
      applyPoll(pollIdMessage(pollId), Object.assign({}, r, { your_vote: optionId }));
    } catch (e) { toast(e.message, 'error'); }
  }
  async function closePoll(pollId) {
    try { await api('/polls/' + pollId + '/close', { method: 'POST' }); }
    catch (e) { toast(e.message, 'error'); }
  }
  // Find the message id currently carrying a given poll (for local update).
  function pollIdMessage(pollId) {
    for (const key in state.msgs) {
      for (const m of (state.msgs[key].list || [])) {
        if (m.poll && m.poll.id === pollId) return m.id;
      }
    }
    return null;
  }
  function applyPoll(messageId, poll) {
    if (!messageId || !poll) return;
    for (const key in state.msgs) {
      const st = state.msgs[key];
      if (!st || !st.list) continue;
      st.list = st.list.map((m) => {
        if (m.id !== messageId) return m;
        // Preserve MY vote (broadcasts null it out for other viewers).
        const yv = (poll.your_vote != null) ? poll.your_vote
          : (m.poll ? m.poll.your_vote : null);
        return Object.assign({}, m, { poll: Object.assign({}, poll, { your_vote: yv }) });
      });
    }
    renderMessages({});
  }

  // ─── v2.1: events ──────────────────────────────────────────────────────────
  function toBasicUTC(iso) {
    // ISO-Z (2026-09-10T17:00:00Z) -> iCal basic UTC (20260910T170000Z)
    return String(iso || '').replace(/[-:]/g, '').replace(/\.\d+/, '');
  }
  function fmtEventTime(ev) {
    try {
      const d = new Date(ev.starts_at);
      const opts = { weekday: 'short', month: 'short', day: 'numeric',
        hour: 'numeric', minute: '2-digit' };
      if (ev.timezone) opts.timeZone = ev.timezone;
      let s = new Intl.DateTimeFormat([], opts).format(d);
      return s + (ev.timezone ? ' · ' + ev.timezone.split('/').pop().replace('_', ' ') : ' UTC');
    } catch (e) { return ev.starts_at; }
  }
  function gcalUrl(ev) {
    const start = toBasicUTC(ev.starts_at);
    let end = ev.ends_at ? toBasicUTC(ev.ends_at) : null;
    if (!end) {
      const d = new Date(ev.starts_at); d.setHours(d.getHours() + 1);
      end = toBasicUTC(d.toISOString());
    }
    const p = new URLSearchParams({
      action: 'TEMPLATE', text: ev.title || 'Event',
      dates: start + '/' + end,
      details: (ev.description || '') + (ev.host ? '\nHost: ' + ev.host : ''),
      location: ev.location || '',
    });
    if (ev.timezone) p.set('ctz', ev.timezone);
    return 'https://calendar.google.com/calendar/render?' + p.toString();
  }

  function eventCardEl(ev, opts) {
    opts = opts || {};
    const card = h('div', { class: 'cm-event-card' + (opts.pinned ? ' pinned' : '')
      + (ev.cancelled ? ' cancelled' : '') });
    if (opts.pinned) {
      card.appendChild(h('div', { class: 'cm-event-flag' },
        h('span', { class: 'chrome' }, ev.cancelled ? '📅 Cancelled' : '📌 Next event')));
    }
    card.appendChild(h('div', { class: 'cm-event-title' }, ev.title));
    card.appendChild(h('div', { class: 'cm-event-when' }, '🗓 ' + fmtEventTime(ev)));
    if (ev.location) {
      const isUrl = /^https?:\/\//i.test(ev.location);
      card.appendChild(h('div', { class: 'cm-event-where' }, '📍 ',
        isUrl ? h('a', { href: ev.location, target: '_blank', rel: 'noopener noreferrer' }, 'Join link')
          : ev.location));
    }
    if (ev.host) card.appendChild(h('div', { class: 'cm-event-host' }, '👤 ' + ev.host));
    if (ev.description) card.appendChild(h('div', { class: 'cm-event-desc' }, ev.description));
    if (!ev.cancelled) {
      const actions = h('div', { class: 'cm-event-actions' },
        // Interest is a message to the host about who is coming, so a view-only
        // reader does not send one. The calendar links stay: adding an event to
        // your own calendar tells nobody here anything.
        state.canPost ? h('button', {
          class: 'cm-btn ' + (ev.viewer_interested ? 'cm-btn-primary' : 'cm-btn-ghost'),
          onClick: () => rsvpEvent(ev.id),
        }, (ev.viewer_interested ? '✓ Interested' : 'Interested')
          + (ev.rsvp_count ? ' · ' + ev.rsvp_count : '')) : null,
        h('a', { class: 'cm-btn cm-btn-ghost', href: gcalUrl(ev), target: '_blank', rel: 'noopener noreferrer' },
          'Add to Google Calendar'),
        h('a', { class: 'cm-btn cm-btn-ghost', href: API + '/events/' + ev.id + '/calendar.ics?t=' + Date.now(),
          download: true }, 'Download .ics'));
      if (ev.message_id) {
        actions.appendChild(h('button', { class: 'cm-btn cm-btn-ghost',
          onClick: () => openThread(ev.message_id) }, 'Discuss'));
      }
      if (state.isAdmin) {
        actions.appendChild(h('button', { class: 'cm-linkbtn', onClick: () => cancelEvent(ev.id) }, 'Cancel'));
      }
      card.appendChild(actions);
    }
    return card;
  }

  function renderEventsBanner() {
    const wrap = h('div', { class: 'cm-events-banner' });
    const up = state.events.upcoming || [];
    if (state.isAdmin) {
      wrap.appendChild(h('button', { class: 'cm-btn cm-btn-primary cm-new-event',
        onClick: openNewEvent }, '+ New event'));
    }
    if (up.length) {
      wrap.appendChild(eventCardEl(up[0], { pinned: true }));
      if (up.length > 1) {
        const more = h('div', { class: 'cm-events-more' });
        more.appendChild(h('div', { class: 'cm-rail-label' },
          h('span', { class: 'chrome' }, 'More upcoming')));
        for (const ev of up.slice(1)) more.appendChild(eventCardEl(ev, {}));
        wrap.appendChild(more);
      }
    } else {
      wrap.appendChild(h('div', { class: 'cm-empty' },
        h('div', { class: 'cm-empty-title' }, 'No upcoming events'),
        h('p', {}, state.isAdmin ? 'Post one with “New event” — it pins to the top here.'
          : 'When the team posts an event, it pins here so you never miss it.')));
    }
    const past = state.events.past || [];
    if (past.length) {
      const toggle = h('button', { class: 'cm-past-toggle',
        onClick: () => { state.events.pastOpen = !state.events.pastOpen; renderMessages({}); } },
        (state.events.pastOpen ? '▾ ' : '▸ ') + 'Past events (' + past.length + ')');
      wrap.appendChild(toggle);
      if (state.events.pastOpen) {
        const list = h('div', { class: 'cm-events-past' });
        for (const ev of past) {
          list.appendChild(h('div', { class: 'cm-event-past-row' },
            h('span', { class: 'cm-event-past-title' }, ev.title),
            h('span', { class: 'cm-event-past-when' }, fmtEventTime(ev))));
        }
        wrap.appendChild(list);
      }
    }
    return wrap;
  }

  async function loadEvents() {
    try {
      const [up, past] = await Promise.all([
        api('/events?scope=upcoming'), api('/events?scope=past'),
      ]);
      state.events.upcoming = up.events || [];
      state.events.past = past.events || [];
      state.events.loadedFor = 'events';
    } catch (e) { /* transient */ }
  }
  async function rsvpEvent(eventId) {
    try {
      const r = await api('/events/' + eventId + '/rsvp', { method: 'POST' });
      applyEvent(r);
    } catch (e) { toast(e.message, 'error'); }
  }
  async function cancelEvent(eventId) {
    if (!window.confirm('Cancel this event? Interested members keep their calendar entry.')) return;
    try { const r = await api('/events/' + eventId + '/cancel', { method: 'POST' }); applyEvent(r.event || r); }
    catch (e) { toast(e.message, 'error'); }
  }
  function applyEvent(ev) {
    if (!ev || !ev.id) return;
    const patch = (arr) => arr.map((e) => (e.id === ev.id ? Object.assign({}, e, ev) : e));
    state.events.upcoming = patch(state.events.upcoming);
    state.events.past = patch(state.events.past);
    if (state.active === 'events') renderMessages({});
  }

  // ─── v2.1: modal + creation forms ──────────────────────────────────────────
  function openModal(title, buildBody) {
    const overlay = h('div', { class: 'cm-modal-overlay',
      onClick: (e) => { if (e.target === overlay) overlay.remove(); } });
    const close = () => overlay.remove();
    const modal = h('div', { class: 'cm-modal', role: 'dialog', 'aria-label': title });
    modal.appendChild(h('div', { class: 'cm-modal-head' },
      h('span', { class: 'cm-side-title' }, title),
      h('button', { class: 'cm-iconbtn', 'aria-label': 'Close', onClick: close }, '✕')));
    modal.appendChild(buildBody(close));
    overlay.appendChild(modal);
    document.body.appendChild(overlay);
    const first = modal.querySelector('input, textarea');
    if (first) first.focus();
  }

  function field(label, input) {
    return h('label', { class: 'cm-field' }, h('span', {}, label), input);
  }

  function openNewEvent() {
    openModal('New event', (close) => {
      const title = h('input', { type: 'text', maxlength: '200', placeholder: 'Nephrology Journal Club' });
      const date = h('input', { type: 'date' });
      const time = h('input', { type: 'time' });
      const tz = h('input', { type: 'text', placeholder: 'America/New_York',
        value: (Intl.DateTimeFormat().resolvedOptions().timeZone || '') });
      const loc = h('input', { type: 'text', maxlength: '500', placeholder: 'Zoom link or address' });
      const host = h('input', { type: 'text', maxlength: '200', placeholder: 'Dr. Raman' });
      const desc = h('textarea', { rows: '3', placeholder: 'What is it about? (no patient identifiers)' });
      const body = h('div', { class: 'cm-modal-body' },
        field('Title', title),
        h('div', { class: 'cm-field-row' }, field('Date', date), field('Time', time)),
        field('Timezone', tz),
        field('Location or join link', loc),
        field('Host (optional)', host),
        field('Description (optional)', desc),
        h('div', { class: 'cm-modal-actions' },
          h('button', { class: 'cm-btn cm-btn-ghost', onClick: close }, 'Cancel'),
          h('button', { class: 'cm-btn cm-btn-primary', onClick: async () => {
            if (!title.value.trim() || !date.value || !time.value) {
              toast('Title, date and time are required.', 'error'); return;
            }
            try {
              await api('/events', { method: 'POST', body: {
                title: title.value.trim(), description: desc.value.trim(),
                starts_at: date.value + 'T' + time.value,
                timezone: tz.value.trim() || null,
                location: loc.value.trim(), host: host.value.trim(),
                channel_slug: 'events',
              } });
              close();
              toast('Event posted — pinned to the top of #events.');
            } catch (e) {
              toast(e.status === 422 ? (e.detail && e.detail.message) || e.message : e.message, 'error');
            }
          } }, 'Post event')));
      return body;
    });
  }

  function openNewPoll(slug) {
    openModal('New poll', (close) => {
      const q = h('input', { type: 'text', maxlength: '300', placeholder: 'How would you manage this?' });
      const opts = [
        h('input', { type: 'text', maxlength: '120', placeholder: 'Option 1' }),
        h('input', { type: 'text', maxlength: '120', placeholder: 'Option 2' }),
      ];
      const optsHost = h('div', { class: 'cm-poll-opts-edit' });
      function renderOpts() {
        clear(optsHost);
        opts.forEach((inp) => optsHost.appendChild(inp));
        if (opts.length < 6) {
          optsHost.appendChild(h('button', { class: 'cm-linkbtn', onClick: () => {
            opts.push(h('input', { type: 'text', maxlength: '120', placeholder: 'Option ' + (opts.length + 1) }));
            renderOpts();
          } }, '+ Add option'));
        }
      }
      renderOpts();
      return h('div', { class: 'cm-modal-body' },
        field('Question', q),
        field('Options', optsHost),
        h('div', { class: 'cm-modal-actions' },
          h('button', { class: 'cm-btn cm-btn-ghost', onClick: close }, 'Cancel'),
          h('button', { class: 'cm-btn cm-btn-primary', onClick: async () => {
            const options = opts.map((i) => i.value.trim()).filter(Boolean);
            if (!q.value.trim() || options.length < 2) {
              toast('A question and at least two options are required.', 'error'); return;
            }
            try {
              await api('/polls', { method: 'POST', body: {
                channel_slug: slug, question: q.value.trim(), options } });
              close();
            } catch (e) {
              toast(e.status === 422 ? (e.detail && e.detail.message) || e.message : e.message, 'error');
            }
          } }, 'Post poll')));
    });
  }

  // ─── v2.1: pinned messages ─────────────────────────────────────────────────
  async function togglePin(m) {
    const pinned = !!m.pinned;
    try {
      await api('/messages/' + m.id + '/pin', { method: pinned ? 'DELETE' : 'POST' });
    } catch (e) { toast(e.message, 'error'); }
  }
  function openPins(slug) {
    state.sidePanel = 'pins';
    const side = document.getElementById('cmSide');
    clear(side);
    side.appendChild(h('div', { class: 'cm-side-head' },
      h('span', { class: 'cm-side-title' }, 'Pinned messages'),
      h('button', { class: 'cm-iconbtn', 'aria-label': 'Close panel', onClick: closeSide }, '✕')));
    const body = h('div', { class: 'cm-side-body' });
    const pins = state.pins[slug] || [];
    if (!pins.length) {
      body.appendChild(h('div', { class: 'cm-empty' },
        h('p', {}, 'Nothing pinned yet. Use the 📌 on any message to keep it here.')));
    }
    for (const m of pins) body.appendChild(messageEl(m, { context: 'thread' }));
    side.appendChild(body);
    openSide();
  }
  async function loadPins(slug) {
    try { const d = await api('/channels/' + slug + '/pins'); state.pins[slug] = d.pins || []; }
    catch (e) { /* transient */ }
  }

  // ─── v2.1: channel bookmarks ───────────────────────────────────────────────
  async function loadBookmarks(slug) {
    try { const d = await api('/channels/' + slug + '/bookmarks'); state.bookmarks[slug] = d.bookmarks || []; }
    catch (e) { /* transient */ }
  }
  function renderBookmarkBar(slug) {
    const marks = state.bookmarks[slug] || [];
    if (!marks.length && !state.me) return null;
    const bar = h('div', { class: 'cm-bookmark-bar' });
    for (const bm of marks) {
      bar.appendChild(h('span', { class: 'cm-bookmark' },
        h('a', { href: bm.url, target: '_blank', rel: 'noopener noreferrer', title: bm.url }, '🔖 ' + bm.title),
        (state.canPost && (bm.added_by === (state.me && state.me.user_id) || state.isAdmin))
          ? h('button', { class: 'cm-bookmark-x', 'aria-label': 'Remove bookmark',
              onClick: () => removeBookmark(bm.id) }, '✕') : null));
    }
    if (state.canPost) {
      bar.appendChild(h('button', { class: 'cm-bookmark-add', title: 'Add a bookmark',
        onClick: () => openNewBookmark(slug) }, '+'));
    } else if (!marks.length) {
      return null;
    }
    return bar;
  }
  function openNewBookmark(slug) {
    openModal('Add a bookmark', (close) => {
      const title = h('input', { type: 'text', maxlength: '120', placeholder: 'KDIGO 2024 guideline' });
      const url = h('input', { type: 'url', maxlength: '1000', placeholder: 'https://…' });
      return h('div', { class: 'cm-modal-body' },
        field('Title', title), field('URL', url),
        h('div', { class: 'cm-modal-actions' },
          h('button', { class: 'cm-btn cm-btn-ghost', onClick: close }, 'Cancel'),
          h('button', { class: 'cm-btn cm-btn-primary', onClick: async () => {
            if (!title.value.trim() || !/^https?:\/\//i.test(url.value.trim())) {
              toast('A title and an http(s) URL are required.', 'error'); return;
            }
            try {
              await api('/channels/' + slug + '/bookmarks', { method: 'POST',
                body: { title: title.value.trim(), url: url.value.trim() } });
              close();
            } catch (e) { toast(e.message, 'error'); }
          } }, 'Add')));
    });
  }
  async function removeBookmark(id) {
    try { await api('/bookmarks/' + id, { method: 'DELETE' }); }
    catch (e) { toast(e.message, 'error'); }
  }

  function reactionsEl(m) {
    const groups = m.reactions || [];
    if (!groups.length) return null;
    const box = h('div', { class: 'cm-reactions' });
    for (const g of groups) {
      const mine = state.me && (g.user_ids || []).indexOf(state.me.user_id) !== -1;
      const names = (g.user_ids || []).map((id) => {
        const mem = state.membersById[id];
        return mem ? mem.display_name : 'former member';
      }).join(', ');
      box.appendChild(h('button', {
        class: 'cm-react' + (mine ? ' mine' : ''),
        title: names,
        'aria-label': g.emoji + ' ' + g.count + ' — ' + names,
        // Who reacted is worth seeing even when you may not join in.
        onClick: state.canPost ? () => react(m, g.emoji) : null,
      }, g.emoji, ' ', h('span', { class: 'n' }, String(g.count))));
    }
    return box;
  }

  const attImgCache = {};
  function attachmentsEl(m) {
    const atts = m.attachments || [];
    if (!atts.length) return null;
    const box = h('div', { class: 'cm-atts' });
    for (const att of atts) {
      if ((att.mime || '').indexOf('image/') === 0) {
        const img = h('img', { class: 'cm-att-img', alt: att.name || 'attachment' });
        loadAttachmentBlob(att.asset_id).then((url) => { if (url) img.src = url; });
        box.appendChild(h('button', { class: 'cm-iconbtn', style: 'padding:0;border:0',
          'aria-label': 'Download ' + (att.name || 'image'),
          onClick: () => downloadAttachment(att) }, img));
      } else {
        box.appendChild(h('button', { class: 'cm-att-file', onClick: () => downloadAttachment(att) },
          '📎 ', att.name || 'attachment',
          h('span', { class: 'chrome' }, Math.max(1, Math.round((att.byte_size || 0) / 1024)) + ' kb')));
      }
    }
    return box;
  }
  async function loadAttachmentBlob(assetId) {
    if (attImgCache[assetId]) return attImgCache[assetId];
    try {
      const res = await fetch(API + '/attachments/' + encodeURIComponent(assetId), {
        headers: { 'Authorization': 'Bearer ' + state.token },
      });
      if (!res.ok) return null;
      const url = URL.createObjectURL(await res.blob());
      attImgCache[assetId] = url;
      return url;
    } catch (e) { return null; }
  }
  async function downloadAttachment(att) {
    const url = await loadAttachmentBlob(att.asset_id);
    if (!url) return toast('Could not load the attachment.', 'error');
    const a = h('a', { href: url, download: att.name || 'attachment' });
    document.body.appendChild(a); a.click(); a.remove();
  }

  // ─── Reactions / emoji popover ─────────────────────────────────────────────
  function closeEmoji() {
    state.emojiFor = null;
    document.querySelectorAll('.cm-emoji-pop').forEach((n) => n.remove());
  }
  function toggleEmojiPop(m) {
    if (state.emojiFor === m.id) { closeEmoji(); return; }
    closeEmoji();
    state.emojiFor = m.id;
    const host = document.querySelector('.cm-msg[data-mid="' + m.id + '"]');
    if (!host) return;
    const pop = h('div', { class: 'cm-emoji-pop', role: 'menu' });
    for (const e of QUICK_EMOJI) {
      pop.appendChild(h('button', { role: 'menuitem', 'aria-label': 'React ' + e,
        onClick: () => { react(m, e); closeEmoji(); } }, e));
    }
    host.appendChild(pop);
  }
  async function react(m, emoji) {
    try {
      const d = await api('/messages/' + m.id + '/reactions', { method: 'POST', body: { emoji } });
      applyReactions(m.id, d.reactions);
    } catch (e) { toast(e.message, 'error'); }
  }
  function applyReactions(mid, reactions) {
    for (const slug in state.msgs) {
      for (const m of state.msgs[slug].list) if (m.id === mid) m.reactions = reactions;
    }
    if (state.thread) {
      if (state.thread.root && state.thread.root.id === mid) state.thread.root.reactions = reactions;
      for (const r of state.thread.replies) if (r.id === mid) r.reactions = reactions;
    }
    renderMessages({});
    renderThreadPanel();
  }

  // ─── Edit / delete ─────────────────────────────────────────────────────────
  function startEdit(m) { state.editing = m.id; renderMessages({}); renderThreadPanel(); }
  function editBoxEl(m) {
    const ta = h('textarea', { value: m.body, 'aria-label': 'Edit message' });
    const save = async () => {
      const body = ta.value.trim();
      if (!body) return;
      try {
        await api('/messages/' + m.id, { method: 'PATCH', body: { body } });
        state.editing = null;
        // WS event updates the copy; also update locally for the no-WS case.
        m.body = body; m.edited_at = new Date().toISOString();
        renderMessages({}); renderThreadPanel();
      } catch (e) {
        if (e.status === 422 && e.detail && e.detail.code === 'phi_detected') {
          toast(e.detail.message, 'error');
        } else toast(e.message, 'error');
      }
    };
    const box = h('div', { class: 'cm-edit-box' }, ta,
      h('div', { class: 'cm-edit-actions' },
        h('button', { class: 'cm-btn cm-btn-primary', onClick: save }, 'Save'),
        h('button', { class: 'cm-btn cm-btn-ghost',
          onClick: () => { state.editing = null; renderMessages({}); renderThreadPanel(); } }, 'Cancel')));
    ta.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); save(); }
      if (e.key === 'Escape') { state.editing = null; renderMessages({}); renderThreadPanel(); }
    });
    setTimeout(() => ta.focus(), 0);
    return box;
  }
  async function deleteMessage(m) {
    if (!window.confirm('Delete this message? This cannot be undone.')) return;
    try {
      await api('/messages/' + m.id, { method: 'DELETE' });
      applyDelete(m.id, m.parent_message_id);
    } catch (e) { toast(e.message, 'error'); }
  }
  function applyDelete(mid, parentId) {
    for (const slug in state.msgs) {
      const st = state.msgs[slug];
      // A deleted REPLY decrements its root's thread teaser (audit finding —
      // the count went stale until a full channel reload).
      if (parentId) {
        for (const m of st.list) {
          if (m.id === parentId) {
            if (m._replyIds) m._replyIds.delete(mid);
            m.reply_count = Math.max(0, (m.reply_count || 0) - 1);
          }
        }
      }
      st.list = st.list.map((m) => m.id === mid
        ? Object.assign({}, m, { deleted: true, body: '', reactions: [], attachments: [] })
        : m).filter((m) => !m.deleted || m.reply_count > 0);
    }
    if (state.thread) {
      state.thread.replies = state.thread.replies.filter((r) => r.id !== mid);
      if (state.thread.root && state.thread.root.id === mid) closeSide();
    }
    renderMessages({}); renderThreadPanel();
  }

  // ─── Composer ──────────────────────────────────────────────────────────────
  function composerState(key) {
    if (!state._composers) state._composers = {};
    if (!state._composers[key]) {
      state._composers[key] = { attachments: [], phi: null, mentionIds: [], typingSentAt: 0 };
    }
    return state._composers[key];
  }

  function renderComposer() {
    const wrap = document.getElementById('cmComposerWrap');
    if (!wrap) return;
    clear(wrap);
    // A user-level read-only account (advisor). Checked before the channel and
    // DM branches because it holds everywhere: there is no room in here they
    // may post in, so there is no composer to build anywhere.
    if (!state.canPost) {
      wrap.appendChild(h('div', { class: 'cm-composer', style: 'padding: var(--sp-3)' },
        h('div', { class: 'cm-composer-hint' },
          'You have view-only access. Every channel is open to read; posting is '
          + 'for the physicians doing the work.')));
      wrap.appendChild(h('div', { class: 'cm-phi-notice' },
        h('span', { class: 'dot dot-pink', 'aria-hidden': 'true' }), PHI_NOTICE));
      return;
    }
    if (isDmKey(state.active)) {
      const d = activeDm();
      const peer = (d && d.peer) || {};
      wrap.appendChild(buildComposer({
        key: 'dm:' + state.active,
        placeholder: 'Message ' + (peer.display_name || 'colleague') + '…',
        onSend: (body, cs) => sendDmMessage(state.active, body, cs),
        typingMeta: { dm: state.active },
      }));
      return;
    }
    const ch = activeChannel();
    if (!ch) return;
    if (ch.post_policy === 'admin' && !state.isAdmin) {
      wrap.appendChild(h('div', { class: 'cm-composer', style: 'padding: var(--sp-3)' },
        h('div', { class: 'cm-composer-hint' },
          'Only the Archangel team posts in #' + ch.slug + '. Open any post’s thread to reply or ask about it.')));
      wrap.appendChild(h('div', { class: 'cm-phi-notice' },
        h('span', { class: 'dot dot-pink', 'aria-hidden': 'true' }), PHI_NOTICE));
      return;
    }
    wrap.appendChild(buildComposer({
      key: 'ch:' + ch.slug,
      placeholder: 'Message #' + ch.slug + '…',
      onSend: (body, cs) => sendMessage(ch.slug, body, cs, null),
      typingMeta: { channel: ch.slug },
      pollSlug: ch.slug,   // v2.1: 📊 poll affordance in all-post channels
    }));
  }

  function buildComposer(cfg) {
    const cs = composerState(cfg.key);
    const anchor = h('div', { class: 'cm-composer-anchor' });
    const warnHost = h('div', {});
    const pendingHost = h('div', {});
    const hintEl = h('div', { class: 'cm-phi-hint', hidden: true, 'aria-live': 'polite' });
    const ta = h('textarea', {
      placeholder: cfg.placeholder, 'aria-label': cfg.placeholder,
      rows: '1',
    });
    const sendBtn = h('button', { class: 'cm-btn cm-btn-primary', 'aria-label': 'Send message' }, 'Send');

    function refreshPending() {
      clear(pendingHost);
      if (!cs.attachments.length) return;
      const box = h('div', { class: 'cm-pending-atts' });
      cs.attachments.forEach((att, i) => {
        box.appendChild(h('span', { class: 'cm-pending-att' }, '📎 ' + att.name,
          h('button', { 'aria-label': 'Remove attachment ' + att.name,
            onClick: () => { cs.attachments.splice(i, 1); refreshPending(); } }, '✕')));
      });
      pendingHost.appendChild(box);
    }

    function renderWarn() {
      clear(warnHost);
      if (!cs.phi) return;
      const { detail, body } = cs.phi;
      // §7.3: highlight the exact spans in a preview of the text the user
      // typed. Server spans are code-point indices — convert to UTF-16 first
      // so emoji ahead of an identifier can't shift the highlight.
      let htmlParts = [];
      let cursor = 0;
      const spans = spansToUtf16(body,
        (detail.findings || []).map((f) => f.span).sort((a, b) => a[0] - b[0]));
      for (const [s, e] of spans) {
        if (s >= cursor) {
          htmlParts.push(esc(body.slice(cursor, s)));
          htmlParts.push('<mark>' + esc(body.slice(s, e)) + '</mark>');
          cursor = e;
        }
      }
      htmlParts.push(esc(body.slice(cursor)));
      warnHost.appendChild(h('div', { class: 'cm-phi-warn', role: 'alert' },
        h('div', { class: 'cm-phi-warn-title' }, detail.message ||
          'This looks like it contains patient-identifiable information. Remove it to post.'),
        h('div', { class: 'cm-phi-warn-preview', html: htmlParts.join('') }),
        h('div', { class: 'cm-phi-warn-actions' },
          h('button', { class: 'cm-btn cm-btn-primary', onClick: () => {
            // one-tap "remove and post" (§7.3): strip flagged spans, re-send
            let cleaned = body;
            for (const [s, e] of spans.slice().sort((a, b) => b[0] - a[0])) {
              cleaned = cleaned.slice(0, s) + cleaned.slice(e);
            }
            cleaned = cleaned.replace(/[ \t]{2,}/g, ' ').trim();
            cs.phi = null; renderWarn();
            ta.value = cleaned;
            doSend();
          } }, 'Remove flagged text & post'),
          h('button', { class: 'cm-btn cm-btn-ghost', onClick: () => { cs.phi = null; renderWarn(); ta.focus(); } },
            'Edit it myself'))));
    }

    async function doSend() {
      const body = ta.value.trim();
      if (!body && !cs.attachments.length) return;
      sendBtn.setAttribute('disabled', '');
      try {
        await cfg.onSend(body, cs);
        ta.value = '';
        ta.style.height = 'auto';
        cs.attachments = [];
        cs.phi = null;
        refreshPending(); renderWarn();
        hintEl.setAttribute('hidden', '');
      } catch (e) {
        if (e.status === 422 && e.detail && e.detail.code === 'phi_detected') {
          cs.phi = { detail: e.detail, body };
          renderWarn();
        } else {
          toast(e.message, 'error');
        }
      }
      sendBtn.removeAttribute('disabled');
      ta.focus();
    }
    sendBtn.addEventListener('click', doSend);

    // mention autocomplete
    let mentionPop = null; let mentionSel = 0; let mentionMatches = [];
    function closeMentions() { if (mentionPop) { mentionPop.remove(); mentionPop = null; } }
    function openMentions(prefix) {
      closeMentions();
      const q = prefix.toLowerCase();
      mentionMatches = state.members.filter((m) =>
        m.user_id !== (state.me && state.me.user_id) &&
        m.display_name.toLowerCase().indexOf(q) !== -1).slice(0, 8);
      if (!mentionMatches.length) return;
      mentionSel = 0;
      mentionPop = h('div', { class: 'cm-mention-pop', role: 'listbox' });
      mentionMatches.forEach((m, i) => {
        mentionPop.appendChild(h('button', {
          class: 'cm-mention-opt' + (i === mentionSel ? ' sel' : ''), role: 'option',
          onClick: () => pickMention(m),
        }, avatarEl(m, 'small'), h('span', {}, m.display_name),
          m.specialty ? h('span', { class: 'cm-member-row-spec' }, m.specialty) : null));
      });
      anchor.appendChild(mentionPop);
    }
    function pickMention(m) {
      const pos = ta.selectionStart;
      const before = ta.value.slice(0, pos).replace(/@([\w .\-]*)$/, '@' + m.display_name + ' ');
      ta.value = before + ta.value.slice(pos);
      if (cs.mentionIds.indexOf(m.user_id) === -1) cs.mentionIds.push(m.user_id);
      closeMentions();
      ta.focus();
      ta.selectionStart = ta.selectionEnd = before.length;
    }

    ta.addEventListener('input', () => {
      ta.style.height = 'auto';
      ta.style.height = Math.min(180, ta.scrollHeight) + 'px';
      // live PHI hint (client-side pre-check — server is authoritative, §7.1)
      const hint = phiHint(ta.value);
      if (hint) {
        hintEl.textContent = 'Heads up: this looks like it may contain ' + hint + '. Identifiers are blocked at send.';
        hintEl.removeAttribute('hidden');
      } else hintEl.setAttribute('hidden', '');
      // mention autocomplete on a trailing @token
      const uptoCaret = ta.value.slice(0, ta.selectionStart);
      const m = /(^|\s)@([\w .\-]{0,30})$/.exec(uptoCaret);
      if (m) openMentions(m[2]); else closeMentions();
      // typing signal (throttled)
      const now = Date.now();
      if (state.wsOk && now - cs.typingSentAt > 2000 && ta.value) {
        cs.typingSentAt = now;
        try {
          state.ws.send(JSON.stringify(Object.assign({ type: 'typing' }, cfg.typingMeta || {})));
        } catch (e) { /* socket mid-flap */ }
      }
    });
    ta.addEventListener('keydown', (e) => {
      if (mentionPop) {
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
          e.preventDefault();
          mentionSel = (mentionSel + (e.key === 'ArrowDown' ? 1 : mentionMatches.length - 1)) % mentionMatches.length;
          Array.prototype.forEach.call(mentionPop.children, (c, i) => c.classList.toggle('sel', i === mentionSel));
          return;
        }
        if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); pickMention(mentionMatches[mentionSel]); return; }
        if (e.key === 'Escape') { closeMentions(); return; }
      }
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); doSend(); }
    });

    // attachments
    const fileInput = h('input', { type: 'file', hidden: true,
      accept: '.png,.jpg,.jpeg,.pdf,.txt,.csv,.md,image/png,image/jpeg,application/pdf,text/plain,text/csv,text/markdown' });
    fileInput.addEventListener('change', async () => {
      const file = fileInput.files && fileInput.files[0];
      fileInput.value = '';
      if (!file) return;
      const fd = new FormData();
      fd.append('file', file);
      try {
        toast('Screening attachment…');
        const att = await api('/attachments', { method: 'POST', body: fd, isForm: true });
        cs.attachments.push(att);
        refreshPending();
      } catch (e) {
        if (e.status === 422 && e.detail && e.detail.code === 'phi_detected') {
          toast(e.detail.message, 'error');
        } else toast(e.message, 'error');
      }
    });

    const composer = h('div', { class: 'cm-composer' },
      pendingHost, ta,
      h('div', { class: 'cm-composer-row' },
        h('button', { class: 'cm-iconbtn', title: 'Attach a file (screened for identifiers)',
          'aria-label': 'Attach a file', onClick: () => fileInput.click() }, '📎'),
        cfg.pollSlug ? h('button', { class: 'cm-iconbtn', title: 'Create a poll',
          'aria-label': 'Create a poll', onClick: () => openNewPoll(cfg.pollSlug) }, '📊') : null,
        h('div', { class: 'cm-composer-hint' }, 'Enter to send · Shift+Enter for a new line · **bold** *italic* `code`'),
        sendBtn),
      fileInput);

    anchor.appendChild(warnHost);
    anchor.appendChild(composer);
    anchor.appendChild(hintEl);
    // §7.5: standing deterrent — always visible, never dismissible.
    anchor.appendChild(h('div', { class: 'cm-phi-notice' },
      h('span', { class: 'dot dot-pink', 'aria-hidden': 'true' }), PHI_NOTICE));
    return anchor;
  }

  function mentionsInBody(body, csMentionIds) {
    // Ids picked via autocomplete, kept only if the @Name still appears.
    const ids = [];
    for (const uid of csMentionIds || []) {
      const m = state.membersById[uid];
      if (m && body.indexOf('@' + m.display_name) !== -1) ids.push(uid);
    }
    // Also catch hand-typed exact @Full Name mentions.
    for (const m of state.members) {
      if (ids.indexOf(m.user_id) === -1 && body.indexOf('@' + m.display_name) !== -1) {
        ids.push(m.user_id);
      }
    }
    return ids;
  }

  async function sendMessage(slug, body, cs, parentId) {
    const payload = {
      body,
      mention_user_ids: mentionsInBody(body, cs.mentionIds),
      attachment_ids: cs.attachments.map((a) => a.asset_id),
    };
    if (parentId) payload.parent_message_id = parentId;
    const msg = await api('/channels/' + encodeURIComponent(slug) + '/messages',
      { method: 'POST', body: payload });
    cs.mentionIds = [];
    ingestMessage(msg);
  }

  async function sendDmMessage(dmId, body, cs) {
    const msg = await api('/dms/' + encodeURIComponent(dmId) + '/messages', {
      method: 'POST',
      body: { body, attachment_ids: cs.attachments.map((a) => a.asset_id) },
    });
    ingestMessage(msg);
    const d = state.dms.find((x) => x.id === dmId);
    if (d) { d.last_message_id = msg.id; d.last_message_at = msg.created_at; }
  }

  // ─── Threads ───────────────────────────────────────────────────────────────
  async function openThread(rootId) {
    try {
      const d = await api('/messages/' + rootId + '/thread');
      state.thread = { rootId: d.root.id, root: d.root, replies: d.replies || [] };
      state.sidePanel = 'thread';
      renderThreadPanel();
      openSide();
    } catch (e) { toast(e.message, 'error'); }
  }

  function renderThreadPanel() {
    if (state.sidePanel !== 'thread' || !state.thread) return;
    const side = document.getElementById('cmSide');
    if (!side) return;
    clear(side);
    const t = state.thread;
    side.appendChild(h('div', { class: 'cm-side-head' },
      h('span', { class: 'cm-side-title' }, 'Thread'),
      h('button', { class: 'cm-iconbtn', 'aria-label': 'Close panel', onClick: closeSide }, '✕')));
    const body = h('div', { class: 'cm-side-body' });
    body.appendChild(messageEl(t.root, { context: 'thread' }));
    body.appendChild(h('div', { class: 'cm-thread-root-sep' },
      t.replies.length + (t.replies.length === 1 ? ' reply' : ' replies')));
    if (!t.replies.length) {
      body.appendChild(h('div', { class: 'cm-empty' },
        h('p', {}, 'No replies yet. Keep questions about this post attached here.')));
    }
    for (const r of t.replies) body.appendChild(messageEl(r, { context: 'thread' }));
    side.appendChild(body);
    const composerHost = h('div', { class: 'cm-side-composer' });
    composerHost.appendChild(buildComposer({
      key: 'th:' + t.rootId,
      placeholder: 'Reply in thread…',
      onSend: (bodyTxt, cs) => sendMessage(t.root.channel, bodyTxt, cs, t.rootId),
      typingMeta: { channel: t.root.channel, thread_root: t.rootId },
    }));
    side.appendChild(composerHost);
    body.scrollTop = body.scrollHeight;
  }

  // ─── Member profile panel + hover card (§2) ────────────────────────────────
  function profileRows(m) {
    const rows = [];
    const add = (k, v) => { if (v != null && v !== '') rows.push(h('div', { class: 'cm-profile-row' }, h('dt', {}, k), h('dd', {}, String(v)))); };
    add('Specialty', m.specialty);
    add('Years in practice', m.years_in_practice);
    add('Institution', m.institution);
    add('Board-certified', m.board_certified ? 'Yes' : null);
    add('Fellowship-trained', m.fellowship_trained ? 'Yes' : null);
    add('Status', m.is_staff ? 'Archangel Health team' : (m.verified ? 'Credential-verified' : null));
    return rows;
  }

  function openMember(userId) {
    const m = state.membersById[userId];
    if (!m) return;
    hideHoverCard();
    state.sidePanel = 'member';
    state.sideMember = m;
    const side = document.getElementById('cmSide');
    clear(side);
    side.appendChild(h('div', { class: 'cm-side-head' },
      h('span', { class: 'cm-side-title' }, 'Profile'),
      h('button', { class: 'cm-iconbtn', 'aria-label': 'Close panel', onClick: closeSide }, '✕')));
    const body = h('div', { class: 'cm-side-body' });
    body.appendChild(h('div', { class: 'cm-profile' },
      h('div', { class: 'cm-profile-head' },
        avatarEl(m, 'big'),
        h('div', {},
          h('div', { class: 'cm-profile-name' }, m.display_name,
            m.verified ? h('span', { class: 'cm-verified', title: 'Credential-verified' }) : null),
          h('div', { class: 'chrome' }, state.online.has(m.user_id) ? 'online' : 'offline'))),
      state.canPost && state.me && m.user_id !== state.me.user_id
        ? h('button', {
            class: 'cm-btn cm-btn-primary',
            style: 'width:100%;justify-content:center;margin-bottom:var(--sp-4)',
            onClick: () => startDmWith(m.user_id),
          }, 'Send a message')
        : null,
      m.blurb ? h('div', { class: 'cm-profile-blurb' }, m.blurb) : null,
      h('dl', { class: 'cm-profile-rows' }, profileRows(m))));
    side.appendChild(body);
    openSide();
  }

  let hoverCardEl = null; let hoverTimer = null;
  function showHoverCard(author, evt) {
    if (!author || !author.user_id) return;
    const m = state.membersById[author.user_id] || author;
    hoverTimer = setTimeout(() => {
      hideHoverCard();
      hoverCardEl = h('div', { class: 'cm-hovercard', role: 'tooltip' },
        h('div', { class: 'cm-profile-head', style: 'margin-bottom:8px' },
          avatarEl(m),
          h('div', {},
            h('div', { style: 'font-weight:500' }, m.display_name,
              m.verified ? h('span', { class: 'cm-verified', style: 'margin-left:6px' }) : null),
            specChipEl(m))),
        m.blurb ? h('div', { style: 'font-size:var(--t-sm);color:var(--ink-soft)' }, m.blurb) : null);
      document.body.appendChild(hoverCardEl);
      const r = hoverCardEl.getBoundingClientRect();
      let x = Math.min(evt.clientX + 12, window.innerWidth - r.width - 12);
      let y = evt.clientY + 14;
      if (y + r.height > window.innerHeight - 12) y = evt.clientY - r.height - 10;
      hoverCardEl.style.left = x + 'px';
      hoverCardEl.style.top = Math.max(8, y) + 'px';
    }, 350);
  }
  function hideHoverCard() {
    if (hoverTimer) { clearTimeout(hoverTimer); hoverTimer = null; }
    if (hoverCardEl) { hoverCardEl.remove(); hoverCardEl = null; }
  }

  function openSide() { document.getElementById('cmSide').classList.add('open'); }
  function closeSide() {
    state.sidePanel = null;
    state.thread = null;
    const side = document.getElementById('cmSide');
    if (side) { side.classList.remove('open'); clear(side); }
  }

  // ─── Search ────────────────────────────────────────────────────────────────
  let searchTimer = null;
  function closeSearchPop(wrap) {
    const pop = wrap.querySelector('.cm-search-pop');
    if (pop) pop.remove();
  }
  function onSearchInput(q, wrap) {
    if (searchTimer) clearTimeout(searchTimer);
    q = q.trim();
    if (!q) { closeSearchPop(wrap); return; }
    searchTimer = setTimeout(async () => {
      let d;
      try { d = await api('/search?q=' + encodeURIComponent(q)); }
      catch (e) { return; }
      closeSearchPop(wrap);
      const pop = h('div', { class: 'cm-search-pop', role: 'listbox' });
      if (!d.results.length) {
        pop.appendChild(h('div', { class: 'cm-search-empty' },
          'No messages match “' + q + '”.'));
      }
      for (const m of d.results.slice(0, 30)) {
        pop.appendChild(h('button', { class: 'cm-search-hit', role: 'option',
          onClick: () => { closeSearchPop(wrap); jumpToMessage(m); } },
          h('div', { class: 'cm-search-hit-meta' },
            h('span', { class: 'chrome chrome-strong' }, m.dm ? 'DM' : '#' + m.channel),
            h('span', { class: 'chrome' }, (m.author && m.author.display_name) || ''),
            h('span', { class: 'chrome' }, fmtDay(m.created_at))),
          h('div', { class: 'cm-search-hit-body' }, m.body)));
      }
      wrap.appendChild(pop);
    }, 300);
  }
  async function jumpToMessage(m) {
    const key = m.dm || m.channel;
    const targetId = m.parent_message_id || m.id;
    await openContainer(key, { force: true });
    // walk history until the message is in the loaded page (bounded)
    let tries = 0;
    while (tries++ < 6) {
      const st = state.msgs[key];
      if (st.list.some((x) => x.id === targetId) || !st.hasMore) break;
      const oldest = st.list.length ? st.list[0].id : null;
      if (!oldest) break;
      const d = await api(messagesUrl(key) + '?limit=100&before=' + oldest);
      st.list = (d.messages || []).concat(st.list);
      st.hasMore = !!d.has_more;
    }
    renderMessages({});
    const node = document.querySelector('.cm-msg[data-mid="' + targetId + '"]');
    if (node) {
      node.scrollIntoView({ block: 'center' });
      node.classList.add('highlight');
      setTimeout(() => node.classList.remove('highlight'), 2500);
    }
    if (m.parent_message_id) openThread(m.parent_message_id);
  }

  // ─── Unread / read cursor ──────────────────────────────────────────────────
  let readTimer = null;
  function markReadIfAtBottom(force) {
    const scroll = document.getElementById('cmScroll');
    const st = state.msgs[state.active];
    if (!scroll || !st || !st.list.length) return;
    if (document.hidden) return;
    const atBottom = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 60;
    if (!atBottom && !force) return;
    const lastId = st.list[st.list.length - 1].id;
    const key = state.active;
    const container = isDmKey(key) ? activeDm() : activeChannel();
    if (!container) return;
    if ((container.unread || 0) === 0 && !force) return;
    if (readTimer) clearTimeout(readTimer);
    readTimer = setTimeout(async () => {
      try {
        const d = await api(readUrl(key),
          { method: 'POST', body: { last_read_message_id: lastId } });
        if (isDmKey(key)) {
          const dm = state.dms.find((x) => x.id === key);
          if (dm) dm.unread = 0;
        } else {
          for (const c of state.channels) {
            const u = (d.unread || {})[c.slug];
            if (u) { c.unread = u.unread; c.mentions = u.mentions; }
          }
        }
        renderRail();
      } catch (e) { /* transient */ }
    }, 400);
  }

  // ─── Real-time: WS + polling fallback (§4) ─────────────────────────────────
  async function connectWs() {
    // Exchange the JWT for a short-lived single-use ticket over an ordinary
    // authenticated POST, so the long-lived token never rides a URL where
    // access/proxy logs could capture it (audit finding).
    let ticket = null;
    try {
      const d = await api('/ws-ticket', { method: 'POST', body: {} });
      ticket = d && d.ticket;
    } catch (e) { /* fall through to retry below */ }
    if (!ticket) {
      startPolling();
      const delay = Math.min(15000, 1000 * Math.pow(2, state.wsRetry++));
      setTimeout(connectWs, delay);
      return;
    }
    const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
    let ws;
    try {
      ws = new WebSocket(proto + location.host + API + '/ws?ticket=' + encodeURIComponent(ticket));
    } catch (e) { startPolling(); return; }
    state.ws = ws;
    ws.onopen = () => {
      const wasDown = state.wsRetry > 0;
      state.wsOk = true; state.wsRetry = 0;
      stopPolling();
      // Heal any gap accrued while the socket was down: refetch everything
      // after the newest message we hold, for EVERY loaded channel (audit
      // finding — background channels silently missed outage messages).
      if (wasDown) resyncLoadedChannels();
    };
    ws.onmessage = (e) => {
      let ev;
      try { ev = JSON.parse(e.data); } catch (err) { return; }
      handleEvent(ev);
    };
    ws.onclose = (e) => {
      state.wsOk = false;
      if (e && (e.code === 4401 || e.code === 4403)) { location.reload(); return; }
      startPolling();
      const delay = Math.min(15000, 1000 * Math.pow(2, state.wsRetry++));
      setTimeout(connectWs, delay);
    };
    ws.onerror = () => { try { ws.close(); } catch (err) { /* already */ } };
  }

  async function catchUpChannel(key) {
    const st = state.msgs[key];
    if (!st || !st.loaded) return;
    let guard = 0;
    while (guard++ < 10) {
      const lastId = st.list.length ? st.list[st.list.length - 1].id : 0;
      let d;
      try {
        d = await api(messagesUrl(key) + '?after=' + lastId + '&limit=100');
      } catch (e) { return; }
      for (const m of d.messages || []) ingestMessage(m);
      if (!d.has_more) break;
    }
  }
  async function resyncLoadedChannels() {
    for (const key in state.msgs) await catchUpChannel(key);
    try { await loadChannels(); await loadDms(); renderRail(); } catch (e) { /* transient */ }
    // v2.1: pin/bookmark/event WS events aren't replayed by message catch-up —
    // refetch the side data for the channel in view so it heals after a drop.
    if (!isDmKey(state.active)) {
      try {
        await Promise.all([loadPins(state.active), loadBookmarks(state.active),
          state.active === 'events' ? loadEvents() : Promise.resolve()]);
        renderHead(); renderMessages({});
      } catch (e) { /* transient */ }
    }
    if (state.thread) {
      try {
        const t = await api('/messages/' + state.thread.rootId + '/thread');
        state.thread.root = t.root; state.thread.replies = t.replies;
        renderThreadPanel();
      } catch (e) { /* transient */ }
    }
  }

  function startPolling() {
    if (state.pollTimer) return;
    state.pollTimer = setInterval(async () => {
      try {
        // catchUpChannel loops on has_more, so a burst larger than one page
        // arrives whole instead of leaving a silent gap (audit finding).
        await catchUpChannel(state.active);
        if (state.thread) {
          const t = await api('/messages/' + state.thread.rootId + '/thread');
          state.thread.root = t.root; state.thread.replies = t.replies;
          renderThreadPanel();
        }
        await loadChannels();
        await loadDms();
        renderRail();
      } catch (e) { /* transient */ }
    }, 5000);
  }
  function stopPolling() {
    if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
  }

  function handleEvent(ev) {
    switch (ev.type) {
      case 'hello':
      case 'presence': {
        state.online = new Set(ev.online || []);
        for (const m of state.members) m.online = state.online.has(m.user_id);
        renderRail();
        break;
      }
      case 'message.created': ingestMessage(ev.message); break;
      case 'message.updated': {
        const msg = ev.message;
        const st = state.msgs[msg.channel];
        if (st) st.list = st.list.map((m) => (m.id === msg.id ? msg : m));
        if (state.thread) {
          if (state.thread.root.id === msg.id) state.thread.root = msg;
          state.thread.replies = state.thread.replies.map((r) => (r.id === msg.id ? msg : r));
        }
        renderMessages({}); renderThreadPanel();
        break;
      }
      case 'message.deleted': applyDelete(ev.id, ev.parent_message_id); break;
      case 'reaction': applyReactions(ev.message_id, ev.reactions); break;
      // ── v2.1 social events ──
      case 'event.created': {
        // Refetch both lists so the upcoming/past split + ordering stay correct.
        if (state.active === 'events') {
          loadEvents().then(() => { if (state.active === 'events') renderMessages({}); });
        } else {
          state.events.loadedFor = null;  // force a reload next time #events opens
        }
        break;
      }
      case 'event.updated': applyEvent(ev.event || ev); break;
      case 'event.rsvp': {
        const patch = (arr) => arr.map((e) => (e.id === ev.event_id
          ? Object.assign({}, e, { rsvp_count: ev.rsvp_count }) : e));
        state.events.upcoming = patch(state.events.upcoming);
        state.events.past = patch(state.events.past);
        if (state.active === 'events') renderMessages({});
        break;
      }
      case 'poll.updated': applyPoll(ev.message_id, ev.poll); break;
      case 'pins.updated': {
        state.pins[ev.channel] = ev.pins || [];
        // Reflect the pinned flag on the in-stream messages + header count.
        const pinnedSet = new Set((ev.pins || []).map((m) => m.id));
        const st = state.msgs[ev.channel];
        if (st && st.list) st.list = st.list.map((m) =>
          Object.assign({}, m, { pinned: pinnedSet.has(m.id) }));
        if (state.active === ev.channel) { renderHead(); renderMessages({}); }
        if (state.sidePanel === 'pins' && ev.channel === state.active) openPins(ev.channel);
        break;
      }
      case 'bookmark.added': {
        const list = state.bookmarks[ev.channel] || (state.bookmarks[ev.channel] = []);
        if (!list.some((b) => b.id === ev.bookmark.id)) list.push(ev.bookmark);
        if (state.active === ev.channel) renderHead();
        break;
      }
      case 'bookmark.removed': {
        if (state.bookmarks[ev.channel]) {
          state.bookmarks[ev.channel] = state.bookmarks[ev.channel].filter((b) => b.id !== ev.bookmark_id);
        }
        if (state.active === ev.channel) renderHead();
        break;
      }
      case 'typing': {
        if (state.me && ev.user_id === state.me.user_id) break;
        const key = ev.dm ? 'dm:' + ev.dm
          : (ev.thread_root ? 'th:' + ev.thread_root : 'ch:' + ev.channel);
        state.typing[key] = { name: ev.name, until: Date.now() + 4000 };
        renderTyping();
        break;
      }
    }
  }

  function renderTyping() {
    const el = document.getElementById('cmTyping');
    if (!el) return;
    const key = (isDmKey(state.active) ? 'dm:' : 'ch:') + state.active;
    const t = state.typing[key];
    if (t && t.until > Date.now()) {
      el.textContent = t.name + ' is typing…';
      setTimeout(renderTyping, 1200);
    } else {
      el.textContent = '';
    }
  }

  function ingestMessage(msg) {
    if (!msg) return;
    // Direct messages: their own container, no threads.
    if (msg.dm) {
      const st = state.msgs[msg.dm];
      if (st && st.loaded && !st.list.some((m) => m.id === msg.id)) {
        st.list.push(msg);
        st.list.sort((a, b) => a.id - b.id);
        if (msg.dm === state.active) renderMessages({});
      }
      let dm = state.dms.find((x) => x.id === msg.dm);
      if (!dm) {
        // first message of a conversation someone just opened with us
        loadDms().then(renderRail).catch(() => { /* transient */ });
      } else {
        dm.last_message_id = msg.id;
        dm.last_message_at = msg.created_at;
        const mine = state.me && msg.author && msg.author.user_id === state.me.user_id;
        const viewing = msg.dm === state.active && !document.hidden;
        if (!mine && !viewing) dm.unread = (dm.unread || 0) + 1;
        state.dms.sort((a, b) => (b.last_message_id || 0) - (a.last_message_id || 0));
        renderRail();
      }
      if (msg.dm === state.active) markReadIfAtBottom();
      return;
    }
    // thread replies live in the thread panel + bump the root's teaser
    if (msg.parent_message_id) {
      const st = state.msgs[msg.channel];
      if (st) {
        for (const m of st.list) {
          if (m.id === msg.parent_message_id) {
            if (!m._replyIds) m._replyIds = new Set();
            if (!m._replyIds.has(msg.id)) {
              m._replyIds.add(msg.id);
              m.reply_count = Math.max((m.reply_count || 0) + 1, m._replyIds.size);
              m.last_reply_at = msg.created_at;
            }
          }
        }
      }
      if (state.thread && state.thread.rootId === msg.parent_message_id &&
          !state.thread.replies.some((r) => r.id === msg.id)) {
        state.thread.replies.push(msg);
        renderThreadPanel();
      }
      renderMessages({});
      bumpUnread(msg);
      return;
    }
    const st = state.msgs[msg.channel];
    if (st && st.loaded && !st.list.some((m) => m.id === msg.id)) {
      st.list.push(msg);
      st.list.sort((a, b) => a.id - b.id);
      if (msg.channel === state.active) renderMessages({});
    }
    bumpUnread(msg);
    if (msg.channel === state.active) markReadIfAtBottom();
  }

  function bumpUnread(msg) {
    if (!state.me || !msg.author || msg.author.user_id === state.me.user_id) return;
    const atBottomActive = msg.channel === state.active && !document.hidden;
    if (atBottomActive) return; // will be marked read
    const ch = state.channels.find((c) => c.slug === msg.channel);
    if (ch) { ch.unread = (ch.unread || 0) + 1; renderRail(); }
  }

  // ─── Go ────────────────────────────────────────────────────────────────────
  document.addEventListener('DOMContentLoaded', boot);
})();
