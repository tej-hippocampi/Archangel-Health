/* ═══════════════════════════════════════════════════════════
   Data Provider Portal — provider.js
   Vanilla JS. No frameworks, no build step. Single-page, JS-driven
   state machine with three screens:
     1) login            → POST /auth/login, store token, GET /provider/me
     2) reset (forced)    → shown when me.must_reset_password === true
     3) upload            → the only screen once authed + reset done

   Security posture:
   - Bearer token in localStorage['asclepius_provider_token'].
   - Every dynamic, server-provided string is written with textContent
     (never innerHTML) to prevent injection.
   - A mid-session 401/403 clears the token and bounces to login
     (the account may have been revoked).
   ═══════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  const API_BASE = "/api/asclepius";
  const TOKEN_KEY = "asclepius_provider_token";
  const MIN_PW_LEN = 12;

  // ─── DOM roots ──────────────────────────────────────────────
  const root = document.getElementById("prvRoot");
  const header = document.getElementById("prvHeader");
  const userBadge = document.getElementById("prvUserBadge");
  const logoutBtn = document.getElementById("prvLogoutBtn");
  const toastRegion = document.getElementById("prvToasts");

  // In-memory copy of the current provider profile (from /provider/me).
  let currentUser = null;

  // ─── Token helpers ──────────────────────────────────────────
  const getToken = () => localStorage.getItem(TOKEN_KEY) || "";
  const setToken = (t) => localStorage.setItem(TOKEN_KEY, t);
  const clearToken = () => localStorage.removeItem(TOKEN_KEY);

  // Thrown for 401/403 so callers can trigger a bounce to login.
  class AuthError extends Error {}

  // ─── Fetch helpers ──────────────────────────────────────────
  function authHeaders(extra) {
    const h = Object.assign({ Accept: "application/json" }, extra || {});
    const tok = getToken();
    if (tok) h.Authorization = "Bearer " + tok;
    return h;
  }

  async function parseJson(res) {
    return res.json().catch(() => ({}));
  }

  // GET/POST JSON. Throws AuthError on 401/403, Error otherwise.
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
    if (!res.ok) {
      throw new Error(data.detail || ("Request failed (" + res.status + ")"));
    }
    return data;
  }

  const apiGet = (path) => apiJson("GET", path);
  const apiPost = (path, body) => apiJson("POST", path, body);

  // ─── UI utilities ───────────────────────────────────────────
  function clear(el) {
    while (el.firstChild) el.removeChild(el.firstChild);
  }

  function mountTemplate(id) {
    const tpl = document.getElementById(id);
    clear(root);
    root.appendChild(tpl.content.cloneNode(true));
  }

  function showError(el, msg) {
    if (!el) return;
    el.textContent = msg || "";
    el.hidden = !msg;
  }

  function toast(msg, kind) {
    const t = document.createElement("div");
    t.className = "asc-toast" + (kind ? " " + kind : "");
    t.textContent = msg; // textContent — never innerHTML.
    toastRegion.appendChild(t);
    setTimeout(() => {
      t.style.transition = "opacity .3s";
      t.style.opacity = "0";
      setTimeout(() => t.remove(), 320);
    }, 4200);
  }

  // A recoverable auth failure: wipe token, drop chrome, go to login.
  function bounceToLogin(msg) {
    clearToken();
    currentUser = null;
    header.hidden = true;
    renderLogin();
    if (msg) toast(msg, "error");
  }

  // ─── Status vocabulary (plain-English rendering) ───────────
  // Maps API upload statuses to a human label, badge class, and icon.
  const STATUS_META = {
    received:     { label: "Received",        badge: "asc-badge-gray",    icon: "·",
                    help: "We've got your file and queued it for processing." },
    parsing:      { label: "Reading",         badge: "asc-badge-primary", icon: "…",
                    help: "We're reading and understanding the contents." },
    needs_review: { label: "Needs review",    badge: "asc-badge-amber",   icon: "!",
                    help: "A team member will take a closer look before ingesting." },
    ingested:     { label: "Accepted",        badge: "asc-badge-green",   icon: "✓",
                    help: "Successfully ingested. Nothing more needed from you." },
    quarantined:  { label: "Held for safety", badge: "asc-badge-amber",   icon: "!",
                    help: "Set aside pending a safety check." },
    failed:       { label: "Could not process", badge: "asc-badge-red",   icon: "✕",
                    help: "We were unable to process this. See the reason below." },
    // per-file outcomes emitted by the ingestion pipeline
    parsed:       { label: "Read",             badge: "asc-badge-green",   icon: "✓",
                    help: "Read successfully and folded into your case." },
    rejected:     { label: "Not accepted",     badge: "asc-badge-red",     icon: "✕",
                    help: "This file type isn't accepted (e.g. an executable or script)." },
    excluded:     { label: "Excluded",         badge: "asc-badge-gray",    icon: "—",
                    help: "Imaging can't be graded and was left out — the rest of your bundle is unaffected." }
  };

  function statusMeta(status) {
    return STATUS_META[status] || {
      label: status || "Unknown",
      badge: "asc-badge-gray",
      icon: "•",
      help: ""
    };
  }

  function makeBadge(status) {
    const meta = statusMeta(status);
    const span = document.createElement("span");
    span.className = "asc-badge " + meta.badge;
    span.textContent = meta.label;
    return span;
  }

  function formatBytes(n) {
    if (!n && n !== 0) return "";
    if (n < 1024) return n + " B";
    const units = ["KB", "MB", "GB", "TB"];
    let v = n / 1024, i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return v.toFixed(v >= 10 ? 0 : 1) + " " + units[i];
  }

  function formatWhen(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    return d.toLocaleString(undefined, {
      year: "numeric", month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit"
    });
  }

  // ══════════════════════════════════════════════════════════
  //  SCREEN 1 — LOGIN
  // ══════════════════════════════════════════════════════════
  function renderLogin() {
    header.hidden = true;
    mountTemplate("tplLogin");
    const form = document.getElementById("prvLoginForm");
    const errBox = document.getElementById("prvLoginError");
    const btn = document.getElementById("prvLoginBtn");
    const emailEl = document.getElementById("prvEmail");
    const pwEl = document.getElementById("prvPassword");
    emailEl.focus();

    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const email = (emailEl.value || "").trim();
      const password = pwEl.value || "";
      showError(errBox, "");
      if (!email || !password) {
        showError(errBox, "Enter your email and password.");
        return;
      }
      btn.disabled = true;
      btn.textContent = "Signing in…";
      try {
        const data = await apiPost("/auth/login", { email, password });
        if (!data.token) throw new Error("No token returned by the server.");
        setToken(data.token);
        await loadProfileAndRoute();
      } catch (e) {
        // Login failures (incl. 401) surface inline rather than bouncing.
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
    // Lightweight client-side hint only (not a security control).
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
    const form = document.getElementById("prvResetForm");
    const errBox = document.getElementById("prvResetError");
    const okBox = document.getElementById("prvResetOk");
    const btn = document.getElementById("prvResetBtn");
    const newEl = document.getElementById("prvNewPw");
    const confirmEl = document.getElementById("prvConfirmPw");
    const strengthEl = document.getElementById("prvStrength");
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
      if (pw.length < MIN_PW_LEN) {
        showError(errBox, "Your new password must be at least " + MIN_PW_LEN + " characters.");
        newEl.focus();
        return;
      }
      if (pw !== confirm) {
        showError(errBox, "The two passwords don't match.");
        confirmEl.focus();
        return;
      }
      btn.disabled = true;
      btn.textContent = "Saving…";
      try {
        // The temporary password was used at login; the contract's
        // current_password field is not re-collected here, so we send the
        // new password and (best-effort) the value still in the login field.
        await apiPost("/provider/password", {
          current_password: "",
          new_password: pw
        });
        okBox.textContent = "Password updated. Taking you to your uploads…";
        okBox.hidden = false;
        if (currentUser) currentUser.must_reset_password = false;
        setTimeout(() => renderUpload(), 700);
      } catch (e) {
        if (e instanceof AuthError) { bounceToLogin(e.message); return; }
        showError(errBox, e.message || "Could not update your password.");
        btn.disabled = false;
        btn.textContent = "Save password & continue";
      }
    });
  }

  // ══════════════════════════════════════════════════════════
  //  SCREEN 3 — UPLOAD (main / only screen)
  // ══════════════════════════════════════════════════════════
  function renderHeader() {
    clear(userBadge);
    const email = document.createElement("span");
    email.className = "asc-user-email";
    email.textContent = (currentUser && currentUser.email) || "";
    const role = document.createElement("span");
    role.className = "asc-user-role";
    role.textContent = (currentUser && currentUser.org_name) || "Data provider";
    userBadge.appendChild(email);
    userBadge.appendChild(role);
    header.hidden = false;
  }

  function renderUpload() {
    renderHeader();
    mountTemplate("tplUpload");

    const drop = document.getElementById("prvDrop");
    const fileInput = document.getElementById("prvFileInput");
    const refreshBtn = document.getElementById("prvRefreshBtn");
    const introSub = document.getElementById("prvIntroSub");

    if (currentUser && currentUser.email) {
      introSub.textContent =
        "Signed in as " + currentUser.email +
        ". Drop your files below — everything is transmitted over an encrypted " +
        "connection and only ever visible to you and our ingestion team.";
    }

    // Open the native picker from the drop-zone button.
    drop.addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", () => {
      if (fileInput.files && fileInput.files.length) {
        uploadFiles(fileInput.files);
        fileInput.value = ""; // allow re-selecting the same file
      }
    });

    // Drag & drop wiring with visual affordance.
    ["dragenter", "dragover"].forEach((evt) =>
      drop.addEventListener(evt, (e) => {
        e.preventDefault();
        e.stopPropagation();
        drop.classList.add("is-dragover");
      })
    );
    ["dragleave", "dragend", "drop"].forEach((evt) =>
      drop.addEventListener(evt, (e) => {
        e.preventDefault();
        e.stopPropagation();
        drop.classList.remove("is-dragover");
      })
    );
    drop.addEventListener("drop", (e) => {
      const dt = e.dataTransfer;
      if (dt && dt.files && dt.files.length) uploadFiles(dt.files);
    });

    refreshBtn.addEventListener("click", () => loadHistory());

    loadHistory();
  }

  // Upload a batch via XHR (for real upload progress), field name `files`.
  function uploadFiles(fileList) {
    const files = Array.prototype.slice.call(fileList);
    if (!files.length) return;

    const progress = document.getElementById("prvProgress");
    const progressLabel = document.getElementById("prvProgressLabel");
    const progressBar = document.getElementById("prvProgressBar");
    const progressFill = document.getElementById("prvProgressFill");
    const results = document.getElementById("prvResults");

    const form = new FormData();
    files.forEach((f) => form.append("files", f, f.name));

    progress.hidden = false;
    progressLabel.textContent =
      "Uploading " + files.length + " file" + (files.length === 1 ? "" : "s") + "…";
    progressFill.style.width = "0%";
    progressBar.setAttribute("aria-valuenow", "0");

    const xhr = new XMLHttpRequest();
    xhr.open("POST", API_BASE + "/provider/uploads");
    const tok = getToken();
    if (tok) xhr.setRequestHeader("Authorization", "Bearer " + tok);
    xhr.setRequestHeader("Accept", "application/json");

    xhr.upload.addEventListener("progress", (e) => {
      if (!e.lengthComputable) return;
      const pct = Math.round((e.loaded / e.total) * 100);
      progressFill.style.width = pct + "%";
      progressBar.setAttribute("aria-valuenow", String(pct));
      if (pct >= 100) progressLabel.textContent = "Processing your files…";
    });

    xhr.addEventListener("load", () => {
      progress.hidden = true;
      let data = {};
      try { data = JSON.parse(xhr.responseText || "{}"); } catch (_) { /* ignore */ }

      if (xhr.status === 401 || xhr.status === 403) {
        bounceToLogin(data.detail || "Your session has ended. Please sign in again.");
        return;
      }
      if (xhr.status === 413) {
        toast(data.detail || "That upload is too large. Please split it into smaller files.", "error");
        return;
      }
      if (xhr.status < 200 || xhr.status >= 300) {
        toast(data.detail || ("Upload failed (" + xhr.status + ")."), "error");
        return;
      }

      renderBatchResults(results, data);
      toast("Upload received.", "success");
      loadHistory();
    });

    xhr.addEventListener("error", () => {
      progress.hidden = true;
      toast("Network error during upload. Please try again.", "error");
    });

    xhr.send(form);
  }

  // Render the per-file result list for the just-completed batch.
  function renderBatchResults(container, data) {
    const fileResults = (data && Array.isArray(data.files)) ? data.files : [];
    if (!fileResults.length) {
      // Fall back to the batch-level status if no per-file detail returned.
      const meta = statusMeta(data && data.status);
      const li = document.createElement("li");
      li.className = "prv-result";
      const icon = document.createElement("span");
      icon.className = "prv-result-icon";
      icon.textContent = meta.icon;
      const main = document.createElement("div");
      main.className = "prv-result-main";
      const name = document.createElement("div");
      name.className = "prv-result-name";
      name.textContent = "Batch " + ((data && data.upload_id) || "");
      const msg = document.createElement("div");
      msg.className = "prv-result-msg";
      msg.textContent = meta.help;
      main.appendChild(name);
      main.appendChild(msg);
      li.appendChild(icon);
      li.appendChild(main);
      li.appendChild(makeBadge(data && data.status));
      container.insertBefore(li, container.firstChild);
      return;
    }

    // Newest batch on top; iterate in reverse so display order is preserved.
    for (let i = fileResults.length - 1; i >= 0; i--) {
      const fr = fileResults[i];
      const meta = statusMeta(fr.status);
      const li = document.createElement("li");
      li.className = "prv-result";

      const icon = document.createElement("span");
      icon.className = "prv-result-icon";
      icon.textContent = meta.icon;

      const main = document.createElement("div");
      main.className = "prv-result-main";

      const name = document.createElement("div");
      name.className = "prv-result-name";
      name.textContent = fr.filename || "(unnamed file)";
      main.appendChild(name);

      if (fr.detected_type) {
        const type = document.createElement("div");
        type.className = "prv-result-type";
        type.textContent = "Detected: " + fr.detected_type;
        main.appendChild(type);
      }

      const msg = document.createElement("div");
      msg.className = "prv-result-msg";
      // Prefer server-provided plain-English outcome, else our help text.
      msg.textContent = fr.outcome || meta.help;
      main.appendChild(msg);

      li.appendChild(icon);
      li.appendChild(main);
      li.appendChild(makeBadge(fr.status));
      container.insertBefore(li, container.firstChild);
    }
  }

  // Load & render the provider's own upload history.
  async function loadHistory() {
    const body = document.getElementById("prvHistoryBody");
    const empty = document.getElementById("prvHistoryEmpty");
    if (!body) return;
    try {
      const data = await apiGet("/provider/uploads");
      const uploads = (data && Array.isArray(data.uploads)) ? data.uploads : [];
      clear(body);
      if (!uploads.length) {
        empty.hidden = false;
        return;
      }
      empty.hidden = true;
      uploads.forEach((u) => body.appendChild(historyRow(u)));
    } catch (e) {
      if (e instanceof AuthError) { bounceToLogin(e.message); return; }
      toast(e.message || "Could not load your upload history.", "error");
    }
  }

  function historyRow(u) {
    const tr = document.createElement("tr");

    // Received
    const tdWhen = document.createElement("td");
    tdWhen.textContent = formatWhen(u.received_at);
    tr.appendChild(tdWhen);

    // Files (+ size)
    const tdFiles = document.createElement("td");
    tdFiles.className = "prv-hist-files";
    const count = (u.file_count != null) ? u.file_count : 0;
    let filesText = count + " file" + (count === 1 ? "" : "s");
    if (u.total_bytes != null) filesText += " · " + formatBytes(u.total_bytes);
    tdFiles.textContent = filesText;
    tr.appendChild(tdFiles);

    // Status badge
    const tdStatus = document.createElement("td");
    tdStatus.appendChild(makeBadge(u.status));
    tr.appendChild(tdStatus);

    // Details: failure reason when present, else per-file summary.
    const tdDetail = document.createElement("td");
    tdDetail.className = "prv-hist-detail";
    if (u.reason) {
      tdDetail.textContent = u.reason;
    } else if (Array.isArray(u.files) && u.files.length) {
      const named = u.files
        .map((f) => f && f.filename)
        .filter(Boolean)
        .slice(0, 3)
        .join(", ");
      const extra = u.files.length > 3 ? " +" + (u.files.length - 3) + " more" : "";
      tdDetail.textContent = named ? named + extra : statusMeta(u.status).help;
    } else {
      tdDetail.textContent = statusMeta(u.status).help;
    }
    tr.appendChild(tdDetail);

    return tr;
  }

  // ══════════════════════════════════════════════════════════
  //  ROUTING
  // ══════════════════════════════════════════════════════════
  async function loadProfileAndRoute() {
    try {
      const me = await apiGet("/provider/me");
      currentUser = me;
      if (me && me.must_reset_password === true) {
        renderReset();
      } else {
        renderUpload();
      }
    } catch (e) {
      if (e instanceof AuthError) { bounceToLogin(e.message); return; }
      // Non-auth error fetching profile: show login with a note.
      bounceToLogin(e.message || "Could not load your account. Please sign in again.");
    }
  }

  // ─── Global chrome events ───────────────────────────────────
  logoutBtn.addEventListener("click", () => {
    clearToken();
    currentUser = null;
    header.hidden = true;
    renderLogin();
    toast("Signed out.", "info");
  });

  // ─── Boot ───────────────────────────────────────────────────
  function boot() {
    if (getToken()) {
      loadProfileAndRoute();
    } else {
      renderLogin();
    }
  }

  boot();
})();
