/**
 * Route-specific additions to the v3 "console" system — menu panel, minimal
 * hero, and the per-audience routes (research / data / health-systems /
 * physicians / mission). Same tokens, same laws as baseStyles.ts:
 * air is the design · scale not boldness · zero black fills · gradients only
 * as blurred auras · mono chrome = wayfinding. No new colors or typefaces.
 */

export const routeStyles = `
/* screen-reader-only (per-route document h1 that carries the heading order) */
.arch-landing .arch-sr-only {
  position: absolute;
  width: 1px; height: 1px;
  padding: 0; margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

/* ============ shell ============ */

.arch-landing .nav-left { display: flex; align-items: center; gap: 0.75rem; }

.arch-landing .menu-trigger { gap: 0.7em; }
.arch-landing .menu-glyph { display: inline-flex; flex-direction: column; gap: 3px; width: 14px; }
.arch-landing .menu-glyph i {
  height: 1px;
  background: currentColor;
  transition: transform 0.2s ease;
}
.arch-landing .menu-trigger:hover .menu-glyph i:first-child { transform: translateY(-1px); }
.arch-landing .menu-trigger:hover .menu-glyph i:last-child { transform: translateY(1px); }

/* ============ menu panel ============ */

.arch-landing .menu-overlay {
  position: fixed;
  inset: 0;
  z-index: 80;
  background: rgba(238, 240, 239, 0.98);
  backdrop-filter: blur(14px) saturate(1.4);
  -webkit-backdrop-filter: blur(14px) saturate(1.4);
  display: flex;
  flex-direction: column;
  overflow-y: auto;
  opacity: 0;
  transform: scale(0.98);
  animation: arch-menu-in 260ms cubic-bezier(0.16, 1, 0.3, 1) forwards;
}
.arch-landing .menu-overlay.closing {
  animation: arch-menu-out 180ms cubic-bezier(0.4, 0, 0.2, 1) forwards;
}
@keyframes arch-menu-in { to { opacity: 1; transform: scale(1); } }
@keyframes arch-menu-out { from { opacity: 1; transform: scale(1); } to { opacity: 0; transform: scale(0.99); } }

.arch-landing .menu-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 1rem var(--pagepad);
}

.arch-landing .menu-rows {
  width: min(880px, 100%);
  margin: 0 auto;
  padding: clamp(0.5rem, 4vh, 2.5rem) var(--pagepad) 1rem;
  flex: 1;
}

.arch-landing .menu-row { border-top: 1px solid var(--hairline); }
.arch-landing .menu-row:first-child { border-top: none; }

.arch-landing .menu-row-btn {
  display: flex;
  align-items: center;
  gap: 1.2rem;
  width: 100%;
  min-height: 76px;
  padding: 0.6rem 1.1rem;
  border-radius: var(--r-sm);
  text-align: left;
  transition: background 160ms ease;
  opacity: 0;
  transform: translateY(10px);
}
.arch-landing .menu-open .menu-row-btn { animation: arch-menu-row 420ms cubic-bezier(0.16, 1, 0.3, 1) forwards; }
@keyframes arch-menu-row { to { opacity: 1; transform: none; } }

.arch-landing .menu-row-btn:hover { background: var(--card); }
.arch-landing .menu-row-btn:hover .chrome { transform: translateX(2px); }
.arch-landing .menu-row-btn .chrome { min-width: 14.5em; transition: transform 160ms ease; }
.arch-landing .menu-row-btn.active { background: var(--card); }

.arch-landing .menu-row-title {
  font-size: clamp(1.5rem, 3.4vw, 2.1rem);
  letter-spacing: -0.015em;
  line-height: 1.1;
}
.arch-landing .menu-row-btn .chip { margin-left: 0.4rem; font-size: 0.68rem; }
.arch-landing .menu-chev {
  margin-left: auto;
  color: var(--ink-faint);
  font-size: 0.85rem;
  transition: transform 240ms cubic-bezier(0.16, 1, 0.3, 1);
}
.arch-landing .menu-chev.openv { transform: rotate(90deg); }

.arch-landing .menu-sub {
  display: grid;
  grid-template-rows: 0fr;
  transition: grid-template-rows 240ms cubic-bezier(0.16, 1, 0.3, 1);
}
.arch-landing .menu-sub.open { grid-template-rows: 1fr; }
.arch-landing .menu-sub-inner { overflow: hidden; min-height: 0; }
.arch-landing .menu-sub-item {
  display: flex;
  align-items: center;
  gap: 0.8rem;
  width: 100%;
  padding: 0.7rem 1.1rem 0.7rem 2.4rem;
  border-radius: var(--r-sm);
  font-size: 1.02rem;
  color: var(--ink-soft);
  text-align: left;
  transition: background 160ms ease, color 160ms ease;
  opacity: 0;
}
.arch-landing .menu-sub.open .menu-sub-item { animation: arch-menu-row 300ms cubic-bezier(0.16, 1, 0.3, 1) forwards; }
.arch-landing .menu-sub-item:hover { background: var(--card); color: var(--ink); }
.arch-landing .menu-sub-item .chrome { font-size: 0.6rem; min-width: 3.2em; }

.arch-landing .menu-foot {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.8rem;
  width: min(880px, 100%);
  margin: 0 auto;
  padding: 1.2rem var(--pagepad) 2rem;
  border-top: 1px solid var(--hairline);
}
.arch-landing .menu-foot .spacer { flex: 1; }
.arch-landing .menu-foot .menu-mail { font-size: 0.85rem; color: var(--ink-soft); text-decoration: underline; text-underline-offset: 3px; text-decoration-color: var(--hairline); }

/* ============ minimal hero (/) ============ */

.arch-landing .hero-min {
  position: relative;
  min-height: calc(100svh - 70px);
  display: flex;
  align-items: center;
  justify-content: center;
  text-align: center;
  padding: 0 var(--pagepad) 8vh; /* optical lift ~4vh above true center */
  overflow: clip;
  isolation: isolate;
}
.arch-landing .hero-min .glow-field { z-index: -1; }
.arch-landing .hero-min .glow-field::before,
.arch-landing .hero-min .glow-field::after { content: none; } /* no full-bleed gradient — aura only */
.arch-landing .hero-min .glow-a {
  left: 50%; top: 50%;
  width: 58vw; height: 52vh;
  transform: translate(-72%, -68%);
  animation: arch-min-breathe 6s cubic-bezier(0.16, 1, 0.3, 1) 1, arch-min-drift-a 21s ease-in-out 6s infinite alternate;
}
.arch-landing .hero-min .glow-b {
  left: 50%; top: 50%;
  width: 62vw; height: 56vh;
  transform: translate(-30%, -34%);
  animation: arch-min-breathe 6s cubic-bezier(0.16, 1, 0.3, 1) 1, arch-min-drift-b 24s ease-in-out 6s infinite alternate;
}
@keyframes arch-min-breathe {
  0% { scale: 1; }
  50% { scale: 1.03; }
  100% { scale: 1; }
}
@keyframes arch-min-drift-a {
  from { transform: translate(-72%, -68%) rotate(0deg); opacity: 1; }
  to { transform: translate(-64%, -60%) rotate(5deg); opacity: 0.94; }
}
@keyframes arch-min-drift-b {
  from { transform: translate(-30%, -34%) rotate(0deg); opacity: 1; }
  to { transform: translate(-38%, -42%) rotate(-5deg); opacity: 0.94; }
}

.arch-landing .hero-min h1 {
  font-size: clamp(2.3rem, 5vw, 3.6rem);
  margin: 0 auto;
  opacity: 0;
  transform: translateY(8px);
  animation: arch-mask-in 700ms cubic-bezier(0.16, 1, 0.3, 1) forwards;
}
.arch-landing .hero-min .hero-ctas {
  margin-top: 2.5rem;
  opacity: 0;
  transform: translateY(8px);
  animation: arch-mask-in 700ms cubic-bezier(0.16, 1, 0.3, 1) 120ms forwards;
}
@keyframes arch-mask-in { to { opacity: 1; transform: none; } }
.arch-landing .hero-min .btn { min-width: 12.5rem; justify-content: center; }
.arch-landing .h1-break { display: none; }
/* one line on wide desktop; natural balance-wrap at mid widths (where a single
   line would overflow and clip); controlled break ≤640px */
@media (min-width: 1200px) { .arch-landing .hero-min h1 { white-space: nowrap; } }

/* ============ route scaffolding ============ */

.arch-landing .route { min-height: 60vh; }
.arch-landing .lede { margin-top: 1.1rem; font-size: 1.06rem; }
.arch-landing .chip-row { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-top: 1.4rem; }
.arch-landing .sub-crumb { margin-top: clamp(3.5rem, 8vh, 5.5rem); scroll-margin-top: 90px; }
.arch-landing .route-cta { margin-top: clamp(2rem, 5vh, 3rem); display: flex; flex-direction: column; align-items: flex-start; gap: 0.8rem; }
.arch-landing .cta-note { font-size: 0.8rem; color: var(--ink-faint); }

/* ============ /research ============ */

.arch-landing .r-cards { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem; margin-top: clamp(1.8rem, 4vh, 2.6rem); }

.arch-landing .notify-form { display: flex; gap: 0.6rem; margin-top: clamp(1.8rem, 4vh, 2.4rem); max-width: 26rem; }
.arch-landing .notify-input {
  flex: 1;
  min-width: 0;
  padding: 0.72em 1.1em;
  border: 1px solid var(--hairline);
  border-radius: var(--r-chip);
  background: var(--card);
  font-family: var(--sans);
  font-size: 0.92rem;
  color: var(--ink);
}
.arch-landing .notify-input::placeholder { color: var(--ink-faint); }
.arch-landing .notify-done { margin-top: 1.6rem; display: inline-flex; align-items: center; gap: 0.6rem; color: var(--ink-soft); font-size: 0.92rem; }

/* ============ /data ============ */

/* trace draw-on-scroll (moved from old hero) */
.arch-landing .trace-scroll .trace,
.arch-landing .trace-scroll .trace-orange { animation: none; }
.arch-landing .trace-scroll .trace { stroke-dasharray: 1; stroke-dashoffset: 1; opacity: 1; }
.arch-landing .trace-scroll.in .trace-shared { animation: arch-draw-once 700ms cubic-bezier(0.16, 1, 0.3, 1) forwards; }
.arch-landing .trace-scroll.in .trace-green { animation: arch-draw-once 700ms cubic-bezier(0.16, 1, 0.3, 1) 550ms forwards; }
.arch-landing .trace-scroll.in .trace-green2 { animation: arch-draw-once 700ms cubic-bezier(0.16, 1, 0.3, 1) 700ms forwards; }
.arch-landing .trace-scroll .trace-orange { opacity: 0; }
.arch-landing .trace-scroll.in .trace-orange { animation: arch-fade-once 600ms ease 800ms forwards; }
.arch-landing .trace-scroll .trace-node { fill: var(--card); stroke: var(--ink); stroke-width: 1.2; opacity: 0; }
.arch-landing .trace-scroll.in .trace-node { animation: arch-node-pulse 900ms ease 1150ms forwards; transform-origin: center; transform-box: fill-box; }
@keyframes arch-draw-once { from { stroke-dashoffset: 1; } to { stroke-dashoffset: 0; } }
@keyframes arch-fade-once { to { opacity: 1; } }
@keyframes arch-node-pulse {
  0% { opacity: 0; transform: scale(0.6); }
  45% { opacity: 1; transform: scale(1.35); }
  100% { opacity: 1; transform: scale(1); }
}
.arch-landing .pillar-trace { margin-top: 1.6rem; opacity: 0.8; }
.arch-landing .pillar-trace svg { width: 100%; height: clamp(80px, 11vw, 150px); display: block; }

.arch-landing .sample-link { margin-top: 1.4rem; }

/* slide-over drawer (sample record) */
.arch-landing .drawer-overlay {
  position: fixed;
  inset: 0;
  z-index: 70;
  background: rgba(238, 240, 239, 0.75);
  backdrop-filter: blur(6px);
  -webkit-backdrop-filter: blur(6px);
  opacity: 0;
  animation: arch-fade-once 200ms ease forwards;
}
.arch-landing .drawer {
  position: fixed;
  top: 0; right: 0; bottom: 0;
  z-index: 71;
  width: min(560px, 94vw);
  background: var(--card);
  border-left: 1px solid var(--hairline);
  box-shadow: var(--shadow-float);
  display: flex;
  flex-direction: column;
  transform: translateX(24px);
  opacity: 0;
  animation: arch-drawer-in 320ms cubic-bezier(0.16, 1, 0.3, 1) forwards;
}
@keyframes arch-drawer-in { to { transform: none; opacity: 1; } }
.arch-landing .drawer-head {
  display: flex;
  align-items: center;
  gap: 0.8rem;
  padding: 1.1rem 1.4rem;
  border-bottom: 1px solid var(--hairline);
}
.arch-landing .drawer-head .chrome { flex: 1; }
.arch-landing .drawer-close {
  font-size: 1.3rem;
  color: var(--ink-faint);
  line-height: 1;
  padding: 0.2rem 0.5rem;
  border-radius: 8px;
  cursor: pointer;
}
.arch-landing .drawer-close:hover { color: var(--ink); background: var(--card-in); }
.arch-landing .drawer-body { flex: 1; overflow-y: auto; padding: 1.2rem 1.4rem; }
.arch-landing .code-block {
  margin: 0;
  padding: 1rem 1.1rem;
  border: 1px solid var(--hairline);
  border-radius: var(--r-sm);
  background: var(--card-in);
  font-family: var(--mono);
  font-size: 0.72rem;
  line-height: 1.55;
  color: var(--ink-soft);
  overflow-x: auto;
  white-space: pre;
}
.arch-landing .drawer-foot { padding: 1rem 1.4rem 1.3rem; border-top: 1px solid var(--hairline); display: flex; gap: 0.7rem; align-items: center; }
.arch-landing .drawer-note { font-size: 0.72rem; color: var(--ink-faint); }

/* statement block (02.2 positioning line) */
.arch-landing .env-statement {
  margin: clamp(2rem, 5vh, 3rem) auto 0;
  text-align: center;
}
.arch-landing .env-statement .big { font-size: clamp(1.3rem, 2.4vw, 1.8rem); color: var(--ink); letter-spacing: -0.01em; max-width: none; }
.arch-landing .env-statement .sub { margin: 0.5rem auto 0; font-size: 0.95rem; }

/* environment diagram */
.arch-landing .env-card { margin-top: clamp(1.8rem, 4vh, 2.6rem); padding: 1.6rem 1.6rem 1.2rem; }
.arch-landing .env-svg { width: 100%; height: auto; display: block; }
.arch-landing .env-lane-line { stroke: var(--hairline); stroke-width: 1; }
.arch-landing .env-lane-label { font-family: var(--mono); font-size: 10.5px; letter-spacing: 0.09em; text-transform: uppercase; fill: var(--ink-faint); }
.arch-landing .env-path { fill: none; stroke: rgba(26, 27, 26, 0.3); stroke-width: 1.4; stroke-linecap: round; }
.arch-landing .env-node { stroke-width: 1.2; transition: opacity 200ms ease; }
.arch-landing .env-node-label { font-family: var(--mono); font-size: 9.5px; letter-spacing: 0.06em; text-transform: uppercase; fill: var(--ink-soft); }
.arch-landing .env-node-label.dim, .arch-landing .env-node.dim { opacity: 0.22; }
.arch-landing .env-scrub { display: flex; align-items: center; gap: 0.9rem; margin-top: 1rem; }
.arch-landing .env-scrub .chrome { font-size: 0.58rem; white-space: nowrap; }
.arch-landing .env-range {
  -webkit-appearance: none;
  appearance: none;
  flex: 1;
  height: 2px;
  border-radius: 2px;
  background: var(--hairline);
  cursor: pointer;
}
.arch-landing .env-range::-webkit-slider-thumb {
  -webkit-appearance: none;
  appearance: none;
  width: 14px; height: 14px;
  border-radius: 50%;
  background: var(--card);
  border: 1.5px solid var(--ink);
  cursor: grab;
}
.arch-landing .env-range::-moz-range-thumb {
  width: 14px; height: 14px;
  border-radius: 50%;
  background: var(--card);
  border: 1.5px solid var(--ink);
  cursor: grab;
}
.arch-landing .env-steps-mobile { display: none; list-style: none; }

/* benchmarks table */
.arch-landing .bench-wrap { margin-top: clamp(1.8rem, 4vh, 2.6rem); overflow-x: auto; }
.arch-landing .bench { width: 100%; border-collapse: collapse; min-width: 640px; }
.arch-landing .bench th {
  font-family: var(--mono);
  font-size: 0.62rem;
  font-weight: 400;
  letter-spacing: 0.09em;
  text-transform: uppercase;
  color: var(--ink-faint);
  text-align: left;
  padding: 0 0.9em 0.7em 0;
}
.arch-landing .bench td {
  padding: 0.85em 0.9em 0.85em 0;
  border-top: 1px solid var(--hairline);
  font-size: 0.88rem;
  color: var(--ink-soft);
  vertical-align: top;
}
.arch-landing .bench td:first-child { color: var(--ink); font-weight: 500; white-space: nowrap; }

.arch-landing .eval-pack { margin-top: clamp(1.8rem, 4vh, 2.4rem); max-width: 30rem; }
.arch-landing .eval-pack .chrome-box { font-size: 0.6rem; margin-bottom: 1rem; }
.arch-landing .eval-pack p { font-size: 0.92rem; }

/* physical AI waveform */
.arch-landing .wave-card { margin-top: clamp(1.8rem, 4vh, 2.6rem); padding: 1.8rem 1.6rem 1.4rem; }
.arch-landing .wave-card svg { width: 100%; height: auto; display: block; }
.arch-landing .wave-path { fill: none; stroke: var(--orange); stroke-width: 1.5; stroke-linecap: round; stroke-dasharray: 1; stroke-dashoffset: 1; }
.arch-landing .wave-base { fill: none; stroke: var(--hairline); stroke-width: 1; }
.arch-landing .reveal.in .wave-path { animation: arch-draw-once 1400ms cubic-bezier(0.16, 1, 0.3, 1) forwards; }
.arch-landing .wave-pulse { fill: var(--orange); opacity: 0; transform-origin: center; transform-box: fill-box; }
.arch-landing .reveal.in .wave-pulse { animation: arch-node-pulse 1100ms ease 1200ms forwards; }

/* ============ /health-systems ============ */

.arch-landing .flow { display: flex; align-items: stretch; gap: 0.5rem; margin-top: clamp(2rem, 5vh, 3rem); }
.arch-landing .flow-stage {
  flex: 1;
  background: var(--card);
  border: 1px solid var(--hairline);
  border-radius: var(--r-sm);
  padding: 1.1rem 1rem 1.2rem;
  box-shadow: var(--shadow-card);
  opacity: 0;
  transition: opacity 600ms ease;
}
.arch-landing .flow.in .flow-stage { opacity: 1; }
.arch-landing .flow-stage .chrome { display: block; font-size: 0.58rem; margin-bottom: 0.55rem; }
.arch-landing .flow-stage .fs-title { display: block; font-size: 0.95rem; font-weight: 500; color: var(--ink); }
.arch-landing .flow-stage .fs-sub { display: block; margin-top: 0.35rem; font-size: 0.72rem; color: var(--ink-faint); }
.arch-landing .flow-arrow { align-self: center; color: var(--ink-faint); font-size: 0.85rem; opacity: 0; transition: opacity 600ms ease; flex: none; }
.arch-landing .flow.in .flow-arrow { opacity: 0.7; }

.arch-landing .trust-rows { margin-top: clamp(2rem, 5vh, 3rem); border-top: 1px solid var(--hairline); }
.arch-landing .trust-row { border-bottom: 1px solid var(--hairline); }
.arch-landing .trust-row.reveal { transform: none; } /* opacity only — stillness reads as seriousness */
.arch-landing .trust-btn {
  display: flex;
  align-items: baseline;
  gap: 1.1rem;
  width: 100%;
  padding: 1.05rem 0.4rem;
  text-align: left;
  cursor: pointer;
}
.arch-landing .trust-btn .chrome { min-width: 12em; flex: none; }
.arch-landing .trust-line { font-size: 0.95rem; color: var(--ink); flex: 1; }
.arch-landing .trust-btn .menu-chev { align-self: center; }
.arch-landing .trust-body {
  display: grid;
  grid-template-rows: 0fr;
  transition: grid-template-rows 240ms cubic-bezier(0.16, 1, 0.3, 1);
}
.arch-landing .trust-body.open { grid-template-rows: 1fr; }
.arch-landing .trust-body-inner { overflow: hidden; min-height: 0; }
.arch-landing .trust-body p { padding: 0 0.4rem 1.1rem calc(12em + 1.5rem); font-size: 0.88rem; }
.arch-landing .trust-tag { font-size: 0.6rem; padding: 0.3em 0.8em; margin-left: 0.6rem; }

/* ============ /physicians ============ */

.arch-landing .pay-card {
  margin-top: clamp(2rem, 5vh, 3rem);
  padding: 1.9rem 1.8rem 1.7rem;
  max-width: 26rem;
}
.arch-landing .pay-card .doto { font-size: clamp(2.9rem, 5vw, 4.2rem); color: var(--ink); }
.arch-landing .pay-card .per { font-size: 0.45em; color: var(--ink-faint); letter-spacing: 0; }
.arch-landing .pay-card .label { display: block; margin-top: 0.7rem; }

.arch-landing .steps-strip { position: relative; display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem; margin-top: clamp(1.8rem, 4vh, 2.6rem); }
.arch-landing .steps-strip::before {
  content: '';
  position: absolute;
  top: 2.2rem;
  left: 4%;
  width: 92%;
  height: 1px;
  background: var(--hairline);
  transform: scaleX(0);
  transform-origin: left;
  transition: transform 1200ms cubic-bezier(0.16, 1, 0.3, 1) 200ms;
}
.arch-landing .steps-strip.in::before { transform: scaleX(1); }
.arch-landing .step-card {
  position: relative;
  background: var(--card);
  border: 1px solid var(--hairline);
  border-radius: var(--r-md);
  padding: 1.5rem 1.4rem 1.6rem;
  box-shadow: var(--shadow-card);
  display: flex;
  flex-direction: column;
  gap: 0.8rem;
}
.arch-landing .step-card .step-n { font-size: 1.6rem; color: var(--ink-faint); }
.arch-landing .step-card .chrome-box { align-self: flex-start; font-size: 0.6rem; padding: 0.5em 0.9em; }
.arch-landing .step-card h3 { font-size: 1.05rem; }
.arch-landing .step-card p { font-size: 0.85rem; }

.arch-landing .fr-rows { margin-top: clamp(1.8rem, 4vh, 2.6rem); border-top: 1px solid var(--hairline); max-width: 46rem; }
.arch-landing .fr-row {
  display: flex;
  align-items: baseline;
  gap: 1.1rem;
  padding: 0.95rem 0.3rem;
  border-bottom: 1px solid var(--hairline);
}
.arch-landing .fr-row .chrome { min-width: 11em; flex: none; }
.arch-landing .fr-row p { font-size: 0.92rem; color: var(--ink-soft); max-width: none; }

.arch-landing .closing-line { margin-top: 1.6rem; color: var(--ink); font-size: 1.02rem; }

/* ============ /mission ============ */

.arch-landing .thesis { margin-top: clamp(2rem, 5vh, 3rem); display: flex; flex-direction: column; gap: 0.9rem; }
.arch-landing .thesis p { font-size: clamp(1.05rem, 1.7vw, 1.25rem); color: var(--ink); max-width: 44rem; }
.arch-landing .thesis p:nth-child(3) { color: var(--ink-soft); }
.arch-landing .thesis-byline {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 0.5rem;
  margin-top: 0.8rem;
  font-size: 0.95rem;
  color: var(--ink-soft);
}
.arch-landing .thesis-byline .chrome { color: var(--ink-faint); }
.arch-landing .thesis-byline a { color: var(--ink); text-decoration: underline; text-underline-offset: 3px; text-decoration-color: var(--hairline); }
.arch-landing .thesis-byline .amp { color: var(--ink-faint); }

.arch-landing .team { display: grid; grid-template-columns: repeat(2, 1fr); gap: 1rem; margin-top: clamp(1.8rem, 4vh, 2.6rem); max-width: 52rem; }
.arch-landing .team-card {
  display: flex;
  flex-direction: column;
  gap: 0.85rem;
  padding: 1.7rem 1.6rem 1.8rem;
}
.arch-landing .t-avatar {
  width: 52px; height: 52px;
  border-radius: 50%;
  background: var(--card-in);
  border: 1px solid var(--hairline);
  display: grid;
  place-items: center;
  font-family: var(--mono);
  font-size: 0.85rem;
  letter-spacing: 0.06em;
  color: var(--ink-soft);
}
.arch-landing .team-card h3 { font-size: 1.2rem; }
.arch-landing .t-role { font-size: 0.9rem; color: var(--ink-soft); }
.arch-landing .t-aff { display: inline-flex; align-items: center; gap: 0.5rem; }
.arch-landing .team-card .chrome-box { align-self: flex-start; font-size: 0.6rem; margin-top: 0.3rem; }

.arch-landing .contact-rows { margin-top: clamp(1.8rem, 4vh, 2.6rem); border-top: 1px solid var(--hairline); max-width: 34rem; }
.arch-landing .contact-row {
  display: flex;
  align-items: baseline;
  gap: 1.1rem;
  padding: 0.95rem 0.3rem;
  border-bottom: 1px solid var(--hairline);
}
.arch-landing .contact-row .chrome { min-width: 9em; flex: none; }
.arch-landing .contact-row a { font-size: 0.92rem; text-decoration: underline; text-underline-offset: 3px; text-decoration-color: var(--hairline); }
.arch-landing .place-line { margin-top: clamp(1.8rem, 4vh, 2.4rem); display: inline-flex; align-items: center; gap: 0.7rem; }

/* footer nav additions */
.arch-landing .foot-nav { display: flex; gap: 1.1rem; align-items: baseline; }
.arch-landing .foot-nav a { font-size: 0.85rem; }

/* ============ responsive ============ */

@media (max-width: 900px) {
  .arch-landing .r-cards, .arch-landing .steps-strip { grid-template-columns: 1fr; }
  .arch-landing .steps-strip::before { display: none; }
  .arch-landing .flow { flex-direction: column; }
  .arch-landing .flow-arrow { transform: rotate(90deg); align-self: flex-start; margin-left: 1.4rem; }
  .arch-landing .team { grid-template-columns: 1fr; }
}

@media (max-width: 720px) {
  .arch-landing .env-svg-wrap, .arch-landing .env-scrub { display: none; }
  .arch-landing .env-steps-mobile { display: flex; flex-direction: column; gap: 0; margin-top: 0.4rem; }
  .arch-landing .env-step-m { display: flex; align-items: center; gap: 0.8rem; padding: 0.62em 0.2em; border-top: 1px solid var(--hairline); font-size: 0.88rem; color: var(--ink-soft); }
  .arch-landing .env-step-m:first-child { border-top: none; }
  .arch-landing .trust-btn { flex-wrap: wrap; gap: 0.35rem 1.1rem; }
  .arch-landing .trust-line { flex-basis: 100%; }
  .arch-landing .trust-body p { padding-left: 0.4rem; }
}

@media (max-width: 640px) {
  .arch-landing .menu-row-btn { min-height: 64px; gap: 0.9rem; }
  .arch-landing .menu-row-btn .chrome { min-width: 0; }
  .arch-landing .menu-trigger .menu-label { display: none; }
  .arch-landing .h1-break { display: block; }
  .arch-landing .hero-min .hero-ctas { flex-direction: column; align-items: stretch; }
  .arch-landing .hero-min .btn { width: 100%; min-height: 44px; }
  .arch-landing .notify-form { flex-direction: column; }
  .arch-landing .fr-row, .arch-landing .contact-row { flex-direction: column; gap: 0.3rem; }
  .arch-landing .bench { min-width: 0; }
  .arch-landing .bench, .arch-landing .bench tbody, .arch-landing .bench tr, .arch-landing .bench td { display: block; }
  .arch-landing .bench thead { display: none; }
  .arch-landing .bench tr { border: 1px solid var(--hairline); border-radius: var(--r-sm); background: var(--card); padding: 0.9rem 1rem; margin-top: 0.8rem; }
  .arch-landing .bench td { border: none; padding: 0.25em 0; }
  .arch-landing .bench td:first-child { white-space: normal; } /* long benchmark names wrap in stacked cards */
  .arch-landing .bench td::before {
    content: attr(data-th);
    display: block;
    font-family: var(--mono);
    font-size: 0.58rem;
    letter-spacing: 0.09em;
    text-transform: uppercase;
    color: var(--ink-faint);
    margin: 0.5em 0 0.15em;
  }
}

/* ============ reduced motion ============ */

@media (prefers-reduced-motion: reduce) {
  .arch-landing .menu-overlay { animation-duration: 1ms; }
  .arch-landing .menu-open .menu-row-btn,
  .arch-landing .menu-sub.open .menu-sub-item { animation-duration: 1ms; animation-delay: 0ms !important; }
  .arch-landing .menu-sub, .arch-landing .trust-body, .arch-landing .menu-chev { transition: none; }
  .arch-landing .hero-min .glow-a, .arch-landing .hero-min .glow-b { animation: none; }
  .arch-landing .hero-min h1, .arch-landing .hero-min .hero-ctas { animation: arch-fade-once 120ms ease forwards; transform: none; }
  .arch-landing .trace-scroll .trace, .arch-landing .trace-scroll .trace-orange,
  .arch-landing .trace-scroll .trace-node, .arch-landing .wave-path, .arch-landing .wave-pulse {
    animation: none !important; stroke-dashoffset: 0; opacity: 1;
  }
  .arch-landing .steps-strip::before { transition: none; transform: scaleX(1); }
  .arch-landing .flow-stage, .arch-landing .flow-arrow { transition: none; }
  .arch-landing .drawer, .arch-landing .drawer-overlay { animation-duration: 1ms; }
}
`;
