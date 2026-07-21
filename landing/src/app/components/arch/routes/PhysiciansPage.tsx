/**
 * `/physicians` — the offer first, signup last (PRD §7). Pay above the fold,
 * 3-step strip, friction-removers table, then the CTA that mints a self-serve
 * onboarding link (the same magic link the admin console issues).
 */

import { useEffect, useRef, useState } from "react";
import type { ShellActions } from "../ArchShell";

const STEPS = [
  { n: "1", tag: "Case", title: "Work through a real de-identified case.", line: "Labs, notes, medications, imaging." },
  { n: "2", tag: "Response", title: "Review an AI model's answer to it.", line: "The full reasoning, not just the conclusion." },
  { n: "3", tag: "Judgment", title: "Annotate, correct, refine, rate.", line: "Mark where the reasoning breaks. Write what's right." },
];

const FRICTION = [
  { tag: "Time", line: "10–15 minutes per case. Async, no minimums, no shifts." },
  { tag: "Not patient care", line: "Annotation of de-identified cases. No patient contact, no clinical liability." },
  { tag: "Who qualifies", line: "Board-certified or board-eligible. Credentials verified before your first case." },
  { tag: "Specialties", line: "Nephrology, cardiology, primary care medicine, oncology, radiology — more opening." },
  { tag: "Attribution", line: "Your credentials travel with every record you ratify." },
];

/** Doto numeral counts 150 → 300 once on entry, then rests (PRD §7 motion). */
function PayFigure() {
  const ref = useRef<HTMLSpanElement | null>(null);
  // Rest at the correct full range (300); the count-up resets to 150 and climbs
  // only once the figure enters view — so it never sits showing "$150–$150+".
  const [n, setN] = useState(300);
  const ran = useRef(false);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches || !("IntersectionObserver" in window)) {
      return; // already resting at 300
    }
    let raf = 0;
    const io = new IntersectionObserver(
      (entries) => {
        if (!entries.some((e) => e.isIntersecting) || ran.current) return;
        ran.current = true;
        io.disconnect();
        const t0 = performance.now();
        const dur = 1200;
        const tick = (t: number) => {
          const p = Math.min(1, (t - t0) / dur);
          const eased = 1 - Math.pow(1 - p, 3);
          setN(Math.round(150 + eased * 150));
          if (p < 1) raf = requestAnimationFrame(tick);
        };
        raf = requestAnimationFrame(tick);
      },
      { threshold: 0.4 }
    );
    io.observe(el);
    return () => {
      io.disconnect();
      if (raf) cancelAnimationFrame(raf);
    };
  }, []);

  return (
    <span ref={ref} className="doto">
      $150–${n}+<span className="per"> / hour</span>
    </span>
  );
}

export function PhysiciansPage({ actions }: { actions: ShellActions }) {
  return (
    <div className="route">
      <section className="section">
        <p className="crumb chrome reveal"><span className="root">Archangel</span><span className="sep">/</span><span className="here">04 · Physicians</span></p>
        <div className="reveal">
          <h2>Your expertise will power the future of medicine.</h2>
        </div>

        {/* Lede left, pay right — typography on canvas, no card (PRD §3). */}
        <div className="pay-band reveal">
          <p className="lede pay-band-lede">The AI being built now will practice alongside you. You decide what it learns.</p>
          <div className="pay-figure">
            <PayFigure />
            <span className="label">Varies per task — difficulty, specialty, depth.</span>
          </div>
        </div>

        {/* What you'll do — 3-step strip, one line each. */}
        <div className="steps-strip">
          {STEPS.map((s) => (
            <div className="step-card reveal" key={s.n}>
              <span className="doto step-n" aria-hidden="true">{s.n}</span>
              <span className="chrome chrome-box"><span className="dot dot-green" />{s.tag}</span>
              <h3>{s.title}</h3>
              <p>{s.line}</p>
            </div>
          ))}
        </div>
        <p className="closing-line reveal">It's the thinking you already do on rounds, captured.</p>

        {/* Friction-removers — table, one short line each. */}
        <div className="fr-rows reveal">
          {FRICTION.map((f) => (
            <div className="fr-row" key={f.tag}>
              <span className="chrome">{f.tag}</span>
              <p>{f.line}</p>
            </div>
          ))}
        </div>

        {/* Signup comes only after the offer. */}
        <div className="route-cta reveal">
          <button type="button" className="btn btn-primary" onClick={actions.openPhysicianOnboard}>
            Become a contributor
          </button>
          <p className="cta-note">Onboarding takes a few minutes — your personal link is created instantly.</p>
        </div>
      </section>
    </div>
  );
}
