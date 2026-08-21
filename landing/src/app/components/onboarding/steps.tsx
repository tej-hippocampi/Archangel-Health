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

export function Step1NameEmail({
  data,
  setData,
  onNext,
  error,
}: {
  data: OnboardingData;
  setData: (patch: Partial<OnboardingData>) => void;
  onNext: () => Promise<boolean>;
  error?: string;
}) {
  // Each screen gates ONLY on what it asks for. Gating screen 1 on a licence
  // number it never showed is how a Continue button goes dead with no
  // explanation anywhere on the page.
  const identityValid =
    c.fullLegalName.trim().length > 0 &&
    /^\d{10}$/.test(c.npi.trim()) &&
    // The physician's own phone is required (PRD Phase 4). Their mobile is
    // how verification actually gets resolved when NPPES is ambiguous.
    c.phone.trim().length >= 7 &&
    c.degree.trim().length > 0 &&
    c.primarySpecialty.trim().length > 0 &&
    c.currentlyActive !== null;

  const trainingValid =
    // Gate A2 needs both halves; gate A4 needs the attestation. Required at the
    // form rather than surfaced later as an "unresolved gate" in the admin queue,
    // because a physician can answer these in five seconds and an admin cannot.
    c.licenseNumber.trim().length > 0 &&
    c.licenseState.trim().length === 2 &&
    c.residencyCompleted !== null &&
    // AUDIT H1: required, because the alternative is inferring it from a blank or a
    // zero — and inferring "not practising" from the zero a physician on parental
    // leave types is the exact defect this field exists to close.
    c.practiceStatus !== "";

  // Screen 3 is entirely optional by design, so it never blocks.
  const valid =
    phase === 1 ? identityValid
    : phase === 2 ? trainingValid
    : phase === 3 ? true
    : identityValid && trainingValid;

  const isAsclepius = data.product === "asclepius";

  return (
    <OnboardingCard
      eyebrow="Step 1"
      title={isAsclepius ? "Welcome to Asclepius — let's start your journey." : "Let's get you set up."}
      lede={
        isAsclepius
          ? "A few minutes to get your evaluator workspace ready. We'll start with you."
          : "A few minutes to bring your health system online. We'll start with you."
      }
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
        label="Work email"
        placeholder="you@yourhealthsystem.org"
        type="email"
        value={data.email}
        onChange={(v) => setData({ email: v })}
        hint="Use your health-system email — we'll send a verification code here."
        autoComplete="email"
      />
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
  onUploaded,
}: {
  filename: string;
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
      <FieldLabel optional>CV or résumé</FieldLabel>
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
          {filename ? `${filename} attached` : "PDF or text, up to 10 MB. Optional."}
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
}) {
  const c = data.credentials;
  const set = (patch: Partial<Credentials>) => setData({ credentials: { ...c, ...patch } });
  const show = (n: 1 | 2 | 3) => phase === undefined || phase === n;

  const valid =
    c.fullLegalName.trim().length > 0 &&
    /^\d{10}$/.test(c.npi.trim()) &&
    // The physician's own phone is required (PRD Phase 4). Their mobile is
    // how verification actually gets resolved when NPPES is ambiguous.
    c.phone.trim().length >= 7 &&
    c.degree.trim().length > 0 &&
    c.primarySpecialty.trim().length > 0 &&
    c.currentlyActive !== null &&
    // Gate A2 needs both halves; gate A4 needs the attestation. Required at the
    // form rather than surfaced later as an "unresolved gate" in the admin queue,
    // because a physician can answer these in five seconds and an admin cannot.
    c.licenseNumber.trim().length > 0 &&
    c.licenseState.trim().length === 2 &&
    c.residencyCompleted !== null &&
    // AUDIT H1: required, because the alternative is inferring it from a blank or a
    // zero — and inferring "not practising" from the zero a physician on parental
    // leave types is the exact defect this field exists to close.
    c.practiceStatus !== "";

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
        : phase === 3 ? "All optional. This is the screen that decides what work reaches you."
        : memberMode
          ? "Your verified credentials are attached to the data you label — this is what makes it valuable. Please be accurate."
          : "As Director of Data Training, your credentials anchor the dataset your team produces."
      }
    >
      <InlineError>{error}</InlineError>

      {show(1) && (<>
      <TextField
        label="Full legal name"
        placeholder="Dr. Tej Patel"
        value={c.fullLegalName}
        onChange={(v) => set({ fullLegalName: v })}
      />
      <div style={TWO_COL}>
        <TextField
          label="NPI number"
          placeholder="10-digit NPI"
          value={c.npi}
          onChange={(v) => set({ npi: v.replace(/\D/g, "").slice(0, 10) })}
          hint="National Provider Identifier (10 digits)."
          error={c.npi.length > 0 && !/^\d{10}$/.test(c.npi) ? "NPI must be 10 digits." : undefined}
        />
        <SelectField
          label="Degree"
          placeholder="Select degree"
          value={c.degree}
          onChange={(v) => set({ degree: v })}
          options={DEGREE_OPTIONS}
        />
      </div>

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
      <SectionHeading title="Residency" sub="Institution + year." />
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
        label="Residency complete — not currently in training?"
        value={c.residencyCompleted}
        onChange={(v) => set({ residencyCompleted: v })}
      />
      <TextField
        label="Year you completed residency"
        placeholder="2010"
        value={c.residencyCompletionYear}
        onChange={(v) => set({ residencyCompletionYear: v.replace(/\D/g, "").slice(0, 4) })}
        hint="Used once, as a yes/no: at least three years post-residency. It is never scored as a number of years."
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
          All optional, and you can add them later. We ask because this is how work
          finds you. A physician who reads French gets the French cases. A paediatric
          nephrologist gets paediatric nephrology. Specialist and multilingual work
          pays materially more than general review, and we can only route it to you
          if we know it about you.
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
    a.attestConfidentiality && a.attestNoDisciplinaryAction;
  const valid = allChecked && initials.length >= 2;

  return (
    <OnboardingCard
      maxWidth={680}
      eyebrow={eyebrow}
      title="Attestations & rights."
      lede="A few legal must-haves before you label data. Read each, then sign with your initials."
    >
      <InlineError>{error}</InlineError>

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
              onChange={(v) => set({ signedInitials: v.slice(0, 8) })}
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
}: {
  data: OnboardingData;
  onOpenWorkspace: () => Promise<boolean> | boolean;
  memberMode?: boolean;
}) {
  return (
    <OnboardingCard maxWidth={620} title="Your workspace is ready.">
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
        <Stat label="Workspace" value={data.orgName || `${data.firstName} ${data.lastName}`.trim() || "Your workspace"} />
        <Stat label="Specialty" value={data.credentials.primarySpecialty || "Not set"} />
        <Stat
          label="Your role"
          value={memberMode ? data.roleLabel || "Clinician" : "Director"}
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
        You're already signed in. Your login details are in that email whenever you need them.
      </p>

      <PrimaryButton fullWidth onClick={onOpenWorkspace} loadingLabel="Opening…" successLabel="Opening ✓">
        Open my dashboard →
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
  background: "var(--card-in)",
  border: "1px solid var(--hairline)",
  borderRadius: 18,
  padding: "16px 18px",
  margin: "0 0 22px",
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
