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
 * and a non-clinical flavor relaxes the MD credential screens. There are
 * three of those, because they go to different people:
 *   ?flavor=general    an invited non-clinical signer
 *   ?flavor=advisor    a supporter who looks around and refers
 *   ?flavor=referrer   someone who holds a referral link and nothing else
 * Submitting mints the same guarded self-serve invite the "Become a
 * contributor" modal uses and drops straight into the wizard.
 */
export default function JoinEntry() {
  const params = useMemo(
    () => new URLSearchParams(typeof window !== "undefined" ? window.location.search : ""),
    [],
  );
  const flavor = (params.get("flavor") || "").trim().toLowerCase();
  /* Which door this link is. All three non-clinical ones skip the physician
     credential screens; what changes is what the page promises, because the
     three are handed to different people for different reasons. */
  const advisor = flavor === "advisor";
  const referrer = flavor === "referrer";
  const general = flavor === "general" || advisor || referrer;
  const [firstName, setFirstName] = useState((params.get("first") || "").trim());
  const [lastName, setLastName] = useState((params.get("last") || "").trim());
  const [email, setEmail] = useState((params.get("email") || "").trim());
  const [honeypot, setHoneypot] = useState("");
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
        // Attribution is the LINK and nothing else. A physician sends their
        // personal link, the person they sent it to opens it, and the credit is
        // recorded without either of them doing anything about it. There is no
        // code to type, here or anywhere: a step someone can forget is a
        // referral we lose and a physician who is not paid for an introduction
        // they actually made.
        //
        // The ref param survives editing every field: attribution belongs to
        // the link that brought them here, not to the address they typed.
        referral_code: (params.get("ref") || "").trim(),
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
          eyebrow={
            referrer ? "Refer physicians"
              : advisor ? "Join as an advisor"
                : general ? "Join Archangel"
                  : "Join as a physician"
          }
          title={redirecting ? "Taking you to onboarding." : "Get started."}
          lede={
            redirecting
              ? "One moment."
              : referrer
                ? "You do not need to be a doctor. Confirm your name and email and you get your own referral link: every physician who joins through it, and every health system you introduce, is credited to you and paid out."
                : advisor
                  ? "Confirm your name and email and we will set up your access. You can look around the platform, and you get your own referral link for the physicians and health systems you know. No medical credentials needed."
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
              {/* Honeypot: never shown to a person, always filled by a naive
                  bot. The name is deliberately NOT "company_website", and the
                  field carries no id, label or placeholder. Chrome and Safari
                  match address-profile fields on exactly those signals and fill
                  them regardless of autocomplete="off", which both browsers
                  ignore for address data. A real physician with a saved profile
                  was therefore tripping the honeypot, being handed the decoy
                  link, and dead-ending on "Invalid or expired onboarding link"
                  with a 200 OK and no row written. Only the browser-visible
                  signals move here; the API field is still company_website. */}
              <input
                type="text"
                name="jn_ref_tag"
                value={honeypot}
                onChange={(e) => setHoneypot(e.target.value)}
                tabIndex={-1}
                autoComplete="off"
                aria-hidden="true"
                data-lpignore="true"
                data-1p-ignore=""
                data-form-type="other"
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
