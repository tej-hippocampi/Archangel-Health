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
    // A self-signup never chose its username; we derived it from the
    // organization name and said it once. Prefill so coming back to this form
    // is not a dead end.
    try {
      const remembered = localStorage.getItem("prv_last_username");
      if (remembered) usernameEl.value = remembered;
    } catch (_) {}
    const toSignup = document.getElementById("prvToSignup");
    if (toSignup) toSignup.addEventListener("click", renderSignup);
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
        try { localStorage.setItem("prv_last_username", data.username); } catch (_) {}
        // Through the router rather than straight to upload: the login response
        // does not carry surfaces, and a pending account must not open onto a
        // door it cannot use.
        await loadProfileAndRoute();
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
        okBox.textContent = "Password updated. One moment…";
        okBox.hidden = false;
        if (currentUser) currentUser.must_reset = false;
        // Route through the profile, never straight to a panel. This used to
        // call renderUpload() directly, which was right when the only account
        // that ever reached this screen was one an operator had already
        // approved. A self-serve organization arrives here on its FIRST sign-in
        // — before it has told us anything and before anyone has approved it —
        // and hard-routing it landed it on a locked upload screen with no rail
        // and no way out of it. loadProfileAndRoute is the one function that
        // knows where somebody belongs; there is no second copy of that answer.
        setTimeout(() => { loadProfileAndRoute(); }, 700);
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

    loadRequests();
    loadHistory();
  }

  // ─── What we have asked this organization for ───────────────
  //
  // Above the drop zone because it is the reason a partner is on this screen
  // when they are on it for a reason. Rendered only when something is open: a
  // permanent "no open requests" panel is a permanent reminder of an absence,
  // and this screen's job is to make sending data feel easy.
  //
  // A failure here is silent. The request list is a prompt, not a gate, and an
  // error banner above the upload control would read as "uploading is broken"
  // to a hospital IT contact who came here to send us a file.
  async function loadRequests() {
    const slot = document.getElementById("prvRequests");
    if (!slot) return;
    let data;
    try {
      data = await apiGet("/hs/requests");
    } catch (e) {
      // Including a 403, which `apiJson` reports as an AuthError. The upload
      // screen is only reached by an account that may upload, so a refusal here
      // means the ORGANIZATION's state changed under an open session -- and
      // bouncing that partner to a login form over a panel they never asked for
      // would read as being signed out for no reason.
      return;
    }
    const requests = (data && Array.isArray(data.requests)) ? data.requests : [];
    clear(slot);
    if (!requests.length) return;

    const card = document.createElement("section");
    card.className = "asc-card prv-requests";
    const head = document.createElement("div");
    head.className = "asc-card-head";
    const headInner = document.createElement("div");
    const title = document.createElement("div");
    title.className = "asc-card-title";
    title.textContent = requests.length === 1
      ? "We are looking for data"
      : "We are looking for data (" + requests.length + " requests)";
    const sub = document.createElement("div");
    sub.className = "asc-card-sub";
    // Straight from the server. The "several partners may answer" line is the
    // one thing every request has to say, and it should not be possible to ship
    // a client that forgets it.
    sub.textContent = data.how_it_works || "";
    headInner.appendChild(title);
    headInner.appendChild(sub);
    head.appendChild(headInner);
    card.appendChild(head);

    requests.forEach((r) => {
      const body = document.createElement("div");
      body.className = "asc-card-pad prv-request";
      const line = document.createElement("div");
      line.className = "prv-request-head";
      const name = document.createElement("strong");
      name.textContent = r.title || "";
      const meta = document.createElement("span");
      meta.className = "prv-request-meta";
      meta.textContent = [
        r.specialty,
        r.case_count + (r.case_count === 1 ? " case" : " cases"),
        r.due_date ? "useful by " + r.due_date : ""
      ].filter(Boolean).join(" · ");
      line.appendChild(name);
      line.appendChild(meta);
      body.appendChild(line);
      if (r.details) {
        const details = document.createElement("div");
        details.className = "prv-request-details";
        details.textContent = r.details;
        body.appendChild(details);
      }
      card.appendChild(body);
    });
    slot.appendChild(card);
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
    // The integrity receipt (PRD §6). The checksum is what turns "we got it"
    // into something the sender can check against their own copy, and a
    // hospital handing over years of records is entitled to that rather than
    // to a green tick. Truncated because 64 hex characters in a table cell is
    // unreadable; the full value is selectable in the title.
    if (u.sha256) {
      const integrity = document.createElement("div");
      integrity.className = "prv-hist-integrity";
      const digest = document.createElement("code");
      digest.className = "prv-mono";
      digest.textContent = "sha256 " + u.sha256.slice(0, 16) + "…";
      digest.title = u.sha256;
      integrity.appendChild(digest);
      if (u.verified_at) {
        const when = document.createElement("span");
        when.className = "prv-hist-verified";
        when.textContent = " verified " + formatWhen(u.verified_at);
        integrity.appendChild(when);
      }
      tdDetail.appendChild(integrity);
    }
    tr.appendChild(tdDetail);

    return tr;
  }

  // ══════════════════════════════════════════════════════════
  //  SCREEN 4 — SIGN UP
  // ══════════════════════════════════════════════════════════
  // The address a signup is waiting on, held only between the two screens.
  let pendingSignupEmail = "";

  function renderSignup() {
    header.hidden = true;
    hideRail();
    mountTemplate("tplSignup");
    const form = document.getElementById("prvSignupForm");
    const errBox = document.getElementById("prvSignupError");
    const btn = document.getElementById("prvSignupBtn");
    const nameEl = document.getElementById("prvSuName");
    const emailEl = document.getElementById("prvSuEmail");
    const orgEl = document.getElementById("prvSuOrg");
    const pwEl = document.getElementById("prvSuPw");
    const strength = document.getElementById("prvSuStrength");
    const hpEl = document.getElementById("prvSuHp");
    nameEl.focus();

    pwEl.addEventListener("input", () => {
      const s = scorePassword(pwEl.value || "");
      strength.className = "prv-strength " + s.cls;
      strength.textContent = pwEl.value ? s.label : "";
    });
    document.getElementById("prvToLogin").addEventListener("click", renderLogin);

    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      showError(errBox, "");
      const payload = {
        full_name: (nameEl.value || "").trim(),
        email: (emailEl.value || "").trim(),
        organization: (orgEl.value || "").trim(),
        password: pwEl.value || "",
        company_website: hpEl.value || ""
      };
      if (!payload.full_name || !payload.email || !payload.organization) {
        showError(errBox, "Fill in your name, work email and health system.");
        return;
      }
      if ((payload.password || "").length < MIN_PW_LEN) {
        showError(errBox, "Choose a password of at least " + MIN_PW_LEN + " characters.");
        return;
      }
      btn.disabled = true;
      btn.textContent = "Sending your code…";
      try {
        await apiPost("/hs/signup", payload);
        pendingSignupEmail = payload.email;
        renderVerify();
      } catch (e) {
        showError(errBox, e.message || "We could not start that. Please try again.");
        btn.disabled = false;
        btn.textContent = "Continue";
      }
    });
  }

  // ══════════════════════════════════════════════════════════
  //  SCREEN 5 — CONFIRM EMAIL
  // ══════════════════════════════════════════════════════════
  function renderVerify() {
    header.hidden = true;
    hideRail();
    mountTemplate("tplVerify");
    const form = document.getElementById("prvVerifyForm");
    const errBox = document.getElementById("prvVerifyError");
    const btn = document.getElementById("prvVerifyBtn");
    const codeEl = document.getElementById("prvVerifyCode");
    document.getElementById("prvVerifySub").textContent =
      "We sent a six-digit code to " + pendingSignupEmail;
    codeEl.focus();

    document.getElementById("prvResendBtn").addEventListener("click", async () => {
      try {
        await apiPost("/hs/signup/resend", { email: pendingSignupEmail });
        toast("Sent. Check your inbox.", "info");
      } catch (_) {
        toast("Sent. Check your inbox.", "info");
      }
    });

    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      showError(errBox, "");
      const code = (codeEl.value || "").trim();
      if (code.length !== 6) {
        showError(errBox, "Enter the six-digit code.");
        return;
      }
      btn.disabled = true;
      btn.textContent = "Confirming…";
      try {
        const data = await apiPost("/hs/signup/verify",
                                   { email: pendingSignupEmail, code });
        // The username was derived from their organization name and they have
        // never seen it. Remember it so the login form is prefilled if they
        // ever come back to it, and say it out loud on the next screen.
        try { localStorage.setItem("prv_last_username", data.username); } catch (_) {}
        toast("Your username is " + data.username + ". It is in your email too.", "info");
        pendingSignupEmail = "";
        await loadProfileAndRoute();
      } catch (e) {
        showError(errBox, e.message || "That code is not right, or it has expired.");
        btn.disabled = false;
        btn.textContent = "Confirm";
      }
    });
  }

  // ══════════════════════════════════════════════════════════
  //  NAVIGATION RAIL
  //
  //  Ported from the physician portal, including its rule: a surface the
  //  session lacks shows the item LOCKED rather than hiding it. A health
  //  system that just signed up is going to be approved within the day, and
  //  hiding the product makes it look empty at exactly the moment we are
  //  trying to show them what they joined.
  // ══════════════════════════════════════════════════════════
  const RAIL_ITEMS = [
    { dest: "upload",  label: "Upload",    surface: "upload",
      lockedHint: "Opens once your agreement is signed" },
    // "About you", never "Intake". An operator word must not reach a hospital
    // contact, the same rule the upload status vocabulary follows.
    { dest: "application", label: "About you",  surface: "intake" },
    { dest: "agreement",   label: "Agreement",  surface: "intake" },
    { dest: "payouts", label: "Payouts",   surface: "payouts" },
    { dest: "account", label: "Account",   surface: "account" }
  ];

  const railEl = document.getElementById("prvRail");
  let currentPanel = "upload";

  // Deny on absence: an older cached profile is not permission.
  function sessionHasSurface(surface) {
    if (!surface) return true;
    const list = (currentUser && currentUser.surfaces) || null;
    return Array.isArray(list) && list.indexOf(surface) !== -1;
  }

  // The organization's own onboarding state, as the server reports it. The rail
  // reads this ALONGSIDE the surface list because the two are different
  // questions: the surface says this login may upload, the state says the
  // organization has finished the paperwork behind it. Both have to be true,
  // and the server enforces both regardless of what this function decides —
  // this is what the rail LOOKS like, never what is allowed.
  function orgState() {
    return (currentUser && currentUser.state) || "active";
  }

  function orgNextStep() {
    return (currentUser && currentUser.next_step) || "";
  }

  function railUnlocked(item) {
    if (!sessionHasSurface(item.surface)) return false;
    if (item.dest === "upload") return orgState() === "active";
    return true;
  }

  function hideRail() {
    railEl.hidden = true;
    document.body.classList.remove("prv-has-rail");
  }

  function renderRail() {
    clear(railEl);
    RAIL_ITEMS.forEach((item) => {
      const unlocked = railUnlocked(item);
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "asc-rail-item" +
        (currentPanel === item.dest ? " active" : "") +
        (unlocked ? "" : " locked");
      const label = document.createElement("span");
      label.className = "asc-rail-label";
      label.textContent = item.label;
      btn.appendChild(label);
      if (!unlocked) {
        const lock = document.createElement("span");
        lock.className = "asc-rail-lock";
        lock.textContent = "Locked";
        btn.appendChild(lock);
        // The state's own sentence beats the generic hint: it names what has
        // to happen next rather than saying "not yet".
        btn.title = (item.dest === "upload" && orgNextStep())
          ? orgNextStep()
          : (item.lockedHint || "");
      }
      // A locked item opens the explanation, not a 403.
      btn.addEventListener("click", () =>
        setPanel(unlocked ? item.dest : "pending"));
      railEl.appendChild(btn);
    });
    railEl.hidden = false;
    document.body.classList.add("prv-has-rail");
  }

  function setPanel(dest) {
    if (dest !== "pending") currentPanel = dest;
    renderRail();
    if (dest === "upload") renderUpload();
    else if (dest === "payouts") renderPayouts();
    else if (dest === "application") renderApplication();
    else if (dest === "agreement") renderAgreement();
    else if (dest === "account") renderAccount();
    else renderPending();
  }

  // ══════════════════════════════════════════════════════════
  //  SCREEN 6 — ABOUT YOU
  //
  //  One page: the four structured questions, the team, and the older
  //  free-text note folded away underneath. Two forms that look like each
  //  other on two tabs is how you get a partner who fills in neither.
  // ══════════════════════════════════════════════════════════

  // Where the state banner is drawn, if the panel has one. Every panel that
  // shows it reads the SAME server sentence, so the portal never has two
  // opinions about what happens next.
  function paintState() {
    const card = document.getElementById("prvStateCard");
    if (!card) return;
    const chip = document.getElementById("prvStateChip");
    const next = document.getElementById("prvStateNext");
    const label = (currentUser && currentUser.state_label) || "";
    if (!label) { card.hidden = true; return; }
    chip.textContent = label;
    chip.className = "prv-state-chip prv-state-" + orgState();
    next.textContent = orgNextStep();
    card.hidden = false;
  }

  // One question, rendered by its shape. Radio for a single choice, because a
  // three-option select hides two of the three answers behind a click and the
  // whole point of these questions is that "Not sure" is visibly allowed.
  function radioGroup(question, chosen) {
    const wrap = document.createElement("fieldset");
    wrap.className = "prv-qgroup";
    const legend = document.createElement("legend");
    legend.className = "asc-label";
    legend.textContent = question.label;          // textContent: server copy
    wrap.appendChild(legend);
    if (question.help) {
      const help = document.createElement("p");
      help.className = "asc-help";
      help.textContent = question.help;
      wrap.appendChild(help);
    }
    (question.options || []).forEach((opt) => {
      const row = document.createElement("label");
      row.className = "prv-choice";
      const input = document.createElement("input");
      input.type = "radio";
      input.name = question.key;
      input.value = opt.value;
      if (chosen === opt.value) input.checked = true;
      const span = document.createElement("span");
      span.textContent = opt.label;
      row.appendChild(input);
      row.appendChild(span);
      wrap.appendChild(row);
    });
    return wrap;
  }

  function selectField(field, chosen) {
    const wrap = document.createElement("div");
    wrap.className = "asc-field";
    const label = document.createElement("label");
    label.className = "asc-label";
    label.setAttribute("for", "prv-f-" + field.key);
    label.textContent = field.label;
    const sel = document.createElement("select");
    sel.className = "asc-input";
    sel.id = "prv-f-" + field.key;
    sel.name = field.key;
    const blank = document.createElement("option");
    blank.value = "";
    blank.textContent = "Choose one";
    sel.appendChild(blank);
    (field.options || []).forEach((opt) => {
      const o = document.createElement("option");
      o.value = opt.value;
      o.textContent = opt.label;
      if (chosen === opt.value) o.selected = true;
      sel.appendChild(o);
    });
    wrap.appendChild(label);
    wrap.appendChild(sel);
    return wrap;
  }

  function multiField(field, chosenList) {
    const chosen = Array.isArray(chosenList) ? chosenList : [];
    const wrap = document.createElement("fieldset");
    wrap.className = "prv-qgroup prv-multi";
    const legend = document.createElement("legend");
    legend.className = "asc-label";
    legend.textContent = field.label;
    wrap.appendChild(legend);
    const grid = document.createElement("div");
    grid.className = "prv-multi-grid";
    (field.options || []).forEach((opt) => {
      const row = document.createElement("label");
      row.className = "prv-choice prv-choice-inline";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.name = field.key;
      input.value = opt.value;
      if (chosen.indexOf(opt.value) !== -1) input.checked = true;
      const span = document.createElement("span");
      span.textContent = opt.label;
      row.appendChild(input);
      row.appendChild(span);
      grid.appendChild(row);
    });
    wrap.appendChild(grid);
    return wrap;
  }

  async function renderApplication(opts) {
    renderHeader();
    mountTemplate("tplApplication");
    paintState();

    const fieldsEl = document.getElementById("prvAppFields");
    const errBox = document.getElementById("prvAppError");
    const okBox = document.getElementById("prvAppOk");
    const btn = document.getElementById("prvAppBtn");
    const form = document.getElementById("prvAppForm");

    if (opts && opts.firstRun) {
      document.getElementById("prvAppSub").textContent =
        "Four questions, so the first conversation starts from something real. " +
        "“Not sure” is a real answer to any of them.";
    }

    let data;
    try {
      data = await apiGet("/hs/application");
    } catch (e) {
      if (e instanceof AuthError) { bounceToLogin(e.message); return; }
      showError(errBox, e.message || "Could not load the questions.");
      return;
    }
    const previous = data.submitted || {};
    (data.prompts || []).forEach((q) => {
      if (q.options) {
        fieldsEl.appendChild(radioGroup(q, previous[q.key]));
        return;
      }
      // The composite question: one legend, three inputs under it.
      const group = document.createElement("div");
      group.className = "prv-qgroup prv-qgroup-composite";
      const legend = document.createElement("div");
      legend.className = "asc-label";
      legend.textContent = q.label;
      group.appendChild(legend);
      if (q.help) {
        const help = document.createElement("p");
        help.className = "asc-help";
        help.textContent = q.help;
        group.appendChild(help);
      }
      (q.fields || []).forEach((f) => {
        group.appendChild(f.kind === "multiselect"
          ? multiField(f, previous[f.key])
          : selectField(f, previous[f.key]));
      });
      fieldsEl.appendChild(group);
    });

    if (previous.submitted_at) {
      btn.textContent = "Update answers";
    }

    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      showError(errBox, "");
      okBox.hidden = true;
      const body = {};
      let missing = false;
      (data.prompts || []).forEach((q) => {
        if (q.options) {
          const picked = form.querySelector('input[name="' + q.key + '"]:checked');
          if (!picked) { missing = true; return; }
          body[q.key] = picked.value;
          return;
        }
        (q.fields || []).forEach((f) => {
          if (f.kind === "multiselect") {
            body[f.key] = Array.prototype.slice
              .call(form.querySelectorAll('input[name="' + f.key + '"]:checked'))
              .map((el) => el.value);
            return;
          }
          const el = document.getElementById("prv-f-" + f.key);
          if (!el || !el.value) { missing = true; return; }
          body[f.key] = el.value;
        });
      });
      if (missing) {
        showError(errBox, "Please answer every question. “Not sure” counts.");
        return;
      }
      btn.disabled = true;
      btn.textContent = "Submitting…";
      try {
        await apiPost("/hs/application", body);
        okBox.textContent =
          "Thank you. We are reading it, and we will come back to you within " +
          "one to two business days.";
        okBox.hidden = false;
        try { currentUser = await apiGet("/hs/me"); } catch (_) {}
        paintState();
        renderRail();
      } catch (e) {
        if (e instanceof AuthError) { bounceToLogin(e.message); return; }
        showError(errBox, e.message || "Could not save that. Please try again.");
      }
      btn.disabled = false;
      btn.textContent = "Update answers";
    });

    renderMembers();
    renderNotes();
  }

  // ─── §3.2 the team, on the same page ────────────────────────
  function memberRow() {
    const row = document.createElement("div");
    row.className = "asc-field prv-member-row";
    const input = document.createElement("input");
    input.className = "asc-input";
    input.type = "email";
    input.placeholder = "colleague@yourhospital.org";
    input.setAttribute("aria-label", "Teammate email address");
    row.appendChild(input);
    return row;
  }

  async function renderMembers() {
    const listEl = document.getElementById("prvMembers");
    const rowsEl = document.getElementById("prvMemberRows");
    const errBox = document.getElementById("prvMembersError");
    const okBox = document.getElementById("prvMembersOk");
    const btn = document.getElementById("prvMembersBtn");
    const form = document.getElementById("prvMembersForm");
    if (!listEl) return;

    async function paint() {
      clear(listEl);
      let data;
      try {
        data = await apiGet("/hs/members");
      } catch (e) {
        if (e instanceof AuthError) { bounceToLogin(e.message); return; }
        showError(errBox, e.message || "Could not load your team.");
        return;
      }
      (data.members || []).forEach((m) => {
        const li = document.createElement("li");
        li.className = "prv-member";
        const who = document.createElement("span");
        who.className = "prv-member-who";
        who.textContent = m.email || m.username;
        li.appendChild(who);
        if (m.is_you) {
          const you = document.createElement("span");
          you.className = "prv-member-you";
          you.textContent = "you";
          li.appendChild(you);
        }
        listEl.appendChild(li);
      });
    }

    rowsEl.appendChild(memberRow());
    document.getElementById("prvAddRow").addEventListener("click", () => {
      if (rowsEl.children.length >= 10) return;
      rowsEl.appendChild(memberRow());
    });

    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      showError(errBox, "");
      okBox.hidden = true;
      const emails = Array.prototype.slice.call(rowsEl.querySelectorAll("input"))
        .map((el) => (el.value || "").trim())
        .filter((v) => v);
      if (!emails.length) {
        showError(errBox, "Add an email address first.");
        return;
      }
      btn.disabled = true;
      btn.textContent = "Sending…";
      try {
        const res = await apiPost("/hs/members", { emails: emails });
        const added = (res && res.added) || [];
        okBox.textContent = added.length
          ? "Invitations sent to " + added.join(", ") + "."
          : "Everyone you listed already has access.";
        okBox.hidden = false;
        clear(rowsEl);
        rowsEl.appendChild(memberRow());
        await paint();
      } catch (e) {
        if (e instanceof AuthError) { bounceToLogin(e.message); return; }
        showError(errBox, e.message || "Could not send those invitations.");
      }
      btn.disabled = false;
      btn.textContent = "Send invitations";
    });

    paint();
  }

  // ─── The older free-text note, folded away ──────────────────
  async function renderNotes() {
    const fields = document.getElementById("prvIntakeFields");
    const errBox = document.getElementById("prvIntakeError");
    const okBox = document.getElementById("prvIntakeOk");
    const btn = document.getElementById("prvIntakeBtn");
    const form = document.getElementById("prvIntakeForm");
    if (!fields) return;

    let prompts = [];
    let previous = {};
    try {
      const data = await apiGet("/hs/intake");
      prompts = data.prompts || [];
      if (data.submitted && data.submitted.length) previous = data.submitted[0].answers || {};
    } catch (e) {
      if (e instanceof AuthError) { bounceToLogin(e.message); return; }
      showError(errBox, e.message || "Could not load these questions.");
      return;
    }

    const inputs = {};
    prompts.forEach((p) => {
      const wrap = document.createElement("div");
      wrap.className = "asc-field prv-intake-field";
      const label = document.createElement("label");
      label.className = "asc-label";
      label.setAttribute("for", "prv-in-" + p.key);
      // textContent, always: this copy is server-provided.
      label.textContent = p.label;
      if (!p.required) {
        const hint = document.createElement("span");
        hint.className = "asc-label-hint";
        hint.textContent = " (optional)";
        label.appendChild(hint);
      }
      const ta = document.createElement("textarea");
      ta.className = "asc-input";
      ta.id = "prv-in-" + p.key;
      ta.rows = 3;
      ta.placeholder = p.placeholder || "";
      ta.value = previous[p.key] || "";
      wrap.appendChild(label);
      wrap.appendChild(ta);
      fields.appendChild(wrap);
      inputs[p.key] = ta;
    });

    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      showError(errBox, "");
      okBox.hidden = true;
      const payload = {};
      Object.keys(inputs).forEach((k) => { payload[k] = inputs[k].value || ""; });
      btn.disabled = true;
      btn.textContent = "Saving…";
      try {
        await apiPost("/hs/intake", payload);
        okBox.textContent = "Thank you. We have that.";
        okBox.hidden = false;
        try { currentUser = await apiGet("/hs/me"); } catch (_) {}
        renderRail();
      } catch (e) {
        if (e instanceof AuthError) { bounceToLogin(e.message); return; }
        showError(errBox, e.message || "Could not save that. Please try again.");
      }
      btn.disabled = false;
      btn.textContent = "Save";
    });
  }

  // ══════════════════════════════════════════════════════════
  //  SCREEN 10 — THE AGREEMENT
  //
  //  Full text on screen, then the clickwrap. The Sign button stays disabled
  //  until both boxes are ticked and both fields are filled, which is the
  //  affirmative-assent requirement made visible rather than merely enforced
  //  on the server — the server enforces it too, and refuses either way.
  // ══════════════════════════════════════════════════════════
  async function renderAgreement() {
    renderHeader();
    mountTemplate("tplAgreement");

    const sub = document.getElementById("prvAgreementSub");
    const version = document.getElementById("prvAgreementVersion");
    const textEl = document.getElementById("prvAgreementText");
    const signedBox = document.getElementById("prvSignedBox");
    const signBox = document.getElementById("prvSignBox");

    let data;
    try {
      data = await apiGet("/hs/agreement");
    } catch (e) {
      if (e instanceof AuthError) { bounceToLogin(e.message); return; }
      textEl.textContent =
        "We could not load the agreement just now. Please refresh in a moment.";
      return;
    }

    version.textContent = data.doc_version || "";
    // textContent into a <pre>-styled box: the agreement is TEXT and must never
    // be parsed as markup. A contract that renders differently from the bytes
    // that were hashed is the one defect this feature cannot have.
    textEl.textContent = data.text || "";

    if (data.signed) {
      const when = formatWhen(data.signed.signed_at);
      sub.textContent = "Signed for your organization. Nothing further is needed.";
      document.getElementById("prvSignedLine").textContent =
        "Signed by " + (data.signed.signed_by || "") +
        (data.signed.signed_by_title ? ", " + data.signed.signed_by_title : "") +
        " on " + when + ".";
      document.getElementById("prvSignedHash").textContent =
        "Document fingerprint " + (data.signed.doc_sha256 || "").slice(0, 16) +
        "… · version " + (data.signed.doc_version || "");
      signedBox.hidden = false;
      return;
    }

    if (!data.can_sign) {
      sub.textContent = data.next_step || "";
      return;
    }

    sub.textContent =
      "Read it in full, then sign below. Uploading opens the moment you do.";
    signBox.hidden = false;
    document.getElementById("prvSignAuthorityLabel").textContent =
      "I am authorized to sign on behalf of " + (data.organization || "my organization") + ".";

    const authority = document.getElementById("prvSignAuthority");
    const esign = document.getElementById("prvSignEsign");
    const name = document.getElementById("prvSignName");
    const title = document.getElementById("prvSignTitle");
    const btn = document.getElementById("prvSignBtn");
    const errBox = document.getElementById("prvSignError");
    name.value = data.signer_name_prefill || "";

    function refresh() {
      btn.disabled = !(authority.checked && esign.checked &&
                       (name.value || "").trim() && (title.value || "").trim());
    }
    [authority, esign].forEach((el) => el.addEventListener("change", refresh));
    [name, title].forEach((el) => el.addEventListener("input", refresh));
    refresh();

    document.getElementById("prvSignForm").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      showError(errBox, "");
      btn.disabled = true;
      btn.textContent = "Signing…";
      try {
        await apiPost("/hs/agreement/sign", {
          typed_name: (name.value || "").trim(),
          typed_title: (title.value || "").trim(),
          authority_affirmed: authority.checked,
          consent_esign: esign.checked,
          // Echoed back so the server can refuse a signature against a document
          // that changed while this page was open.
          doc_sha256: data.doc_sha256 || ""
        });
        try { currentUser = await apiGet("/hs/me"); } catch (_) {}
        toast("Signed. Uploading is open.", "success");
        setPanel("upload");
        return;
      } catch (e) {
        if (e instanceof AuthError) { bounceToLogin(e.message); return; }
        showError(errBox, e.message || "We could not record that signature.");
      }
      btn.textContent = "Sign agreement";
      refresh();
    });
  }

  // ══════════════════════════════════════════════════════════
  //  SCREEN 7 — PAYOUTS
  // ══════════════════════════════════════════════════════════
  function formatMoney(cents) {
    const n = (Number(cents) || 0) / 100;
    return "$" + n.toLocaleString(undefined,
      { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function summaryStat(label, value) {
    const box = document.createElement("div");
    box.className = "prv-stat";
    const l = document.createElement("span");
    l.className = "prv-stat-label";
    l.textContent = label;
    const v = document.createElement("span");
    v.className = "prv-stat-value";
    v.textContent = value;
    box.appendChild(l);
    box.appendChild(v);
    return box;
  }

  async function renderPayouts() {
    renderHeader();
    mountTemplate("tplPayouts");
    const summaryEl = document.getElementById("prvPayoutSummary");
    const bodyEl = document.getElementById("prvPayoutBody");
    const emptyEl = document.getElementById("prvPayoutEmpty");
    const emptyText = document.getElementById("prvPayoutEmptyText");
    const tableWrap = document.getElementById("prvPayoutTableWrap");
    const howEl = document.getElementById("prvHowWePay");
    document.getElementById("prvPayoutsRefresh")
      .addEventListener("click", () => setPanel("payouts"));

    let data;
    try {
      data = await apiGet("/hs/payouts");
    } catch (e) {
      if (e instanceof AuthError) { bounceToLogin(e.message); return; }
      // A visible error, never a reassuring zero. A $0.00 that actually means
      // "we could not load your ledger" is the worst thing this page can show.
      tableWrap.hidden = true;
      emptyEl.hidden = false;
      emptyText.textContent =
        "We could not load your payouts just now. Please refresh in a moment.";
      return;
    }

    howEl.textContent = data.how_we_pay || "";
    const s = data.summary || {};
    clear(summaryEl);
    summaryEl.appendChild(summaryStat("Total recorded", formatMoney(s.total_cents)));
    summaryEl.appendChild(summaryStat("Paid", formatMoney(s.paid_cents)));
    summaryEl.appendChild(summaryStat("Awaiting payment", formatMoney(s.pending_cents)));

    renderRail(data.rail || {});

    renderAccrual(data.accrual || {});

    renderInvoices(data.invoices || []);

    const rows = data.payouts || [];
    if (!rows.length) {
      tableWrap.hidden = true;
      emptyEl.hidden = false;
      // The server's own sentence. It says two true things and no more: this is
      // where money appears, and the amounts come from the agreement they
      // signed rather than from anything on this page.
      emptyText.textContent = data.empty_note ||
        "Nothing recorded yet. Every payment we make to your organization will " +
        "appear here.";
      return;
    }
    tableWrap.hidden = false;
    emptyEl.hidden = true;
    rows.forEach((p) => {
      const tr = document.createElement("tr");
      const when = document.createElement("td");
      when.textContent = formatWhen(p.recorded_at);
      const what = document.createElement("td");
      what.textContent = p.description || "Data licence";
      const period = document.createElement("td");
      period.textContent = (p.period_start && p.period_end)
        ? p.period_start + " to " + p.period_end : "—";
      const status = document.createElement("td");
      const badge = document.createElement("span");
      badge.className = "asc-badge " +
        (p.status === "paid" ? "asc-badge-green"
          : p.status === "cancelled" ? "asc-badge-gray" : "asc-badge-amber");
      badge.textContent = p.status;
      status.appendChild(badge);
      const amount = document.createElement("td");
      amount.className = "prv-num prv-payout-amount";
      amount.textContent = formatMoney(p.amount_cents);
      [when, what, period, status, amount].forEach((td) => tr.appendChild(td));
      bodyEl.appendChild(tr);
    });
  }

  // What is owed, what is billed, what has cleared. The three states an
  // obligation passes through, so a partner can reconcile their own records
  // against ours without asking anyone.
  //
  // HIDDEN ENTIRELY UNTIL A PRICE EXISTS, and that is the rule this block is
  // really about. Three zeroes on a money page read as "you are owed nothing",
  // which is a different and false statement from "nobody has priced your data
  // yet". The unpriced case is what the accrual count line below already says
  // honestly, so this one stays out of its way.
  //
  // The server does every sum. This page turns cents into dollars and nothing
  // else: arithmetic here would be a second answer to a question the ledger has
  // already answered.
  function renderRail(rail) {
    const host = document.getElementById("prvPayoutRail");
    if (!host) return;
    if (!rail.priced) {
      host.hidden = true;
      return;
    }
    host.hidden = false;
    const stats = document.getElementById("prvPayoutRailStats");
    clear(stats);
    stats.appendChild(summaryStat("Accrued", formatMoney(rail.accrued_cents)));
    stats.appendChild(summaryStat("Invoiced", formatMoney(rail.invoiced_cents)));
    stats.appendChild(summaryStat("Settled", formatMoney(rail.settled_cents)));
    document.getElementById("prvPayoutRailNote").textContent = rail.note || "";
  }

  // The one line between "we took your data" and "we paid you for it". A
  // partner whose upload was accepted six weeks ago and whose ledger still reads
  // zero has no way, without this, to tell acceptance from loss.
  //
  // COUNTS ONLY, and the server does the subtraction. Turning a count into a
  // figure here would be this page inventing a price, which is the one thing
  // §15 forbids and the one thing a finance contact would quote back at us.
  function renderAccrual(accrual) {
    const host = document.getElementById("prvPayoutAccrual");
    if (!host) return;
    const waiting = Number(accrual.awaiting_pricing || 0);
    if (waiting <= 0) {
      host.hidden = true;
      return;
    }
    host.hidden = false;
    document.getElementById("prvPayoutAccrualLine").textContent =
      waiting + (waiting === 1 ? " upload accepted" : " uploads accepted") +
      " and awaiting pricing";
    document.getElementById("prvPayoutAccrualNote").textContent =
      accrual.note || "";
  }

  // What we have BILLED, as distinct from what we have PAID above. Drafts never
  // reach here — the server filters them — so every row is a number we have
  // committed to.
  function renderInvoices(invoices) {
    const host = document.getElementById("prvInvoices");
    if (!host) return;
    clear(host);
    if (!invoices.length) {
      host.hidden = true;
      return;
    }
    host.hidden = false;
    invoices.forEach((inv) => {
      const row = document.createElement("div");
      row.className = "prv-invoice";
      const period = document.createElement("span");
      period.className = "prv-mono";
      period.textContent = inv.period;
      const what = document.createElement("span");
      what.className = "prv-invoice-what";
      what.textContent = inv.description || "Data licence";
      const badge = document.createElement("span");
      badge.className = "asc-badge " +
        (inv.status === "paid" ? "asc-badge-green" : "asc-badge-amber");
      badge.textContent = inv.status;
      const amount = document.createElement("span");
      amount.className = "prv-num prv-invoice-amount";
      amount.textContent = formatMoney(inv.amount_cents);
      [period, what, badge, amount].forEach((el) => row.appendChild(el));
      host.appendChild(row);
    });
  }

  // ══════════════════════════════════════════════════════════
  //  SCREEN 8 — ACCOUNT
  // ══════════════════════════════════════════════════════════
  function renderAccount() {
    renderHeader();
    mountTemplate("tplAccount");
    const form = document.getElementById("prvAccountForm");
    const errBox = document.getElementById("prvAccountError");
    const okBox = document.getElementById("prvAccountOk");
    const btn = document.getElementById("prvAccountBtn");
    const cur = document.getElementById("prvAcCurrent");
    const next = document.getElementById("prvAcNew");
    const strength = document.getElementById("prvAcStrength");

    document.getElementById("prvAccountSub").textContent =
      "You sign in as " + ((currentUser && currentUser.username) || "") + ".";

    next.addEventListener("input", () => {
      const s = scorePassword(next.value || "");
      strength.className = "prv-strength " + s.cls;
      strength.textContent = next.value ? s.label : "";
    });

    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      showError(errBox, "");
      okBox.hidden = true;
      if ((next.value || "").length < MIN_PW_LEN) {
        showError(errBox, "Use at least " + MIN_PW_LEN + " characters.");
        return;
      }
      btn.disabled = true;
      btn.textContent = "Saving…";
      try {
        await apiPost("/hs/password",
                      { current_password: cur.value || "", new_password: next.value });
        okBox.textContent = "Password changed.";
        okBox.hidden = false;
        cur.value = "";
        next.value = "";
        strength.textContent = "";
      } catch (e) {
        if (e instanceof AuthError) { bounceToLogin(e.message); return; }
        showError(errBox, e.message || "Could not change your password.");
      }
      btn.disabled = false;
      btn.textContent = "Change password";
    });
  }

  // ══════════════════════════════════════════════════════════
  //  SCREEN 9 — IN REVIEW
  // ══════════════════════════════════════════════════════════
  function renderPending() {
    renderHeader();
    mountTemplate("tplPending");
    // The server's own sentence for whichever state this organization is in.
    // The template's copy is the fallback, not the source: a state added later
    // says the right thing here without this file changing.
    const title = document.getElementById("prvPendingTitle");
    const next = document.getElementById("prvPendingNext");
    if (currentUser && currentUser.state_label) title.textContent = currentUser.state_label;
    if (orgNextStep()) next.textContent = orgNextStep();
    document.getElementById("prvPendingIntake")
      .addEventListener("click", () => setPanel("application"));
    // The one state where there is something to DO rather than wait for.
    const sign = document.getElementById("prvPendingSign");
    if (orgState() === "approved_awaiting_dla") {
      sign.hidden = false;
      sign.addEventListener("click", () => setPanel("agreement"));
    }
  }

  // ══════════════════════════════════════════════════════════
  //  ROUTING
  // ══════════════════════════════════════════════════════════
  async function loadProfileAndRoute() {
    try {
      const me = await apiGet("/hs/me");
      currentUser = me;
      // 1. A passphrase we mailed has to be replaced before it guards anything.
      //    A self-signup never lands here: it chose its own password.
      if (me && me.must_reset === true) {
        hideRail();
        renderReset();
        return;
      }
      // 2. An organization still at the start of onboarding lands on the
      //    application, every time, until it has been submitted. That is the
      //    one thing standing between them and a decision, and a portal that
      //    opens onto a locked upload screen instead teaches them there is
      //    nothing here for them.
      if (orgState() === "intake") {
        currentPanel = "application";
        renderRail();
        renderApplication({ firstRun: true });
        return;
      }
      // 3. Approved and unsigned: the agreement is the whole job.
      if (orgState() === "approved_awaiting_dla" && !(me && me.agreement)) {
        currentPanel = "agreement";
        renderRail();
        renderAgreement();
        return;
      }
      // 4. A brand-new signup on an organization that predates the state
      //    machine is still walked into the questions once. An existing partner
      //    is never routed here, whatever intake_at says.
      if (me && me.intake_needed === true) {
        currentPanel = "application";
        renderRail();
        renderApplication({ firstRun: true });
        return;
      }
      // 5. Otherwise the last panel, defaulting to upload when it is open and
      //    to the application when it is not, so nobody ever opens onto a
      //    locked door.
      const uploadOpen = sessionHasSurface("upload") && orgState() === "active";
      const dest = uploadOpen
        ? (currentPanel || "upload")
        : (currentPanel === "upload" || !currentPanel ? "application" : currentPanel);
      setPanel(dest);
    } catch (e) {
      if (e instanceof AuthError) { hideRail(); renderLogin(); return; }
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
