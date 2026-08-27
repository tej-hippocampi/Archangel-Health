/**
 * Archangel Health — onboarding step screens (Step 1 → Step 6).
 *
 * Each step is a thin layout component over the primitives. State + API calls
 * live in `OnboardingWizard.tsx`; steps only own the local form state and
 * delegate transitions back to the parent via `onNext`.
 *
 * `onNext` returns a Promise<boolean>: resolving `false` keeps the
 * PrimaryButton in the Idle state (so server errors don't fake-flash success).
 */

import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";

import { API_BASE } from "@/lib/auth-api";

import {
  Avatar,
  BackLink,
  ChipMultiSelect,
  CodeInput,
  FieldLabel,
  InlineError,
  OnboardingCard,
  PrimaryButton,
  RolePill,
  SelectField,
  StatusPill,
  TextArea,
  TextField,
  YesNoToggle,
} from "./primitives";

/* Shared shape — same across all steps so the wizard owns one state object. */

// Pass-4 role taxonomy. The director is auto-seeded as a `surgeon` on
// /finish, so the wizard only invites the two non-surgeon seats. The pod
// caps at 4 = director + 1 RN + 2 NP/PAs.
export type RoleLabel = "RN Care Coordinator" | "NP / PA";

export const TEAM_CAP_NON_DIRECTOR = 3;
export const TEAM_CAP_TOTAL = 4;
export const TEAM_CAP_RN_COORDINATOR = 1;
export const TEAM_CAP_NP_PA = 2;

export type Member = {
  id: number;
  firstName: string;
  lastName: string;
  email: string;
  role: RoleLabel;
  status: "Invited" | "Active";
};

/* ── Asclepius (data-training product) types ───────────────────────────── */

export type Product = "archangel" | "asclepius";

export type AsclepiusRole = "physician" | "np" | "pa" | "resident_fellow";

export const ASCLEPIUS_ROLE_LABELS: Record<AsclepiusRole, string> = {
  physician: "Physician (MD/DO/MBBS)",
  np: "Nurse Practitioner (NP)",
  pa: "Physician Assistant (PA)",
  resident_fellow: "Resident / Fellow",
};

export const ASCLEPIUS_TEAM_CAP = 10;

export type AsclepiusMember = {
  id: number;
  firstName: string;
  lastName: string;
  email: string;
  role: AsclepiusRole;
  status: "Invited" | "Active";
};

export type BoardCert = { board: string; specialty: string; subspecialty: string; active: boolean };
export type Fellowship = { institution: string; specialty: string; year: string };
export type TrainingRow = { institution: string; year: string };

export type Credentials = {
  fullLegalName: string;
  /* The PHYSICIAN's own mobile — deliberately NOT `OnboardingData.phone`,
     which is the organization's front-office line posted to
     /asclepius/institution. Two different humans answer these. */
  phone: string;
  linkedinUrl: string;
  /* Free text; resolved to a health-system id downstream. */
  healthSystem: string;
  /* Set by the CV upload step for UI state only. The asset sha itself is
     recorded server-side at upload time and is never carried by the client —
     a client-supplied sha would be an unvalidated reference into the shared
     asset store. */
  cvFilename: string;
  npi: string;
  /* ── Where this doctor practises and where they are licensed ────────────
     The form used to require a 10-digit NPI and a two-letter US state, so a
     doctor registered with SCFHS or an Indian state council could not finish
     it — not because we would turn them away, but because there was nowhere
     to put what they hold.

     These route the VERIFICATION and nothing else: which registry answers,
     which document to ask for, what an admin opens to check by hand. Country
     is one step from IMG status, which §3.3 forbids as a score input, so it
     is pinned immobile at the encoder alongside it — see the forbidden-key
     property test in test_tiering_score.py. Note what is still absent below:
     medical school and graduation year stay off this type. */
  countryOfPractice: string;   // ISO 3166-1 alpha-2
  countryOfLicensure: string;
  countryOfDegree: string;
  /* The non-US twin of `npi`: SCFHS number, state council registration, GMC
     reference. Kept separate so `npi` keeps meaning exactly one thing. */
  registrationNumber: string;
  /* Whatever else that country's registry needs before it will answer — the
     Indian state council, for instance. Keyed by RegistryConfig.FieldSpec. */
  registryExtras: Record<string, string>;
  /* Primary medical qualification as the doctor names it. "MD" means the
     primary degree across much of Europe and a POSTGRADUATE specialty degree
     in India, so the word is recorded rather than interpreted. */
  qualification: string;
  /* UI state only, like cvFilename; the sha is recorded server-side. */
  licenseDocFilename: string;
  degree: string;
  boardCertifications: BoardCert[];
  fellowship: Fellowship[];
  residency: TrainingRow[];
  primarySpecialty: string;
  /* Optional free text: the doctor's niche within their specialty and the
     case types they focus on (e.g. a general surgeon's surgical subfocus).
     Descriptive metadata only, NEVER a scoring input (see the never-collect
     note below and tiering.py); same treatment as `subspecialties`. */
  specialtyNiche: string;
  subspecialties: string[];
  practiceSettings: string[];
  currentlyActive: boolean | null;
  yearsInActivePractice: string;
  languages: string[];
  /* ── Reviewer eligibility (PRD C §2 gates + §3.2 features) ──────────────
     Every field below is read exactly once and reduced to a boolean or a
     licence cross-check. None is stored as a magnitude the model can scale on. */
  licenseNumber: string;
  licenseState: string;
  residencyCompleted: boolean | null;
  /* Consumed as `post_residency_ge_3yr`, a CAPPED BINARY, and discarded. The
     Choudhry review found an INVERSE relationship between years in practice and
     quality of care, and years-in-practice is simultaneously the most direct
     available proxy for age — so it is a gate, never a continuous scaling term. */
  residencyCompletionYear: string;
  /* Threshold input for `currently_practicing` (>= 4 = practising). Someone
     doing 20 half-days is not "more practising" than someone doing 4. */
  clinicalHalfDaysPerMonth: string;
  /* AUDIT H1. `currently_practicing` used to be a hard binary cliff at four
     half-days a month, worth 0.80 — the third-largest term in the model. Part-time
     clinical practice is not evenly distributed by sex, caregiving status or
     disability, and a physician on parental or medical leave scored identically to
     one who had left medicine, because leave produces the same number: zero.

     These two fields are what let the encoder tell those apart. `practiceStatus`
     is enumerated rather than inferred from a zero count precisely because "0
     because I am on leave" and "0 because I stopped in 2019" are the same number
     and opposite facts. See docs/PRD_C_COUNSEL_MEMO.md §3.1. */
  practiceStatus: "" | "active" | "on_leave" | "not_practising";
  halfDaysBeforeLeave: string;
  continuingCertification: boolean | null;
  structuredReviewExperience: string[];
};

/* NOT ON THIS TYPE, DELIBERATELY, AND NOT TO BE RE-ADDED (PRD C §3.3):
   medical school name or rank · US-MD vs IMG · ECFMG certification as a SCORE
   input (it satisfies gate A3 as one of several equivalent degrees, and nothing
   more) · graduation year · date of birth · sex · continuous years in practice ·
   practice ZIP or region · self-rated expertise.

   `medicalSchool` was on this type and on the form until this release. It
   satisfied no gate, and both of its fields — institution and year — are on the
   never-collect list. `backend/asclepius/tiering.py` additionally refuses to read
   any of these keys, and `test_tiering_score.py` asserts that varying each of
   them across its plausible range changes the score by exactly 0.0, so a legacy
   row that still carries one cannot influence a tier. */

/* AUDIT H1: "on leave" is a first-class answer, listed alongside the others rather
   than hidden behind a zero. A physician should not have to decide whether parental
   leave means they type 0 and get penalised or type their usual number and misreport. */
const PRACTICE_STATUS_OPTIONS = [
  { value: "active", label: "Actively seeing patients" },
  { value: "on_leave", label: "On leave (parental, medical, caregiving, military, sabbatical)" },
  { value: "not_practising", label: "Not currently in clinical practice" },
];

const STRUCTURED_REVIEW_SUGGESTIONS = [
  "cec_dsmb",
  "journal_peer_review",
  "board_item_writing",
  "guideline_panel",
  "core_faculty",
  "program_director",
];

export type Attestations = {
  consentCredentialShare: boolean;
  attestIndependentJudgment: boolean;
  ipAssignment: boolean;
  noPhi: boolean;
  /* Hard gates A7 and A6 (PRD C §2). Separate from `attestIndependentJudgment`
     on purpose: confidentiality and independence are two different promises, and
     a single combined checkbox cannot be revoked or audited separately. */
  attestConfidentiality: boolean;
  attestNoDisciplinaryAction: boolean;
  /* Work has to meet the rubric to be paid for. Said plainly, and signed for,
     BEFORE anyone does a case -- a physician finding this out from a payout
     that did not arrive would be right to be angry, and would be hearing our
     quality bar for the first time at the worst possible moment. */
  attestWorkQuality: boolean;
  signedInitials: string;
};

export function emptyCredentials(fullLegalName = ""): Credentials {
  return {
    fullLegalName,
    phone: "",
    linkedinUrl: "",
    healthSystem: "",
    cvFilename: "",
    npi: "",
    // Defaults to the US so the form opens exactly as it always has for the
    // doctors who are most of the traffic; changing the country is what opens
    // the rest of the world's fields.
    countryOfPractice: "US",
    countryOfLicensure: "US",
    countryOfDegree: "US",
    registrationNumber: "",
    registryExtras: {},
    qualification: "",
    licenseDocFilename: "",
    degree: "",
    boardCertifications: [{ board: "", specialty: "", subspecialty: "", active: true }],
    fellowship: [{ institution: "", specialty: "", year: "" }],
    residency: [{ institution: "", year: "" }],
    primarySpecialty: "",
    specialtyNiche: "",
    subspecialties: [],
    practiceSettings: [],
    currentlyActive: null,
    yearsInActivePractice: "",
    languages: [],
    licenseNumber: "",
    licenseState: "",
    residencyCompleted: null,
    residencyCompletionYear: "",
    clinicalHalfDaysPerMonth: "",
    practiceStatus: "",
    halfDaysBeforeLeave: "",
    continuingCertification: null,
    structuredReviewExperience: [],
  };
}

/* Placeholder for the optional "tell us more about your specialty" box. The
   example adapts to what the doctor typed as their primary specialty: surgeons
   get a surgical-niche nudge, everyone else a generic within-specialty nudge,
   and an empty specialty falls back to a neutral prompt. */
function nichePlaceholder(specialty: string): string {
  const s = specialty.trim();
  if (!s) return "e.g. the subspecialties you focus on and the specific case types you handle most.";
  if (s.toLowerCase().includes("surg"))
    return `e.g. in ${s}, your surgical niche (hepatobiliary, colorectal, trauma) and the case types you most want to review.`;
  return `e.g. within ${s}, the specific case types and clinical scenarios you focus on most.`;
}

export function emptyAttestations(): Attestations {
  return {
    consentCredentialShare: false,
    attestIndependentJudgment: false,
    ipAssignment: false,
    noPhi: false,
    attestConfidentiality: false,
    attestNoDisciplinaryAction: false,
    attestWorkQuality: false,
    signedInitials: "",
  };
}

export type OnboardingData = {
  firstName: string;
  lastName: string;
  email: string;
  orgName: string;
  department: string;
  phone: string;
  members: Member[];
  // Asclepius branch
  product: Product | "";
  specialty: string;
  ascMembers: AsclepiusMember[];
  credentials: Credentials;
  attestations: Attestations;
  // Member-mode (invited clinician) context
  roleLabel: string;
  workspaceUrl: string;
};

const TWO_COL: CSSProperties = { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 };
const CARD_FOOTER_BACK: CSSProperties = { marginTop: 18, textAlign: "center" };

/* ─────────────────────────────────────────────────────────────
   Step 1 — Name + email
   ───────────────────────────────────────────────────────────── */

/** Which door this person came through. A physician is signing up to do
 *  clinical work; the other two are not, and the screens below are the only
 *  ones they see. */
export type SignupKind = "physician" | "advisor" | "referrer";

export function Step1NameEmail({
  data,
  setData,
  onNext,
  error,
  kind = "physician",
}: {
  data: OnboardingData;
  setData: (patch: Partial<OnboardingData>) => void;
  onNext: () => Promise<boolean>;
  error?: string;
  kind?: SignupKind;
}) {
  const a = data.attestations;
  // An advisor reads physicians discussing their own practice. That is not a
  // clinical attestation and it is not the seven a doctor signs, but the people
  // whose conversations those are deserve one line on file, so this screen is
  // where it is asked. A referral partner sees nothing but their own link and
  // is asked for nothing beyond a name and a mailbox.
  const needsConfidentiality = kind === "advisor";

  // Each screen gates ONLY on what it asks for. Gating screen 1 on a licence
  // number it never showed is how a Continue button goes dead with no
  // explanation anywhere on the page.
  const valid =
    data.firstName.trim().length > 0 &&
    data.lastName.trim().length > 0 &&
    /\S+@\S+\.\S+/.test(data.email.trim()) &&
    (!needsConfidentiality || a.attestConfidentiality);

  const isAsclepius = data.product === "asclepius";

  const COPY: Record<SignupKind, { title: string; lede: string; hint: string }> = {
    physician: {
      title: isAsclepius
        ? "Welcome to Asclepius, let's start your journey."
        : "Let's get you set up.",
      lede: isAsclepius
        ? "A few minutes to get your evaluator workspace ready. We'll start with you."
        : "A few minutes to bring your health system online. We'll start with you.",
      hint: "Use your health-system email, we'll send a verification code here.",
    },
    advisor: {
      title: "Set up your advisor access.",
      lede: "Two screens. Your name, a code to your inbox, and a password. No credentials, because you are not signing up to label cases.",
      hint: "We'll send a verification code here.",
    },
    referrer: {
      title: "Set up your referral link.",
      lede: "Your name, a code to your inbox, and a password. That is the whole signup.",
      hint: "We'll send a verification code here.",
    },
  };
  const copy = COPY[kind];

  return (
    <OnboardingCard
      eyebrow="Step 1"
      title={copy.title}
      lede={copy.lede}
    >
      <InlineError>{error}</InlineError>
      <div style={TWO_COL}>
        <TextField
          label="First name"
          placeholder="Tej"
          value={data.firstName}
          onChange={(v) => setData({ firstName: v })}
          autoFocus
          autoComplete="given-name"
        />
        <TextField
          label="Last name"
          placeholder="Patel"
          value={data.lastName}
          onChange={(v) => setData({ lastName: v })}
          autoComplete="family-name"
        />
      </div>
      <TextField
        label={kind === "physician" ? "Work email" : "Email"}
        placeholder={kind === "physician" ? "you@yourhealthsystem.org" : "you@example.com"}
        type="email"
        value={data.email}
        onChange={(v) => setData({ email: v })}
        hint={copy.hint}
        autoComplete="email"
      />
      {needsConfidentiality && (
        <div style={{ marginTop: 18 }}>
          <CheckRow
            checked={a.attestConfidentiality}
            onToggle={() =>
              setData({
                attestations: { ...a, attestConfidentiality: !a.attestConfidentiality },
              })
            }
            title="Confidentiality"
            body="You will be able to read the cases, model outputs and physician discussion inside Archangel. I will keep what I see confidential, and will not reproduce or republish it."
          />
        </div>
      )}
      <div style={{ marginTop: 12 }}>
        <PrimaryButton
          fullWidth
          disabled={!valid}
          onClick={onNext}
          loadingLabel="Continuing…"
          successLabel="Continue ✓"
        >
          Continue
        </PrimaryButton>
      </div>
    </OnboardingCard>
  );
}

/* ─────────────────────────────────────────────────────────────
   Step 2 — Email verification (two states: pre-send / post-send)
   ───────────────────────────────────────────────────────────── */

export function Step2Verify({
  data,
  onSendCode,
  onVerify,
  onBack,
  error,
  eyebrow = "Step 2",
}: {
  data: OnboardingData;
  /** POST /api/onboarding/request-otp (or /member/request-otp); resolve `false` on error to stay Idle. */
  onSendCode: () => Promise<boolean>;
  /** POST /api/onboarding/verify-otp (or /member/verify-otp) with the 6-digit code; resolve `false` to stay Idle. */
  onVerify: (code: string) => Promise<boolean>;
  onBack: () => void;
  error?: string;
  eyebrow?: string;
}) {
  const [sent, setSent] = useState(false);
  const [resendIn, setResendIn] = useState(0);
  const [code, setCode] = useState("");

  useEffect(() => {
    if (resendIn <= 0) return;
    const t = window.setTimeout(() => setResendIn((s) => Math.max(0, s - 1)), 1000);
    return () => window.clearTimeout(t);
  }, [resendIn]);

  const sendCode = async () => {
    const ok = await onSendCode();
    if (ok) {
      setSent(true);
      setResendIn(30);
    }
    return ok;
  };

  const resend = async () => {
    if (resendIn > 0) return false;
    const ok = await onSendCode();
    if (ok) setResendIn(30);
    return ok;
  };

  const codeReady = code.length === 6;

  return (
    <OnboardingCard
      eyebrow={eyebrow}
      title="Verify your email."
      lede={
        sent ? (
          <>
            We sent a 6‑digit code to <span style={{ color: "var(--ah-green-deep)" }}>{data.email}</span>. Enter it below.
          </>
        ) : (
          <>
            We&apos;ll send a one‑time code to <span style={{ color: "var(--ah-green-deep)" }}>{data.email}</span> to confirm it&apos;s yours.
          </>
        )
      }
    >
      <InlineError>{error}</InlineError>

      {!sent && (
        <PrimaryButton
          fullWidth
          variant="secondary"
          onClick={sendCode}
          loadingLabel="Sending code…"
          successLabel="Code sent ✓"
          icon={
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z" />
              <polyline points="22,6 12,13 2,6" />
            </svg>
          }
        >
          Send 6‑digit code to my email
        </PrimaryButton>
      )}

      {sent && (
        <>
          <FieldLabel>Verification code</FieldLabel>
          <CodeInput value={code} onChange={setCode} />
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              fontSize: 12,
              color: "var(--ink-faint)",
              marginBottom: 22,
            }}
          >
            <span>Enter all 6 digits.</span>
            <button
              type="button"
              onClick={() => void resend()}
              disabled={resendIn > 0}
              style={{
                background: "transparent",
                border: "none",
                color: resendIn > 0 ? "var(--ink-faint)" : "var(--ah-green-deep)",
                fontSize: 12,
                fontWeight: 600,
                cursor: resendIn > 0 ? "default" : "pointer",
              }}
            >
              {resendIn > 0 ? `Resend in ${resendIn}s` : "Resend code"}
            </button>
          </div>
          <PrimaryButton
            fullWidth
            disabled={!codeReady}
            onClick={() => onVerify(code)}
            loadingLabel="Verifying…"
            successLabel="Verified ✓"
          >
            Verify code
          </PrimaryButton>
        </>
      )}

      <div style={CARD_FOOTER_BACK}>
        <BackLink onClick={onBack} />
      </div>
    </OnboardingCard>
  );
}

/* ─────────────────────────────────────────────────────────────
   Step 3 — Health system details
   ───────────────────────────────────────────────────────────── */

export function Step3Org({
  data,
  setData,
  onNext,
  onBack,
  error,
}: {
  data: OnboardingData;
  setData: (patch: Partial<OnboardingData>) => void;
  onNext: () => Promise<boolean>;
  onBack: () => void;
  error?: string;
}) {
  const valid =
    data.orgName.trim().length > 0 &&
    data.department.trim().length > 0 &&
    data.phone.trim().length > 0;

  return (
    <OnboardingCard
      eyebrow="Step 3 of 5"
      title="Tell us about your health system."
      lede="This is the workspace your team will sign in to."
    >
      <InlineError>{error}</InlineError>
      <TextField
        label="Health system name"
        placeholder="Cedars Sinai"
        value={data.orgName}
        onChange={(v) => setData({ orgName: v })}
        autoFocus
        autoComplete="organization"
      />
      <TextField
        label="Surgery department name"
        placeholder="Orthopedic Surgery"
        value={data.department}
        onChange={(v) => setData({ department: v })}
      />
      <TextField
        label="Health system phone"
        placeholder="(555) 123‑4567"
        type="tel"
        value={data.phone}
        onChange={(v) => setData({ phone: v })}
        autoComplete="tel"
      />
      <div
        style={{
          background: "var(--ah-green-wash)",
          border: "1px solid var(--ah-green-line)",
          borderRadius: 12,
          padding: "14px 16px",
          display: "flex",
          alignItems: "center",
          gap: 12,
          marginBottom: 24,
        }}
      >
        <div
          style={{
            width: 32,
            height: 32,
            borderRadius: 9,
            background: "var(--ah-green-wash)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            flexShrink: 0,
          }}
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--ah-green-deep)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M20 7L9 18l-5-5" />
          </svg>
        </div>
        <div style={{ fontSize: 13, color: "var(--ink-soft)", lineHeight: 1.5 }}>
          Your role: <strong style={{ color: "var(--ink)" }}>Director of TEAM Initiative</strong> — assigned automatically as the onboarding owner.
        </div>
      </div>
      <PrimaryButton fullWidth disabled={!valid} onClick={onNext} loadingLabel="Saving…" successLabel="Saved ✓">
        Continue
      </PrimaryButton>
      <div style={CARD_FOOTER_BACK}>
        <BackLink onClick={onBack} />
      </div>
    </OnboardingCard>
  );
}

/* ─────────────────────────────────────────────────────────────
   Step 4 — Your TEAM (Director card + add-member panel + member list)
   ───────────────────────────────────────────────────────────── */

export function Step4YourTeam({
  data,
  onAddMember,
  onRemoveMember,
  onNext,
  onBack,
  error,
}: {
  data: OnboardingData;
  /** POST /api/onboarding/add-team-member; resolve `false` on error to stay Idle. */
  onAddMember: (m: Omit<Member, "id" | "status">) => Promise<boolean>;
  onRemoveMember: (id: number) => void;
  onNext: () => Promise<boolean>;
  onBack: () => void;
  error?: string;
}) {
  const [showAdd, setShowAdd] = useState(false);
  const [draft, setDraft] = useState<{ firstName: string; lastName: string; email: string; role: RoleLabel | "" }>({
    firstName: "",
    lastName: "",
    email: "",
    role: "",
  });

  const directorName = `${data.firstName || "You"} ${data.lastName || ""}`.trim();
  const members = data.members;

  const rnCount = members.filter((m) => m.role === "RN Care Coordinator").length;
  const nppaCount = members.filter((m) => m.role === "NP / PA").length;
  const teamFull = members.length >= TEAM_CAP_NON_DIRECTOR;
  const totalCount = members.length + 1; // +1 for the director seat

  const roleOptions: { value: RoleLabel; label: string; disabled?: boolean }[] = [
    {
      value: "RN Care Coordinator",
      label: rnCount >= TEAM_CAP_RN_COORDINATOR
        ? "RN Care Coordinator (cap reached)"
        : "RN Care Coordinator",
      disabled: rnCount >= TEAM_CAP_RN_COORDINATOR,
    },
    {
      value: "NP / PA",
      label: nppaCount >= TEAM_CAP_NP_PA ? "NP / PA (cap reached)" : "NP / PA",
      disabled: nppaCount >= TEAM_CAP_NP_PA,
    },
  ];

  const draftValid =
    draft.firstName.trim() &&
    draft.lastName.trim() &&
    /\S+@\S+\.\S+/.test(draft.email) &&
    draft.role !== "" &&
    !teamFull &&
    !(draft.role === "RN Care Coordinator" && rnCount >= TEAM_CAP_RN_COORDINATOR) &&
    !(draft.role === "NP / PA" && nppaCount >= TEAM_CAP_NP_PA);

  const submitDraft = async () => {
    if (!draftValid) return false;
    const ok = await onAddMember({
      firstName: draft.firstName.trim(),
      lastName: draft.lastName.trim(),
      email: draft.email.trim(),
      role: draft.role as RoleLabel,
    });
    if (ok) {
      setDraft({ firstName: "", lastName: "", email: "", role: "" });
      window.setTimeout(() => setShowAdd(false), 600);
    }
    return ok;
  };

  return (
    <OnboardingCard
      maxWidth={720}
      eyebrow="Step 4 of 5"
      title="Your TEAM."
      lede="Your surgical pod is exactly 4 people: you (director / surgeon), 1 RN care coordinator, and 2 NP / PAs."
    >
      <InlineError>{error}</InlineError>

      {/* Director card — distinguished */}
      <div
        style={{
          background: "var(--card)",
          border: "1px solid var(--ah-green-line)",
          borderRadius: 14,
          padding: "20px 22px",
          marginBottom: 22,
          position: "relative",
          boxShadow: "var(--shadow-card)",
        }}
      >
        <div
          style={{
            position: "absolute",
            top: -10,
            left: 22,
            background: "var(--card)",
            padding: "0 10px",
            fontSize: 10,
            fontWeight: 400,
            letterSpacing: "0.08em",
            textTransform: "uppercase",
            fontFamily: "var(--mono)",
            color: "var(--ah-green-deep)",
          }}
        >
          Director of TEAM Initiative
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          <Avatar name={directorName} email={data.email} size={52} you />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4, flexWrap: "wrap" }}>
              <h3
                style={{
                  fontFamily: "var(--sans)",
                  fontSize: 20,
                  fontWeight: 500,
                  letterSpacing: "-0.01em",
                  color: "var(--ink)",
                  margin: 0,
                }}
              >
                {directorName || "You"}
              </h3>
              <StatusPill status="You" />
            </div>
            <div
              style={{
                fontSize: 13,
                color: "var(--ink-soft)",
                display: "flex",
                alignItems: "center",
                gap: 10,
                flexWrap: "wrap",
              }}
            >
              <span>{data.email}</span>
              <span style={{ width: 3, height: 3, borderRadius: "50%", background: "var(--ah-faint-30)" }} />
              <span>{data.orgName || "—"}</span>
              <span style={{ width: 3, height: 3, borderRadius: "50%", background: "var(--ah-faint-30)" }} />
              <span>{data.department || "—"}</span>
            </div>
          </div>
        </div>
      </div>

      {/* Members header */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 14 }}>
        <div>
          <div
            style={{
              fontSize: 11,
              fontWeight: 400,
              letterSpacing: "0.08em",
              textTransform: "uppercase",
              fontFamily: "var(--mono)",
              color: "var(--ink-faint)",
              marginBottom: 4,
            }}
          >
            Team members
          </div>
          <div style={{ fontSize: 13, color: "var(--ink-soft)" }}>
            {teamFull ? (
              <>
                <span style={{ color: "var(--ah-green-deep)", fontWeight: 600 }}>Team is complete.</span>{" "}
                Pod has 4 / 4 — director (surgeon), {rnCount} RN, {nppaCount} NP / PA.
              </>
            ) : (
              <>
                Team: <strong style={{ color: "var(--ink)" }}>{totalCount} / {TEAM_CAP_TOTAL}</strong>
                {totalCount === 1 ? " — director (surgeon)" : ""}
              </>
            )}
          </div>
        </div>
        {!showAdd && !teamFull && (
          <button
            type="button"
            onClick={() => setShowAdd(true)}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 8,
              padding: "9px 14px",
              borderRadius: 9999,
              background: "var(--ah-green-wash)",
              border: "1px solid var(--ah-green-line)",
              color: "var(--ah-green-deep)",
              fontSize: 13,
              fontWeight: 600,
              cursor: "pointer",
            }}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <line x1="12" y1="5" x2="12" y2="19" />
              <line x1="5" y1="12" x2="19" y2="12" />
            </svg>
            Add member
          </button>
        )}
      </div>

      {/* Members list */}
      <div style={{ display: "grid", gap: 10, marginBottom: showAdd ? 22 : 28 }}>
        {members.length === 0 && !showAdd && (
          <div
            style={{
              border: "1px dashed var(--hairline-strong)",
              borderRadius: 12,
              padding: "28px 20px",
              textAlign: "center",
              color: "var(--ink-faint)",
              fontSize: 13,
            }}
          >
            Click <strong style={{ color: "var(--ah-green-deep)" }}>Add member</strong> to invite the surgeons and care coordinators on your TEAM.
          </div>
        )}
        {members.map((m) => (
          <MemberRow key={m.id} member={m} onRemove={() => onRemoveMember(m.id)} />
        ))}
      </div>

      {/* Add panel */}
      {showAdd && (
        <div
          style={{
            background: "var(--card-in)",
            border: "1px solid var(--ah-green-line)",
            borderRadius: 14,
            padding: "20px 22px",
            marginBottom: 22,
            animation: "ah-onb-fade-up 320ms cubic-bezier(0.16, 1, 0.3, 1)",
          }}
        >
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
            <div style={{ fontSize: 13, fontWeight: 600, color: "var(--ink)" }}>Invite a team member</div>
            <button
              type="button"
              onClick={() => setShowAdd(false)}
              style={{
                background: "transparent",
                border: "none",
                color: "var(--ink-faint)",
                fontSize: 18,
                cursor: "pointer",
                lineHeight: 1,
              }}
              aria-label="Close"
            >
              ×
            </button>
          </div>
          <div style={TWO_COL}>
            <TextField
              label="First name"
              placeholder="Jordan"
              value={draft.firstName}
              onChange={(v) => setDraft((d) => ({ ...d, firstName: v }))}
            />
            <TextField
              label="Last name"
              placeholder="Reyes"
              value={draft.lastName}
              onChange={(v) => setDraft((d) => ({ ...d, lastName: v }))}
            />
          </div>
          <TextField
            label="Work email"
            placeholder="jordan@yourhealthsystem.org"
            type="email"
            value={draft.email}
            onChange={(v) => setDraft((d) => ({ ...d, email: v }))}
          />
          <SelectField
            label="Role"
            placeholder="Select a role"
            value={draft.role}
            onChange={(v) => setDraft((d) => ({ ...d, role: v as RoleLabel }))}
            options={roleOptions}
          />
          <PrimaryButton
            fullWidth
            disabled={!draftValid}
            onClick={submitDraft}
            loadingLabel="Sending invite…"
            successLabel="Invite sent ✓"
          >
            Send invitation
          </PrimaryButton>
        </div>
      )}

      <div style={{ height: 1, background: "var(--hairline)", margin: "8px 0 22px" }} />
      <PrimaryButton fullWidth onClick={onNext} loadingLabel="Finishing setup…" successLabel="Workspace ready ✓">
        {members.length === 0
          ? "Skip for now & continue"
          : `Continue with ${members.length + 1} ${members.length + 1 === 1 ? "person" : "people"}`}
      </PrimaryButton>
      <div style={CARD_FOOTER_BACK}>
        <BackLink onClick={onBack} />
      </div>
    </OnboardingCard>
  );
}

function MemberRow({ member, onRemove }: { member: Member; onRemove: () => void }) {
  const [hover, setHover] = useState(false);
  return (
    <div
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 14,
        padding: "14px 18px",
        borderRadius: 12,
        background: hover ? "var(--card-in)" : "var(--card-in)",
        border: "1px solid " + (hover ? "var(--hairline-strong)" : "var(--hairline)"),
        transition: "all 200ms cubic-bezier(0.16, 1, 0.3, 1)",
        animation: "ah-onb-fade-up 320ms cubic-bezier(0.16, 1, 0.3, 1)",
      }}
    >
      <Avatar name={`${member.firstName} ${member.lastName}`} size={42} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
          <span style={{ fontSize: 15, fontWeight: 500, color: "var(--ink)" }}>
            {member.firstName} {member.lastName}
          </span>
          <RolePill role={member.role} />
        </div>
        <div style={{ fontSize: 13, color: "var(--ink-soft)", marginTop: 3 }}>{member.email}</div>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <StatusPill status={member.status} />
        {hover && (
          <button
            type="button"
            onClick={onRemove}
            title="Remove"
            aria-label={`Remove ${member.firstName} ${member.lastName}`}
            style={{
              width: 28,
              height: 28,
              borderRadius: 8,
              background: "var(--ah-pink-wash)",
              border: "1px solid var(--ah-pink-line)",
              color: "var(--ah-pink-deep)",
              cursor: "pointer",
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        )}
      </div>
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────
   Step 5 — Sign-in
   ───────────────────────────────────────────────────────────── */

export function Step5SignIn({
  data,
  slug,
  onSignIn,
  onBack,
  error,
}: {
  data: OnboardingData;
  slug: string;
  /** Returns false to stay Idle (e.g. wrong password). */
  onSignIn: (email: string, password: string) => Promise<boolean>;
  onBack: () => void;
  error?: string;
}) {
  const [email, setEmail] = useState(data.email);
  const [pw, setPw] = useState("");
  const valid = /\S+@\S+\.\S+/.test(email) && pw.length >= 4;
  const workspaceSlug = slug || (data.orgName || "workspace").toLowerCase().replace(/\s+/g, "-");

  return (
    <OnboardingCard
      eyebrow="Step 5 of 5"
      title="Sign in to your workspace."
      lede={
        <>
          Workspace:{" "}
          <span style={{ fontFamily: "var(--mono)", color: "var(--ah-green-deep)" }}>
            {workspaceSlug}
          </span>
        </>
      }
    >
      <InlineError>{error}</InlineError>
      <TextField
        label="Email"
        placeholder="you@yourhealthsystem.org"
        type="email"
        value={email}
        onChange={setEmail}
        autoFocus
        autoComplete="username"
      />
      <TextField
        label="Password"
        placeholder="Temporary password"
        type="password"
        value={pw}
        onChange={setPw}
        hint="Use the temporary password we sent to your email — you'll change it now."
        autoComplete="current-password"
      />
      <PrimaryButton
        fullWidth
        disabled={!valid}
        onClick={() => onSignIn(email, pw)}
        loadingLabel="Signing in…"
        successLabel="Welcome ✓"
      >
        Sign in
      </PrimaryButton>
      <div style={{ textAlign: "center", marginTop: 16 }}>
        <a
          href={`/t/${encodeURIComponent(workspaceSlug)}/sign-in`}
          style={{ fontSize: 13, color: "var(--ink-soft)", textDecoration: "none" }}
        >
          Forgot password?
        </a>
      </div>
      <div style={CARD_FOOTER_BACK}>
        <BackLink onClick={onBack} />
      </div>
    </OnboardingCard>
  );
}

/* ─────────────────────────────────────────────────────────────
   Step 6 — Success
   ───────────────────────────────────────────────────────────── */

export function Step6Success({
  data,
  onOpenWorkspace,
}: {
  data: OnboardingData;
  onOpenWorkspace: () => Promise<boolean> | boolean;
}) {
  const memberCount = data.members.length;
  return (
    <OnboardingCard maxWidth={620} title="Your workspace is ready.">
      <div
        style={{
          width: 76,
          height: 76,
          borderRadius: "50%",
          margin: "0 auto 28px",
          background: "radial-gradient(circle, var(--ah-green-glow) 0%, transparent 70%)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        <div
          style={{
            width: 56,
            height: 56,
            borderRadius: "50%",
            background: "var(--green)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            boxShadow: "none",
          }}
        >
          <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="var(--card)" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="20 6 9 17 4 12" />
          </svg>
        </div>
      </div>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(3, 1fr)",
          gap: 12,
          marginBottom: 28,
        }}
      >
        <Stat label="Health system" value={data.orgName || "—"} />
        <Stat label="Department" value={data.department || "—"} />
        <Stat label="TEAM members" value={`${memberCount + 1}`} />
      </div>
      <p
        style={{
          fontSize: 14,
          color: "var(--ink-soft)",
          textAlign: "center",
          lineHeight: 1.6,
          marginTop: 0,
          marginBottom: 26,
        }}
      >
        We&apos;ve sent welcome credentials to <strong style={{ color: "var(--ink)" }}>{data.email}</strong>
        {memberCount > 0 ? (
          <>
            {" "}and to{" "}
            <strong style={{ color: "var(--ink)" }}>
              {memberCount} team member{memberCount !== 1 ? "s" : ""}
            </strong>
          </>
        ) : null}
        . You can now open your roster, send discharge materials, and start tracking episodes.
      </p>
      <PrimaryButton fullWidth onClick={onOpenWorkspace} loadingLabel="Opening…" successLabel="Opening ✓">
        Open my workspace
      </PrimaryButton>
    </OnboardingCard>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div
      style={{
        background: "var(--card-in)",
        border: "1px solid var(--hairline)",
        borderRadius: 12,
        padding: "14px 14px",
      }}
    >
      <div
        style={{
          fontSize: 10,
          fontWeight: 400,
          letterSpacing: "0.08em",
          textTransform: "uppercase",
          fontFamily: "var(--mono)",
          color: "var(--ink-faint)",
          marginBottom: 6,
        }}
      >
        {label}
      </div>
      <div
        style={{
          fontSize: 15,
          fontWeight: 500,
          color: "var(--ink)",
          fontFamily: "var(--sans)",
          letterSpacing: "-0.005em",
          lineHeight: 1.2,
          wordBreak: "break-word",
        }}
      >
        {value}
      </div>
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════════
   ASCLEPIUS (data-training product) — Steps 3–8.
   ═══════════════════════════════════════════════════════════════ */

const THREE_COL: CSSProperties = { display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 14 };

function SectionHeading({ title, sub }: { title: string; sub?: ReactNode }) {
  return (
    <div style={{ margin: "26px 0 14px" }}>
      <div
        style={{
          fontSize: 11,
          fontWeight: 400,
          letterSpacing: "0.08em",
          textTransform: "uppercase",
          fontFamily: "var(--mono)",
          color: "var(--ink-soft)",
        }}
      >
        {title}
      </div>
      {sub && <div style={{ fontSize: 12.5, color: "var(--ink-soft)", marginTop: 5 }}>{sub}</div>}
    </div>
  );
}

function RepeatableCard({
  children,
  onRemove,
  removable,
}: {
  children: ReactNode;
  onRemove?: () => void;
  removable?: boolean;
}) {
  return (
    <div
      style={{
        position: "relative",
        background: "var(--card-in)",
        border: "1px solid var(--hairline)",
        borderRadius: 12,
        padding: "16px 16px 0",
        marginBottom: 12,
        animation: "ah-onb-fade-up 280ms cubic-bezier(0.16, 1, 0.3, 1)",
      }}
    >
      {removable && (
        <button
          type="button"
          onClick={onRemove}
          aria-label="Remove"
          style={{
            position: "absolute",
            top: 10,
            right: 10,
            width: 26,
            height: 26,
            borderRadius: 8,
            background: "var(--ah-pink-wash)",
            border: "1px solid var(--ah-pink-line)",
            color: "var(--ah-pink-deep)",
            cursor: "pointer",
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            fontSize: 15,
            lineHeight: 1,
          }}
        >
          ×
        </button>
      )}
      {children}
    </div>
  );
}

function AddRowButton({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 8,
        padding: "9px 14px",
        borderRadius: 9999,
        background: "var(--ah-green-wash)",
        border: "1px dashed var(--ah-green-line)",
        color: "var(--ah-green-deep)",
        fontSize: 13,
        fontWeight: 600,
        cursor: "pointer",
        marginBottom: 6,
      }}
    >
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
        <line x1="12" y1="5" x2="12" y2="19" />
        <line x1="5" y1="12" x2="19" y2="12" />
      </svg>
      {label}
    </button>
  );
}

/* ── Step 3 — Health institution (Asclepius) ────────────────────── */

export function Step4Institution({
  data,
  setData,
  onNext,
  onBack,
  error,
}: {
  data: OnboardingData;
  setData: (patch: Partial<OnboardingData>) => void;
  onNext: () => Promise<boolean>;
  onBack: () => void;
  error?: string;
}) {
  // Everything here is optional now: specialty is captured with your credentials,
  // and a blank organization name defaults to your own name. You're signing up
  // as an individual clinician; a practice/workspace name only matters if you
  // plan to invite colleagues later.
  const valid = true;
  return (
    <OnboardingCard
      eyebrow="Step 4 of 8"
      title="Name your workspace."
      lede="You're signing up on your own today. If you'd like colleagues from your practice to join later, give your workspace a name so they land in the same place. If not, skip this."
    >
      <InlineError>{error}</InlineError>
      <TextField
        label="Practice or workspace name (optional)"
        placeholder="Northridge Nephrology"
        value={data.orgName}
        onChange={(v) => setData({ orgName: v })}
        autoFocus
        autoComplete="organization"
        hint="Skip this if you're just signing up for yourself. We'll use your name instead."
      />
      <div
        style={{
          background: "var(--ah-green-wash)",
          border: "1px solid var(--ah-green-line)",
          borderRadius: 12,
          padding: "14px 16px",
          display: "flex",
          alignItems: "center",
          gap: 12,
          marginBottom: 24,
        }}
      >
        <div
          style={{
            width: 32,
            height: 32,
            borderRadius: 9,
            background: "var(--ah-green-wash)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            flexShrink: 0,
          }}
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--ah-green-deep)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M20 7L9 18l-5-5" />
          </svg>
        </div>
        <div style={{ fontSize: 13, color: "var(--ink-soft)", lineHeight: 1.5 }}>
          You can start reviewing and labeling cases right away. If you want to bring people from your practice on board later, you'll be able to invite them from your dashboard.
        </div>
      </div>
      <PrimaryButton fullWidth disabled={!valid} onClick={onNext} loadingLabel="Saving…" successLabel="Saved ✓">
        Continue
      </PrimaryButton>
      <div style={CARD_FOOTER_BACK}>
        <BackLink onClick={onBack} />
      </div>
    </OnboardingCard>
  );
}

/* ── Step 5 — Credentials (director or invited member) ──────────── */

const DEGREE_OPTIONS = [
  { value: "MD", label: "MD" },
  { value: "DO", label: "DO" },
  { value: "MBBS", label: "MBBS" },
  { value: "Other", label: "Other" },
];

/* ── What each country's registry needs ────────────────────────────────────
   Served by GET /api/onboarding/credential-config, whose source of truth is
   backend/asclepius/registry/config.py. Fetched once when the credentials
   screen mounts rather than per country change: the whole table is a few
   kilobytes, and a round trip every time somebody scrolls the country list
   would make the form feel broken on a hotel wifi.

   The fallback below is not a copy of that table — it is the US, the path the
   large majority of signups take, so a doctor whose config request fails still
   has a working form instead of an empty picker. */
export type RegistryFieldSpec = {
  key: string;
  label: string;
  kind: "text" | "select" | "date";
  options: string[];
  required: boolean;
  hint: string;
};

export type RegistryCountry = {
  country: string;
  country_name: string;
  registry_name: string;
  id_label: string;
  id_regex: string | null;
  id_hint: string;
  method: string;
  extra_fields: RegistryFieldSpec[];
};

type CredentialConfig = {
  countries: RegistryCountry[];
  default: { id_label: string; id_hint: string; method: string };
  qualifications: string[];
};

const CREDENTIAL_CONFIG_FALLBACK: CredentialConfig = {
  countries: [{
    country: "US", country_name: "United States",
    registry_name: "NPPES (CMS National Provider Identifier)",
    id_label: "NPI number", id_regex: "^[12]\\d{9}$", id_hint: "10 digits",
    method: "api", extra_fields: [],
  }],
  default: { id_label: "Medical registration number", id_hint: "", method: "document" },
  qualifications: ["MD", "DO", "MBBS", "MBChB", "MBBCh", "BMBS", "Staatsexamen", "Other"],
};

function useCredentialConfig(): CredentialConfig {
  const [cfg, setCfg] = useState<CredentialConfig>(CREDENTIAL_CONFIG_FALLBACK);
  useEffect(() => {
    let live = true;
    fetch(`${API_BASE}/api/onboarding/credential-config`)
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (live && data && Array.isArray(data.countries) && data.countries.length) {
          setCfg(data);
        }
      })
      .catch(() => { /* the fallback is a working US form, not an error state */ });
    return () => { live = false; };
  }, []);
  return cfg;
}
const PRACTICE_SETTING_SUGGESTIONS = [
  "Academic",
  "Private practice",
  "Hospital",
  "Dialysis unit",
  "Outpatient clinic",
  "VA / government",
];
const LANGUAGE_SUGGESTIONS = ["English", "Spanish", "Mandarin", "Hindi", "Arabic", "French"];

/** The onboarding token, read from the route the wizard was opened on
 *  (`/onboard/<token>` or `/onboard/m/<token>` — see App.tsx). Taken from the
 *  URL rather than threaded through props so the CV control stays inside the
 *  onboarding step files. Returns "" when it cannot be determined, and the
 *  upload control then hides itself rather than offering an action that
 *  cannot work. */
function onboardingTokenFromLocation(): string {
  if (typeof window === "undefined") return "";
  const m = window.location.pathname.match(/\/onboard\/(?:m\/)?([^/?#]+)/);
  try {
    return m ? decodeURIComponent(m[1]) : "";
  } catch {
    return m ? m[1] : "";
  }
}

const CV_MAX_BYTES = 10 * 1024 * 1024;
const CV_ACCEPT = ".pdf,.txt,application/pdf,text/plain";

/** Optional CV upload.
 *
 *  Non-blocking on the form by construction: this control owns its own error
 *  state and never propagates failure to the step's `valid` gate, so a
 *  physician whose upload fails still completes signup with an empty field.
 *  The response deliberately carries no asset sha — the server records it
 *  against this person's row, so the client cannot name a file it did not
 *  upload. */
function CvUploadField({
  filename,
  documentRequired = false,
  onUploaded,
}: {
  filename: string;
  documentRequired?: boolean;
  onUploaded: (filename: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const inputRef = useRef<HTMLInputElement | null>(null);
  const token = onboardingTokenFromLocation();
  if (!token) return null;

  const upload = async (file: File) => {
    setProblem("");
    if (file.size > CV_MAX_BYTES) {
      setProblem("That file is over 10 MB. You can continue without it.");
      return;
    }
    setBusy(true);
    try {
      const form = new FormData();
      form.append("token", token);
      form.append("file", file);
      const res = await fetch("/api/onboarding/asclepius/cv", {
        method: "POST",
        body: form,
      });
      if (!res.ok) {
        setProblem("We couldn't attach that file. You can continue without it.");
      } else {
        onUploaded(file.name);
      }
    } catch {
      setProblem("We couldn't attach that file. You can continue without it.");
    } finally {
      setBusy(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  };

  return (
    <div style={{ marginBottom: 20 }}>
      {/* For a doctor whose national register we cannot query, this file IS
          the verification — so it is asked for by name and marked required
          rather than sitting there as an optional CV they have no reason to
          attach. Same upload, same store; only the ask changes. */}
      <FieldLabel optional={!documentRequired}>
        {documentRequired ? "Registration certificate or licence card" : "CV or résumé"}
      </FieldLabel>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 12,
          flexWrap: "wrap",
          background: "var(--card-in)",
          border: "1px solid var(--hairline)",
          borderRadius: 10,
          padding: "12px 16px",
        }}
      >
        <button
          type="button"
          onClick={() => inputRef.current?.click()}
          disabled={busy}
          style={{
            background: "transparent",
            border: "1px solid var(--hairline-strong)",
            borderRadius: 8,
            padding: "7px 14px",
            font: "inherit",
            fontSize: 14,
            color: "var(--ink)",
            cursor: busy ? "default" : "pointer",
            opacity: busy ? 0.6 : 1,
          }}
        >
          {busy ? "Attaching…" : filename ? "Replace file" : "Attach a file"}
        </button>
        <span style={{ fontSize: 13, color: "var(--ink-soft)" }}>
          {filename
            ? `${filename} attached`
            : documentRequired
              ? "PDF or image, up to 10 MB. This is how we verify you."
              : "PDF or text, up to 10 MB. Optional."}
        </span>
        <input
          ref={inputRef}
          type="file"
          accept={CV_ACCEPT}
          style={{ display: "none" }}
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) void upload(f);
          }}
        />
      </div>
      {problem && (
        <div style={{ marginTop: 8, fontSize: 13, color: "var(--ah-pink)" }}>{problem}</div>
      )}
    </div>
  );
}

export function Step5Credentials({
  data,
  setData,
  onNext,
  onBack,
  error,
  eyebrow,
  memberMode = false,
  phase,
  relaxed = false,
}: {
  data: OnboardingData;
  setData: (patch: Partial<OnboardingData>) => void;
  onNext: () => Promise<boolean>;
  onBack: () => void;
  error?: string;
  eyebrow: string;
  memberMode?: boolean;
  /** 1 = who you are, 2 = your training, 3 = what makes you rare.
   *  Omitted renders every field on one screen, which is what member mode
   *  still does: an invited clinician arrives already expecting a form. */
  phase?: 1 | 2 | 3;
  /** /join?flavor=general: the signer may not be a practicing MD, so nothing
   *  on these screens blocks. Fields stay visible for an MD who wants their
   *  credentials on file; a non-MD continues past them untouched. */
  relaxed?: boolean;
}) {
  const c = data.credentials;
  const set = (patch: Partial<Credentials>) => setData({ credentials: { ...c, ...patch } });
  const show = (n: 1 | 2 | 3) => phase === undefined || phase === n;

  // Each screen gates ONLY on what it asks for. Gating screen 1 on a licence
  // number it never showed is how a Continue button goes dead with no
  // explanation anywhere on the page.
  // A doctor licensed outside the US has no NPI and no two-letter state, and
  // requiring them was the whole reason a Saudi consultant could not finish
  // this form. What replaces them is that country's own registration number —
  // required to be present, never required to match a shape, because several
  // countries publish no format and a doctor should not lose an evening to our
  // guess about punctuation. The registry check reports; a human decides.
  const isUS = (c.countryOfLicensure || "US").toUpperCase() === "US";
  const cfg = useCredentialConfig();
  const countryOptions = cfg.countries.map((x) => ({
    value: x.country, label: x.country_name,
  }));
  const registry = cfg.countries.find(
    (x) => x.country === (c.countryOfLicensure || "US").toUpperCase(),
  ) || { ...cfg.default, country: "", country_name: "", registry_name: "",
         id_regex: null, extra_fields: [] as RegistryFieldSpec[] };
  const qualificationOptions = cfg.qualifications.map((q) => ({ value: q, label: q }));
  const registryFormatWarning =
    !isUS && registry.id_regex && c.registrationNumber.trim().length > 0 &&
    !new RegExp(registry.id_regex).test(c.registrationNumber.trim())
      ? "That does not look like the usual format — worth a second check, but you can continue."
      : "";

  const identityValid =
    c.fullLegalName.trim().length > 0 &&
    (isUS
      ? /^\d{10}$/.test(c.npi.trim())
      : c.registrationNumber.trim().length > 0) &&
    // The physician's own phone is required (PRD Phase 4). Their mobile is
    // how verification actually gets resolved when the registry is ambiguous.
    c.phone.trim().length >= 7 &&
    (isUS ? c.degree.trim().length > 0 : c.qualification.trim().length > 0) &&
    c.primarySpecialty.trim().length > 0 &&
    c.currentlyActive !== null;

  const trainingValid =
    // Gate A2 needs both halves; gate A4 needs the attestation. Required at the
    // form rather than surfaced later as an "unresolved gate" in the admin queue,
    // because a physician can answer these in five seconds and an admin cannot.
    // Outside the US registration IS the licence, and A1 already judged it, so
    // there is no second number to ask for here.
    (!isUS || (c.licenseNumber.trim().length > 0 && c.licenseState.trim().length === 2)) &&
    c.residencyCompleted !== null &&
    // AUDIT H1: required, because the alternative is inferring it from a blank or a
    // zero — and inferring "not practising" from the zero a physician on parental
    // leave types is the exact defect this field exists to close.
    c.practiceStatus !== "";

  // Screen 3 is entirely optional by design, so it never blocks. Member mode
  // (phase undefined) renders every field at once and so gates on both. A
  // relaxed (general-flavor) signup never blocks at all: the signer may not
  // be a practicing MD, and verification decides access either way.
  const valid = relaxed ? true
    : phase === 1 ? identityValid
    : phase === 2 ? trainingValid
    : phase === 3 ? true
    : identityValid && trainingValid;

  return (
    <OnboardingCard
      maxWidth={720}
      eyebrow={eyebrow}
      title={
        phase === 1 ? "Who you are."
        : phase === 2 ? "Your training."
        : phase === 3 ? "What makes you rare."
        : memberMode ? "Confirm your credentials." : "Your credentials."
      }
      lede={
        phase === 1 ? "About a minute. We use your NPI to fill in as much of the next screen as we can."
        : phase === 2 ? "Confirm what we found, and add anything we missed."
        : phase === 3 ? (
            <>
              <strong style={{ color: "var(--ink)", fontWeight: 650 }}>All optional.</strong>{" "}
              This is the screen that decides what work reaches you.
            </>
          )
        : memberMode
          ? "Your verified credentials are attached to the data you label — this is what makes it valuable. Please be accurate."
          : "As Director of Data Training, your credentials anchor the dataset your team produces."
      }
    >
      <InlineError>{error}</InlineError>

      {relaxed && show(1) && (
        <div style={RARE_INTRO}>
          <p style={{ ...RARE_BODY, margin: 0 }}>
            <strong style={RARE_STRONG}>Not a practicing physician?</strong>{" "}
            Nothing on this screen or the next blocks you. If you are an MD,
            filling these in gets your credentials verified and opens paid
            casework; otherwise just continue.
          </p>
        </div>
      )}

      {show(1) && (<>
      <TextField
        label="Full legal name"
        placeholder="Dr. Tej Patel"
        value={c.fullLegalName}
        onChange={(v) => set({ fullLegalName: v })}
      />

      {/* Where they practise and where they are licensed, asked separately
          because they routinely differ — and asked FIRST, because the answer
          decides what the rest of this screen is allowed to ask for. */}
      <div style={TWO_COL}>
        <SelectField
          label="Where do you practise?"
          placeholder="Select country"
          value={c.countryOfPractice}
          onChange={(v) => set({
            countryOfPractice: v,
            // Licensed where you practise is the common case; the field below
            // is there for everyone else.
            ...(c.countryOfLicensure === c.countryOfPractice
              ? { countryOfLicensure: v, countryOfDegree: c.countryOfDegree || v }
              : {}),
          })}
          options={countryOptions}
        />
        <SelectField
          label="Where are you licensed?"
          placeholder="Select country"
          value={c.countryOfLicensure}
          onChange={(v) => set({ countryOfLicensure: v, registrationNumber: "", registryExtras: {} })}
          options={countryOptions}
        />
      </div>

      <div style={TWO_COL}>
        {isUS ? (
          <TextField
            label="NPI number"
            placeholder="10-digit NPI"
            value={c.npi}
            onChange={(v) => set({ npi: v.replace(/\D/g, "").slice(0, 10) })}
            hint="National Provider Identifier (10 digits)."
            error={c.npi.length > 0 && !/^\d{10}$/.test(c.npi) ? "NPI must be 10 digits." : undefined}
          />
        ) : (
          <TextField
            label={registry.id_label}
            placeholder={registry.id_hint || "Registration number"}
            value={c.registrationNumber}
            onChange={(v) => set({ registrationNumber: v })}
            // The format note is advisory and shares the hint line: plenty of
            // countries publish no format, and a doctor whose real number looks
            // unusual to us still gets through. It never becomes `error`,
            // which would disable Continue.
            hint={[
              registry.registry_name
                ? `As registered with ${registry.registry_name}.`
                : "As printed on your registration certificate.",
              registryFormatWarning,
            ].filter(Boolean).join(" ")}
          />
        )}
        <SelectField
          label={isUS ? "Degree" : "Primary medical qualification"}
          placeholder="Select qualification"
          value={isUS ? c.degree : c.qualification}
          onChange={(v) => set(isUS ? { degree: v } : { qualification: v, degree: v })}
          options={isUS ? DEGREE_OPTIONS : qualificationOptions}
        />
      </div>

      {/* Whatever else this country's registry needs before it will answer —
          India cannot be searched without knowing the state council. */}
      {!isUS && registry.extra_fields.map((f) => (
        <div key={f.key}>
          {f.kind === "select" ? (
            <SelectField
              label={f.label}
              placeholder={f.hint || "Select"}
              value={c.registryExtras[f.key] || ""}
              onChange={(v) => set({ registryExtras: { ...c.registryExtras, [f.key]: v } })}
              options={f.options.map((o) => ({ value: o, label: o }))}
            />
          ) : (
            <TextField
              label={f.label}
              placeholder={f.hint || ""}
              value={c.registryExtras[f.key] || ""}
              onChange={(v) => set({ registryExtras: { ...c.registryExtras, [f.key]: v } })}
            />
          )}
        </div>
      ))}

      {/* Countries whose registers are captcha-walled, priced or simply absent
          are verified from the certificate instead. Say so plainly here rather
          than letting the doctor wonder why nothing happened. */}
      {!isUS && registry.method === "document" && (
        <div style={RARE_INTRO}>
          <p style={{ ...RARE_BODY, margin: 0 }}>
            <strong style={RARE_STRONG}>We verify {registry.country_name || "your country"} by document.</strong>{" "}
            {registry.registry_name} has no register we can check automatically, so
            upload your registration certificate or licence card on the next screen
            and a person here reads it. It does not slow your account down.
          </p>
        </div>
      )}

      <TextField
        label="Primary specialty"
        placeholder="Nephrology"
        value={c.primarySpecialty}
        onChange={(v) => set({ primarySpecialty: v })}
      />

      {/* ── Contact & corroboration (PRD-B Seam 4) ──────────────────────────
          These are the physician's OWN details. `data.phone` elsewhere in this
          wizard is the organization's front-office line — a different field
          for a different human. Do not merge them. */}
      <div style={TWO_COL}>
        <TextField
          label="Your mobile number"
          placeholder="+1 (555) 010-7788"
          type="tel"
          value={c.phone}
          onChange={(v) => set({ phone: v })}
          hint="Direct line for you — not your practice's main number."
          error={
            c.phone.trim().length > 0 && c.phone.trim().length < 7
              ? "Enter a reachable phone number."
              : undefined
          }
        />
      </div>

      <YesNoToggle
        label="Currently in active practice?"
        value={c.currentlyActive}
        onChange={(v) => set({ currentlyActive: v })}
      />

      </>)}

      {show(2) && (<>
      {/* Board certifications */}
      <SectionHeading
        title="Board certifications"
        sub="Board + specialty + subspecialty + active status."
      />
      {c.boardCertifications.map((bc, i) => (
        <RepeatableCard
          key={i}
          removable={c.boardCertifications.length > 1}
          onRemove={() =>
            set({ boardCertifications: c.boardCertifications.filter((_, j) => j !== i) })
          }
        >
          <TextField
            label="Board"
            placeholder="American Board of Internal Medicine"
            value={bc.board}
            onChange={(v) => {
              const next = [...c.boardCertifications];
              next[i] = { ...bc, board: v };
              set({ boardCertifications: next });
            }}
          />
          <div style={TWO_COL}>
            <TextField
              label="Specialty"
              placeholder="Internal Medicine"
              value={bc.specialty}
              onChange={(v) => {
                const next = [...c.boardCertifications];
                next[i] = { ...bc, specialty: v };
                set({ boardCertifications: next });
              }}
            />
            <TextField
              label="Subspecialty"
              placeholder="Nephrology"
              value={bc.subspecialty}
              onChange={(v) => {
                const next = [...c.boardCertifications];
                next[i] = { ...bc, subspecialty: v };
                set({ boardCertifications: next });
              }}
            />
          </div>
          <YesNoToggle
            label="Currently active / valid?"
            value={bc.active}
            onChange={(v) => {
              const next = [...c.boardCertifications];
              next[i] = { ...bc, active: v };
              set({ boardCertifications: next });
            }}
          />
        </RepeatableCard>
      ))}
      <AddRowButton
        label="Add board certification"
        onClick={() =>
          set({
            boardCertifications: [
              ...c.boardCertifications,
              { board: "", specialty: "", subspecialty: "", active: true },
            ],
          })
        }
      />

      {/* Fellowship */}
      <SectionHeading title="Fellowship" sub="Institution + specialty + year." />
      {c.fellowship.map((f, i) => (
        <RepeatableCard
          key={i}
          removable={c.fellowship.length > 1}
          onRemove={() => set({ fellowship: c.fellowship.filter((_, j) => j !== i) })}
        >
          <div style={THREE_COL}>
            <TextField
              label="Institution"
              placeholder="Cedars-Sinai"
              value={f.institution}
              onChange={(v) => {
                const next = [...c.fellowship];
                next[i] = { ...f, institution: v };
                set({ fellowship: next });
              }}
            />
            <TextField
              label="Specialty"
              placeholder="Nephrology"
              value={f.specialty}
              onChange={(v) => {
                const next = [...c.fellowship];
                next[i] = { ...f, specialty: v };
                set({ fellowship: next });
              }}
            />
            <TextField
              label="Year"
              placeholder="2013"
              value={f.year}
              onChange={(v) => {
                const next = [...c.fellowship];
                next[i] = { ...f, year: v.replace(/\D/g, "").slice(0, 4) };
                set({ fellowship: next });
              }}
            />
          </div>
        </RepeatableCard>
      ))}
      <AddRowButton
        label="Add fellowship"
        onClick={() => set({ fellowship: [...c.fellowship, { institution: "", specialty: "", year: "" }] })}
      />

      {/* Residency */}
      <SectionHeading
        title="Residency"
        sub="Institution + year. Still in training? Put the year you expect to finish."
      />
      {c.residency.map((r, i) => (
        <RepeatableCard
          key={i}
          removable={c.residency.length > 1}
          onRemove={() => set({ residency: c.residency.filter((_, j) => j !== i) })}
        >
          <div style={TWO_COL}>
            <TextField
              label="Institution"
              placeholder="Johns Hopkins"
              value={r.institution}
              onChange={(v) => {
                const next = [...c.residency];
                next[i] = { ...r, institution: v };
                set({ residency: next });
              }}
            />
            <TextField
              label="Year"
              placeholder="2010"
              value={r.year}
              onChange={(v) => {
                const next = [...c.residency];
                next[i] = { ...r, year: v.replace(/\D/g, "").slice(0, 4) };
                set({ residency: next });
              }}
            />
          </div>
        </RepeatableCard>
      ))}
      <AddRowButton
        label="Add residency"
        onClick={() => set({ residency: [...c.residency, { institution: "", year: "" }] })}
      />

      {/* Medical school is DELIBERATELY NOT COLLECTED — see the `medicalSchool`
          removal note on the Credentials type. It satisfies no gate, and both the
          institution and the graduation year are on the never-collect list. */}

      {/* Reviewer eligibility (PRD C §2 gates A2/A4 and §3.2 features).
          Each field below feeds exactly one gate or one binary feature, and none
          of them is stored as a magnitude. */}
      <SectionHeading
        title="Licence & practice"
        sub="What we verify. Nothing here is scored by seniority."
      />
      <div style={TWO_COL}>
        <TextField
          label="State licence number"
          placeholder="MD-99881"
          value={c.licenseNumber}
          onChange={(v) => set({ licenseNumber: v })}
          hint="Cross-checked against your NPPES record."
        />
        <TextField
          label="Licence state"
          placeholder="MA"
          value={c.licenseState}
          onChange={(v) => set({ licenseState: v.toUpperCase().slice(0, 2) })}
        />
      </div>

      <YesNoToggle
        label="Have you finished residency?"
        value={c.residencyCompleted}
        onChange={(v) => set({ residencyCompleted: v })}
      />
      <TextField
        label={c.residencyCompleted === false
          ? "Year you expect to finish"
          : "Year you finished residency"}
        placeholder={c.residencyCompleted === false ? "2028" : "2010"}
        value={c.residencyCompletionYear}
        onChange={(v) => set({ residencyCompletionYear: v.replace(/\D/g, "").slice(0, 4) })}
        hint={c.residencyCompleted === false
          ? "A future year is expected here. Residents and fellows are welcome; "
            + "put down when you expect to finish."
          : "Used once, as a yes/no: at least three years post-residency. It is "
            + "never scored as a number of years."}
      />

      <SelectField
        label="Current practice status"
        placeholder="Select status"
        value={c.practiceStatus}
        onChange={(v) => set({ practiceStatus: v as Credentials["practiceStatus"] })}
        options={PRACTICE_STATUS_OPTIONS}
      />
      {c.practiceStatus === "on_leave" ? (
        <TextField
          label="Clinical half-days per month before your leave"
          optional
          placeholder="8"
          value={c.halfDaysBeforeLeave}
          onChange={(v) => set({ halfDaysBeforeLeave: v.replace(/\D/g, "").slice(0, 3) })}
          hint="We score your practice as it stood before the leave. Leave is not counted against you, and leaving this blank does not count as zero."
        />
      ) : (
        <TextField
          label="Clinical half-days per month"
          optional={c.practiceStatus === "not_practising"}
          placeholder="8"
          value={c.clinicalHalfDaysPerMonth}
          onChange={(v) => set({ clinicalHalfDaysPerMonth: v.replace(/\D/g, "").slice(0, 3) })}
          hint="Averaged over the last 12 months. Part-time practice counts — this is not a threshold you either clear or fail."
        />
      )}
      </>)}

      {show(3) && (<>
      <div style={RARE_INTRO}>
        <div style={RARE_EYEBROW}>Every answer here raises what we can pay you</div>
        <p style={RARE_BODY}>
          <strong style={RARE_STRONG}>All optional, and you can add them later.</strong>{" "}
          We ask because this is how work finds you. A physician who reads French gets
          the French cases. A paediatric nephrologist gets paediatric nephrology.{" "}
          <strong style={RARE_STRONG}>
            Specialist and multilingual work pays materially more than general review
          </strong>
          , and we can only route it to you if we know it about you. Every field you
          fill makes your profile stronger.
        </p>
      </div>

      <TextField
        label="LinkedIn profile"
        optional
        placeholder="linkedin.com/in/yourname"
        value={c.linkedinUrl}
        onChange={(v) => set({ linkedinUrl: v })}
        hint="Helps us confirm who you are faster, which shortens the wait."
      />

      <TextField
        label="Health system or practice"
        optional
        placeholder="Northridge Nephrology Associates"
        value={c.healthSystem}
        onChange={(v) => set({ healthSystem: v })}
        hint="Institution-linked work is some of the best paid we route."
      />

      <CvUploadField
        filename={c.cvFilename}
        documentRequired={!isUS && registry.method === "document"}
        onUploaded={(filename) => set({ cvFilename: filename })}
      />

      <YesNoToggle
        label="Participating in continuing certification (MOC/CC)?"
        value={c.continuingCertification}
        onChange={(v) => set({ continuingCertification: v })}
      />

      <ChipMultiSelect
        label="Structured review experience"
        value={c.structuredReviewExperience}
        onChange={(v) => set({ structuredReviewExperience: v })}
        placeholder="Select all that apply"
        suggestions={STRUCTURED_REVIEW_SUGGESTIONS}
        hint="Adjudicating against a rubric is the skill this work needs, and it is learned in these rooms."
      />

      {/* Focus areas */}
      <SectionHeading title="Clinical focus" />
      <ChipMultiSelect
        label="Subspecialty & focus areas"
        value={c.subspecialties}
        onChange={(v) => set({ subspecialties: v })}
        placeholder="e.g. dialysis, transplant, CKD"
        suggestions={["Dialysis", "Transplant", "Glomerular disease", "CKD", "Hypertension"]}
        hint="Type and press Enter, or tap a suggestion. Select as many as apply."
      />
      <TextArea
        label="Tell us more about your specialty"
        optional
        rows={4}
        value={c.specialtyNiche}
        onChange={(v) => set({ specialtyNiche: v })}
        placeholder={nichePlaceholder(c.primarySpecialty)}
        hint="Optional. The more specific you are about your niche and the case types you handle, the better we can match you to relevant work."
      />
      <ChipMultiSelect
        label="Practice setting"
        value={c.practiceSettings}
        onChange={(v) => set({ practiceSettings: v })}
        placeholder="e.g. academic, private practice"
        suggestions={PRACTICE_SETTING_SUGGESTIONS}
      />
      <ChipMultiSelect
        label="Languages spoken"
        value={c.languages}
        onChange={(v) => set({ languages: v })}
        placeholder="List all languages"
        suggestions={LANGUAGE_SUGGESTIONS}
      />

      </>)}

      <div style={{ height: 1, background: "var(--hairline)", margin: "8px 0 22px" }} />
      <PrimaryButton fullWidth disabled={!valid} onClick={onNext} loadingLabel="Saving…" successLabel="Saved ✓">
        {phase === 3 ? "Finish and continue" : "Continue"}
      </PrimaryButton>
      {phase === 3 && (
        <button type="button" style={SKIP_LINK} onClick={onNext}>
          Skip for now, add these from my profile later
        </button>
      )}
      <div style={CARD_FOOTER_BACK}>
        <BackLink onClick={onBack} />
      </div>
    </OnboardingCard>
  );
}

/* ── Step 6 — Attestations & rights ─────────────────────────────── */

function CheckRow({
  checked,
  onToggle,
  title,
  body,
}: {
  checked: boolean;
  onToggle: () => void;
  title: string;
  body: string;
}) {
  return (
    <button
      type="button"
      onClick={onToggle}
      style={{
        width: "100%",
        textAlign: "left",
        display: "flex",
        gap: 14,
        padding: "16px 18px",
        borderRadius: 12,
        background: checked ? "var(--ah-green-wash)" : "var(--card-in)",
        border: "1px solid " + (checked ? "var(--ah-green-line)" : "var(--hairline)"),
        marginBottom: 12,
        cursor: "pointer",
        transition: "all 200ms cubic-bezier(0.16, 1, 0.3, 1)",
      }}
    >
      <span
        style={{
          flexShrink: 0,
          marginTop: 1,
          width: 22,
          height: 22,
          borderRadius: 7,
          border: "1.5px solid " + (checked ? "var(--green)" : "var(--hairline-strong)"),
          background: checked ? "var(--green)" : "transparent",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        {checked && (
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="var(--card)" strokeWidth="3.5" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="20 6 9 17 4 12" />
          </svg>
        )}
      </span>
      <span>
        <span style={{ display: "block", fontSize: 14.5, fontWeight: 600, color: "var(--ink)", marginBottom: 4 }}>
          {title}
        </span>
        <span style={{ display: "block", fontSize: 13, lineHeight: 1.55, color: "var(--ink-soft)" }}>
          {body}
        </span>
      </span>
    </button>
  );
}

export function Step6Attestations({
  data,
  setData,
  onNext,
  onBack,
  error,
  eyebrow,
  finishLabel,
}: {
  data: OnboardingData;
  setData: (patch: Partial<OnboardingData>) => void;
  onNext: () => Promise<boolean>;
  onBack: () => void;
  error?: string;
  eyebrow: string;
  finishLabel: string;
}) {
  const a = data.attestations;
  const set = (patch: Partial<Attestations>) => setData({ attestations: { ...a, ...patch } });
  const initials = a.signedInitials.trim();
  const allChecked =
    a.consentCredentialShare && a.attestIndependentJudgment && a.ipAssignment && a.noPhi &&
    a.attestConfidentiality && a.attestNoDisciplinaryAction && a.attestWorkQuality;
  const valid = allChecked && initials.length >= 2;
  const setAll = (checked: boolean) =>
    set({
      consentCredentialShare: checked,
      attestIndependentJudgment: checked,
      ipAssignment: checked,
      noPhi: checked,
      attestWorkQuality: checked,
      attestConfidentiality: checked,
      attestNoDisciplinaryAction: checked,
    });

  return (
    <OnboardingCard
      maxWidth={680}
      eyebrow={eyebrow}
      title="Attestations & rights."
      lede="A few legal must-haves before you label data. Read each, then sign with your initials."
    >
      <InlineError>{error}</InlineError>

      {/* One tap for a reader who has read the six and agrees to all of them.
          The initials signature below is still the actual act of signing. */}
      <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 12 }}>
        <button
          type="button"
          onClick={() => setAll(!allChecked)}
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 8,
            padding: "8px 14px",
            borderRadius: 999,
            border: "1px solid " + (allChecked ? "var(--ah-green-line)" : "var(--hairline-strong)"),
            background: allChecked ? "var(--ah-green-wash)" : "var(--card-in)",
            color: "var(--ink)",
            fontSize: 13,
            fontWeight: 600,
            cursor: "pointer",
            transition: "all 200ms cubic-bezier(0.16, 1, 0.3, 1)",
          }}
        >
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke={allChecked ? "var(--green)" : "var(--ink-faint)"} strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <polyline points="20 6 9 17 4 12" />
          </svg>
          {allChecked ? "Clear all six" : "Agree to all six"}
        </button>
      </div>

      <CheckRow
        checked={a.consentCredentialShare}
        onToggle={() => set({ consentCredentialShare: !a.consentCredentialShare })}
        title="Consent to attach my credentials"
        body="I consent to attaching my verified credential metadata to the records I label and sharing it with data buyers."
      />
      <CheckRow
        checked={a.attestIndependentJudgment}
        onToggle={() => set({ attestIndependentJudgment: !a.attestIndependentJudgment })}
        title="Independent professional judgment"
        body="I attest that my labels reflect my own independent professional judgment as a licensed clinician."
      />
      <CheckRow
        checked={a.attestWorkQuality}
        onToggle={() => set({ attestWorkQuality: !a.attestWorkQuality })}
        title="Work is paid when it meets the rubric"
        body="I understand that each case is reviewed, and that work which does not follow the rubric, or is rushed or incomplete, may not be paid. If a case is not paid I will be told which one and why, and I can ask for it to be looked at again."
      />
      <CheckRow
        checked={a.ipAssignment}
        onToggle={() => set({ ipAssignment: !a.ipAssignment })}
        title="IP assignment / license grant"
        body="I assign / grant a license for the labels I produce so they may be packaged and sold as training data."
      />
      <CheckRow
        checked={a.noPhi}
        onToggle={() => set({ noPhi: !a.noPhi })}
        title="No PHI"
        body="I confirm I will not enter any patient health information (PHI) into Archangel."
      />
      {/* Gate A7. Independence is attested above; confidentiality is its own
          promise and its own checkbox, because they can be breached separately. */}
      <CheckRow
        checked={a.attestConfidentiality}
        onToggle={() => set({ attestConfidentiality: !a.attestConfidentiality })}
        title="Confidentiality"
        body="I will keep the cases, prompts, model outputs and other physicians' work I see confidential, and will not reproduce or republish them."
      />
      {/* Gate A6. Declining is a definitive answer, not a blank — which is why
          this is a checkbox and not an optional field: the gate distinguishes
          'disclosed an action' from 'never asked'. */}
      <CheckRow
        checked={a.attestNoDisciplinaryAction}
        onToggle={() =>
          set({ attestNoDisciplinaryAction: !a.attestNoDisciplinaryAction })
        }
        title="No active board disciplinary action"
        body="I attest that I am not currently subject to an active disciplinary action by any state medical board, and that my licence is active and unrestricted."
      />

      <div style={{ marginTop: 18 }}>
        <FieldLabel>Sign with your initials</FieldLabel>
        <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
          <div style={{ width: 140 }}>
            <TextField
              label={undefined}
              placeholder="T.P."
              value={a.signedInitials}
              onChange={(v) => set({ signedInitials: v.toUpperCase().slice(0, 8) })}
            />
          </div>
          <div
            style={{
              flex: 1,
              fontFamily: "var(--sans)",
              fontSize: 30,
              color: initials ? "var(--ah-green-deep)" : "var(--ink-faint)",
              borderBottom: "1px solid var(--hairline-strong)",
              paddingBottom: 8,
              minHeight: 44,
              letterSpacing: "0.08em",
            }}
          >
            {initials || "—"}
          </div>
        </div>
        <div style={{ fontSize: 12, color: "var(--ink-soft)", marginTop: 8 }}>
          Typing your initials constitutes your electronic signature on the attestations above.
        </div>
      </div>

      <div style={{ height: 1, background: "var(--hairline)", margin: "22px 0" }} />
      <PrimaryButton
        fullWidth
        disabled={!valid}
        onClick={onNext}
        loadingLabel="Submitting…"
        successLabel="Signed ✓"
      >
        {finishLabel}
      </PrimaryButton>
      <div style={CARD_FOOTER_BACK}>
        <BackLink onClick={onBack} />
      </div>
    </OnboardingCard>
  );
}

/* ── Step 7 — Add your team (Asclepius) ─────────────────────────── */

export function Step7AsclepiusTeam({
  data,
  onAddMember,
  onRemoveMember,
  onNext,
  onBack,
  error,
}: {
  data: OnboardingData;
  onAddMember: (m: Omit<AsclepiusMember, "id" | "status">) => Promise<boolean>;
  onRemoveMember: (id: number) => void;
  onNext: () => Promise<boolean>;
  onBack: () => void;
  error?: string;
}) {
  const [showAdd, setShowAdd] = useState(false);
  const [draft, setDraft] = useState<{ firstName: string; lastName: string; email: string; role: AsclepiusRole | "" }>({
    firstName: "",
    lastName: "",
    email: "",
    role: "",
  });

  const directorName = `${data.firstName || "You"} ${data.lastName || ""}`.trim();
  const members = data.ascMembers;
  const teamFull = members.length >= ASCLEPIUS_TEAM_CAP;

  const roleOptions = (Object.keys(ASCLEPIUS_ROLE_LABELS) as AsclepiusRole[]).map((r) => ({
    value: r,
    label: ASCLEPIUS_ROLE_LABELS[r],
  }));

  const draftValid =
    draft.firstName.trim() &&
    draft.lastName.trim() &&
    /\S+@\S+\.\S+/.test(draft.email) &&
    draft.role !== "" &&
    !teamFull;

  const submitDraft = async () => {
    if (!draftValid) return false;
    const ok = await onAddMember({
      firstName: draft.firstName.trim(),
      lastName: draft.lastName.trim(),
      email: draft.email.trim(),
      role: draft.role as AsclepiusRole,
    });
    if (ok) {
      setDraft({ firstName: "", lastName: "", email: "", role: "" });
      window.setTimeout(() => setShowAdd(false), 600);
    }
    return ok;
  };

  return (
    <OnboardingCard
      maxWidth={720}
      eyebrow="Step 7 of 7"
      title="Add your team."
      lede="Invite the clinicians who'll label data with you. Each gets a link to set up their own credentials. You can add up to 10."
    >
      <InlineError>{error}</InlineError>

      {/* Director card */}
      <div
        style={{
          background: "var(--card)",
          border: "1px solid var(--ah-green-line)",
          borderRadius: 14,
          padding: "20px 22px",
          marginBottom: 22,
          position: "relative",
          boxShadow: "var(--shadow-card)",
        }}
      >
        <div
          style={{
            position: "absolute",
            top: -10,
            left: 22,
            background: "var(--card)",
            padding: "0 10px",
            fontSize: 10,
            fontWeight: 400,
            letterSpacing: "0.08em",
            textTransform: "uppercase",
            fontFamily: "var(--mono)",
            color: "var(--ah-green-deep)",
          }}
        >
          Director of Data Training
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          <Avatar name={directorName} email={data.email} size={52} you />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4, flexWrap: "wrap" }}>
              <h3
                style={{
                  fontFamily: "var(--sans)",
                  fontSize: 20,
                  fontWeight: 500,
                  letterSpacing: "-0.01em",
                  color: "var(--ink)",
                  margin: 0,
                }}
              >
                {directorName || "You"}
              </h3>
              <StatusPill status="You" />
            </div>
            <div
              style={{
                fontSize: 13,
                color: "var(--ink-soft)",
                display: "flex",
                alignItems: "center",
                gap: 10,
                flexWrap: "wrap",
              }}
            >
              <span>{data.email}</span>
              <span style={{ width: 3, height: 3, borderRadius: "50%", background: "var(--ah-faint-30)" }} />
              <span>{data.orgName || "—"}</span>
              <span style={{ width: 3, height: 3, borderRadius: "50%", background: "var(--ah-faint-30)" }} />
              <span>{data.specialty || "—"}</span>
            </div>
          </div>
        </div>
      </div>

      {/* Members header */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 14 }}>
        <div>
          <div
            style={{
              fontSize: 11,
              fontWeight: 400,
              letterSpacing: "0.08em",
              textTransform: "uppercase",
              fontFamily: "var(--mono)",
              color: "var(--ink-faint)",
              marginBottom: 4,
            }}
          >
            Team members
          </div>
          <div style={{ fontSize: 13, color: "var(--ink-soft)" }}>
            {teamFull ? (
              <span style={{ color: "var(--ah-green-deep)", fontWeight: 600 }}>Team is full (10 invited).</span>
            ) : (
              <>
                <strong style={{ color: "var(--ink)" }}>{members.length}</strong> of {ASCLEPIUS_TEAM_CAP} invited
              </>
            )}
          </div>
        </div>
        {!showAdd && !teamFull && (
          <button
            type="button"
            onClick={() => setShowAdd(true)}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 8,
              padding: "9px 14px",
              borderRadius: 9999,
              background: "var(--ah-green-wash)",
              border: "1px solid var(--ah-green-line)",
              color: "var(--ah-green-deep)",
              fontSize: 13,
              fontWeight: 600,
              cursor: "pointer",
            }}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <line x1="12" y1="5" x2="12" y2="19" />
              <line x1="5" y1="12" x2="19" y2="12" />
            </svg>
            Add member
          </button>
        )}
      </div>

      {/* Members list */}
      <div style={{ display: "grid", gap: 10, marginBottom: showAdd ? 22 : 28 }}>
        {members.length === 0 && !showAdd && (
          <div
            style={{
              border: "1px dashed var(--hairline-strong)",
              borderRadius: 12,
              padding: "28px 20px",
              textAlign: "center",
              color: "var(--ink-faint)",
              fontSize: 13,
            }}
          >
            Click <strong style={{ color: "var(--ah-green-deep)" }}>Add member</strong> to invite the clinicians on your team — or skip and add them later.
          </div>
        )}
        {members.map((m) => (
          <AsclepiusMemberRow key={m.id} member={m} onRemove={() => onRemoveMember(m.id)} />
        ))}
      </div>

      {/* Add panel */}
      {showAdd && (
        <div
          style={{
            background: "var(--card-in)",
            border: "1px solid var(--ah-green-line)",
            borderRadius: 14,
            padding: "20px 22px",
            marginBottom: 22,
            animation: "ah-onb-fade-up 320ms cubic-bezier(0.16, 1, 0.3, 1)",
          }}
        >
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
            <div style={{ fontSize: 13, fontWeight: 600, color: "var(--ink)" }}>Invite a team member</div>
            <button
              type="button"
              onClick={() => setShowAdd(false)}
              style={{
                background: "transparent",
                border: "none",
                color: "var(--ink-faint)",
                fontSize: 18,
                cursor: "pointer",
                lineHeight: 1,
              }}
              aria-label="Close"
            >
              ×
            </button>
          </div>
          <div style={TWO_COL}>
            <TextField
              label="First name"
              placeholder="Nina"
              value={draft.firstName}
              onChange={(v) => setDraft((d) => ({ ...d, firstName: v }))}
            />
            <TextField
              label="Last name"
              placeholder="Lee"
              value={draft.lastName}
              onChange={(v) => setDraft((d) => ({ ...d, lastName: v }))}
            />
          </div>
          <TextField
            label="Work email"
            placeholder="nina@yourorg.org"
            type="email"
            value={draft.email}
            onChange={(v) => setDraft((d) => ({ ...d, email: v }))}
          />
          <SelectField
            label="Role"
            placeholder="Select a role"
            value={draft.role}
            onChange={(v) => setDraft((d) => ({ ...d, role: v as AsclepiusRole }))}
            options={roleOptions}
          />
          <PrimaryButton
            fullWidth
            disabled={!draftValid}
            onClick={submitDraft}
            loadingLabel="Sending invite…"
            successLabel="Invite sent ✓"
          >
            Send invitation
          </PrimaryButton>
        </div>
      )}

      <div style={{ height: 1, background: "var(--hairline)", margin: "8px 0 22px" }} />
      <PrimaryButton fullWidth onClick={onNext} loadingLabel="Finishing setup…" successLabel="Workspace ready ✓">
        {members.length === 0 ? "Skip for now & finish" : `Finish with ${members.length} invited`}
      </PrimaryButton>
      <div style={CARD_FOOTER_BACK}>
        <BackLink onClick={onBack} />
      </div>
    </OnboardingCard>
  );
}

function AsclepiusMemberRow({
  member,
  onRemove,
}: {
  member: AsclepiusMember;
  onRemove: () => void;
}) {
  const [hover, setHover] = useState(false);
  return (
    <div
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 14,
        padding: "14px 18px",
        borderRadius: 12,
        background: hover ? "var(--card-in)" : "var(--card-in)",
        border: "1px solid " + (hover ? "var(--hairline-strong)" : "var(--hairline)"),
        transition: "all 200ms cubic-bezier(0.16, 1, 0.3, 1)",
        animation: "ah-onb-fade-up 320ms cubic-bezier(0.16, 1, 0.3, 1)",
      }}
    >
      <Avatar name={`${member.firstName} ${member.lastName}`} size={42} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
          <span style={{ fontSize: 15, fontWeight: 500, color: "var(--ink)" }}>
            {member.firstName} {member.lastName}
          </span>
          <RolePill role={ASCLEPIUS_ROLE_LABELS[member.role]} />
        </div>
        <div style={{ fontSize: 13, color: "var(--ink-soft)", marginTop: 3 }}>{member.email}</div>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <StatusPill status={member.status} />
        {hover && (
          <button
            type="button"
            onClick={onRemove}
            title="Remove"
            aria-label={`Remove ${member.firstName} ${member.lastName}`}
            style={{
              width: 28,
              height: 28,
              borderRadius: 8,
              background: "var(--ah-pink-wash)",
              border: "1px solid var(--ah-pink-line)",
              color: "var(--ah-pink-deep)",
              cursor: "pointer",
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        )}
      </div>
    </div>
  );
}

/* ── Step 8 — Workspace ready (Asclepius) ───────────────────────── */

export function Step8AsclepiusSuccess({
  data,
  onOpenWorkspace,
  memberMode = false,
  kind = "physician",
}: {
  data: OnboardingData;
  onOpenWorkspace: () => Promise<boolean> | boolean;
  memberMode?: boolean;
  kind?: SignupKind;
}) {
  // Three different accounts land here, and the physician's copy is wrong for
  // the other two: an advisor has no workspace and a referral partner has no
  // specialty. Say what each of them actually got.
  const TITLE: Record<SignupKind, string> = {
    physician: "Your workspace is ready.",
    advisor: "You're in.",
    referrer: "Your referral link is ready.",
  };
  const CTA: Record<SignupKind, string> = {
    physician: "Open my dashboard →",
    advisor: "Look around →",
    referrer: "Open my referral page →",
  };
  const CLOSING: Record<SignupKind, string> = {
    physician:
      "You're already signed in. Your login details are in that email whenever you need them.",
    advisor:
      "You're signed in with view-only access: you can read the community and click through a practice case, and the referral page is yours to use.",
    referrer:
      "You're signed in. Your referral page has your link and shows who has signed up through it.",
  };
  return (
    <OnboardingCard maxWidth={620} title={TITLE[kind]}>
      {/* Star-the-email banner */}
      <div
        style={{
          display: "flex",
          gap: 12,
          alignItems: "flex-start",
          background: "var(--ah-lime-wash)",
          border: "1px solid var(--ah-lime-line)",
          borderRadius: 12,
          padding: "14px 16px",
          marginBottom: 26,
        }}
      >
        <span
          style={{
            width: 8,
            height: 8,
            borderRadius: "50%",
            background: "var(--lime)",
            flexShrink: 0,
            marginTop: 6,
          }}
          aria-hidden="true"
        />
        <div style={{ fontSize: 13.5, lineHeight: 1.55, color: "var(--ink-soft)" }}>
          You're signed in and ready to go. We also emailed <strong style={{ color: "var(--ink)" }}>{data.email}</strong> your
          login details so you can sign back in any time.
        </div>
      </div>

      <div
        style={{
          width: 76,
          height: 76,
          borderRadius: "50%",
          margin: "0 auto 28px",
          background: "radial-gradient(circle, var(--ah-green-glow) 0%, transparent 70%)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        <div
          style={{
            width: 56,
            height: 56,
            borderRadius: "50%",
            background: "var(--green)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            boxShadow: "none",
          }}
        >
          <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="var(--card)" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="20 6 9 17 4 12" />
          </svg>
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 12, marginBottom: 28 }}>
        <Stat
          label={kind === "physician" ? "Workspace" : "Account"}
          value={
            kind === "physician"
              ? data.orgName || `${data.firstName} ${data.lastName}`.trim() || "Your workspace"
              : `${data.firstName} ${data.lastName}`.trim() || data.email
          }
        />
        {kind === "physician" ? (
          <Stat label="Specialty" value={data.credentials.primarySpecialty || "Not set"} />
        ) : (
          <Stat label="Access" value={kind === "advisor" ? "View only" : "Referral"} />
        )}
        <Stat
          label="Your role"
          value={
            kind === "advisor"
              ? "Advisor"
              : kind === "referrer"
                ? "Referral partner"
                : memberMode
                  ? data.roleLabel || "Clinician"
                  : "Director"
          }
        />
      </div>

      <p
        style={{
          fontSize: 14,
          color: "var(--ink-soft)",
          textAlign: "center",
          lineHeight: 1.6,
          marginTop: 0,
          marginBottom: 26,
        }}
      >
        {CLOSING[kind]}
      </p>

      <PrimaryButton fullWidth onClick={onOpenWorkspace} loadingLabel="Opening…" successLabel="Opening ✓">
        {CTA[kind]}
      </PrimaryButton>
    </OnboardingCard>
  );
}

/* ─────────────────────────────────────────────────────────────
   StepChoosePassword — the physician picks their own credential.

   This sits immediately after the OTP, never on the credentials screen.
   Two reasons: member mode verifies LAST, so capturing a password earlier
   would let a typo'd address end up with an account whose credential was
   chosen by someone who cannot receive its mail; and the credentials screen
   is the longest in the flow, where a 400 on any field would re-render and
   clear the pair.

   Requirements are shown as a live checklist rather than enforced by a
   regex error after the fact. Composition rules are deliberately absent:
   "one symbol and one digit" reliably produces Password1! and nothing safer.
   ───────────────────────────────────────────────────────────── */

const PASSWORD_MIN = 12;

export function StepChoosePassword({
  data,
  onSubmit,
  onBack,
  error,
  eyebrow = "Step 3",
}: {
  data: OnboardingData;
  /** POST /api/onboarding/asclepius/password (or /member/password). Resolve false to stay put. */
  onSubmit: (password: string) => Promise<boolean>;
  onBack: () => void;
  error?: string;
  eyebrow?: string;
}) {
  const [pw, setPw] = useState("");
  const [confirm, setConfirm] = useState("");
  const [touched, setTouched] = useState(false);

  const longEnough = pw.length >= PASSWORD_MIN;
  const notEmail = !!pw && pw.toLowerCase() !== (data.email || "").toLowerCase();
  const varied = new Set(pw).size >= 5;
  const matches = !!pw && pw === confirm;
  const valid = longEnough && notEmail && varied && matches;

  const check = (ok: boolean, label: string) => (
    <li
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        color: ok ? "var(--ah-green-deep)" : "var(--ink-faint)",
        margin: "4px 0",
        fontSize: "0.85rem",
      }}
    >
      <span aria-hidden="true" style={{ width: 12 }}>{ok ? "✓" : "·"}</span>
      {label}
    </li>
  );

  return (
    <OnboardingCard
      eyebrow={eyebrow}
      title="Choose your password."
      lede={
        <>
          You&apos;ll use this with <span style={{ color: "var(--ah-green-deep)" }}>{data.email}</span> to
          sign in. We won&apos;t email it to you, and you can change or reset it any time.
        </>
      }
    >
      <InlineError>{error}</InlineError>

      <FieldLabel>Password</FieldLabel>
      <TextField
        type="password"
        value={pw}
        onChange={setPw}
        onBlur={() => setTouched(true)}
        autoComplete="new-password"
        placeholder="At least 12 characters"
      />

      <div style={{ marginTop: 10 }}>
        <FieldLabel>Confirm password</FieldLabel>
        <TextField
          type="password"
          value={confirm}
          onChange={setConfirm}
          autoComplete="new-password"
          placeholder="Type it again"
        />
      </div>

      <ul style={{ listStyle: "none", padding: 0, margin: "14px 0 0" }}>
        {check(longEnough, `${PASSWORD_MIN} characters or more`)}
        {check(varied, "A mix of characters, not one repeated")}
        {check(notEmail, "Not your email address")}
        {check(matches, "Both entries match")}
      </ul>

      <div style={{ marginTop: 22 }}>
        <PrimaryButton
          disabled={!valid}
          onClick={async () => {
            setTouched(true);
            if (!valid) return false;
            return onSubmit(pw);
          }}
        >
          Continue
        </PrimaryButton>
      </div>

      <BackLink onClick={onBack} />
    </OnboardingCard>
  );
}
/* Styles for the "what makes you rare" screen. It leads with the reason rather
   than the fields, because the fields have always been there and nobody filled
   them in: the old screen marked four of thirteen optional fields with a faint
   grey "Optional" and said nothing about why any of them mattered. */
const RARE_INTRO: React.CSSProperties = {
  background: "var(--ah-green-wash, var(--card-in))",
  border: "1px solid var(--ah-green-line, var(--hairline))",
  borderRadius: 18,
  padding: "16px 18px",
  margin: "0 0 22px",
};
const RARE_STRONG: React.CSSProperties = {
  color: "var(--ink)",
  fontWeight: 650,
};
const RARE_EYEBROW: React.CSSProperties = {
  fontFamily: "var(--mono)",
  fontSize: "0.68rem",
  letterSpacing: "0.09em",
  textTransform: "uppercase",
  color: "var(--ah-green-deep, #3c7a31)",
  marginBottom: 8,
};
const RARE_BODY: React.CSSProperties = {
  margin: 0,
  fontSize: "0.9rem",
  lineHeight: 1.6,
  color: "var(--ink-soft)",
};
const SKIP_LINK: React.CSSProperties = {
  display: "block",
  width: "100%",
  marginTop: 12,
  background: "none",
  border: 0,
  padding: 0,
  font: "inherit",
  fontSize: "0.85rem",
  color: "var(--ink-faint)",
  textDecoration: "underline",
  textUnderlineOffset: 3,
  cursor: "pointer",
};
