import { motion } from "motion/react";
import { AuthProvider } from "@/contexts/AuthContext";
import RecoveryResourcesEmailPreview from "@/app/components/RecoveryResourcesEmailPreview";
import TeamCalculator from "@/app/components/TeamCalculator";
import TeamWhitepaperPage from "@/app/components/TeamWhitepaperPage";
import PodcastAndBlogsPage from "@/app/components/PodcastAndBlogsPage";
import { SiteHeader, parseLandingView } from "@/app/components/SiteHeader";
import OnboardingWizard from "@/app/components/OnboardingWizard";
import TenantSignIn from "@/app/components/TenantSignIn";

const HIPPOCRATES_BG = "/hippocrates-email-bg.png";

const MAIL = "aryaabhatia@berkeley.edu";
const mailto = (subject: string) => `mailto:${MAIL}?subject=${encodeURIComponent(subject)}`;

const reveal = {
  initial: { opacity: 0, y: 30 },
  whileInView: { opacity: 1, y: 0 },
  viewport: { once: true, margin: "-100px" },
} as const;

const ease = [0.16, 1, 0.3, 1] as const;

function SectionMarker({ children }: { children: string }) {
  return <div className="section-marker">{children}</div>;
}

function CheckIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#00ffff" strokeWidth="3" aria-hidden="true">
      <path d="M20 6L9 17l-5-5" />
    </svg>
  );
}

interface SupervisionCardProps {
  tag: string;
  title: string;
  body: string;
  delay?: number;
}

function SupervisionCard({ tag, title, body, delay = 0 }: SupervisionCardProps) {
  return (
    <motion.div {...reveal} transition={{ duration: 0.8, delay, ease }} className="sup-card">
      <div className="driver-card-frame-outer" />
      <div className="sup-card-content">
        <span className="mono-tag">{tag}</span>
        <h3 className="sup-card-title">{title}</h3>
        <p className="sup-card-body">{body}</p>
      </div>
    </motion.div>
  );
}

interface PlanCardProps {
  who: string;
  title: string;
  body: string;
  href: string;
  delay?: number;
}

function PlanCard({ who, title, body, href, delay = 0 }: PlanCardProps) {
  return (
    <motion.a {...reveal} transition={{ duration: 0.8, delay, ease }} className="plan-card" href={href}>
      <div className="driver-card-frame-outer">
        <div className="driver-card-frame-inner" />
      </div>
      <div className="driver-card-aura" />
      <img src={HIPPOCRATES_BG} alt="" className="driver-card-bg" />
      <div className="plan-card-content">
        <span className="plan-card-who">{who}</span>
        <h3 className="plan-card-title">{title}</h3>
        <p className="plan-card-body">{body}</p>
        <span className="plan-card-go" aria-hidden="true">→</span>
      </div>
    </motion.a>
  );
}

function CaseRecordCard() {
  return (
    <motion.div {...reveal} transition={{ duration: 0.8, ease }} className="case-card">
      <div className="driver-card-frame-outer">
        <div className="driver-card-frame-inner" />
      </div>
      <div className="case-card-inner">
        <div className="case-card-head">
          <span>Case record</span>
          <span>de-identified</span>
        </div>
        <div className="case-redact" aria-hidden="true">
          <i /><i /><i /><i />
        </div>
        <div className="case-redact-label">PT NAME · MRN · DOB</div>
        <table className="case-table">
          <thead>
            <tr><th>Lab</th><th>Value</th><th>Ref</th><th>Flag</th></tr>
          </thead>
          <tbody>
            <tr><td>Creatinine</td><td>3.1</td><td>0.7–1.3</td><td className="flag-hi">H ▲</td></tr>
            <tr><td>Potassium</td><td>5.9</td><td>3.5–5.0</td><td className="flag-hi">H ▲</td></tr>
            <tr><td>Bicarbonate</td><td>16</td><td>22–29</td><td className="flag-lo">L ▼</td></tr>
            <tr><td>Hemoglobin</td><td>9.4</td><td>13.5–17.5</td><td className="flag-lo">L ▼</td></tr>
            <tr><td>Urine Na⁺</td><td>12</td><td>—</td><td></td></tr>
          </tbody>
        </table>
        <div className="case-hpi">
          <h4>HPI</h4>
          <p>
            Progressive fatigue ×3 wk. Recently started <span className="case-rx">▮▮▮▮▮▮</span> for
            joint pain. Exam: trace edema, BP 168/94. History pulls toward one diagnosis; the labs
            argue for another.
          </p>
        </div>
        <div className="case-card-foot">Reviewed — board-certified specialist ✓</div>
      </div>
    </motion.div>
  );
}

function DivergencePanel() {
  return (
    <motion.div
      initial={{ opacity: 0, y: 24 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 1, delay: 1.1, ease }}
      className="diverge-panel"
    >
      <div className="driver-card-frame-outer">
        <div className="driver-card-frame-inner" />
      </div>
      <div className="diverge-inner">
        <div className="diverge-legend">
          <span><i className="diverge-dot diverge-dot-expert" />expert reasoning</span>
          <span><i className="diverge-dot diverge-dot-model" />model reasoning</span>
          <span><i className="diverge-dot diverge-dot-gap" />our data lives where they diverge</span>
        </div>
        <svg className="diverge-svg" viewBox="0 0 900 88" preserveAspectRatio="none" aria-hidden="true">
          <defs>
            <linearGradient id="gapFill" x1="0" y1="0" x2="1" y2="0">
              <stop offset="0%" stopColor="#00ffff" stopOpacity="0.02" />
              <stop offset="100%" stopColor="#00ffff" stopOpacity="0.12" />
            </linearGradient>
          </defs>
          <path
            d="M300,38 C420,35 520,26 640,18 C740,12 820,10 900,9 L900,82 C820,81 740,79 640,74 C520,68 420,58 300,52 Z"
            fill="url(#gapFill)"
          />
          <path
            d="M0,44 C150,44 210,40 300,38 C420,35 520,26 640,18 C740,12 820,10 900,9"
            fill="none" stroke="#00ffff" strokeWidth="1.6"
          />
          <path
            d="M0,44 C150,44 210,48 300,52 C420,58 520,68 640,74 C740,79 820,81 900,82"
            fill="none" stroke="#6a6a70" strokeWidth="1.6" strokeDasharray="4 4"
          />
          <circle cx="300" cy="45" r="3" fill="#00ffff" />
        </svg>
        <div className="diverge-caption">One case. Two credentialed opinions. One model failure. All captured.</div>
      </div>
    </motion.div>
  );
}

function LandingContent() {
  return (
    <>
      <div className="app-container">
        <section className="hero-section" id="top">
          <img
            src="https://static.scientificamerican.com/dam/m/37bca03526cc32df/original/AI-pill-gif-healthspans.gif?m=1741035098.088&w=1200"
            alt=""
            className="hero-bg-gif"
            aria-hidden="true"
          />
          <div className="hero-overlay-radial" />
          <div className="hero-overlay-gradient" />

          <div className="hero-content">
            <div className="hero-inner">
              <motion.div
                initial={{ opacity: 0, y: 16 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 1, delay: 0.2, ease }}
                className="hero-eyebrow"
              >
                Clinical reasoning data · Human judgment at the frontier
              </motion.div>

              <motion.h1
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 1.2, delay: 0.4, ease }}
                className="hero-headline"
              >
                The cases frontier models fail.
                <br />
                <span className="hero-headline-accent">The reasoning that resolves them.</span>
              </motion.h1>

              <motion.p
                initial={{ opacity: 0, y: 14 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 1, delay: 0.65, ease }}
                className="hero-lede"
              >
                Archangel Health captures expert clinical reasoning over real, de-identified cases hard
                enough to split board-certified specialists — delivered as preference pairs, ideal
                answers, and step-level reasoning traces, ready for training and evals.
              </motion.p>

              <motion.div
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 1, delay: 0.85, ease }}
                className="hero-cta-bottom"
              >
                <a href={mailto("Data request — Archangel Health")} className="hero-demo-button">
                  <img src={HIPPOCRATES_BG} alt="" className="hippocrates-bg-button" />
                  <span className="hero-demo-button-text">Request data</span>
                </a>
                <a href={mailto("Becoming a contributor — Archangel Health")} className="hero-demo-button hero-button-ghost">
                  <span className="hero-demo-button-text">Become a contributor</span>
                </a>
              </motion.div>

              <DivergencePanel />
            </div>
          </div>
        </section>

        <section className="section section-dark" id="problem">
          <div className="section-container section-narrow">
            <motion.div {...reveal} transition={{ duration: 0.8, ease }} className="section-header-center">
              <SectionMarker>01 · Presenting problem</SectionMarker>
              <h2 className="section-title">
                Frontier models pass the easy benchmarks.
                <br />
                <span className="title-accent">So we build the ones they can’t.</span>
              </h2>
            </motion.div>
            <motion.div {...reveal} transition={{ duration: 0.8, delay: 0.15, ease }} className="prose-center">
              <p>
                Our cases are hard by construction: the labs and the narrative pull in different
                directions, the right answer needs both, and the wrong answer looks plausible. On these
                presentations, frontier models fail — and board-certified specialists disagree on the
                diagnosis, the intervention, even the ground truth.
              </p>
              <p className="prose-emphasis">
                That disagreement isn’t noise. It’s the most valuable training signal in medicine —
                and we are the ones capturing it.
              </p>
            </motion.div>
          </div>
        </section>

        <section className="section section-dark" id="findings">
          <div className="section-container">
            <motion.div {...reveal} transition={{ duration: 0.8, ease }} className="section-header-center">
              <SectionMarker>02 · Findings</SectionMarker>
              <h2 className="section-title">
                One case, <span className="title-accent">four kinds of supervision.</span>
              </h2>
              <p className="section-lede">
                Every record starts as a multimodal case — structured labs plus an EHR-style note —
                and ships as finished, schema-validated training data.
              </p>
            </motion.div>

            <div className="findings-grid">
              <CaseRecordCard />
              <div className="sup-grid">
                <SupervisionCard
                  tag="RLHF · DPO"
                  title="Preference pair"
                  body="The chosen answer against a plausible hard-negative — the mistake a good model actually makes."
                  delay={0.1}
                />
                <SupervisionCard
                  tag="SFT"
                  title="Ideal answer"
                  body="The complete expert resolution of the case, written to be learned from."
                  delay={0.18}
                />
                <SupervisionCard
                  tag="PRM"
                  title="Reasoning trace"
                  body="Step-level expert reasoning with corrections — exactly where a model’s chain goes wrong, and why."
                  delay={0.26}
                />
                <SupervisionCard
                  tag="Provenance"
                  title="Full lineage"
                  body="Credential attributes, guideline citations, difficulty score, versioning. Every record answers for itself."
                  delay={0.34}
                />
              </div>
            </div>
          </div>
        </section>

        <section className="section section-dark" id="assessment">
          <div className="section-container">
            <motion.div {...reveal} transition={{ duration: 0.8, ease }} className="section-header-center">
              <SectionMarker>03 · Assessment</SectionMarker>
              <h2 className="section-title">
                We don’t claim difficulty. <span className="title-accent">We measure it.</span>
              </h2>
              <p className="section-lede">
                Every case is scored against frontier models with our own difficulty rubric, and our
                benchmarks exist to prove one thing: that this data pushes models past where public
                benchmarks stop. Data that doesn’t move the frontier doesn’t ship.
              </p>
            </motion.div>

            <div className="assessment-grid">
              <motion.div {...reveal} transition={{ duration: 0.8, delay: 0.1, ease }} className="stat-block">
                <div className="stat-number">6+/10</div>
                <p className="stat-sub">
                  of the most commonly used medical benchmarks improve when models train on our data.
                </p>
              </motion.div>

              <motion.div {...reveal} transition={{ duration: 0.8, delay: 0.2, ease }} className="quality-panel">
                <div className="driver-card-frame-outer">
                  <div className="driver-card-frame-inner" />
                </div>
                <div className="quality-panel-content">
                  <h4 className="quality-panel-head">Quality report — every record</h4>
                  <ul className="quality-list">
                    <li><CheckIcon />Difficulty scored against frontier models</li>
                    <li><CheckIcon />Contamination-checked against public benchmarks</li>
                    <li><CheckIcon />Guideline-grounded, with citations</li>
                    <li><CheckIcon />No PHI — context-preserving de-identification</li>
                    <li><CheckIcon />Watermarked &amp; traceable, licensed per end-buyer</li>
                    <li><CheckIcon />IP-cleared, contributor credentials verified</li>
                  </ul>
                </div>
              </motion.div>
            </div>
          </div>
        </section>

        <section className="section section-dark" id="consults">
          <div className="section-container">
            <motion.div {...reveal} transition={{ duration: 0.8, ease }} className="section-header-center">
              <SectionMarker>04 · Consults</SectionMarker>
              <h2 className="section-title">
                A network of specialists, <span className="title-accent">paid for their judgment.</span>
              </h2>
            </motion.div>

            <div className="consults-grid">
              <motion.div {...reveal} transition={{ duration: 0.8, delay: 0.1, ease }} className="consult-col">
                <span className="plan-card-who">For physicians</span>
                <h3 className="consult-title">Your reasoning is the product.</h3>
                <p className="consult-body">
                  Specialists work through hard cases on our platform — annotating the reasoning,
                  ratifying the ground truth, flagging where models go wrong — and get paid for the
                  judgment only they can supply.
                </p>
              </motion.div>
              <motion.div {...reveal} transition={{ duration: 0.8, delay: 0.2, ease }} className="consult-col">
                <span className="plan-card-who">For labs &amp; health-AI teams</span>
                <h3 className="consult-title">Datasets, spun up on demand.</h3>
                <p className="consult-body">
                  Specialty, modality, format, difficulty — scoped to your model and your gap. Next:
                  longitudinal cases that follow the patient past the decision, linking case,
                  intervention, and real outcome.
                </p>
              </motion.div>
            </div>

            <motion.blockquote {...reveal} transition={{ duration: 0.9, delay: 0.15, ease }} className="pull-quote">
              Doctors earn from their judgment.
              <br />
              Models learn from it.
              <br />
              <span className="pull-quote-accent">The hardest cases become the most valuable data.</span>
            </motion.blockquote>
          </div>
        </section>

        <section className="section section-dark" id="plan">
          <div className="section-container">
            <motion.div {...reveal} transition={{ duration: 0.8, ease }} className="section-header-center">
              <SectionMarker>05 · Plan</SectionMarker>
              <h2 className="section-title">Three ways in.</h2>
            </motion.div>

            <div className="plan-grid">
              <PlanCard
                who="Physicians"
                title="Become a contributor"
                body="Reason through hard cases. Get paid for your judgment."
                href={mailto("Becoming a contributor — Archangel Health")}
                delay={0.1}
              />
              <PlanCard
                who="Labs & health-AI teams"
                title="Request data"
                body="Scoped samples, fitted pilots, bespoke datasets."
                href={mailto("Data request — Archangel Health")}
                delay={0.2}
              />
              <PlanCard
                who="Health systems, practices & software companies"
                title="Provide your data"
                body="We buy de-identified clinical data — from care organizations and the software applications that hold it."
                href={mailto("Providing de-identified data — Archangel Health")}
                delay={0.3}
              />
            </div>

            <motion.p {...reveal} transition={{ duration: 0.8, delay: 0.35, ease }} className="plan-else">
              Something else in mind?{" "}
              <a href={mailto("Partnership — Archangel Health")}>Other partnerships &amp; collaborations →</a>
            </motion.p>
          </div>
        </section>

        <footer className="footer">
          <div className="footer-container">
            <div className="footer-content">
              <div className="footer-brand">
                <div className="footer-logo">
                  <span className="footer-logo-text">ARCHANGEL HEALTH</span>
                </div>
                <p className="footer-tagline">
                  Expert clinical reasoning over real, de-identified cases — preference pairs, ideal
                  answers, and step-level reasoning traces for training and evals.
                </p>
                <p className="footer-location">Berkeley, California</p>
              </div>
              <div className="footer-contact">
                <a href={`mailto:${MAIL}`} className="footer-link">
                  {MAIL}
                </a>
                <p className="footer-promise">
                  Real. De-identified. IP-cleared.
                  <br />
                  Never resold beyond license.
                </p>
                <p className="footer-copyright">© 2026 Archangel Health. All rights reserved.</p>
              </div>
            </div>
          </div>
        </footer>

        <style>{styles}</style>
      </div>
    </>
  );
}

const styles = `
  * {
    box-sizing: border-box;
  }

  body {
    margin: 0;
    padding: 0;
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    background: #0a0a0b;
    color: #f5f5f7;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
  }

  .app-container {
    width: 100%;
    overflow-x: hidden;
    background: #0a0a0b;
    position: relative;
  }

  .auth-btn {
    cursor: pointer;
    outline: none;
  }

  .auth-btn:focus-visible {
    box-shadow: 0 0 0 2px rgba(0, 255, 255, 0.5);
  }

  .auth-btn-primary:hover {
    box-shadow: 0 0 24px rgba(0, 255, 255, 0.35);
  }

  /* ── Hero ── */

  .hero-section {
    position: relative;
    min-height: 100vh;
    width: 100%;
    overflow: hidden;
    background: #0a0a0b;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 5rem 0 4rem;
  }

  .hero-bg-gif {
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    object-fit: cover;
    z-index: 0;
    pointer-events: none;
    opacity: 0.7;
  }

  .hero-overlay-radial {
    position: absolute;
    inset: 0;
    z-index: 1;
    background: radial-gradient(circle, transparent, rgba(10, 10, 11, 0.3), rgba(10, 10, 11, 0.9));
    pointer-events: none;
  }

  .hero-overlay-gradient {
    position: absolute;
    inset: 0;
    z-index: 1;
    background: linear-gradient(180deg, rgba(10, 10, 11, 0.5), transparent, rgba(10, 10, 11, 0.85));
    pointer-events: none;
  }

  .hero-content {
    position: relative;
    z-index: 20;
    width: 100%;
    padding: 0 1.5rem;
  }

  .hero-inner {
    max-width: 1200px;
    margin: 0 auto;
    text-align: center;
    padding: 2rem 0;
  }

  .hero-eyebrow {
    font-family: 'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, monospace;
    font-size: 0.75rem;
    font-weight: 500;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: #00ffff;
    text-shadow: 0 0 18px rgba(0, 255, 255, 0.35);
    margin-bottom: 1.75rem;
  }

  .hero-headline {
    font-size: clamp(2.5rem, 6vw, 4.5rem);
    font-weight: 500;
    line-height: 1.12;
    letter-spacing: -0.03em;
    color: #f5f5f7;
    margin: 0 auto 1.75rem;
    max-width: 1100px;
    text-shadow: 0 0 40px rgba(0, 255, 255, 0.08);
  }

  .hero-headline-accent {
    background: linear-gradient(100deg, #f5f5f7 20%, #67e8f9 60%, #00ffff 90%);
    -webkit-background-clip: text;
    background-clip: text;
    -webkit-text-fill-color: transparent;
  }

  .hero-lede {
    font-size: clamp(1rem, 1.6vw, 1.1875rem);
    line-height: 1.75;
    color: rgba(245, 245, 247, 0.78);
    max-width: 760px;
    margin: 0 auto 2.5rem;
    text-shadow: 0 1px 4px rgba(0, 0, 0, 0.6);
  }

  .hero-cta-bottom {
    margin-top: 0;
    display: flex;
    justify-content: center;
    gap: 1rem;
    flex-wrap: wrap;
  }

  .hero-demo-button {
    position: relative;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 0.875rem 2.5rem;
    background: rgba(26, 26, 29, 0.85);
    border: 1.5px solid #ffffff;
    border-radius: 9999px;
    font-size: 1rem;
    font-weight: 500;
    letter-spacing: -0.01em;
    color: #f5f5f7;
    text-decoration: none;
    overflow: hidden;
    transition: all 0.3s cubic-bezier(0.16, 1, 0.3, 1);
    box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.1), 0 4px 16px rgba(0, 0, 0, 0.4);
  }

  .hippocrates-bg-button {
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    object-fit: cover;
    z-index: 0;
    opacity: 0.45;
    transition: opacity 0.3s cubic-bezier(0.16, 1, 0.3, 1);
    filter: grayscale(1) sepia(0.5) brightness(0.72) contrast(0.92);
  }

  .hero-demo-button-text {
    position: relative;
    z-index: 10;
    text-shadow: 0 1px 4px rgba(0, 0, 0, 0.7), 0 0 8px rgba(0, 0, 0, 0.5);
  }

  .hero-demo-button:hover {
    background: rgba(26, 26, 29, 0.9);
    transform: translateY(-2px);
    box-shadow: 0 0 0 1px #ffffff, 0 0 20px rgba(255, 255, 255, 0.2), 0 8px 24px rgba(0, 0, 0, 0.5);
    border-color: #ffffff;
  }

  .hero-demo-button:hover .hippocrates-bg-button {
    opacity: 0.65;
  }

  .hero-button-ghost {
    border-color: rgba(255, 255, 255, 0.35);
    background: rgba(10, 10, 11, 0.55);
  }

  .hero-button-ghost:hover {
    border-color: rgba(0, 255, 255, 0.7);
    box-shadow: 0 0 0 1px rgba(0, 255, 255, 0.35), 0 0 24px rgba(0, 255, 255, 0.18), 0 8px 24px rgba(0, 0, 0, 0.5);
  }

  /* ── Divergence panel ── */

  .diverge-panel {
    position: relative;
    margin: 4rem auto 0;
    max-width: 900px;
    border-radius: 1.25rem;
    overflow: hidden;
    border: 1px solid rgba(0, 255, 255, 0.08);
    background: linear-gradient(135deg, rgba(22, 22, 25, 0.92) 0%, rgba(10, 10, 11, 0.96) 100%);
    backdrop-filter: blur(10px);
    text-align: left;
  }

  .diverge-inner {
    position: relative;
    z-index: 10;
    padding: 1.75rem 2rem 1.5rem;
  }

  .diverge-legend {
    display: flex;
    gap: 1.5rem;
    flex-wrap: wrap;
    font-family: 'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, monospace;
    font-size: 0.6875rem;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: rgba(245, 245, 247, 0.55);
    margin-bottom: 1.25rem;
  }

  .diverge-legend span {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .diverge-dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
  }

  .diverge-dot-expert {
    background: #00ffff;
    box-shadow: 0 0 10px rgba(0, 255, 255, 0.6);
  }

  .diverge-dot-model {
    background: #6a6a70;
  }

  .diverge-dot-gap {
    background: rgba(0, 255, 255, 0.12);
    border: 1px solid rgba(0, 255, 255, 0.55);
  }

  .diverge-svg {
    width: 100%;
    height: 88px;
    display: block;
  }

  .diverge-caption {
    margin-top: 1.1rem;
    font-family: 'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, monospace;
    font-size: 0.75rem;
    letter-spacing: 0.05em;
    color: rgba(245, 245, 247, 0.5);
  }

  /* ── Sections ── */

  .section {
    position: relative;
    padding: 5rem 1.5rem;
    width: 100%;
  }

  .section-dark {
    background: linear-gradient(180deg, #0a0a0b 0%, #1a1a1d 50%, #0a0a0b 100%);
  }

  .section-container {
    max-width: 1400px;
    margin: 0 auto;
  }

  .section-narrow {
    max-width: 980px;
  }

  .section-header-center {
    text-align: center;
    margin-bottom: 3rem;
  }

  .section-marker {
    font-family: 'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, monospace;
    font-size: 0.6875rem;
    font-weight: 500;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: rgba(0, 255, 255, 0.75);
    margin-bottom: 1.25rem;
  }

  .section-title {
    font-size: clamp(1.8rem, 4vw, 2.75rem);
    font-weight: 500;
    letter-spacing: -0.03em;
    color: #f5f5f7;
    margin: 0;
    max-width: 1000px;
    margin-left: auto;
    margin-right: auto;
    line-height: 1.2;
  }

  .title-accent {
    background: linear-gradient(100deg, #f5f5f7 10%, #67e8f9 55%, #00ffff 95%);
    -webkit-background-clip: text;
    background-clip: text;
    -webkit-text-fill-color: transparent;
  }

  .section-lede {
    font-size: clamp(1rem, 1.5vw, 1.125rem);
    line-height: 1.75;
    color: rgba(245, 245, 247, 0.7);
    max-width: 720px;
    margin: 1.5rem auto 0;
  }

  .prose-center {
    max-width: 720px;
    margin: 0 auto;
    text-align: center;
  }

  .prose-center p {
    font-size: clamp(1.0625rem, 1.6vw, 1.1875rem);
    line-height: 1.8;
    color: rgba(245, 245, 247, 0.78);
    margin: 0 0 1.5rem;
  }

  .prose-emphasis {
    color: #f5f5f7 !important;
    font-weight: 500;
    padding: 1.5rem 1.75rem;
    border: 1px solid rgba(103, 232, 249, 0.14);
    border-radius: 0.75rem;
    background: linear-gradient(135deg, rgba(103, 232, 249, 0.06) 0%, rgba(45, 212, 191, 0.03) 100%);
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.2), inset 0 0 20px rgba(45, 212, 191, 0.03);
  }

  /* ── Findings: case record + supervision cards ── */

  .findings-grid {
    display: grid;
    grid-template-columns: minmax(340px, 480px) 1fr;
    gap: 2rem;
    align-items: start;
    justify-content: center;
  }

  .case-card {
    position: relative;
    border-radius: 1.25rem;
    overflow: hidden;
    border: 1px solid rgba(0, 255, 255, 0.08);
    background: linear-gradient(135deg, rgba(22, 22, 25, 0.96) 0%, rgba(10, 10, 11, 0.98) 100%);
  }

  .case-card-inner {
    position: relative;
    z-index: 10;
  }

  .case-card-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.85rem 1.25rem;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    font-family: 'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, monospace;
    font-size: 0.6875rem;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: rgba(245, 245, 247, 0.5);
  }

  .case-redact {
    display: flex;
    gap: 6px;
    padding: 1rem 1.25rem 0.3rem;
  }

  .case-redact i {
    height: 11px;
    border-radius: 2px;
    background: repeating-linear-gradient(45deg, #212127, #212127 4px, #17171b 4px, #17171b 8px);
    flex: 1;
  }

  .case-redact-label {
    padding: 0 1.25rem 0.8rem;
    font-family: 'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, monospace;
    font-size: 0.625rem;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: rgba(245, 245, 247, 0.32);
  }

  .case-table {
    width: 100%;
    border-collapse: collapse;
    font-family: 'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, monospace;
    font-size: 0.78rem;
  }

  .case-table th {
    text-align: left;
    padding: 0.6rem 1.25rem;
    color: rgba(245, 245, 247, 0.45);
    font-weight: 400;
    font-size: 0.625rem;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    border-top: 1px solid rgba(255, 255, 255, 0.08);
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  }

  .case-table td {
    padding: 0.6rem 1.25rem;
    border-bottom: 1px solid rgba(255, 255, 255, 0.05);
    color: rgba(245, 245, 247, 0.7);
  }

  .case-table td:first-child {
    color: #f5f5f7;
  }

  .flag-hi {
    color: #ff453a;
    font-weight: 600;
  }

  .flag-lo {
    color: #ff9f0a;
    font-weight: 600;
  }

  .case-hpi {
    padding: 1.1rem 1.25rem;
  }

  .case-hpi h4 {
    font-family: 'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, monospace;
    font-size: 0.625rem;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: rgba(245, 245, 247, 0.45);
    margin: 0 0 0.6rem;
    font-weight: 400;
  }

  .case-hpi p {
    font-size: 0.875rem;
    color: rgba(245, 245, 247, 0.75);
    line-height: 1.7;
    margin: 0;
  }

  .case-rx {
    background: #212127;
    border-radius: 3px;
    padding: 1px 7px;
    color: rgba(245, 245, 247, 0.4);
    font-family: 'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, monospace;
    font-size: 0.72rem;
  }

  .case-card-foot {
    padding: 0.85rem 1.25rem;
    border-top: 1px solid rgba(255, 255, 255, 0.08);
    font-family: 'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, monospace;
    font-size: 0.72rem;
    color: #00ffff;
    letter-spacing: 0.04em;
    text-shadow: 0 0 14px rgba(0, 255, 255, 0.3);
  }

  .sup-grid {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 1.25rem;
  }

  .sup-card {
    position: relative;
    border-radius: 1.25rem;
    overflow: hidden;
    border: 1px solid rgba(0, 255, 255, 0.08);
    background: linear-gradient(135deg, rgba(22, 22, 25, 0.96) 0%, rgba(10, 10, 11, 0.98) 100%);
    transition: transform 0.4s cubic-bezier(0.16, 1, 0.3, 1), box-shadow 0.4s cubic-bezier(0.16, 1, 0.3, 1);
  }

  .sup-card:hover {
    transform: translateY(-4px);
    box-shadow: 0 16px 40px rgba(0, 0, 0, 0.5), 0 0 0 1px rgba(103, 232, 249, 0.1), 0 0 20px rgba(45, 212, 191, 0.08);
  }

  .sup-card-content {
    position: relative;
    z-index: 10;
    padding: 1.5rem 1.5rem 1.4rem;
    display: flex;
    flex-direction: column;
  }

  .mono-tag {
    display: inline-block;
    align-self: flex-start;
    font-family: 'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, monospace;
    font-size: 0.625rem;
    font-weight: 500;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: #00ffff;
    border: 1px solid rgba(0, 255, 255, 0.35);
    border-radius: 4px;
    padding: 3px 8px;
    margin-bottom: 0.9rem;
  }

  .sup-card-title {
    font-size: 1.0625rem;
    font-weight: 600;
    letter-spacing: -0.015em;
    color: #ffffff;
    margin: 0 0 0.5rem;
    line-height: 1.3;
  }

  .sup-card-body {
    font-size: 0.875rem;
    line-height: 1.65;
    color: rgba(245, 245, 247, 0.72);
    margin: 0;
  }

  /* ── Assessment: stat + quality report ── */

  .assessment-grid {
    display: grid;
    grid-template-columns: 1fr 1.1fr;
    gap: 3rem;
    align-items: center;
    max-width: 1100px;
    margin: 0 auto;
  }

  .stat-block {
    text-align: center;
  }

  .stat-number {
    font-size: clamp(3.5rem, 8vw, 5.75rem);
    font-weight: 600;
    line-height: 1;
    letter-spacing: -0.04em;
    color: #00ffff;
    text-shadow: 0 0 50px rgba(0, 255, 255, 0.3);
  }

  .stat-sub {
    font-size: 1rem;
    line-height: 1.7;
    color: rgba(245, 245, 247, 0.72);
    max-width: 32ch;
    margin: 1.25rem auto 0;
  }

  .quality-panel {
    position: relative;
    border-radius: 1.25rem;
    overflow: hidden;
    border: 1px solid rgba(0, 255, 255, 0.08);
    background: linear-gradient(135deg, rgba(22, 22, 25, 0.96) 0%, rgba(10, 10, 11, 0.98) 100%);
  }

  .quality-panel-content {
    position: relative;
    z-index: 10;
    padding: 1.75rem 2rem;
  }

  .quality-panel-head {
    font-family: 'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, monospace;
    font-size: 0.6875rem;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: rgba(245, 245, 247, 0.5);
    margin: 0 0 1.25rem;
    font-weight: 500;
  }

  .quality-list {
    list-style: none;
    display: grid;
    gap: 0.8rem;
    margin: 0;
    padding: 0;
  }

  .quality-list li {
    display: flex;
    gap: 0.75rem;
    font-size: 0.9375rem;
    color: rgba(245, 245, 247, 0.82);
    align-items: flex-start;
    line-height: 1.55;
  }

  .quality-list li svg {
    flex: 0 0 auto;
    margin-top: 4px;
    filter: drop-shadow(0 0 6px rgba(0, 255, 255, 0.45));
  }

  /* ── Consults ── */

  .consults-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 2rem;
    max-width: 1100px;
    margin: 0 auto;
  }

  .consult-col {
    padding: 2rem 2.25rem;
    border: 1px solid rgba(103, 232, 249, 0.12);
    border-radius: 1.25rem;
    background: linear-gradient(135deg, rgba(103, 232, 249, 0.05) 0%, rgba(45, 212, 191, 0.02) 100%);
  }

  .consult-title {
    font-size: 1.5rem;
    font-weight: 500;
    letter-spacing: -0.025em;
    color: #ffffff;
    margin: 0.75rem 0 0.75rem;
    line-height: 1.25;
  }

  .consult-body {
    font-size: 1rem;
    line-height: 1.75;
    color: rgba(245, 245, 247, 0.72);
    margin: 0;
  }

  .pull-quote {
    margin: 4rem auto 0;
    max-width: 820px;
    text-align: center;
    font-size: clamp(1.35rem, 2.6vw, 1.9rem);
    font-weight: 500;
    letter-spacing: -0.02em;
    line-height: 1.5;
    color: #f5f5f7;
  }

  .pull-quote-accent {
    color: #00ffff;
    text-shadow: 0 0 30px rgba(0, 255, 255, 0.25);
  }

  /* ── Plan ── */

  .plan-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
    gap: 2rem;
  }

  .plan-card {
    position: relative;
    display: block;
    min-height: 260px;
    border-radius: 1.25rem;
    overflow: hidden;
    border: 1px solid rgba(0, 255, 255, 0.08);
    background: linear-gradient(135deg, rgba(22, 22, 25, 0.96) 0%, rgba(10, 10, 11, 0.98) 100%);
    backdrop-filter: blur(10px);
    text-decoration: none;
    transition: transform 0.4s cubic-bezier(0.16, 1, 0.3, 1), box-shadow 0.4s cubic-bezier(0.16, 1, 0.3, 1);
  }

  .plan-card:hover {
    transform: translateY(-8px) scale(1.01);
    box-shadow: 0 20px 60px rgba(0, 0, 0, 0.6), 0 0 0 1px rgba(103, 232, 249, 0.1), 0 0 20px rgba(45, 212, 191, 0.08);
  }

  .plan-card:hover .driver-card-bg {
    opacity: 0.4;
    transform: scale(1.05);
    filter: grayscale(1) sepia(0.5) brightness(0.8) contrast(0.98);
  }

  .plan-card:hover .driver-card-aura {
    opacity: 1;
  }

  .plan-card-content {
    position: relative;
    z-index: 10;
    padding: 2.25rem 2rem;
    display: flex;
    flex-direction: column;
    height: 100%;
    min-height: 260px;
  }

  .plan-card-who {
    font-family: 'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, monospace;
    font-size: 0.625rem;
    font-weight: 500;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: rgba(0, 255, 255, 0.75);
  }

  .plan-card-title {
    font-size: 1.375rem;
    font-weight: 600;
    letter-spacing: -0.02em;
    color: #ffffff;
    margin: 0.75rem 0 0.6rem;
    line-height: 1.25;
    text-shadow: 0 2px 8px rgba(0, 0, 0, 0.5);
  }

  .plan-card-body {
    font-size: 0.9375rem;
    line-height: 1.7;
    color: rgba(245, 245, 247, 0.78);
    margin: 0;
    flex: 1;
    text-shadow: 0 1px 3px rgba(0, 0, 0, 0.4);
  }

  .plan-card-go {
    margin-top: 1.5rem;
    font-size: 1.125rem;
    font-weight: 700;
    color: #00ffff;
    transition: transform 0.3s cubic-bezier(0.16, 1, 0.3, 1);
  }

  .plan-card:hover .plan-card-go {
    transform: translateX(6px);
  }

  .plan-else {
    margin: 2.5rem auto 0;
    text-align: center;
    font-size: 0.9375rem;
    color: rgba(245, 245, 247, 0.65);
  }

  .plan-else a {
    color: #00ffff;
    text-decoration: none;
    border-bottom: 1px solid rgba(0, 255, 255, 0.4);
    transition: opacity 0.2s ease;
  }

  .plan-else a:hover {
    opacity: 0.8;
  }

  /* ── Shared card chrome (frames, aura, texture) ── */

  .driver-card-frame-outer {
    position: absolute;
    inset: 0;
    border: 6px solid transparent;
    border-radius: 1.25rem;
    background: linear-gradient(
      135deg,
      rgba(103, 232, 249, 0.22) 0%,
      rgba(45, 212, 191, 0.14) 25%,
      rgba(103, 232, 249, 0.18) 50%,
      rgba(45, 212, 191, 0.12) 75%,
      rgba(103, 232, 249, 0.16) 100%
    ) border-box;
    -webkit-mask: linear-gradient(#fff 0 0) padding-box, linear-gradient(#fff 0 0);
    -webkit-mask-composite: xor;
    mask-composite: exclude;
    opacity: 0.4;
    pointer-events: none;
    transition: all 0.4s cubic-bezier(0.16, 1, 0.3, 1);
    z-index: 15;
  }

  .driver-card-frame-inner {
    position: absolute;
    inset: 10px;
    border: 1px solid rgba(103, 232, 249, 0.16);
    border-radius: 0.75rem;
    box-shadow: inset 0 0 6px rgba(45, 212, 191, 0.1), 0 0 6px rgba(103, 232, 249, 0.08);
    transition: all 0.4s cubic-bezier(0.16, 1, 0.3, 1);
  }

  .driver-card-aura {
    position: absolute;
    inset: -20px;
    background: radial-gradient(circle at 50% 50%, rgba(103, 232, 249, 0.01) 0%, rgba(45, 212, 191, 0.006) 40%, transparent 70%);
    opacity: 0;
    transition: opacity 0.5s cubic-bezier(0.16, 1, 0.3, 1);
    pointer-events: none;
    z-index: 5;
    filter: blur(15px);
  }

  .driver-card-bg {
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    object-fit: cover;
    z-index: 0;
    opacity: 0.28;
    filter: grayscale(1) sepia(0.5) brightness(0.72) contrast(0.92);
    transition: all 0.5s cubic-bezier(0.16, 1, 0.3, 1);
  }

  /* ── Footer ── */

  .footer {
    padding: 4rem 1.5rem 2rem;
    background: #0a0a0b;
    border-top: 1px solid rgba(255, 255, 255, 0.08);
  }

  .footer-container {
    max-width: 1400px;
    margin: 0 auto;
  }

  .footer-content {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 3rem;
    flex-wrap: wrap;
  }

  .footer-brand {
    flex: 1;
    min-width: 250px;
  }

  .footer-logo-text {
    font-weight: 600;
    font-size: 1.125rem;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: #f5f5f7;
    opacity: 0.95;
  }

  .footer-tagline {
    font-size: 0.9375rem;
    color: rgba(245, 245, 247, 0.6);
    margin: 1rem 0 0;
    line-height: 1.5;
    max-width: 52ch;
  }

  .footer-location {
    font-size: 0.875rem;
    color: rgba(245, 245, 247, 0.5);
    margin: 0.6rem 0 0;
  }

  .footer-contact {
    text-align: right;
  }

  .footer-link {
    font-size: 1rem;
    color: #00ffff;
    text-decoration: none;
    transition: opacity 0.2s ease;
  }

  .footer-link:hover {
    opacity: 0.8;
  }

  .footer-promise {
    font-family: 'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, monospace;
    font-size: 0.6875rem;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: rgba(245, 245, 247, 0.4);
    margin: 1rem 0 0;
    line-height: 1.7;
  }

  .footer-copyright {
    font-size: 0.875rem;
    color: rgba(245, 245, 247, 0.5);
    margin: 1rem 0 0;
  }

  /* ── Responsive ── */

  @media (max-width: 1020px) {
    .findings-grid {
      grid-template-columns: 1fr;
      max-width: 640px;
      margin: 0 auto;
    }
  }

  @media (max-width: 860px) {
    .assessment-grid,
    .consults-grid {
      grid-template-columns: 1fr;
      gap: 2rem;
    }
  }

  @media (max-width: 768px) {
    .hero-section {
      min-height: 90vh;
    }

    .section {
      padding: 3rem 1rem;
    }

    .section-header-center {
      margin-bottom: 2.5rem;
    }

    .sup-grid {
      grid-template-columns: 1fr;
      gap: 1.25rem;
    }

    .plan-grid {
      grid-template-columns: 1fr;
      gap: 1.5rem;
    }

    .plan-card,
    .plan-card-content {
      min-height: auto;
    }

    .diverge-inner {
      padding: 1.25rem 1.25rem 1.1rem;
    }

    .consult-col {
      padding: 1.5rem 1.5rem;
    }

    .footer-content {
      flex-direction: column;
      gap: 2rem;
    }

    .footer-contact {
      text-align: left;
    }
  }

  @media (min-width: 768px) {
    .hero-headline {
      letter-spacing: -0.04em;
    }
  }
`;

export default function App() {
  const isEmailPreviewRoute =
    typeof window !== "undefined" &&
    (window.location.pathname === "/email-preview" || window.location.search.includes("emailPreview=1"));

  if (isEmailPreviewRoute) {
    return <RecoveryResourcesEmailPreview />;
  }

  const path = typeof window !== "undefined" ? window.location.pathname : "/";
  const memberOnboardMatch = path.match(/^\/onboard\/m\/([^/]+)\/?$/);
  if (memberOnboardMatch) {
    return (
      <AuthProvider>
        <OnboardingWizard token={decodeURIComponent(memberOnboardMatch[1])} mode="member" />
      </AuthProvider>
    );
  }
  const onboardMatch = path.match(/^\/onboard\/([^/]+)\/?$/);
  if (onboardMatch) {
    return (
      <AuthProvider>
        <OnboardingWizard token={decodeURIComponent(onboardMatch[1])} />
      </AuthProvider>
    );
  }
  const tenantSignInMatch = path.match(/^\/t\/([^/]+)\/sign-in\/?$/);
  if (tenantSignInMatch) {
    return (
      <AuthProvider>
        <TenantSignIn slug={decodeURIComponent(tenantSignInMatch[1])} />
      </AuthProvider>
    );
  }

  const view = parseLandingView();

  return (
    <AuthProvider>
      <SiteHeader activeView={view} />
      {view === "home" && <LandingContent />}
      {view === "whitepaper" && <TeamWhitepaperPage />}
      {view === "calculator" && <TeamCalculator />}
      {view === "podcastBlogs" && <PodcastAndBlogsPage />}
    </AuthProvider>
  );
}
