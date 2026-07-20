/**
 * `/data` — data buyers (PRD §5). Four pillars, each one headline + one line
 * + one visual. The anatomy block, pipeline, and quality report are preserved
 * verbatim from the original landing. 02.2's environment diagram is the
 * centerpiece; the sample record ships ungated in a drawer.
 */

import { useEffect, useRef, useState } from "react";
import { EnvDiagram } from "../EnvDiagram";
import type { ShellActions } from "../ArchShell";

/* ---------- ungated sample record (illustrative, de-identified) ---------- */

const SAMPLE_RECORD = {
  record_id: "arch-sample-0001",
  status: "illustrative_sample",
  de_identification: { method: "expert_determination", dates: "shifted_per_patient", phi: "removed" },
  modalities: ["labs", "vitals", "notes", "outcome"],
  case: {
    presentation: "Adult with fatigue, nausea, and declining urine output over two weeks.",
    labs: [
      { analyte: "Creatinine", value: 3.1, unit: "mg/dL", ref: "0.7–1.3", flag: "H" },
      { analyte: "Potassium", value: 5.9, unit: "mmol/L", ref: "3.5–5.0", flag: "H" },
      { analyte: "Bicarbonate", value: 16, unit: "mmol/L", ref: "22–29", flag: "L" },
      { analyte: "Hemoglobin", value: 9.4, unit: "g/dL", ref: "13.5–17.5", flag: "L" },
    ],
    notes_excerpt: "Narrative and labs pull in opposite directions; the plausible answer is wrong.",
  },
  model_probe: {
    model: "frontier_model",
    verdict: "failed",
    failure_mode: "anchored on the narrative; missed the lab-narrative divergence",
  },
  specialists: [
    { specialty: "nephrology", board_certified: true, position: "A" },
    { specialty: "nephrology", board_certified: true, position: "B" },
  ],
  adjudication: { divergence_score: 7.4, method: "multi_rater_adjudication" },
  outcome_90d: "Dialysis avoided — renal function recovered.",
  supervision: {
    preference_pair: { chosen: "expert_resolution", rejected: "plausible_hard_negative" },
    ideal_answer: "complete expert resolution (SFT)",
    reasoning_trace: "step-level expert reasoning (PRM)",
    provenance: "credentials, citations, difficulty score, versioning",
  },
};

const SAMPLE_SCHEMA = {
  $schema: "https://json-schema.org/draft/2020-12/schema",
  title: "Archangel clinical reasoning record",
  type: "object",
  required: ["record_id", "de_identification", "case", "model_probe", "specialists", "adjudication", "supervision"],
  properties: {
    record_id: { type: "string" },
    de_identification: {
      type: "object",
      properties: {
        method: { const: "expert_determination" },
        dates: { const: "shifted_per_patient" },
        phi: { const: "removed" },
      },
    },
    modalities: { type: "array", items: { enum: ["labs", "vitals", "notes", "imaging", "outcome"] } },
    case: {
      type: "object",
      properties: {
        presentation: { type: "string" },
        labs: {
          type: "array",
          items: {
            type: "object",
            required: ["analyte", "value", "unit", "ref"],
            properties: {
              analyte: { type: "string" },
              value: { type: "number" },
              unit: { type: "string" },
              ref: { type: "string" },
              flag: { enum: ["H", "L", null] },
            },
          },
        },
        notes_excerpt: { type: "string" },
      },
    },
    model_probe: {
      type: "object",
      properties: { model: { type: "string" }, verdict: { enum: ["failed", "passed"] }, failure_mode: { type: "string" } },
    },
    specialists: {
      type: "array",
      minItems: 2,
      items: {
        type: "object",
        properties: { specialty: { type: "string" }, board_certified: { type: "boolean" }, position: { type: "string" } },
      },
    },
    adjudication: {
      type: "object",
      properties: { divergence_score: { type: "number" }, method: { type: "string" }, kappa: { type: "number" } },
    },
    outcome_90d: { type: "string" },
    supervision: {
      type: "object",
      properties: {
        preference_pair: { type: "object" },
        ideal_answer: { type: "string" },
        reasoning_trace: { type: "string" },
        provenance: { type: "string" },
      },
    },
  },
};

function downloadSchema() {
  const blob = new Blob([JSON.stringify(SAMPLE_SCHEMA, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "archangel-record.schema.json";
  a.click();
  URL.revokeObjectURL(url);
}

function SampleDrawer({ onClose }: { onClose: () => void }) {
  const closeRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [onClose]);

  return (
    <>
      <div className="drawer-overlay" onMouseDown={onClose} />
      <div className="drawer" role="dialog" aria-modal="true" aria-label="Sample record">
        <div className="drawer-head">
          <span className="chrome">Sample record · JSON</span>
          <span className="chip chip-lime">de-identified</span>
          <button ref={closeRef} type="button" className="drawer-close" onClick={onClose} aria-label="Close">×</button>
        </div>
        <div className="drawer-body">
          <pre className="code-block">{JSON.stringify(SAMPLE_RECORD, null, 2)}</pre>
        </div>
        <div className="drawer-foot">
          <button type="button" className="btn btn-primary" onClick={downloadSchema}>Download schema</button>
          <span className="drawer-note">Illustrative sample — real records ship schema-validated with full provenance.</span>
        </div>
      </div>
    </>
  );
}

// Names carry the credibility — an ML lead already knows each one. Set at
// display scale, ink only, no gap/supply columns (PRD final-pass §1).
const BENCH_NAMES = [
  "HealthBench",
  "MedAlign",
  "MedXpertQA-R",
  "HealthBench Hard",
  "AgentClinic",
  "MedHELM",
  "MedAgentsBench",
];

export function DataPage(_props: { actions: ShellActions }) {
  const [drawerOpen, setDrawerOpen] = useState(false);

  return (
    <div className="route">
      <section className="section">
        <p className="crumb chrome reveal"><span className="root">Archangel</span><span className="sep">/</span><span className="here">02 · Data buyers</span></p>
        <div className="reveal">
          <h2>Medicine is complex. <span className="quiet">Our data reduces it.</span></h2>
          <p className="lede">The cases that break frontier models — and the expert reasoning that resolves them.</p>
        </div>

        {/* ============ 02.1 — Difficult reasoning cases ============ */}
        <p className="crumb chrome reveal sub-crumb" id="02-1"><span className="root">02.1</span><span className="sep">/</span><span className="here">Reasoning cases</span></p>
        <div className="reveal">
          <h3 style={{ fontSize: "1.4rem" }}>Models get it right for the wrong reason.</h3>
          <p className="lede">Labs and narrative pull opposite ways. The wrong answer looks plausible.</p>
          <div className="chip-row" aria-label="Modalities">
            <span className="chip">Labs</span>
            <span className="chip">Vitals</span>
            <span className="chip">Notes</span>
            <span className="chip">Imaging</span>
            <span className="chip">Outcome</span>
          </div>
        </div>

        {/* the money animation — trace draws on scroll, divergence node pulses once */}
        <div className="pillar-trace trace-scroll" aria-hidden="true">
          <svg viewBox="0 0 1440 190" preserveAspectRatio="none">
            <path className="trace trace-shared" pathLength={1} d="M0 95 H300 l14 -20 14 34 14 -14 H520" />
            <path className="trace trace-green" pathLength={1} d="M520 95 C 660 95, 720 44, 880 38 H1440" />
            <path className="trace trace-green trace-green2" pathLength={1} d="M520 95 C 660 95, 720 70, 880 66 H1440" />
            <path className="trace-orange" d="M520 95 C 660 95, 720 142, 880 150" />
            <circle className="trace-node" cx="520" cy="95" r="5" />
          </svg>
        </div>

        {/* ---- anatomy block: preserved verbatim, relocated from the old landing ---- */}
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

        {/* ungated sample + preserved quality report */}
        <div className="split" style={{ marginTop: "clamp(1.8rem, 4vh, 2.6rem)" }}>
          <div className="reveal">
            <p className="lede-strong">See the format before you talk to anyone.</p>
            <div className="sample-link">
              <button type="button" className="btn btn-primary" onClick={() => setDrawerOpen(true)}>
                View a sample record →
              </button>
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

        {/* ============ 02.2 — Difficult clinical environments ============ */}
        <p className="crumb chrome reveal sub-crumb" id="02-2"><span className="root">02.2</span><span className="sep">/</span><span className="here">Clinical environments</span></p>
        <div className="reveal">
          <h3 style={{ fontSize: "1.4rem" }}>Being a doctor isn’t producing the answer.</h3>
          <p className="lede">It’s navigating everything around it — over time, through other people.</p>
        </div>

        <div className="r-cards">
          <div className="derive reveal">
            <span className="chrome chrome-box"><span className="dot dot-green" />Longitudinal</span>
            <h3>The case doesn’t end at the decision.</h3>
            <p>Outcome linked, so the reward is real.</p>
          </div>
          <div className="derive reveal">
            <span className="chrome chrome-box"><span className="dot dot-pink" />Multi-stakeholder</span>
            <h3>Medicine is a negotiation.</h3>
            <p>Payers, referrals, follow-up, adherence.</p>
          </div>
          <div className="derive reveal">
            <span className="chrome chrome-box"><span className="dot dot-orange" />Sequential</span>
            <h3>Information has to be earned.</h3>
            <p>The agent decides what to ask and order.</p>
          </div>
        </div>

        <div className="env-statement reveal">
          <p className="big">A clinical RL environment — a gym for medical agents.</p>
          <p className="sub">Verifiable rewards from real outcomes and physician-ratified rubrics.</p>
        </div>

        <EnvDiagram />

        {/* ============ 02.3 — Benchmarks ============ */}
        <p className="crumb chrome reveal sub-crumb" id="02-3"><span className="root">02.3</span><span className="sep">/</span><span className="here">Benchmarks</span></p>
        <div className="reveal">
          <h3 style={{ fontSize: "1.4rem" }}>Climb the benchmarks. Then build the next ones.</h3>
        </div>

        {/* Block A — the benchmark grid, names only. A masthead, not a table. */}
        <div className="bench-climb">
          <span className="chrome reveal">We help labs climb</span>
          <div className="bench-grid">
            {BENCH_NAMES.map((name, i) => (
              <span className="bench-name reveal" key={name} style={{ transitionDelay: `${i * 40}ms` }}>{name}</span>
            ))}
          </div>
          <p className="bench-suite reveal">
            <span className="chrome">Agentic-EHR suite — MedAgentBench · EHR-Complex · FHIR-AgentBench · EHRAgent</span>
          </p>
        </div>

        {/* Block B — rubrics and evals attached, beside the eval pack. */}
        <div className="split bench-rubrics">
          <div className="reveal">
            <p className="lede-strong">Rubrics and evals attached.</p>
            <p className="lede bench-claim-sub">Every dataset ships with the rubric and eval that prove the climb.</p>
          </div>
          <div className="c-card eval-pack reveal">
            <span className="chrome chrome-box"><span className="dot dot-lime" />Eval pack</span>
            <p>Rubric records + grader + score.py + validity report. Re-licensed per model release.</p>
          </div>
        </div>

        {/* Block C — building the next ones. */}
        <div className="bench-next reveal">
          <p className="lede-strong">Benchmarks saturate. We author the next ones.</p>
          <p className="lede bench-claim-sub">Physician-grounded evaluations of clinical reasoning and clinical application.</p>
        </div>

        {/* ============ 02.4 — Physical AI ============ */}
        <p className="crumb chrome reveal sub-crumb" id="02-4"><span className="root">02.4</span><span className="sep">/</span><span className="here">Physical AI</span></p>
        <div className="reveal">
          <h3 style={{ fontSize: "1.4rem" }}>Surgical data isn’t in any EHR.</h3>
          <p className="lede">We’re building hardware to capture it — training data for robotic surgery and physical AI medical products.</p>
          <p style={{ marginTop: "1rem" }}>
            <span className="chrome chrome-box"><span className="dot dot-faint" />In development</span>
          </p>
          <p className="chrome bench-suite" style={{ marginTop: "1.1rem" }}>Tactile · Force · Motion · Sensing</p>
        </div>

        <div className="c-card wave-card reveal" aria-hidden="true">
          <span className="label">Instrument contact — the signal no chart records.</span>
          <svg viewBox="0 0 900 160" preserveAspectRatio="none" style={{ marginTop: "0.9rem" }}>
            <line className="wave-base" x1="0" y1="80" x2="900" y2="80" />
            <path
              className="wave-path"
              pathLength={1}
              d="M0 80 H120 C 150 80, 165 40, 190 46 C 215 52, 222 96, 250 92 C 278 88, 285 30, 320 34 C 355 38, 360 104, 400 98 C 440 92, 450 56, 490 60 C 530 64, 540 88, 580 84 C 620 80, 640 24, 680 30 C 720 36, 730 92, 770 88 C 810 84, 830 72, 900 76"
            />
            <circle className="wave-pulse" cx="320" cy="34" r="6" />
          </svg>
        </div>
      </section>

      {drawerOpen && <SampleDrawer onClose={() => setDrawerOpen(false)} />}
    </div>
  );
}
