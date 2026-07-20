/**
 * `/health-systems` — institutional, calm, conspicuously trustworthy (PRD §6).
 * The data-flow diagram carries the trust argument; five collapsed rows hold
 * the compliance depth. Minimal motion — opacity only, stillness reads as
 * seriousness.
 */

import { Fragment, useState } from "react";
import type { ShellActions } from "../ArchShell";

const FLOW = [
  { chrome: "Stage 1", title: "Your record", sub: "" },
  { chrome: "Stage 2", title: "De-identified", sub: "Expert Determination — dates shifted, not deleted" },
  { chrome: "Stage 3", title: "Watermarked", sub: "& traceable" },
  { chrome: "Stage 4", title: "Licensed", sub: "to a named end-buyer" },
  { chrome: "Stage 5", title: "Never resold", sub: "beyond license" },
];

const TRUST_ROWS: { tag: string; line: string; body: string; onRequest?: boolean }[] = [
  {
    tag: "De-identification",
    line: "Expert Determination, not Safe Harbor. Dates shifted, so intervals survive.",
    body:
      "Safe Harbor destroys temporal structure — the intervals between labs, decisions, and outcomes — which is exactly what makes clinical data scientifically useful. Expert Determination preserves it: dates are consistently shifted per patient, quasi-identifiers are assessed for re-identification risk, and free text — the hardest de-identification surface — is scanned and scrubbed before anything moves.",
  },
  {
    tag: "We do the work",
    line: "Adapters, PHI scanning, risk assessment. You don't build a pipeline.",
    body:
      "We adapt to your source formats — FHIR R4, HL7v2, lab CSV, free-text notes — and run PHI scanning and re-identification risk assessment on our side. Your team's lift is access and governance, not engineering.",
  },
  {
    tag: "Security",
    line: "Encrypted in transit and at rest. Least privilege. Every access logged.",
    body:
      "Partner data lives in segregated per-partner environments. Access follows least privilege, every access is logged and attributable to a person, and encryption applies in transit and at rest.",
  },
  {
    tag: "Where your data goes",
    line: "Watermarked, traceable, licensed per named buyer. Never resold.",
    body:
      "Every shipped record is watermarked and traceable to its license. Licenses name the end-buyer; resale beyond the license is contractually excluded, and the watermark makes leakage attributable.",
  },
  {
    tag: "Governance",
    line: "We work inside your existing governance and IRB review.",
    body:
      "Your data-governance committee and IRB processes stay in the loop — we structure agreements and reviews around your existing controls rather than asking you to create new ones.",
  },
  {
    tag: "Data can stay put",
    line: "Federated evaluation: the model and grader travel to you. Records never leave your premises.",
    body:
      "For partners who cannot move data at all, we support a federated arrangement: evaluation harnesses and graders run inside your perimeter, and only scores leave.",
    onRequest: true,
  },
];

export function HealthSystemsPage({ actions }: { actions: ShellActions }) {
  const [open, setOpen] = useState<string | null>(null);

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

        {/* Five collapsed rows — depth one click down. Opacity-only reveals. */}
        <div className="trust-rows">
          {TRUST_ROWS.map((r, i) => {
            const isOpen = open === r.tag;
            return (
              <div className="trust-row reveal" key={r.tag} style={{ transitionDelay: `${i * 60}ms` }}>
                <button
                  type="button"
                  className="trust-btn"
                  aria-expanded={isOpen}
                  onClick={() => setOpen(isOpen ? null : r.tag)}
                >
                  <span className="chrome">
                    {r.tag}
                    {r.onRequest && <span className="chip trust-tag">On request</span>}
                  </span>
                  <span className="trust-line">{r.line}</span>
                  <span className={`menu-chev${isOpen ? " openv" : ""}`} aria-hidden="true">›</span>
                </button>
                <div className={`trust-body${isOpen ? " open" : ""}`}>
                  <div className="trust-body-inner">
                    <p>{r.body}</p>
                  </div>
                </div>
              </div>
            );
          })}
        </div>

        <div className="route-cta reveal">
          <button type="button" className="btn btn-primary" onClick={() => actions.openLead("provide_data")}>
            Become a data partner
          </button>
          <p className="cta-note">We'll walk through economics after an initial conversation.</p>
        </div>
      </section>
    </div>
  );
}
