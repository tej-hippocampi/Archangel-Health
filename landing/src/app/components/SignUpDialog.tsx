"use client";

import * as React from "react";
import { createPortal } from "react-dom";
import { useAuth } from "@/contexts/AuthContext";
import * as authApi from "@/lib/auth-api";
import { authDialogStyles } from "./authDialogStyles";

/**
 * The patient CARD is gone from the role screen (PRD §1) but the
 * `patient-codes` STEP stays: care-team emails already in inboxes deep-link to
 * `/#recovery-plan`, which opens this dialog straight at that step. Deleting
 * the step would break a live URL to fix a layout problem.
 */
type Step =
  | "role"
  | "register"
  | "verify-email"
  | "doctor-onboard"
  | "patient-codes"
  | "org"
  | "org-verify"
  | "org-done";

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  initialStep?: "role" | "patient-codes";
};

export function SignUpDialog({ open, onOpenChange, initialStep = "role" }: Props) {
  const { register, verifyEmail, resendVerification, error, clearError, token } = useAuth();
  const [step, setStep] = React.useState<Step>(initialStep);

  // When dialog opens, show the requested step (e.g. patient-codes when coming from email link)
  React.useEffect(() => {
    if (open) setStep(initialStep);
  }, [open, initialStep]);
  const [email, setEmail] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [name, setName] = React.useState("");
  const [signupPhone, setSignupPhone] = React.useState("");
  const [smsConsentOptIn, setSmsConsentOptIn] = React.useState(false);
  const [verifyCode, setVerifyCode] = React.useState("");
  const [resendStatus, setResendStatus] = React.useState<string | null>(null);
  const [officePhone, setOfficePhone] = React.useState("");
  const [doctorType, setDoctorType] = React.useState("");
  const [hospitalAffiliations, setHospitalAffiliations] = React.useState("");
  const [clinicCode, setClinicCode] = React.useState("");
  const [resourceCode, setResourceCode] = React.useState("");
  // ─ Health system / organization: three fields and a code (PRD §2) ─
  const [orgName, setOrgName] = React.useState("");
  const [orgContact, setOrgContact] = React.useState("");
  const [orgEmail, setOrgEmail] = React.useState("");
  const [orgCode, setOrgCode] = React.useState("");
  const [orgHoneypot, setOrgHoneypot] = React.useState("");
  const [orgUsername, setOrgUsername] = React.useState("");
  const [submitting, setSubmitting] = React.useState(false);
  const [apiError, setApiError] = React.useState<string | null>(null);

  const resetAndClose = () => {
    setStep("role");
    setEmail("");
    setPassword("");
    setName("");
    setSignupPhone("");
    setSmsConsentOptIn(false);
    setVerifyCode("");
    setResendStatus(null);
    setOfficePhone("");
    setDoctorType("");
    setHospitalAffiliations("");
    setClinicCode("");
    setResourceCode("");
    setOrgName("");
    setOrgContact("");
    setOrgEmail("");
    setOrgCode("");
    setOrgHoneypot("");
    setOrgUsername("");
    setApiError(null);
    clearError();
    onOpenChange(false);
  };

  const handleRegister = async (e: React.FormEvent) => {
    e.preventDefault();
    clearError();
    setSubmitting(true);
    try {
      const user = await register(email, password, name || undefined, signupPhone || undefined, smsConsentOptIn);
      setStep(user.email_verified ? "doctor-onboard" : "verify-email");
      setOfficePhone("");
      setDoctorType("");
      setHospitalAffiliations("");
    } catch {
      // error set in context
    } finally {
      setSubmitting(false);
    }
  };

  const handleVerifyEmail = async (e: React.FormEvent) => {
    e.preventDefault();
    setApiError(null);
    setResendStatus(null);
    setSubmitting(true);
    try {
      await verifyEmail(email, verifyCode);
      setStep("doctor-onboard");
    } catch (e) {
      setApiError(e instanceof Error ? e.message : "Verification failed");
    } finally {
      setSubmitting(false);
    }
  };

  const handleResendVerification = async () => {
    setApiError(null);
    setResendStatus(null);
    try {
      await resendVerification(email);
      setResendStatus("A new code was sent to your email.");
    } catch (e) {
      setApiError(e instanceof Error ? e.message : "Could not resend the code");
    }
  };

  const handleDoctorOnboard = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!token) return;
    setApiError(null);
    clearError();
    setSubmitting(true);
    try {
      await authApi.doctorOnboard(token, {
        name: name.trim() || email,
        email,
        office_phone: officePhone,
        doctor_type: doctorType,
        hospital_affiliations: hospitalAffiliations,
      });
      resetAndClose();
      if (token) await authApi.redirectToDoctorPortal(token);
    } catch (e) {
      setApiError(e instanceof Error ? e.message : "Onboarding failed");
    } finally {
      setSubmitting(false);
    }
  };

  const handlePatientCodes = async (e: React.FormEvent) => {
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

  const handleOrgSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setApiError(null);
    clearError();
    setSubmitting(true);
    try {
      await authApi.healthSystemSignup({
        fullName: orgContact.trim(),
        email: orgEmail.trim().toLowerCase(),
        organization: orgName.trim(),
        companyWebsite: orgHoneypot,
      });
      setStep("org-verify");
    } catch (err) {
      setApiError(err instanceof Error ? err.message : "Something went wrong.");
    } finally {
      setSubmitting(false);
    }
  };

  const handleOrgVerify = async (e: React.FormEvent) => {
    e.preventDefault();
    setApiError(null);
    setSubmitting(true);
    try {
      const result = await authApi.healthSystemVerify(
        orgEmail.trim().toLowerCase(),
        orgCode.trim(),
      );
      setOrgUsername(result.username);
      // A beat before the redirect, not a straight jump. The username was
      // derived from their organization name and they have never seen it; if
      // the session cookie does not survive the hop to the portal (a different
      // host in some deployments) this screen is the only place they can read
      // it before their email arrives.
      setStep("org-done");
      window.setTimeout(() => {
        window.location.assign(authApi.healthSystemPortalUrl());
      }, 2200);
    } catch (err) {
      setApiError(err instanceof Error ? err.message : "That code is not right.");
      setSubmitting(false);
    }
  };

  if (!open) return null;

  const modal = (
    <div
      className="auth-modal-overlay adg-scrim"
      role="dialog"
      aria-modal="true"
      aria-labelledby="signup-title"
    >
      <style>{authDialogStyles}</style>
      <div
        className="adg-panel"
        onClick={(e) => e.stopPropagation()}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="adg-body">
        <div className="adg-head">
          <div>
            <h2 id="signup-title" className="adg-title">
              {step === "role" && "Sign up"}
              {step === "register" && "Create account"}
              {step === "verify-email" && "Verify your email"}
              {step === "doctor-onboard" && "Doctor onboarding"}
              {step === "patient-codes" && "View your recovery plan"}
              {step === "org" && "Health system sign-up"}
              {step === "org-verify" && "Confirm your email"}
              {step === "org-done" && "You are in"}
            </h2>
            <p className="adg-sub">
              {step === "role" && "Which one are you?"}
              {step === "register" && "Create your Archangel Health account."}
              {step === "verify-email" && `We sent a code and a link to ${email || "your email"}.`}
              {step === "doctor-onboard" && "Tell us about your practice."}
              {step === "patient-codes" && "Enter the codes from your care team email."}
              {step === "org" &&
                "Three things, and we will take you straight into your portal."}
              {step === "org-verify" &&
                `We sent a six-digit code to ${orgEmail || "your email"}.`}
              {step === "org-done" && "Taking you to your portal."}
            </p>
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

        {/* Step: Choose role */}
        {step === "role" && (
          <div className="adg-form">
            <div className="adg-roles">
              <button type="button" className="adg-role" onClick={() => setStep("register")}>
                <span className="adg-role-for">
                  <span className="adg-dot adg-dot-green" aria-hidden="true" />
                  <span className="adg-chrome">Credentialed</span>
                </span>
                <span className="adg-role-title">Physician</span>
                <span className="adg-role-sub">
                  Label, review, advise &mdash; paid clinical AI work
                </span>
              </button>
              <button type="button" className="adg-role" onClick={() => setStep("org")}>
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

        {/* Step: Register (doctor) */}
        {step === "register" && (
          <form onSubmit={handleRegister} className="adg-form">
            <div className="adg-field">
              <label className="adg-label" htmlFor="signup-email">Email</label>
              <input
                id="signup-email"
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
              <label className="adg-label" htmlFor="signup-name">Name (optional)</label>
              <input
                id="signup-name"
                className="adg-input"
                type="text"
                placeholder="Your name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                autoComplete="name"
              />
            </div>
            <div className="adg-field">
              <label className="adg-label" htmlFor="signup-password">Password</label>
              <input
                id="signup-password"
                className="adg-input"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                minLength={8}
                autoComplete="new-password"
              />
            </div>
            <div className="adg-field">
              <label className="adg-label" htmlFor="signup-phone">Mobile phone (optional)</label>
              <input
                id="signup-phone"
                className="adg-input"
                type="tel"
                placeholder="+1 (555) 123-4567"
                value={signupPhone}
                onChange={(e) => setSignupPhone(e.target.value)}
                autoComplete="tel"
              />
            </div>
            <label className="adg-checkbox-row" htmlFor="signup-sms-consent">
              <input
                id="signup-sms-consent"
                type="checkbox"
                checked={smsConsentOptIn}
                onChange={(e) => setSmsConsentOptIn(e.target.checked)}
                disabled={!signupPhone}
              />
              <span>Text me when a task needs my attention (optional).</span>
            </label>
            <div className="adg-actions">
              <button type="button" className="adg-btn adg-btn-secondary" onClick={() => setStep("role")}>
                Back
              </button>
              <button type="submit" className="adg-btn adg-btn-primary" disabled={submitting}>
                {submitting ? "Creating account…" : "Create account"}
              </button>
            </div>
          </form>
        )}

        {/* Step: Verify email */}
        {step === "verify-email" && (
          <form onSubmit={handleVerifyEmail} className="adg-form">
            <div className="adg-field">
              <label className="adg-label" htmlFor="verify-code">Verification code</label>
              <input
                id="verify-code"
                className="adg-input adg-input-code"
                type="text"
                inputMode="numeric"
                placeholder="6-digit code"
                value={verifyCode}
                onChange={(e) => setVerifyCode(e.target.value)}
                required
                autoComplete="one-time-code"
              />
            </div>
            <p className="adg-sub">
              Or click the link we emailed you to verify instantly.{" "}
              <button type="button" className="adg-link" onClick={handleResendVerification}>
                Resend code
              </button>
            </p>
            {resendStatus && <p className="adg-sub">{resendStatus}</p>}
            <div className="adg-actions">
              <button type="button" className="adg-btn adg-btn-secondary" onClick={() => setStep("register")}>
                Back
              </button>
              <button type="submit" className="adg-btn adg-btn-primary" disabled={submitting}>
                {submitting ? "Verifying…" : "Verify email"}
              </button>
            </div>
          </form>
        )}

        {/* Step: Doctor onboarding */}
        {step === "doctor-onboard" && (
          <form onSubmit={handleDoctorOnboard} className="adg-form">
            <div className="adg-field">
              <label className="adg-label" htmlFor="onboard-name">Name</label>
              <input
                id="onboard-name"
                className="adg-input"
                type="text"
                placeholder="Dr. Jane Smith"
                value={name}
                onChange={(e) => setName(e.target.value)}
                required
              />
            </div>
            <div className="adg-field">
              <label className="adg-label" htmlFor="onboard-email">Email</label>
              <input
                id="onboard-email"
                className="adg-input"
                type="email"
                value={email}
                readOnly
              />
            </div>
            <div className="adg-field">
              <label className="adg-label" htmlFor="onboard-phone">Office phone</label>
              <input
                id="onboard-phone"
                className="adg-input"
                type="tel"
                placeholder="+1 (555) 123-4567"
                value={officePhone}
                onChange={(e) => setOfficePhone(e.target.value)}
                required
              />
            </div>
            <div className="adg-field">
              <label className="adg-label" htmlFor="onboard-type">Type of doctor</label>
              <input
                id="onboard-type"
                className="adg-input"
                type="text"
                placeholder="e.g. Surgeon, Oncologist, PCP"
                value={doctorType}
                onChange={(e) => setDoctorType(e.target.value)}
                required
              />
            </div>
            <div className="adg-field">
              <label className="adg-label" htmlFor="onboard-affiliations">Hospital affiliations</label>
              <input
                id="onboard-affiliations"
                className="adg-input"
                type="text"
                placeholder="e.g. Memorial Hospital, City Medical Center"
                value={hospitalAffiliations}
                onChange={(e) => setHospitalAffiliations(e.target.value)}
              />
            </div>
            <div className="adg-actions">
              <button type="button" className="adg-btn adg-btn-secondary" onClick={() => setStep("register")}>
                Back
              </button>
              <button type="submit" className="adg-btn adg-btn-primary" disabled={submitting}>
                {submitting ? "Saving…" : "Complete setup"}
              </button>
            </div>
          </form>
        )}

        {/* Step: Patient codes */}
        {step === "patient-codes" && (
          <form onSubmit={handlePatientCodes} className="adg-form">
            <div className="adg-field">
              <label className="adg-label" htmlFor="patient-clinic-code">Health system code</label>
              <input
                id="patient-clinic-code"
                className="adg-input adg-input-code"
                type="text"
                placeholder="From your email"
                value={clinicCode}
                onChange={(e) => setClinicCode(e.target.value.toUpperCase())}
                required
              />
            </div>
            <div className="adg-field">
              <label className="adg-label" htmlFor="patient-resource-code">Resource code</label>
              <input
                id="patient-resource-code"
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

        {/* Step: Health system — three fields, one button (PRD §2).
            No password field. The backend mints a temporary one, mails it, and
            forces its replacement at first sign-in: the same compromise the
            physician onboarding makes, for the same SOC 2 reason. Asking a CIO
            to invent a password before they know what this is costs signups
            and buys nothing. */}
        {step === "org" && (
          <form onSubmit={handleOrgSubmit} className="adg-form">
            <div className="adg-field">
              <label className="adg-label" htmlFor="signup-org-contact">Your name</label>
              <input
                id="signup-org-contact"
                className="adg-input"
                type="text"
                placeholder="Dana Reyes"
                value={orgContact}
                onChange={(e) => setOrgContact(e.target.value)}
                required
                autoComplete="name"
              />
            </div>
            <div className="adg-field">
              <label className="adg-label" htmlFor="signup-org-email">Work email</label>
              <input
                id="signup-org-email"
                className="adg-input"
                type="email"
                placeholder="d.reyes@yourhospital.org"
                value={orgEmail}
                onChange={(e) => setOrgEmail(e.target.value)}
                required
                autoComplete="email"
              />
            </div>
            <div className="adg-field">
              <label className="adg-label" htmlFor="signup-org-name">
                Health system / organization name
              </label>
              <input
                id="signup-org-name"
                className="adg-input"
                type="text"
                placeholder="St Mary&apos;s Health"
                value={orgName}
                onChange={(e) => setOrgName(e.target.value)}
                required
                autoComplete="organization"
              />
            </div>
            {/* Honeypot, mirrored from the contributor modal: never shown to a
                person, always filled by a naive bot. */}
            <input
              type="text"
              name="company_website"
              value={orgHoneypot}
              onChange={(e) => setOrgHoneypot(e.target.value)}
              tabIndex={-1}
              autoComplete="off"
              aria-hidden="true"
              style={{ position: "absolute", left: -9999, width: 1, height: 1, opacity: 0 }}
            />
            <div className="adg-actions">
              <button type="button" className="adg-btn adg-btn-secondary" onClick={() => setStep("role")}>
                Back
              </button>
              <button
                type="submit"
                className="adg-btn adg-btn-primary"
                disabled={submitting || !orgContact.trim() || !orgEmail.trim() || !orgName.trim()}
              >
                {submitting ? "One moment…" : "Continue"}
              </button>
            </div>
          </form>
        )}

        {/* Step: the six digits. */}
        {step === "org-verify" && (
          <form onSubmit={handleOrgVerify} className="adg-form">
            <div className="adg-field">
              <label className="adg-label" htmlFor="signup-org-code">Six-digit code</label>
              <input
                id="signup-org-code"
                className="adg-input adg-input-code"
                type="text"
                inputMode="numeric"
                autoComplete="one-time-code"
                maxLength={6}
                placeholder="000000"
                value={orgCode}
                onChange={(e) => setOrgCode(e.target.value.replace(/\D/g, ""))}
                required
                autoFocus
              />
            </div>
            <button
              type="button"
              className="adg-linkish"
              onClick={() => {
                void authApi.healthSystemResendCode(orgEmail.trim().toLowerCase());
                setApiError(null);
              }}
            >
              Send it again
            </button>
            <div className="adg-actions">
              <button type="button" className="adg-btn adg-btn-secondary" onClick={() => setStep("org")}>
                Back
              </button>
              <button
                type="submit"
                className="adg-btn adg-btn-primary"
                disabled={submitting || orgCode.length !== 6}
              >
                {submitting ? "Confirming…" : "Confirm"}
              </button>
            </div>
          </form>
        )}

        {/* Step: done. The username is on screen because they never chose it. */}
        {step === "org-done" && (
          <div className="adg-form">
            <p className="adg-sub">
              Your portal for <strong>{orgName.trim()}</strong> is open. You sign
              in as <strong>{orgUsername}</strong> — it is in your email too,
              with a temporary password to replace on your first sign-in.
            </p>
            <div className="adg-actions">
              <a
                className="adg-btn adg-btn-primary"
                href={authApi.healthSystemPortalUrl()}
              >
                Open the portal
              </a>
            </div>
          </div>
        )}
        </div>
      </div>
    </div>
  );

  return createPortal(modal, document.body);
}
