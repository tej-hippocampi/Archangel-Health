"use client";

import * as React from "react";
import { createPortal } from "react-dom";
import { useAuth } from "@/contexts/AuthContext";
import * as authApi from "@/lib/auth-api";
import { authDialogStyles } from "./authDialogStyles";

/**
 * Two doors, not three.
 *
 * The patient card is gone from this dialog (PRD §1). It was a peri-op surface
 * whose routes are already flag-gated, and it sat beside "Doctor" implying the
 * landing page served two audiences equally when it serves one. The BACKEND
 * patient routes are untouched — this is a dialog edit — and the care-team
 * emails that deep-link patients to `/#recovery-plan` still open the sign-up
 * dialog at its code-entry step, which is why that step survives there and this
 * one has nothing to keep.
 *
 * "Health system / organization" is not a step: they have a username and a
 * password for a different app, so the card navigates to it rather than
 * rendering a second login form the portal would have to keep in sync.
 */
type Step = "role" | "doctor";

type Props = { open: boolean; onOpenChange: (open: boolean) => void };

export function SignInDialog({ open, onOpenChange }: Props) {
  const { login, error, clearError } = useAuth();
  const [step, setStep] = React.useState<Step>("role");
  const [email, setEmail] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [notice, setNotice] = React.useState("");
  const [submitting, setSubmitting] = React.useState(false);
  const [apiError, setApiError] = React.useState<string | null>(null);
  const [demoRoutes, setDemoRoutes] = React.useState<Record<string, authApi.DemoSignInRoute>>({});

  const parseTenantSlugFromError = React.useCallback((message: string): string | null => {
    const m = String(message || "").match(/\/t\/([^/\s]+)\/sign-in/i);
    return m?.[1] ? decodeURIComponent(m[1]) : null;
  }, []);

  React.useEffect(() => {
    if (!open || step !== "doctor") return;
    authApi.getDemoSignInRoutes().then(setDemoRoutes).catch(() => setDemoRoutes({}));
  }, [open, step]);

  const resetAndClose = () => {
    setStep("role");
    setEmail("");
    setPassword("");
    setNotice("");
    setApiError(null);
    clearError();
    onOpenChange(false);
  };

  const handleDoctorSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    clearError();
    setApiError(null);
    setSubmitting(true);
    const trimmedEmail = email.trim();
    const normalizedEmail = trimmedEmail.toLowerCase();
    const resolveRoute = async (): Promise<authApi.DemoSignInRoute | undefined> => {
      const cached = demoRoutes[normalizedEmail];
      if (cached) return cached;
      const fresh = await authApi.getDemoSignInRoutes();
      if (Object.keys(fresh).length) {
        setDemoRoutes(fresh);
      }
      return fresh[normalizedEmail];
    };
    try {
      const route = await resolveRoute();
      if (route?.type === "tenant" && route.slug) {
        const data = await authApi.tenantLogin(route.slug, trimmedEmail, password);
        if (!data.access_token) {
          throw new Error("Could not open doctor portal.");
        }
        resetAndClose();
        await authApi.redirectToDoctorPortal(data.access_token);
        return;
      }
      try {
        const asc = await authApi.asclepiusLogin(trimmedEmail, password);
        resetAndClose();
        await authApi.redirectToAsclepiusPortal(asc.token);
        return;
      } catch {
        // Not an Asclepius account, or the wrong password for one — Asclepius
        // and the landing table return the same generic 401 for both cases
        // (no account-enumeration leak), so we can't tell which. Fall through
        // to the landing plane; its own error is still accurate either way.
      }
      await login(trimmedEmail, password);
      resetAndClose();
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Sign in failed";
      const fallbackSlug = parseTenantSlugFromError(msg);
      if (fallbackSlug) {
        try {
          const data = await authApi.tenantLogin(fallbackSlug, trimmedEmail, password);
          if (!data.access_token) {
            throw new Error("Could not open doctor portal.");
          }
          resetAndClose();
          await authApi.redirectToDoctorPortal(data.access_token);
          return;
        } catch (tenantErr) {
          const tenantMsg = tenantErr instanceof Error ? tenantErr.message : "Sign in failed";
          setApiError(tenantMsg);
          return;
        }
      }
      setApiError(msg);
    } finally {
      setSubmitting(false);
    }
  };

  if (!open) return null;

  const modal = (
    <div
      className="auth-modal-overlay adg-scrim"
      role="dialog"
      aria-modal="true"
      aria-labelledby="signin-title"
    >
      <style>{authDialogStyles}</style>
      <div className="adg-panel">
        <div className="adg-body">
          <div className="adg-head">
            <div>
              <h2 id="signin-title" className="adg-title">
                {step === "role" && "Sign in"}
                {step === "doctor" && "Physician sign in"}
              </h2>
              <p className="adg-sub">
                {step === "role" && "Which one are you?"}
                {step === "doctor" && "Sign in with your account to access the physician dashboard."}
              </p>
              {step === "doctor" && authApi.signInServerHost() && (
                <p className="adg-chrome adg-server">Server · {authApi.signInServerHost()}</p>
              )}
            </div>
            <button type="button" onClick={resetAndClose} className="adg-close" aria-label="Close">
              ×
            </button>
          </div>

          {(error || apiError) && (
            <p className="adg-error" role="alert">
              <span className="adg-dot adg-dot-pink" aria-hidden="true" />
              <span>{apiError || error}</span>
            </p>
          )}

          {step === "role" && (
            <div className="adg-form">
              <div className="adg-roles">
                <button type="button" className="adg-role" onClick={() => setStep("doctor")}>
                  <span className="adg-role-for">
                    <span className="adg-dot adg-dot-green" aria-hidden="true" />
                    <span className="adg-chrome">Credentialed</span>
                  </span>
                  <span className="adg-role-title">Physician</span>
                  <span className="adg-role-sub">
                    Label, review, advise &mdash; paid clinical AI work
                  </span>
                </button>
                {/* Sentence case, and it names the work rather than the app:
                    the person clicking this runs a data platform team and has
                    never heard of us. */}
                <button
                  type="button"
                  className="adg-role"
                  onClick={() => window.location.assign(authApi.healthSystemPortalUrl())}
                >
                  <span className="adg-role-for">
                    <span className="adg-dot adg-dot-faint" aria-hidden="true" />
                    <span className="adg-chrome">Organization</span>
                  </span>
                  <span className="adg-role-title">Health system / organization</span>
                  <span className="adg-role-sub">
                    Contribute clinical data for task creation and licensing
                  </span>
                </button>
              </div>
              <div className="adg-actions">
                <button type="button" className="adg-btn adg-btn-secondary" onClick={resetAndClose}>
                  Cancel
                </button>
              </div>
            </div>
          )}

          {step === "doctor" && (
            <form onSubmit={handleDoctorSubmit} className="adg-form">
              <div className="adg-field">
                <label className="adg-label" htmlFor="signin-email">Email</label>
                <input
                  id="signin-email"
                  className="adg-input"
                  type="email"
                  placeholder="you@example.com"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  required
                  autoComplete="email"
                />
              </div>
              <div className="adg-field">
                <label className="adg-label" htmlFor="signin-password">Password</label>
                <input
                  id="signin-password"
                  className="adg-input"
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  required
                  autoComplete="current-password"
                />
                {/* The endpoint answers identically for a known and an unknown
                    address, so this is safe to offer without knowing which
                    plane the email belongs to. */}
                <button
                  type="button"
                  className="adg-linkish"
                  style={{ marginTop: 8 }}
                  onClick={async () => {
                    const addr = email.trim();
                    // setApiError, not setError: there is no setError in this
                    // component, so this line threw a ReferenceError and the
                    // button did nothing for anyone who clicked it with the
                    // email field empty. esbuild strips types without checking
                    // them, which is why a build never caught it.
                    if (!addr) { setApiError("Enter your email above first."); return; }
                    const { message } = await authApi.asclepiusForgotPassword(addr);
                    setApiError(null);
                    setNotice(message);
                  }}
                >
                  Forgot password?
                </button>
              </div>
              {notice ? <div className="adg-notice">{notice}</div> : null}
              <div className="adg-actions">
                <button type="button" className="adg-btn adg-btn-secondary" onClick={() => setStep("role")}>
                  Back
                </button>
                <button type="submit" className="adg-btn adg-btn-primary" disabled={submitting}>
                  {submitting ? "Signing in…" : "Sign in"}
                </button>
              </div>
            </form>
          )}

        </div>
      </div>
    </div>
  );

  return createPortal(modal, document.body);
}
