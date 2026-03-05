import { useState, useEffect } from "react";
import { motion } from "motion/react";
import { AuthProvider, useAuth } from "@/contexts/AuthContext";
import { SignInDialog } from "@/app/components/SignInDialog";
import { SignUpDialog } from "@/app/components/SignUpDialog";
import ArchangelHealthLogo from "@/app/components/ArchangelHealthLogo";
import * as authApi from "@/lib/auth-api";

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
    return () => { cancelled = true; };
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
    <div className="hero relative min-h-screen w-full overflow-hidden bg-[#0a0a0b]">
      {/* Animated GIF Background - FIRST CHILD */}
      <img
        src="https://static.scientificamerican.com/dam/m/37bca03526cc32df/original/AI-pill-gif-healthspans.gif?m=1741035098.088&w=1200"
        alt=""
        className="bg-gif"
        aria-hidden="true"
      />

      {/* Dark vignette overlay for legibility */}
      <div className="absolute inset-0 z-[1] bg-gradient-radial from-transparent via-[#0a0a0b]/30 to-[#0a0a0b]/90 pointer-events-none" />
      <div className="absolute inset-0 z-[1] bg-gradient-to-b from-[#0a0a0b]/50 via-transparent to-[#0a0a0b]/70 pointer-events-none" />

      {/* Archangel Health logo (Figma) */}
      <div className="hero-logo">
        <ArchangelHealthLogo />
      </div>

      {/* Auth nav — fixed top right; inline z-index/pointer-events so nothing can cover or block clicks */}
      <nav
        className="auth-nav fixed top-6 right-6 md:top-8 md:right-8 flex items-center justify-end gap-3"
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
                  onClick={() => { setSignUpInitialStep("role"); setSignUpOpen(true); }}
                  className="auth-btn auth-btn-primary inline-flex items-center justify-center rounded-full px-4 py-2 text-sm font-medium text-[#0a0a0b] bg-[#f5f5f7] hover:bg-[#00ffff] hover:text-[#0a0a0b] transition-all shadow-[0_0_20px_rgba(0,255,255,0.2)]"
                >
                  Sign up
                </button>
              </>
            )}
          </>
        )}
      </nav>

      {/* Main centered content */}
      <div className="relative z-20 flex items-center justify-center min-h-screen px-6 md:px-8">
        <div className="max-w-5xl w-full flex flex-col items-center">
          <motion.h1
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 1.2, delay: 0.4, ease: [0.16, 1, 0.3, 1] }}
            className="headline text-[#f5f5f7] mb-12 md:mb-16 text-center"
          >
            Medicine solved diagnosis, nobody solved understanding
          </motion.h1>

          <motion.div
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 1, delay: 0.8, ease: [0.16, 1, 0.3, 1] }}
          >
            <a
              href="https://calendly.com/tejxpatel23/archangel-health-intro"
              target="_blank"
              rel="noopener noreferrer"
              className="premium-button group relative inline-block text-center no-underline"
            >
              <span className="relative z-10">Book a demo</span>
            </a>
          </motion.div>
        </div>
      </div>

      <style>{`
        .hero { position: relative; overflow: hidden; }
        .auth-nav { left: auto; }
        .auth-btn { cursor: pointer; outline: none; }
        .auth-btn:focus-visible { box-shadow: 0 0 0 2px rgba(0,255,255,0.5); }
        .auth-btn-primary:hover { box-shadow: 0 0 24px rgba(0,255,255,0.35); }
        .bg-gif {
          position: absolute; top: 0; left: 0; width: 100%; height: 100%;
          object-fit: cover; z-index: 0; pointer-events: none; opacity: 0.7;
        }
        /* Raise content above GIF; do NOT apply to logo, nav, or modal overlay so they stay on top and clickable. */
        .hero > *:not(.bg-gif):not(.hero-logo):not(.auth-nav):not(.auth-modal-overlay) { z-index: 1; }
        .bg-gradient-radial { background: radial-gradient(circle, var(--tw-gradient-stops)); }
        .headline {
          font-size: clamp(2rem, 6vw, 4.5rem); font-weight: 500; line-height: 1.1;
          letter-spacing: -0.03em; font-variant-numeric: proportional-nums;
          text-rendering: optimizeLegibility; -webkit-font-smoothing: antialiased;
          text-shadow: 0 0 40px rgba(0, 255, 255, 0.08);
        }
        @media (min-width: 768px) { .headline { letter-spacing: -0.04em; } }
        .premium-button {
          position: relative; padding: 1rem 2.5rem; font-size: 1rem; font-weight: 500;
          letter-spacing: -0.01em; color: #0a0a0b;
          background: linear-gradient(135deg, #f5f5f7 0%, #e0e0e5 100%);
          border: 1px solid rgba(255, 255, 255, 0.3); border-radius: 9999px;
          transition: all 0.4s cubic-bezier(0.16, 1, 0.3, 1); cursor: pointer; overflow: hidden;
          box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.1), 0 4px 12px rgba(0, 0, 0, 0.3),
            inset 0 1px 0 rgba(255, 255, 255, 0.5);
        }
        .premium-button::before {
          content: ''; position: absolute; inset: 0;
          background: linear-gradient(135deg, #00ffff 0%, #00cccc 100%);
          opacity: 0; transition: opacity 0.4s cubic-bezier(0.16, 1, 0.3, 1); z-index: 1;
        }
        .premium-button:hover {
          transform: translateY(-2px);
          box-shadow: 0 0 0 1px #00ffff, 0 0 20px rgba(0, 255, 255, 0.4),
            0 0 40px rgba(0, 255, 255, 0.2), 0 8px 24px rgba(0, 0, 0, 0.4),
            inset 0 1px 0 rgba(255, 255, 255, 0.6);
        }
        .premium-button:hover::before { opacity: 1; }
        .premium-button:hover span { color: #0a0a0b; }
        .premium-button:active { transform: translateY(-1px); }
        @media (min-width: 768px) {
          .premium-button { padding: 1.125rem 3rem; font-size: 1.0625rem; }
        }
      `}</style>
    </div>

    <SignInDialog open={signInOpen} onOpenChange={setSignInOpen} />
    <SignUpDialog open={signUpOpen} onOpenChange={setSignUpOpen} initialStep={signUpInitialStep} />
    </>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <LandingContent />
    </AuthProvider>
  );
}
