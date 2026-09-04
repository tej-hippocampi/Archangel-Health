import { useEffect, useMemo, useState } from "react";
import OnboardingStyles from "./onboarding/OnboardingStyles";
import {
  ChromeHeader,
  InlineError,
  OnboardingCard,
  PrimaryButton,
  SelectField,
  TextArea,
  TextField,
} from "./onboarding/primitives";
import * as authApi from "@/lib/auth-api";

/**
 * /partner — the link that goes at the bottom of the health-system one-pager.
 *
 * One job: collect enough that the meeting starts from something real. It is
 * deliberately NOT the portal signup. A CIO reading a one-pager is not ready to
 * create an account and will not upload anything today; asking them to is how
 * you lose them. They answer a short set of questions. The portal link is what
 * we send after the call.
 *
 * ─── The booking lives in the email now, not on this page ──────────────────
 * This screen used to end at a Calendly button, and a visitor who did not click
 * it in that second was gone: nothing had been sent, so there was nothing to
 * reply to and nothing to follow up. The submit now triggers a letter carrying
 * the same link (``build_hs_interest_thanks_email``), which can be forwarded to
 * whoever actually holds the calendar and can be chased once if it goes quiet.
 * Do not put the button back: two doors to one booking is how one of them stops
 * being maintained.
 *
 * Not an ArchShell route on purpose. `useLandingAuth` bounces a signed-in user
 * off the marketing shell to their portal, which would eject exactly the person
 * we are courting, and `normalizePath` refuses to navigate anywhere outside
 * ARCH_PATHS. So this is a top-level route in App.tsx alongside /join, built on
 * the onboarding primitives that /join already uses.
 *
 * ─── Why three of these questions are not like the others ──────────────────
 * Authority, de-identification and data scale are QUALIFYING questions, agreed
 * word for word in the Sep 1 meeting, and they are the reason
 * docs/prds/prd-health-systems.md can call a submission the legal audit trail
 * of an authority attestation. That claim only holds if the authority question
 * is asked on the form being archived, so they are sent as their own fields and
 * stored in their own columns rather than folded into the prose `message` with
 * everything else. They also go into the message, because the message is what a
 * person reads in an inbox and half an answer there is worse than none.
 */

type Answers = {
  name: string;
  email: string;
  organization: string;
  role: string;
  dataHeld: string;
  licensable: string;
  scale: string;
  timeline: string;
  authority: string;
  deidentification: string;
  dataScale: string;
};

/* The wording is the answer. These are stored verbatim and read back months
   later by whoever has to say what this organization told us, so the option a
   CIO picked has to be a sentence rather than a token we would then have to
   keep a decoder for. "Not sure yet" is offered on both because it is the true
   answer for most first conversations, and a form with no honest option for it
   collects a confident "yes" that nobody meant. */
const AUTHORITY_OPTIONS = [
  { value: "Yes, we can license de-identified clinical data to a commercial party",
    label: "Yes" },
  { value: "Not sure yet, we would need to check with legal or compliance",
    label: "Not sure yet" },
  { value: "No, we cannot license de-identified clinical data to a commercial party",
    label: "No" },
];

const DEIDENTIFICATION_OPTIONS = [
  { value: "Yes, we can de-identify and date-shift", label: "Yes, both" },
  { value: "We can de-identify, but not date-shift", label: "De-identify only" },
  { value: "Not sure yet", label: "Not sure yet" },
  { value: "No, we cannot de-identify or date-shift", label: "No" },
];

/* The lead endpoint takes one `message` string, so the structure has to live in
   the text. Labelled sections rather than JSON: this lands in an inbox and gets
   read by a person, and it is pasted into a CRM by a person after that. */
function composeMessage(a: Answers): string {
  const rows: [string, string][] = [
    /* The contact's name, which used to exist only on this page: it went into
       the Calendly prefill and nowhere else. Two letters now need it, the
       thanks and the one reminder, and the second is composed days later by a
       sweep that has only the stored row. `routers/leads.py::partner_lead_contact`
       reads these two labels back out; keep the pair in step with it. */
    ["Contact", a.name],
    ["Health system", a.organization],
    ["Their role", a.role],
    ["Scale", a.scale],
    ["Authority to license", a.authority],
    ["De-identify and date-shift", a.deidentification],
    ["Patients, years, specialties", a.dataScale],
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
  /* `ref` is the physician's own referral code, the same attribution key /join
     uses, and `asclepius/referrals.py::partner_url` has always put BOTH on the
     link it builds. This page read only `hs`, so a physician who copied the
     plain referral link out of their dashboard and sent it to a health system
     themselves got no credit for the introduction at all. Opaque here: the
     backend resolves it and an unknown code is a silent no-op. */
  const referralCode = (params.get("ref") || "").trim();
  const [referrerFirstName, setReferrerFirstName] = useState("");
  const [role, setRole] = useState("");
  const [dataHeld, setDataHeld] = useState("");
  const [licensable, setLicensable] = useState("");
  const [scale, setScale] = useState("");
  const [timeline, setTimeline] = useState("");
  const [authority, setAuthority] = useState("");
  const [deidentification, setDeidentification] = useState("");
  const [dataScale, setDataScale] = useState("");
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
  /* The four original required fields, plus the two qualifying questions that
     are one click each. Every extra required BOX on a page like this is a
     person who closes the tab, which is why the third qualifying question, the
     one that needs typing, stays optional and why scale, role, licensing and
     timeline stay optional too. A dropdown is not a box in that sense: it costs
     a click, and a form that lets the authority question be skipped is a form
     whose archive cannot be called an attestation. */
  const canSubmit =
    emailValid && !!name.trim() && !!organization.trim() && !!dataHeld.trim()
    && !!authority && !!deidentification && !busy;

  async function handleSubmit() {
    if (!canSubmit) return;
    setBusy(true);
    setError("");
    const answers: Answers = {
      name, email, organization, role, dataHeld, licensable, scale, timeline,
      authority, deidentification, dataScale,
    };
    try {
      await authApi.submitLead({
        source: "health_system_partner",
        email: email.trim().toLowerCase(),
        message: composeMessage(answers),
        company_website: honeypot,
        referral_token: referralToken || undefined,
        referral_code: referralCode || undefined,
        /* Sent as their own fields as well as inside the message. The message is
           for the person reading the inbox; these are what gets archived in
           columns, and what somebody has to be able to produce later. */
        authority_answer: authority,
        deidentification_answer: deidentification,
        data_scale_answer: dataScale.trim(),
      });
      setSent(true);
    } catch (e) {
      setBusy(false);
      setError(e instanceof Error ? e.message : "Something went wrong. Please try again.");
    }
  }

  /* ── Thank-you beat ─────────────────────────────────────────────────────
     A thank-you, and nothing to click. The booking button that used to live
     here is gone on purpose: it made the visit the only chance we ever had at
     this organization, because a person who closed the tab left no address we
     had said anything to. The next step arrives by email, where it can be
     forwarded, replied to, and followed up once. */
  if (sent) {
    return (
      <div className="ah-onb-root">
        <OnboardingStyles />
        <ChromeHeader onExit={() => window.location.assign("/")} exitLabel="Back to site" />
        <main style={{ flex: 1, padding: "56px 24px 80px", position: "relative" }}>
          <OnboardingCard
            maxWidth={560}
            eyebrow="Health systems"
            title="Thank you for submitting."
            lede={
              referrerFirstName
                ? `We read every one of these ourselves, and we will email you the next step. ${referrerFirstName} will be glad you took it.`
                : "We read every one of these ourselves, and we will email you the next step."
            }
          >
            <p
              style={{
                margin: "6px 0 0",
                fontSize: "0.9rem",
                lineHeight: 1.6,
                color: "var(--ink-soft)",
              }}
            >
              That email has a link to book a short call with us, so you can
              forward it to whoever should be on it. If it has not arrived
              within a few minutes, check your spam folder and then write to us
              directly.
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

          {/* The three qualifying questions, as agreed on Sep 1. They sit
              together and last because they read as paperwork next to the two
              open questions above, and someone who has just described their
              data is further in than someone who has not started. */}
          <SelectField
            label="Does your organization have the authority to license de-identified clinical data to a commercial party?"
            placeholder="Select an answer"
            value={authority}
            onChange={setAuthority}
            options={AUTHORITY_OPTIONS}
          />

          <SelectField
            label="Can you de-identify and date-shift?"
            placeholder="Select an answer"
            value={deidentification}
            onChange={setDeidentification}
            options={DEIDENTIFICATION_OPTIONS}
          />

          <TextArea
            label="Roughly how many patients, over how many years, and in which specialties?"
            placeholder="e.g. around 80,000 patients over 12 years, mostly nephrology and cardiology"
            optional
            rows={3}
            value={dataScale}
            onChange={setDataScale}
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
