import { useEffect } from "react";
import { SignInDialog } from "@/app/components/SignInDialog";
import { SignUpDialog } from "@/app/components/SignUpDialog";
import ArchangelHealthLogo from "@/app/components/ArchangelHealthLogo";
import { useLandingAuth } from "@/app/hooks/useLandingAuth";

export type LandingView = "home" | "whitepaper" | "calculator" | "podcastBlogs";

export function parseLandingView(): LandingView {
  if (typeof window === "undefined") return "home";
  const path = window.location.pathname.replace(/\/$/, "");
  const params = new URLSearchParams(window.location.search);
  if (path === "/team-calculator" || params.get("team-calculator") === "1" || params.get("view") === "calculator") {
    return "calculator";
  }
  if (path === "/podcast-blogs" || params.get("view") === "podcasts") {
    return "podcastBlogs";
  }
  if (params.get("view") === "whitepaper") return "whitepaper";
  return "home";
}

interface SiteHeaderProps {
  activeView: LandingView;
}

export function SiteHeader({ activeView }: SiteHeaderProps) {
  const {
    user,
    loading,
    logout,
    signInOpen,
    setSignInOpen,
    signUpOpen,
    setSignUpOpen,
    signUpInitialStep,
    openSignUp,
    doctorPortalUrl,
    doctorPortalLabel,
  } = useLandingAuth();

  const tabClass = (view: LandingView) => {
    const on = activeView === view;
    return `site-header-tab ${on ? "site-header-tab-active" : ""}`;
  };

  return (
    <>
      <header className="site-header" role="banner">
        <a href="/" className="site-header-brand" aria-label="Archangel Health home">
          <ArchangelHealthLogo variant="inline" />
        </a>

        <nav className="site-header-nav" aria-label="Site sections">
          <a href="/" className={tabClass("home")} aria-current={activeView === "home" ? "page" : undefined}>
            Home
          </a>
          <a
            href="/team-calculator"
            className={tabClass("calculator")}
            aria-current={activeView === "calculator" ? "page" : undefined}
          >
            TEAM calculator
          </a>
          <a
            href="/podcast-blogs"
            className={tabClass("podcastBlogs")}
            aria-current={activeView === "podcastBlogs" ? "page" : undefined}
          >
            Podcast &amp; Blogs
          </a>
        </nav>

        <nav className="site-header-auth" aria-label="Account">
          {!loading && (
            <>
              {user ? (
                <>
                  <span className="site-header-email">{user.email}</span>
                  {DOCTOR_APP_URL && (
                    <a href={DOCTOR_APP_URL} className="auth-btn auth-btn-primary site-header-auth-btn">
                      {user.name ? user.name.trim().split(" ").slice(0, 2).join(" ") : "Doctor Portal"}
                    </a>
                  )}
                  <button type="button" onClick={logout} className="auth-btn site-header-auth-btn site-header-auth-outline">
                    Sign out
                  </button>
                </>
              ) : (
                <>
                  <button type="button" onClick={() => setSignInOpen(true)} className="auth-btn site-header-auth-btn site-header-auth-outline">
                    Sign in
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      setSignUpInitialStep("role");
                      setSignUpOpen(true);
                    }}
                    className="auth-btn auth-btn-primary site-header-auth-btn"
                  >
                    Sign up
                  </button>
                </>
              )}
            </>
          )}
        </nav>
      </header>

      <SignInDialog open={signInOpen} onOpenChange={setSignInOpen} />
      <SignUpDialog open={signUpOpen} onOpenChange={setSignUpOpen} initialStep={signUpInitialStep} />

      <style>{`
        .site-header {
          position: sticky;
          top: 0;
          z-index: 200;
          display: flex;
          flex-wrap: wrap;
          align-items: center;
          justify-content: space-between;
          gap: 0.75rem 1rem;
          padding: 0.75rem 1rem 0.75rem 1rem;
          border-bottom: 1px solid rgba(255, 255, 255, 0.08);
          background: rgba(10, 10, 11, 0.92);
          backdrop-filter: blur(12px);
          -webkit-backdrop-filter: blur(12px);
        }
        @media (min-width: 900px) {
          .site-header {
            padding-left: 1.5rem;
            padding-right: 1.5rem;
            flex-wrap: nowrap;
          }
        }
        .site-header-brand {
          text-decoration: none;
          color: inherit;
          flex-shrink: 0;
        }
        .site-header-nav {
          display: flex;
          flex-wrap: wrap;
          align-items: center;
          justify-content: center;
          gap: 0.35rem 0.5rem;
          order: 3;
          width: 100%;
        }
        @media (min-width: 900px) {
          .site-header-nav {
            order: 0;
            width: auto;
            flex: 1;
            justify-content: center;
          }
        }
        .site-header-tab {
          padding: 0.45rem 0.85rem;
          border-radius: 9999px;
          font-size: 0.8125rem;
          font-weight: 500;
          color: rgba(245, 245, 247, 0.75);
          text-decoration: none;
          border: 1px solid transparent;
          transition: color 0.2s, border-color 0.2s, background 0.2s;
          white-space: nowrap;
        }
        .site-header-tab:hover {
          color: #f5f5f7;
          background: rgba(255, 255, 255, 0.06);
        }
        .site-header-tab-active {
          color: #0a0a0b;
          background: #00ffff;
          border-color: rgba(0, 255, 255, 0.5);
        }
        .site-header-tab-active:hover {
          color: #0a0a0b;
          background: #77ffff;
        }
        .site-header-auth {
          display: flex;
          align-items: center;
          justify-content: flex-end;
          gap: 0.5rem;
          flex-shrink: 0;
          margin-left: auto;
        }
        .site-header-email {
          font-size: 0.8125rem;
          font-weight: 500;
          color: rgba(245, 245, 247, 0.95);
          max-width: 120px;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
        }
        @media (min-width: 640px) {
          .site-header-email {
            max-width: 180px;
          }
        }
        .site-header-auth-btn {
          display: inline-flex;
          align-items: center;
          justify-content: center;
          border-radius: 9999px;
          padding: 0.4rem 0.9rem;
          font-size: 0.8125rem;
          font-weight: 500;
          cursor: pointer;
          outline: none;
          border: none;
          text-decoration: none;
        }
        .site-header-auth-outline {
          border: 1px solid rgba(255, 255, 255, 0.3);
          background: transparent;
          color: #f5f5f7;
        }
        .site-header-auth-outline:hover {
          background: rgba(255, 255, 255, 0.1);
        }
        .auth-btn-primary.site-header-auth-btn {
          color: #0a0a0b;
          background: #f5f5f7;
        }
        .auth-btn-primary.site-header-auth-btn:hover {
          background: #00ffff;
        }
      `}</style>
    </>
  );
}
