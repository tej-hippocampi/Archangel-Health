/**
 * Archangel Health — clinical reasoning data landing.
 * Faithful port of the approved static page (chart-review aesthetic on deep ink,
 * gold = expert reasoning · cyan = model reasoning), embedded as the SPA home view.
 * Self-contained: brings its own fixed nav and footer; do not render SiteHeader with it.
 */

import { useEffect, useRef, useState } from "react";
import { useAuth } from "@/contexts/AuthContext";
import { SignInDialog } from "@/app/components/SignInDialog";
import * as authApi from "@/lib/auth-api";
import "@/styles/clinical-fonts.css";

const MAIL = "aryaabhatia@berkeley.edu";
const mailto = (subject: string) => `mailto:${MAIL}?subject=${encodeURIComponent(subject)}`;

export default function ClinicalDataLanding() {
  const { user, loading, logout, token } = useAuth();
  const [signInOpen, setSignInOpen] = useState(false);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!user || !token) return;
    let cancelled = false;
    authApi.getDoctorProfile(token).then((profile) => {
      if (!cancelled && profile) {
        void authApi.redirectToDoctorPortal(token);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [user, token]);

  /* ---------- scroll reveals ---------- */
  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const revealEls = root.querySelectorAll(".reveal");
    if ("IntersectionObserver" in window && !reduceMotion) {
      const io = new IntersectionObserver(
        (entries) => {
          entries.forEach((e) => {
            if (e.isIntersecting) {
              e.target.classList.add("in");
              io.unobserve(e.target);
            }
          });
        },
        { threshold: 0.15, rootMargin: "0px 0px -40px 0px" }
      );
      revealEls.forEach((el) => io.observe(el));
      return () => io.disconnect();
    }
    revealEls.forEach((el) => el.classList.add("in"));
  }, []);

  /* ---------- hero reasoning-trace canvas ---------- */
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    const GOLD = "#e2a83d";
    const CYAN = "#58b7c9";
    const BONE = "rgba(233,228,216,0.65)";

    let W = 0;
    let H = 0;
    let dpr = 1;

    interface Trace {
      shared: number[][];
      gold: number[][];
      cyan: number[][];
      xDiv: number;
      yDiv: number;
      opacity: number;
      speed: number;
      phase: number;
    }
    let traces: Trace[] = [];

    // Build one trace: a shared vitals line that diverges into expert (gold, up)
    // and model (cyan, down) paths.
    function makeTrace(yFrac: number, divFrac: number, opacity: number, speed: number, phase: number, seed: number): Trace {
      const y0 = H * yFrac;
      const xDiv = W * divFrac;
      const shared: number[][] = [];
      const step = 6;
      let x: number;
      for (x = -20; x <= xDiv; x += step) {
        let y = y0;
        // gentle baseline wander
        y += Math.sin(x * 0.008 + seed * 7) * 6;
        // ECG-like complexes roughly every 260px
        const beat = ((x + seed * 500) % 260) / 260;
        if (beat > 0.42 && beat < 0.47) y -= 26 * Math.sin(((beat - 0.42) / 0.05) * Math.PI);
        if (beat > 0.47 && beat < 0.52) y += 12 * Math.sin(((beat - 0.47) / 0.05) * Math.PI);
        shared.push([x, y]);
      }
      const yEnd = shared[shared.length - 1][1];
      const spread = H * 0.16;
      const gold: number[][] = [];
      let cyan: number[][] = [];
      const runOut = W - xDiv + 40;
      for (x = xDiv; x <= W + 40; x += step) {
        const t = Math.min(1, (x - xDiv) / (runOut * 0.55));
        const ease = 1 - Math.pow(1 - t, 3);
        const wobble = Math.sin(x * 0.01 + seed * 3) * 4;
        gold.push([x, yEnd - spread * ease + wobble]);
        cyan.push([x, yEnd + spread * 0.85 * ease + wobble * 0.6]);
      }
      // model path terminates early — it fails
      cyan = cyan.slice(0, Math.floor(cyan.length * 0.62));
      return { shared, gold, cyan, xDiv, yDiv: yEnd, opacity, speed, phase };
    }

    function build() {
      if (!canvas || !ctx) return;
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      W = canvas.clientWidth;
      H = canvas.clientHeight;
      canvas.width = W * dpr;
      canvas.height = H * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const mobile = W < 700;
      traces = mobile
        ? [makeTrace(0.88, 0.42, 0.9, 0.9, 0.0, 0.31)]
        : [
            makeTrace(0.6, 0.55, 1.0, 1.0, 0.0, 0.31),
            makeTrace(0.3, 0.68, 0.4, 0.75, 0.45, 0.77),
            makeTrace(0.86, 0.62, 0.3, 0.6, 0.8, 0.13),
          ];
    }

    function drawPoly(pts: number[][], from: number, to: number, color: string, width: number, glow: number) {
      if (!ctx || to - from < 2) return;
      ctx.beginPath();
      ctx.moveTo(pts[from][0], pts[from][1]);
      for (let i = from + 1; i < to; i++) ctx.lineTo(pts[i][0], pts[i][1]);
      ctx.strokeStyle = color;
      ctx.lineWidth = width;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      ctx.shadowColor = color;
      ctx.shadowBlur = glow;
      ctx.stroke();
      ctx.shadowBlur = 0;
    }

    // dot grid, drawn once per frame very faint
    function drawGrid() {
      if (!ctx) return;
      ctx.fillStyle = "rgba(233,228,216,0.045)";
      const gap = 44;
      for (let gx = gap; gx < W; gx += gap) {
        for (let gy = gap; gy < H; gy += gap) {
          ctx.fillRect(gx, gy, 1.2, 1.2);
        }
      }
    }

    function fadeLeft() {
      if (!ctx || W < 700) return;
      ctx.save();
      ctx.globalCompositeOperation = "destination-out";
      const g = ctx.createLinearGradient(0, 0, W * 0.62, 0);
      g.addColorStop(0, "rgba(0,0,0,0.88)");
      g.addColorStop(0.7, "rgba(0,0,0,0.55)");
      g.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, W * 0.62, H);
      ctx.restore();
    }

    let rafId: number | null = null;

    function frame(now: number) {
      if (!ctx) return;
      ctx.clearRect(0, 0, W, H);
      drawGrid();
      const period = 9000; // ms per sweep
      traces.forEach((tr) => {
        const p = ((now * tr.speed) / period + tr.phase) % 1.15; // pause between sweeps
        const head = p * (W + 80) - 20;
        let alpha = tr.opacity;
        // fade the whole trace out at the end of its sweep
        if (p > 1.0) alpha *= Math.max(0, 1 - (p - 1.0) / 0.15);

        ctx.globalAlpha = alpha;

        let sharedEnd = 0;
        while (sharedEnd < tr.shared.length && tr.shared[sharedEnd][0] < head) sharedEnd++;
        drawPoly(tr.shared, 0, sharedEnd, BONE, 1.6, 0);

        if (head > tr.xDiv) {
          let gEnd = 0;
          while (gEnd < tr.gold.length && tr.gold[gEnd][0] < head) gEnd++;
          drawPoly(tr.gold, 0, gEnd, GOLD, 2, 10);

          let cEnd = 0;
          while (cEnd < tr.cyan.length && tr.cyan[cEnd][0] < head) cEnd++;
          ctx.setLineDash([6, 5]);
          drawPoly(tr.cyan, 0, cEnd, CYAN, 1.6, 8);
          ctx.setLineDash([]);

          // divergence node
          ctx.beginPath();
          ctx.arc(tr.xDiv, tr.yDiv, 4, 0, Math.PI * 2);
          ctx.fillStyle = "#0a0e12";
          ctx.strokeStyle = BONE;
          ctx.lineWidth = 1.6;
          ctx.fill();
          ctx.stroke();

          // model-failure mark where the cyan path ends
          if (cEnd >= tr.cyan.length && tr.cyan.length) {
            const last = tr.cyan[tr.cyan.length - 1];
            ctx.strokeStyle = "rgba(226,96,78,0.9)";
            ctx.lineWidth = 2;
            ctx.beginPath();
            ctx.moveTo(last[0] - 5, last[1] - 5);
            ctx.lineTo(last[0] + 5, last[1] + 5);
            ctx.moveTo(last[0] + 5, last[1] - 5);
            ctx.lineTo(last[0] - 5, last[1] + 5);
            ctx.stroke();
          }
        }
        ctx.globalAlpha = 1;
      });
      fadeLeft();
      rafId = requestAnimationFrame(frame);
    }

    function staticFrame() {
      if (!ctx) return;
      ctx.clearRect(0, 0, W, H);
      drawGrid();
      traces.forEach((tr) => {
        ctx.globalAlpha = tr.opacity;
        drawPoly(tr.shared, 0, tr.shared.length, BONE, 1.6, 0);
        drawPoly(tr.gold, 0, tr.gold.length, GOLD, 2, 10);
        ctx.setLineDash([6, 5]);
        drawPoly(tr.cyan, 0, tr.cyan.length, CYAN, 1.6, 8);
        ctx.setLineDash([]);
        ctx.beginPath();
        ctx.arc(tr.xDiv, tr.yDiv, 4, 0, Math.PI * 2);
        ctx.fillStyle = "#0a0e12";
        ctx.strokeStyle = BONE;
        ctx.lineWidth = 1.6;
        ctx.fill();
        ctx.stroke();
        ctx.globalAlpha = 1;
      });
      fadeLeft();
    }

    let resizeTimer: ReturnType<typeof setTimeout> | null = null;

    function start() {
      build();
      if (reduceMotion) {
        staticFrame();
      } else {
        if (rafId) cancelAnimationFrame(rafId);
        rafId = requestAnimationFrame(frame);
      }
    }

    function onResize() {
      if (resizeTimer) clearTimeout(resizeTimer);
      resizeTimer = setTimeout(start, 150);
    }

    window.addEventListener("resize", onResize);
    start();

    return () => {
      window.removeEventListener("resize", onResize);
      if (rafId) cancelAnimationFrame(rafId);
      if (resizeTimer) clearTimeout(resizeTimer);
    };
  }, []);

  return (
    <div ref={rootRef} className="clinical-landing">
      <header className="nav" id="top">
        <a className="wordmark" href="/" aria-label="Archangel Health — home">
          <svg className="halo" viewBox="0 0 24 24" aria-hidden="true">
            <ellipse cx="12" cy="12" rx="9" ry="5.4" fill="none" stroke="currentColor" strokeWidth="1.7" transform="rotate(-24 12 12)" />
          </svg>
          <span>Archangel&nbsp;Health</span>
        </a>
        <nav className="nav-links" aria-label="Primary">
          <a href="#findings">The data</a>
          <a href="#consults">Contributors</a>
          {!loading &&
            (user ? (
              <button type="button" className="nav-auth" onClick={logout}>
                Sign out
              </button>
            ) : (
              <button type="button" className="nav-auth" onClick={() => setSignInOpen(true)}>
                Sign in
              </button>
            ))}
          <a className="nav-cta" href={mailto("Data request — Archangel Health")}>Request data</a>
        </nav>
      </header>

      <main>
        {/* ======================= HERO ======================= */}
        <section className="hero">
          <canvas id="trace-canvas" ref={canvasRef} aria-hidden="true" />
          <div className="hero-inner">
            <p className="eyebrow">Clinical reasoning data · human judgment at the frontier</p>
            <h1>
              <span className="h1-a">The cases frontier models fail.</span>
              <span className="h1-b">The reasoning that resolves them.</span>
            </h1>
            <p className="hero-sub">
              Archangel Health captures expert clinical reasoning over real, de-identified cases hard
              enough to split board-certified specialists — delivered as preference pairs, ideal answers,
              and step-level reasoning traces, ready for training and evals.
            </p>
            <div className="hero-ctas">
              <a className="btn btn-solid" href={mailto("Data request — Archangel Health")}>Request data</a>
              <a className="btn btn-ghost" href={mailto("Becoming a contributor — Archangel Health")}>Become a contributor</a>
            </div>
          </div>
          <div className="hero-legend" aria-hidden="true">
            <span><i className="key key-gold" />expert reasoning</span>
            <span><i className="key key-cyan" />model reasoning</span>
            <span className="legend-note">our data lives where they diverge</span>
          </div>
        </section>

        {/* ================== 01 PRESENTING PROBLEM ================== */}
        <section className="section" id="problem">
          <div className="section-head reveal">
            <p className="chart-label">01 · Presenting problem</p>
          </div>
          <div className="split">
            <div className="split-copy reveal">
              <h2>
                Frontier models pass the easy benchmarks.<br />
                <em>So we build the ones they can’t.</em>
              </h2>
              <p>
                Our cases are hard by construction: the labs and the narrative pull in different
                directions, the right answer needs both, and the wrong answer looks plausible.
                On these presentations, frontier models fail — and board-certified specialists
                disagree on the diagnosis, the intervention, even the ground truth.
              </p>
              <p className="lede-strong">
                That disagreement isn’t noise. It’s the most valuable training signal in medicine —
                and we are the ones capturing it.
              </p>
            </div>
            <div className="split-visual reveal" aria-hidden="true">
              <div className="diverge-fig">
                <svg viewBox="0 0 560 310" preserveAspectRatio="xMidYMid meet">
                  <path className="fig-grid" d="M0 78 H560 M0 155 H560 M0 232 H560" />
                  <path className="fig-shared" d="M0 155 H160 l14 -26 14 44 14 -18 H260" />
                  <path className="fig-gold" d="M260 155 C 325 155, 345 94, 415 86 H560" />
                  <path className="fig-gold2" d="M260 155 C 325 155, 345 126, 415 122 H560" />
                  <path className="fig-cyan" d="M260 155 C 325 155, 345 220, 405 230" />
                  <circle className="fig-node" cx="260" cy="155" r="5" />
                  <g className="fig-x">
                    <path d="M406 223 l16 16 M422 223 l-16 16" />
                  </g>
                  <text className="fig-t fig-t-gold" x="552" y="72" textAnchor="end">specialist A</text>
                  <text className="fig-t fig-t-gold" x="552" y="144" textAnchor="end">specialist B</text>
                  <text className="fig-t fig-t-cyan" x="414" y="266" textAnchor="middle">frontier model</text>
                  <text className="fig-t fig-t-dim" x="248" y="188" textAnchor="end">divergence</text>
                </svg>
              </div>
              <p className="fig-caption">One case. Two credentialed opinions. One model failure. All captured.</p>
            </div>
          </div>
        </section>

        {/* ================== 02 FINDINGS ================== */}
        <section className="section" id="findings">
          <div className="section-head reveal">
            <p className="chart-label">02 · Findings</p>
            <h2>One case, <em>four kinds of supervision.</em></h2>
            <p className="section-sub">
              Every record starts as a multimodal case — structured labs plus an
              EHR-style note — and ships as finished, schema-validated training data.
            </p>
          </div>
          <div className="anatomy">
            <figure className="case-card reveal" aria-label="Example de-identified case record">
              <figcaption className="case-head">
                <span className="case-title">Case record</span>
                <span className="case-badge">de-identified</span>
              </figcaption>
              <div className="case-id mono">
                <span>PT NAME&nbsp;&nbsp;<i className="redact w9" /></span>
                <span>MRN&nbsp;&nbsp;<i className="redact w7" /></span>
                <span>DOB&nbsp;&nbsp;<i className="redact w6" /></span>
              </div>
              <table className="labs mono" aria-label="Laboratory results">
                <thead>
                  <tr><th>lab</th><th>value</th><th>ref</th><th>flag</th></tr>
                </thead>
                <tbody>
                  <tr><td>Creatinine</td><td>3.1</td><td>0.7–1.3</td><td className="flag-h">H ▲</td></tr>
                  <tr><td>Potassium</td><td>5.9</td><td>3.5–5.0</td><td className="flag-h">H ▲</td></tr>
                  <tr><td>Bicarbonate</td><td>16</td><td>22–29</td><td className="flag-l">L ▼</td></tr>
                  <tr><td>Hemoglobin</td><td>9.4</td><td>13.5–17.5</td><td className="flag-l">L ▼</td></tr>
                  <tr><td>Urine Na⁺</td><td>12</td><td>—</td><td></td></tr>
                </tbody>
              </table>
              <div className="note mono">
                <span className="note-label">HPI</span>
                <p>
                  Progressive fatigue ×3 wk. Recently started <i className="redact w8 inline" /> for joint
                  pain. Exam: trace edema, BP 168/94. History pulls toward one diagnosis;
                  the labs argue for another.
                </p>
              </div>
              <div className="case-attest mono">
                <span className="attest-sig">Reviewed — board-certified specialist</span>
                <span className="attest-check">✓</span>
              </div>
            </figure>
            <div className="derives reveal" role="list">
              <div className="derive" role="listitem">
                <span className="derive-tag mono">RLHF · DPO</span>
                <h3>Preference pair</h3>
                <p>The chosen answer against a plausible hard-negative — the mistake a good model actually makes.</p>
              </div>
              <div className="derive" role="listitem">
                <span className="derive-tag mono">SFT</span>
                <h3>Ideal answer</h3>
                <p>The complete expert resolution of the case, written to be learned from.</p>
              </div>
              <div className="derive" role="listitem">
                <span className="derive-tag mono">PRM</span>
                <h3>Reasoning trace</h3>
                <p>Step-level expert reasoning with corrections — exactly where a model’s chain goes wrong, and why.</p>
              </div>
              <div className="derive" role="listitem">
                <span className="derive-tag mono">Provenance</span>
                <h3>Full lineage</h3>
                <p>Credential attributes, guideline citations, difficulty score, versioning. Every record answers for itself.</p>
              </div>
            </div>
          </div>
        </section>

        {/* ================== 03 ASSESSMENT ================== */}
        <section className="section" id="assessment">
          <div className="split split-rev">
            <div className="split-copy reveal">
              <p className="chart-label">03 · Assessment</p>
              <h2>We don’t claim difficulty. <em>We measure it.</em></h2>
              <p>
                Every case is scored against frontier models with our own difficulty rubric, and our
                benchmarks exist to prove one thing: that this data pushes models past where public
                benchmarks stop. Data that doesn’t move the frontier doesn’t ship.
              </p>
              <div className="stat">
                <span className="stat-num">6<span className="stat-plus">+</span><span className="stat-of">/10</span></span>
                <p className="stat-label">of the most commonly used medical benchmarks improve when models train on our data.</p>
              </div>
            </div>
            <div className="qc reveal">
              <p className="qc-title mono">Quality report — every record</p>
              <ul className="qc-list">
                <li><span className="qc-check">✓</span>Difficulty scored against frontier models</li>
                <li><span className="qc-check">✓</span>Contamination-checked against public benchmarks</li>
                <li><span className="qc-check">✓</span>Guideline-grounded, with citations</li>
                <li><span className="qc-check">✓</span>No PHI — context-preserving de-identification</li>
                <li><span className="qc-check">✓</span>Watermarked &amp; traceable, licensed per end-buyer</li>
                <li><span className="qc-check">✓</span>IP-cleared, contributor credentials verified</li>
              </ul>
            </div>
          </div>
        </section>

        {/* ================== 04 CONSULTS ================== */}
        <section className="section" id="consults">
          <div className="section-head reveal">
            <p className="chart-label">04 · Consults</p>
            <h2>A network of specialists, <em>paid for their judgment.</em></h2>
          </div>
          <div className="network">
            <div className="net-panel reveal">
              <p className="net-for mono">For physicians</p>
              <h3>Your reasoning is the product.</h3>
              <p>
                Specialists work through hard cases on our platform — annotating the reasoning,
                ratifying the ground truth, flagging where models go wrong — and get paid for the
                judgment only they can supply.
              </p>
            </div>
            <div className="net-spine" aria-hidden="true"><span /></div>
            <div className="net-panel reveal">
              <p className="net-for mono">For labs &amp; health-AI teams</p>
              <h3>Datasets, spun up on demand.</h3>
              <p>
                Specialty, modality, format, difficulty — scoped to your model and your gap.
                Next: longitudinal cases that follow the patient past the decision, linking case,
                intervention, and real outcome.
              </p>
            </div>
          </div>
        </section>

        {/* ================== STATEMENT ================== */}
        <section className="statement">
          <p className="statement-line reveal">
            Doctors earn from their judgment.<br />
            Models learn from it.<br />
            <em>The hardest cases become the most valuable data.</em>
          </p>
        </section>

        {/* ================== 05 PLAN ================== */}
        <section className="section" id="plan">
          <div className="section-head reveal">
            <p className="chart-label">05 · Plan</p>
            <h2>Three ways in.</h2>
          </div>
          <div className="doors reveal">
            <a className="door" href={mailto("Becoming a contributor — Archangel Health")}>
              <span className="door-for mono">Physicians</span>
              <span className="door-title">Become a contributor</span>
              <span className="door-sub">Reason through hard cases. Get paid for your judgment.</span>
              <span className="door-arrow" aria-hidden="true">→</span>
            </a>
            <a className="door" href={mailto("Data request — Archangel Health")}>
              <span className="door-for mono">Labs &amp; health-AI teams</span>
              <span className="door-title">Request data</span>
              <span className="door-sub">Scoped samples, fitted pilots, bespoke datasets.</span>
              <span className="door-arrow" aria-hidden="true">→</span>
            </a>
            <a className="door" href={mailto("Providing de-identified data — Archangel Health")}>
              <span className="door-for mono">Health systems, practices &amp; software companies</span>
              <span className="door-title">Provide your data</span>
              <span className="door-sub">We buy de-identified clinical data — from care organizations and the software applications that hold it.</span>
              <span className="door-arrow" aria-hidden="true">→</span>
            </a>
          </div>
          <p className="doors-note reveal mono">
            Something else in mind? <a href={mailto("Partnership — Archangel Health")}>Other partnerships &amp; collaborations →</a>
          </p>
        </section>
      </main>

      <footer className="footer">
        <div className="foot-left">
          <span className="foot-mark">Archangel Health</span>
          <span className="foot-loc mono">Berkeley, California</span>
        </div>
        <div className="foot-right">
          <a href={`mailto:${MAIL}`}>{MAIL}</a>
        </div>
        <p className="foot-line mono">Real. De-identified. IP-cleared. Never resold beyond license.</p>
      </footer>

      <SignInDialog open={signInOpen} onOpenChange={setSignInOpen} />

      <style>{styles}</style>
    </div>
  );
}

const styles = `
/* ============================================================
   Archangel Health — chart-review aesthetic on deep ink
   gold = expert reasoning · cyan = model reasoning
   ============================================================ */

.clinical-landing {
  --ink: #0a0e12;
  --ink-raised: #10151c;
  --ink-lift: #161d26;
  --line: rgba(233, 228, 216, 0.10);
  --line-soft: rgba(233, 228, 216, 0.06);
  --bone: #e9e4d8;
  --slate: #8d97a3;
  --slate-dim: #5c6672;
  --halo: #e2a83d;
  --halo-bright: #f2c266;
  --monitor: #58b7c9;
  --flag: #e2604e;
  --serif: 'Newsreader', 'Iowan Old Style', Georgia, serif;
  --sans: 'Instrument Sans', -apple-system, 'Helvetica Neue', Arial, sans-serif;
  --mono: 'IBM Plex Mono', 'SF Mono', Menlo, monospace;
  --pad-x: clamp(20px, 5vw, 72px);
}

html { scroll-behavior: smooth; }

body {
  margin: 0;
  padding: 0;
  background: #0a0e12;
}

.clinical-landing {
  background: var(--ink);
  color: var(--bone);
  font-family: var(--sans);
  font-size: 17px;
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
  overflow-x: hidden;
}

.clinical-landing *, .clinical-landing *::before, .clinical-landing *::after { box-sizing: border-box; }

.clinical-landing ::selection { background: rgba(226, 168, 61, 0.35); }

.clinical-landing img, .clinical-landing svg, .clinical-landing canvas { max-width: 100%; display: block; }

.clinical-landing a { color: inherit; text-decoration: none; }

.clinical-landing .mono { font-family: var(--mono); }

.clinical-landing h1, .clinical-landing h2, .clinical-landing h3 {
  margin: 0;
  font-family: var(--serif);
  font-weight: 420;
  line-height: 1.06;
  letter-spacing: -0.012em;
}

.clinical-landing p { margin: 0; }

.clinical-landing h2 { font-size: clamp(2rem, 4.4vw, 3.6rem); text-wrap: balance; }

.clinical-landing h2 em, .clinical-landing h1 em {
  font-style: italic;
  color: var(--halo-bright);
  font-weight: 380;
}

/* ---------------- nav ---------------- */

.clinical-landing .nav {
  position: fixed;
  inset: 0 0 auto 0;
  z-index: 50;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 var(--pad-x);
  height: 64px;
  background: rgba(10, 14, 18, 0.72);
  backdrop-filter: blur(14px);
  -webkit-backdrop-filter: blur(14px);
  border-bottom: 1px solid var(--line-soft);
}

.clinical-landing .wordmark {
  display: flex;
  align-items: center;
  gap: 10px;
  font-weight: 600;
  font-size: 0.86rem;
  letter-spacing: 0.14em;
  text-transform: uppercase;
}

.clinical-landing .halo { width: 20px; height: 20px; color: var(--halo); }

.clinical-landing .nav-links {
  display: flex;
  align-items: center;
  gap: clamp(16px, 3vw, 34px);
  font-size: 0.86rem;
  letter-spacing: 0.02em;
}

.clinical-landing .nav-links a { color: var(--slate); transition: color 0.2s; }
.clinical-landing .nav-links a:hover { color: var(--bone); }
.clinical-landing .nav-links a[aria-current="page"] { color: var(--bone); }

.clinical-landing .nav-auth {
  font-family: var(--sans);
  font-size: 0.86rem;
  letter-spacing: 0.02em;
  color: var(--slate);
  background: none;
  border: none;
  padding: 0;
  cursor: pointer;
  transition: color 0.2s;
}
.clinical-landing .nav-auth:hover { color: var(--bone); }

.clinical-landing .nav-cta {
  color: var(--ink) !important;
  background: var(--halo);
  padding: 9px 18px;
  border-radius: 2px;
  font-weight: 600;
  transition: background 0.2s;
}
.clinical-landing .nav-cta:hover { background: var(--halo-bright); }

/* ---------------- buttons ---------------- */

.clinical-landing .btn {
  display: inline-block;
  font-size: 0.92rem;
  font-weight: 600;
  letter-spacing: 0.02em;
  padding: 14px 28px;
  border-radius: 2px;
  transition: background 0.2s, border-color 0.2s, color 0.2s;
}

.clinical-landing .btn-solid { background: var(--halo); color: var(--ink); }
.clinical-landing .btn-solid:hover { background: var(--halo-bright); }

.clinical-landing .btn-ghost {
  border: 1px solid rgba(233, 228, 216, 0.25);
  color: var(--bone);
}
.clinical-landing .btn-ghost:hover { border-color: var(--bone); }

/* ---------------- hero ---------------- */

.clinical-landing .hero {
  position: relative;
  min-height: 100vh;
  min-height: 100svh;
  display: flex;
  align-items: center;
  padding: 120px var(--pad-x) 80px;
}

.clinical-landing .hero::before {
  content: "";
  position: absolute;
  inset: 0;
  z-index: 0;
  background:
    radial-gradient(42% 46% at 74% 36%, rgba(226, 168, 61, 0.07), transparent 70%),
    radial-gradient(36% 40% at 86% 78%, rgba(88, 183, 201, 0.06), transparent 70%);
  pointer-events: none;
}

.clinical-landing #trace-canvas {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  z-index: 0;
}

.clinical-landing .hero-inner {
  position: relative;
  z-index: 1;
  max-width: 1120px;
}

.clinical-landing .eyebrow {
  font-family: var(--mono);
  font-size: 0.72rem;
  letter-spacing: 0.22em;
  text-transform: uppercase;
  color: var(--slate);
  margin-bottom: 28px;
}

.clinical-landing h1 { font-size: clamp(2.5rem, 5vw, 4.55rem); }

.clinical-landing .h1-a, .clinical-landing .h1-b { display: block; }

.clinical-landing .h1-a { color: var(--bone); }
.clinical-landing .h1-b {
  margin-top: 0.08em;
  font-style: italic;
  font-weight: 380;
  color: var(--halo-bright);
}

.clinical-landing .hero-sub {
  margin-top: 30px;
  max-width: 620px;
  color: var(--slate);
  font-size: clamp(1rem, 1.4vw, 1.15rem);
}

.clinical-landing .hero-ctas {
  margin-top: 40px;
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
}

.clinical-landing .hero-legend {
  position: absolute;
  z-index: 1;
  left: var(--pad-x);
  right: var(--pad-x);
  bottom: 26px;
  display: flex;
  gap: 26px;
  flex-wrap: wrap;
  font-family: var(--mono);
  font-size: 0.7rem;
  letter-spacing: 0.08em;
  color: var(--slate-dim);
}

.clinical-landing .hero-legend span { display: inline-flex; align-items: center; gap: 8px; }

.clinical-landing .key { width: 18px; height: 2px; display: inline-block; }
.clinical-landing .key-gold { background: var(--halo); box-shadow: 0 0 8px rgba(226,168,61,.8); }
.clinical-landing .key-cyan { background: var(--monitor); box-shadow: 0 0 8px rgba(88,183,201,.8); }

.clinical-landing .legend-note { margin-left: auto; font-style: italic; }

/* ---------------- sections ---------------- */

.clinical-landing .section {
  padding: clamp(90px, 12vh, 150px) var(--pad-x);
  border-top: 1px solid var(--line-soft);
  max-width: 1360px;
  margin: 0 auto;
}

.clinical-landing .chart-label {
  font-family: var(--mono);
  font-size: 0.72rem;
  letter-spacing: 0.22em;
  text-transform: uppercase;
  color: var(--halo);
  margin-bottom: 22px;
}

.clinical-landing .chart-label::before {
  content: "";
  display: inline-block;
  width: 26px;
  height: 1px;
  background: var(--halo);
  vertical-align: middle;
  margin-right: 12px;
  opacity: 0.7;
}

.clinical-landing .section-head { margin-bottom: clamp(40px, 6vh, 70px); max-width: 820px; }

.clinical-landing .section-sub {
  margin-top: 20px;
  color: var(--slate);
  max-width: 560px;
}

.clinical-landing .split {
  display: grid;
  grid-template-columns: minmax(0, 1.05fr) minmax(0, 1fr);
  gap: clamp(40px, 6vw, 90px);
  align-items: center;
}

.clinical-landing .split-copy p { margin-top: 22px; color: var(--slate); max-width: 520px; }

.clinical-landing .split-copy .lede-strong {
  color: var(--bone);
  font-family: var(--serif);
  font-size: 1.35rem;
  line-height: 1.4;
  font-style: italic;
}

/* ---------------- divergence figure (01) ---------------- */

.clinical-landing .diverge-fig {
  background: var(--ink-raised);
  border: 1px solid var(--line);
  border-radius: 4px;
  padding: clamp(18px, 3vw, 34px);
}

.clinical-landing .diverge-fig svg { width: 100%; height: auto; }

.clinical-landing .fig-grid { stroke: var(--line-soft); stroke-width: 1; }

.clinical-landing .fig-shared, .clinical-landing .fig-gold, .clinical-landing .fig-gold2, .clinical-landing .fig-cyan {
  fill: none;
  stroke-width: 2.6;
  stroke-linecap: round;
}

.clinical-landing .fig-shared { stroke: var(--bone); opacity: 0.7; }
.clinical-landing .fig-gold  { stroke: var(--halo); filter: drop-shadow(0 0 6px rgba(226,168,61,.5)); }
.clinical-landing .fig-gold2 { stroke: var(--halo); opacity: 0.55; }
.clinical-landing .fig-cyan  { stroke: var(--monitor); stroke-dasharray: 7 5; filter: drop-shadow(0 0 6px rgba(88,183,201,.4)); }

.clinical-landing .fig-node { fill: var(--ink); stroke: var(--bone); stroke-width: 2.4; }

.clinical-landing .fig-x path { stroke: var(--flag); stroke-width: 2.5; stroke-linecap: round; }

.clinical-landing .fig-t {
  font-family: var(--mono);
  font-size: 13px;
  letter-spacing: 0.06em;
}
.clinical-landing .fig-t-gold { fill: var(--halo); }
.clinical-landing .fig-t-cyan { fill: var(--monitor); }
.clinical-landing .fig-t-dim  { fill: var(--slate-dim); font-style: italic; }

.clinical-landing .fig-caption {
  margin-top: 18px;
  padding-top: 16px;
  border-top: 1px solid var(--line-soft);
  font-family: var(--mono);
  font-size: 0.74rem;
  color: var(--slate-dim);
  letter-spacing: 0.04em;
}

/* ---------------- findings / anatomy (02) ---------------- */

.clinical-landing .anatomy {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1.1fr);
  gap: clamp(36px, 5vw, 70px);
  align-items: start;
}

.clinical-landing .case-card {
  margin: 0;
  background: var(--ink-raised);
  border: 1px solid var(--line);
  border-radius: 4px;
  overflow: hidden;
  box-shadow: 0 30px 80px rgba(0, 0, 0, 0.45);
}

.clinical-landing .case-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 14px 20px;
  border-bottom: 1px solid var(--line);
  background: var(--ink-lift);
}

.clinical-landing .case-title {
  font-family: var(--mono);
  font-size: 0.78rem;
  letter-spacing: 0.14em;
  text-transform: uppercase;
}

.clinical-landing .case-badge {
  font-family: var(--mono);
  font-size: 0.66rem;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--halo);
  border: 1px solid rgba(226, 168, 61, 0.4);
  padding: 4px 10px;
  border-radius: 999px;
}

.clinical-landing .case-id {
  display: flex;
  gap: 22px;
  flex-wrap: wrap;
  padding: 16px 20px;
  font-size: 0.72rem;
  color: var(--slate-dim);
  letter-spacing: 0.06em;
  border-bottom: 1px solid var(--line-soft);
}

.clinical-landing .redact {
  display: inline-block;
  height: 0.85em;
  background: #2a3038;
  border-radius: 1px;
  vertical-align: middle;
}
.clinical-landing .redact.inline { vertical-align: baseline; transform: translateY(2px); }
.clinical-landing .w6 { width: 3.6em; } .clinical-landing .w7 { width: 4.4em; }
.clinical-landing .w8 { width: 5em; } .clinical-landing .w9 { width: 6em; }

.clinical-landing .labs {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.8rem;
}

.clinical-landing .labs th, .clinical-landing .labs td {
  text-align: left;
  padding: 9px 20px;
  border-bottom: 1px solid var(--line-soft);
}

.clinical-landing .labs th {
  font-weight: 500;
  font-size: 0.66rem;
  text-transform: uppercase;
  letter-spacing: 0.14em;
  color: var(--slate-dim);
}

.clinical-landing .labs td { color: var(--slate); }
.clinical-landing .labs td:nth-child(2) { color: var(--bone); }

.clinical-landing .flag-h { color: var(--flag) !important; }
.clinical-landing .flag-l { color: var(--monitor) !important; }

.clinical-landing .note { padding: 18px 20px; font-size: 0.8rem; }

.clinical-landing .note-label {
  display: block;
  font-size: 0.66rem;
  letter-spacing: 0.14em;
  color: var(--slate-dim);
  margin-bottom: 8px;
}

.clinical-landing .note p { color: var(--slate); line-height: 1.7; }

.clinical-landing .case-attest {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 14px 20px;
  border-top: 1px solid var(--line);
  font-size: 0.72rem;
  letter-spacing: 0.06em;
}

.clinical-landing .attest-sig { color: var(--halo); font-style: italic; }
.clinical-landing .attest-check { color: var(--halo); }

.clinical-landing .derives { display: grid; gap: 0; }

.clinical-landing .derive {
  padding: 26px 0 26px 30px;
  border-left: 1px solid var(--line);
  position: relative;
}

.clinical-landing .derive::before {
  content: "";
  position: absolute;
  left: -4px;
  top: 38px;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--halo);
  box-shadow: 0 0 10px rgba(226, 168, 61, 0.7);
}

.clinical-landing .derive-tag {
  font-size: 0.66rem;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--monitor);
}

.clinical-landing .derive h3 { font-size: 1.5rem; margin: 8px 0 8px; }

.clinical-landing .derive p { color: var(--slate); font-size: 0.95rem; max-width: 420px; }

/* ---------------- assessment (03) ---------------- */

.clinical-landing .split-rev { align-items: center; }

.clinical-landing .qc {
  background: var(--ink-raised);
  border: 1px solid var(--line);
  border-radius: 4px;
  padding: clamp(24px, 3vw, 40px);
}

.clinical-landing .qc-title {
  font-size: 0.72rem;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--slate-dim);
  padding-bottom: 18px;
  border-bottom: 1px solid var(--line);
  margin-bottom: 8px;
}

.clinical-landing .qc-list { list-style: none; margin: 0; padding: 0; }

.clinical-landing .qc-list li {
  display: flex;
  gap: 14px;
  align-items: baseline;
  padding: 13px 0;
  border-bottom: 1px solid var(--line-soft);
  color: var(--slate);
  font-size: 0.95rem;
}

.clinical-landing .qc-list li:last-child { border-bottom: none; }

.clinical-landing .qc-check { color: var(--halo); font-size: 0.85rem; }

.clinical-landing .stat {
  margin-top: 40px;
  padding-top: 30px;
  border-top: 1px solid var(--line);
  display: flex;
  align-items: baseline;
  gap: 22px;
  max-width: 520px;
}

.clinical-landing .stat-num {
  font-family: var(--serif);
  font-size: clamp(3rem, 5vw, 4.2rem);
  line-height: 1;
  color: var(--halo-bright);
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}

.clinical-landing .stat-plus {
  font-size: 0.5em;
  vertical-align: 0.7em;
  margin-left: 0.04em;
}

.clinical-landing .stat-of {
  font-size: 0.52em;
  color: var(--slate);
  font-style: italic;
}

.clinical-landing .stat-label {
  margin: 0 !important;
  color: var(--slate);
  font-size: 0.95rem;
  max-width: 260px;
}

/* ---------------- consults (04) ---------------- */

.clinical-landing .network {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
  gap: clamp(28px, 4vw, 56px);
  align-items: stretch;
}

.clinical-landing .net-panel {
  background: var(--ink-raised);
  border: 1px solid var(--line);
  border-radius: 4px;
  padding: clamp(26px, 3.4vw, 44px);
}

.clinical-landing .net-for {
  font-size: 0.68rem;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--monitor);
}

.clinical-landing .net-panel:first-child .net-for { color: var(--halo); }

.clinical-landing .net-panel h3 { font-size: clamp(1.5rem, 2.4vw, 2rem); margin: 14px 0 14px; }

.clinical-landing .net-panel p { color: var(--slate); font-size: 0.98rem; }

.clinical-landing .net-spine { display: flex; align-items: center; }

.clinical-landing .net-spine span {
  width: 1px;
  align-self: stretch;
  background: linear-gradient(to bottom, transparent, var(--halo) 30%, var(--monitor) 70%, transparent);
  opacity: 0.85;
}

/* ---------------- statement bridge ---------------- */

.clinical-landing .statement {
  border-top: 1px solid var(--line-soft);
  padding: clamp(110px, 16vh, 190px) var(--pad-x);
  text-align: center;
}

.clinical-landing .statement-line {
  font-family: var(--serif);
  font-size: clamp(1.8rem, 3.6vw, 3.1rem);
  line-height: 1.32;
  font-weight: 400;
  letter-spacing: -0.01em;
}

.clinical-landing .statement-line em {
  font-style: italic;
  color: var(--halo-bright);
}

/* ---------------- plan / doors (05) ---------------- */

.clinical-landing .doors {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 1px;
  background: var(--line);
  border: 1px solid var(--line);
  border-radius: 4px;
  overflow: hidden;
}

.clinical-landing .door {
  background: var(--ink-raised);
  padding: clamp(30px, 3.6vw, 48px);
  display: flex;
  flex-direction: column;
  gap: 12px;
  position: relative;
  transition: background 0.25s;
  min-height: 260px;
}

.clinical-landing .door:hover { background: var(--ink-lift); }

.clinical-landing .door-for {
  font-size: 0.66rem;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--slate-dim);
}

.clinical-landing .door-title {
  font-family: var(--serif);
  font-size: clamp(1.5rem, 2.2vw, 1.9rem);
  line-height: 1.15;
}

.clinical-landing .door-sub { color: var(--slate); font-size: 0.92rem; }

.clinical-landing .door-arrow {
  margin-top: auto;
  color: var(--halo);
  font-size: 1.3rem;
  transition: transform 0.25s;
}

.clinical-landing .door:hover .door-arrow { transform: translateX(8px); }

.clinical-landing .doors-note {
  margin-top: 26px;
  font-size: 0.74rem;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--slate-dim);
}

.clinical-landing .doors-note a {
  color: var(--slate);
  border-bottom: 1px solid var(--line);
  padding-bottom: 3px;
  transition: color 0.2s, border-color 0.2s;
}

.clinical-landing .doors-note a:hover { color: var(--halo-bright); border-color: var(--halo); }

/* ---------------- footer ---------------- */

.clinical-landing .footer {
  border-top: 1px solid var(--line);
  padding: 60px var(--pad-x) 40px;
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 24px;
}

.clinical-landing .foot-left { display: flex; flex-direction: column; gap: 6px; }

.clinical-landing .foot-mark {
  font-family: var(--serif);
  font-size: 1.25rem;
  font-style: italic;
}

.clinical-landing .foot-loc { font-size: 0.7rem; letter-spacing: 0.12em; color: var(--slate-dim); text-transform: uppercase; }

.clinical-landing .foot-right {
  display: flex;
  gap: 28px;
  align-items: center;
  font-size: 0.88rem;
  color: var(--slate);
}

.clinical-landing .foot-right a:hover { color: var(--bone); }

.clinical-landing .foot-line {
  grid-column: 1 / -1;
  margin-top: 26px;
  padding-top: 20px;
  border-top: 1px solid var(--line-soft);
  font-size: 0.68rem;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--slate-dim);
}

/* ---------------- reveal animation ---------------- */

.clinical-landing .reveal {
  opacity: 0;
  transform: translateY(28px);
  transition: opacity 0.9s cubic-bezier(0.2, 0.6, 0.2, 1), transform 0.9s cubic-bezier(0.2, 0.6, 0.2, 1);
}

.clinical-landing .reveal.in { opacity: 1; transform: none; }

/* ---------------- responsive ---------------- */

@media (max-width: 900px) {
  .clinical-landing .split, .clinical-landing .split-rev, .clinical-landing .anatomy { grid-template-columns: 1fr; }
  .clinical-landing .network { grid-template-columns: 1fr; }
  .clinical-landing .net-spine { display: none; }
  .clinical-landing .doors { grid-template-columns: 1fr; }
  .clinical-landing .door { min-height: 0; }
  .clinical-landing .hero-legend .legend-note { margin-left: 0; width: 100%; }
  .clinical-landing .footer { grid-template-columns: 1fr; }
}

@media (max-width: 600px) {
  .clinical-landing { font-size: 16px; }
  .clinical-landing .nav { height: 58px; }
  .clinical-landing .nav-links { gap: 14px; font-size: 0.8rem; }
  .clinical-landing .nav-cta { padding: 7px 12px; }
  .clinical-landing .wordmark span { display: none; }
  .clinical-landing .hero { padding-top: 100px; }
  .clinical-landing .hero-ctas .btn { width: 100%; text-align: center; }
  .clinical-landing .case-id { gap: 12px; }
}

/* ---------------- reduced motion ---------------- */

@media (prefers-reduced-motion: reduce) {
  html { scroll-behavior: auto; }
  .clinical-landing .reveal { opacity: 1; transform: none; transition: none; }
  .clinical-landing * { transition-duration: 0.01ms !important; }
}

/* ---------------- focus ---------------- */

.clinical-landing a:focus-visible, .clinical-landing .btn:focus-visible, .clinical-landing .nav-auth:focus-visible {
  outline: 2px solid var(--halo);
  outline-offset: 3px;
  border-radius: 2px;
}
`;
