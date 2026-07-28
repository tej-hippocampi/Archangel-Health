/**
 * `/research` — first published results (was "coming soon", PRD §4). One gold
 * run of the clinical-reasoning benchmark: leaderboard, what broke, and the
 * methodology behind the score. Case content (prompts/criteria/responses)
 * stays private to avoid contaminating the models under test; only
 * aggregate scores and metadata are shown. One CTA at the end: keep this
 * page to "leaderboard + Get the full report," nothing more (user flagged
 * the prior 3-door grid + notify-form as button clutter).
 */

import type { ShellActions } from "../ArchShell";

const RANKS = [
  { rank: "01", name: "Grok 4.5", ci: "95% CI 59.6–98.2", score: 90, meta: ["Reproduced trap in 1 of 10", "1 lucky guess"] },
  { rank: "02", name: "GPT-5.5", ci: "95% CI 49.0–94.3", score: 80, meta: ["Reproduced trap in 2 of 10", "2 lucky guesses"] },
  { rank: "03", name: "Gemini 3.1 Pro", ci: "95% CI 49.0–94.3", score: 80, meta: ["Reproduced trap in 2 of 10", "1 lucky guess"] },
  { rank: "04", name: "Claude Opus 4.8", ci: "95% CI 39.7–89.2", score: 70, meta: ["Reproduced trap in 3 of 10", "Judge caveat: see below"], flagged: true },
];

const FINDINGS = [
  {
    tag: "Convergent failure",
    title: "Three labs, the same wrong diagnosis.",
    line: "On a hyponatremia case built to be under-worked-up, Claude, Gemini, and GPT-5.5 (three models from three separate labs) all jumped to SIADH and fluid restriction before the case gave enough data to justify it. Grok was the only model that held back and asked for the missing workup.",
  },
  {
    tag: "Lab-artifact trap",
    title: "A false emergency, treated as real.",
    line: "The potassium of 6.8 came from a hemolyzed sample. Claude and GPT-5.5 reached for emergency treatment (IV calcium) instead of recognizing the artifact and repeating the draw first.",
  },
  {
    tag: "Judge caveat",
    title: "One failure looks like our mistake, not the model's.",
    line: "On the HRS-AKI case, Claude's answer stated exactly what the criterion asked for. The judge flagged it failed anyway. Left uncorrected pending physician confirmation; if confirmed, Claude's score moves 70 → 80.",
  },
];

export function ResearchPage({ actions }: { actions: ShellActions }) {
  return (
    <div className="route">
      <section className="section">
        <p className="crumb chrome reveal"><span className="root">Archangel</span><span className="sep">/</span><span className="here">01 · Research</span></p>
        <div className="reveal">
          <h2>Frontier models pass the easy benchmarks. <span className="quiet">Physician-authored hard cases still break them.</span></h2>
          <p className="lede">
            We ran four frontier models (Grok 4.5, GPT-5.5, Gemini 3.1 Pro, and Claude Opus 4.8)
            against ten physician-vetted nephrology cases built to be hard: the labs and the
            narrative pull in different directions, and the wrong answer looks plausible.
          </p>
          <p className="lede-strong">Every model missed a trap somewhere. Even the top-ranked one.</p>
        </div>

        <div className="aura aura-expert stat-card reveal">
          <span className="doto">5<span className="of">/10</span></span>
          <p>gold cases exposed a failure in at least one model, after all four models scored a
          perfect 100 on generic textbook traps.</p>
        </div>
        <p className="label reveal" style={{ marginTop: "1rem", maxWidth: "38rem" }}>
          Provisional: with ten cases, this shows a failure rate meaningfully above zero, not a
          fine-grained model ranking. Ranking models within a few points needs 100–150+ cases.
        </p>

        {/* ============ leaderboard ============ */}
        <p className="crumb chrome reveal sub-crumb" id="leaderboard"><span className="root">01.1</span><span className="sep">/</span><span className="here">Leaderboard</span></p>
        <div className="reveal">
          <h3 style={{ fontSize: "1.4rem" }}>One gold run, ranked by trap avoidance.</h3>
          <p className="lede">Trap-avoidance score = 100 × (1 − reproduction rate): how often a
          model reproduced the exact failure a case was built to trigger.</p>
        </div>
        <div className="board reveal">
          {RANKS.map((r) => (
            <div className="rank-row" key={r.name}>
              <span className="rank-num chrome">{r.rank}</span>
              <div className="rank-model">
                <span className="rank-name">{r.name}</span>
                <span className="rank-ci label">{r.ci}</span>
              </div>
              <span className="rank-score doto">{r.score}</span>
              <div className="rank-meta">
                {r.meta.map((m, i) => (
                  <span className={`chip${r.flagged && i === r.meta.length - 1 ? " chip-lime" : ""}`} key={m}>{m}</span>
                ))}
              </div>
            </div>
          ))}
        </div>
        <p className="label reveal" style={{ marginTop: "1.2rem", maxWidth: "40rem" }}>
          Confidence intervals overlap across all four models. This run supports "the failure
          rate is meaningfully above zero," not a confident order.
        </p>

        {/* ============ what broke ============ */}
        <p className="crumb chrome reveal sub-crumb" id="findings"><span className="root">01.2</span><span className="sep">/</span><span className="here">What broke</span></p>
        <div className="reveal">
          <h3 style={{ fontSize: "1.4rem" }}>Three findings, from ten cases.</h3>
        </div>
        <div className="r-cards">
          {FINDINGS.map((f) => (
            <div className="derive reveal" key={f.tag}>
              <span className="chrome chrome-box"><span className="dot dot-faint" />{f.tag}</span>
              <h3>{f.title}</h3>
              <p>{f.line}</p>
            </div>
          ))}
        </div>

        {/* ============ methodology ============ */}
        <p className="crumb chrome reveal sub-crumb" id="methodology"><span className="root">01.3</span><span className="sep">/</span><span className="here">Methodology</span></p>
        <div className="split">
          <div className="reveal">
            <h3 style={{ fontSize: "1.4rem" }}>Graded by a calibrated judge, <span className="quiet">not a gut check.</span></h3>
            <p style={{ marginTop: "1.1rem" }}>Claude Opus 4.8 grades each answer against
            physician-written criteria. Before it grades anything real, it's checked against a
            physician-labeled reference set, and blocked from grading if it doesn't clear the bar.</p>
            <div className="aura aura-model stat-card">
              <span className="doto">100<span className="of">%</span></span>
              <p>judge agreement with physician ground truth on 26 reference pairs (95% CI
              87.1–100%), above our 85% calibration threshold.</p>
            </div>
          </div>
          <div className="qc reveal">
            <p className="qc-title chrome">What's public, what stays private</p>
            <ul className="qc-list">
              <li><span className="dot dot-green" />Scores, rankings, and methodology are public</li>
              <li><span className="dot dot-green" />All 10 gold cases are nephrology, physician-vetted, high-severity by design</li>
              <li><span className="dot dot-pink" />Case prompts, criteria text, and model responses stay private</li>
              <li><span className="dot dot-green" />Held private specifically to prevent contaminating the models under test</li>
              <li><span className="dot dot-orange" />Physician ratification of the calibration labels: in progress</li>
            </ul>
          </div>
        </div>

        {/* ============ single CTA ============ */}
        <div className="route-cta reveal">
          <button type="button" className="btn btn-primary" onClick={() => actions.openLead("request_data")}>
            Get the full report
          </button>
          <p className="cta-note">Methodology, case-level pass/fail breakdowns, and the training data behind every failure.</p>
        </div>
      </section>
    </div>
  );
}
