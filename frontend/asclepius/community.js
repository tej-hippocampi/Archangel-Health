/* ═══════════════════════════════════════════════════════════════════════════
   Asclepius Community — Archangel-coded Slack (Community PRD)
   Vanilla SPA, no frameworks, no build step. Auth = the existing Asclepius
   JWT (same origin as the doctor portal; §1 handoff). Every privileged call
   is re-checked server-side — this file is presentation only.
   ═══════════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  const API = '/api/community';
  // Sandbox PRD §1.3 — see asclepius.js: the realm this page IS.
  const REALM = (window.__REALM === 'sandbox') ? 'sandbox' : 'live';
  const REALM_HEADER = 'X-Asclepius-Realm';
  function realmHeaders(h) { h = h || {}; h[REALM_HEADER] = REALM; return h; }
  function realmPath(p) { return REALM === 'sandbox' ? '/sandbox' + p : p; }
  const TOKEN_KEY = REALM === 'sandbox' ? 'asclepius_token_sandbox' : 'asclepius_token';
  const PHI_NOTICE = 'Colleague discussion only. Do not post patient-identifiable information.';

  // ─── State ─────────────────────────────────────────────────────────────────
  // PREVIEW MODE: the real interface, rendered from a server fixture, for an
  // applicant who cannot yet see the community. The gate in community/router.py
  // is untouched and still refuses them; nothing here ever reaches it. See
  // backend/asclepius/community_preview.py, which has no store access by
  // construction, so this cannot show a real colleague or a real message.
  const IS_PREVIEW = (function () {
    try { return new URLSearchParams(location.search).get('preview') === '1'; }
    catch (e) { return false; }
  }());

  const state = {
    token: null,
    me: null,           // my member profile
    isAdmin: false,
    canPost: true,      // false for a view-only account (advisor); set from /me
    preview: false,     // true when the fixture is on screen, not the community
    previewBanner: '',  // the server's sentence; not dismissible, never invented here
    channels: [],       // [{slug,name,description,post_policy,unread,mentions}]
    digest: null,       // {enabled, last_at, next_at} — the news schedule

    dms: [],            // conversations: DMs carry {peer}, case rooms carry {title, participants}
    active: 'general',  // channel slug OR a dm id ("dm-…") — keys never collide
    msgs: {},           // container key (slug or dm id) -> {list, hasMore, loaded}
    // The directory rows, which are SUMMARIES: id, name, initials, avatar,
    // specialty and the two badges, which is everything the rail and @mention
    // completion read. The rest of a profile (blurb, institution, years,
    // training) is fetched one colleague at a time into `profilesById`, so
    // opening the page no longer downloads a full dossier per member.
    members: [],
    membersById: {},
    profilesById: {},   // user_id -> full profile, once somebody has been opened
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
    // The newest #medical-ai-news post that carries a payload, fetched once at
    // boot. Two surfaces read it and neither owns it: the pinned home card
    // draws it collapsed, and the presence bar counts the unread ones.
    latestDigest: null,
    newDigests: 0,
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
    const headers = realmHeaders(opts.headers || {});
    if (state.token) headers['Authorization'] = 'Bearer ' + state.token;
    let body = opts.body;
    if (body !== undefined && !opts.isForm) {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(body);
    }
    let res;
    // Almost everything here is a community route. The one exception is the
    // staff persona post, which is an admin endpoint on the asclepius prefix,
    // and it should still go through this function for the auth header and the
    // error shaping rather than growing a second fetch idiom.
    const base = opts.base || API;
    try {
      res = await fetch(base + path, { method: opts.method || 'GET', headers, body });
    } catch (e) {
      throw { status: 0, detail: null, message: 'Network error: check your connection.' };
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

  /* A scheduled time in the reader's own timezone. The digest fires on a UTC
     hour, and telling a physician in Mumbai that the next digest is at "13:00
     UTC" is telling them to do arithmetic to find out whether to wait. */
  function fmtWhen(iso) {
    try {
      const d = new Date(iso);
      if (isNaN(d.getTime())) return '';
      const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
      const today = new Date();
      const tomorrow = new Date(Date.now() + 864e5);
      if (d.toDateString() === today.toDateString()) return 'today at ' + time;
      if (d.toDateString() === tomorrow.toDateString()) return 'tomorrow at ' + time;
      return d.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' })
        + ' at ' + time;
    } catch (e) { return ''; }
  }

  // ─── Client-side PHI pre-check (§7.1 — a UX nicety; the server decides) ────
  //
  // The third element is whether the pattern still counts INSIDE A URL. Most of
  // these are shape-only — a long digit run, a date-looking token, a
  // capitalized pair — and inside a link that shape is a path segment or a
  // share id, not an identifier. Pasting
  // https://www.linkedin.com/feed/update/urn:li:share:7501080186667581440/ was
  // warning a physician that they had posted an account number.
  //
  // An SSN and a keyword-anchored date of birth are the exceptions: those are
  // identifiers wherever they appear, and a link is not a laundering channel.
  // This mirrors community/phi_gate.py's _SHAPE_ONLY_CATEGORIES exactly; the
  // server is still the one that decides.
  const URL_RE = /(?:https?:\/\/|www\.)\S+/gi;
  const PHI_HINTS = [
    [/\b(MRN|MBI|medical record)\b/i, 'an MRN', false],
    [/\b\d{3}-\d{2}-\d{4}\b/, 'an SSN', true],
    [/\b(DOB|date of birth)\b/i, 'a date of birth', true],
    [/\b\d{1,2}\/\d{1,2}\/\d{2,4}\b/, 'an exact date', false],
    [/(?:^|\D)\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}(?!\d)/, 'a phone number', false],
    [/\b[\w.+-]+@[\w-]+\.[\w.-]+\b/, 'an email address', true],
    [/\b\d{7,}\b/, 'a long identifier-like number', false],
    [/\b(patient|pt)\.?\s+(name\s*)?(is\s*)?[A-Z][a-z]+\s+[A-Z][a-z]+\b/, 'a patient name', false],
  ];
  // Same length, so nothing about the rest of the text shifts under the mask.
  function maskUrls(text) {
    return String(text || '').replace(URL_RE, (u) => ' '.repeat(u.length));
  }
  function phiHint(text) {
    const masked = maskUrls(text);
    for (const [re, label, insideUrl] of PHI_HINTS) {
      if (re.test(insideUrl ? text : masked)) return label;
    }
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
        headers: realmHeaders({ 'Content-Type': 'application/json' }),
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
    // The signed-out check stays AHEAD of the preview branch. The preview
    // endpoint wants an account, and a session-less preview would be a public
    // page that looks like a room full of real physicians.
    if (!state.token) return renderSignedOut();
    if (IS_PREVIEW && await bootPreview()) return;
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
      // A 403 is the gate, and for an applicant the gate is a dead end: the
      // rail sends them here and the page tells them to come back later. Offer
      // the fixture first. The endpoint 404s anyone who CAN read the real
      // rooms, so asking is self-describing and cannot leak a preview to a
      // colleague who should be seeing the community itself.
      if (e.status === 403) {
        if (await bootPreview()) return;
        return renderGate();
      }
      return renderError(e.message);
    }
    await Promise.all([loadChannels(), loadMembers(), loadDms()]);
    // NOT awaited. It needs the channel list (it counts against that room's
    // unread), so it cannot join the group above -- and awaiting it here would
    // put a whole round trip in front of first paint for every reader,
    // including the ones who will never scroll to a pinned card. It repaints
    // the two surfaces that read it when it lands.
    loadLatestDigest().then(() => {
      renderGreeting();
      if (state.active === 'general') renderMessages({});
    });
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

  // The fixture writes `at` for a reader ("09:12", "Yesterday", "Monday",
  // "Last week") because it is describing a shape, not a moment. The renderer
  // wants an ISO string it can group into day separators. Convert HERE, at the
  // seam, so messageEl and fmtDay never learn that a preview exists: the whole
  // point of this feature is that it is the real interface.
  function previewIso(at, index) {
    const now = new Date();
    const clock = /^(\d{1,2}):(\d{2})$/.exec(String(at || ''));
    if (clock) {
      const d = new Date(now);
      d.setHours(Number(clock[1]), Number(clock[2]), 0, 0);
      return d.toISOString();
    }
    const DAYS_BACK = { yesterday: 1, 'last week': 7 };
    const key = String(at || '').trim().toLowerCase();
    let back = DAYS_BACK[key];
    if (back === undefined) {
      const names = ['sunday', 'monday', 'tuesday', 'wednesday',
        'thursday', 'friday', 'saturday'];
      const wanted = names.indexOf(key);
      // The most recent occurrence of that weekday, never today and never the
      // future: a fixture dated tomorrow reads as a broken clock.
      back = wanted === -1 ? (2 + index) : ((now.getDay() - wanted + 7) % 7) || 7;
    }
    const d = new Date(now.getTime() - back * 864e5);
    d.setHours(10, 0, 0, 0);
    return d.toISOString();
  }

  /** Paint the fixture. Returns false if it is not available, so the caller
   *  can fall through to whatever it would have shown otherwise. */
  async function bootPreview() {
    let payload;
    try {
      payload = await api('/community/preview', { base: '/api/asclepius' });
    } catch (e) {
      return false;
    }
    if (!payload || !payload.preview) return false;

    state.preview = true;
    state.previewBanner = payload.banner || '';
    state.canPost = false;      // every write affordance already keys off this
    state.isAdmin = false;
    state.me = null;
    state.dms = [];
    state.channels = (payload.channels || []).map((c) => Object.assign({}, c));
    state.members = (payload.members || []).map((m) => Object.assign({}, m));
    state.membersById = {};
    state.online = new Set();
    for (const m of state.members) state.membersById[m.user_id] = m;

    state.msgs = {};
    const byChannel = payload.messages || {};
    for (const ch of state.channels) {
      const rows = byChannel[ch.slug] || [];
      state.msgs[ch.slug] = {
        loaded: true,
        hasMore: false,
        list: rows.map((m, i) => ({
          id: m.id,
          body: m.body,
          created_at: previewIso(m.at, i),
          author: state.membersById[m.author] || { display_name: 'Archangel' },
          mentions: [],
          reactions: [],
          reply_count: 0,
          pinned: false,
          deleted: false,
        })),
      };
    }
    if (!state.msgs[state.active]) {
      state.active = (state.channels[0] || {}).slug || state.active;
    }

    // No openChannel (it fetches and marks read), no WebSocket, no loaders.
    // There is no server state behind any of it.
    renderApp();
    renderMessages();
    return true;
  }

  function renderSignedOut() {
    setRoot(h('div', { class: 'cm-gate' },
      h('div', { class: 'cm-gate-card' },
        h('div', { class: 'chrome cm-gate-kicker' }, 'Archangel Health · Community'),
        h('h1', { class: 'cm-gate-title' }, 'Sign in through the doctor portal'),
        h('p', { class: 'cm-gate-sub' },
          'The community opens from inside the Archangel Health portal. Sign in there, then choose Community from the side panel.'),
        h('div', { class: 'cm-gate-actions' },
          h('a', { class: 'cm-btn cm-btn-primary', href: realmPath('/asclepius') }, 'Open the doctor portal')))));
  }
  function renderGate() {
    setRoot(h('div', { class: 'cm-gate' },
      h('div', { class: 'cm-gate-card' },
        h('div', { class: 'chrome cm-gate-kicker' }, 'Archangel Health · Community'),
        h('h1', { class: 'cm-gate-title' }, 'Community access is for verified contributors'),
        h('p', { class: 'cm-gate-sub' },
          'This space is reserved for credential-verified contributor physicians. Once your credentials are verified you’ll be able to join the conversation.'),
        h('div', { class: 'cm-gate-actions' },
          h('a', { class: 'cm-btn cm-btn-ghost', href: realmPath('/asclepius') }, 'Back to the portal')))));
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
    // When the digest last ran and when it runs next, so an empty
    // #medical-ai-news can say which of those it is waiting on.
    state.digest = d.digest || null;
  }
  async function loadDms() {
    const d = await api('/dms');
    state.dms = d.dms || [];
  }
  function isDmKey(key) { return typeof key === 'string' && key.indexOf('dm-') === 0; }
  function activeDm() { return state.dms.find((d) => d.id === state.active) || null; }
  // A per-case group room (Task Pipeline PRD B1) rather than a two-party DM.
  // Read off `kind`, which the server sets, and never off the absence of a
  // peer: a two-party DM whose other side left also has no peer, and it is
  // still a DM.
  function isCaseRoom(d) { return !!d && d.kind === 'case_room'; }
  // A group somebody opened. Same object as a case room — a title, a roster, no
  // peer — and rendered the same way, but it is NOT a case room and the
  // distinction is load-bearing: the server opens a case room to Archangel
  // admins so a founder can step into a stuck case, and a group is private to
  // its participants exactly like a two-party DM.
  function isGroup(d) { return !!d && d.kind === 'group'; }
  function isRoster(d) { return isCaseRoom(d) || isGroup(d); }
  function roomTitle(d) {
    return (d && d.title) || (isGroup(d) ? 'Group' : 'Case room');
  }
  function roomRoster(d) {
    return ((d && d.participants) || []).map((m) => m.display_name).filter(Boolean);
  }
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
  /* The latest digest, for the landing card and the presence line (§1.3, §3.1).
   *
   * No new endpoint: this is the channel's own message list. The page size is
   * 25 rather than a handful because #medical-ai-news is NOT a digest-only
   * room -- the morning routine posts briefs into it too (community/morning.py)
   * -- so a window of five could hold nothing but briefs and quietly leave the
   * landing page with no card, which by design explains nothing.
   *
   * It never throws. A reader who cannot see the room, or a room that has never
   * posted, simply has no card, and a landing page that 500s over a decoration
   * is a worse outcome than a landing page without one. */
  async function loadLatestDigest() {
    state.latestDigest = null;
    state.newDigests = 0;
    try {
      const d = await api('/channels/medical-ai-news/messages?limit=25');
      const msgs = (d.messages || []).filter((m) => !m.deleted);
      const digests = msgs.filter((m) => digestOf(m));
      // The list arrives oldest-first, like every other channel fetch, so the
      // newest digest is the last row that has a payload.
      state.latestDigest = digests.length ? digests[digests.length - 1] : null;

      /* How many of them are NEW, counted rather than assumed.
       *
       * The channel's unread number counts every unread message in the room, of
       * any kind, so labelling it "3 new digests" reads three morning briefs as
       * three digests. Unread messages are the LAST `unread` rows in the room,
       * so the digests among that tail are the digests this reader has not seen
       * -- which is the number §3.1 actually asks for, from data the client
       * already holds. */
      const ch = state.channels.find((c) => c.slug === 'medical-ai-news');
      const unread = Math.max(0, Math.min((ch && ch.unread) || 0, msgs.length));
      state.newDigests = unread
        ? msgs.slice(msgs.length - unread).filter((m) => digestOf(m)).length
        : 0;
    } catch (e) {
      state.latestDigest = null;
      state.newDigests = 0;
    }
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

  /* ── Greeting and presence (Digest Design PRD §3.1) ──────────────────────
   *
   * One line, and everything about it is already on the client: the clock is
   * the browser's, the name is the profile's, the head count is the presence
   * set the socket already maintains, and the digest count is the unread the
   * channel list already carries. No endpoint, no avatar row, no second unread
   * badge — the rail has one and two of them disagree the moment one is stale.
   *
   * "Good morning, Dr. Patel." is nine words at its longest and the budget is
   * nine, which is the whole point: the room should feel like colleagues are in
   * it, and colleagues do not introduce themselves twice. */
  function greetingWord(now) {
    const hour = (now || new Date()).getHours();
    if (hour < 12) return 'Good morning';
    if (hour < 18) return 'Good afternoon';
    return 'Good evening';
  }

  /* The surname, the way a colleague would say it. `display_name` is whatever
     the physician set, so "Dr." is added only when it is not already there and
     the fallback is the whole name rather than a guess at which word is the
     family one. */
  function greetingName(me) {
    const raw = String((me && me.display_name) || '').trim();
    if (!raw) return null;
    if (/^(dr\.?|prof\.?)\s/i.test(raw)) return raw;
    const parts = raw.split(/\s+/);
    return 'Dr. ' + parts[parts.length - 1];
  }

  function renderGreeting() {
    const bar = document.getElementById('cmGreet');
    if (!bar) return;
    clear(bar);
    // The preview is a fixture, and greeting an applicant by a name we made up
    // for them is the one thing that would give the fixture away.
    if (state.preview) return;
    const name = greetingName(state.me);
    bar.appendChild(h('div', { class: 'cm-greet' },
      greetingWord() + (name ? ', ' + name : '') + '.'));

    const online = state.members.filter((m) => state.online.has(m.user_id)).length;
    // Hollow dot and a bare number when nobody is here. No sentence: "nobody is
    // online right now" is a sentence about absence, and the room does not need
    // one written for it.
    const presence = h('div', { class: 'cm-presence-bar' },
      h('span', { class: 'cm-presence-dot' + (online ? ' on' : ''), 'aria-hidden': 'true' }),
      h('span', { class: 'chrome' }, online + ' online'));

    // Counted in loadLatestDigest from the room's own rows, never taken from
    // the channel's unread number: that counts every unread message in the
    // room, and the morning brief posts there too.
    const fresh = Math.max(0, state.newDigests || 0);
    if (fresh) {
      presence.appendChild(h('span', { class: 'chrome cm-presence-sep' }, '·'));
      presence.appendChild(h('button', {
        class: 'cm-presence-digest chrome', type: 'button',
        onClick: () => openChannel('medical-ai-news'),
      }, fresh + (fresh === 1 ? ' new digest' : ' new digests')));
    }
    bar.appendChild(presence);
  }

  // ─── App layout ────────────────────────────────────────────────────────────
  function renderApp() {
    const app = h('div', { class: 'cm-app' },
      h('nav', { class: 'cm-rail', id: 'cmRail', 'aria-label': 'Channels and members' }),
      h('section', { class: 'cm-main' },
        h('header', { class: 'cm-head', id: 'cmHead' }),
        // Above the header's siblings and outside the scroller, so it is the
        // first line in every view and cannot be scrolled away.
        h('div', { class: 'cm-greet-bar', id: 'cmGreet' }),
        // Above the stream and outside it, so it cannot be scrolled away. No
        // close control: one sentence is what keeps a fixture from reading as
        // a room full of real colleagues, and a banner you can dismiss is a
        // banner that is gone by the second channel.
        state.preview
          ? h('div', { class: 'cm-preview-banner', role: 'note' },
              h('span', { class: 'dot dot-orange', 'aria-hidden': 'true' }),
              h('span', {}, state.previewBanner))
          : null,
        h('div', { class: 'cm-scroll', id: 'cmScroll', role: 'log', 'aria-label': 'Messages' }),
        h('div', { class: 'cm-typing', id: 'cmTyping', 'aria-live': 'polite' }),
        h('div', { class: 'cm-composer-wrap', id: 'cmComposerWrap' })),
      h('aside', { class: 'cm-side', id: 'cmSide', 'aria-label': 'Details panel' }));
    setRoot(app);
    renderRail();
    renderGreeting();
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
      h('div', { class: 'chrome' }, 'Archangel Health'),
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
        ch.post_policy === 'admin' ? h('span', { class: 'cm-chan-lock', title: 'Announcements: Archangel team posts, replies open in threads' }, 'ro') : null,
        unread > 0 && !isActive
          ? h('span', { class: 'cm-chan-unread' }, unread > 99 ? '99+' : String(unread))
          : (unread > 0 ? h('span', { class: 'dot dot-lime', 'aria-label': 'unread' }) : null));
    };
    /* One labelled section per channel GROUP the server can send.
     *
     * This list used to name two of them, and everything else fell through the
     * "not specialty, not country" filter into the unlabelled Channels
     * section. The server has since opened subspecialty, city and crossed
     * (specialty-in-region) rooms, so #transplant-nephrology, #boston and
     * #nephrology-emea all piled in under the core rooms with nothing saying
     * what they were or why they had appeared. Reading the group off the
     * channel, rather than filtering for the ones we happen to know about,
     * means the next cohort the server opens lands in its own section instead
     * of on that pile.
     *
     * Own room first inside each group, which is the rule the specialty and
     * country sections already followed: a nephrologist in Boston should find
     * their own rooms at the top rather than scrolling an alphabet to reach
     * them. `me` carries the same cohort keys the channels do, so each group
     * sorts on its own key rather than a shared guess. */
    const me = state.me || {};
    const GROUPS = [
      { key: 'specialty', label: 'Specialty',
        mine: (c) => (c.specialty || '').toLowerCase() === (me.specialty || '').toLowerCase() },
      { key: 'subspecialty', label: 'Subspecialty',
        mine: (c) => (me.subspecialties || []).indexOf(c.subspecialty) !== -1 },
      { key: 'country', label: 'Countries',
        mine: (c) => (c.country || '').toUpperCase() === (me.country || '').toUpperCase() },
      { key: 'specialty_region', label: 'Specialty by region',
        mine: (c) => (c.specialty || '').toLowerCase() === (me.specialty || '').toLowerCase()
                  && (c.region || '') === (me.region || '') },
      { key: 'city', label: 'Cities',
        mine: (c) => (c.city || '') === (me.city || '') },
    ];
    const grouped = {};
    for (const g of GROUPS) grouped[g.key] = [];
    const coreChans = [];
    for (const c of state.channels) {
      const grp = c.group || 'core';
      if (grouped[grp]) grouped[grp].push(c); else coreChans.push(c);
    }

    const chSection = h('div', { class: 'cm-rail-section' },
      h('div', { class: 'cm-rail-label' }, h('span', { class: 'chrome' }, 'Channels')));
    for (const ch of coreChans) chSection.appendChild(chanBtn(ch));
    scrollBox.appendChild(chSection);

    for (const g of GROUPS) {
      const chans = grouped[g.key];
      if (!chans.length) continue;
      // A cohort key we do not hold matches nothing, so `mine` is false for
      // every room and the order is left as the server sent it.
      chans.sort((a, b) => (g.mine(b) ? 1 : 0) - (g.mine(a) ? 1 : 0));
      const section = h('div', { class: 'cm-rail-section' },
        h('div', { class: 'cm-rail-label' }, h('span', { class: 'chrome' }, g.label)));
      for (const ch of chans) section.appendChild(chanBtn(ch));
      scrollBox.appendChild(section);
    }

    /* Conversations. Two kinds ride this one list, and they are not the same
     * thing, so they do not render the same way.
     *
     * A DM is a person: a presence dot and their name. A case room has three
     * people and no one of them is the room's name, so it renders its TITLE
     * and how many colleagues are in it. The server used to hand rooms a
     * synthetic peer built from the bot account so this loop would not print
     * "Former member" at a room; that shim is gone with this branch, and a
     * room that renders as a two-party DM is worse than one that renders
     * plainly as a room.
     *
     * Rooms sit above DMs under their own label: a room appears because a case
     * was routed to you, which is work, and a DM appears because a colleague
     * chose to talk to you, which is not. */
    const rooms = state.dms.filter(isCaseRoom);
    const groups = state.dms.filter(isGroup);
    const dms = state.dms.filter((d) => !isRoster(d));

    // Case rooms and member-made groups render identically — a title and a head
    // count, because neither has a peer to be named by — so they share one row
    // builder and differ only in which section they sit under.
    function rosterRow(d) {
      const isActive = d.id === state.active;
      const people = (d.participants || []).length;
      return h('button', {
        class: 'cm-chan cm-room-row' + (isActive ? ' active' : ''),
        'aria-current': isActive ? 'page' : null,
        onClick: () => openDm(d.id),
      },
        h('span', { class: 'cm-room-mark', 'aria-hidden': 'true' }),
        h('span', { class: 'cm-chan-name cm-room-name' }, roomTitle(d)),
        people ? h('span', { class: 'cm-room-count' }, String(people)) : null,
        d.unread > 0 && !isActive
          ? h('span', { class: 'cm-chan-unread' }, d.unread > 99 ? '99+' : String(d.unread))
          : null);
    }

    if (rooms.length) {
      const roomSection = h('div', { class: 'cm-rail-section' },
        h('div', { class: 'cm-rail-label' }, h('span', { class: 'chrome' }, 'Case rooms')));
      for (const d of rooms) roomSection.appendChild(rosterRow(d));
      scrollBox.appendChild(roomSection);
    }

    if (groups.length) {
      const groupSection = h('div', { class: 'cm-rail-section' },
        h('div', { class: 'cm-rail-label' }, h('span', { class: 'chrome' }, 'Groups')));
      for (const d of groups) groupSection.appendChild(rosterRow(d));
      scrollBox.appendChild(groupSection);
    }

    const dmSection = h('div', { class: 'cm-rail-section' },
      h('div', { class: 'cm-rail-label' },
        h('span', { class: 'chrome' }, 'Direct messages'),
        state.canPost
          ? h('button', {
              class: 'cm-rail-add', type: 'button', title: 'Start a group conversation',
              'aria-label': 'New group', onClick: openNewGroup,
            }, 'New group')
          : null));
    if (!dms.length) {
      dmSection.appendChild(h('div', { class: 'cm-rail-hint' },
        'Open a colleague’s profile to start one.'));
    }
    for (const d of dms) {
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
    // The preview has no conversations and no way to start one, so the section
    // would be a label over a hint pointing at profiles that are fixtures. The
    // member directory below it stays: seeing who is in the rooms is most of
    // what an applicant came to look at.
    if (!state.preview) scrollBox.appendChild(dmSection);

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
    // The bar's two numbers are the rail's two numbers -- the same presence set
    // and the same unread count -- so they repaint together and cannot drift
    // into saying different things about the same room.
    renderGreeting();

    // The avatar is already a button (it opens the profile), so the way into
    // notification settings is its own control beside the name rather than a
    // wrapper around it: a button inside a button is invalid markup and the
    // browser breaks the inner one out of the outer, taking the row apart.
    // In the preview there is no `me` to render and no preferences to set, so
    // the foot would be an empty avatar over a settings button that 403s.
    // What belongs there instead is the way back.
    if (state.preview) {
      rail.appendChild(h('div', { class: 'cm-rail-foot' },
        h('a', { class: 'cm-btn cm-btn-ghost', href: realmPath('/asclepius') },
          'Back to the portal')));
      return;
    }
    rail.appendChild(h('div', { class: 'cm-rail-foot' },
      h('div', { class: 'cm-rail-me' },
        avatarEl(me, 'small'),
        h('span', { class: 'cm-rail-me-name' }, me.display_name || ''),
        h('button', {
          class: 'cm-rail-me-prefs chrome',
          title: 'Choose which emails you get',
          onClick: () => openNotificationPrefs(),
        }, 'Notifications')),
      h('div', { class: 'cm-rail-retention' },
        state.retention || 'Messages are retained indefinitely unless an admin removes them.')));
  }

  /* ── Notification settings ──────────────────────────────────────────────
   *
   * There was no preferences UI at all. The only way to stop community email
   * was the unsubscribe link in the footer of a message, and that link stops
   * EVERYTHING: a member who wanted fewer pins had one button, and pressing it
   * also silenced the mention that tells them a colleague asked them
   * something. The four switches below are the four the server keeps, and the
   * panel writes them one at a time so a stale tab cannot revert a change made
   * somewhere else.
   *
   * Reached from the member's own name in the rail foot, which is where a
   * person looks for their own settings and is the only thing down there that
   * is about them rather than about the room. */
  const NEWS_FREQUENCY_LABELS = {
    daily: 'Every day',
    weekly: 'Once a week',
    off: 'Never',
  };

  const NOTIFICATION_SWITCHES = [
    ['activity_emails', 'Mentions, direct messages and announcements',
     'Someone writes to you or names you.'],
    ['post_emails', 'New posts in your channels',
     'The morning brief, the news digest, and anything else the Archangel account posts.'],
    ['pin_emails', 'Pinned messages',
     'A colleague marks something in a channel as worth reading.'],
  ];

  function openNotificationPrefs() {
    openModal('Notifications', (close) => {
      const body = h('div', { class: 'cm-modal-body' },
        h('div', { class: 'cm-prefs-loading chrome' }, 'Reading your settings…'));

      function paintPrefs(prefs) {
        clear(body);
        // One PATCH-shaped write per switch. Sending the whole object back
        // would let a tab that has been open all afternoon overwrite a
        // change made in another one.
        async function save(patch, revert) {
          try {
            const next = await api('/prefs', { method: 'POST', body: patch });
            paintPrefs(next);
          } catch (e) {
            revert();
            toast(e.message, 'error');
          }
        }

        const freq = h('select', { class: 'cm-persona-channel', onChange: (e) => {
          save({ news_frequency: e.target.value }, () => { e.target.value = prefs.news_frequency; });
        } },
          (prefs.options || ['daily', 'weekly', 'off']).map((value) => h('option', {
            value: value,
            selected: prefs.news_frequency === value ? 'selected' : null,
          }, NEWS_FREQUENCY_LABELS[value] || value)));

        body.appendChild(h('p', { class: 'cm-persona-note' },
          'Everything here is email only. Your unread badges and the messages '
          + 'themselves are unaffected.'));
        body.appendChild(field('The medical AI digest', freq));
        NOTIFICATION_SWITCHES.forEach(([key, label, note]) => {
          const box = h('input', {
            type: 'checkbox',
            checked: prefs[key] === false ? null : 'checked',
            onChange: (e) => {
              const wanted = e.target.checked;
              const patch = {};
              patch[key] = wanted;
              save(patch, () => { e.target.checked = !wanted; });
            },
          });
          body.appendChild(h('label', { class: 'cm-prefs-switch' }, box,
            h('span', {},
              h('span', { class: 'cm-prefs-switch-label' }, label),
              h('span', { class: 'cm-prefs-switch-note' }, note))));
        });
        body.appendChild(h('div', { class: 'cm-modal-actions' },
          h('button', { class: 'cm-btn cm-btn-ghost', onClick: close }, 'Done')));
      }

      api('/prefs').then(paintPrefs).catch((e) => {
        clear(body);
        body.appendChild(h('p', { class: 'cm-persona-note' },
          'Could not read your settings: ' + e.message));
      });
      return body;
    });
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
      if (isRoster(d)) {
        // The roster is the header. Who else is in here is the first thing you
        // need before you say anything, and it is not derivable from a name the
        // way a DM's other side is.
        const roster = roomRoster(d);
        head.appendChild(h('span', { class: 'cm-head-name' }, roomTitle(d)));
        head.appendChild(h('span', { class: 'cm-head-desc' },
          isGroup(d)
            ? (roster.length
                ? 'In this group: ' + roster.join(', ') + '. Colleague discussion only: no PHI.'
                : 'A group conversation. Colleague discussion only: no PHI.')
            : (roster.length
                ? 'On this case: ' + roster.join(', ') + '. Colleague discussion only: no PHI.'
                : 'A room for the colleagues on this case. Colleague discussion only: no PHI.')));
        if (isGroup(d) && state.canPost) {
          head.appendChild(h('button', {
            class: 'cm-head-btn', type: 'button', title: 'Add colleagues to this group',
            onClick: () => openAddGroupMembers(d),
          }, 'Add people'));
        }
      } else {
        const peer = (d && d.peer) || {};
        head.appendChild(h('span', { class: 'cm-head-name' }, peer.display_name || 'Conversation'));
        head.appendChild(h('span', { class: 'cm-head-desc' },
          'Direct messages are between the two of you.'
          + (peer.blurb ? ' ' + peer.blurb : '')));
      }
    } else {
      const ch = activeChannel();
      if (!ch) return;
      head.appendChild(h('span', { class: 'cm-head-name' }, '#' + ch.slug));
      head.appendChild(h('span', { class: 'cm-head-desc' }, ch.description || ''));
    }
    // Search and the pin panel both query the server, which refuses this
    // account, so in the preview they are controls that can only disappoint.
    // A fixture has nothing to search anyway.
    if (state.preview) return;

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
      if (state.isAdmin) {
        head.appendChild(h('button', {
          class: 'cm-head-btn cm-persona-btn', 'aria-label': 'Post as Archangel',
          title: 'Post as the Archangel account',
          onClick: () => openPersonaComposer(slug),
        }, 'Post as Archangel'));
      }
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
    // Every room is already in memory and there is no read state to mark, so
    // switching channels in the preview is a repaint and nothing else.
    if (state.preview) { renderMessages({ stickBottom: true }); return; }
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

  /* One sentence and one button, per room (Digest Design PRD §3.3).
   *
   * The old copy explained the room in three clauses and then described the
   * verification model, which is a paragraph a physician reads once and never
   * again. What an empty room needs is a reason to type — so the sentence is
   * the reason and the button is the way in, and the rooms nobody may post in
   * get no button rather than a disabled one.
   *
   * Verbatim from the PRD. These are the founders' words, not a paraphrase of
   * them, and the whole difference between "homey" and "friendly product copy"
   * is whether somebody actually says it this way. */
  const EMPTY_COPY = {
    'general': ['The kitchen table. Say hello.', 'Say hello'],
    'introductions': ['Who are you, and what do you see most in clinic?', 'Introduce yourself'],
    'questions-help': ['Ask. One of us answers today.', 'Ask'],
    'future-of-medical-ai': ['Where is this going? Contrarian welcome.', 'Start a thread'],
    'medical-ai-news': ['First digest at 6am PT.', null],
    'task-announcements': ['We post here when there\u2019s work.', null],
    'events': ['We post here when there\u2019s work.', null],
  };

  /* ── The branded home panel (Admin Launch PRD §5.2) ──────────────────────
   *
   * The two-line grey empty state said nothing about what this room is or who
   * is in it — which is exactly what a physician arriving from an invite email
   * needs to read first. This is the same treatment on EVERY channel: the mark,
   * the name, what the room is for, and the channels worth opening next.
   *
   * The channel's own description comes from the SERVER (community/store.py
   * DEFAULT_CHANNELS), so a channel added there arrives here already described.
   * EMPTY_COPY stays as the hand-written override for the three that have one.
   *
   * What this panel does NOT own is the PHI rule. That lives on the composer
   * (`cm-phi-notice`, §7.5) and renders on every channel whether or not it has
   * messages — standing, never dismissible. Nothing here restyles it.
   */
  /* The rooms nobody may post in. Their empty state gets no button rather than
     a disabled one: an invitation you cannot accept is worse than no
     invitation, and the server would refuse the post anyway. */
  const READ_ONLY_ROOMS = ['medical-ai-news', 'task-announcements', 'events'];

  /* Why #medical-ai-news is empty, said out loud.
   *
   * This room is filled by a routine, not by people, so an empty one means the
   * routine has not run YET, has run and found nothing, or is switched off —
   * three different situations that the branded hero rendered identically. It
   * also rendered identically to a fourth that was the real one for months:
   * the digest posted, and the deploy that followed deleted the database it
   * posted into. A reader had no way to tell "quiet" from "broken", so they
   * assumed quiet, and nobody went looking.
   *
   * Returns null for every other channel and whenever the schedule could not
   * be read, so the hero keeps its normal copy rather than guessing. */
  function digestEmptyCopy(slug) {
    if (slug !== 'medical-ai-news') return null;
    const d = state.digest;
    // `enabled === null` is the server saying it could not read the schedule
    // (community/router.py _digest_schedule returns a dict either way, never
    // null). Without this the room explained itself from nothing: "No digest
    // yet today, digests only post when there is something worth a physician's
    // time" is a confident sentence assembled out of three unknowns.
    if (!d || d.enabled == null) return null;
    if (d.enabled === false) {
      return ['The digest is paused',
        'Automatic posts to this room are switched off, so it stays quiet '
        + 'until someone from the Archangel team posts here.'];
    }
    const next = d.next_at ? fmtWhen(d.next_at) : '';
    const ranToday = d.last_at
      && String(d.last_at).slice(0, 10) === new Date().toISOString().slice(0, 10);
    // "Ran today and this room is still empty" is a real, ordinary outcome —
    // a day where nothing cleared the relevance bar posts nothing by design —
    // and saying so is the difference between a quiet room and a broken one.
    const lead = ranToday
      ? 'Today\'s run finished and found nothing worth posting'
      : 'No digest yet today';
    // One sentence, like every other empty state now (§3.3). The PRD's copy
    // for this room is "First digest at 6am PT", and it is deliberately NOT
    // used whenever the schedule is readable: a fixed sentence says the same
    // thing on the morning the pipeline is switched off, and telling a quiet
    // room from a broken one is the reason this function exists.
    return [lead, next ? 'The next run is ' + next + '.'
                       : 'Quiet days stay quiet here.'];
  }

  function homePanel(slug) {
    const ch = state.channels.find((c) => c.slug === slug) || {};
    const digestCopy = digestEmptyCopy(slug);
    const copy = EMPTY_COPY[slug] || [];

    // The digest room explains ITSELF when the schedule is readable, because
    // "quiet" and "broken" look identical otherwise. Everywhere else the room's
    // one sentence is the title and there is nothing under it.
    // A room with hand-written copy leads with its one sentence. A room without
    // leads with its NAME and puts the server's description underneath: a
    // channel description is a paragraph, and a paragraph set as a heading
    // reads as a layout mistake rather than a welcome.
    const line = digestCopy ? digestCopy[1] : (copy[0] ? null : (ch.description || null));
    const title = digestCopy ? digestCopy[0] : (copy[0] || ('#' + (ch.name || slug)));
    const label = digestCopy ? null : copy[1];

    const panel = h('div', { class: 'cm-home' },
      h('img', {
        class: 'cm-home-mark', src: '/static/asclepius/ah-mark.png',
        width: '96', height: '96',
        // frontend/ is served at /static (main.py); backend/assets is
        // /email-assets and is for email only. There is no assets/ dir here.
        alt: 'Archangel Health',
      }),
      h('div', { class: 'cm-home-title cm-empty-title' }, title));
    if (line) panel.appendChild(h('p', { class: 'cm-home-body' }, line));
    // No button in a room this reader cannot write in, and none in the preview,
    // where the composer it would focus does not exist.
    if (label && state.canPost && READ_ONLY_ROOMS.indexOf(slug) === -1) {
      panel.appendChild(h('button', {
        class: 'cm-home-cta', type: 'button',
        onClick: () => {
          // Found through the channel composer's OWN wrapper rather than by an
          // id on the textarea: `buildComposer` also builds the thread panel's
          // box, so an id there means two elements share it whenever a thread
          // is open. This also leaves the composer itself untouched, which §7
          // asks for.
          const wrap = document.getElementById('cmComposerWrap');
          const ta = wrap && wrap.querySelector('textarea');
          if (ta) ta.focus();
        },
      }, label));
    }
    return panel;
  }

  /* ── The pinned home card (§1.3) ─────────────────────────────────────────
   *
   * The landing room shows the morning's digest collapsed: the title, the top
   * story, and `Read all {n} →` into the channel post. Same component as the
   * card in #medical-ai-news, in its collapsed mode — one component, two
   * contexts, because two components is two places for the design to drift.
   *
   * Returns null everywhere except the landing room, and whenever no digest has
   * been fetched. A landing page is not the place to explain an absence. */
  function pinnedDigestEl() {
    if (state.active !== 'general' || state.preview) return null;
    const m = state.latestDigest;
    const dg = m && digestOf(m);
    if (!dg) return null;
    return h('div', { class: 'cm-digest-pin' }, digestCardEl(m, dg, { collapsed: true }));
  }

  /* `Read all →`: the channel post itself, not a copy of it. The card the
     reader taps and the card they land on are the same post, so the pinned one
     never has to be kept in sync with anything. */
  function openDigestPost(m) {
    openChannel('medical-ai-news');
    if (!m || !m.id) return;
    // The row appears when the channel's fetch lands, and a single timeout is a
    // bet on how long that takes: slower than the guess and the scroll silently
    // never happens. Look for it a few times over about two seconds instead,
    // then stop. A row that never arrives is a no-op rather than an error --
    // the reader is in the right room either way.
    let tries = 0;
    const seek = () => {
      const row = document.querySelector('[data-mid="' + m.id + '"]');
      if (row && row.scrollIntoView) { row.scrollIntoView({ block: 'center' }); return; }
      if (++tries < 10) setTimeout(seek, 200);
    };
    setTimeout(seek, 120);
  }

  function renderMessages(opts) {
    opts = opts || {};
    const scroll = document.getElementById('cmScroll');
    if (!scroll) return;
    const st = state.msgs[state.active] || { list: [] };
    const atBottom = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 60;
    clear(scroll);
    const inDm = isDmKey(state.active);
    // The landing room leads with the morning's digest, collapsed, whether or
    // not anybody has posted in it yet -- so it sits above BOTH the stream and
    // the empty panel rather than inside either.
    const pinned = pinnedDigestEl();
    if (pinned) scroll.appendChild(pinned);
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
        const d = activeDm();
        if (isCaseRoom(d)) {
          const roster = roomRoster(d);
          copy = [roomTitle(d),
            'A room for the colleagues on this case'
            + (roster.length ? ': ' + roster.join(', ') : '')
            + '. Colleague discussion only: no PHI.'];
        } else {
          const peer = (d || {}).peer || {};
          copy = ['A private conversation',
            'This is the beginning of your direct messages with '
            + (peer.display_name || 'this colleague') + '. Colleague discussion only: no PHI.'];
        }
      } else {
        // A real channel: the branded panel, not two lines of grey. The pinned
        // digest is already above it.
        scroll.appendChild(homePanel(state.active));
        return;
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
  const avatarBlobCache = {};
  /* A colleague's face, if they set one, and their initials until it arrives.
     The avatar endpoint is bearer-authenticated and an <img src> cannot carry
     an Authorization header, so the bytes are fetched with the session token
     and handed over as a blob: URL -- the same thing loadAttachmentBlob does
     for images posted in a channel.

     The initials are rendered FIRST and stay if the fetch fails. A broken
     image glyph where a physician's face should be is worse than the two
     letters everyone had before. */
  function avatarEl(member, size) {
    const acc = (member && member.specialty_accent) || 'green';
    const url = member && member.avatar_url;
    const el = h('button', {
      class: 'cm-avatar acc-' + acc + (url ? ' has-img' : '')
        + (size === 'big' ? ' cm-profile-avatar' : ''),
      style: size === 'small' ? 'width:24px;height:24px;font-size:0.55rem' : null,
      'aria-label': 'Profile: ' + ((member && member.display_name) || 'member'),
      onClick: member && member.user_id ? () => openMember(member.user_id) : null,
    }, (member && member.initials) || '-');
    if (url) loadAvatarBlob(url).then((objectUrl) => {
      if (!objectUrl) return;
      clear(el);
      el.appendChild(h('img', { class: 'cm-avatar-img', src: objectUrl, alt: '' }));
    });
    return el;
  }

  function loadAvatarBlob(url) {
    if (avatarBlobCache[url] !== undefined) return Promise.resolve(avatarBlobCache[url]);
    // Same shape as asclepius.js's loadAvatarBlob (extracted by a node
    // harness): no helper call; the sandbox shell's fetch wrapper stamps the realm.
    return fetch(url, {
      headers: state.token ? { Authorization: 'Bearer ' + state.token } : {},
    }).then((res) => (res.ok ? res.blob() : null))
      .then((blob) => {
        const objectUrl = blob ? URL.createObjectURL(blob) : null;
        // Cached either way, null included: a member with no picture must not
        // cost one 404 per message they have ever posted in the channel.
        avatarBlobCache[url] = objectUrl;
        return objectUrl;
      })
      .catch(() => null);
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

  /* ── The digest card (Digest Design PRD §1) ──────────────────────────────
   *
   * A digest is not a message somebody wrote, and it is not a list either. It
   * used to render as one: a mono eyebrow, a section label, a headline, a grey
   * sentence, a source that said "STAT ↗". Nothing was promoted, nothing was
   * tinted, and the headline and the description said the same thing twice.
   *
   * Now one story leads. It gets a rule in its tag's tint, a badge, a bigger
   * headline, one sentence of deck and its why-it-matters on a lime wash with
   * no label — the wash IS the label. Everything else is a compact row: tag,
   * headline, one line, one link.
   *
   * Old digests keep their old rendering. They have no payload, `digestOf`
   * returns null, and `renderBody` handles them exactly as before — which is
   * the entire migration: no backfill, no dual-write, and a room that scrolls
   * from markdown posts into cards without a gap.
   *
   * Everything here is a TEXT NODE via h(). The headlines, decks and one-liners
   * were written by a model over somebody else's web page, and the server's
   * escaping is not a reason for the client to stop doing its own. */

  /* An SVG element is NOT an HTML element, and `h()` builds HTML.
     `document.createElement('svg')` returns an HTMLUnknownElement that lays out
     as an inline box and paints nothing, which is the failure mode where the
     markup is right in devtools and the icon is simply absent. Five glyphs are
     also the entire reason this page loads no icon font and must not start
     loading one, so they are inline paths and this is what draws them. */
  const SVG_NS = 'http://www.w3.org/2000/svg';
  function svgIcon(paths) {
    const el = document.createElementNS(SVG_NS, 'svg');
    el.setAttribute('viewBox', '0 0 24 24');
    el.setAttribute('width', '14');
    el.setAttribute('height', '14');
    el.setAttribute('fill', 'none');
    el.setAttribute('stroke', 'currentColor');
    el.setAttribute('stroke-width', '1.8');
    el.setAttribute('stroke-linecap', 'round');
    el.setAttribute('stroke-linejoin', 'round');
    el.setAttribute('aria-hidden', 'true');
    el.setAttribute('focusable', 'false');
    for (const d of paths) {
      const p = document.createElementNS(SVG_NS, 'path');
      p.setAttribute('d', d);
      el.appendChild(p);
    }
    return el;
  }

  /* The tag vocabulary: five sections, five Tabler outline shapes, five washes.
     Keyed on the contract's own section names (community/digest_contract.py
     SECTIONS), so a section the backend adds without a tag here is visible as a
     plain chip rather than silently dropped. */
  const DIGEST_TAGS = {
    Regulation: { key: 'regulation', icon: [
      'M7 20h10', 'M6 6l6 -1l6 1', 'M12 3v17',
      'M9 12l-3 -6l-3 6a3 3 0 0 0 6 0', 'M21 12l-3 -6l-3 6a3 3 0 0 0 6 0'] },
    Research: { key: 'research', icon: [
      'M9 3h6', 'M10 9h4', 'M10 3v6l-4.5 8a2 2 0 0 0 1.7 3h9.6a2 2 0 0 0 1.7 -3l-4.5 -8v-6'] },
    Deployment: { key: 'deployment', icon: [
      'M3 21h18', 'M5 21v-16a2 2 0 0 1 2 -2h10a2 2 0 0 1 2 2v16',
      'M9 21v-4a2 2 0 0 1 2 -2h2a2 2 0 0 1 2 2v4', 'M10 9h4', 'M12 7v4'] },
    Evals: { key: 'evals', icon: [
      'M4 12h16a1 1 0 0 1 1 1v6a1 1 0 0 1 -1 1h-16a1 1 0 0 1 -1 -1v-6a1 1 0 0 1 1 -1z',
      'M6 12v3', 'M9 12v2', 'M12 12v3', 'M15 12v2', 'M18 12v3', 'M3 4v3', 'M3 5h18', 'M21 4v3'] },
    Opinion: { key: 'opinion', icon: [
      'M10 11h-4a1 1 0 0 1 -1 -1v-3a1 1 0 0 1 1 -1h3a1 1 0 0 1 1 1v6c0 2.667 -1.333 4.333 -4 5',
      'M19 11h-4a1 1 0 0 1 -1 -1v-3a1 1 0 0 1 1 -1h3a1 1 0 0 1 1 1v6c0 2.667 -1.333 4.333 -4 5'] },
  };

  function tagChipEl(section) {
    const tag = DIGEST_TAGS[section];
    const chip = h('span', {
      class: 'cm-tag' + (tag ? ' cm-tag-' + tag.key : ''),
    });
    if (tag) chip.appendChild(svgIcon(tag.icon));
    chip.appendChild(h('span', { class: 'cm-tag-label' }, String(section || '')));
    return chip;
  }

  function digestOf(m) {
    if (!m || (m.kind !== 'digest_news' && m.kind !== 'digest_papers')) return null;
    const p = m.payload;
    if (!p || typeof p !== 'object' || !Array.isArray(p.items)) return null;
    // Defensive, not paranoid: the server validated this payload before storing
    // it, but a row written by an older build or repaired by hand must degrade
    // to the markdown body rather than render a card with holes in it.
    const items = p.items.filter((it) => it && it.headline);
    return items.length ? { title: p.title || 'Medical AI Digest', items } : null;
  }

  /* Which story leads, said once. The server marks it (`lead: true`) and this
     mirrors community/digest_contract.py `lead_and_rest` for the payloads
     written before it did — the fallback is the first item, which is the same
     answer the backend falls back to, so the card and the email cannot promote
     different stories. */
  function digestLead(items) {
    const idx = Math.max(0, items.findIndex((it) => it.lead));
    return { lead: items[idx], rest: items.filter((_, i) => i !== idx) };
  }

  /* `Full article →` and the publisher beside it in mono. One primary action
     per item and never a bare host string: "statnews.com" is not how a person
     says STAT, and the link already carries the host. */
  function digestSourceEl(it) {
    const url = String(it.url || '');
    const safe = /^https?:\/\//i.test(url);
    const row = h('div', { class: 'cm-digest-src' });
    if (safe) {
      row.appendChild(h('a', {
        class: 'cm-digest-link', href: url, target: '_blank', rel: 'noopener noreferrer',
      }, 'Full article', h('span', { class: 'cm-digest-arrow', 'aria-hidden': 'true' }, '→')));
    }
    if (it.source) row.appendChild(h('span', { class: 'cm-digest-pub' }, it.source));
    return row.childNodes.length ? row : null;
  }

  function digestHeadlineEl(it, cls) {
    const url = String(it.url || '');
    const safe = /^https?:\/\//i.test(url);
    return safe
      ? h('a', { class: cls, href: url, target: '_blank', rel: 'noopener noreferrer' }, it.headline)
      : h('div', { class: cls }, it.headline);
  }

  function digestLeadEl(it, m) {
    const tag = DIGEST_TAGS[it.section];
    const wrap = h('div', {
      class: 'cm-digest-lead' + (tag ? ' cm-tag-' + tag.key : ''),
    },
      h('div', { class: 'cm-digest-leadhead' },
        h('span', { class: 'cm-digest-badge' }, it.urgent ? 'BREAKING' : 'TOP STORY'),
        tagChipEl(it.section)),
      digestHeadlineEl(it, 'cm-digest-headline'));
    if (it.deck) wrap.appendChild(h('p', { class: 'cm-digest-deck' }, it.deck));
    // No label above it. The wash is the label -- a "WHY IT MATTERS" eyebrow is
    // a line that can be deleted without the card losing anything.
    if (it.why_it_matters) wrap.appendChild(h('div', { class: 'cm-why' }, it.why_it_matters));
    const src = digestSourceEl(it);
    if (src) wrap.appendChild(src);
    const admin = m ? digestAdminEl(m, it, true) : null;
    if (admin) wrap.appendChild(admin);
    return wrap;
  }

  /* The admin override (§2.2), on the post itself.
   *
   * It lives here rather than on the admin console's community card because
   * that card's feed carries an author, a channel and a body string -- no
   * message id and no payload -- so the action would have needed a wider
   * summary endpoint to reach the thing it acts on. The digest post IS the
   * admin menu for the digest post.
   *
   * Two verbs and no text field. `Set as top story` moves the rule and the
   * badge; `Clear breaking` takes the badge off. Neither can change a word on
   * the card, because every word came through the compose contract and the
   * house-style gate, and an override that accepted prose would be a way to
   * put unvalidated text on a post signed by the platform. Setting BREAKING is
   * not offered at all: it is earned by one of four same-day events the
   * compose pass can see and an admin cannot.
   *
   * Invisible to everyone else. A physician reading the morning's news has no
   * business seeing the levers behind it. */
  function digestAdminEl(m, it, isLead) {
    if (!state.isAdmin || state.preview) return null;
    const row = h('div', { class: 'cm-digest-admin' });
    const send = (patch, label) => {
      api('/admin/messages/' + m.id + '/digest-lead', { method: 'POST', body: patch })
        .then((updated) => {
          // The server answers with the re-rendered message and the socket
          // broadcasts the same thing to everyone else in the room. Patching
          // the row in hand keeps the admin who pressed it from waiting on
          // their own broadcast to see what they did.
          Object.assign(m, updated || {});
          renderMessages({});
          toast(label, 'success');
        })
        .catch((e) => toast(e.message, 'error'));
    };
    if (!isLead) {
      row.appendChild(h('button', { class: 'cm-digest-admin-btn', type: 'button',
        onClick: () => send({ url: it.url }, 'Top story set') }, 'Set as top story'));
    }
    if (isLead && it.urgent) {
      row.appendChild(h('button', { class: 'cm-digest-admin-btn', type: 'button',
        onClick: () => send({ urgent: false }, 'Breaking cleared') }, 'Clear breaking'));
    }
    return row.childNodes.length ? row : null;
  }

  function digestItemEl(it, m) {
    const row = h('div', { class: 'cm-digest-item' }, tagChipEl(it.section),
      digestHeadlineEl(it, 'cm-digest-subhead'));
    // Compact items carry no deck by contract; guarded anyway, because a row
    // repaired by hand must not put a second paragraph in a one-line card.
    if (it.why_it_matters) {
      row.appendChild(h('div', { class: 'cm-digest-line' }, it.why_it_matters));
    }
    const src = digestSourceEl(it);
    if (src) row.appendChild(src);
    const admin = m ? digestAdminEl(m, it, false) : null;
    if (admin) row.appendChild(admin);
    return row;
  }

  /* The reader's own weekday, not the server's. A digest fires on a UTC hour
     and telling a physician in Mumbai it is Tuesday when their phone says
     Wednesday is the same arithmetic `fmtWhen` already refuses to make them do. */
  function digestWeekday(iso) {
    try {
      const d = new Date(iso);
      // An unparseable date does not throw here: `new Date('x')` is a valid
      // Date object whose time is NaN, and toLocaleDateString returns the
      // STRING "Invalid Date" for it. A try/catch never sees that, and the card
      // renders "Invalid Date · 3 stories" in its header.
      if (isNaN(d.getTime())) return '';
      return d.toLocaleDateString([], { weekday: 'long' });
    } catch (e) { return ''; }
  }

  /* One component, two contexts (§1.3). `collapsed` is the pinned home card:
     the same title and the same lead, then `Read all {n} →` into the channel
     post. A second component would be a second place for the design to drift. */
  function digestCardEl(m, dg, opts) {
    opts = opts || {};
    const collapsed = !!opts.collapsed;
    const { lead, rest } = digestLead(dg.items);
    const weekday = digestWeekday(m.created_at);
    const n = dg.items.length;

    const card = h('div', { class: 'cm-digest' + (collapsed ? ' cm-digest-pinned' : '') },
      h('div', { class: 'cm-digest-head' },
        h('div', { class: 'cm-digest-title' }, dg.title),
        h('div', { class: 'cm-digest-meta' },
          (weekday ? weekday + ' · ' : '') + n + (n === 1 ? ' story' : ' stories'))));

    // The pinned card carries no admin controls: it is the same post seen from
    // the landing room, and two places to press the same button is two places
    // to press it twice.
    card.appendChild(digestLeadEl(lead, collapsed ? null : m));
    if (!collapsed) for (const it of rest) card.appendChild(digestItemEl(it, m));

    card.appendChild(h('button', {
      class: 'cm-digest-foot', type: 'button',
      onClick: () => (collapsed ? openDigestPost(m) : openThread(m.id)),
    }, collapsed ? 'Read all ' + n : 'Discuss',
      h('span', { class: 'cm-digest-arrow', 'aria-hidden': 'true' }, '→')));
    return card;
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

    // A structured digest renders as a card; everything else, including every
    // digest written before the payload column existed, renders as a body.
    const dg = digestOf(m);
    const bodyEl = dg
      ? digestCardEl(m, dg)
      : h('div', { class: 'cm-msg-body', html: renderBody(m.body, m.mentions) });

    // A view-only reader keeps the thread affordance, because opening a thread
    // is reading. Reacting and pinning change what everyone else sees, and the
    // server refuses both, so the buttons go rather than fail.
    const canPin = !inThread && !isDmKey(state.active) && state.canPost;
    // A digest carries ONE action, on the card. The reaction and reply buttons
    // go: a row of chrome beside a designed card is clutter, and the card's own
    // "Discuss in thread" is the same affordance said once and legibly. Pin and
    // delete stay, because they are moderation and a digest is as moderable as
    // anything else in the room.
    const actions = h('div', { class: 'cm-msg-actions', role: 'toolbar', 'aria-label': 'Message actions' },
      state.canPost && !dg ? h('button', { class: 'cm-act', 'data-emoji-btn': '1', title: 'Add reaction', 'aria-label': 'Add reaction',
        onClick: (e) => { e.stopPropagation(); toggleEmojiPop(m); } }, '😀') : null,
      !inThread && !dg ? h('button', { class: 'cm-act', title: 'Reply in thread', 'aria-label': 'Reply in thread',
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
    // A pin was a 0.7rem emoji at 75% opacity in the header row, which is
    // smaller than the timestamp beside it. Somebody pinned it because the room
    // should not scroll past it, and nothing about the message said so.
    if (m.pinned) kindClass += ' cm-msg-pinned';

    const col = h('div', { class: 'cm-msg-col' },
      h('div', { class: 'cm-msg-head' },
        h('button', { class: 'cm-msg-author', onClick: a.user_id ? () => openMember(a.user_id) : null,
          onMouseenter: (e) => showHoverCard(a, e), onMouseleave: hideHoverCard },
          a.display_name || 'Former member'),
        a.verified ? h('span', { class: 'cm-verified', title: 'Credential-verified' }) : null,
        specChipEl(a),
        m.pinned ? h('span', { class: 'cm-pin-marker', title: 'Pinned' }, 'PINNED') : null,
        h('span', { class: 'cm-msg-time' }, fmtTime(m.created_at),
          m.edited_at ? h('span', { class: 'cm-msg-edited' }, '(edited)') : null)),
      state.editing === m.id ? editBoxEl(m) : bodyEl,
      cardsEl(m.cards),
      m.poll ? pollCardEl(m) : null,
      attachmentsEl(m),
      dg ? null : reactionsEl(m),
      // The card carries its own thread affordance with the same count, so the
      // teaser underneath would be the second copy of one link.
      !inThread && !dg && m.reply_count > 0 ? threadTeaser(m) : null);
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
        h('p', {}, state.isAdmin ? 'Post one with “New event”, it pins to the top here.'
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

  /* ── Group conversations ────────────────────────────────────────────────
   *
   * A group is a DM with a roster and a name its author chose. The picker is
   * built from ``state.membersById``, which is the SAME directory the rail and
   * the @mention completion read, so the people offered here are exactly the
   * colleagues who could already be DM'd one at a time. There is no second
   * source of "who exists", and so no way for this to offer somebody the
   * server would then refuse.
   */
  function memberPicker(excludeIds) {
    const skip = new Set(excludeIds || []);
    if (state.me) skip.add(state.me.user_id);
    const picked = new Set();
    const list = h('div', { class: 'cm-picker' });
    const people = state.members
      .filter((m) => !skip.has(m.user_id))
      .sort((a, b) => String(a.display_name || '').localeCompare(String(b.display_name || '')));
    if (!people.length) {
      list.appendChild(h('div', { class: 'cm-rail-hint' }, 'No colleagues to add.'));
    }
    for (const m of people) {
      const cb = h('input', { type: 'checkbox' });
      cb.addEventListener('change', () => {
        if (cb.checked) picked.add(m.user_id); else picked.delete(m.user_id);
      });
      list.appendChild(h('label', { class: 'cm-picker-row' }, cb,
        h('span', {}, m.display_name),
        m.specialty ? h('span', { class: 'chrome' }, m.specialty) : null));
    }
    return { el: list, chosen: () => Array.from(picked) };
  }

  function openNewGroup() {
    openModal('New group', (close) => {
      const title = h('input', { type: 'text', maxlength: '120',
        placeholder: 'Transplant call rota' });
      const picker = memberPicker([]);
      return h('div', { class: 'cm-modal-body' },
        field('Name', title),
        field('People', picker.el),
        h('div', { class: 'cm-modal-actions' },
          h('button', { class: 'cm-btn cm-btn-ghost', onClick: close }, 'Cancel'),
          h('button', { class: 'cm-btn cm-btn-primary', onClick: async () => {
            const name = title.value.trim();
            const ids = picker.chosen();
            if (!name) { toast('Give the group a name.', 'error'); return; }
            if (!ids.length) { toast('Pick at least one colleague.', 'error'); return; }
            try {
              const d = await api('/dms/group',
                { method: 'POST', body: { title: name, user_ids: ids } });
              close();
              if (!state.dms.some((x) => x.id === d.id)) state.dms.unshift(d);
              renderRail();
              await openDm(d.id, { force: true });
            } catch (e) { toast(e.message, 'error'); }
          } }, 'Create group')));
    });
  }

  function openAddGroupMembers(dm) {
    openModal('Add people', (close) => {
      const already = ((dm && dm.participants) || []).map((m) => m.user_id);
      const picker = memberPicker(already);
      return h('div', { class: 'cm-modal-body' },
        field('People', picker.el),
        h('div', { class: 'cm-modal-actions' },
          h('button', { class: 'cm-btn cm-btn-ghost', onClick: close }, 'Cancel'),
          h('button', { class: 'cm-btn cm-btn-primary', onClick: async () => {
            const ids = picker.chosen();
            if (!ids.length) { toast('Pick at least one colleague.', 'error'); return; }
            try {
              const updated = await api('/dms/' + encodeURIComponent(dm.id) + '/members',
                { method: 'POST', body: { user_ids: ids } });
              close();
              applyDmSummary(updated);
              renderHead();
            } catch (e) { toast(e.message, 'error'); }
          } }, 'Add')));
    });
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
              toast('Event posted: pinned to the top of #events.');
            } catch (e) {
              toast(e.status === 422 ? (e.detail && e.detail.message) || e.message : e.message, 'error');
            }
          } }, 'Post event')));
      return body;
    });
  }

  // ─── Posting as the Archangel account (staff only) ─────────────────────────
  // The bot voice used to be reachable only from code, so anything the team
  // wanted to say in it had to be shipped. This is the same voice with a human
  // trigger. It lives here rather than in the portal's admin tabs on purpose:
  // it belongs beside the channels it writes into, and those tabs are being
  // rebuilt elsewhere.
  //
  // Only channels where a bot post is what a reader already expects. #general
  // and the specialty rooms are absent deliberately: a room of colleagues
  // talking to each other is not somewhere the company account should appear
  // as though it were one of them. The server enforces the same list.
  const PERSONA_CHANNELS = [
    'task-announcements', 'events', 'medical-ai-news',
    'research-and-opportunities', 'team-ai-spotlight',
  ];

  function personaChannels() {
    // Intersected with what this admin can actually see, so the picker never
    // offers a room the post would 404 in.
    const live = state.channels.map((c) => c.slug);
    const out = PERSONA_CHANNELS.filter((s) => live.indexOf(s) !== -1);
    return out.length ? out : PERSONA_CHANNELS.slice();
  }

  function openPersonaComposer(preferSlug) {
    openModal('Post as Archangel', (close) => {
      const slugs = personaChannels();
      const picker = h('select', { class: 'cm-persona-channel' });
      slugs.forEach((s) => picker.appendChild(h('option', { value: s }, '#' + s)));
      if (preferSlug && slugs.indexOf(preferSlug) !== -1) picker.value = preferSlug;
      const text = h('textarea', {
        rows: '8', maxlength: '8000',
        placeholder: 'Written in the Archangel voice. No patient identifiers.',
      });
      const announce = h('input', { type: 'checkbox' });
      const announceRow = h('label', { class: 'cm-persona-announce' }, announce,
        h('span', {}, 'Email this to every member'));
      function syncAnnounce() {
        // The fan-out rule is the server's, mirrored here so the box is not
        // offered where it would be silently ignored.
        const on = picker.value === 'task-announcements';
        announceRow.style.display = on ? '' : 'none';
        if (!on) announce.checked = false;
      }
      picker.addEventListener('change', syncAnnounce);
      syncAnnounce();
      const post = h('button', { class: 'cm-btn cm-btn-primary', onClick: async () => {
        const body = text.value.trim();
        if (!body) { toast('Nothing to post.', 'error'); return; }
        post.disabled = true;
        try {
          await api('/admin/community/post', {
            method: 'POST',
            base: '/api/asclepius',
            body: { channel_slug: picker.value, body: body, announce: announce.checked },
          });
          close();
          toast('Posted to #' + picker.value + ' as Archangel.');
        } catch (e) {
          post.disabled = false;
          toast(e.message, 'error');
        }
      } }, 'Post as Archangel');
      return h('div', { class: 'cm-modal-body' },
        field('Channel', picker),
        field('Message', text),
        announceRow,
        h('p', { class: 'cm-persona-note' },
          'This posts under the Archangel account, not your name. Every post is '
          + 'logged against you.'),
        h('div', { class: 'cm-modal-actions' },
          h('button', { class: 'cm-btn cm-btn-ghost', onClick: close }, 'Cancel'),
          post));
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
        'aria-label': g.emoji + ' ' + g.count + ' - ' + names,
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
        headers: realmHeaders({ 'Authorization': 'Bearer ' + state.token }),
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
          state.preview
            // Not "you have view-only access": in the preview there is nothing
            // here to have access to. Say what unlocks it instead.
            ? 'This is a preview. The rooms, and the colleagues in them, open '
              + 'when your application is approved.'
            : 'You have view-only access. Every channel is open to read; posting is '
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
        placeholder: isCaseRoom(d)
          ? 'Message the case team…'
          : 'Message ' + (peer.display_name || 'colleague') + '…',
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
    /* Your own send sticks to the bottom.
     *
     * ``ingestMessage`` renders with ``{}``, which keeps the scroll position
     * unless the reader was already at the bottom — right for somebody else's
     * message arriving while you read history, and wrong for your own, which
     * then lands off-screen and reads as a message that did not send. */
    if (dmId === state.active) renderMessages({ stickBottom: true });
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
  /* "SA" means nothing to a colleague reading a profile; "Saudi Arabia" does.
   * Resolved in the browser rather than by adding a display name to the member
   * payload, because the payload is the card's whitelist and this is a
   * rendering concern. Falls back to the raw code for a region the platform has
   * no name for, which is still better than a blank line — the same rule
   * asclepius/card.py::_country_display states for the card itself. */
  function countryLabel(code) {
    const raw = String(code || '').trim().toUpperCase();
    if (!raw) return null;
    try {
      const names = new Intl.DisplayNames([navigator.language || 'en'], { type: 'region' });
      return names.of(raw) || raw;
    } catch (e) { return raw; }
  }

  /* The credential line: "Nephrology · 15-19 yrs" (§3.2).
     One line under the name, assembled from the two Tier A facts every member
     row already carries. Staff say who they are instead, because "Archangel ·
     12 yrs" reads as a specialty they do not have. */
  function credentialLine(m) {
    if (m.is_bot) return 'Archangel Health';
    const bits = [];
    if (m.is_staff) bits.push('Archangel Health');
    else if (m.specialty) bits.push(m.specialty);
    if (m.years_in_practice != null && m.years_in_practice !== '') {
      bits.push(m.years_in_practice + ' yrs');
    }
    return bits.join(' · ') || null;
  }

  /* THE SAME FACTS AS THE VERIFIED CARD, and no others.
   *
   * backend/asclepius/card.py::CARD_FIELDS is the one place that decides what a
   * verified physician's profile says: picture, name, checkmark, specialty,
   * years in practice, country. Three surfaces render that card and they are
   * required not to drift; this panel was a fourth surface rendering a
   * different, longer set (institution, board certification, fellowship
   * training), so a colleague opening a name in the community saw something
   * other than what the same person shares as their card. The redesign turned
   * that list into three stats and changed nothing about WHICH three.
   *
   * The PRD asks for cases, joined and last-active instead. None of the three
   * exists in the community's member payload (community/router.py member_map is
   * Tier A plus the users table, and case counts live on the asclepius plane
   * behind a different gate), and widening the API to decorate a card is how a
   * profile panel becomes a fourth source of truth again. So: the card's three.
   *
   * The avatar, the name and the checkmark are drawn by the panel head. Nothing
   * gets added here without being added to CARD_FIELDS first, and the
   * contributor score never appears on either -- a physician is not shown their
   * own, so a colleague certainly is not. */
  function profileStats(m) {
    // "Not shared" rather than a dash. A dash in a stat slot reads as a value
    // the product failed to load; the words say the true thing, which is that
    // this colleague has not filled that field in.
    const unset = 'Not shared';
    const stats = [
      ['Specialty', m.is_staff ? 'Archangel' : (m.specialty || unset)],
      ['In practice', (m.years_in_practice != null && m.years_in_practice !== '')
        ? m.years_in_practice + ' yrs' : unset],
      ['Country', countryLabel(m.country) || unset],
    ];
    return h('div', { class: 'cm-profile-stats' }, stats.map(([k, v]) =>
      h('div', { class: 'cm-profile-stat' },
        h('div', { class: 'cm-profile-stat-v' }, String(v)),
        h('div', { class: 'cm-profile-stat-k chrome' }, k))));
  }

  /* Where the founders' 20 minutes lives, handed over by the shell.
     Read once, defensively: a page served without the attribute renders a
     profile with one action instead of throwing inside the panel. */
  function founderCalendly() {
    try {
      const url = document.body.getAttribute('data-founder-calendly') || '';
      return /^https:\/\//i.test(url) ? url : null;
    } catch (e) { return null; }
  }

  /* The best view of a colleague we currently hold.
   *
   * The directory row is a summary, so it carries a name, an avatar and a
   * specialty and nothing else; the profile fields arrive from
   * /members/{id} the first time somebody is opened and are kept. Merging
   * rather than replacing means a panel opened before the fetch lands still
   * renders the name and avatar we already had, and the extra rows fill in. */
  function memberView(userId, fallback) {
    return Object.assign({}, fallback || {}, state.membersById[userId] || {},
                         state.profilesById[userId] || {});
  }

  async function loadMemberProfile(userId) {
    if (state.profilesById[userId]) return state.profilesById[userId];
    try {
      const d = await api('/members/' + encodeURIComponent(userId));
      if (d && d.member) state.profilesById[userId] = d.member;
      return state.profilesById[userId] || null;
    } catch (e) {
      // A profile that will not load is a thinner panel, never a broken one:
      // the summary we already hold still renders.
      return null;
    }
  }

  function openMember(userId) {
    if (!state.membersById[userId]) return;
    // Fill the panel in with whatever we hold now, then fetch the rest and
    // redraw. Waiting on the request first would leave the rail click doing
    // nothing visible for a round trip.
    // No dossier fetch in preview: there is no such member to fetch, and the
    // endpoint refuses this account anyway. The summary the fixture supplied
    // is the whole profile.
    if (!state.preview && !state.profilesById[userId]) {
      loadMemberProfile(userId).then((full) => {
        if (full && state.sidePanel === 'member'
            && state.sideMember && state.sideMember.user_id === userId) {
          openMember(userId);
        }
      });
    }
    const m = memberView(userId);
    hideHoverCard();
    state.sidePanel = 'member';
    state.sideMember = m;
    const side = document.getElementById('cmSide');
    clear(side);
    side.appendChild(h('div', { class: 'cm-side-head' },
      h('span', { class: 'cm-side-title' }, 'Profile'),
      h('button', { class: 'cm-iconbtn', 'aria-label': 'Close panel', onClick: closeSide }, '✕')));
    const body = h('div', { class: 'cm-side-body' });
    /* Three stats and ONE action (§3.2). The blurb and the field list are gone:
       the blurb repeated the credential line in longer words, and the list said
       the same three facts the stats say. What is left is a card a colleague
       reads in a glance and one thing they can do about it.

       Founders add a second: twenty minutes, on the same link every physician
       gets on the portal. Two actions on one card is a rule broken on purpose,
       for the two people it is worth breaking for. */
    const calendly = founderCalendly();
    const cred = credentialLine(m);
    const canMessage = state.canPost && state.me && m.user_id !== state.me.user_id;
    const actions = h('div', { class: 'cm-profile-actions' });
    if (canMessage) {
      actions.appendChild(h('button', {
        class: 'cm-btn cm-btn-primary cm-profile-action',
        onClick: () => startDmWith(m.user_id),
      }, 'Message'));
    }
    if (m.is_staff && !m.is_bot && calendly) {
      actions.appendChild(h('a', {
        class: 'cm-btn cm-btn-ghost cm-profile-action',
        href: calendly, target: '_blank', rel: 'noopener noreferrer',
      }, 'Book 20 minutes'));
    }

    body.appendChild(h('div', { class: 'cm-profile' },
      h('div', { class: 'cm-profile-head' },
        avatarEl(m, 'big'),
        h('div', {},
          h('div', { class: 'cm-profile-name' }, m.display_name,
            m.verified ? h('span', { class: 'cm-verified', title: 'Credential-verified' }) : null),
          cred ? h('div', { class: 'cm-profile-cred' }, cred) : null,
          h('div', { class: 'chrome' }, state.online.has(m.user_id) ? 'online' : 'offline'))),
      profileStats(m),
      actions.childNodes.length ? actions : null));
    side.appendChild(body);
    openSide();
  }

  let hoverCardEl = null; let hoverTimer = null;
  function showHoverCard(author, evt) {
    if (!author || !author.user_id) return;
    // A message author arrives on the message itself and still carries the
    // full Tier A profile, so the card is unchanged for the case it is used in
    // most. Anything already fetched wins over it; the summary is the floor.
    const m = memberView(author.user_id, author);
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
      // The socket cannot carry a header, so the realm rides beside the ticket;
      // the server binds the ticket to the realm it was minted in.
      ws = new WebSocket(proto + location.host + API + '/ws?ticket=' + encodeURIComponent(ticket) + '&realm=' + REALM);
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
        // The bar counts the same set the rail does, so it repaints with it.
        renderGreeting();
        renderRail();
        break;
      }
      case 'message.created': ingestMessage(ev.message); break;
      /* A conversation that now exists for you: somebody opened a DM with you,
       * the bot opened one to route you a case, or a case room was created and
       * you are on it. Every one of those used to be invisible until a reload,
       * because the client stops polling while the socket is healthy and the
       * server never pushed anything when it wrote them. */
      case 'dm.created': applyDmSummary(ev.dm); break;
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

  /* Insert or refresh one conversation in the rail.
   *
   * Merged rather than replaced, and this is the reason: a ``dm.created`` for a
   * conversation already on screen (a case room that gained a member, a group
   * somebody was added to) carries no unread count and no last-message id,
   * because the server built it from the conversation row rather than from this
   * viewer's read cursor. Overwriting would silently zero the unread badge of a
   * conversation with unread messages in it.
   */
  function applyDmSummary(dm) {
    if (!dm || !dm.id) return;
    const i = state.dms.findIndex((x) => x.id === dm.id);
    if (i === -1) state.dms.unshift(dm);
    else state.dms[i] = Object.assign({}, state.dms[i], dm, {
      unread: dm.unread != null && dm.unread > 0 ? dm.unread : state.dms[i].unread,
      last_message_id: dm.last_message_id || state.dms[i].last_message_id,
      last_message_at: dm.last_message_at || state.dms[i].last_message_at,
    });
    renderRail();
    if (dm.id === state.active) renderHead();
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
      const mine = state.me && msg.author && msg.author.user_id === state.me.user_id;
      const viewing = msg.dm === state.active && !document.hidden;
      const counts = !mine && !viewing;
      let dm = state.dms.find((x) => x.id === msg.dm);
      if (!dm) {
        /* First message of a conversation we do not hold yet. The refetch used
         * to be the whole branch, and it dropped the unread: ``loadDms`` reads
         * the count off the server's read cursor, and on a message that arrived
         * a millisecond ago that cursor has not moved, so the badge came back
         * correct only by luck of timing. Bump it explicitly once the row is
         * here, exactly as the known-conversation branch does. */
        loadDms().then(() => {
          const fresh = state.dms.find((x) => x.id === msg.dm);
          if (fresh && counts && !(fresh.unread > 0)) fresh.unread = 1;
          renderRail();
        }).catch(() => { /* transient */ });
      } else {
        dm.last_message_id = msg.id;
        dm.last_message_at = msg.created_at;
        if (counts) dm.unread = (dm.unread || 0) + 1;
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
