/**
 * Archangel Health — clinical reasoning data landing (v3 "console" system).
 * Faithful port of the approved static mockup: light "canvas" theme, green/
 * orange/pink/lime palette, Instrument Sans + IBM Plex Mono + Doto, a full-color
 * glow hero, and the record-anatomy console. Embedded as the SPA home view;
 * self-contained (its own nav + footer).
 *
 * FRONTEND ONLY for now — the nav/CTAs are mailto links exactly as designed.
 * See the component's docblock note + the repo handoff for where auth/data
 * functionality (Sign in / Sign up / doctor portal / signout / #recovery-plan)
 * re-attaches when we wire it back up.
 */

import { useEffect, useRef, useState } from "react";
import "@/styles/clinical-fonts.css";

const MAIL = "aryaabhatia@berkeley.edu";
const mailto = (subject: string) => `mailto:${MAIL}?subject=${encodeURIComponent(subject)}`;

export default function ClinicalDataLanding() {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  /* ---------- scroll reveals ---------- */
  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const revealEls = root.querySelectorAll(".reveal");
    if (reduced || !("IntersectionObserver" in window)) {
      revealEls.forEach((el) => el.classList.add("in"));
      return;
    }
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.isIntersecting) {
            e.target.classList.add("in");
            io.unobserve(e.target);
          }
        }
      },
      { threshold: 0.15, rootMargin: "0px 0px -40px 0px" }
    );
    revealEls.forEach((el) => io.observe(el));
    return () => io.disconnect();
  }, []);

  useEffect(() => {
    return () => {
      if (toastTimer.current) clearTimeout(toastTimer.current);
    };
  }, []);

  // Mail links fail inside sandboxed previews and on machines with no mail app.
  // On any mailto click: copy the address and confirm in a toast, so the button
  // always does something useful.
  const handleMailto = (e: React.MouseEvent<HTMLAnchorElement>) => {
    const email = e.currentTarget.href.replace("mailto:", "").split("?")[0];
    const done = (copied: boolean) => {
      setToast(copied ? `Email copied — ${email}` : `Email us: ${email}`);
      if (toastTimer.current) clearTimeout(toastTimer.current);
      toastTimer.current = setTimeout(() => setToast(null), 4200);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(email).then(
        () => done(true),
        () => done(false)
      );
    } else {
      done(false);
    }
  };

  return (
    <div ref={rootRef} className="arch-landing">
      <header className="nav" id="top">
        <a className="wordmark" href="/" aria-label="Archangel Health — home">
          <svg className="halo" viewBox="0 0 24 24" aria-hidden="true">
            <ellipse cx="12" cy="12" rx="9" ry="5.4" fill="none" stroke="currentColor" strokeWidth="1.7" transform="rotate(-24 12 12)" />
          </svg>
          <span>Archangel&nbsp;Health</span>
        </a>
        <nav className="nav-links" aria-label="Primary">
          <a className="chrome chrome-box hide-sm" href="#findings">The data</a>
          <a className="chrome chrome-box hide-sm" href="#consults">Contributors</a>
          <a className="chrome chrome-box solid" href={mailto("Data request — Archangel Health")} onClick={handleMailto}>
            Request data
          </a>
        </nav>
      </header>

      <main>
        {/* ======================= HERO ======================= */}
        <section className="hero hero-glow">
          <div className="glow-field" aria-hidden="true">
            <i className="glow-a" />
            <i className="glow-b" />
            <span className="doto gn gn1">7.4</span>
            <span className="doto gn gn2">3.1</span>
            <span className="doto gn gn3">106</span>
          </div>
          <div className="chip-float" aria-hidden="true">
            <span className="up">↗</span> Divergence captured
            <span className="smear" />
          </div>
          <div className="hero-inner">
            <p className="chrome chrome-box"><span className="dot dot-lime" />Clinical reasoning data</p>
            <h1>
              <span className="h1-a">The cases frontier models fail.</span>
              <span className="h1-b">The reasoning that resolves them.</span>
            </h1>
            <p className="hero-sub">
              Expert clinical reasoning over real, de-identified, multimodal cases hard enough
              to split board-certified specialists — shipped as preference pairs, ideal answers,
              and step-level reasoning traces.
            </p>
            <div className="hero-ctas">
              <a className="btn btn-primary" href={mailto("Data request — Archangel Health")} onClick={handleMailto}>Request data</a>
              <a className="btn" href={mailto("Becoming a contributor — Archangel Health")} onClick={handleMailto}>Become a contributor</a>
            </div>
          </div>

          <div className="hero-trace" aria-hidden="true">
            <svg viewBox="0 0 1440 190" preserveAspectRatio="none">
              <path className="trace trace-shared" pathLength={1} d="M0 95 H300 l14 -20 14 34 14 -14 H520" />
              <path className="trace trace-green" pathLength={1} d="M520 95 C 660 95, 720 44, 880 38 H1440" />
              <path className="trace trace-green trace-green2" pathLength={1} d="M520 95 C 660 95, 720 70, 880 66 H1440" />
              <path className="trace-orange" d="M520 95 C 660 95, 720 142, 880 150" />
            </svg>
          </div>
        </section>

        {/* ================== 01 PRESENTING PROBLEM ================== */}
        <section className="section" id="problem">
          <p className="crumb chrome reveal"><span className="root">Archangel</span><span className="sep">/</span><span className="here">01 · Presenting problem</span></p>
          <div className="split">
            <div className="reveal">
              <h2>Frontier models pass the easy benchmarks. <span className="quiet">So we build the ones they can’t.</span></h2>
              <p style={{ marginTop: "1.3rem" }}>
                Our cases are hard by construction: the labs and the narrative pull in different
                directions, and the wrong answer looks plausible. On these presentations, frontier
                models fail — and board-certified specialists disagree.
              </p>
              <p className="lede-strong">That disagreement isn’t noise. It’s the most valuable training
              signal in medicine.</p>
            </div>
            <div className="reveal" aria-hidden="true">
              <div className="c-card fig-card">
                <svg viewBox="0 0 560 310" preserveAspectRatio="xMidYMid meet">
                  <path className="fig-grid" d="M0 78 H560 M0 155 H560 M0 232 H560" />
                  <path className="fig-shared" d="M0 155 H160 l14 -26 14 44 14 -18 H260" />
                  <path className="fig-gold" d="M260 155 C 325 155, 345 94, 415 86 H560" />
                  <path className="fig-gold2" d="M260 155 C 325 155, 345 126, 415 122 H560" />
                  <path className="fig-cyan" d="M260 155 C 325 155, 345 220, 405 230" />
                  <circle className="fig-node" cx="260" cy="155" r="5" />
                  <g className="fig-x"><path d="M406 223 l16 16 M422 223 l-16 16" /></g>
                  <text className="fig-t fig-t-gold" x="552" y="72" textAnchor="end">specialist A</text>
                  <text className="fig-t fig-t-gold" x="552" y="144" textAnchor="end">specialist B</text>
                  <text className="fig-t fig-t-cyan" x="414" y="266" textAnchor="middle">frontier model</text>
                  <text className="fig-t fig-t-dim" x="248" y="188" textAnchor="end">divergence</text>
                </svg>
                <p className="fig-caption">One case. Two credentialed opinions. One model failure. All captured.</p>
              </div>
            </div>
          </div>
        </section>

        {/* ================== 02 FINDINGS ================== */}
        <section className="section" id="findings">
          <p className="crumb chrome reveal"><span className="root">Archangel</span><span className="sep">/</span><span className="here">02 · Findings</span></p>
          <div className="reveal">
            <h2>One case, <span className="quiet">four kinds of supervision.</span></h2>
            <p className="section-sub">Every record starts as a real multimodal case — structured labs,
            vitals, and clinical notes, with longitudinal outcomes linked past the decision —
            and ships as schema-validated training data.</p>
          </div>
          <div className="anatomy">
            <figure className="c-card c-case reveal" aria-label="Illustrative sample case record: de-identified nephrology labs, a 90-day linked outcome, and a divergence score of 7.4.">
              <div className="case-head">
                <span className="title">Case record</span>
                <span className="chip chip-lime">de-identified</span>
              </div>
              <div className="modalities" aria-hidden="true">
                <span className="chip">Labs</span>
                <span className="chip">Vitals</span>
                <span className="chip">Notes</span>
                <span className="chip">Outcome</span>
              </div>
              <div className="case-id-row label" aria-hidden="true">
                <span>PT NAME&nbsp;&nbsp;<i className="redact w9" /></span>
                <span>MRN&nbsp;&nbsp;<i className="redact w7" /></span>
                <span>DOB&nbsp;&nbsp;<i className="redact w5" /></span>
              </div>
              <table className="labs" aria-hidden="true">
                <tbody>
                  <tr><td>Creatinine</td><td className="val">3.1</td><td className="ref">0.7–1.3</td><td className="fl fl-h">H ▲</td></tr>
                  <tr><td>Potassium</td><td className="val">5.9</td><td className="ref">3.5–5.0</td><td className="fl fl-h">H ▲</td></tr>
                  <tr><td>Bicarbonate</td><td className="val">16</td><td className="ref">22–29</td><td className="fl fl-l">L ▼</td></tr>
                  <tr><td>Hemoglobin</td><td className="val">9.4</td><td className="ref">13.5–17.5</td><td className="fl fl-l">L ▼</td></tr>
                </tbody>
              </table>
              <div className="record-score" aria-hidden="true">
                <span className="chrome">Divergence</span>
                <span className="doto">7.4</span>
                <span className="label">two specialists split · model failed</span>
              </div>
              <div className="outcome-row" aria-hidden="true">
                <span className="chrome">Outcome · 90d</span>
                <span>Dialysis avoided — renal function recovered.</span>
              </div>
              <div className="case-attest" aria-hidden="true">
                Reviewed — board-certified specialist
                <span className="dot dot-green" />
              </div>
            </figure>

            <div className="derives reveal" role="list">
              <div className="derive" role="listitem">
                <span className="chrome chrome-box">RLHF · DPO</span>
                <h3>Preference pair</h3>
                <p>The chosen answer against the plausible hard-negative a good model actually produces.</p>
              </div>
              <div className="derive" role="listitem">
                <span className="chrome chrome-box">SFT</span>
                <h3>Ideal answer</h3>
                <p>The complete expert resolution, written to be learned from.</p>
              </div>
              <div className="derive" role="listitem">
                <span className="chrome chrome-box">PRM</span>
                <h3>Reasoning trace</h3>
                <p>Step-level expert reasoning — exactly where a model’s chain goes wrong, and why.</p>
              </div>
              <div className="derive" role="listitem">
                <span className="chrome chrome-box">Provenance</span>
                <h3>Full lineage</h3>
                <p>Credentials, citations, difficulty score, versioning. Every record answers for itself.</p>
              </div>
            </div>
          </div>

          <div className="c-card c-pipe reveal" aria-hidden="true">
            <span className="label">Case pipeline</span>
            <div className="pipe-track">
              <div className="pipe-stop"><span className="dot dot-faint" /><span className="label">Intake</span></div>
              <span className="seg" />
              <div className="pipe-stop"><span className="dot dot-orange" /><span className="label">Model probe</span></div>
              <span className="seg" />
              <div className="pipe-stop"><span className="dot dot-green" /><span className="label">Specialists ×2</span></div>
              <span className="seg" />
              <div className="pipe-stop"><span className="dot dot-green" /><span className="label">Adjudicated</span></div>
              <span className="seg" />
              <div className="pipe-stop"><span className="dot dot-faint" /><span className="label">Outcome linked</span></div>
              <span className="seg" />
              <div className="pipe-stop"><span className="dot dot-faint" /><span className="label">Shipped</span></div>
            </div>
          </div>
          <p className="stage-caption reveal">Illustrative sample record — every real record ships
          de-identified, schema-validated, with full provenance.</p>
        </section>

        {/* ================== 03 ASSESSMENT ================== */}
        <section className="section" id="assessment">
          <p className="crumb chrome reveal"><span className="root">Archangel</span><span className="sep">/</span><span className="here">03 · Assessment</span></p>
          <div className="split">
            <div className="reveal">
              <h2>We don’t claim difficulty. <span className="quiet">We measure it.</span></h2>
              <p style={{ marginTop: "1.3rem" }}>
                Every case is scored against frontier models with our own rubric.
                Data that doesn’t move the frontier doesn’t ship.
              </p>
              <div className="aura aura-expert stat-card">
                <span className="doto">6+<span className="of">/10</span></span>
                <p>of the most commonly used medical benchmarks improve when models train on our data.</p>
              </div>
            </div>
            <div className="qc reveal">
              <p className="qc-title chrome">Quality report — every record</p>
              <ul className="qc-list">
                <li><span className="dot dot-green" />Difficulty scored against frontier models</li>
                <li><span className="dot dot-green" />Contamination-checked against public benchmarks</li>
                <li><span className="dot dot-green" />Guideline-grounded, with citations</li>
                <li><span className="dot dot-pink" />No PHI — context-preserving de-identification</li>
                <li><span className="dot dot-green" />Watermarked &amp; traceable, licensed per end-buyer</li>
                <li><span className="dot dot-green" />IP-cleared, contributor credentials verified</li>
              </ul>
            </div>
          </div>
        </section>

        {/* ================== 04 CONSULTS ================== */}
        <section className="section" id="consults">
          <p className="crumb chrome reveal"><span className="root">Archangel</span><span className="sep">/</span><span className="here">04 · Consults</span></p>
          <div className="reveal">
            <h2>A network of specialists, <span className="quiet">paid for their judgment.</span></h2>
          </div>
          <div className="network">
            <div className="net-panel reveal">
              <span className="chrome chrome-box">For physicians</span>
              <h3>Your reasoning is the product.</h3>
              <p>Work through hard cases — annotate the reasoning, ratify the ground truth, flag
              where models go wrong — and get paid for the judgment only you can supply.</p>
            </div>
            <div className="net-panel reveal">
              <span className="chrome chrome-box">For labs &amp; health-AI teams</span>
              <h3>Datasets, spun up on demand.</h3>
              <p>Specialty, modality, format, difficulty — scoped to your model and your gap.
              Longitudinal cases follow the patient past the decision, linking case,
              intervention, and real outcome.</p>
            </div>
          </div>
        </section>

        {/* ================== STATEMENT ================== */}
        <section className="statement">
          <p className="statement-line reveal">
            Doctors earn from their judgment.<br />
            Models learn from it.<br />
            <span className="quiet">The hardest cases become the most valuable data.</span>
          </p>
        </section>

        {/* ================== 05 PLAN ================== */}
        <section className="section" id="plan">
          <p className="crumb chrome reveal"><span className="root">Archangel</span><span className="sep">/</span><span className="here">05 · Plan</span></p>
          <div className="reveal"><h2>Three ways in.</h2></div>
          <div className="doors reveal">
            <a className="door" href={mailto("Becoming a contributor — Archangel Health")} onClick={handleMailto}>
              <span className="door-for"><span className="dot dot-green" /><span className="chrome">Physicians</span></span>
              <span className="door-title">Become a contributor</span>
              <span className="door-sub">Reason through hard cases. Get paid for your judgment.</span>
              <span className="door-arrow" aria-hidden="true">→</span>
            </a>
            <a className="door" href={mailto("Data request — Archangel Health")} onClick={handleMailto}>
              <span className="door-for"><span className="dot dot-orange" /><span className="chrome">Labs &amp; health-AI teams</span></span>
              <span className="door-title">Request data</span>
              <span className="door-sub">Scoped samples, fitted pilots, bespoke datasets.</span>
              <span className="door-arrow" aria-hidden="true">→</span>
            </a>
            <a className="door" href={mailto("Providing de-identified data — Archangel Health")} onClick={handleMailto}>
              <span className="door-for"><span className="dot dot-pink" /><span className="chrome">Health systems &amp; software</span></span>
              <span className="door-title">Provide your data</span>
              <span className="door-sub">We buy de-identified clinical data from the organizations and software that hold it.</span>
              <span className="door-arrow" aria-hidden="true">→</span>
            </a>
          </div>
          <p className="doors-note reveal">Something else in mind? <a href={mailto("Partnership — Archangel Health")} onClick={handleMailto}>Other partnerships &amp; collaborations →</a></p>
        </section>
      </main>

      <footer className="footer">
        <div className="foot-left">
          <span className="foot-mark">Archangel Health</span>
          <span className="label">Berkeley, California</span>
        </div>
        <div className="foot-right">
          <a href={`mailto:${MAIL}`} onClick={handleMailto}>{MAIL}</a>
        </div>
        <p className="foot-line chrome">Real · De-identified · IP-cleared · Never resold beyond license</p>
      </footer>

      <div className={`toast${toast ? " show" : ""}`} role="status">{toast}</div>

      <style>{styles}</style>
    </div>
  );
}

const styles = `
/* ============================================================
   Archangel Health — v3 "console" system
   Laws: air is the design · scale not boldness · zero black
   fills · gradients only as blurred auras · mono chrome = wayfinding.
   All rules scoped under .arch-landing so the console theme cannot
   leak into the shared header, dialogs, or other landing views.
   ============================================================ */

html { scroll-behavior: smooth; }

.arch-landing {
  --canvas: #eef0ef;
  --card: #fbfcfa;
  --card-in: #f4f5f3;
  --hairline: rgba(26, 27, 26, 0.08);
  --ink: #1a1b1a;
  --ink-soft: #5c5e5a;
  --ink-faint: #8b8d89;
  --green: #4ca63c;
  --orange: #ec9440;
  --pink: #e8447b;
  --lime: #d5e14e;
  --r-chip: 999px;
  --r-sm: 18px;
  --r-md: 28px;
  --r-lg: 36px;
  --r-xl: 44px;
  --shadow-card: 0 1px 2px rgba(26, 27, 26, 0.03);
  --shadow-float: 0 24px 60px -36px rgba(26, 27, 26, 0.28);
  --sans: 'Instrument Sans', system-ui, -apple-system, sans-serif;
  --mono: 'IBM Plex Mono', ui-monospace, monospace;
  --doto: 'Doto', monospace;
  --pagepad: clamp(1.25rem, 4vw, 2.5rem);
  --measure: 34rem;

  position: relative;
  min-height: 100vh;
  background: var(--canvas);
  color: var(--ink);
  font-family: var(--sans);
  font-size: 1rem;
  font-weight: 400;
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
  overflow-x: clip;
}

.arch-landing *, .arch-landing *::before, .arch-landing *::after { margin: 0; padding: 0; box-sizing: border-box; }

/* page atmosphere — felt, not seen */
.arch-landing::before {
  content: '';
  position: fixed;
  inset: 0;
  z-index: 0;
  pointer-events: none;
  background:
    radial-gradient(56rem 40rem at 12% -6%, rgba(76, 166, 60, 0.055), transparent 70%),
    radial-gradient(52rem 44rem at 96% 44%, rgba(236, 148, 64, 0.05), transparent 70%),
    radial-gradient(40rem 34rem at 50% 108%, rgba(232, 68, 123, 0.028), transparent 70%);
}
.arch-landing > * { position: relative; z-index: 1; }

.arch-landing a { color: inherit; text-decoration: none; }
.arch-landing p a, .arch-landing .foot-right a { text-decoration: underline; text-underline-offset: 3px; text-decoration-color: var(--hairline); }

.arch-landing ::selection { background: var(--lime); color: var(--ink); }

.arch-landing :focus-visible {
  outline: 2px solid var(--ink);
  outline-offset: 3px;
  border-radius: 8px;
}

/* ---------- type ---------- */

.arch-landing h1, .arch-landing h2, .arch-landing h3 {
  font-weight: 400;
  letter-spacing: -0.015em;
  line-height: 1.12;
  text-wrap: balance;
}

.arch-landing h1 { font-size: clamp(2.2rem, 4.4vw, 3.5rem); }
.arch-landing h2 { font-size: clamp(1.7rem, 3vw, 2.5rem); }
.arch-landing h3 { font-size: 1.1rem; font-weight: 500; letter-spacing: 0; }

.arch-landing .quiet { color: var(--ink-faint); }

.arch-landing p { color: var(--ink-soft); max-width: var(--measure); }

.arch-landing .label {
  font-size: 0.78rem;
  color: var(--ink-faint);
  letter-spacing: 0.01em;
}

/* mono instrument chrome */
.arch-landing .chrome {
  font-family: var(--mono);
  font-size: 0.68rem;
  font-weight: 400;
  letter-spacing: 0.09em;
  text-transform: uppercase;
  color: var(--ink-soft);
}

.arch-landing .chrome-box {
  display: inline-flex;
  align-items: center;
  gap: 0.55em;
  padding: 0.62em 1.05em;
  border: 1px solid rgba(26, 27, 26, 0.16);
  border-radius: 7px;
  background: transparent;
  transition: background 0.2s ease, border-color 0.2s ease;
}
.arch-landing .chrome-box:hover { background: var(--card); border-color: rgba(26, 27, 26, 0.3); }
.arch-landing .chrome-box.solid { border-color: var(--ink); }

/* dotted data values — thin, huge, data only */
.arch-landing .doto {
  font-family: var(--doto);
  font-weight: 400;
  font-variation-settings: 'ROND' 100;
  letter-spacing: 0.04em;
  line-height: 1;
}

/* status dots */
.arch-landing .dot {
  display: inline-block;
  width: 7px; height: 7px;
  border-radius: 50%;
  flex: none;
}
.arch-landing .dot-green { background: var(--green); }
.arch-landing .dot-orange { background: var(--orange); }
.arch-landing .dot-pink { background: var(--pink); }
.arch-landing .dot-faint { background: rgba(26, 27, 26, 0.18); }

/* chips & pills */
.arch-landing .chip {
  display: inline-flex;
  align-items: center;
  gap: 0.5em;
  padding: 0.45em 1em;
  border-radius: var(--r-chip);
  background: var(--card);
  border: 1px solid var(--hairline);
  font-size: 0.8rem;
  color: var(--ink-soft);
}
.arch-landing .chip-lime {
  background: var(--lime);
  border-color: transparent;
  color: var(--ink);
  font-weight: 500;
}

.arch-landing .btn {
  display: inline-flex;
  align-items: center;
  gap: 0.6em;
  padding: 0.78em 1.5em;
  border-radius: var(--r-chip);
  background: var(--card);
  border: 1px solid var(--hairline);
  font-size: 0.95rem;
  font-weight: 500;
  color: var(--ink);
  transition: transform 0.2s ease, box-shadow 0.2s ease, border-color 0.2s ease;
}
.arch-landing .btn:hover { transform: translateY(-1px); box-shadow: var(--shadow-float); }
.arch-landing .btn-primary { border-color: rgba(26, 27, 26, 0.55); }

/* aura cards — the only place gradients exist. */
.arch-landing .aura {
  position: relative;
  overflow: hidden;
  border-radius: var(--r-md);
  isolation: isolate;
}
.arch-landing .aura::after {
  content: '';
  position: absolute;
  inset: 0;
  z-index: -1;
  background: radial-gradient(105% 105% at 50% 46%, transparent 52%, rgba(255, 255, 255, 0.28));
}
.arch-landing .aura-diverge {
  background:
    radial-gradient(120% 55% at 46% -10%, rgba(238, 158, 74, 0.95), rgba(238, 158, 74, 0) 64%),
    radial-gradient(80% 40% at 60% 26%, rgba(244, 196, 130, 0.5), transparent 70%),
    radial-gradient(150% 100% at 50% 115%, #43a137 30%, rgba(76, 166, 60, 0) 92%),
    linear-gradient(180deg, #ecbc80, #a8b95e 42%, #6aa94b 68%, #4ca63c);
}
.arch-landing .aura-model {
  background:
    radial-gradient(90% 80% at 52% 42%, #ec9440 8%, rgba(236, 148, 64, 0.45) 58%, rgba(246, 205, 158, 0.9) 100%),
    linear-gradient(180deg, #f2c894, #eaa45c);
}
.arch-landing .aura-expert {
  background:
    radial-gradient(85% 60% at 78% 100%, rgba(216, 232, 170, 0.4), transparent 70%),
    radial-gradient(120% 100% at 40% -10%, #3c9a31 20%, rgba(76, 166, 60, 0) 90%),
    linear-gradient(168deg, #529f3e, #6fae52 62%, #7db863);
}
.arch-landing .aura-flag {
  background:
    radial-gradient(90% 80% at 50% 44%, #e8447b 6%, rgba(232, 68, 123, 0.5) 60%, rgba(244, 168, 197, 0.9) 100%),
    linear-gradient(180deg, #f08bb2, #e75d8d);
}

/* micro-dataviz */
.arch-landing .spark { display: flex; align-items: flex-end; gap: 5px; height: 26px; }
.arch-landing .spark i {
  width: 3px;
  border-radius: 2px;
  background-image: radial-gradient(circle, currentColor 1.2px, transparent 1.4px);
  background-size: 3px 5px;
  background-repeat: repeat-y;
  opacity: 0.85;
}

.arch-landing .ticks {
  height: 12px;
  background-image: linear-gradient(to right, currentColor 1px, transparent 1px);
  background-size: 9px 100%;
  opacity: 0.22;
  border-radius: 1px;
}

/* ---------- reveals ---------- */

.arch-landing .reveal { opacity: 0; transform: translateY(20px); transition: opacity 0.7s ease, transform 0.7s cubic-bezier(0.2, 0.7, 0.2, 1); }
.arch-landing .reveal.in { opacity: 1; transform: none; }

/* ---------- nav ---------- */

.arch-landing .nav {
  position: sticky;
  top: 0;
  z-index: 40;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  padding: 1rem var(--pagepad);
  background: rgba(238, 240, 239, 0.72);
  backdrop-filter: blur(22px) saturate(1.5);
  -webkit-backdrop-filter: blur(22px) saturate(1.5);
  border-bottom: 1px solid var(--hairline);
}

.arch-landing .wordmark {
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
  font-weight: 500;
  font-size: 1.02rem;
  letter-spacing: -0.01em;
}
.arch-landing .wordmark .halo { width: 21px; height: 21px; color: var(--green); }

.arch-landing .nav-links { display: flex; align-items: center; gap: 0.6rem; }

/* ---------- hero ---------- */

.arch-landing .hero {
  position: relative;
  padding: clamp(3.5rem, 8vh, 6rem) var(--pagepad) 0;
  text-align: center;
}

.arch-landing .hero-glow {
  color: #fff;
  padding-bottom: clamp(4.5rem, 10vh, 7.5rem);
  overflow: clip;
  isolation: isolate;
}

.arch-landing .glow-field {
  position: absolute;
  inset: 0;
  z-index: -2;
  pointer-events: none;
}
.arch-landing .glow-field::before {
  content: '';
  position: absolute;
  inset: 0;
  background: linear-gradient(178deg,
    #d28d41 0%, #c3924a 24%, #87a94e 54%, #4ca63c 82%, #47a039 100%);
}
.arch-landing .glow-a, .arch-landing .glow-b {
  position: absolute;
  border-radius: 50%;
  filter: blur(80px) saturate(1.15);
  will-change: transform;
}
.arch-landing .glow-a {
  width: 72vw; height: 62vh;
  left: -12%; top: -20%;
  background: radial-gradient(closest-side, rgba(240, 162, 76, 0.95), transparent 72%);
  animation: arch-glow-drift-a 24s ease-in-out infinite alternate;
}
.arch-landing .glow-b {
  width: 78vw; height: 66vh;
  right: -14%; bottom: -22%;
  background: radial-gradient(closest-side, rgba(62, 156, 46, 0.9), transparent 72%);
  animation: arch-glow-drift-b 30s ease-in-out infinite alternate;
}
@keyframes arch-glow-drift-a {
  from { transform: translate3d(-3%, -2%, 0) rotate(0deg) scale(1); }
  to { transform: translate3d(6%, 5%, 0) rotate(6deg) scale(1.12); }
}
@keyframes arch-glow-drift-b {
  from { transform: translate3d(3%, 2%, 0) rotate(0deg) scale(1.08); }
  to { transform: translate3d(-6%, -4%, 0) rotate(-7deg) scale(0.96); }
}
.arch-landing .gn {
  position: absolute;
  color: #fff;
  font-size: clamp(6rem, 12vw, 11rem);
}
.arch-landing .gn1 { left: 4%; top: 16%; opacity: 0.2; filter: blur(6px); }
.arch-landing .gn2 { right: 5%; top: 42%; opacity: 0.13; filter: blur(11px); }
.arch-landing .gn3 { left: 16%; bottom: 4%; opacity: 0.1; filter: blur(13px); }
.arch-landing .glow-field::after {
  content: '';
  position: absolute;
  inset: 0;
  background:
    radial-gradient(56rem 30rem at 50% 32%, rgba(52, 58, 26, 0.17), transparent 70%),
    linear-gradient(180deg, transparent 70%, var(--canvas) 98%);
}

/* white-on-glow component variants */
.arch-landing .hero-glow .chrome-box {
  border-color: rgba(255, 255, 255, 0.55);
  color: rgba(255, 255, 255, 0.95);
  background: rgba(255, 255, 255, 0.1);
}
.arch-landing .dot-lime { background: var(--lime); }
.arch-landing .hero-glow .h1-b { color: rgba(255, 255, 255, 0.8); }
.arch-landing .hero-glow .hero-sub { color: rgba(255, 255, 255, 0.93); }
.arch-landing .hero-glow .btn {
  border-color: transparent;
  box-shadow: 0 14px 34px -20px rgba(30, 46, 14, 0.55);
}
.arch-landing .hero-glow .btn-primary { border-color: rgba(26, 27, 26, 0.4); }
.arch-landing .hero-glow .chip-float {
  top: clamp(6.5rem, 15vh, 10rem);
  right: 7%;
  color: var(--ink);
  background: rgba(251, 252, 250, 0.95);
  border-color: rgba(255, 255, 255, 0.9);
}
.arch-landing .hero-glow .trace-shared { stroke: rgba(255, 255, 255, 0.5); }
.arch-landing .hero-glow .trace-green { stroke: rgba(255, 255, 255, 0.95); }
.arch-landing .hero-glow .trace-orange { stroke: rgba(255, 255, 255, 0.7); }

.arch-landing .hero-trace {
  position: relative;
  z-index: -1;
  margin: 1.6rem calc(-1 * var(--pagepad)) -3rem;
  pointer-events: none;
  opacity: 0.55;
}
.arch-landing .hero-trace svg { width: 100%; height: clamp(90px, 13vw, 190px); display: block; }
.arch-landing .trace {
  fill: none;
  stroke-width: 1.4;
  stroke-linecap: round;
  stroke-dasharray: 1;
  stroke-dashoffset: 0;
  animation: arch-trace-draw 11s cubic-bezier(0.4, 0, 0.3, 1) infinite;
}
.arch-landing .trace-shared { stroke: rgba(26, 27, 26, 0.35); }
.arch-landing .trace-green { stroke: var(--green); animation-delay: 0.9s; }
.arch-landing .trace-green2 { opacity: 0.5; animation-delay: 1.15s; }
.arch-landing .trace-orange {
  fill: none;
  stroke: var(--orange);
  stroke-width: 1.4;
  stroke-linecap: round;
  stroke-dasharray: 1 7;
  animation: arch-trace-fade 11s ease-in-out infinite;
  animation-delay: 1.4s;
}
@keyframes arch-trace-draw {
  0% { stroke-dashoffset: 1; opacity: 0; }
  6% { opacity: 1; }
  38% { stroke-dashoffset: 0; }
  78% { stroke-dashoffset: 0; opacity: 1; }
  92%, 100% { stroke-dashoffset: 0; opacity: 0; }
}
@keyframes arch-trace-fade {
  0%, 10% { opacity: 0; }
  34%, 78% { opacity: 1; }
  92%, 100% { opacity: 0; }
}

.arch-landing .hero-inner { max-width: 52rem; margin: 0 auto; }

.arch-landing .hero .chrome-box { margin-bottom: 1.6rem; }
.arch-landing .hero .chrome-box .dot { width: 6px; height: 6px; }

.arch-landing .h1-b { display: block; color: var(--ink-faint); }

.arch-landing .hero-sub {
  margin: 1.5rem auto 0;
  font-size: 1.06rem;
  max-width: 36rem;
}

.arch-landing .hero-ctas {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 0.8rem;
  margin-top: 2.1rem;
}

/* ---------- record anatomy (02) ---------- */

.arch-landing .anatomy {
  display: grid;
  grid-template-columns: 0.95fr 1.05fr;
  gap: 1rem;
  align-items: start;
  margin-top: clamp(1.8rem, 4vh, 2.6rem);
}
.arch-landing .anatomy .derives { grid-template-columns: repeat(2, 1fr); margin-top: 0; }
.arch-landing .anatomy + .c-pipe { margin-top: 1rem; }

.arch-landing .record-score {
  display: flex;
  align-items: center;
  gap: 0.8rem;
  padding: 0.55em 0.9em;
  border-radius: var(--r-sm);
  background: var(--card-in);
}
.arch-landing .record-score .chrome { font-size: 0.58rem; color: var(--ink-faint); }
.arch-landing .record-score .doto { font-size: 1.5rem; }
.arch-landing .record-score .label { font-size: 0.7rem; }

.arch-landing .modalities { display: flex; flex-wrap: wrap; gap: 0.4rem; }
.arch-landing .modalities .chip { font-size: 0.72rem; padding: 0.35em 0.85em; background: var(--card-in); border-color: transparent; }

.arch-landing .stage-caption {
  margin-top: 1.1rem;
  text-align: center;
  font-size: 0.78rem;
  color: var(--ink-faint);
}

/* sidebar / console (kept for parity with the design system) */
.arch-landing .console-grid { display: grid; grid-template-columns: 218px 1fr; gap: 1rem; }
.arch-landing .c-side { display: flex; flex-direction: column; gap: 0.45rem; }
.arch-landing .side-row {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  padding: 0.68em 0.7em 0.68em 1em;
  border-radius: var(--r-chip);
  background: var(--card-in);
  font-size: 0.86rem;
  color: var(--ink-soft);
}
.arch-landing .side-row .badge {
  margin-left: auto;
  font-size: 0.68rem;
  color: var(--ink-faint);
  background: var(--card);
  border: 1px solid var(--hairline);
  border-radius: var(--r-chip);
  padding: 0.28em 0.7em;
}
.arch-landing .side-row.active { background: var(--card); border: 1px solid var(--hairline); color: var(--ink); }
.arch-landing .side-foot { margin-top: auto; padding: 0.9rem 1rem 0.2rem; }

.arch-landing .c-main { display: grid; gap: 1rem; }
.arch-landing .c-row1 { display: grid; grid-template-columns: 1.05fr 1.3fr 0.95fr; gap: 1rem; }

.arch-landing .c-card {
  background: var(--card);
  border: 1px solid var(--hairline);
  border-radius: var(--r-md);
  padding: 1.15rem 1.25rem;
  box-shadow: var(--shadow-card);
}

.arch-landing .c-score {
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  padding: 1.4rem 1.2rem 1.2rem;
  color: #fff;
  border: none;
}
.arch-landing .c-score .label { color: rgba(255, 255, 255, 0.85); }
.arch-landing .c-score .doto { font-size: clamp(3.4rem, 5vw, 4.6rem); margin: 1.1rem 0 0.35rem; color: #fff; }
.arch-landing .c-score .sub { font-size: 0.74rem; color: rgba(255, 255, 255, 0.85); }
.arch-landing .c-score .spark { margin-top: auto; padding-top: 1.2rem; color: #fff; }

/* case record */
.arch-landing .c-case { display: flex; flex-direction: column; gap: 0.7rem; }
.arch-landing .case-head { display: flex; align-items: center; justify-content: space-between; gap: 0.6rem; }
.arch-landing .case-head .title { font-weight: 500; font-size: 0.92rem; }
.arch-landing .case-head .chip-lime { font-size: 0.66rem; padding: 0.3em 0.85em; }

.arch-landing .redact {
  display: inline-block;
  height: 0.66em;
  border-radius: 4px;
  background: var(--card-in);
  vertical-align: baseline;
}
.arch-landing .w5 { width: 2.6em; } .arch-landing .w7 { width: 3.6em; } .arch-landing .w9 { width: 4.8em; }

.arch-landing .case-id-row { display: flex; gap: 1.1rem; flex-wrap: wrap; }

.arch-landing .labs { width: 100%; border-collapse: collapse; }
.arch-landing .labs td {
  padding: 0.42em 0;
  border-top: 1px solid var(--hairline);
  font-size: 0.8rem;
  color: var(--ink-soft);
}
.arch-landing .labs td.val { font-family: var(--doto); font-variation-settings: 'ROND' 100; font-weight: 500; font-size: 0.95rem; color: var(--ink); }
.arch-landing .labs td.ref { color: var(--ink-faint); font-size: 0.72rem; }
.arch-landing .labs td.fl { text-align: right; font-size: 0.68rem; letter-spacing: 0.04em; }
.arch-landing .fl-h { color: var(--orange); } .arch-landing .fl-l { color: var(--pink); }

.arch-landing .outcome-row {
  display: flex;
  align-items: baseline;
  gap: 0.7rem;
  padding: 0.6em 0.9em;
  border-radius: var(--r-sm);
  background: var(--card-in);
  font-size: 0.76rem;
  color: var(--ink-soft);
}
.arch-landing .outcome-row .chrome { font-size: 0.58rem; white-space: nowrap; color: var(--ink-faint); }

.arch-landing .case-attest {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  margin-top: auto;
  padding-top: 0.4rem;
  font-size: 0.72rem;
  color: var(--ink-faint);
}
.arch-landing .case-attest .dot { margin-left: auto; }

/* supervision outputs */
.arch-landing .c-outputs { display: flex; flex-direction: column; gap: 0.55rem; }
.arch-landing .c-outputs .head { font-weight: 500; font-size: 0.92rem; margin-bottom: 0.2rem; }
.arch-landing .out-row {
  display: flex;
  align-items: center;
  gap: 0.7rem;
  padding: 0.6em 0.9em;
  border-radius: var(--r-sm);
  background: var(--card-in);
  font-size: 0.8rem;
  color: var(--ink-soft);
}
.arch-landing .out-row .chrome { font-size: 0.6rem; color: var(--ink-faint); min-width: 4.6em; }
.arch-landing .out-row .ok { margin-left: auto; color: var(--green); font-size: 0.78rem; }

/* pipeline strip */
.arch-landing .c-pipe {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.95rem 1.25rem;
}
.arch-landing .c-pipe .label { margin-right: 0.8rem; white-space: nowrap; }
.arch-landing .pipe-track { display: flex; align-items: center; gap: 0.4rem; flex: 1; min-width: 0; }
.arch-landing .pipe-track .seg { flex: 1; height: 1px; background: var(--hairline); }
.arch-landing .pipe-stop { display: flex; flex-direction: column; align-items: center; gap: 0.4rem; }
.arch-landing .pipe-stop .label { margin: 0; font-size: 0.62rem; white-space: nowrap; }

/* floating glass chip */
.arch-landing .chip-float {
  position: absolute;
  z-index: 3;
  top: -1.1rem;
  right: 6%;
  display: inline-flex;
  align-items: center;
  gap: 0.55em;
  padding: 0.7em 1.2em;
  border-radius: var(--r-chip);
  background: rgba(251, 252, 250, 0.86);
  backdrop-filter: blur(10px);
  -webkit-backdrop-filter: blur(10px);
  border: 1px solid rgba(255, 255, 255, 0.7);
  box-shadow: var(--shadow-float);
  font-size: 0.84rem;
  font-weight: 500;
  transform: rotate(2deg);
  animation: arch-drift 9s ease-in-out infinite;
  overflow: hidden;
  isolation: isolate;
}
.arch-landing .chip-float .up { color: var(--green); }
.arch-landing .chip-float .smear {
  position: absolute;
  right: 8%;
  bottom: 12%;
  z-index: -1;
  width: 22px; height: 7px;
  border-radius: 50%;
  background: linear-gradient(90deg, #f6b, #fd8, #8e8, #8cf);
  filter: blur(6px);
  opacity: 0.35;
  pointer-events: none;
}

@keyframes arch-drift {
  0%, 100% { transform: rotate(2deg) translateY(0); }
  50% { transform: rotate(1.2deg) translateY(-5px); }
}

/* ---------- sections ---------- */

.arch-landing .section {
  max-width: 1180px;
  margin: 0 auto;
  padding: clamp(4.5rem, 10vh, 7.5rem) var(--pagepad) 0;
}

.arch-landing .crumb {
  display: flex;
  align-items: baseline;
  gap: 0.7em;
  padding-bottom: 0.8rem;
  margin-bottom: clamp(1.8rem, 4vh, 2.8rem);
  border-bottom: 1px solid var(--hairline);
}
.arch-landing .crumb .sep { color: var(--ink-faint); opacity: 0.5; }
.arch-landing .crumb .here { color: var(--ink); }
.arch-landing .crumb .root { color: var(--ink-faint); }

.arch-landing .section h2 .quiet { display: block; }

.arch-landing .section-sub { margin-top: 1rem; }

.arch-landing .split {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: clamp(2rem, 5vw, 4.5rem);
  align-items: start;
}

.arch-landing .lede-strong { margin-top: 1.2rem; color: var(--ink); }

/* divergence figure */
.arch-landing .fig-card { padding: 1.8rem 1.6rem 1.2rem; border-radius: var(--r-md); }
.arch-landing .fig-card svg { width: 100%; height: auto; display: block; }
.arch-landing .fig-grid { stroke: var(--hairline); stroke-width: 1; }
.arch-landing .fig-shared { stroke: rgba(26, 27, 26, 0.35); stroke-width: 1.4; fill: none; }
.arch-landing .fig-gold, .arch-landing .fig-gold2 { stroke: var(--green); stroke-width: 1.6; fill: none; }
.arch-landing .fig-gold2 { opacity: 0.55; }
.arch-landing .fig-cyan { stroke: var(--orange); stroke-width: 1.6; fill: none; stroke-dasharray: 1 6; stroke-linecap: round; }
.arch-landing .fig-node { fill: var(--card); stroke: var(--ink); stroke-width: 1.2; }
.arch-landing .fig-x path { stroke: var(--pink); stroke-width: 1.8; stroke-linecap: round; }
.arch-landing .fig-t { font-family: var(--mono); font-size: 11px; letter-spacing: 0.06em; text-transform: uppercase; }
.arch-landing .fig-t-gold { fill: var(--green); }
.arch-landing .fig-t-cyan { fill: var(--orange); }
.arch-landing .fig-t-dim { fill: var(--ink-faint); }
.arch-landing .fig-caption { margin: 1rem 0 0; font-size: 0.78rem; color: var(--ink-faint); }

/* supervision formats */
.arch-landing .derives {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 1rem;
  margin-top: clamp(1.8rem, 4vh, 2.6rem);
}
.arch-landing .derive {
  background: var(--card);
  border: 1px solid var(--hairline);
  border-radius: var(--r-md);
  padding: 1.5rem 1.4rem 1.7rem;
  box-shadow: var(--shadow-card);
  display: flex;
  flex-direction: column;
  gap: 0.9rem;
}
.arch-landing .derive .chrome-box { align-self: flex-start; font-size: 0.6rem; padding: 0.5em 0.9em; }
.arch-landing .derive p { font-size: 0.88rem; }

/* assessment */
.arch-landing .stat-card {
  margin-top: 1.8rem;
  padding: 1.7rem 1.6rem 1.5rem;
  color: #fff;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 0.4rem;
  max-width: 24rem;
}
.arch-landing .stat-card .doto { font-size: clamp(3.2rem, 5vw, 4.4rem); }
.arch-landing .stat-card .of { font-size: 0.5em; opacity: 0.85; letter-spacing: 0.02em; }
.arch-landing .stat-card p { color: rgba(255, 255, 255, 0.92); font-size: 0.88rem; max-width: 17rem; }

.arch-landing .qc {
  background: var(--card);
  border: 1px solid var(--hairline);
  border-radius: var(--r-md);
  padding: 1.6rem 1.7rem;
  box-shadow: var(--shadow-card);
}
.arch-landing .qc-title { margin-bottom: 0.9rem; }
.arch-landing .qc-list { list-style: none; }
.arch-landing .qc-list li {
  display: flex;
  align-items: center;
  gap: 0.8rem;
  padding: 0.72em 0.2em;
  border-top: 1px solid var(--hairline);
  font-size: 0.9rem;
  color: var(--ink-soft);
}
.arch-landing .qc-list li:first-child { border-top: none; }

/* consults */
.arch-landing .network {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1rem;
  margin-top: clamp(1.8rem, 4vh, 2.6rem);
}
.arch-landing .net-panel {
  background: var(--card);
  border: 1px solid var(--hairline);
  border-radius: var(--r-md);
  padding: 1.9rem 1.8rem 2.1rem;
  box-shadow: var(--shadow-card);
}
.arch-landing .net-panel .chrome-box { font-size: 0.6rem; margin-bottom: 1.3rem; }
.arch-landing .net-panel h3 { margin-bottom: 0.7rem; font-size: 1.25rem; }
.arch-landing .net-panel p { font-size: 0.93rem; }

/* statement */
.arch-landing .statement {
  max-width: 1180px;
  margin: 0 auto;
  padding: clamp(5rem, 12vh, 8rem) var(--pagepad) 0;
  text-align: center;
}
.arch-landing .statement-line {
  font-size: clamp(1.5rem, 3vw, 2.3rem);
  line-height: 1.35;
  letter-spacing: -0.01em;
  color: var(--ink);
  max-width: none;
}
.arch-landing .statement-line .quiet { display: block; }

/* doors */
.arch-landing .doors { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem; margin-top: clamp(1.8rem, 4vh, 2.6rem); }
.arch-landing .door {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 0.8rem;
  background: var(--card);
  border: 1px solid var(--hairline);
  border-radius: var(--r-md);
  padding: 1.7rem 1.6rem 1.9rem;
  box-shadow: var(--shadow-card);
  transition: transform 0.2s ease, box-shadow 0.2s ease;
}
.arch-landing .door:hover { transform: translateY(-2px); box-shadow: var(--shadow-float); }
.arch-landing .door-for { display: flex; align-items: center; gap: 0.6rem; }
.arch-landing .door-title { font-size: 1.2rem; font-weight: 500; letter-spacing: -0.01em; }
.arch-landing .door-sub { font-size: 0.88rem; color: var(--ink-soft); max-width: 22rem; }
.arch-landing .door-arrow {
  margin-top: auto;
  align-self: flex-end;
  width: 34px; height: 34px;
  border-radius: 50%;
  background: var(--card-in);
  display: grid;
  place-items: center;
  color: var(--ink);
  font-size: 0.9rem;
  transition: background 0.2s ease;
}
.arch-landing .door:hover .door-arrow { background: var(--lime); }
.arch-landing .doors-note { margin-top: 1.6rem; font-size: 0.8rem; color: var(--ink-faint); }
.arch-landing .doors-note a { text-decoration: underline; text-underline-offset: 3px; }

/* ---------- toast (mailto fallback confirmation) ---------- */

.arch-landing .toast {
  position: fixed;
  left: 50%;
  bottom: 1.6rem;
  z-index: 60;
  transform: translate(-50%, 12px);
  padding: 0.75em 1.4em;
  border-radius: var(--r-chip);
  background: var(--card);
  border: 1px solid var(--hairline);
  box-shadow: var(--shadow-float);
  font-size: 0.88rem;
  font-weight: 500;
  color: var(--ink);
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.25s ease, transform 0.25s ease;
  max-width: min(92vw, 32rem);
  text-align: center;
}
.arch-landing .toast.show { opacity: 1; transform: translate(-50%, 0); }
@media (prefers-reduced-motion: reduce) {
  .arch-landing .toast { transition: opacity 0.25s ease; transform: translate(-50%, 0); }
}

/* ---------- footer ---------- */

.arch-landing .footer {
  max-width: 1180px;
  margin: clamp(4rem, 10vh, 7rem) auto 0;
  padding: 2rem var(--pagepad) 2.6rem;
  border-top: 1px solid var(--hairline);
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 0.9rem;
  align-items: baseline;
}
.arch-landing .foot-left { display: flex; align-items: baseline; gap: 1rem; }
.arch-landing .foot-mark { font-weight: 500; }
.arch-landing .foot-line { grid-column: 1 / -1; }

/* ---------- responsive ---------- */

@media (max-width: 1020px) {
  .arch-landing .c-row1 { grid-template-columns: 1fr 1fr; }
  .arch-landing .c-outputs { grid-column: 1 / -1; }
  .arch-landing .derives { grid-template-columns: repeat(2, 1fr); }
  .arch-landing .anatomy { grid-template-columns: 1fr; }
}

@media (max-width: 860px) {
  .arch-landing .console-grid { grid-template-columns: 1fr; }
  .arch-landing .c-side { flex-direction: row; flex-wrap: wrap; }
  .arch-landing .side-foot { display: none; }
  .arch-landing .split, .arch-landing .network { grid-template-columns: 1fr; }
}

@media (max-width: 640px) {
  .arch-landing .c-row1 { grid-template-columns: 1fr; }
  .arch-landing .doors { grid-template-columns: 1fr; }
  .arch-landing .derives { grid-template-columns: 1fr; }
  .arch-landing .nav-links .hide-sm { display: none; }
  .arch-landing .c-pipe { flex-wrap: wrap; }
  .arch-landing .hero-glow .chip-float { right: 4%; top: auto; bottom: 5.5rem; font-size: 0.74rem; padding: 0.55em 1em; }
  .arch-landing .gn2, .arch-landing .gn3 { display: none; }
  .arch-landing .anatomy .derives { grid-template-columns: 1fr; }
  .arch-landing .footer { grid-template-columns: 1fr; }
}

/* ---------- reduced motion ---------- */

@media (prefers-reduced-motion: reduce) {
  .arch-landing .reveal { opacity: 1; transform: none; transition: none; }
  .arch-landing .chip-float { animation: none; }
  .arch-landing .btn:hover, .arch-landing .door:hover { transform: none; }
  .arch-landing .glow-a, .arch-landing .glow-b { animation: none; }
  .arch-landing .trace, .arch-landing .trace-orange { animation: none; stroke-dashoffset: 0; opacity: 1; }
}
`;
