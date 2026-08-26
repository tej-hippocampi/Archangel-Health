import { useMemo, useState } from "react";
import OnboardingStyles from "./onboarding/OnboardingStyles";
import {
  ChromeHeader,
  InlineError,
  OnboardingCard,
  PrimaryButton,
  TextField,
} from "./onboarding/primitives";
import * as authApi from "@/lib/auth-api";

/**
 * /join — the shareable get-started entry.
 *
 * One page, one purpose: whoever holds this link starts onboarding without an
 * intermediate email-entry modal. Query params prefill it so a personalized
 * link (`/join?first=Amara&last=Okafor&email=a@x.org`) lands with nothing to
 * type, a referral link (`/join?ref=DRCHEN99`) attributes the signup, and a
 * general link (`/join?flavor=general`) relaxes the MD credential screens for
 * a non-clinical signer such as a business advisor. Submitting mints the same
 * guarded self-serve invite the "Become a contributor" modal uses and drops
 * straight into the wizard.
 */
export default function JoinEntry() {
  const params = useMemo(
    () => new URLSearchParams(typeof window !== "undefined" ? window.location.search : ""),
    [],
  );
  const flavor = (params.get("flavor") || "").trim().toLowerCase();
  const general = flavor === "general";
  const [firstName, setFirstName] = useState((params.get("first") || "").trim());
  const [lastName, setLastName] = useState((params.get("last") || "").trim());
  const [email, setEmail] = useState((params.get("email") || "").trim());
  const [honeypot, setHoneypot] = useState("");
  /* A referral code typed in by hand. The Referral tab tells physicians they
     can "give them the code to enter at archangelhealth.ai/join", and until now
     this page only ever read ?ref= from the URL -- so a code passed along in a
     text message had nowhere to go and the referrer silently lost the credit.
     A code in the URL still wins: it is the one the referrer actually sent. */
  const [typedCode, setTypedCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [redirecting, setRedirecting] = useState(false);
  const [error, setError] = useState("");

  const emailValid = /.+@.+\..+/.test(email.trim());

  async function handleSubmit() {
    if (busy || !emailValid) return;
    setBusy(true);
    setError("");
    try {
      const { onboarding_url } = await authApi.createPhysicianOnboardingLink({
        email: email.trim().toLowerCase(),
        company_website: honeypot,
        first_name: firstName.trim(),
        last_name: lastName.trim(),
        // The ref param survives editing every field: attribution belongs to
        // the link that brought them here, not to the address they typed.
        referral_code: (params.get("ref") || typedCode).trim(),
        flavor,
      });
      setRedirecting(true);
      window.location.assign(onboarding_url);
    } catch (e) {
      setBusy(false);
      setError(e instanceof Error ? e.message : "Something went wrong. Please try again.");
    }
  }

  return (
    <div className="ah-onb-root">
      <OnboardingStyles />
      <ChromeHeader onExit={() => window.location.assign("/")} />
      <main style={{ flex: 1, padding: "56px 24px 80px", position: "relative" }}>
        <OnboardingCard
          maxWidth={560}
          eyebrow={general ? "Join Archangel" : "Join as a physician"}
          title={redirecting ? "Taking you to onboarding." : "Get started."}
          lede={
            redirecting
              ? "One moment."
              : general
                ? "You have an invite link. Confirm your name and email and we will set up your access; the medical credential steps are optional for you."
                : "Confirm your name and email and we will take you straight into onboarding. Paid clinical AI evaluation work, on your schedule."
          }
        >
          {!redirecting && (
            <>
              <InlineError>{error}</InlineError>
              <div style={{ display: "flex", gap: 14 }}>
                <div style={{ flex: 1 }}>
                  <TextField
                    label="First name"
                    placeholder="Amara"
                    value={firstName}
                    onChange={setFirstName}
                  />
                </div>
                <div style={{ flex: 1 }}>
                  <TextField
                    label="Last name"
                    placeholder="Okafor"
                    value={lastName}
                    onChange={setLastName}
                  />
                </div>
              </div>
              <TextField
                label="Work email"
                placeholder="a.okafor@hospital.org"
                type="email"
                value={email}
                onChange={setEmail}
              />
              {!params.get("ref") && (
                <TextField
                  label="Referral code"
                  optional
                  placeholder="From the colleague who invited you"
                  value={typedCode}
                  onChange={(v) => setTypedCode(v.trim().toUpperCase())}
                />
              )}
              {/* Honeypot, mirrored from the contributor modal: never shown to
                  a person, always filled by a naive bot. */}
              <input
                type="text"
                name="company_website"
                value={honeypot}
                onChange={(e) => setHoneypot(e.target.value)}
                tabIndex={-1}
                autoComplete="off"
                aria-hidden="true"
                style={{ position: "absolute", left: -9999, width: 1, height: 1, opacity: 0 }}
              />
              <div style={{ marginTop: 20 }}>
                <PrimaryButton onClick={handleSubmit} disabled={!emailValid || busy} loading={busy}>
                  Start onboarding
                </PrimaryButton>
              </div>
              <p style={{ margin: "18px 0 0", fontSize: "0.8rem", lineHeight: 1.55, color: "var(--ink-faint)" }}>
                We will email you the same link so you can pause and resume any
                time. Verification of your details happens inside onboarding.
              </p>
            </>
          )}
        </OnboardingCard>
      </main>
    </div>
  );
}
