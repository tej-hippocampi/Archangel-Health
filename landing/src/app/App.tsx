import { useState, useEffect } from "react";
import { motion } from "motion/react";
import { AuthProvider, useAuth } from "@/contexts/AuthContext";
import { SignInDialog } from "@/app/components/SignInDialog";
import { SignUpDialog } from "@/app/components/SignUpDialog";
import ArchangelHealthLogo from "@/app/components/ArchangelHealthLogo";
import RecoveryResourcesEmailPreview from "@/app/components/RecoveryResourcesEmailPreview";
import * as authApi from "@/lib/auth-api";

const HIPPOCRATES_BG = "/hippocrates-email-bg.png";

interface DriverCardProps {
  title: string;
  bullets: string[];
  whyItMatters: string;
  delay?: number;
}

function DriverCard({ title, bullets, whyItMatters, delay = 0 }: DriverCardProps) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 30 }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true, margin: "-100px" }}
      transition={{ duration: 0.8, delay, ease: [0.16, 1, 0.3, 1] }}
      className="driver-card"
    >
      <div className="driver-card-frame-outer">
        <div className="driver-card-frame-inner" />
      </div>
      <div className="driver-card-aura" />
      <img src={HIPPOCRATES_BG} alt="" className="driver-card-bg" />
      <div className="driver-card-content">
        <h3 className="driver-card-title">{title}</h3>
        <ul className="driver-card-bullets">
          {bullets.map((bullet, index) => (
            <li key={index} className="driver-card-bullet">
              {bullet}
            </li>
          ))}
        </ul>
        <div className="driver-card-financial">
          <p className="driver-card-financial-text">{whyItMatters}</p>
        </div>
      </div>
    </motion.div>
  );
}

// Dashboard link after login: env or default to backend in dev
const env = (import.meta as unknown as { env: { VITE_DASHBOARD_URL?: string; VITE_API_URL?: string; DEV?: boolean } }).env;
const DASHBOARD_URL =
  env?.VITE_DASHBOARD_URL ??
  env?.VITE_API_URL ??
  (env?.DEV ? "http://localhost:8000" : "");

function LandingContent() {
  const { user, loading, logout, token } = useAuth();
  const [signInOpen, setSignInOpen] = useState(false);
  const [signUpOpen, setSignUpOpen] = useState(false);
  const [signUpInitialStep, setSignUpInitialStep] = useState<"role" | "patient-codes">("role");

  // After login, redirect to dashboard only if doctor has completed onboarding (has profile)
  useEffect(() => {
    if (!user || !DASHBOARD_URL || !token) return;
    let cancelled = false;
    authApi.getDoctorProfile(token).then((profile) => {
      if (!cancelled && profile) {
        window.location.href = DASHBOARD_URL + "#auth=" + encodeURIComponent(token);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [user, token]);

  // When landing with ?signout=1 or pathname /auth/signout (from doctor dashboard Sign out), clear auth and clean URL
  const { logout: authLogout } = useAuth();
  useEffect(() => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    const isSignoutQuery = params.get("signout") === "1";
    const isSignoutPath = window.location.pathname === "/auth/signout";
    if (isSignoutQuery || isSignoutPath) {
      authLogout();
      params.delete("signout");
      const newSearch = params.toString();
      const newUrl = (isSignoutPath ? "/" : window.location.pathname) + (newSearch ? "?" + newSearch : "") + window.location.hash;
      window.history.replaceState(null, "", newUrl);
    }
  }, [authLogout]);

  // When landing with #recovery-plan (e.g. from email "View your recovery plan" link), open Sign up dialog on patient code step
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (window.location.hash === "#recovery-plan") {
      setSignUpInitialStep("patient-codes");
      setSignUpOpen(true);
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
    }
  }, []);

  return (
    <>
      <div className="app-container">
        <div className="hero-logo">
          <ArchangelHealthLogo />
        </div>

        <nav
          className="auth-nav absolute top-6 right-6 md:top-8 md:right-8 flex items-center justify-end gap-3"
          style={{ zIndex: 100, pointerEvents: "auto" }}
          aria-label="Account"
        >
          {!loading && (
            <>
              {user ? (
                <>
                  <span className="text-[#f5f5f7]/95 text-sm font-medium max-w-[140px] truncate">
                    {user.email}
                  </span>
                  {DASHBOARD_URL && (
                    <a
                      href={DASHBOARD_URL}
                      className="auth-btn auth-btn-primary inline-flex items-center justify-center rounded-full px-4 py-2 text-sm font-medium text-[#0a0a0b] bg-[#f5f5f7] hover:bg-[#e0e0e5] transition-colors"
                    >
                      {user.name
                        ? user.name.trim().split(" ").slice(0, 2).join(" ")
                        : "Doctor Portal"}
                    </a>
                  )}
                  <button
                    type="button"
                    onClick={logout}
                    className="auth-btn inline-flex items-center justify-center rounded-full border border-[rgba(255,255,255,0.3)] px-4 py-2 text-sm font-medium text-[#f5f5f7] hover:bg-white/10 transition-colors"
                  >
                    Sign out
                  </button>
                </>
              ) : (
                <>
                  <button
                    type="button"
                    onClick={() => setSignInOpen(true)}
                    className="auth-btn inline-flex items-center justify-center rounded-full border border-[rgba(255,255,255,0.3)] px-4 py-2 text-sm font-medium text-[#f5f5f7] hover:bg-white/10 transition-colors"
                  >
                    Sign in
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      setSignUpInitialStep("role");
                      setSignUpOpen(true);
                    }}
                    className="auth-btn auth-btn-primary inline-flex items-center justify-center rounded-full px-4 py-2 text-sm font-medium text-[#0a0a0b] bg-[#f5f5f7] hover:bg-[#00ffff] hover:text-[#0a0a0b] transition-all shadow-[0_0_20px_rgba(0,255,255,0.2)]"
                  >
                    Sign up
                  </button>
                </>
              )}
            </>
          )}
        </nav>

        <section className="hero-section">
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
              <motion.h1
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 1.2, delay: 0.4, ease: [0.16, 1, 0.3, 1] }}
                className="hero-headline"
              >
                The Platform to Win at TEAM
              </motion.h1>

              <motion.div
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 1, delay: 0.8, ease: [0.16, 1, 0.3, 1] }}
                className="hero-cta-bottom"
              >
                <a
                  href="https://calendly.com/tejxpatel23/archangel-health-intro"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="hero-demo-button"
                >
                  <img src={HIPPOCRATES_BG} alt="" className="hippocrates-bg-button" />
                  <span className="hero-demo-button-text">Book a demo</span>
                </a>
              </motion.div>
            </div>
          </div>
        </section>

        <section className="section section-dark">
          <div className="section-container">
            <motion.div
              initial={{ opacity: 0, y: 20 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true, margin: "-100px" }}
              transition={{ duration: 0.8, ease: [0.16, 1, 0.3, 1] }}
              className="section-header-center section-header-team"
            >
              <h2 className="section-title">Three TEAM performance drivers, built into one platform.</h2>
            </motion.div>

            <div className="drivers-grid">
              <DriverCard
                title="Information Transfer PRO-PM"
                bullets={[
                  "Personalized post-discharge instruction videos and battlecards generated directly from the patient's own discharge notes",
                  "Automated SMS nudges every other day, with structured patient check-ins at Day 7, 14, and 30 — negative responses escalated to your care team immediately",
                  "Patients arrive at CMS's HCAHPS survey already informed and confident — your Information Transfer score improves before CMS ever asks",
                ]}
                whyItMatters="Your Information Transfer score feeds directly into your CQS — the multiplier that adjusts your entire TEAM reconciliation payment by up to ±15%."
                delay={0.1}
              />
              <DriverCard
                title="Reduce Readmissions"
                bullets={[
                  "Every patient gets a 24/7 AI discharge companion trained on their surgeon's own instructions and discharge notes",
                  "Recovery questions get answered instantly — eliminating the confusion that sends patients to the ER at 2am",
                  "Patients follow the care pathway their surgeon defined instead of defaulting to the emergency room",
                ]}
                whyItMatters="Every readmission costs $15,000–$30,000 against your episode budget — reducing them is the single fastest way to stay under target price and keep your savings."
                delay={0.2}
              />
              <DriverCard
                title="Document PCP Referrals Without the Friction"
                bullets={[
                  "PCP referral details captured and timestamped automatically at the point of discharge",
                  "A documented, audit-ready referral record generated for every single TEAM patient",
                  "Patient receives SMS confirmation — TEAM's mandatory referral requirement closed on every episode, without manual effort",
                ]}
                whyItMatters="TEAM mandates a documented PCP referral for every patient at discharge — missing it is a compliance gap that directly hits your CQS score."
                delay={0.3}
              />
            </div>
          </div>
        </section>

        <footer className="footer">
          <div className="footer-container">
            <div className="footer-content">
              <div className="footer-brand">
                <div className="footer-logo">
                  <span className="footer-logo-text">ARCHANGEL HEALTH</span>
                </div>
                <p className="footer-tagline">Intelligent patient education for TEAM episode performance</p>
              </div>
              <div className="footer-contact">
                <a href="mailto:tejpatel@archangelhealth.ai" className="footer-link">
                  tejpatel@archangelhealth.ai
                </a>
                <p className="footer-copyright">© 2026 Archangel Health. All rights reserved.</p>
              </div>
            </div>
          </div>
        </footer>

        <style>{styles}</style>
      </div>

      <SignInDialog open={signInOpen} onOpenChange={setSignInOpen} />
      <SignUpDialog open={signUpOpen} onOpenChange={setSignUpOpen} initialStep={signUpInitialStep} />
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

  .hero-logo > div {
    position: absolute !important;
    top: 1.5rem !important;
    left: 1.5rem !important;
    z-index: 100 !important;
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

  .auth-nav {
    position: absolute !important;
  }

  .hero-section {
    position: relative;
    min-height: 100vh;
    width: 100%;
    overflow: hidden;
    background: #0a0a0b;
    display: flex;
    align-items: center;
    justify-content: center;
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
    background: linear-gradient(180deg, rgba(10, 10, 11, 0.5), transparent, rgba(10, 10, 11, 0.7));
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

  .hero-headline {
    font-size: clamp(2.5rem, 6vw, 4.5rem);
    font-weight: 500;
    line-height: 1.15;
    letter-spacing: -0.03em;
    color: #f5f5f7;
    margin: 0 auto 2.5rem;
    max-width: 1100px;
    text-shadow: 0 0 40px rgba(0, 255, 255, 0.08);
  }

  .hero-cta-bottom {
    margin-top: 0;
    display: flex;
    justify-content: center;
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

  .section-header-center {
    text-align: center;
    margin-bottom: 3rem;
  }

  .section-header-team {
    margin-top: -0.4rem;
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

  .drivers-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
    gap: 2rem;
  }

  .driver-card {
    position: relative;
    min-height: 740px;
    border-radius: 1.25rem;
    overflow: hidden;
    border: 1px solid rgba(0, 255, 255, 0.08);
    background: linear-gradient(135deg, rgba(22, 22, 25, 0.96) 0%, rgba(10, 10, 11, 0.98) 100%);
    backdrop-filter: blur(10px);
    transition: transform 0.4s cubic-bezier(0.16, 1, 0.3, 1), box-shadow 0.4s cubic-bezier(0.16, 1, 0.3, 1);
  }

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

  .driver-card-content {
    position: relative;
    z-index: 10;
    padding: 3rem 2.5rem;
    display: flex;
    flex-direction: column;
    height: 100%;
  }

  .driver-card-title {
    font-size: 1.625rem;
    font-weight: 600;
    letter-spacing: -0.025em;
    color: #ffffff;
    margin: 0 0 2rem;
    line-height: 1.25;
    min-height: 2.5rem;
    text-shadow: 0 2px 8px rgba(0, 0, 0, 0.5);
  }

  .driver-card-bullets {
    list-style: none;
    padding: 0;
    margin: 0 0 auto;
    flex-grow: 1;
    display: flex;
    flex-direction: column;
    gap: 1.25rem;
  }

  .driver-card-bullet {
    font-size: 1rem;
    line-height: 1.75;
    color: rgba(245, 245, 247, 0.85);
    padding-left: 1.75rem;
    position: relative;
    text-shadow: 0 1px 3px rgba(0, 0, 0, 0.4);
  }

  .driver-card-bullet::before {
    content: '→';
    position: absolute;
    left: 0;
    color: rgba(103, 232, 249, 0.7);
    font-weight: 700;
    font-size: 1.125rem;
  }

  .driver-card-financial {
    padding: 1.75rem 1.5rem;
    background: linear-gradient(135deg, rgba(103, 232, 249, 0.06) 0%, rgba(45, 212, 191, 0.03) 100%);
    border: 1px solid rgba(103, 232, 249, 0.14);
    border-radius: 0.75rem;
    margin-top: 2rem;
    backdrop-filter: blur(8px);
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.2), inset 0 0 20px rgba(45, 212, 191, 0.03);
    flex-shrink: 0;
  }

  .driver-card-financial-text {
    font-size: 1.0625rem;
    line-height: 1.7;
    color: #ffffff;
    font-weight: 500;
    margin: 0;
    text-shadow: 0 1px 4px rgba(0, 0, 0, 0.5);
    letter-spacing: -0.01em;
  }

  .driver-card:hover {
    transform: translateY(-8px) scale(1.01);
    box-shadow: 0 20px 60px rgba(0, 0, 0, 0.6), 0 0 0 1px rgba(103, 232, 249, 0.1), 0 0 20px rgba(45, 212, 191, 0.08);
  }

  .driver-card:hover .driver-card-bg {
    opacity: 0.4;
    transform: scale(1.05);
    filter: grayscale(1) sepia(0.5) brightness(0.8) contrast(0.98);
  }

  .driver-card:hover .driver-card-aura {
    opacity: 1;
  }

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

  .footer-copyright {
    font-size: 0.875rem;
    color: rgba(245, 245, 247, 0.5);
    margin: 1rem 0 0;
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

    .drivers-grid {
      grid-template-columns: 1fr;
      gap: 1.5rem;
    }

    .driver-card {
      min-height: auto;
    }

    .driver-card-content {
      padding: 2rem 1.5rem;
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
    .hero-logo > div {
      top: 2rem !important;
      left: 2rem !important;
    }

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

  return (
    <AuthProvider>
      <LandingContent />
    </AuthProvider>
  );
}
