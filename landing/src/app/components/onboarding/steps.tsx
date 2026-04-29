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

import { useEffect, useState, type CSSProperties } from "react";

import {
  Avatar,
  BackLink,
  CodeInput,
  FieldLabel,
  InlineError,
  OnboardingCard,
  PrimaryButton,
  RolePill,
  SelectField,
  StatusPill,
  TextField,
} from "./primitives";

/* Shared shape — same across all steps so the wizard owns one state object. */

export type RoleLabel = "Doctor / Surgeon" | "Nurse / Care Coordinator";

export type Member = {
  id: number;
  firstName: string;
  lastName: string;
  email: string;
  role: RoleLabel;
  status: "Invited" | "Active";
};

export type OnboardingData = {
  firstName: string;
  lastName: string;
  email: string;
  orgName: string;
  department: string;
  phone: string;
  members: Member[];
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
  const valid =
    data.firstName.trim().length > 0 &&
    data.lastName.trim().length > 0 &&
    /\S+@\S+\.\S+/.test(data.email);

  return (
    <OnboardingCard
      eyebrow="Step 1 of 5"
      title="Let's get you set up."
      lede="A few minutes to bring your health system online. We'll start with you."
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
}: {
  data: OnboardingData;
  /** POST /api/onboarding/request-otp; resolve `false` on error to stay Idle. */
  onSendCode: () => Promise<boolean>;
  /** POST /api/onboarding/verify-otp with the 6-digit code; resolve `false` to stay Idle. */
  onVerify: (code: string) => Promise<boolean>;
  onBack: () => void;
  error?: string;
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
      eyebrow="Step 2 of 5"
      title="Verify your email."
      lede={
        sent ? (
          <>
            We sent a 6‑digit code to <span style={{ color: "#67E8F9" }}>{data.email}</span>. Enter it below.
          </>
        ) : (
          <>
            We&apos;ll send a one‑time code to <span style={{ color: "#67E8F9" }}>{data.email}</span> to confirm it&apos;s yours.
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
              color: "rgba(245,245,247,0.5)",
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
                color: resendIn > 0 ? "rgba(245,245,247,0.32)" : "#67E8F9",
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
          background: "rgba(103,232,249,0.06)",
          border: "1px solid rgba(103,232,249,0.18)",
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
            background: "rgba(103,232,249,0.14)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            flexShrink: 0,
          }}
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#67E8F9" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M20 7L9 18l-5-5" />
          </svg>
        </div>
        <div style={{ fontSize: 13, color: "rgba(245,245,247,0.78)", lineHeight: 1.5 }}>
          Your role: <strong style={{ color: "#F5F5F7" }}>Director of TEAM Initiative</strong> — assigned automatically as the onboarding owner.
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

  const draftValid =
    draft.firstName.trim() && draft.lastName.trim() && /\S+@\S+\.\S+/.test(draft.email) && draft.role !== "";

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
      lede="Your TEAM Initiative leadership and the surgeons, nurses, and coordinators you'll work alongside."
    >
      <InlineError>{error}</InlineError>

      {/* Director card — distinguished */}
      <div
        style={{
          background: "linear-gradient(135deg, rgba(38,99,235,0.12) 0%, rgba(103,232,249,0.06) 100%)",
          border: "1px solid rgba(103,232,249,0.25)",
          borderRadius: 14,
          padding: "20px 22px",
          marginBottom: 22,
          position: "relative",
          boxShadow: "0 0 0 1px rgba(103,232,249,0.04), 0 8px 32px rgba(38,99,235,0.10)",
        }}
      >
        <div
          style={{
            position: "absolute",
            top: -10,
            left: 22,
            background: "#0B0C12",
            padding: "0 10px",
            fontSize: 10,
            fontWeight: 700,
            letterSpacing: "0.18em",
            textTransform: "uppercase",
            color: "#67E8F9",
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
                  fontFamily: "'Fraunces', 'Iowan Old Style', 'Charter', Georgia, serif",
                  fontSize: 20,
                  fontWeight: 500,
                  letterSpacing: "-0.01em",
                  color: "#F5F5F7",
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
                color: "rgba(245,245,247,0.62)",
                display: "flex",
                alignItems: "center",
                gap: 10,
                flexWrap: "wrap",
              }}
            >
              <span>{data.email}</span>
              <span style={{ width: 3, height: 3, borderRadius: "50%", background: "rgba(245,245,247,0.3)" }} />
              <span>{data.orgName || "—"}</span>
              <span style={{ width: 3, height: 3, borderRadius: "50%", background: "rgba(245,245,247,0.3)" }} />
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
              fontWeight: 700,
              letterSpacing: "0.14em",
              textTransform: "uppercase",
              color: "rgba(245,245,247,0.5)",
              marginBottom: 4,
            }}
          >
            Team members
          </div>
          <div style={{ fontSize: 13, color: "rgba(245,245,247,0.55)" }}>
            {members.length === 0
              ? "No members yet — add the surgeons and nurses on your TEAM."
              : `${members.length} ${members.length === 1 ? "person" : "people"} on your TEAM.`}
          </div>
        </div>
        {!showAdd && (
          <button
            type="button"
            onClick={() => setShowAdd(true)}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 8,
              padding: "9px 14px",
              borderRadius: 9999,
              background: "rgba(103,232,249,0.10)",
              border: "1px solid rgba(103,232,249,0.32)",
              color: "#67E8F9",
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
              border: "1px dashed rgba(255,255,255,0.10)",
              borderRadius: 12,
              padding: "28px 20px",
              textAlign: "center",
              color: "rgba(245,245,247,0.42)",
              fontSize: 13,
            }}
          >
            Click <strong style={{ color: "#67E8F9" }}>Add member</strong> to invite the surgeons and care coordinators on your TEAM.
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
            background: "rgba(15,17,24,0.55)",
            border: "1px solid rgba(103,232,249,0.20)",
            borderRadius: 14,
            padding: "20px 22px",
            marginBottom: 22,
            animation: "ah-onb-fade-up 320ms cubic-bezier(0.16, 1, 0.3, 1)",
          }}
        >
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
            <div style={{ fontSize: 13, fontWeight: 600, color: "#F5F5F7" }}>Invite a team member</div>
            <button
              type="button"
              onClick={() => setShowAdd(false)}
              style={{
                background: "transparent",
                border: "none",
                color: "rgba(245,245,247,0.5)",
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
            options={[
              { value: "Doctor / Surgeon", label: "Doctor / Surgeon" },
              { value: "Nurse / Care Coordinator", label: "Nurse / Care Coordinator" },
            ]}
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

      <div style={{ height: 1, background: "rgba(255,255,255,0.07)", margin: "8px 0 22px" }} />
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
        background: hover ? "rgba(255,255,255,0.04)" : "rgba(15,17,24,0.45)",
        border: "1px solid " + (hover ? "rgba(255,255,255,0.14)" : "rgba(255,255,255,0.07)"),
        transition: "all 200ms cubic-bezier(0.16, 1, 0.3, 1)",
        animation: "ah-onb-fade-up 320ms cubic-bezier(0.16, 1, 0.3, 1)",
      }}
    >
      <Avatar name={`${member.firstName} ${member.lastName}`} size={42} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
          <span style={{ fontSize: 15, fontWeight: 500, color: "#F5F5F7" }}>
            {member.firstName} {member.lastName}
          </span>
          <RolePill role={member.role} />
        </div>
        <div style={{ fontSize: 13, color: "rgba(245,245,247,0.55)", marginTop: 3 }}>{member.email}</div>
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
              background: "rgba(248,113,113,0.10)",
              border: "1px solid rgba(248,113,113,0.32)",
              color: "#FCA5A5",
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
          <span style={{ fontFamily: "ui-monospace, 'SF Mono', Menlo, monospace", color: "#67E8F9" }}>
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
          style={{ fontSize: 13, color: "rgba(245,245,247,0.55)", textDecoration: "none" }}
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
          background: "radial-gradient(circle, rgba(103,232,249,0.30) 0%, rgba(103,232,249,0) 70%)",
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
            background: "linear-gradient(135deg, #67E8F9 0%, #2DD4BF 100%)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            boxShadow: "0 0 30px rgba(103,232,249,0.45), 0 0 0 1px rgba(103,232,249,0.5)",
          }}
        >
          <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#07070A" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
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
          color: "rgba(245,245,247,0.62)",
          textAlign: "center",
          lineHeight: 1.6,
          marginTop: 0,
          marginBottom: 26,
        }}
      >
        We&apos;ve sent welcome credentials to <strong style={{ color: "#F5F5F7" }}>{data.email}</strong>
        {memberCount > 0 ? (
          <>
            {" "}and to{" "}
            <strong style={{ color: "#F5F5F7" }}>
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
        background: "rgba(15,17,24,0.55)",
        border: "1px solid rgba(255,255,255,0.07)",
        borderRadius: 12,
        padding: "14px 14px",
      }}
    >
      <div
        style={{
          fontSize: 10,
          fontWeight: 700,
          letterSpacing: "0.12em",
          textTransform: "uppercase",
          color: "rgba(245,245,247,0.5)",
          marginBottom: 6,
        }}
      >
        {label}
      </div>
      <div
        style={{
          fontSize: 15,
          fontWeight: 500,
          color: "#F5F5F7",
          fontFamily: "'Fraunces', 'Iowan Old Style', 'Charter', Georgia, serif",
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
