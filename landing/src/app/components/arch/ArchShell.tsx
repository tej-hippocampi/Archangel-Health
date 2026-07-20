/**
 * Archangel Health — landing shell (PRD: Archangel_Landing_Rebuild_v2).
 * Menu-driven, audience-segmented SPA: hero-only home plus five routes, all
 * inside the preserved v3 "console" design system. Owns routing (pushState),
 * the full-viewport menu panel, lead/auth/contributor modals, per-route
 * titles, and the shared reveal IntersectionObserver.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { SignInDialog } from "@/app/components/SignInDialog";
import { SignUpDialog } from "@/app/components/SignUpDialog";
import {
  LeadFormModal,
  ContributorChooser,
  PhysicianOnboardModal,
  type LeadKind,
} from "@/app/components/LandingContactModals";
import { useLandingAuth } from "@/app/hooks/useLandingAuth";
import { baseStyles } from "./baseStyles";
import { routeStyles } from "./routeStyles";
import { MenuPanel } from "./MenuPanel";
import { HomePage } from "./routes/HomePage";
import { ResearchPage } from "./routes/ResearchPage";
import { DataPage } from "./routes/DataPage";
import { HealthSystemsPage } from "./routes/HealthSystemsPage";
import { PhysiciansPage } from "./routes/PhysiciansPage";
import { MissionPage } from "./routes/MissionPage";
import "@/styles/clinical-fonts.css";

export const MAIL = "aryaabhatia@berkeley.edu";
export const mailto = (subject: string) => `mailto:${MAIL}?subject=${encodeURIComponent(subject)}`;

export type ArchPath = "/" | "/research" | "/data" | "/health-systems" | "/physicians" | "/mission";

export const ARCH_PATHS: ArchPath[] = ["/", "/research", "/data", "/health-systems", "/physicians", "/mission"];

const TITLES: Record<ArchPath, { title: string; desc: string }> = {
  "/": {
    title: "Archangel Health — Data to Power Clinical and Medical AI",
    desc: "Expert clinical reasoning over real, de-identified cases — training data for clinical and medical AI.",
  },
  "/research": {
    title: "Research — Archangel Health",
    desc: "Publishing on how frontier models fail clinical reasoning — and how to measure it.",
  },
  "/data": {
    title: "Data buyers — Archangel Health",
    desc: "The cases that break frontier models — and the expert reasoning that resolves them.",
  },
  "/health-systems": {
    title: "Health systems — Archangel Health",
    desc: "Longitudinal, de-identified patient data is the raw material for medical AI. Expert Determination, watermarked, never resold.",
  },
  "/physicians": {
    title: "Physicians & experts — Archangel Health",
    desc: "Work through real de-identified cases, judge AI reasoning, and earn $150–$300+/hour for your expertise.",
  },
  "/mission": {
    title: "Mission — Archangel Health",
    desc: "Doctors earn from their judgment. Models learn from it. Team, mission, and contact.",
  },
};

export type ShellActions = {
  navigate: (to: string) => void;
  openLead: (kind: LeadKind) => void;
  openContributor: () => void;
  openPhysicianOnboard: () => void;
  handleMailto: (e: React.MouseEvent<HTMLAnchorElement>) => void;
};

function normalizePath(p: string): ArchPath {
  const clean = (p || "/").replace(/\/+$/, "") || "/";
  return (ARCH_PATHS as string[]).includes(clean) ? (clean as ArchPath) : "/";
}

export default function ArchShell({ initialPath }: { initialPath?: string }) {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const [route, setRoute] = useState<ArchPath>(() =>
    normalizePath(initialPath ?? (typeof window !== "undefined" ? window.location.pathname : "/"))
  );
  const [menuOpen, setMenuOpen] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const menuTriggerRef = useRef<HTMLButtonElement | null>(null);

  const {
    user,
    loading,
    logout,
    signInOpen,
    setSignInOpen,
    signUpOpen,
    setSignUpOpen,
    signUpInitialStep,
    doctorPortalUrl,
    doctorPortalLabel,
  } = useLandingAuth();
  const [leadModal, setLeadModal] = useState<LeadKind | null>(null);
  const [contributorOpen, setContributorOpen] = useState(false);
  const [physOnboardOpen, setPhysOnboardOpen] = useState(false);

  /* ---------- routing (pushState SPA) ---------- */
  const navigate = useCallback((to: string) => {
    const [pathPart, hash] = to.split("#");
    const next = normalizePath(pathPart);
    if (typeof window !== "undefined" && window.location.pathname !== next) {
      window.history.pushState({}, "", hash ? `${next}#${hash}` : next);
    } else if (hash && typeof window !== "undefined") {
      window.history.replaceState({}, "", `${next}#${hash}`);
    }
    setRoute(next);
    setMenuOpen(false);
    requestAnimationFrame(() => {
      if (hash) {
        document.getElementById(hash)?.scrollIntoView({ block: "start" });
      } else {
        window.scrollTo(0, 0);
      }
    });
  }, []);

  useEffect(() => {
    const onPop = () => setRoute(normalizePath(window.location.pathname));
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  /* ---------- per-route title + meta description ---------- */
  useEffect(() => {
    const meta = TITLES[route];
    document.title = meta.title;
    document.querySelector('meta[name="description"]')?.setAttribute("content", meta.desc);
  }, [route]);

  /* ---------- scroll reveals (re-armed per route) ---------- */
  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const revealEls = root.querySelectorAll(".reveal, .trace-scroll, .flow, .steps-strip");
    if (reduced || !("IntersectionObserver" in window)) {
      revealEls.forEach((el) => el.classList.add("in"));
      return;
    }
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.isIntersecting) {
            e.target.classList.add("in");
            io.unobserve(e.target);
          }
        }
      },
      { threshold: 0.15, rootMargin: "0px 0px -40px 0px" }
    );
    revealEls.forEach((el) => io.observe(el));
    return () => io.disconnect();
  }, [route]);

  /* ---------- deep-link hash on first load ---------- */
  useEffect(() => {
    const hash = window.location.hash.replace("#", "");
    if (!hash || hash === "recovery-plan") return;
    const t = setTimeout(() => document.getElementById(hash)?.scrollIntoView({ block: "start" }), 120);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    return () => {
      if (toastTimer.current) clearTimeout(toastTimer.current);
    };
  }, []);

  /* ---------- mailto fallback (copy + toast) ---------- */
  const handleMailto = useCallback((e: React.MouseEvent<HTMLAnchorElement>) => {
    const email = e.currentTarget.href.replace("mailto:", "").split("?")[0];
    const done = (copied: boolean) => {
      setToast(copied ? `Email copied — ${email}` : `Email us: ${email}`);
      if (toastTimer.current) clearTimeout(toastTimer.current);
      toastTimer.current = setTimeout(() => setToast(null), 4200);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(email).then(
        () => done(true),
        () => done(false)
      );
    } else {
      done(false);
    }
  }, []);

  const actions: ShellActions = {
    navigate,
    openLead: (kind) => setLeadModal(kind),
    openContributor: () => setContributorOpen(true),
    openPhysicianOnboard: () => setPhysOnboardOpen(true),
    handleMailto,
  };

  const closeMenu = useCallback(() => {
    setMenuOpen(false);
    menuTriggerRef.current?.focus();
  }, []);

  return (
    <div ref={rootRef} className="arch-landing">
      <header className="nav" id="top">
        <div className="nav-left">
          <button
            ref={menuTriggerRef}
            type="button"
            className="chrome chrome-box menu-trigger"
            aria-haspopup="dialog"
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen(true)}
          >
            <span className="menu-glyph" aria-hidden="true"><i /><i /><i /></span>
            <span className="menu-label">Menu</span>
          </button>
          <a
            className="wordmark"
            href="/"
            aria-label="Archangel Health — home"
            onClick={(e) => {
              e.preventDefault();
              navigate("/");
            }}
          >
            <svg className="halo" viewBox="0 0 24 24" aria-hidden="true">
              <ellipse cx="12" cy="12" rx="9" ry="5.4" fill="none" stroke="currentColor" strokeWidth="1.7" transform="rotate(-24 12 12)" />
            </svg>
            <span>Archangel&nbsp;Health</span>
          </a>
        </div>
        <nav className="nav-links" aria-label="Primary">
          {!loading &&
            (user ? (
              <>
                {doctorPortalUrl && (
                  <a className="chrome chrome-box hide-sm" href={doctorPortalUrl}>{doctorPortalLabel}</a>
                )}
                <button type="button" className="chrome chrome-box hide-sm" onClick={logout}>Sign out</button>
              </>
            ) : (
              <button type="button" className="chrome chrome-box hide-sm" onClick={() => setSignInOpen(true)}>
                Sign in
              </button>
            ))}
          <button type="button" className="chrome chrome-box solid" onClick={() => setLeadModal("request_data")}>
            Request data
          </button>
        </nav>
      </header>

      {menuOpen && (
        <MenuPanel
          active={route}
          onClose={closeMenu}
          onNavigate={navigate}
          onRequestData={() => {
            setMenuOpen(false);
            setLeadModal("request_data");
          }}
          onSignIn={() => {
            setMenuOpen(false);
            setSignInOpen(true);
          }}
          onSignOut={logout}
          signedIn={Boolean(user)}
          portalUrl={doctorPortalUrl || null}
          portalLabel={doctorPortalLabel}
          mail={MAIL}
        />
      )}

      <main>
        {route === "/" && <HomePage actions={actions} />}
        {route === "/research" && <ResearchPage actions={actions} />}
        {route === "/data" && <DataPage actions={actions} />}
        {route === "/health-systems" && <HealthSystemsPage actions={actions} />}
        {route === "/physicians" && <PhysiciansPage actions={actions} />}
        {route === "/mission" && <MissionPage actions={actions} />}
      </main>

      <footer className="footer">
        <div className="foot-left">
          <span className="foot-mark">Archangel Health</span>
          <span className="label">Berkeley, California</span>
        </div>
        <div className="foot-right foot-nav">
          <a
            href="/mission"
            onClick={(e) => {
              e.preventDefault();
              navigate("/mission");
            }}
          >
            Mission &amp; team
          </a>
          <a href={`mailto:${MAIL}`} onClick={handleMailto}>{MAIL}</a>
        </div>
        <p className="foot-line chrome">Real · De-identified · IP-cleared · Never resold beyond license</p>
      </footer>

      <div className={`toast${toast ? " show" : ""}`} role="status">{toast}</div>

      {/* Auth (unchanged flows): Sign in → portal handoff, Sign up → onboarding. */}
      <SignInDialog open={signInOpen} onOpenChange={setSignInOpen} />
      <SignUpDialog open={signUpOpen} onOpenChange={setSignUpOpen} initialStep={signUpInitialStep} />

      {/* Lead-capture forms → backend /api/leads → emails the configured recipient. */}
      <LeadFormModal kind="request_data" open={leadModal === "request_data"} onClose={() => setLeadModal(null)} />
      <LeadFormModal kind="provide_data" open={leadModal === "provide_data"} onClose={() => setLeadModal(null)} />

      {/* "Become a contributor" → annotator (the /physicians offer, then
          self-serve onboarding) or data contributor (Provide data). */}
      <ContributorChooser
        open={contributorOpen}
        onClose={() => setContributorOpen(false)}
        onAnnotator={() => {
          setContributorOpen(false);
          // Offer first, signup last (PRD §7): land on the physician route;
          // its CTA mints the self-serve onboarding link.
          navigate("/physicians");
        }}
        onDataContributor={() => {
          setContributorOpen(false);
          setLeadModal("provide_data");
        }}
      />

      {/* Physician self-serve onboarding — mints /onboard/<token> and redirects. */}
      <PhysicianOnboardModal open={physOnboardOpen} onClose={() => setPhysOnboardOpen(false)} />

      <style>{baseStyles}</style>
      <style>{routeStyles}</style>
    </div>
  );
}
