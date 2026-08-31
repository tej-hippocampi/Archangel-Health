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

/* "Who qualifies" said "Board-certified or board-eligible", which is both
   narrower than the policy and American-only phrasing: it reads as a refusal to
   a retired cardiologist, a resident, and every physician licensed outside the
   US. The wizard has supported all three for a while (registry/config.py covers
   15 countries and falls back to document review for the rest, and
   currently_practicing is a 0-5 scale rather than a binary). The page was the
   last thing still saying no.
   The "Time" row lost "10-15 minutes per case": multiplied against the hourly
   figure above it, it turned a range into a per-case promise. */
const FRICTION = [
  { tag: "Time", line: "Async, no minimums, no shifts. Take one case or twenty, on your own hours." },
  { tag: "Not patient care", line: "Annotation of de-identified cases. No patient contact, no clinical liability." },
  { tag: "Who qualifies", line: "Any physician with a medical degree. Retired, in training, and internationally licensed all qualify. Credentials verified before your first case." },
  { tag: "Where you are", line: "Anywhere in the world. Registry checks run automatically for the US, India and Pakistan; every other country is verified by document review." },
  { tag: "Specialties", line: "Nephrology, cardiology, primary care medicine, oncology, radiology. More opening." },
  { tag: "Attribution", line: "Your credentials travel with every record you ratify." },
];

/* What actually moves a rate. Named on the page so the figure above reads as a
   range and not as a quote: pay is set per case, and payout.py can settle a
   graded case below its posted rate (floor 0.60x, ceiling 1.25x). The
   multipliers themselves belong in the signup attestation, not in marketing. */
const RATE_FACTORS = [
  "Your specialty, and how hard the case is",
  "Your experience level",
  "Where you practice",
  "How the finished case is graded",
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
        <div className="route-header reveal">
          <h2>Your expertise will power the future of medicine.</h2>
          <button type="button" className="btn btn-lime route-header-cta" onClick={actions.openPhysicianOnboard}>
            Become a contributor
          </button>
        </div>

        {/* Two ways to earn — annotate & refer, side by side, equal weight.
            Typography on canvas, no card (PRD §3). */}
        <div className="pay-band reveal">
          <p className="lede pay-band-lede">The AI being built now will practice alongside you. You decide what it learns.</p>
          <div className="earnings">
            <span className="chrome earnings-head">Two ways to earn</span>
            <div className="earn-grid">
              <div className="earn-way">
                <span className="chrome chrome-box"><span className="dot dot-green" />Annotate</span>
                <PayFigure />
                <span className="chrome pay-qualifier">Typical range, not a guarantee</span>
                <span className="label">Every case is priced on its own. What you earn depends on:</span>
                <ul className="rate-factors">
                  {RATE_FACTORS.map((f) => <li key={f}>{f}</li>)}
                </ul>
              </div>
              <div className="earn-way">
                <span className="chrome chrome-box"><span className="dot dot-green" />Refer</span>
                {/* $50, matching payments.referral_bounty_cents. This said
                    "$50-$100", a range that matched no constant anywhere in
                    the codebase and would have been half wrong on the first
                    payout. There is no ceiling on how many you can make. */}
                <span className="doto">$50<span className="per"> / referral</span></span>
                <span className="label">
                  For every physician you refer whose first case is accepted, and $25 to them.
                  No limit on how many.
                </span>
                {/* The bounty is flat and the eligibility is wide, but neither was
                    stated, so referrers self-censored against the "board-certified"
                    line further down the page and never sent the retired colleague
                    or the friend doing a fellowship in Karachi. */}
                <span className="label refer-eligible">
                  Refer any physician, anywhere in the world. Retired doctors, residents
                  and fellows, and physicians licensed outside the US all count.
                </span>
              </div>
            </div>
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
      </section>
    </div>
  );
}
