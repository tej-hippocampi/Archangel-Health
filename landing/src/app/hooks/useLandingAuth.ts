/**
 * Shared auth wiring for the landing's top-level entry pages (SiteHeader and the
 * clinical-data home nav). Both surfaces must honor the same backend-owned URL
 * contracts — `?signout=1` / `/auth/signout` (backend redirects sign-out here),
 * `/#recovery-plan` (patient care-team emails link here), and the doctor-portal
 * auto-redirect for signed-in doctors — so the behavior lives in one place
 * rather than being copy-pasted per header, where the copies drift.
 */

import { useCallback, useEffect, useState } from "react";
import { useAuth } from "@/contexts/AuthContext";
import * as authApi from "@/lib/auth-api";

export type SignUpStep = "role" | "patient-codes";

export function useLandingAuth() {
  const { user, loading, logout, token } = useAuth();
  const [signInOpen, setSignInOpen] = useState(false);
  const [signUpOpen, setSignUpOpen] = useState(false);
  const [signUpInitialStep, setSignUpInitialStep] = useState<SignUpStep>("role");

  // Signed-in doctors are handed off to the doctor portal. getDoctorProfile
  // rejects on network/CORS failure and redirectToDoctorPortal throws when the
  // portal URL/handoff is unavailable; swallow both so an unreachable backend
  // just leaves the visitor on the landing instead of an unhandled rejection.
  useEffect(() => {
    if (!user || !token) return;
    let cancelled = false;
    authApi
      .getDoctorProfile(token)
      .then((profile) => {
        if (!cancelled && profile) {
          return authApi.redirectToDoctorPortal(token);
        }
      })
      .catch(() => {
        /* backend unreachable / no portal handoff — stay on the landing */
      });
    return () => {
      cancelled = true;
    };
  }, [user, token]);

  // Sign-out: backend redirects to `{landing}?signout=1` (or `/auth/signout`).
  useEffect(() => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    const isSignoutQuery = params.get("signout") === "1";
    const isSignoutPath = window.location.pathname === "/auth/signout";
    if (isSignoutQuery || isSignoutPath) {
      logout();
      params.delete("signout");
      const newSearch = params.toString();
      const newUrl =
        (isSignoutPath ? "/" : window.location.pathname) +
        (newSearch ? "?" + newSearch : "") +
        window.location.hash;
      window.history.replaceState(null, "", newUrl);
    }
  }, [logout]);

  // Patient care-team emails deep-link to `/#recovery-plan` to open the sign-up
  // wizard straight at the resource-code step.
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (window.location.hash === "#recovery-plan") {
      setSignUpInitialStep("patient-codes");
      setSignUpOpen(true);
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
    }
  }, []);

  const openSignUp = useCallback((step: SignUpStep = "role") => {
    setSignUpInitialStep(step);
    setSignUpOpen(true);
  }, []);

  // Empty when the landing is served from the backend origin (calls are
  // same-origin) — callers hide the portal link rather than render href="".
  const doctorPortalUrl = authApi.doctorAppUrl();

  // "Firstname Lastname" for the signed-in portal link, or a stable fallback.
  const doctorPortalLabel =
    user?.name && user.name.trim() ? user.name.trim().split(/\s+/).slice(0, 2).join(" ") : "Doctor Portal";

  return {
    user,
    loading,
    logout,
    token,
    signInOpen,
    setSignInOpen,
    signUpOpen,
    setSignUpOpen,
    signUpInitialStep,
    openSignUp,
    doctorPortalUrl,
    doctorPortalLabel,
  };
}
