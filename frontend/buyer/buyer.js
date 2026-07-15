/* ═══════════════════════════════════════════════════════════
   Secure Data Workspace — buyer.js
   Vanilla JS, no build step. Single-page state machine, three screens:
     1) login             → POST /auth/login, store token, GET /buyer/me
     2) reset (forced)    → shown when me.must_reset_password === true
     3) workspace         → the deliveries list once authed + reset done

   Security posture (mirrors the provider portal):
   - Bearer token in localStorage['asclepius_buyer_token'].
   - Every dynamic, server-provided string is written with textContent.
   - A mid-session 401/403 clears the token and bounces to login.
   ═══════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  const API_BASE = "/api/asclepius";
  const TOKEN_KEY = "asclepius_buyer_token";
  const MIN_PW_LEN = 12;

  const root = document.getElementById("bwRoot");
  const header = document.getElementById("bwHeader");
  const userBadge = document.getElementById("bwUserBadge");
  const logoutBtn = document.getElementById("bwLogoutBtn");
  const toastRegion = document.getElementById("bwToasts");

  let currentUser = null;

  const getToken = () => localStorage.getItem(TOKEN_KEY) || "";
  const setToken = (t) => localStorage.setItem(TOKEN_KEY, t);
  const clearToken = () => localStorage.removeItem(TOKEN_KEY);

  class AuthError extends Error {}

  function authHeaders(extra) {
    const h = Object.assign({ Accept: "application/json" }, extra || {});
    const tok = getToken();
    if (tok) h.Authorization = "Bearer " + tok;
    return h;
  }

  async function parseJson(res) { return res.json().catch(() => ({})); }

  async function apiJson(method, path, body) {
    const opts = { method, headers: authHeaders() };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(API_BASE + path, opts);
    const data = await parseJson(res);
    if (res.status === 401 || res.status === 403) {
      throw new AuthError(data.detail || "Your session has ended.");
    }
    if (!res.ok) throw new Error(data.detail || ("Request failed (" + res.status + ")"));
    return data;
  }

  const apiGet = (path) => apiJson("GET", path);
  const apiPost = (path, body) => apiJson("POST", path, body);

  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); }

  function mountTemplate(id) {
    const tpl = document.getElementById(id);
    clear(root);
    root.appendChild(tpl.content.cloneNode(true));
  }

  function showError(el, msg) { if (el) { el.textContent = msg || ""; el.hidden = !msg; } }

  function toast(msg, kind) {
    const t = document.createElement("div");
    t.className = "asc-toast" + (kind ? " " + kind : "");
    t.textContent = msg;
    toastRegion.appendChild(t);
    setTimeout(() => {
      t.style.transition = "opacity .3s";
      t.style.opacity = "0";
      setTimeout(() => t.remove(), 320);
    }, 4200);
  }

  function bounceToLogin(msg) {
    clearToken();
    currentUser = null;
    header.hidden = true;
    renderLogin();
    if (msg) toast(msg, "error");
  }

  function formatBytes(n) {
    if (!n && n !== 0) return "";
    if (n < 1024) return n + " B";
    const units = ["KB", "MB", "GB", "TB"];
    let v = n / 1024, i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return v.toFixed(v >= 10 ? 0 : 1) + " " + units[i];
  }

  // Stored timestamps are naive-UTC ISO strings — pin to UTC then render Pacific.
  function formatWhen(iso) {
    if (!iso) return "—";
    let s = iso;
    if (typeof s === "string" && /\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(s) && !/[zZ]|[+-]\d{2}:?\d{2}$/.test(s)) s += "Z";
    const d = new Date(s);
    if (isNaN(d.getTime())) return String(iso);
    return d.toLocaleString("en-US", { timeZone: "America/Los_Angeles" }) + " PT";
  }

  // ══════════════════════════════════════════════════════════
  //  SCREEN 1 — LOGIN
  // ══════════════════════════════════════════════════════════
  function renderLogin() {
    header.hidden = true;
    mountTemplate("tplLogin");
    const form = document.getElementById("bwLoginForm");
    const errBox = document.getElementById("bwLoginError");
    const btn = document.getElementById("bwLoginBtn");
    const emailEl = document.getElementById("bwEmail");
    const pwEl = document.getElementById("bwPassword");
    emailEl.focus();

    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const email = (emailEl.value || "").trim();
      const password = pwEl.value || "";
      showError(errBox, "");
      if (!email || !password) { showError(errBox, "Enter your email and password."); return; }
      btn.disabled = true;
      btn.textContent = "Signing in…";
      try {
        const data = await apiPost("/auth/login", { email, password });
        if (!data.token) throw new Error("No token returned by the server.");
        setToken(data.token);
        await loadProfileAndRoute();
      } catch (e) {
        showError(errBox, e.message || "Sign-in failed. Check your details.");
        btn.disabled = false;
        btn.textContent = "Sign in securely";
      }
    });
  }

  // ══════════════════════════════════════════════════════════
  //  SCREEN 2 — FORCED PASSWORD RESET
  // ══════════════════════════════════════════════════════════
  function scorePassword(pw) {
    let score = 0;
    if (pw.length >= MIN_PW_LEN) score++;
    if (pw.length >= 16) score++;
    if (/[a-z]/.test(pw) && /[A-Z]/.test(pw)) score++;
    if (/\d/.test(pw)) score++;
    if (/[^A-Za-z0-9]/.test(pw)) score++;
    if (pw.length < MIN_PW_LEN) return { cls: "weak", label: "Too short — use at least " + MIN_PW_LEN + " characters." };
    if (score <= 2) return { cls: "fair", label: "Okay — add length or variety to strengthen it." };
    if (score === 3) return { cls: "fair", label: "Good." };
    return { cls: "strong", label: "Strong password." };
  }

  function renderReset() {
    header.hidden = true;
    mountTemplate("tplReset");
    const form = document.getElementById("bwResetForm");
    const errBox = document.getElementById("bwResetError");
    const okBox = document.getElementById("bwResetOk");
    const btn = document.getElementById("bwResetBtn");
    const newEl = document.getElementById("bwNewPw");
    const confirmEl = document.getElementById("bwConfirmPw");
    const strengthEl = document.getElementById("bwStrength");
    newEl.focus();

    newEl.addEventListener("input", () => {
      const pw = newEl.value || "";
      if (!pw) { strengthEl.textContent = ""; strengthEl.className = "prv-strength"; return; }
      const s = scorePassword(pw);
      strengthEl.textContent = s.label;
      strengthEl.className = "prv-strength " + s.cls;
    });

    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      showError(errBox, "");
      okBox.hidden = true;
      const pw = newEl.value || "";
      const confirm = confirmEl.value || "";
      if (pw.length < MIN_PW_LEN) { showError(errBox, "Your new password must be at least " + MIN_PW_LEN + " characters."); newEl.focus(); return; }
      if (pw !== confirm) { showError(errBox, "The two passwords don't match."); confirmEl.focus(); return; }
      btn.disabled = true;
      btn.textContent = "Saving…";
      try {
        await apiPost("/buyer/password", { current_password: "", new_password: pw });
        okBox.textContent = "Password updated. Opening your workspace…";
        okBox.hidden = false;
        if (currentUser) currentUser.must_reset_password = false;
        setTimeout(() => renderWorkspace(), 700);
      } catch (e) {
        if (e instanceof AuthError) { bounceToLogin(e.message); return; }
        showError(errBox, e.message || "Could not update your password.");
        btn.disabled = false;
        btn.textContent = "Save password & continue";
      }
    });
  }

  // ══════════════════════════════════════════════════════════
  //  SCREEN 3 — WORKSPACE (deliveries)
  // ══════════════════════════════════════════════════════════
  function renderHeader() {
    clear(userBadge);
    const email = document.createElement("span");
    email.className = "asc-user-email";
    email.textContent = (currentUser && currentUser.email) || "";
    const role = document.createElement("span");
    role.className = "asc-user-role";
    role.textContent = (currentUser && currentUser.buyer_name) || "Data buyer";
    userBadge.appendChild(email);
    userBadge.appendChild(role);
    header.hidden = false;
  }

  function renderWorkspace() {
    renderHeader();
    mountTemplate("tplWorkspace");
    const introSub = document.getElementById("bwIntroSub");
    if (currentUser && currentUser.email) {
      introSub.textContent =
        "Signed in as " + currentUser.email +
        ". Every dataset delivered to you by Archangel Health is available below.";
    }
    document.getElementById("bwRefreshBtn").addEventListener("click", () => loadDeliveries());
    loadDeliveries();
  }

  async function loadDeliveries() {
    const body = document.getElementById("bwDeliveriesBody");
    const empty = document.getElementById("bwDeliveriesEmpty");
    if (!body) return;
    try {
      const data = await apiGet("/buyer/deliveries");
      const deliveries = (data && Array.isArray(data.deliveries)) ? data.deliveries : [];
      clear(body);
      if (!deliveries.length) { empty.hidden = false; return; }
      empty.hidden = true;
      deliveries.forEach((d) => body.appendChild(deliveryRow(d)));
    } catch (e) {
      if (e instanceof AuthError) { bounceToLogin(e.message); return; }
      toast(e.message || "Could not load your datasets.", "error");
    }
  }

  function deliveryRow(d) {
    const tr = document.createElement("tr");

    const tdWhen = document.createElement("td");
    tdWhen.textContent = formatWhen(d.sent_at);
    tr.appendChild(tdWhen);

    const tdLabel = document.createElement("td");
    tdLabel.textContent = d.label || d.export_id || "Dataset";
    tr.appendChild(tdLabel);

    const tdFmt = document.createElement("td");
    tdFmt.textContent = d.data_format || "jsonl";
    tr.appendChild(tdFmt);

    const tdRecords = document.createElement("td");
    tdRecords.textContent = (d.record_count != null) ? String(d.record_count) : "—";
    tr.appendChild(tdRecords);

    const tdBtn = document.createElement("td");
    const btn = document.createElement("button");
    btn.className = "asc-btn asc-btn-primary asc-btn-sm";
    btn.textContent = "⬇ Download";
    btn.addEventListener("click", () => downloadDelivery(d.export_id, btn));
    tdBtn.appendChild(btn);
    tr.appendChild(tdBtn);

    return tr;
  }

  async function downloadDelivery(exportId, btn) {
    if (btn) { btn.disabled = true; btn.textContent = "Preparing…"; }
    try {
      const res = await fetch(API_BASE + "/buyer/deliveries/" + encodeURIComponent(exportId) + "/download",
        { headers: authHeaders() });
      if (res.status === 401 || res.status === 403) { bounceToLogin("Your session has ended. Please sign in again."); return; }
      if (!res.ok) { toast("Download failed (" + res.status + ").", "error"); return; }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = exportId + ".zip";
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1500);
    } catch (e) {
      toast("Download failed: " + (e.message || ""), "error");
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = "⬇ Download"; }
    }
  }

  // ══════════════════════════════════════════════════════════
  //  ROUTING
  // ══════════════════════════════════════════════════════════
  async function loadProfileAndRoute() {
    try {
      const me = await apiGet("/buyer/me");
      currentUser = me;
      if (me && me.must_reset_password === true) renderReset();
      else renderWorkspace();
    } catch (e) {
      if (e instanceof AuthError) { bounceToLogin(e.message); return; }
      bounceToLogin(e.message || "Could not load your account. Please sign in again.");
    }
  }

  logoutBtn.addEventListener("click", () => {
    clearToken();
    currentUser = null;
    header.hidden = true;
    renderLogin();
    toast("Signed out.", "info");
  });

  function boot() {
    if (getToken()) loadProfileAndRoute();
    else renderLogin();
  }

  boot();
})();
