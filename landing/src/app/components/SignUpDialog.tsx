"use client";

import * as React from "react";
import { createPortal } from "react-dom";
import { useAuth } from "@/contexts/AuthContext";
import * as authApi from "@/lib/auth-api";
import { authDialogStyles } from "./authDialogStyles";

type Step = "role" | "register" | "doctor-onboard" | "patient-codes";

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  initialStep?: "role" | "patient-codes";
};

export function SignUpDialog({ open, onOpenChange, initialStep = "role" }: Props) {
  const { register, error, clearError, token } = useAuth();
  const [step, setStep] = React.useState<Step>(initialStep);

  // When dialog opens, show the requested step (e.g. patient-codes when coming from email link)
  React.useEffect(() => {
    if (open) setStep(initialStep);
  }, [open, initialStep]);
  const [email, setEmail] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [name, setName] = React.useState("");
  const [officePhone, setOfficePhone] = React.useState("");
  const [doctorType, setDoctorType] = React.useState("");
  const [hospitalAffiliations, setHospitalAffiliations] = React.useState("");
  const [clinicCode, setClinicCode] = React.useState("");
  const [resourceCode, setResourceCode] = React.useState("");
  const [submitting, setSubmitting] = React.useState(false);
  const [apiError, setApiError] = React.useState<string | null>(null);

  const resetAndClose = () => {
    setStep("role");
    setEmail("");
    setPassword("");
    setName("");
    setOfficePhone("");
    setDoctorType("");
    setHospitalAffiliations("");
    setClinicCode("");
    setResourceCode("");
    setApiError(null);
    clearError();
    onOpenChange(false);
  };

  const handleRegister = async (e: React.FormEvent) => {
    e.preventDefault();
    clearError();
    setSubmitting(true);
    try {
      await register(email, password, name || undefined);
      setStep("doctor-onboard");
      setOfficePhone("");
      setDoctorType("");
      setHospitalAffiliations("");
    } catch {
      // error set in context
    } finally {
      setSubmitting(false);
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
              {step === "doctor-onboard" && "Doctor onboarding"}
              {step === "patient-codes" && "View your recovery plan"}
            </h2>
            <p className="adg-sub">
              {step === "role" && "Are you a patient or a doctor?"}
              {step === "register" && "Create your Archangel Health account."}
              {step === "doctor-onboard" && "Tell us about your practice."}
              {step === "patient-codes" && "Enter the codes from your care team email."}
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
              <button type="button" className="adg-role" onClick={() => setStep("patient-codes")}>
                <span className="adg-role-for">
                  <span className="adg-dot adg-dot-faint" aria-hidden="true" />
                  <span className="adg-chrome">Access codes</span>
                </span>
                <span className="adg-role-title">Patient</span>
                <span className="adg-role-sub">Health system &amp; resource codes</span>
              </button>
              <button type="button" className="adg-role" onClick={() => setStep("register")}>
                <span className="adg-role-for">
                  <span className="adg-dot adg-dot-green" aria-hidden="true" />
                  <span className="adg-chrome">Credentialed</span>
                </span>
                <span className="adg-role-title">Doctor</span>
                <span className="adg-role-sub">I provide care</span>
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
        </div>
      </div>
    </div>
  );

  return createPortal(modal, document.body);
}
