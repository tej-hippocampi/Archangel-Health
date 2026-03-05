"use client";

import * as React from "react";
import { createPortal } from "react-dom";
import { Button } from "@/app/components/ui/button";
import { Input } from "@/app/components/ui/input";
import { Label } from "@/app/components/ui/label";
import { useAuth } from "@/contexts/AuthContext";
import * as authApi from "@/lib/auth-api";

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
    try {
      await login(email, password);
      resetAndClose();
    } catch {
      // error set in context
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
      className="auth-modal-overlay fixed inset-0 z-[9999] flex items-center justify-center bg-black/60"
      style={{ position: "fixed", zIndex: 9999 }}
      role="dialog"
      aria-modal="true"
      aria-labelledby="signin-title"
    >
      <div className="w-full max-w-md rounded-2xl border border-[rgba(255,255,255,0.12)] bg-[#111118] px-6 py-6 shadow-[0_0_40px_rgba(0,0,0,0.8)]">
        <div className="mb-4 flex items-center justify-between">
          <div>
            <h2 id="signin-title" className="text-lg font-semibold text-[#f5f5f7]">
              {step === "role" && "Sign in"}
              {step === "doctor" && "Doctor sign in"}
              {step === "patient" && "Access your recovery plan"}
            </h2>
            <p className="mt-1 text-sm text-[#a5a5aa]">
              {step === "role" && "Are you a patient or a doctor?"}
              {step === "doctor" && "Sign in with your account to access the doctor dashboard."}
              {step === "patient" && "Enter the codes from your care team email."}
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

        {(error || apiError) && (
          <p
            className="mb-4 rounded-md border border-[#ff3b30]/40 bg-[#2b1413] px-3 py-2 text-sm text-[#ffb3aa]"
            role="alert"
          >
            {apiError || error}
          </p>
        )}

        {step === "role" && (
          <div className="grid gap-4">
            <div className="grid grid-cols-2 gap-3">
              <Button
                type="button"
                variant="outline"
                className="h-auto flex-col gap-2 py-6 border-[rgba(255,255,255,0.25)] text-[#f5f5f7] hover:bg-white/10"
                onClick={() => setStep("patient")}
              >
                <span className="text-2xl">👤</span>
                <span className="font-semibold">Patient</span>
                <span className="text-xs font-normal text-[#a5a5aa]">I have clinic & resource codes</span>
              </Button>
              <Button
                type="button"
                variant="outline"
                className="h-auto flex-col gap-2 py-6 border-[rgba(255,255,255,0.25)] text-[#f5f5f7] hover:bg-white/10"
                onClick={() => setStep("doctor")}
              >
                <span className="text-2xl">👨‍⚕️</span>
                <span className="font-semibold">Doctor</span>
                <span className="text-xs font-normal text-[#a5a5aa]">Email & password</span>
              </Button>
            </div>
            <div className="flex justify-end">
              <Button type="button" variant="outline" onClick={resetAndClose} className="border-[rgba(255,255,255,0.25)]">
                Cancel
              </Button>
            </div>
          </div>
        )}

        {step === "doctor" && (
          <form onSubmit={handleDoctorSubmit} className="grid gap-4">
            <div className="grid gap-2">
              <Label htmlFor="signin-email">Email</Label>
              <Input
                id="signin-email"
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
              <Label htmlFor="signin-password">Password</Label>
              <Input
                id="signin-password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                autoComplete="current-password"
                className="bg-[#0a0a0b] border-[rgba(255,255,255,0.16)] text-[#f5f5f7] placeholder:text-[#6a6a70]"
              />
            </div>
            <div className="mt-4 flex items-center justify-end gap-3">
              <Button
                type="button"
                variant="outline"
                onClick={() => setStep("role")}
                className="border-[rgba(255,255,255,0.25)]"
              >
                Back
              </Button>
              <Button type="submit" disabled={submitting}>
                {submitting ? "Signing in…" : "Sign in"}
              </Button>
            </div>
          </form>
        )}

        {step === "patient" && (
          <form onSubmit={handlePatientSubmit} className="grid gap-4">
            <div className="grid gap-2">
              <Label htmlFor="signin-clinic-code">Clinic code</Label>
              <Input
                id="signin-clinic-code"
                type="text"
                placeholder="From your email"
                value={clinicCode}
                onChange={(e) => setClinicCode(e.target.value.toUpperCase())}
                required
                className="bg-[#0a0a0b] border-[rgba(255,255,255,0.16)] font-mono tracking-wider text-[#f5f5f7] placeholder:text-[#6a6a70]"
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="signin-resource-code">Resource code</Label>
              <Input
                id="signin-resource-code"
                type="text"
                placeholder="From your email"
                value={resourceCode}
                onChange={(e) => setResourceCode(e.target.value.toUpperCase())}
                required
                className="bg-[#0a0a0b] border-[rgba(255,255,255,0.16)] font-mono tracking-wider text-[#f5f5f7] placeholder:text-[#6a6a70]"
              />
            </div>
            <div className="mt-4 flex items-center justify-end gap-3">
              <Button
                type="button"
                variant="outline"
                onClick={() => setStep("role")}
                className="border-[rgba(255,255,255,0.25)]"
              >
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
  );

  return createPortal(modal, document.body);
}
