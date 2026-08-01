/* ═══════════════════════════════════════════════════════════
   Health System Upload Portal — provider.js
   Vanilla JS. No frameworks, no build step. Single-page, JS-driven
   state machine with three screens:
     1) login            → POST /hs/login (username + password)
     2) reset (forced)   → shown when me.must_reset === true
     3) upload           → the only screen once authed + reset done

   Security posture:
   - Session lives in an HttpOnly cookie set by the server; this script
     never sees or stores a credential. Same-origin requests carry the
     cookie automatically (fetch + XHR both).
   - Every dynamic, server-provided string is written with textContent
     (never innerHTML) to prevent injection.
   - A mid-session 401/403 bounces to login (session expired/revoked).
   ═══════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  const API_BASE = "/api/asclepius";
  const MIN_PW_LEN = 12;

  // ─── DOM roots ──────────────────────────────────────────────
  const root = document.getElementById("prvRoot");
  const header = document.getElementById("prvHeader");
  const userBadge = document.getElementById("prvUserBadge");
  const logoutBtn = document.getElementById("prvLogoutBtn");
  const toastRegion = document.getElementById("prvToasts");

  // In-memory copy of the current portal profile (from /hs/me).
  let currentUser = null;

  // Thrown for 401/403 so callers can trigger a bounce to login.
  class AuthError extends Error {}

  async function parseJson(res) {
    return res.json().catch(() => ({}));
  }

  // GET/POST JSON. Throws AuthError on 401/403, Error otherwise.
  async function apiJson(method, path, body) {
    const opts = { method, headers: { Accept: "application/json" }, credentials: "same-origin" };
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

  // A recoverable auth failure: drop chrome, go to login.
  function bounceToLogin(msg) {
    currentUser = null;
    header.hidden = true;
    renderLogin();
    if (msg) toast(msg, "error");
  }

  // ─── Status vocabulary (four plain-language states) ────────
  const STATUS_META = {
    received: {
      label: "Received", badge: "asc-badge-gray", icon: "·",
      help: "We've got your upload and queued it for processing."
    },
    processing: {
      label: "Processing", badge: "asc-badge-primary", icon: "…",
      help: "We're reading and understanding the contents."
    },
    accepted: {
      label: "Accepted", badge: "asc-badge-green", icon: "✓",
      help: "Successfully received and accepted. Nothing more needed from you."
    },
    needs_attention: {
      label: "Needs attention", badge: "asc-badge-amber", icon: "!",
      help: "Our team is taking a closer look. We'll reach out if anything is needed."
    }
  };

  function statusMeta(status) {
    return STATUS_META[status] || STATUS_META.needs_attention;
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
    const usernameEl = document.getElementById("prvUsername");
    const pwEl = document.getElementById("prvPassword");
    usernameEl.focus();

    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const username = (usernameEl.value || "").trim();
      const password = pwEl.value || "";
      showError(errBox, "");
      if (!username || !password) {
        showError(errBox, "Enter your username and password.");
        return;
      }
      btn.disabled = true;
      btn.textContent = "Signing in…";
      try {
        const data = await apiPost("/hs/login", { username, password });
        currentUser = {
          username: data.username,
          organization: data.organization,
          must_reset: data.must_reset === true
        };
        if (currentUser.must_reset) renderReset();
        else renderUpload();
      } catch (e) {
        // Login failures (incl. 401/429) surface inline rather than bouncing.
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
        // The temporary password was consumed at login; on the forced reset the
        // session itself is proof of identity, so no current password is sent.
        await apiPost("/hs/password", { new_password: pw });
        okBox.textContent = "Password updated. Taking you to your uploads…";
        okBox.hidden = false;
        if (currentUser) currentUser.must_reset = false;
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
    const org = document.createElement("span");
    org.className = "asc-user-email";
    org.textContent = (currentUser && currentUser.organization) || "";
    const user = document.createElement("span");
    user.className = "asc-user-role";
    user.textContent = (currentUser && currentUser.username) || "Health system";
    userBadge.appendChild(org);
    userBadge.appendChild(user);
    header.hidden = false;
  }

  function renderUpload() {
    renderHeader();
    mountTemplate("tplUpload");

    const drop = document.getElementById("prvDrop");
    const fileInput = document.getElementById("prvFileInput");
    const refreshBtn = document.getElementById("prvRefreshBtn");
    const introSub = document.getElementById("prvIntroSub");

    if (currentUser && currentUser.organization) {
      introSub.textContent =
        "Signed in for " + currentUser.organization +
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
  // Same-origin XHR carries the session cookie automatically.
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
    xhr.open("POST", API_BASE + "/hs/uploads");
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

      if (xhr.status === 401) {
        bounceToLogin(data.detail || "Your session has ended. Please sign in again.");
        return;
      }
      if (xhr.status === 403) {
        // must_reset re-imposed mid-session (e.g. credentials rotated).
        toast(data.detail || "Please reset your password before uploading.", "error");
        loadProfileAndRoute();
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

      renderBatchResult(results, files, data);
      toast("Upload received.", "success");
      loadHistory();
    });

    xhr.addEventListener("error", () => {
      progress.hidden = true;
      toast("Network error during upload. Please try again.", "error");
    });

    xhr.send(form);
  }

  // Render a result line for the just-completed batch.
  function renderBatchResult(container, files, data) {
    const meta = statusMeta((data && data.status) || "received");
    const li = document.createElement("li");
    li.className = "prv-result";
    const icon = document.createElement("span");
    icon.className = "prv-result-icon";
    icon.textContent = meta.icon;
    const main = document.createElement("div");
    main.className = "prv-result-main";
    const name = document.createElement("div");
    name.className = "prv-result-name";
    name.textContent = files.length === 1
      ? (files[0].name || "your file")
      : files.length + " files";
    const msg = document.createElement("div");
    msg.className = "prv-result-msg";
    msg.textContent = meta.help;
    main.appendChild(name);
    main.appendChild(msg);
    li.appendChild(icon);
    li.appendChild(main);
    li.appendChild(makeBadge((data && data.status) || "received"));
    container.insertBefore(li, container.firstChild);
  }

  // Load & render this health system's upload history.
  async function loadHistory() {
    const body = document.getElementById("prvHistoryBody");
    const empty = document.getElementById("prvHistoryEmpty");
    if (!body) return;
    try {
      const data = await apiGet("/hs/uploads");
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

    // Filename (+ count when a batch)
    const tdFile = document.createElement("td");
    tdFile.className = "prv-hist-files";
    let fileText = u.filename || "—";
    if (u.file_count > 1) fileText += " (" + u.file_count + " files)";
    tdFile.textContent = fileText;
    tr.appendChild(tdFile);

    // Size
    const tdSize = document.createElement("td");
    tdSize.textContent = formatBytes(u.total_bytes);
    tr.appendChild(tdSize);

    // Status badge
    const tdStatus = document.createElement("td");
    tdStatus.appendChild(makeBadge(u.status));
    tr.appendChild(tdStatus);

    // Details: server-provided plain-language detail, else our help text.
    const tdDetail = document.createElement("td");
    tdDetail.className = "prv-hist-detail";
    tdDetail.textContent = u.detail || statusMeta(u.status).help;
    tr.appendChild(tdDetail);

    return tr;
  }

  // ══════════════════════════════════════════════════════════
  //  ROUTING
  // ══════════════════════════════════════════════════════════
  async function loadProfileAndRoute() {
    try {
      const me = await apiGet("/hs/me");
      currentUser = me;
      if (me && me.must_reset === true) {
        renderReset();
      } else {
        renderUpload();
      }
    } catch (e) {
      if (e instanceof AuthError) { renderLogin(); return; }
      // Non-auth error fetching profile: show login with a note.
      bounceToLogin(e.message || "Could not load your account. Please sign in again.");
    }
  }

  // ─── Global chrome events ───────────────────────────────────
  logoutBtn.addEventListener("click", async () => {
    try { await apiPost("/hs/logout", {}); } catch (_) { /* cookie clears anyway */ }
    currentUser = null;
    header.hidden = true;
    renderLogin();
    toast("Signed out.", "info");
  });

  // ─── Boot ───────────────────────────────────────────────────
  // The session cookie is HttpOnly (invisible to JS), so probe /hs/me: a 401
  // lands on login, anything else routes to the right screen.
  loadProfileAndRoute();
})();
