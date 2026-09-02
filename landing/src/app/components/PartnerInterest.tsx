import { useEffect, useMemo, useState } from "react";
import OnboardingStyles from "./onboarding/OnboardingStyles";
import {
  ChromeHeader,
  InlineError,
  OnboardingCard,
  PrimaryButton,
  TextArea,
  TextField,
} from "./onboarding/primitives";
import * as authApi from "@/lib/auth-api";

/**
 * /partner — the link that goes at the bottom of the health-system one-pager.
 *
 * One job, in two beats: collect enough that the meeting starts from something
 * real, then book the meeting. It is deliberately NOT the portal signup. A CIO
 * reading a one-pager is not ready to create an account and will not upload
 * anything today; asking them to is how you lose them. They answer five
 * questions and pick a time. The portal link is what we send after the call.
 *
 * Not an ArchShell route on purpose. `useLandingAuth` bounces a signed-in user
 * off the marketing shell to their portal, which would eject exactly the person
 * we are courting, and `normalizePath` refuses to navigate anywhere outside
 * ARCH_PATHS. So this is a top-level route in App.tsx alongside /join, built on
 * the onboarding primitives that /join already uses.
 */

/* The connected Calendly account: "Quick Meeting", 20 minutes, Google Meet.
   The `month` param is carried through as given. Calendly treats it as the
   month to OPEN on rather than the only month it will show, and pages forward
   on its own, so a month in the past does not strand anyone. */
const CALENDLY_BASE =
  "https://calendly.com/aryaabhatia-berkeley/new-meeting?month=2026-03";

function calendlyUrl(name: string, email: string): string {
  const q = new URLSearchParams();
  const n = name.trim();
  const e = email.trim();
  /* Prefill so they do not retype what they just typed. Calendly reads these
     two off the query string and fills its own form. */
  if (n) q.set("name", n);
  if (e) q.set("email", e);
  const qs = q.toString();
  /* Joined with & because the base already carries a query. Building it with ?
     would produce a second question mark and Calendly would read the tail as
     one malformed parameter. */
  return qs ? `${CALENDLY_BASE}&${qs}` : CALENDLY_BASE;
}

type Answers = {
  name: string;
  email: string;
  organization: string;
  role: string;
  dataHeld: string;
  licensable: string;
  scale: string;
  timeline: string;
};

/* The lead endpoint takes one `message` string, so the structure has to live in
   the text. Labelled sections rather than JSON: this lands in an inbox and gets
   read by a person, and it is pasted into a CRM by a person after that. */
function composeMessage(a: Answers): string {
  const rows: [string, string][] = [
    ["Health system", a.organization],
    ["Their role", a.role],
    ["Scale", a.scale],
    ["Data they hold", a.dataHeld],
    ["Open to licensing", a.licensable],
    ["Timeline", a.timeline],
  ];
  return rows
    .filter(([, v]) => v.trim())
    .map(([k, v]) => `${k}:\n${v.trim()}`)
    .join("\n\n");
}

export default function PartnerInterest() {
  const params = useMemo(
    () => new URLSearchParams(typeof window !== "undefined" ? window.location.search : ""),
    [],
  );

  /* A personalized link works the same way /join's does, so a one-pager sent to
     a named person can arrive with their details already in. */
  const [name, setName] = useState((params.get("name") || "").trim());
  const [email, setEmail] = useState((params.get("email") || "").trim());
  const [organization, setOrganization] = useState((params.get("org") || "").trim());
  /* `hs` is the per-referral token on a link inside an introduction email a
     physician asked us to send. It does two jobs: it fills the form in with
     what we already told this person about themselves, and it carries the
     attribution back on submit so the referring physician's funnel moves. */
  const referralToken = (params.get("hs") || "").trim();
  const [referrerFirstName, setReferrerFirstName] = useState("");
  const [role, setRole] = useState("");
  const [dataHeld, setDataHeld] = useState("");
  const [licensable, setLicensable] = useState("");
  const [scale, setScale] = useState("");
  const [timeline, setTimeline] = useState("");
  const [honeypot, setHoneypot] = useState("");

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [sent, setSent] = useState(false);

  /* Prefill, once, and only into fields the visitor has not already touched --
     a value they typed always beats one we fetched. Fire-and-forget: the form
     is fully usable before this resolves and stays usable if it never does. */
  useEffect(() => {
    if (!referralToken) return;
    let live = true;
    authApi.fetchHsReferralPrefill(referralToken).then((p) => {
      if (!live || !p.found) return;
      if (p.contact_name) setName((v) => v || p.contact_name!.trim());
      if (p.contact_email) setEmail((v) => v || p.contact_email!.trim());
      if (p.hs_name) setOrganization((v) => v || p.hs_name!.trim());
      if (p.contact_role) setRole((v) => v || p.contact_role!.trim());
      if (p.referrer_first_name) setReferrerFirstName(p.referrer_first_name.trim());
    });
    return () => {
      live = false;
    };
  }, [referralToken]);

  const emailValid = /.+@.+\..+/.test(email.trim());
  /* Four required fields, and no more. Every extra required box on a page like
     this is a person who closes the tab. Scale and timeline are nice to have
     and are marked optional. */
  const canSubmit =
    emailValid && !!name.trim() && !!organization.trim() && !!dataHeld.trim() && !busy;

  async function handleSubmit() {
    if (!canSubmit) return;
    setBusy(true);
    setError("");
    const answers: Answers = {
      name, email, organization, role, dataHeld, licensable, scale, timeline,
    };
    try {
      await authApi.submitLead({
        source: "health_system_partner",
        email: email.trim().toLowerCase(),
        message: composeMessage(answers),
        company_website: honeypot,
        referral_token: referralToken || undefined,
      });
      setSent(true);
    } catch (e) {
      setBusy(false);
      setError(e instanceof Error ? e.message : "Something went wrong. Please try again.");
    }
  }

  /* ── Booked-the-meeting beat ────────────────────────────────────────────
     The submit is not the finish line; the meeting is. So the success state is
     not a thank-you note, it is a door with one thing behind it. */
  if (sent) {
    return (
      <div className="ah-onb-root">
        <OnboardingStyles />
        <ChromeHeader onExit={() => window.location.assign("/")} exitLabel="Back to site" />
        <main style={{ flex: 1, padding: "56px 24px 80px", position: "relative" }}>
          <OnboardingCard
            maxWidth={560}
            eyebrow="Health systems"
            title="Got it. Now pick a time."
            lede={
              referrerFirstName
                ? `We read every one of these before the call, so we will come with specifics about your data rather than a pitch. ${referrerFirstName} will be glad you took it.`
                : "We read every one of these before the call, so we will come with specifics about your data rather than a pitch."
            }
          >
            {/* A real anchor, via PrimaryButton's link form. This was a
                <PrimaryButton> wrapped in an <a>, which is a button nested
                inside an anchor: invalid HTML, and Safari will not activate an
                anchor from a nested button, so the control did nothing. */}
            <PrimaryButton fullWidth href={calendlyUrl(name, email)}>
              Book an intro call
            </PrimaryButton>
            <p
              style={{
                margin: "18px 0 0",
                fontSize: "0.8rem",
                lineHeight: 1.55,
                color: "var(--ink-faint)",
                textAlign: "center",
              }}
            >
              20 minutes, over Google Meet, with Aryaa. If none of the times work,
              reply to the confirmation email and we will find one.
            </p>
          </OnboardingCard>
        </main>
      </div>
    );
  }

  return (
    <div className="ah-onb-root">
      <OnboardingStyles />
      <ChromeHeader onExit={() => window.location.assign("/")} exitLabel="Back to site" />
      <main style={{ flex: 1, padding: "56px 24px 80px", position: "relative" }}>
        <OnboardingCard
          maxWidth={620}
          eyebrow="Health systems"
          title="Let's talk about your data."
          lede="A few questions so the first call is useful, then you pick a time. Nothing here commits you to anything, and we do not need patient data to have the conversation."
        >
          <InlineError>{error}</InlineError>

          <div style={{ display: "flex", gap: 14 }}>
            <div style={{ flex: 1 }}>
              <TextField label="Your name" placeholder="Dana Reyes" value={name} onChange={setName} />
            </div>
            <div style={{ flex: 1 }}>
              <TextField
                label="Work email"
                placeholder="d.reyes@stmarys.org"
                type="email"
                value={email}
                onChange={setEmail}
              />
            </div>
          </div>

          <div style={{ display: "flex", gap: 14 }}>
            <div style={{ flex: 1 }}>
              <TextField
                label="Health system"
                placeholder="St Mary's Health"
                value={organization}
                onChange={setOrganization}
              />
            </div>
            <div style={{ flex: 1 }}>
              <TextField
                label="Your role"
                placeholder="CMIO"
                optional
                value={role}
                onChange={setRole}
              />
            </div>
          </div>

          <TextField
            label="Roughly how big"
            placeholder="Beds, sites, or annual encounters. A guess is fine."
            optional
            value={scale}
            onChange={setScale}
          />

          <TextArea
            label="What clinical data do you hold, and in what systems?"
            placeholder="e.g. Epic, ~12 years of nephrology and cardiology encounters with labs, notes and outcomes"
            rows={3}
            value={dataHeld}
            onChange={setDataHeld}
          />

          <TextArea
            label="What would you be open to licensing to us?"
            placeholder="Whatever you already have a sense of. If the answer is 'not sure yet', say that."
            optional
            rows={3}
            value={licensable}
            onChange={setLicensable}
          />

          <TextField
            label="Timeline"
            placeholder="Exploring, this quarter, budgeted for next year"
            optional
            value={timeline}
            onChange={setTimeline}
          />

          {/* Honeypot, mirrored from /join and the contributor modal: never shown
              to a person, always filled by a naive bot. */}
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
            <PrimaryButton
              onClick={handleSubmit}
              disabled={!canSubmit}
              fullWidth
              loadingLabel="Sending"
            >
              Continue to book a call
            </PrimaryButton>
          </div>

          <p
            style={{
              margin: "18px 0 0",
              fontSize: "0.8rem",
              lineHeight: 1.55,
              color: "var(--ink-faint)",
            }}
          >
            We work with de-identified data only, inside your existing governance
            and IRB review, under an agreement signed before anything moves.
            Nothing is shared or resold beyond the license.
          </p>
        </OnboardingCard>
      </main>
    </div>
  );
}
