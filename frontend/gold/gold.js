/* Gold Standard — Clinical Review SPA.
 *
 * Doctor reviews an AI-drafted note and fixes it; every fix is captured as gold
 * plus an auto-suggested error label. Reuses the doctor-portal JWT. Vanilla JS,
 * hash-routed, no build step. Tap/click/voice only — never intercepts keys
 * (contributors may dictate via the Wispr desktop app into plain textareas).
 */
(function () {
  "use strict";

  var API = "/api/gold";
  var TOKEN_KEY = "archangel_doctor_auth_token";
  var PROFILE_KEY = "archangel_doctor_profile_ui_v2";

  var state = {
    token: "",
    taxonomy: null,
    stats: null,
    isOperator: false,
  };

  // ── hyperscript helper ──────────────────────────────────────────────
  function h(tag, attrs) {
    var el = document.createElement(tag);
    var children = [];
    if (attrs && (typeof attrs !== "object" || attrs.nodeType || Array.isArray(attrs))) {
      children = [attrs];
      attrs = null;
    }
    if (attrs) {
      for (var k in attrs) {
        if (!Object.prototype.hasOwnProperty.call(attrs, k)) continue;
        var v = attrs[k];
        if (v == null || v === false) continue;
        if (k === "class") el.className = v;
        else if (k === "html") el.innerHTML = v;
        else if (k === "style" && typeof v === "object") { for (var s in v) el.style[s] = v[s]; }
        else if (k.slice(0, 2) === "on" && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
        else if (k === "dataset") { for (var d in v) el.dataset[d] = v[d]; }
        else if (v === true) el.setAttribute(k, "");
        else el.setAttribute(k, v);
      }
    }
    for (var i = 2; i < arguments.length; i++) append(el, arguments[i]);
    if (children.length) append(el, children[0]);
    return el;
  }
  function append(el, child) {
    if (child == null || child === false) return;
    if (Array.isArray(child)) { child.forEach(function (c) { append(el, c); }); return; }
    if (child.nodeType) el.appendChild(child);
    else el.appendChild(document.createTextNode(String(child)));
  }
  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
  function esc(s) { return String(s == null ? "" : s); }

  // ── toast ───────────────────────────────────────────────────────────
  function toast(msg, kind) {
    var region = document.getElementById("gsToasts");
    var t = h("div", { class: "gs-toast" + (kind ? " " + kind : "") }, msg);
    region.appendChild(t);
    setTimeout(function () { t.style.opacity = "0"; setTimeout(function () { t.remove(); }, 250); }, 3200);
  }

  // ── auth + api ──────────────────────────────────────────────────────
  function resolveToken() {
    // Same-origin: reuse the doctor-portal JWT. Also accept ?auth=/?token= handoff.
    var url = new URL(window.location.href);
    var handoff = url.searchParams.get("auth") || url.searchParams.get("token");
    if (handoff) {
      try { localStorage.setItem(TOKEN_KEY, handoff); } catch (e) {}
      url.searchParams.delete("auth");
      url.searchParams.delete("token");
      window.history.replaceState({}, "", url.pathname + (url.hash || ""));
    }
    try { return localStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; }
  }

  function profileRole() {
    try { return (JSON.parse(localStorage.getItem(PROFILE_KEY) || "{}").role || "").toLowerCase(); }
    catch (e) { return ""; }
  }

  function api(path, opts) {
    opts = opts || {};
    var headers = opts.headers || {};
    if (state.token) headers["Authorization"] = "Bearer " + state.token;
    if (opts.json !== undefined) {
      headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.json);
    }
    return fetch(API + path, { method: opts.method || "GET", headers: headers, body: opts.body })
      .then(function (r) {
        return r.text().then(function (txt) {
          var data = null;
          try { data = txt ? JSON.parse(txt) : null; } catch (e) { data = txt; }
          if (!r.ok) {
            var err = new Error((data && data.detail && (data.detail.message || data.detail)) || ("HTTP " + r.status));
            err.status = r.status; err.data = data;
            throw err;
          }
          return data;
        });
      });
  }

  // ── router ──────────────────────────────────────────────────────────
  function route() { return (window.location.hash || "#/queue").replace(/^#/, ""); }
  function go(hash) { window.location.hash = hash; }
  function navActive() {
    var r = route();
    document.querySelectorAll("#gsNav a").forEach(function (a) {
      var target = a.getAttribute("href").replace(/^#/, "");
      a.classList.toggle("active", r === target || (target === "/queue" && r.indexOf("/review") === 0));
    });
  }

  function render() {
    navActive();
    var r = route();
    if (r.indexOf("/review/") === 0) return renderReview(r.slice("/review/".length));
    if (r === "/capture") return renderCapture();
    return renderQueue();
  }

  // ── sign-in gate ────────────────────────────────────────────────────
  function renderSignIn(root) {
    clear(root);
    root.appendChild(h("div", { class: "gs-signin" },
      h("h1", { class: "gs-page-title" }, "Gold Standard"),
      h("p", { class: "gs-page-sub" }, "Sign in through the doctor portal to review clinical notes."),
      h("p", { style: { marginTop: "18px" } }, h("a", { class: "gs-btn primary", href: "/doctor/sign-in" }, "Go to sign in"))
    ));
  }

  // ── queue view ──────────────────────────────────────────────────────
  function statBadge(status) {
    var map = {
      NEEDS_REVIEW: ["review", "Needs review"], DRAFTING: ["", "Drafting…"],
      DEIDENTIFYING: ["qa", "De-identifying"], NEEDS_QA: ["qa", "Needs QA"],
      EXPORT_READY: ["ready", "Export-ready"], EXPORTED: ["ready", "Exported"],
      CAPTURING: ["", "Capturing"], ERROR: ["qa", "Error"],
    };
    var m = map[status] || ["", status];
    return h("span", { class: "gs-badge " + m[0] }, m[1]);
  }

  function renderQueue() {
    var root = clear(document.getElementById("gsRoot"));
    root.appendChild(h("div", { class: "gs-page-head" },
      h("div", null,
        h("h1", { class: "gs-page-title" }, "Review Queue"),
        h("div", { class: "gs-page-sub" }, "Read the AI draft and fix it. Every fix becomes gold training data.")
      ),
      h("button", { class: "gs-btn gold", onclick: function () { go("/capture"); } }, "＋ New capture")
    ));

    var statsWrap = h("div", { class: "gs-stats" });
    root.appendChild(statsWrap);
    var listWrap = h("div", { class: "gs-panel" },
      h("div", { class: "gs-panel-title" }, "Awaiting your review"),
      h("div", { class: "gs-list" }, h("div", { class: "gs-empty" }, "Loading…"))
    );
    root.appendChild(listWrap);

    Promise.all([
      api("/stats").catch(function () { return null; }),
      api("/visits?status=NEEDS_REVIEW").catch(function () { return { visits: [] }; }),
    ]).then(function (res) {
      var stats = res[0], list = (res[1] && res[1].visits) || [];
      state.stats = stats;
      state.isOperator = !!(stats && stats.is_operator);
      clear(statsWrap);
      if (stats) {
        var q = stats.queues || {}, tot = stats.totals || {}, sg = stats.surgeon || {};
        [
          ["gold", q.needs_review || 0, "Needs review"],
          ["", tot.submitted || 0, "Submitted"],
          ["", tot.captured || 0, "Captured"],
          ["", sg.visits_contributed || 0, "Your records"],
          ["", "$" + (sg.amount_earned_usd || 0), "Your earnings"],
        ].forEach(function (s) {
          statsWrap.appendChild(h("div", { class: "gs-stat" + (s[0] ? " " + s[0] : "") },
            h("div", { class: "n" }, s[1]), h("div", { class: "l" }, s[2])));
        });
      }
      var body = listWrap.querySelector(".gs-list");
      clear(body);
      if (!list.length) { body.appendChild(h("div", { class: "gs-empty" }, "Nothing to review right now. Start a new capture to add data.")); return; }
      list.forEach(function (v) {
        body.appendChild(h("div", { class: "gs-row", onclick: function () { go("/review/" + v.id); } },
          h("div", { class: "gs-row-main" },
            h("div", { class: "gs-row-id" }, v.record_id || v.id),
            h("div", { class: "gs-row-meta" },
              (v.specialty || "—") + " · " + (v.encounter_type || "—") +
              (v.audio_duration_sec ? " · " + Math.round(v.audio_duration_sec) + "s" : ""))
          ),
          statBadge(v.status),
          h("span", { class: "gs-btn gs-btn-sm" }, "Review →")
        ));
      });
    }).catch(function (e) {
      if (e.status === 401) return renderSignIn(root);
      clear(statsWrap);
      root.appendChild(h("div", { class: "gs-notice warn" }, "Could not load the queue: " + e.message));
    });
  }

  // ── voice mic button ────────────────────────────────────────────────
  function micButton(getVal, setVal) {
    var btn = h("button", { class: "gs-mic", type: "button", title: "Dictate", "aria-label": "Dictate" }, "🎤");
    var rec = null, chunks = [];
    btn.addEventListener("click", function () {
      if (rec && rec.state === "recording") { rec.stop(); return; }
      if (!navigator.mediaDevices || !window.MediaRecorder) { toast("Voice capture not supported in this browser", "err"); return; }
      navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
        chunks = [];
        rec = new MediaRecorder(stream);
        rec.ondataavailable = function (e) { if (e.data && e.data.size) chunks.push(e.data); };
        rec.onstop = function () {
          stream.getTracks().forEach(function (t) { t.stop(); });
          btn.classList.remove("rec");
          var blob = new Blob(chunks, { type: (rec.mimeType || "audio/webm") });
          if (!blob.size) return;
          var fd = new FormData();
          fd.append("file", blob, "dictation.webm");
          btn.textContent = "…";
          api("/transcribe", { method: "POST", body: fd }).then(function (r) {
            btn.textContent = "🎤";
            var text = (r && r.text || "").trim();
            if (!text) { toast(r && r.error === "stt_no_baa" ? "Dictation needs a BAA-covered STT provider" : "No speech detected", "err"); return; }
            var cur = getVal();
            setVal(cur ? (cur.replace(/\s*$/, "") + " " + text) : text);
          }).catch(function () { btn.textContent = "🎤"; toast("Dictation failed", "err"); });
        };
        rec.start();
        btn.classList.add("rec");
      }).catch(function () { toast("Microphone permission denied", "err"); });
    });
    return btn;
  }

  // ── error-label heuristic (client fallback, §4) ─────────────────────
  var LATERALITY = /\b(left|right|bilateral|lateral|medial|proximal|distal)\b/gi;
  var NUMBERS = /\d+(\.\d+)?/g;
  var DRUGS = /\b(mg|mcg|ml|units?|dose|dosage|tablet|capsule|bid|tid|qd|prn|daily|twice)\b/i;
  function heuristicLabel(section, before, after) {
    before = before || ""; after = after || "";
    var bLat = (before.match(LATERALITY) || []).join("|").toLowerCase();
    var aLat = (after.match(LATERALITY) || []).join("|").toLowerCase();
    if (bLat !== aLat) return "wrong_laterality_or_site";
    if (section === "plan" && (DRUGS.test(before) || DRUGS.test(after))) return "medication_error";
    var bNum = (before.match(NUMBERS) || []).join(",");
    var aNum = (after.match(NUMBERS) || []).join(",");
    if (bNum !== aNum && (bNum || aNum)) return "factual_value_error";
    if (after.replace(/\s+/g, " ").length > before.replace(/\s+/g, " ").length + 12) return "omission";
    if (before.replace(/\s+/g, " ").length > after.replace(/\s+/g, " ").length + 12) return "hallucination";
    return "other";
  }

  // ── split a flat draft note into SOAP sections (fallback, §8.1) ─────
  var SECTIONS = ["subjective", "objective", "assessment", "plan"];
  function splitNote(text) {
    var out = { subjective: "", objective: "", assessment: "", plan: "" };
    if (!text) return out;
    var map = { s: "subjective", o: "objective", a: "assessment", p: "plan",
      subjective: "subjective", objective: "objective", assessment: "assessment", plan: "plan" };
    var re = /^\s*(subjective|objective|assessment|plan|s|o|a|p)\s*[:\-]/i;
    var lines = text.split(/\r?\n/), cur = null;
    lines.forEach(function (line) {
      var m = line.match(re);
      if (m) {
        cur = map[m[1].toLowerCase()];
        var rest = line.slice(m[0].length).trim();
        out[cur] = rest;
      } else if (cur) {
        out[cur] += (out[cur] ? "\n" : "") + line;
      }
    });
    var any = SECTIONS.some(function (k) { return out[k].trim(); });
    if (!any) out.subjective = text.trim(); // no headers found — dump into subjective
    SECTIONS.forEach(function (k) { out[k] = (out[k] || "").trim(); });
    return out;
  }

  // ── review view (the core) ──────────────────────────────────────────
  function renderReview(visitId) {
    var root = clear(document.getElementById("gsRoot"));
    root.appendChild(h("div", { class: "gs-empty" }, "Loading record…"));

    Promise.all([
      state.taxonomy ? Promise.resolve(state.taxonomy) : api("/taxonomy"),
      api("/visits/" + visitId),
    ]).then(function (res) {
      state.taxonomy = res[0];
      buildReview(root, visitId, res[1]);
    }).catch(function (e) {
      if (e.status === 401) return renderSignIn(root);
      clear(root);
      root.appendChild(h("div", { class: "gs-notice warn" }, "Could not load this record: " + e.message));
      root.appendChild(h("button", { class: "gs-btn", onclick: function () { go("/queue"); } }, "← Back to queue"));
    });
  }

  function buildReview(root, visitId, visit) {
    clear(root);
    var tax = state.taxonomy;

    if (visit.status !== "NEEDS_REVIEW") {
      root.appendChild(h("div", { class: "gs-notice info" },
        "This record is not awaiting review (status: " + visit.status + "). Only NEEDS_REVIEW records can be submitted."));
      root.appendChild(h("button", { class: "gs-btn", onclick: function () { go("/queue"); } }, "← Back to queue"));
      return;
    }

    // Section data: prefer server-sectioned draft (§8.1), else split client-side.
    var secData = visit.ai_draft_sections && Object.keys(visit.ai_draft_sections).length
      ? visit.ai_draft_sections : splitNote(visit.ai_draft_note || "");

    // Per-section runtime state
    var sections = SECTIONS.map(function (key) {
      return { key: key, draft: (secData[key] || "").trim(), value: (secData[key] || "").trim(), state: "pending", label: null };
    }).filter(function (s) { return s.draft || s.key === "assessment" || s.key === "plan"; });
    // Always keep the four canonical sections even if a draft section is blank,
    // so the doctor consciously handles each. (An empty draft is still reviewable.)
    if (sections.length < 4) {
      sections = SECTIONS.map(function (key) {
        var found = sections.filter(function (s) { return s.key === key; })[0];
        return found || { key: key, draft: (secData[key] || "").trim(), value: (secData[key] || "").trim(), state: "pending", label: null };
      });
    }

    var difficulty = (visit.difficulty_tags || []).slice();
    var languages = (visit.languages || []).slice();
    var rawCodes = Array.isArray(visit.suggested_codes) && visit.suggested_codes.length
      ? visit.suggested_codes : visit.billing_codes;
    if (!Array.isArray(rawCodes)) rawCodes = [];
    var codes = rawCodes.map(function (c) { c = c || {}; return { system: c.system || "ICD-10", code: c.code || "", description: c.description || "" }; });
    var taskToggles = { prior_auth: false, referral: false, patient_instructions: false };
    (visit.tasks || []).forEach(function (t) { if (t in taskToggles) taskToggles[t] = true; });
    var priorAuth = visit.prior_auth ? Object.assign({ drug_or_service: "", justification_text: "", outcome: "pending" }, visit.prior_auth) : null;

    var startMs = Date.now();

    // ── header ──
    var timerEl = h("span", { class: "gs-timer" }, "0:00");
    var timerInt = setInterval(function () {
      var s = Math.floor((Date.now() - startMs) / 1000);
      timerEl.textContent = Math.floor(s / 60) + ":" + ("0" + (s % 60)).slice(-2);
    }, 1000);

    root.appendChild(h("div", { class: "gs-page-head" },
      h("div", null,
        h("h1", { class: "gs-page-title" }, "Review " + (visit.record_id || "")),
        h("div", { class: "gs-page-sub" }, "Confirm what's right, fix what's wrong. Target ≤ 90 seconds.")
      ),
      h("button", { class: "gs-btn ghost", onclick: function () { clearInterval(timerInt); go("/queue"); } }, "← Queue")
    ));

    var diffChips = h("div", { class: "gs-chip-row" });
    function renderDiffChips() {
      clear(diffChips);
      (tax.difficulty_tags || []).forEach(function (tg) {
        var on = difficulty.indexOf(tg) !== -1;
        diffChips.appendChild(h("span", { class: "gs-chip" + (on ? " on" : ""), onclick: function () {
          var i = difficulty.indexOf(tg);
          if (i === -1) difficulty.push(tg); else difficulty.splice(i, 1);
          renderDiffChips();
        } }, tg.replace(/_/g, " ")));
      });
    }
    renderDiffChips();

    root.appendChild(h("div", { class: "gs-review-head" },
      h("span", { class: "meta" }, (visit.specialty || "—") + " · " + (visit.encounter_type || "—")),
      timerEl
    ));
    root.appendChild(h("div", { class: "gs-panel", style: { padding: "12px 16px", marginBottom: "16px" } },
      h("div", { style: { fontSize: "12px", fontWeight: "700", marginBottom: "8px", color: "#64748b" } }, "Difficulty tags"),
      diffChips
    ));

    // ── grid: transcript | note ──
    var grid = h("div", { class: "gs-review-grid" });
    root.appendChild(grid);

    // transcript
    var transBody = h("div", { class: "gs-transcript-body" });
    if (Array.isArray(visit.transcript_turns) && visit.transcript_turns.length) {
      visit.transcript_turns.forEach(function (t) {
        transBody.appendChild(h("div", { class: "gs-turn" },
          h("span", { class: "spk" }, (t.speaker || t.role || "Speaker") + ": "),
          h("span", null, t.text || t.content || "")));
      });
    } else {
      transBody.appendChild(h("div", null, visit.transcript || "(no transcript)"));
    }
    grid.appendChild(h("div", { class: "gs-panel gs-transcript" },
      h("div", { class: "gs-panel-title" }, "Transcript"),
      transBody
    ));

    // note column
    var noteCol = h("div", null);
    grid.appendChild(noteCol);

    var submitBtn, submitNote;
    function refreshSubmitState() {
      var allHandled = sections.every(function (s) { return s.state !== "pending"; });
      var goldNote = assembleGoldNote(sections);
      var ok = allHandled && goldNote.trim().length > 0;
      submitBtn.disabled = !ok;
      var pending = sections.filter(function (s) { return s.state === "pending"; }).length;
      submitNote.textContent = ok ? "Ready to submit." :
        (pending ? (pending + " section" + (pending > 1 ? "s" : "") + " still need" + (pending === 1 ? "s" : "") + " your confirmation.") : "Note cannot be empty.");
    }

    // fast path
    noteCol.appendChild(h("button", { class: "gs-btn success", style: { marginBottom: "12px", width: "100%" }, onclick: function () {
      sections.forEach(function (s) { s.state = "confirmed"; s.value = s.draft; s.label = null; });
      renderSections(); refreshSubmitState();
      toast("Whole note confirmed — submit when ready", "ok");
    } }, "✓ Whole note looks correct"));

    var sectionsWrap = h("div", null);
    noteCol.appendChild(sectionsWrap);

    function renderSections() {
      clear(sectionsWrap);
      sections.forEach(function (sec) { sectionsWrap.appendChild(sectionCard(sec)); });
    }

    function sectionCard(sec) {
      var card = h("div", { class: "gs-section " + sec.state });
      card.appendChild(h("div", { class: "gs-section-head" },
        h("span", { class: "gs-section-title" }, sec.key),
        h("span", { class: "gs-section-state" },
          sec.state === "confirmed" ? "✓ Confirmed" : sec.state === "edited" ? "✎ Edited" : "Pending")
      ));

      var taWrap = h("div", { class: "gs-ta-wrap" });
      var ta = h("textarea", { class: "gs-textarea", rows: "3" });
      ta.value = sec.value;
      ta.addEventListener("input", function () {
        sec.value = ta.value;
        if (ta.value.trim() !== sec.draft.trim()) {
          if (sec.state !== "edited") { sec.state = "edited"; onEdited(sec, card); }
        } else {
          if (sec.state === "edited") { sec.state = "pending"; sec.label = null; }
        }
        card.className = "gs-section " + sec.state;
        card.querySelector(".gs-section-state").textContent =
          sec.state === "confirmed" ? "✓ Confirmed" : sec.state === "edited" ? "✎ Edited" : "Pending";
        refreshSubmitState();
      });
      taWrap.appendChild(ta);
      taWrap.appendChild(micButton(function () { return ta.value; }, function (v) {
        ta.value = v; ta.dispatchEvent(new Event("input"));
      }));
      card.appendChild(taWrap);

      var actions = h("div", { class: "gs-section-actions" },
        h("button", { class: "gs-btn gs-btn-sm success", onclick: function () {
          sec.state = "confirmed"; sec.value = sec.draft; sec.label = null; ta.value = sec.draft;
          renderSections(); refreshSubmitState();
        } }, "✓ Looks right")
      );
      card.appendChild(actions);

      if (sec.state === "edited") {
        card.appendChild(labelBox(sec, card));
        card.appendChild(h("details", { class: "gs-draft-ref" },
          h("summary", null, "Show original draft"),
          h("div", { class: "txt" }, sec.draft || "(empty)")));
      }
      return card;
    }

    function onEdited(sec, card) {
      // Pre-fill with the client heuristic immediately (works offline), then try
      // to upgrade with the LLM suggestion (§4). Suggestion is confirm/adjust only.
      sec.label = { type: heuristicLabel(sec.key, sec.draft, sec.value), severity: "medium", source: "heuristic" };
      renderSections();
      api("/visits/" + visitId + "/suggest-labels", { method: "POST", json: {
        section: sec.key, original_text: sec.draft, corrected_text: sec.value,
      } }).then(function (r) {
        if (r && r.method === "llm" && r.type && sec.state === "edited") {
          sec.label = { type: r.type, severity: r.severity || "medium", source: "llm" };
          renderSections();
        }
      }).catch(function () {});
    }

    function labelBox(sec, card) {
      var box = h("div", { class: "gs-label-box" });
      box.appendChild(h("div", { class: "hd" }, "🏷 Suggested error label" + (sec.label && sec.label.source === "llm" ? " (AI)" : "")));
      var typeSel = h("select", { class: "gs-select" });
      (tax.types || []).forEach(function (t) {
        var o = h("option", { value: t.type }, t.label || t.type);
        if (sec.label && sec.label.type === t.type) o.selected = true;
        typeSel.appendChild(o);
      });
      typeSel.addEventListener("change", function () { sec.label.type = typeSel.value; });
      var sevSel = h("select", { class: "gs-select" });
      (tax.severities || ["low", "medium", "high"]).forEach(function (sv) {
        var o = h("option", { value: sv }, sv);
        if (sec.label && sec.label.severity === sv) o.selected = true;
        sevSel.appendChild(o);
      });
      sevSel.addEventListener("change", function () { sec.label.severity = sevSel.value; });
      box.appendChild(h("div", { class: "gs-label-row" },
        h("span", { style: { fontSize: "12px", color: "#92400e" } }, "Type"), typeSel,
        h("span", { style: { fontSize: "12px", color: "#92400e" } }, "Severity"), sevSel,
        h("button", { class: "gs-btn gs-btn-sm danger", onclick: function () {
          // "No label needed" — keep the edit as gold but drop the label.
          sec.label = null; renderSections();
        } }, "No label")
      ));
      return box;
    }

    renderSections();

    // ── codes panel ──
    var codesBody = h("div", null);
    function renderCodes() {
      clear(codesBody);
      codes.forEach(function (c, idx) {
        var sysSel = h("select", { class: "gs-select" });
        ["ICD-10", "CPT"].forEach(function (sy) { var o = h("option", { value: sy }, sy); if (c.system === sy) o.selected = true; sysSel.appendChild(o); });
        sysSel.addEventListener("change", function () { c.system = sysSel.value; });
        var codeIn = h("input", { class: "gs-input", style: { width: "110px" }, placeholder: "Code", value: c.code });
        codeIn.addEventListener("input", function () { c.code = codeIn.value; });
        var descIn = h("input", { class: "gs-input", style: { flex: "1", minWidth: "140px" }, placeholder: "Description", value: c.description });
        descIn.addEventListener("input", function () { c.description = descIn.value; });
        codesBody.appendChild(h("div", { class: "gs-code-row" }, sysSel, codeIn, descIn,
          h("button", { class: "gs-btn gs-btn-sm danger", onclick: function () { codes.splice(idx, 1); renderCodes(); } }, "✕")));
      });
      if (!codes.length) codesBody.appendChild(h("div", { class: "gs-page-sub" }, "No billing codes."));
    }
    renderCodes();
    noteCol.appendChild(h("div", { class: "gs-panel" },
      h("div", { class: "gs-panel-title" }, "Billing codes"),
      codesBody,
      h("button", { class: "gs-btn gs-btn-sm", style: { marginTop: "6px" }, onclick: function () {
        codes.push({ system: "ICD-10", code: "", description: "" }); renderCodes();
      } }, "＋ Add code")
    ));

    // ── tasks + prior-auth ──
    var paWrap = h("div", null);
    function renderPriorAuthForm() {
      clear(paWrap);
      if (!taskToggles.prior_auth) { priorAuth = null; return; }
      priorAuth = priorAuth || { drug_or_service: "", justification_text: "", outcome: "pending" };
      var dIn = h("input", { class: "gs-input", style: { width: "100%" }, placeholder: "Drug or service", value: priorAuth.drug_or_service });
      dIn.addEventListener("input", function () { priorAuth.drug_or_service = dIn.value; });
      var jWrap = h("div", { class: "gs-ta-wrap" });
      var jTa = h("textarea", { class: "gs-textarea", rows: "2", placeholder: "Justification" });
      jTa.value = priorAuth.justification_text;
      jTa.addEventListener("input", function () { priorAuth.justification_text = jTa.value; });
      jWrap.appendChild(jTa);
      jWrap.appendChild(micButton(function () { return jTa.value; }, function (v) { jTa.value = v; priorAuth.justification_text = v; }));
      var oSel = h("select", { class: "gs-select" });
      ["pending", "approved", "denied"].forEach(function (o) { var op = h("option", { value: o }, o); if (priorAuth.outcome === o) op.selected = true; oSel.appendChild(op); });
      oSel.addEventListener("change", function () { priorAuth.outcome = oSel.value; });
      paWrap.appendChild(h("div", { class: "gs-field", style: { marginTop: "10px" } },
        h("label", null, "Prior authorization"), dIn, jWrap,
        h("div", { style: { marginTop: "8px" } }, h("span", { style: { fontSize: "12px", color: "#64748b", marginRight: "8px" } }, "Outcome"), oSel)));
    }
    var taskChips = h("div", { class: "gs-chip-row" });
    ["prior_auth", "referral", "patient_instructions"].forEach(function (t) {
      var chip = h("span", { class: "gs-chip" + (taskToggles[t] ? " on" : ""), onclick: function () {
        taskToggles[t] = !taskToggles[t];
        chip.classList.toggle("on", taskToggles[t]);
        if (t === "prior_auth") renderPriorAuthForm();
      } }, t.replace(/_/g, " "));
      taskChips.appendChild(chip);
    });
    renderPriorAuthForm();
    noteCol.appendChild(h("div", { class: "gs-panel" },
      h("div", { class: "gs-panel-title" }, "Workflow tasks"),
      h("div", { class: "gs-page-sub", style: { marginBottom: "8px" } }, "Note generation is always included. Toggle any extra tasks this visit serves."),
      taskChips, paWrap
    ));

    // ── submit bar ──
    submitNote = h("span", { class: "gs-submit-note" }, "");
    submitBtn = h("button", { class: "gs-btn primary", disabled: true, onclick: function () {
      doSubmit();
    } }, "Submit gold record");
    noteCol.appendChild(h("div", { class: "gs-submit-bar" },
      submitNote, h("span", { class: "spacer" }), submitBtn));
    refreshSubmitState();

    function doSubmit() {
      submitBtn.disabled = true; submitBtn.textContent = "Submitting…";
      var goldNote = assembleGoldNote(sections);
      var errorLabels = sections.filter(function (s) { return s.state === "edited" && s.label; }).map(function (s) {
        return { type: s.label.type, severity: s.label.severity || "medium", section: s.key,
          original_text: s.draft, corrected_text: s.value, clinician_verified: true };
      });
      var validCodes = codes.filter(function (c) { return (c.code || "").trim(); });
      var tasks = ["note_generation"].concat(Object.keys(taskToggles).filter(function (t) { return taskToggles[t]; }));
      var payload = {
        gold_note: goldNote,
        error_labels: errorLabels,
        billing_codes: validCodes,
        prior_auth: taskToggles.prior_auth && priorAuth && priorAuth.drug_or_service ? priorAuth : null,
        tasks: tasks,
        difficulty_tags: difficulty,
        languages: languages,
        clinician_review_seconds: Math.round((Date.now() - startMs) / 1000),
      };
      api("/visits/" + visitId + "/submit", { method: "POST", json: payload }).then(function () {
        clearInterval(timerInt);
        toast("Submitted — de-identification started", "ok");
        go("/queue");
      }).catch(function (e) {
        submitBtn.disabled = false; submitBtn.textContent = "Submit gold record";
        toast("Submit failed: " + e.message, "err");
      });
    }
  }

  function assembleGoldNote(sections) {
    var labels = { subjective: "Subjective", objective: "Objective", assessment: "Assessment", plan: "Plan" };
    var parts = [];
    sections.forEach(function (s) {
      var txt = (s.value || "").trim();
      if (txt) parts.push((labels[s.key] || s.key) + ":\n" + txt);
    });
    return parts.join("\n\n");
  }

  // ── capture view (consent → record → draft) ─────────────────────────
  function renderCapture() {
    var root = clear(document.getElementById("gsRoot"));
    root.appendChild(h("div", { class: "gs-page-head" },
      h("div", null,
        h("h1", { class: "gs-page-title" }, "New capture"),
        h("div", { class: "gs-page-sub" }, "Consent → record the visit → the AI drafts a note for you to review.")
      ),
      h("button", { class: "gs-btn ghost", onclick: function () { go("/queue"); } }, "← Queue")
    ));
    var panel = h("div", { class: "gs-panel" });
    root.appendChild(panel);

    var ctx = { visitId: null, specialty: "", encounter_type: "", consentMethod: "in_app_verbal", patientName: "", difficulty: [], languages: [] };
    stepConsent(panel, ctx);
  }

  function stepConsent(panel, ctx) {
    clear(panel);
    panel.appendChild(h("div", { class: "gs-panel-title" }, "1 · Consent"));
    panel.appendChild(h("div", { class: "gs-notice info" }, "Record only with the patient's consent. Declining discards everything immediately."));

    var specIn = h("input", { class: "gs-input", style: { width: "100%" }, placeholder: "e.g. general_surgery" });
    var encIn = h("input", { class: "gs-input", style: { width: "100%" }, placeholder: "e.g. post-op follow-up" });
    var nameIn = h("input", { class: "gs-input", style: { width: "100%" }, placeholder: "Patient name (for redaction only, never exported)" });
    var methodSel = h("select", { class: "gs-select" },
      h("option", { value: "in_app_verbal" }, "In-app verbal"),
      h("option", { value: "e_signature" }, "E-signature"));

    panel.appendChild(h("div", { class: "gs-field" }, h("label", null, "Specialty"), specIn));
    panel.appendChild(h("div", { class: "gs-field" }, h("label", null, "Encounter type"), encIn));
    panel.appendChild(h("div", { class: "gs-field" }, h("label", null, "Consent method"), methodSel));
    panel.appendChild(h("div", { class: "gs-field" }, h("label", null, "Patient name (optional)"),
      nameIn, h("div", { class: "hint" }, "Stored encrypted, used only to redact the name during de-id. Never exported.")));

    panel.appendChild(h("div", { class: "gs-section-actions", style: { marginTop: "10px" } },
      h("button", { class: "gs-btn primary", onclick: function () {
        ctx.specialty = specIn.value.trim(); ctx.encounter_type = encIn.value.trim();
        ctx.consentMethod = methodSel.value; ctx.patientName = nameIn.value.trim();
        api("/visits", { method: "POST", json: { specialty: ctx.specialty || null, encounter_type: ctx.encounter_type || null } })
          .then(function (r) {
            ctx.visitId = r.id;
            return api("/visits/" + r.id + "/consent", { method: "POST", json: {
              consent_given: true, consent_method: ctx.consentMethod, patient_name: ctx.patientName || null } });
          })
          .then(function () { stepRecord(panel, ctx); })
          .catch(function (e) { if (e.status === 401) return renderSignIn(document.getElementById("gsRoot")); toast("Could not start: " + e.message, "err"); });
      } }, "Consent given → record"),
      h("button", { class: "gs-btn danger", onclick: function () {
        // If a visit was allocated, decline discards it server-side.
        if (ctx.visitId) api("/visits/" + ctx.visitId + "/consent", { method: "POST", json: { consent_given: false } }).catch(function () {});
        go("/queue");
      } }, "Decline / cancel")
    ));
  }

  function stepRecord(panel, ctx) {
    clear(panel);
    panel.appendChild(h("div", { class: "gs-panel-title" }, "2 · Record the visit"));
    var status = h("div", { class: "gs-page-sub" }, "Tap record, then stop when the visit ends.");
    var timeEl = h("span", { class: "gs-timer" }, "0:00");
    var recBtn = h("button", { class: "gs-btn gold" }, "● Start recording");
    var uploadBtn = h("button", { class: "gs-btn primary", disabled: true }, "Upload → draft");
    var rec = null, chunks = [], blob = null, t0 = 0, tInt = null;

    recBtn.addEventListener("click", function () {
      if (rec && rec.state === "recording") { rec.stop(); return; }
      if (!navigator.mediaDevices || !window.MediaRecorder) { toast("Recording not supported in this browser", "err"); return; }
      navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
        chunks = []; rec = new MediaRecorder(stream);
        rec.ondataavailable = function (e) { if (e.data && e.data.size) chunks.push(e.data); };
        rec.onstop = function () {
          stream.getTracks().forEach(function (t) { t.stop(); });
          clearInterval(tInt);
          blob = new Blob(chunks, { type: rec.mimeType || "audio/webm" });
          recBtn.textContent = "● Re-record"; recBtn.classList.remove("rec");
          uploadBtn.disabled = false;
          status.textContent = "Recorded " + Math.round(blob.size / 1024) + " KB. Upload to generate the draft.";
        };
        rec.start(); t0 = Date.now();
        tInt = setInterval(function () { var s = Math.floor((Date.now() - t0) / 1000); timeEl.textContent = Math.floor(s / 60) + ":" + ("0" + (s % 60)).slice(-2); }, 1000);
        recBtn.textContent = "■ Stop"; recBtn.classList.add("rec");
        status.textContent = "Recording…";
      }).catch(function () { toast("Microphone permission denied", "err"); });
    });

    uploadBtn.addEventListener("click", function () {
      if (!blob) return;
      uploadBtn.disabled = true; uploadBtn.textContent = "Uploading…";
      var fd = new FormData();
      fd.append("file", blob, "visit.webm");
      fd.append("difficulty_tags", JSON.stringify(ctx.difficulty || []));
      fd.append("languages", JSON.stringify(ctx.languages || []));
      api("/visits/" + ctx.visitId + "/audio", { method: "POST", body: fd })
        .then(function () { stepDrafting(panel, ctx); })
        .catch(function (e) { uploadBtn.disabled = false; uploadBtn.textContent = "Upload → draft"; toast("Upload failed: " + e.message, "err"); });
    });

    panel.appendChild(status);
    panel.appendChild(h("div", { style: { display: "flex", alignItems: "center", gap: "12px", margin: "14px 0" } },
      h("span", { class: "gs-rec-dot" }), timeEl));
    panel.appendChild(h("div", { class: "gs-section-actions" }, recBtn, uploadBtn));
  }

  function stepDrafting(panel, ctx) {
    clear(panel);
    panel.appendChild(h("div", { class: "gs-panel-title" }, "3 · Generating draft"));
    var steps = h("div", { class: "gs-progress" },
      h("div", { class: "step active", id: "st-transcribe" }, h("span", { class: "gs-spinner", style: { width: "16px", height: "16px", margin: "0" } }), "Transcribing audio…"),
      h("div", { class: "step", id: "st-draft" }, "Generating draft note…"));
    panel.appendChild(steps);

    var url = API + "/visits/" + ctx.visitId + "/stream" + (state.token ? "?token=" + encodeURIComponent(state.token) : "");
    var es = new EventSource(url);
    var done = false;
    function finish(toReview) {
      if (done) return; done = true;
      try { es.close(); } catch (e) {}
      if (toReview) go("/review/" + ctx.visitId);
    }
    es.addEventListener("status", function (ev) {
      var d = {}; try { d = JSON.parse(ev.data); } catch (e) {}
      if (d.stage === "DRAFTING") {
        var st = document.getElementById("st-transcribe"); if (st) st.className = "step done";
        var sd = document.getElementById("st-draft"); if (sd) sd.className = "step active";
      }
    });
    es.addEventListener("result", function () {
      document.querySelectorAll("#st-transcribe,#st-draft").forEach(function (e) { e.className = "step done"; });
      toast("Draft ready — opening review", "ok");
      setTimeout(function () { finish(true); }, 500);
    });
    es.addEventListener("error", function (ev) {
      // SSE "error" event may be the pipeline error OR a transport hiccup.
      var d = null; try { d = ev.data ? JSON.parse(ev.data) : null; } catch (e) {}
      if (d && d.message) { toast("Draft failed: " + d.message, "err"); finish(false);
        panel.appendChild(h("button", { class: "gs-btn", style: { marginTop: "12px" }, onclick: function () { go("/queue"); } }, "← Back to queue")); }
    });
  }

  // ── boot ────────────────────────────────────────────────────────────
  function boot() {
    state.token = resolveToken();
    var header = document.getElementById("gsHeader");
    var nav = document.getElementById("gsNav");
    clear(nav);
    nav.appendChild(h("a", { href: "#/queue" }, "Queue"));
    nav.appendChild(h("a", { href: "#/capture" }, "New capture"));
    header.hidden = false;
    var badge = document.getElementById("gsUserBadge");
    var role = profileRole();
    if (role) badge.textContent = role.replace(/_/g, " ");

    if (!state.token) { renderSignIn(document.getElementById("gsRoot")); return; }
    window.addEventListener("hashchange", render);
    render();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
