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

  // Above this, a single request cannot be relied on: the platform closes a
  // request body that has not finished uploading within five minutes, and on a
  // hospital link that is a matter of tens of megabytes, not gigabytes. Larger
  // single files go through the resumable chunked handshake, where the five
  // minutes applies per chunk and stops mattering.
  const CHUNKED_MIN_BYTES = 8 * 1024 * 1024;
  const HASH_SLICE = 8 * 1024 * 1024;

  // ══════════════════════════════════════════════════════════
  //  SHA-256, streaming
  //
  //  crypto.subtle.digest cannot do this job: it takes one buffer, so hashing a
  //  3 GB selection would mean holding 3 GB in the tab. This reads the file in
  //  slices and keeps only the 32-byte state, which is what makes declaring the
  //  whole-file digest up front possible at all. Per-CHUNK digests still use
  //  crypto.subtle — those are bounded and it is far faster.
  // ══════════════════════════════════════════════════════════
  const K256 = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
    0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
    0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
    0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
    0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
    0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
    0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2
  ]);

  function Sha256() {
    this.h = new Uint32Array([
      0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
      0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19
    ]);
    this.buf = new Uint8Array(64);
    this.buflen = 0;
    this.total = 0;
    this.w = new Uint32Array(64);
  }

  Sha256.prototype._block = function (b, off) {
    const w = this.w, h = this.h;
    for (let i = 0; i < 16; i++) {
      w[i] = (b[off + i * 4] << 24) | (b[off + i * 4 + 1] << 16) |
             (b[off + i * 4 + 2] << 8) | b[off + i * 4 + 3];
    }
    for (let i = 16; i < 64; i++) {
      const x = w[i - 15], y = w[i - 2];
      const s0 = ((x >>> 7) | (x << 25)) ^ ((x >>> 18) | (x << 14)) ^ (x >>> 3);
      const s1 = ((y >>> 17) | (y << 15)) ^ ((y >>> 19) | (y << 13)) ^ (y >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) | 0;
    }
    let a = h[0], bb = h[1], c = h[2], d = h[3];
    let e = h[4], f = h[5], g = h[6], hh = h[7];
    for (let i = 0; i < 64; i++) {
      const S1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7));
      const ch = (e & f) ^ (~e & g);
      const t1 = (hh + S1 + ch + K256[i] + w[i]) | 0;
      const S0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10));
      const maj = (a & bb) ^ (a & c) ^ (bb & c);
      const t2 = (S0 + maj) | 0;
      hh = g; g = f; f = e; e = (d + t1) | 0;
      d = c; c = bb; bb = a; a = (t1 + t2) | 0;
    }
    h[0] = (h[0] + a) | 0; h[1] = (h[1] + bb) | 0;
    h[2] = (h[2] + c) | 0; h[3] = (h[3] + d) | 0;
    h[4] = (h[4] + e) | 0; h[5] = (h[5] + f) | 0;
    h[6] = (h[6] + g) | 0; h[7] = (h[7] + hh) | 0;
  };

  Sha256.prototype.update = function (bytes) {
    this.total += bytes.length;
    let i = 0;
    if (this.buflen) {
      const need = Math.min(64 - this.buflen, bytes.length);
      this.buf.set(bytes.subarray(0, need), this.buflen);
      this.buflen += need;
      i = need;
      if (this.buflen === 64) { this._block(this.buf, 0); this.buflen = 0; }
    }
    for (; i + 64 <= bytes.length; i += 64) this._block(bytes, i);
    if (i < bytes.length) {
      this.buf.set(bytes.subarray(i), 0);
      this.buflen = bytes.length - i;
    }
  };

  Sha256.prototype.hex = function () {
    const bitLen = this.total * 8;
    const pad = new Uint8Array(this.buflen < 56 ? 64 : 128);
    pad.set(this.buf.subarray(0, this.buflen), 0);
    pad[this.buflen] = 0x80;
    // Length as a 64-bit big-endian count of BITS. Written as a float-safe high
    // and low word: a >>> 32 in JS is a no-op, so the obvious shift is wrong for
    // anything over 512 MB — exactly the sizes this exists for.
    const hi = Math.floor(bitLen / 0x100000000);
    const lo = bitLen >>> 0;
    const dv = new DataView(pad.buffer);
    dv.setUint32(pad.length - 8, hi);
    dv.setUint32(pad.length - 4, lo);
    for (let i = 0; i < pad.length; i += 64) this._block(pad, i);
    let out = "";
    for (let i = 0; i < 8; i++) out += ("00000000" + (this.h[i] >>> 0).toString(16)).slice(-8);
    return out;
  };

  async function sha256File(file, onProgress) {
    const hasher = new Sha256();
    for (let off = 0; off < file.size; off += HASH_SLICE) {
      const slice = file.slice(off, Math.min(off + HASH_SLICE, file.size));
      hasher.update(new Uint8Array(await slice.arrayBuffer()));
      if (onProgress) onProgress(Math.min(off + HASH_SLICE, file.size), file.size);
    }
    return hasher.hex();
  }

  async function sha256Bytes(buf) {
    if (window.crypto && window.crypto.subtle) {
      const d = await window.crypto.subtle.digest("SHA-256", buf);
      return Array.from(new Uint8Array(d))
        .map((b) => ("0" + b.toString(16)).slice(-2)).join("");
    }
    const hasher = new Sha256();
    hasher.update(new Uint8Array(buf));
    return hasher.hex();
  }

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

  // ══════════════════════════════════════════════════════════
  //  Chunked, resumable upload
  //
  //  Used for a single large file. A batch of small loose files still goes
  //  through the one-request door, which packages them server-side — the
  //  chunked door streams exactly one thing, and asking a hospital to zip five
  //  files themselves so we can avoid writing this branch would be solving our
  //  problem with their time.
  // ══════════════════════════════════════════════════════════
  function progressUi() {
    return {
      wrap: document.getElementById("prvProgress"),
      label: document.getElementById("prvProgressLabel"),
      bar: document.getElementById("prvProgressBar"),
      fill: document.getElementById("prvProgressFill")
    };
  }

  function setProgress(ui, pct, label) {
    ui.wrap.hidden = false;
    ui.fill.style.width = pct + "%";
    ui.bar.setAttribute("aria-valuenow", String(Math.round(pct)));
    if (label) ui.label.textContent = label;
  }

  async function uploadChunked(file) {
    const ui = progressUi();
    const results = document.getElementById("prvResults");

    // Hashing happens before anything is sent, so it gets its own phase in the
    // progress copy — a silent minute on a multi-GB file reads as a hang.
    setProgress(ui, 0, "Checking " + file.name + "…");
    const digest = await sha256File(file, (done, total) =>
      setProgress(ui, (done / total) * 100,
        "Checking " + file.name + " — " + Math.round((done / total) * 100) + "%"));

    let session = await apiPost("/hs/uploads/sessions", {
      filename: file.name, size: file.size, sha256: digest,
      content_type: file.type || "application/octet-stream"
    });

    // Already complete: the same bytes were sent before. Nothing to re-send.
    if (session.complete) {
      ui.wrap.hidden = true;
      renderBatchResult(results, [file], { status: "received" });
      toast("This file was already received.", "success");
      loadHistory();
      return;
    }

    const chunk = session.chunk_size;
    const done = new Set(session.received_parts || []);
    if (done.size) {
      toast("Resuming — " + done.size + " of " + session.part_count +
            " parts already received.", "info");
    }

    for (let n = 1; n <= session.part_count; n++) {
      if (done.has(n)) continue;
      const start = (n - 1) * chunk;
      const blob = file.slice(start, Math.min(start + chunk, file.size));
      const buf = await blob.arrayBuffer();
      const partSha = await sha256Bytes(buf);
      const res = await fetch(
        API_BASE + "/hs/uploads/sessions/" + encodeURIComponent(session.session_id) +
        "/parts/" + n,
        { method: "PUT", credentials: "same-origin", body: buf,
          headers: { "X-Chunk-SHA256": partSha, "Content-Type": "application/octet-stream" } });
      if (res.status === 401 || res.status === 403) {
        throw new AuthError("Your session has ended.");
      }
      if (!res.ok) {
        const d = await parseJson(res);
        throw new Error(d.detail || ("Part " + n + " failed (" + res.status + ")."));
      }
      setProgress(ui, ((n / session.part_count) * 100),
        "Uploading " + file.name + " — part " + n + " of " + session.part_count);
    }

    setProgress(ui, 100, "Finishing up…");
    const out = await apiPost(
      "/hs/uploads/sessions/" + encodeURIComponent(session.session_id) + "/complete", {});
    ui.wrap.hidden = true;
    renderBatchResult(results, [file], out);
    toast("Upload received.", "success");
    loadHistory();
  }

  // Upload a batch via XHR (for real upload progress), field name `files`.
  // Same-origin XHR carries the session cookie automatically.
  function uploadFiles(fileList) {
    const files = Array.prototype.slice.call(fileList);
    if (!files.length) return;

    // One large file → the resumable path. Anything else stays on the
    // single-request door, which is proven and packages loose files for us.
    if (files.length === 1 && files[0].size >= CHUNKED_MIN_BYTES) {
      uploadChunked(files[0]).catch((e) => {
        const ui = progressUi();
        ui.wrap.hidden = true;
        if (e instanceof AuthError) { bounceToLogin(e.message); return; }
        toast(e.message || "Upload failed. Your progress was saved — " +
              "drop the same file again to resume.", "error");
      });
      return;
    }

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
