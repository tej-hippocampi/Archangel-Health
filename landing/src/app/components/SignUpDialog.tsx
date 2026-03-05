"use client";

import * as React from "react";
import { Button } from "@/app/components/ui/button";
import { Input } from "@/app/components/ui/input";
import { Label } from "@/app/components/ui/label";
import { useAuth } from "@/contexts/AuthContext";
import * as authApi from "@/lib/auth-api";

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
      const env = (import.meta as unknown as { env: { VITE_DASHBOARD_URL?: string; VITE_API_URL?: string; DEV?: boolean } }).env;
      const dashboardUrl =
        env?.VITE_DASHBOARD_URL ?? env?.VITE_API_URL ?? (env?.DEV ? "http://localhost:8000" : "");
      if (dashboardUrl && token) {
        window.location.href = dashboardUrl + "#auth=" + encodeURIComponent(token);
      }
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
      className="auth-modal-overlay fixed inset-0 z-[9999] flex items-center justify-center bg-black/60 p-4"
      style={{ position: "fixed", zIndex: 9999 }}
      role="dialog"
      aria-modal="true"
      aria-labelledby="signup-title"
    >
      <div
        className="relative z-10 w-full max-w-md rounded-2xl border border-[rgba(255,255,255,0.12)] bg-[#111118] shadow-[0_0_40px_rgba(0,0,0,0.8)] flex flex-col max-h-[90vh] overflow-hidden"
        onClick={(e) => e.stopPropagation()}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="flex-none px-6 pt-6 pb-2">
        <div className="mb-4 flex items-center justify-between">
          <div>
            <h2 id="signup-title" className="text-lg font-semibold text-[#f5f5f7]">
              {step === "role" && "Sign up"}
              {step === "register" && "Create account"}
              {step === "doctor-onboard" && "Doctor onboarding"}
              {step === "patient-codes" && "View your recovery plan"}
            </h2>
            <p className="mt-1 text-sm text-[#a5a5aa]">
              {step === "role" && "Are you a patient or a doctor?"}
              {step === "register" && "Create your Archangel Health account."}
              {step === "doctor-onboard" && "Tell us about your practice."}
              {step === "patient-codes" && "Enter the codes from your care team email."}
            </p>
          </div>
          <button
            type="button"
            onClick={resetAndClose}
            className="inline-flex size-8 items-center justify-center rounded-full border border-white/10 text-[#f5f5f7]/80 hover:bg-white/10"
          >
            ×
          </button>
        </div>
        </div>

        <div className="flex-1 overflow-y-auto px-6 pb-6 min-h-0">
        {(error || apiError) && (
          <p
            className="mb-4 rounded-md border border-[#ff3b30]/40 bg-[#2b1413] px-3 py-2 text-sm text-[#ffb3aa]"
            role="alert"
          >
            {apiError || error}
          </p>
        )}

        {/* Step: Choose role */}
        {step === "role" && (
          <div className="grid gap-4">
            <div className="grid grid-cols-2 gap-3">
              <Button
                type="button"
                variant="outline"
                className="h-auto flex-col gap-2 py-6 border-[rgba(255,255,255,0.25)] text-[#f5f5f7] hover:bg-white/10"
                onClick={() => setStep("patient-codes")}
              >
                <span className="text-2xl">👤</span>
                <span className="font-semibold">Patient</span>
                <span className="text-xs font-normal text-[#a5a5aa]">I have a clinic & resource code</span>
              </Button>
              <Button
                type="button"
                variant="outline"
                className="h-auto flex-col gap-2 py-6 border-[rgba(255,255,255,0.25)] text-[#f5f5f7] hover:bg-white/10"
                onClick={() => setStep("register")}
              >
                <span className="text-2xl">👨‍⚕️</span>
                <span className="font-semibold">Doctor</span>
                <span className="text-xs font-normal text-[#a5a5aa]">I provide care</span>
              </Button>
            </div>
            <div className="flex justify-end">
              <Button type="button" variant="outline" onClick={resetAndClose} className="border-[rgba(255,255,255,0.25)]">
                Cancel
              </Button>
            </div>
          </div>
        )}

        {/* Step: Register (doctor) */}
        {step === "register" && (
          <form onSubmit={handleRegister} className="grid gap-4">
            <div className="grid gap-2">
              <Label htmlFor="signup-email">Email</Label>
              <Input
                id="signup-email"
                type="email"
                placeholder="you@example.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
                autoComplete="email"
                className="bg-[#0a0a0b] border-[rgba(255,255,255,0.16)] text-[#f5f5f7] placeholder:text-[#6a6a70]"
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="signup-name">Name (optional)</Label>
              <Input
                id="signup-name"
                type="text"
                placeholder="Your name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                autoComplete="name"
                className="bg-[#0a0a0b] border-[rgba(255,255,255,0.16)] text-[#f5f5f7] placeholder:text-[#6a6a70]"
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="signup-password">Password</Label>
              <Input
                id="signup-password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                minLength={8}
                autoComplete="new-password"
                className="bg-[#0a0a0b] border-[rgba(255,255,255,0.16)] text-[#f5f5f7] placeholder:text-[#6a6a70]"
              />
            </div>
            <div className="mt-4 flex items-center justify-end gap-3">
              <Button type="button" variant="outline" onClick={() => setStep("role")} className="border-[rgba(255,255,255,0.25)]">
                Back
              </Button>
              <Button type="submit" disabled={submitting}>
                {submitting ? "Creating account…" : "Create account"}
              </Button>
            </div>
          </form>
        )}

        {/* Step: Doctor onboarding */}
        {step === "doctor-onboard" && (
          <form onSubmit={handleDoctorOnboard} className="grid gap-4">
            <div className="grid gap-2">
              <Label htmlFor="onboard-name">Name</Label>
              <Input
                id="onboard-name"
                type="text"
                placeholder="Dr. Jane Smith"
                value={name}
                onChange={(e) => setName(e.target.value)}
                required
                className="bg-[#0a0a0b] border-[rgba(255,255,255,0.16)] text-[#f5f5f7] placeholder:text-[#6a6a70]"
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="onboard-email">Email</Label>
              <Input
                id="onboard-email"
                type="email"
                value={email}
                readOnly
                className="bg-[#0a0a0b] border-[rgba(255,255,255,0.16)] text-[#f5f5f7] opacity-80"
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="onboard-phone">Office phone</Label>
              <Input
                id="onboard-phone"
                type="tel"
                placeholder="+1 (555) 123-4567"
                value={officePhone}
                onChange={(e) => setOfficePhone(e.target.value)}
                required
                className="bg-[#0a0a0b] border-[rgba(255,255,255,0.16)] text-[#f5f5f7] placeholder:text-[#6a6a70]"
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="onboard-type">Type of doctor</Label>
              <Input
                id="onboard-type"
                type="text"
                placeholder="e.g. Surgeon, Oncologist, PCP"
                value={doctorType}
                onChange={(e) => setDoctorType(e.target.value)}
                required
                className="bg-[#0a0a0b] border-[rgba(255,255,255,0.16)] text-[#f5f5f7] placeholder:text-[#6a6a70]"
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="onboard-affiliations">Hospital affiliations</Label>
              <Input
                id="onboard-affiliations"
                type="text"
                placeholder="e.g. Memorial Hospital, City Medical Center"
                value={hospitalAffiliations}
                onChange={(e) => setHospitalAffiliations(e.target.value)}
                className="bg-[#0a0a0b] border-[rgba(255,255,255,0.16)] text-[#f5f5f7] placeholder:text-[#6a6a70]"
              />
            </div>
            <div className="mt-4 flex items-center justify-end gap-3">
              <Button type="button" variant="outline" onClick={() => setStep("register")} className="border-[rgba(255,255,255,0.25)]">
                Back
              </Button>
              <Button type="submit" disabled={submitting}>
                {submitting ? "Saving…" : "Complete setup"}
              </Button>
            </div>
          </form>
        )}

        {/* Step: Patient codes */}
        {step === "patient-codes" && (
          <form onSubmit={handlePatientCodes} className="grid gap-4">
            <div className="grid gap-2">
              <Label htmlFor="patient-clinic-code">Clinic code</Label>
              <Input
                id="patient-clinic-code"
                type="text"
                placeholder="From your email"
                value={clinicCode}
                onChange={(e) => setClinicCode(e.target.value.toUpperCase())}
                required
                className="bg-[#0a0a0b] border-[rgba(255,255,255,0.16)] font-mono tracking-wider text-[#f5f5f7] placeholder:text-[#6a6a70]"
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="patient-resource-code">Resource code</Label>
              <Input
                id="patient-resource-code"
                type="text"
                placeholder="From your email"
                value={resourceCode}
                onChange={(e) => setResourceCode(e.target.value.toUpperCase())}
                required
                className="bg-[#0a0a0b] border-[rgba(255,255,255,0.16)] font-mono tracking-wider text-[#f5f5f7] placeholder:text-[#6a6a70]"
              />
            </div>
            <div className="mt-4 flex items-center justify-end gap-3">
              <Button type="button" variant="outline" onClick={() => setStep("role")} className="border-[rgba(255,255,255,0.25)]">
                Back
              </Button>
              <Button type="submit" disabled={submitting}>
                {submitting ? "Loading…" : "View recovery plan"}
              </Button>
            </div>
          </form>
        )}
        </div>
      </div>
    </div>
  );

  return modal;
}
