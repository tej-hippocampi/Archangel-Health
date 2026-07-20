/**
 * `/health-systems` — institutional, calm, conspicuously trustworthy (PRD §6).
 * The data-flow diagram carries the trust argument; below it, static two-column
 * trust rows (label + one line, aligned) state the posture without disclosure.
 * Minimal motion — opacity only, stillness reads as seriousness.
 */

import { Fragment } from "react";
import type { ShellActions } from "../ArchShell";

const FLOW = [
  { chrome: "Stage 1", title: "Your record", sub: "" },
  { chrome: "Stage 2", title: "De-identified", sub: "Expert Determination — dates shifted, not deleted" },
  { chrome: "Stage 3", title: "Watermarked", sub: "& traceable" },
  { chrome: "Stage 4", title: "Licensed", sub: "to a named end-buyer" },
  { chrome: "Stage 5", title: "Never resold", sub: "beyond license" },
];

const TRUST_ROWS: { tag: string; line: string; onRequest?: boolean }[] = [
  {
    tag: "De-identification",
    line: "Expert Determination, not Safe Harbor. Dates shifted, so intervals survive.",
  },
  {
    tag: "We do the work",
    line: "Adapters, PHI scanning, risk assessment. You don't build a pipeline.",
  },
  {
    tag: "Security",
    line: "Encrypted in transit and at rest. Least privilege. Every access logged.",
  },
  {
    tag: "Where your data goes",
    line: "Watermarked, traceable, licensed per named buyer. Never resold.",
  },
  {
    tag: "Governance",
    line: "We work inside your existing governance and IRB review.",
  },
  {
    tag: "Data can stay put",
    line: "Federated evaluation: the model and grader travel to you. Records never leave your premises.",
    onRequest: true,
  },
];

export function HealthSystemsPage({ actions }: { actions: ShellActions }) {
  return (
    <div className="route">
      <section className="section">
        <p className="crumb chrome reveal"><span className="root">Archangel</span><span className="sep">/</span><span className="here">03 · Health systems</span></p>
        <div className="reveal">
          <h2>The bottleneck is data only you hold.</h2>
          <p className="lede">Longitudinal, de-identified patient data is the raw material for medical AI.</p>
        </div>

        <div className="chip-row reveal" aria-label="What we're looking for">
          <span className="chip">Lab panels</span>
          <span className="chip">Clinical notes</span>
          <span className="chip">Medications</span>
          <span className="chip">Imaging</span>
          <span className="chip">Longitudinal outcomes</span>
        </div>

        {/* The trust block — a diagram, not an essay. Stages illuminate on scroll. */}
        <div className="flow" role="img" aria-label="Data flow: your record is de-identified under Expert Determination with dates shifted, watermarked and traceable, licensed to a named end-buyer, and never resold beyond license.">
          {FLOW.map((s, i) => (
            <Fragment key={s.title}>
              <div className="flow-stage" style={{ transitionDelay: `${i * 220}ms` }}>
                <span className="chrome">{s.chrome}</span>
                <span className="fs-title">{s.title}</span>
                {s.sub && <span className="fs-sub">{s.sub}</span>}
              </div>
              {i < FLOW.length - 1 && (
                <span className="flow-arrow" style={{ transitionDelay: `${i * 220 + 110}ms` }} aria-hidden="true">→</span>
              )}
            </Fragment>
          ))}
        </div>

        {/* Trust rows — label + one line, aligned in two columns. No disclosure. */}
        <div className="trust-rows">
          {TRUST_ROWS.map((r, i) => (
            <div className="trust-row reveal" key={r.tag} style={{ transitionDelay: `${i * 60}ms` }}>
              <span className="trust-label">
                <span className="chrome">{r.tag}</span>
                {r.onRequest && <span className="chip trust-tag">On request</span>}
              </span>
              <span className="trust-line">{r.line}</span>
            </div>
          ))}
        </div>

        <div className="route-cta reveal">
          <button type="button" className="btn btn-primary" onClick={() => actions.openLead("provide_data")}>
            Become a data partner
          </button>
        </div>
      </section>
    </div>
  );
}
