"use client";

import * as React from "react";
import { createPortal } from "react-dom";
import { useAuth } from "@/contexts/AuthContext";
import * as authApi from "@/lib/auth-api";
import { authDialogStyles } from "./authDialogStyles";

type Step = "role" | "doctor" | "patient";

type Props = { open: boolean; onOpenChange: (open: boolean) => void };

export function SignInDialog({ open, onOpenChange }: Props) {
  const { login, error, clearError } = useAuth();
  const [step, setStep] = React.useState<Step>("role");
  const [email, setEmail] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [clinicCode, setClinicCode] = React.useState("");
  const [resourceCode, setResourceCode] = React.useState("");
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
    setClinicCode("");
    setResourceCode("");
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

  const handlePatientSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setApiError(null);
    clearError();
    setSubmitting(true);
    try {
      const data = await authApi.getPatientByCodes(clinicCode, resourceCode);
      resetAndClose();
      window.location.href = data.dashboard_url;
    } catch (e) {
      setApiError(e instanceof Error ? e.message : "Invalid codes");
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
                {step === "doctor" && "Doctor sign in"}
                {step === "patient" && "Access your recovery plan"}
              </h2>
              <p className="adg-sub">
                {step === "role" && "Are you a patient or a doctor?"}
                {step === "doctor" && "Sign in with your account to access the doctor dashboard."}
                {step === "patient" && "Enter the codes from your care team email."}
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
                <button type="button" className="adg-role" onClick={() => setStep("patient")}>
                  <span className="adg-role-for">
                    <span className="adg-dot adg-dot-faint" aria-hidden="true" />
                    <span className="adg-chrome">Access codes</span>
                  </span>
                  <span className="adg-role-title">Patient</span>
                  <span className="adg-role-sub">Health system &amp; resource codes</span>
                </button>
                <button type="button" className="adg-role" onClick={() => setStep("doctor")}>
                  <span className="adg-role-for">
                    <span className="adg-dot adg-dot-green" aria-hidden="true" />
                    <span className="adg-chrome">Credentialed</span>
                  </span>
                  <span className="adg-role-title">Doctor</span>
                  <span className="adg-role-sub">Email &amp; password</span>
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
              </div>
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

          {step === "patient" && (
            <form onSubmit={handlePatientSubmit} className="adg-form">
              <div className="adg-field">
                <label className="adg-label" htmlFor="signin-clinic-code">Health system code</label>
                <input
                  id="signin-clinic-code"
                  className="adg-input adg-input-code"
                  type="text"
                  placeholder="From your email"
                  value={clinicCode}
                  onChange={(e) => setClinicCode(e.target.value.toUpperCase())}
                  required
                />
              </div>
              <div className="adg-field">
                <label className="adg-label" htmlFor="signin-resource-code">Resource code</label>
                <input
                  id="signin-resource-code"
                  className="adg-input adg-input-code"
                  type="text"
                  placeholder="From your email"
                  value={resourceCode}
                  onChange={(e) => setResourceCode(e.target.value.toUpperCase())}
                  required
                />
              </div>
              <div className="adg-actions">
                <button type="button" className="adg-btn adg-btn-secondary" onClick={() => setStep("role")}>
                  Back
                </button>
                <button type="submit" className="adg-btn adg-btn-primary" disabled={submitting}>
                  {submitting ? "Loading…" : "View recovery plan"}
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
