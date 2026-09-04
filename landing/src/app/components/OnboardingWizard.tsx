/**
 * OnboardingWizard — Archangel Health onboarding.
 *
 * Two internal products share this wizard, distinguished server-side by the
 * `product` column set at invite creation — the signer is never asked to
 * choose:
 *
 *   "archangel" (clinical TEAM platform, admin-generated health-system
 *   invites) — unchanged 5-step flow:
 *     identity → verify → health system → your TEAM → sign in → success
 *     (backend: /step1-identity, /request-otp+/verify-otp,
 *      /step3-organization, /add-team-member + /finish, /tenant login)
 *
 *   "asclepius" (data-training product, self-serve physician-contributor
 *   invites — user-facing copy calls this "Archangel", never "Asclepius")
 *   — Onboarding v2 §2, six screens and NO password step:
 *     identity → verify → CV → review → attestations → submitted
 *     (backend: /asclepius/{cv,cv/status,credentials,attestations,finish})
 *     The CV screen parses the document server-side and the Review screen
 *     arrives pre-filled from it; the account is created `pending` with no
 *     credential, and a temporary password is minted and emailed only when an
 *     admin approves the application. Nothing except name, email and specialty
 *     is required to submit, so Submit is always live.
 *     Compliance/HIPAA gates do not apply to this plane — no PHI is collected.
 *
 * Invited clinicians (mode="member") open /onboard/m/<token> and run a short
 * flow: credentials → attestations → verify → workspace, inheriting org +
 * specialty from their director. Verify is a hard gate (parity with the
 * director's OTP step): /member/finish 403s until it is complete.
 * (backend: /member/{session,credentials,attestations,request-otp,verify-otp,finish}).
 *
 * Each PrimaryButton handler returns Promise<boolean>; resolving `false` keeps
 * the button Idle so server errors don't fake-flash success.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import { API_BASE, apiHeaders } from "@/lib/auth-api";
import * as authApi from "@/lib/auth-api";

import OnboardingStyles from "./onboarding/OnboardingStyles";
import { ChromeHeader, Stepper } from "./onboarding/primitives";
import {
  ASCLEPIUS_ROLE_LABELS,
  Step1NameEmail,
  Step2Verify,
  StepApplicationSubmitted,
  StepChoosePassword,
  StepCv,
  Step3Org,
  Step4Institution,
  Step4YourTeam,
  Step5Credentials,
  Step5SignIn,
  Step6Attestations,
  Step6Success,
  Step7AsclepiusTeam,
  Step8AsclepiusSuccess,
  StepAsclepiusSignIn,
  emptyAttestations,
  emptyCredentials,
  type AsclepiusMember,
  type AsclepiusRole,
  type Credentials,
  type CvParsed,
  type CvStage,
  type Member,
  type OnboardingData,
  type Product,
  type RoleLabel,
  type SignupKind,
} from "./onboarding/steps";

type Mode = "director" | "member";
type Props = { token: string; mode?: Mode };

type StepKey =
  | "identity"
  | "verify"
  | "password"
  | "cv"
  | "review"
  | "submitted"
  | "org"
  | "team"
  | "signin"
  | "success"
  | "institution"
  | "credentials"
  | "credTraining"
  | "credRare"
  | "attestations"
  | "ascTeam"
  | "ascSignIn"
  | "ascSuccess";

const STEP_LABELS: Partial<Record<StepKey, string>> = {
  identity: "You",
  verify: "Verify",
  password: "Password",
  cv: "Your CV",
  review: "Review",
  org: "Health system",
  team: "Your TEAM",
  signin: "Sign in",
  institution: "Institution",
  credentials: "You",
  credTraining: "Training",
  credRare: "Detail",
  attestations: "Attestations",
  ascTeam: "Team",
};

/** Which flavor of link produces which kind of account, mirroring
 *  ``ACCOUNT_KIND_BY_FLAVOR`` in ``backend/routers/onboarding.py``. These are
 *  exactly the flavors whose surfaces the server caps, and the short signup
 *  below is offered to exactly them: a door that asks for less must lead to an
 *  account that can do less, or "skip the credential screens" is just a way to
 *  become a physician without answering anything.
 *
 *  "general" is deliberately absent. It is the older invited-non-clinical
 *  flavor, it maps to no account kind, and an account it created is capped by
 *  nothing, so it keeps the full wizard it has always had. */
const KIND_BY_FLAVOR: Record<string, SignupKind> = {
  advisor: "advisor",
  referrer: "referrer",
};

function signupKindFor(flavor: unknown): SignupKind {
  return KIND_BY_FLAVOR[String(flavor ?? "").trim().toLowerCase()] ?? "physician";
}

/** Ordered step list for the active flow (drives Back + the stepper).
 * Product is decided server-side at invite creation (self-serve links are
 * pre-locked to "asclepius"; admin-generated health-system links default to
 * "archangel") — the wizard never asks the signer to choose. */
function orderFor(mode: Mode, product: Product | "", kind: SignupKind = "physician"): StepKey[] {
  // "password" always comes immediately AFTER "verify", never before it, and
  // never on the credentials screen. Two reasons, in order of weight:
  //
  //  1. Member mode verifies LAST. Capturing a password before the mailbox is
  //     proven would let a typo'd address end up with an account whose password
  //     was set by someone who cannot receive its mail.
  //  2. The credentials screen is the longest in the flow and the most likely
  //     to 400 on a field; a re-render there would clear the password pair.
  //
  // The OTP step is also the natural "you are who you say you are, now claim
  // the account" moment.
  if (mode === "member") return ["credentials", "attestations", "verify", "password", "ascSuccess"];
  const head: StepKey[] = ["identity", "verify", "password"];
  // An advisor and a referral partner are not claiming to be doctors, so every
  // screen after the password has nothing to ask them: no institution, no NPI
  // or registration number, no residency, and above all not the seven clinical
  // attestations, which include independent clinical judgment and no active
  // board disciplinary action. Signing those is not a formality for someone who
  // does not practise; it is signing something untrue. The one promise that
  // does apply to an advisor (confidentiality) is asked on the identity screen.
  if (kind !== "physician") return [...head, "ascSuccess"];
  if (product === "asclepius") {
    // ── Onboarding v2 §2 ───────────────────────────────────────────────────
    // Six screens: identity → verify → CV → review → attestations → submitted.
    //
    // NO PASSWORD STEP on this path, and that is the load-bearing change. The
    // account row is created `pending` with no hash; credentials are minted and
    // mailed when a human approves the application (§5). Asking a physician to
    // invent a password for an account that may never open — and that they will
    // not touch for a day or two even if it does — was a screen that cost
    // completions and bought nothing.
    //
    // The member and advisor/referrer paths above are UNTOUCHED: they still
    // choose a password, because their accounts open immediately.
    return ["identity", "verify", "cv", "review", "attestations", "submitted"];
  }
  return [...head, "org", "team", "signin", "success"];
}

const ROLE_TO_API: Record<RoleLabel, "rn_coordinator" | "np_pa"> = {
  "RN Care Coordinator": "rn_coordinator",
  "NP / PA": "np_pa",
};

function normalizeRoleLabel(raw: unknown): RoleLabel {
  const s = typeof raw === "string" ? raw.trim().toLowerCase() : "";
  if (s === "np_pa" || s === "np / pa" || s === "np/pa" || s === "nppa") return "NP / PA";
  return "RN Care Coordinator";
}

function api(path: string, init?: RequestInit): Promise<Response> {
  return fetch(`${API_BASE}${path}`, {
    ...init,
    headers: apiHeaders({ "Content-Type": "application/json", ...(init?.headers ?? {}) }),
  });
}

function formatApiError(data: unknown): string {
  if (!data || typeof data !== "object") return "Request failed";
  const detail = (data as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item: unknown) => {
        if (item && typeof item === "object" && "msg" in item) {
          const o = item as { msg: string; loc?: unknown };
          const loc = Array.isArray(o.loc) ? o.loc.join(".") : "";
          return loc ? `${loc}: ${o.msg}` : o.msg;
        }
        try {
          return JSON.stringify(item);
        } catch {
          return String(item);
        }
      })
      .join("; ");
  }
  if (detail != null && typeof detail === "object") {
    // A structured detail carries a human message. Printing the JSON at a
    // physician is what put `{"code": "...", "message": "..."}` on screen.
    const msg = (detail as { message?: unknown }).message;
    if (typeof msg === "string" && msg) return msg;
    try {
      return JSON.stringify(detail);
    } catch {
      return "Request failed";
    }
  }
  return "Request failed";
}

/** The API's machine-readable reason, when it gave one.
 *
 *  Only one code matters today: `onboarding_complete`. It is a terminal STATE
 *  rather than an error, and treating it as an error is what turned the end of
 *  a successful signup into a dead end. A physician who reached the thank-you
 *  screen, opened the mission link and pressed Back landed on the verify step,
 *  asked for a code, and was shown "Onboarding already completed for this link"
 *  with no button anywhere on the page.
 */
function apiErrorCode(data: unknown): string {
  if (!data || typeof data !== "object") return "";
  const detail = (data as { detail?: unknown }).detail;
  if (!detail || typeof detail !== "object") return "";
  const code = (detail as { code?: unknown }).code;
  return typeof code === "string" ? code : "";
}

/* ── Terminal screens are STICKY ─────────────────────────────────────────────
   The landing app has no router, so the "see our mission" link on the
   thank-you screen is a full page navigation and Back is a fresh mount. Its
   only source of truth is GET /session, and a physician who had already
   finished came back to the VERIFY step, asked for a code, and hit a 410 with
   nothing on screen but its message.

   Three layers now hold that shut, and this is the client one: the terminal
   step is pinned, and read BEFORE the fetch, so the resume ladder can never
   overrule it. replaceState rather than pushState, or Back needs two presses.
   The server sends Cache-Control: no-store for the same reason from the other
   side. Belt and braces on purpose: which layer was actually failing is an
   inference, and this is cheap. */
const TERMINAL_STEPS = ["submitted", "ascSuccess", "success", "ascSignIn"] as const;

function pinTerminalStep(token: string, step: string): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(`ah-onb-terminal:${token}`, step);
  } catch {
    /* private mode; the history state below still holds within the tab */
  }
  try {
    window.history.replaceState(
      { ...(window.history.state || {}), ahOnbStep: step }, "");
  } catch {
    /* nothing to do; the sessionStorage copy is the durable one */
  }
}

function readPinnedStep(token: string): string {
  if (typeof window === "undefined") return "";
  try {
    const stored = window.sessionStorage.getItem(`ah-onb-terminal:${token}`);
    if (stored) return stored;
  } catch {
    /* fall through to history state */
  }
  const fromHistory = (window.history.state || {}).ahOnbStep;
  return typeof fromHistory === "string" ? fromHistory : "";
}

async function readResponseJson(r: Response): Promise<unknown> {
  const text = await r.text();
  if (!text.trim()) return {};
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return {};
  }
}

function initialData(): OnboardingData {
  return {
    firstName: "",
    lastName: "",
    email: "",
    orgName: "",
    department: "",
    phone: "",
    members: [],
    product: "",
    specialty: "",
    ascMembers: [],
    credentials: emptyCredentials(),
    attestations: emptyAttestations(),
    roleLabel: "",
    workspaceUrl: "",
    asclepiusToken: "",
    cvParsed: null,
    cvAutofilled: [],
    awaitingReview: false,
    password: "",
    passwordSet: false,
  };
}

/** Onboarding v2 §2 screen 3→4: fold a CV parse into the credential fields.
 *
 *  Only fills fields that are EMPTY. A physician who typed something before the
 *  parse landed — or who is resuming a session where they already corrected a
 *  suggestion — must never have their own answer overwritten by ours, and the
 *  race is real: the parse is a background job and the Review screen is one
 *  click away.
 *
 *  Returns the patch AND the list of keys it filled, because the "from your CV"
 *  chips are drawn from exactly what this function decided, not from what the
 *  parse happened to contain. A chip on a field the parse could not fill would
 *  be attributing the physician's own typing to their CV.
 */
function applyCvParse(
  parsed: CvParsed | null,
  current: Credentials,
): { patch: Partial<Credentials>; filled: string[] } {
  const patch: Partial<Credentials> = {};
  const filled: string[] = [];
  if (!parsed || !parsed.ok) return { patch, filled };

  const fill = (key: keyof Credentials, value: string | undefined | null) => {
    const v = (value ?? "").toString().trim();
    if (!v) return;
    if ((current[key] ?? "").toString().trim()) return;   // never overwrite them
    (patch as Record<string, unknown>)[key] = v;
    filled.push(key as string);
  };

  fill("fullLegalName", parsed.full_name);
  // The DISPLAY spelling, not the registry key. The key is lowercase because
  // it is an identifier, and prefilling "nephrology" into a form asking a
  // physician to vouch for their credentials reads as carelessness.
  fill("primarySpecialty", parsed.specialty_display || parsed.specialty);
  fill("linkedinUrl", parsed.linkedin_url);
  fill("healthSystem", parsed.employer);
  // The NPI is only carried across when the parse found a LABELLED,
  // checksum-valid one (the server does that check). Prefilling a ten-digit run
  // that happened to sit near the word NPI would put a wrong number behind a
  // chip that says we read it off their CV.
  fill("npi", parsed.npi);
  // Degrees are a list; the primary is the first medical one we recognized.
  // The qualification control renders `degree` for a US physician and
  // `qualification` for everyone else, and its own onChange writes BOTH. The
  // prefill has to do the same: filling only `degree` left a doctor licensed
  // outside the US looking at an empty box wearing a "from your CV" chip —
  // a label claiming their CV said something the field does not show.
  const degree = (parsed.degrees || []).find((d) => ["MD", "DO", "MBBS", "MBChB", "DPM"].includes(d));
  if (degree) {
    fill("degree", degree);
    fill("qualification", degree);
  }
  if (parsed.years_in_practice != null) fill("yearsInActivePractice", String(parsed.years_in_practice));

  /* Board certifications, now board AND field.
   *
   *  This used to fill a NAME into the board column and leave the rest blank,
   *  because the parse produced a flat list of strings. On the CV from the
   *  walkthrough it filled three: "Nephrologist", "nephrologist with" and
   *  "ABIM", none of which is a certification, while the two real ones went
   *  missing. The parser now emits {board, specialty} and refuses to emit
   *  anything it cannot recognise as both.
   *
   *  `active` is still never asserted. "Currently valid" is a compliance
   *  answer about today, and a document written last year cannot give it;
   *  putting `true` there would be words in a physician's mouth on a field
   *  they sign for. It stays false so the box is unticked and theirs to tick.
   */
  const structured = (parsed.board_certifications_structured || []).filter(
    (c) => c && (c.board || c.specialty));
  const currentCerts = current.boardCertifications || [];
  const certsUntouched = currentCerts.every(
    (bc) => !bc.board.trim() && !bc.specialty.trim() && !bc.subspecialty.trim());
  if (certsUntouched) {
    if (structured.length) {
      patch.boardCertifications = structured.slice(0, 4).map((c) => ({
        board: c.board || "",
        specialty: c.specialty || "",
        subspecialty: c.subspecialty || "",
        active: false,
      }));
      filled.push("boardCertifications");
    } else {
      // A parse from before this shape existed, or one that found labels it
      // could not take apart. Same treatment as before: the name only.
      const flat = (parsed.board_certifications || []).filter(Boolean);
      if (flat.length) {
        patch.boardCertifications = flat.slice(0, 4).map((name) => ({
          board: name, specialty: "", subspecialty: "", active: false,
        }));
        filled.push("boardCertifications");
      }
    }
  }

  /* Fellowship and residency. The parse has carried these since v2 and nothing
   *  ever read them, so a physician whose CV plainly listed both retyped both.
   */
  const training = (parsed.training || []).filter(Boolean);
  const fellowships = training.filter((t) => t.kind === "fellowship");
  const residencies = training.filter(
    (t) => t.kind === "residency" || t.kind === "internship");
  const fellowshipUntouched = (current.fellowship || []).every(
    (f) => !f.institution.trim() && !f.specialty.trim() && !f.year.trim());
  if (fellowships.length && fellowshipUntouched) {
    patch.fellowship = fellowships.slice(0, 3).map((t) => ({
      institution: t.institution || "",
      // The parse knows WHERE and WHEN. It does not reliably know the
      // fellowship's subject, so that box is left for the physician.
      specialty: "",
      year: t.end_year || "",
    }));
    filled.push("fellowship");
  }
  const residencyUntouched = (current.residency || []).every(
    (r) => !r.institution.trim() && !r.year.trim());
  if (residencies.length && residencyUntouched) {
    patch.residency = residencies.slice(0, 3).map((t) => ({
      institution: t.institution || "",
      year: t.end_year || "",
    }));
    filled.push("residency");
  }

  /* The licence. Anchored on a labelled line server-side, so what arrives here
   *  is a state and a number that were written down together.
   */
  const licences = (parsed.licenses || []).filter((l) => l && l.state && l.number);
  const current_licence = licences.find((l) => l.current) || licences[0];
  if (current_licence) {
    fill("licenseNumber", current_licence.number);
    fill("licenseState", current_licence.state);
  }
  return { patch, filled };
}

export default function OnboardingWizard({ token, mode = "director" }: Props) {
  const [step, setStep] = useState<StepKey>(mode === "member" ? "credentials" : "identity");
  const [data, setDataState] = useState<OnboardingData>(initialData);
  const setData = useCallback((patch: Partial<OnboardingData>) => {
    setDataState((d) => ({ ...d, ...patch }));
  }, []);

  const [slug, setSlug] = useState("");
  // Which door this link came from. /join?flavor=general marks an invited
  // non-clinical signer, who still walks the full wizard with the MD credential
  // screens relaxed; flavor=advisor and flavor=referrer produce capped accounts
  // and get the short signup instead (see orderFor).
  const [signupFlavor, setSignupFlavor] = useState("");
  const [signInReason, setSignInReason] =
    useState<"complete" | "account_exists">("complete");
  const signupKind = useMemo(() => signupKindFor(signupFlavor), [signupFlavor]);
  const [authToken, setAuthToken] = useState("");
  // §2 screen 3. `cvStage` null means nothing has been uploaded on this visit,
  // which is what puts the drop zone on screen rather than the scan animation.
  const [cvStage, setCvStage] = useState<CvStage | null>(null);
  const [cvFilename, setCvFilename] = useState("");
  const [cvUploading, setCvUploading] = useState(false);
  const [stepError, setStepError] = useState("");
  const [bootError, setBootError] = useState("");
  const [loading, setLoading] = useState(true);

  // ─────────────────────────────────────────
  // Session bootstrap — resume in-progress onboarding.
  // ─────────────────────────────────────────
  const loadDirectorSession = useCallback(async () => {
    // Read the pin FIRST. If this tab already reached a terminal screen for
    // this token, that is the answer no matter what the resume ladder below
    // would have decided, and it is decided before any network result can race
    // it.
    const pinned = readPinnedStep(token);

    const r = await api(`/api/onboarding/session?token=${encodeURIComponent(token)}`);
    const body = await readResponseJson(r);
    if (!r.ok) {
      // A completed link is a STATE, not an error. It is where a successful
      // signup ends up, so it gets the screen with the buttons on it.
      if (apiErrorCode(body) === "onboarding_complete") {
        setSignInReason("complete");
        setStep("ascSignIn");
        return;
      }
      setBootError(formatApiError(body) || `HTTP ${r.status}`);
      return;
    }
    const d = body as Record<string, any>;
    if (d.slug) setSlug(d.slug);
    setSignupFlavor(String(d.signup_flavor ?? "").trim().toLowerCase());
    const product = (d.product as Product) || "";
    const hydratedMembers: Member[] = (d.team_members ?? []).map((m: any, idx: number) => ({
      id: typeof m.id === "number" && m.id > 0 ? m.id : Date.now() + idx,
      firstName: (m.first_name ?? "").trim(),
      lastName: (m.last_name ?? "").trim(),
      email: (m.email ?? "").trim(),
      role: normalizeRoleLabel(m.role),
      status: m.status === "Active" ? "Active" : "Invited",
    }));
    const ascMembers: AsclepiusMember[] = (d.asclepius_members ?? []).map((m: any, idx: number) => {
      const full = String(m.full_name ?? "").trim();
      const [first, ...rest] = full.split(" ");
      const role = String(m.clinical_role ?? "physician").toLowerCase();
      return {
        id: typeof m.id === "number" && m.id > 0 ? m.id : Date.now() + idx,
        firstName: first ?? "",
        lastName: rest.join(" "),
        email: (m.email ?? "").trim(),
        role: (role in ASCLEPIUS_ROLE_LABELS ? role : "physician") as AsclepiusRole,
        status: m.status === "Active" ? "Active" : "Invited",
      };
    });

    const firstName = (d.director_first_name ?? "").trim();
    const lastName = (d.director_last_name ?? "").trim();
    const fullLegal = `${firstName} ${lastName}`.trim();
    const savedCreds = d.director_credentials && Object.keys(d.director_credentials).length > 0;
    const cvBlock = (d.director_cv ?? {}) as {
      uploaded?: boolean; filename?: string | null; stage?: CvStage | null;
      parsed?: CvParsed | null;
    };
    if (cvBlock.uploaded) {
      setCvFilename(cvBlock.filename || "");
      setCvStage((cvBlock.stage as CvStage | null) ?? null);
    }
    setDataState((prev) => ({
      ...prev,
      firstName,
      lastName,
      email: (d.director_email ?? "").trim(),
      orgName: (d.health_system_name ?? "").trim(),
      department: (d.surgery_department ?? "").trim(),
      specialty: (d.specialty ?? "").trim(),
      phone: (d.phone ?? "").trim(),
      members: hydratedMembers,
      product,
      ascMembers,
      credentials: (() => {
        const base = savedCreds
          ? { ...emptyCredentials(fullLegal), ...d.director_credentials }
          : emptyCredentials(fullLegal);
        // Screen 1's state answer prefills the Review screen's licence block,
        // so the same fact is not asked for twice. Never over a value the
        // physician already has there: the same rule applyCvParse follows.
        const fromStep1 = (d.director_license_state ?? "").trim();
        return fromStep1 && !base.licenseState
          ? { ...base, licenseState: fromStep1 }
          : base;
      })(),
      attestations:
        d.director_attestations && Object.keys(d.director_attestations).length > 0
          ? { ...emptyAttestations(), ...d.director_attestations }
          : emptyAttestations(),
      cvParsed: (cvBlock.parsed ?? null) as CvParsed | null,
      // Chips are NOT restored across a resume. A chip says "we filled this in
      // for you, just now"; three days later the physician has no reason to
      // remember which fields were ours, and labelling values they have since
      // reviewed as unverified CV output would be the wrong claim to make.
      cvAutofilled: [],
      // Whether a password is already on file for this application. A boolean
      // from the server; the hash never leaves the backend. A physician
      // resuming a half-finished application must not be asked again for
      // something they already chose.
      password: "",
      passwordSet: Boolean(d.director_password_set),
    }));

    // The pin wins. The data above is still hydrated, because a pinned screen
    // still renders the physician's own name and email; only the CHOICE of
    // screen is taken away from the ladder.
    if (pinned && (TERMINAL_STEPS as readonly string[]).includes(pinned)) {
      setStep(pinned as StepKey);
      return;
    }

    // A finished link, or an address that already has an account. Both are
    // "you do not need to sign up, you need to sign in" -- and the second one
    // MUST NOT walk the wizard again: /finish passes password_hash
    // unconditionally, so a second pass silently repoints the live account's
    // password to whatever gets typed on the way through.
    // `application_pending` used to be unhandled here and fell through to the
    // resume ladder, which put somebody whose application is already in with us
    // back on a half-filled form.
    if (d.status === "complete" || d.status === "account_exists"
        || d.status === "application_pending") {
      if (product === "asclepius" || d.status === "account_exists") {
        setSignInReason(d.status === "account_exists" ? "account_exists" : "complete");
        setStep("ascSignIn");
      } else {
        setStep("signin");
      }
      return;
    }
    // Resume to the right screen. `step` is the backend's highest completed
    // step (0 identity, 1 verify-done, 2 verified, 3 org/institution saved).
    // Product is fixed server-side at invite creation, so a reload right
    // after verification routes straight into that product's branch instead
    // of showing a choice. Credentials/attestations don't bump the counter,
    // so for Asclepius we resume by inspecting what's already saved.
    const stepNum = Number(d.step) || 0;
    const savedAtts =
      d.director_attestations && Object.keys(d.director_attestations).length > 0;
    const kind = signupKindFor(d.signup_flavor);
    if (stepNum < 1) setStep("identity");
    else if (stepNum < 2) setStep("verify");
    // A short signup has nowhere to resume TO past the password: the screens
    // after it do not exist for this account. Setting a password again is an
    // idempotent upsert, so landing here twice costs nothing.
    else if (kind !== "physician") setStep("password");
    else if (product === "asclepius") {
      // v2 §3: resume to the exact screen they left. The CV and Review screens
      // do not bump the server's step counter (nothing about them is a gate),
      // so the resume point is derived from what has actually been SAVED —
      // which is also why a physician who uploaded a CV and closed the tab
      // comes back to their filled-in Review page and not to the drop zone.
      const cv = (d.director_cv || {}) as Record<string, unknown>;
      if (savedAtts) setStep("attestations");
      else if (savedCreds || cv.uploaded) setStep("review");
      else setStep("cv");
    }
    else if (stepNum < 3) setStep("org");
    else setStep("team");
  }, [token]);

  const loadMemberSession = useCallback(async () => {
    const r = await api(`/api/onboarding/member/session?token=${encodeURIComponent(token)}`);
    const body = await readResponseJson(r);
    if (!r.ok) {
      setBootError(formatApiError(body) || `HTTP ${r.status}`);
      return;
    }
    const d = body as Record<string, any>;
    const firstName = (d.first_name ?? "").trim();
    const lastName = (d.last_name ?? "").trim();
    const fullLegal = (d.full_name ?? `${firstName} ${lastName}`).trim();
    const savedCreds = d.credentials && Object.keys(d.credentials).length > 0;
    setDataState((prev) => ({
      ...prev,
      firstName,
      lastName,
      email: (d.email ?? "").trim(),
      orgName: (d.org_name ?? "").trim(),
      specialty: (d.specialty ?? "").trim(),
      roleLabel: (d.role_label ?? "").trim(),
      product: "asclepius",
      credentials: savedCreds
        ? { ...emptyCredentials(fullLegal), ...d.credentials }
        : emptyCredentials(fullLegal),
      attestations:
        d.attestations && Object.keys(d.attestations).length > 0
          ? { ...emptyAttestations(), ...d.attestations }
          : emptyAttestations(),
    }));
    setStep("credentials");
  }, [token]);

  const loadSession = useCallback(async () => {
    setLoading(true);
    setBootError("");
    try {
      if (mode === "member") await loadMemberSession();
      else await loadDirectorSession();
    } catch (e: unknown) {
      setBootError(e instanceof Error ? e.message : "Could not load onboarding");
    } finally {
      setLoading(false);
    }
  }, [mode, loadDirectorSession, loadMemberSession]);

  useEffect(() => {
    void loadSession();
  }, [loadSession]);

  // Pin the moment a terminal screen is reached, so leaving the page and
  // coming back lands here rather than on the verify step. See the helpers
  // above for why this exists at all.
  useEffect(() => {
    if ((TERMINAL_STEPS as readonly string[]).includes(step)) {
      pinTerminalStep(token, step);
    }
  }, [step, token]);

  const order = useMemo(
    () => orderFor(mode, data.product, signupKind),
    [mode, data.product, signupKind],
  );

  /** Advance one step along the ACTIVE order.
   *  Credentials is three screens now, so a handler that hardcodes its
   *  destination silently skips two of them. */
  const goNext = useCallback(() => {
    setStepError("");
    setStep((cur) => {
      const idx = order.indexOf(cur);
      return idx >= 0 && idx < order.length - 1 ? order[idx + 1] : cur;
    });
  }, [order]);

  const goBack = useCallback(() => {
    setStepError("");
    setStep((cur) => {
      const idx = order.indexOf(cur);
      return idx > 0 ? order[idx - 1] : cur;
    });
  }, [order]);

  // ─────────────────────────────────────────
  // Shared steps (identity / verify / product).
  // ─────────────────────────────────────────
  const submitStep1 = useCallback(async () => {
    setStepError("");
    const r = await api("/api/onboarding/step1-identity", {
      method: "POST",
      body: JSON.stringify({
        token,
        first_name: data.firstName,
        last_name: data.lastName,
        email: data.email,
        // Both optional on the wire. The server hashes the password on arrival
        // and stores only the hash; `undefined` when the physician already set
        // one on an earlier visit, so a resumed session cannot blank it.
        password: data.password || undefined,
        license_state: data.credentials.licenseState || "",
      }),
    });
    const body = await readResponseJson(r);
    if (!r.ok) {
      setStepError(formatApiError(body) || `HTTP ${r.status}`);
      return false;
    }
    // Drop the plaintext the instant it is spent. It lived in React state for
    // one screen and it does not need to outlive the request.
    setDataState((d) => ({ ...d, password: "", passwordSet: true }));
    setStep("verify");
    return true;
  }, [token, data.firstName, data.lastName, data.email, data.password,
      data.credentials.licenseState]);

  const sendOtp = useCallback(async () => {
    setStepError("");
    const r = await api("/api/onboarding/request-otp", {
      method: "POST",
      body: JSON.stringify({ token }),
    });
    const body = await readResponseJson(r);
    if (!r.ok) {
      setStepError(formatApiError(body) || `HTTP ${r.status}`);
      return false;
    }
    return true;
  }, [token]);

  const verifyOtp = useCallback(
    async (code: string) => {
      setStepError("");
      const r = await api("/api/onboarding/verify-otp", {
        method: "POST",
        body: JSON.stringify({ token, code }),
      });
      const body = await readResponseJson(r);
      if (!r.ok) {
        setStepError(formatApiError(body) || "Invalid code");
        return false;
      }
      // Product was decided server-side at invite creation — go straight to
      // the right branch instead of asking the signer to choose.
      // The password step comes AFTER the mailbox is proven, for the self-serve
      // door exactly as it does for member mode; submitPassword then hands off
      // to "institution". Skipping it stranded the physician on the final step,
      // where /asclepius/finish rejects with "Choose a password before
      // finishing" and the wizard offers no route back to set one.
      // v2 §2: the physician path has no password step — the account carries no
      // credential until a human approves the application — so the OTP hands
      // straight off to the CV screen. The advisor/referrer short signup and
      // member mode still choose a password here, and still land on it.
      if (data.product !== "asclepius") { setStep("org"); return true; }
      setStep(signupKind === "physician" ? "cv" : "password");
      return true;
    },
    [token, data.product, signupKind],
  );

  // ─────────────────────────────────────────
  // Archangel branch.
  // ─────────────────────────────────────────
  const submitOrg = useCallback(async () => {
    setStepError("");
    const r = await api("/api/onboarding/step3-organization", {
      method: "POST",
      body: JSON.stringify({
        token,
        health_system_name: data.orgName,
        surgery_department: data.department,
        phone: data.phone,
      }),
    });
    const body = await readResponseJson(r);
    if (!r.ok) {
      setStepError(formatApiError(body) || `HTTP ${r.status}`);
      return false;
    }
    const d = body as { slug?: string };
    if (d.slug) setSlug(d.slug);
    setStep("team");
    return true;
  }, [token, data.orgName, data.department, data.phone]);

  const addTeamMember = useCallback(
    async (m: Omit<Member, "id" | "status">) => {
      setStepError("");
      const r = await api("/api/onboarding/add-team-member", {
        method: "POST",
        body: JSON.stringify({
          token,
          full_name: `${m.firstName} ${m.lastName}`.trim(),
          email: m.email,
          role: ROLE_TO_API[m.role],
        }),
      });
      const body = await readResponseJson(r);
      if (!r.ok) {
        setStepError(formatApiError(body) || `HTTP ${r.status}`);
        return false;
      }
      const newMember: Member = { ...m, id: Date.now(), status: "Invited" };
      setDataState((d) => ({ ...d, members: [...d.members, newMember] }));
      return true;
    },
    [token],
  );

  const removeMember = useCallback((id: number) => {
    setDataState((d) => ({ ...d, members: d.members.filter((m) => m.id !== id) }));
  }, []);

  const finishOnboarding = useCallback(async () => {
    setStepError("");
    const r = await api("/api/onboarding/finish", {
      method: "POST",
      body: JSON.stringify({ token }),
    });
    const body = await readResponseJson(r);
    if (!r.ok) {
      setStepError(formatApiError(body) || `HTTP ${r.status}`);
      return false;
    }
    setStep("signin");
    return true;
  }, [token]);

  const signIn = useCallback(
    async (email: string, password: string) => {
      setStepError("");
      if (!slug) {
        setStepError("Workspace not ready yet: refresh and try again.");
        return false;
      }
      const r = await fetch(`${API_BASE}/api/tenant/${encodeURIComponent(slug)}/auth/login`, {
        method: "POST",
        headers: apiHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ email, password }),
      });
      const body = await readResponseJson(r);
      if (!r.ok) {
        setStepError(formatApiError(body) || "Sign in failed");
        return false;
      }
      const d = body as { access_token?: string };
      if (d.access_token) setAuthToken(d.access_token);
      setStep("success");
      return true;
    },
    [slug],
  );

  const openWorkspace = useCallback(() => {
    void authApi.redirectToDoctorPortal(authToken);
    return true;
  }, [authToken]);

  // ─────────────────────────────────────────
  // Asclepius branch (director).
  // ─────────────────────────────────────────
  const submitInstitution = useCallback(async () => {
    setStepError("");
    const r = await api("/api/onboarding/asclepius/institution", {
      method: "POST",
      body: JSON.stringify({
        token,
        // Org name is optional now; fall back to the physician's own name so the
        // workspace slug is still sensible. Specialty comes from credentials.
        org_name: data.orgName.trim() || `${data.firstName} ${data.lastName}`.trim() || "My workspace",
        specialty: data.specialty,
        phone: data.phone,
      }),
    });
    const body = await readResponseJson(r);
    if (!r.ok) {
      setStepError(formatApiError(body) || `HTTP ${r.status}`);
      return false;
    }
    const d = body as { slug?: string };
    if (d.slug) setSlug(d.slug);
    if (!data.credentials.fullLegalName.trim()) {
      setData({
        credentials: { ...data.credentials, fullLegalName: `${data.firstName} ${data.lastName}`.trim() },
      });
    }
    setStep("credentials");
    return true;
  }, [token, data.orgName, data.specialty, data.phone, data.credentials, data.firstName, data.lastName, setData]);

  const saveCredentials = useCallback(
    async (path: string) => {
      setStepError("");
      const r = await api(path, {
        method: "POST",
        body: JSON.stringify({ token, credentials: data.credentials }),
      });
      const body = await readResponseJson(r);
      if (!r.ok) {
        setStepError(formatApiError(body) || `HTTP ${r.status}`);
        return false;
      }
      return true;
    },
    [token, data.credentials],
  );

  // ─────────────────────────────────────────
  // Onboarding v2 §2 screens 3–4: CV → Review.
  // ─────────────────────────────────────────

  /** Fold whatever the parse found into the empty credential fields, then move
   *  on. Called from BOTH terminal outcomes: a parse that worked and one that
   *  did not. §2 is explicit that a failed parse lands on the Review page as an
   *  empty state and not as an error — the manual path and the failed-parse path
   *  are the same screen, which is what makes "no CV" a real option rather than
   *  a consolation prize. */
  const applyParseAndReview = useCallback((parsed: CvParsed | null) => {
    setDataState((d) => {
      const { patch, filled } = applyCvParse(parsed, d.credentials);
      return {
        ...d,
        credentials: { ...d.credentials, ...patch },
        cvParsed: parsed,
        cvAutofilled: filled,
      };
    });
    // Advance ONLY from the CV screen. This resolves on a background poll that
    // outlives the screen that started it, so a physician who pressed Back
    // while their CV was being read would otherwise be yanked forward onto
    // Review mid-thought. The parse itself still lands either way — it is
    // waiting for them when they arrive.
    setStep((cur) => (cur === "cv" ? "review" : cur));
  }, []);

  /** Poll the parse until it reaches a terminal stage, then advance.
   *
   *  Polling rather than SSE deliberately: this runs for a handful of seconds on
   *  a page the physician is watching, and a streaming endpoint would be a
   *  second transport to keep alive through a proxy for no gain at this
   *  duration. The interval is short because the captions are supposed to feel
   *  like progress, and the cap exists so a background worker that died can
   *  never strand someone on an animation — after it, we go to Review with
   *  whatever we have, which is the same place a failure goes. */
  const pollCvParse = useCallback(async () => {
    const DEADLINE_MS = 90_000;
    const INTERVAL_MS = 900;
    const started = Date.now();
    for (;;) {
      await new Promise((r) => setTimeout(r, INTERVAL_MS));
      let body: Record<string, any> = {};
      try {
        const r = await api(`/api/onboarding/asclepius/cv/status?token=${encodeURIComponent(token)}`);
        body = (await readResponseJson(r)) as Record<string, any>;
        if (!r.ok) throw new Error("status");
      } catch {
        // A dropped poll is not a failed parse. Keep trying until the deadline;
        // the physician's CV is being read on the server either way.
        if (Date.now() - started > DEADLINE_MS) { applyParseAndReview(null); return; }
        continue;
      }
      const stage = (body.stage as CvStage | null) ?? "reading";
      setCvStage(stage);
      if (body.finished) { applyParseAndReview((body.parsed as CvParsed) ?? null); return; }
      if (Date.now() - started > DEADLINE_MS) { applyParseAndReview(null); return; }
    }
  }, [token, applyParseAndReview]);

  const uploadCv = useCallback(
    async (file: File) => {
      setStepError("");
      setCvUploading(true);
      setCvFilename(file.name);
      try {
        const form = new FormData();
        form.append("token", token);
        form.append("file", file);
        const r = await fetch(`${API_BASE}/api/onboarding/asclepius/cv`, {
          method: "POST", body: form, headers: apiHeaders(),
        });
        if (!r.ok) {
          const body = await readResponseJson(r);
          // Never a dead end. The CV is an accelerant, not a requirement, so an
          // upload that fails offers the manual path in the same breath.
          setStepError(
            (formatApiError(body) || "We couldn't read that file.")
            + " You can enter your details by hand instead.");
          setCvStage(null);
          setCvUploading(false);
          return false;
        }
        setCvStage("reading");
        setCvUploading(false);
        // The Review screen renders the same CV field the three-screen flow
        // does, so tell it what is already attached — otherwise a physician who
        // just uploaded a CV meets an empty "Attach a file" control on the very
        // next screen and reasonably concludes it did not work.
        setDataState((d) => ({
          ...d, credentials: { ...d.credentials, cvFilename: file.name },
        }));
        void pollCvParse();
        return true;
      } catch {
        setStepError("We couldn't attach that file. You can enter your details by hand instead.");
        setCvStage(null);
        setCvUploading(false);
        return false;
      }
    },
    [token, pollCvParse],
  );

  /** "No CV? Enter manually →" — the same Review screen, empty. */
  const skipCv = useCallback(() => {
    setStepError("");
    setCvStage(null);
    setDataState((d) => ({ ...d, cvParsed: null, cvAutofilled: [] }));
    setStep("review");
  }, []);

  const submitCredentials = useCallback(async () => {
    // Saves the whole credentials blob every time (the endpoint is an
    // idempotent upsert), so stopping after any of the three screens keeps
    // what was already typed.
    const ok = await saveCredentials("/api/onboarding/asclepius/credentials");
    if (ok) goNext();
    return ok;
  }, [saveCredentials, goNext]);

  /** Provision the account and land on the success screen, signed in.
   *  Shared by the physician flow (which reaches it from attestations) and the
   *  short signup (which reaches it from the password screen), so the session
   *  token is stored the same way for all three doors. */
  const provisionAndLand = useCallback(async () => {
    const fr = await api("/api/onboarding/asclepius/finish", {
      method: "POST",
      body: JSON.stringify({ token }),
    });
    const fbody = await readResponseJson(fr);
    if (!fr.ok) {
      setStepError(formatApiError(fbody) || `HTTP ${fr.status}`);
      return false;
    }
    const d = fbody as { workspace_url?: string; token?: string; awaiting_review?: boolean };
    // The session token is held in React state, not written to localStorage.
    // The portal is a DIFFERENT ORIGIN in production, so a token written here
    // is invisible there -- see the note where storeAsclepiusSession used to
    // live. openAsclepiusWorkspace trades it for a handoff code instead.
    setData({
      workspaceUrl: d.workspace_url || authApi.asclepiusPortalUrl(),
      asclepiusToken: d.token || "",
      awaitingReview: !!d.awaiting_review,
    });
    // v2 §2 screen 6: a physician whose application is awaiting review has no
    // workspace and no session — the server deliberately mints neither. Sending
    // them to the "your workspace is ready" screen would hand them a door that
    // 403s. The advisor/referrer short signup still lands there, because their
    // accounts really are open.
    setStep(d.awaiting_review ? "submitted" : "ascSuccess");
    return true;
  }, [token, setData]);

  /** The Review screen's Submit. Saves the whole credentials blob, then moves
   *  to attestations. The endpoint is an idempotent upsert of the whole blob and
   *  also mirrors the specialty onto the invite row, so this is one call and not
   *  the institution+credentials pair the old three-screen flow needed. */
  const submitReview = useCallback(async () => {
    const ok = await saveCredentials("/api/onboarding/asclepius/credentials");
    if (ok) setStep("attestations");
    return ok;
  }, [saveCredentials]);

  const submitAttestations = useCallback(async () => {
    setStepError("");
    const r = await api("/api/onboarding/asclepius/attestations", {
      method: "POST",
      body: JSON.stringify({ token, attestations: data.attestations }),
    });
    const body = await readResponseJson(r);
    if (!r.ok) {
      setStepError(formatApiError(body) || `HTTP ${r.status}`);
      return false;
    }
    // Team invites moved to the dashboard, so provision the workspace right
    // after attestations: sign-up ends here and lands the doctor in.
    return provisionAndLand();
  }, [token, data.attestations, provisionAndLand]);

  const addAscMember = useCallback(
    async (m: Omit<AsclepiusMember, "id" | "status">) => {
      setStepError("");
      const r = await api("/api/onboarding/asclepius/add-member", {
        method: "POST",
        body: JSON.stringify({
          token,
          full_name: `${m.firstName} ${m.lastName}`.trim(),
          email: m.email,
          role: m.role,
        }),
      });
      const body = await readResponseJson(r);
      if (!r.ok) {
        setStepError(formatApiError(body) || `HTTP ${r.status}`);
        return false;
      }
      const newMember: AsclepiusMember = { ...m, id: Date.now(), status: "Invited" };
      setDataState((d) => ({ ...d, ascMembers: [...d.ascMembers, newMember] }));
      return true;
    },
    [token],
  );

  const removeAscMember = useCallback((id: number) => {
    setDataState((d) => ({ ...d, ascMembers: d.ascMembers.filter((m) => m.id !== id) }));
  }, []);

  const finishAsclepius = useCallback(async () => {
    setStepError("");
    const r = await api("/api/onboarding/asclepius/finish", {
      method: "POST",
      body: JSON.stringify({ token }),
    });
    const body = await readResponseJson(r);
    if (!r.ok) {
      setStepError(formatApiError(body) || `HTTP ${r.status}`);
      return false;
    }
    const d = body as { workspace_url?: string; token?: string };
    // Was `body as { workspace_url?: string }` -- the endpoint returns a
    // session token and this path threw it away, so anyone reaching finish
    // through the team screen landed logged out.
    setData({
      workspaceUrl: d.workspace_url || authApi.asclepiusPortalUrl(),
      asclepiusToken: d.token || "",
    });
    setStep("ascSuccess");
    return true;
  }, [token, setData]);

  // ─────────────────────────────────────────
  // Asclepius branch (invited member).
  // ─────────────────────────────────────────
  const submitMemberCredentials = useCallback(async () => {
    const ok = await saveCredentials("/api/onboarding/member/credentials");
    if (ok) setStep("attestations");
    return ok;
  }, [saveCredentials]);

  const submitMemberAttestations = useCallback(async () => {
    setStepError("");
    const r = await api("/api/onboarding/member/attestations", {
      method: "POST",
      body: JSON.stringify({ token, attestations: data.attestations }),
    });
    const body = await readResponseJson(r);
    if (!r.ok) {
      setStepError(formatApiError(body) || `HTTP ${r.status}`);
      return false;
    }
    setStep("verify");
    return true;
  }, [token, data.attestations]);

  // Hard-gate email verification before the standing account is provisioned
  // (parity with the director's OTP step, mirrored against /member/*).
  const sendMemberOtp = useCallback(async () => {
    setStepError("");
    const r = await api("/api/onboarding/member/request-otp", {
      method: "POST",
      body: JSON.stringify({ token }),
    });
    const body = await readResponseJson(r);
    if (!r.ok) {
      setStepError(formatApiError(body) || `HTTP ${r.status}`);
      return false;
    }
    return true;
  }, [token]);

  const verifyMemberOtp = useCallback(
    async (code: string) => {
      setStepError("");
      const r = await api("/api/onboarding/member/verify-otp", {
        method: "POST",
        body: JSON.stringify({ token, code }),
      });
      const body = await readResponseJson(r);
      if (!r.ok) {
        setStepError(formatApiError(body) || "Invalid code");
        return false;
      }
      // Finishing moved OUT of here: the physician still has to choose a
      // password, and that step deliberately comes after the mailbox is proven.
      setStep("password");
      return true;
    },
    [token, setData],
  );

  /** POST the chosen password, then finish. One handler for both flows: the
   *  only difference is which endpoint pair they hit. */
  const submitPassword = useCallback(
    async (password: string) => {
      setStepError("");
      const isMember = mode === "member";
      const r = await api(
        isMember ? "/api/onboarding/member/password" : "/api/onboarding/asclepius/password",
        { method: "POST", body: JSON.stringify({ token, password }) },
      );
      const body = await readResponseJson(r);
      if (!r.ok) {
        setStepError(formatApiError(body) || "Could not save that password");
        return false;
      }
      if (!isMember) {
        if (signupKind !== "physician") {
          // The short signup ends here. The person row now exists (the password
          // endpoint upserts it), which is why the confidentiality checkbox
          // collected back on the identity screen is saved at this point rather
          // than when it was ticked: there was nothing to save it onto yet.
          if (signupKind === "advisor") {
            const ar = await api("/api/onboarding/asclepius/attestations", {
              method: "POST",
              body: JSON.stringify({
                token,
                attestations: {
                  ...data.attestations,
                  signedInitials:
                    data.attestations.signedInitials
                    || `${data.firstName.trim().charAt(0)}${data.lastName.trim().charAt(0)}`
                      .toUpperCase(),
                },
              }),
            });
            if (!ar.ok) {
              setStepError(formatApiError(await readResponseJson(ar)) || `HTTP ${ar.status}`);
              return false;
            }
          }
          return provisionAndLand();
        }
        // The director still has institution, credentials and attestations to go.
        setStep("institution");
        return true;
      }
      const fr = await api("/api/onboarding/member/finish", {
        method: "POST",
        body: JSON.stringify({ token }),
      });
      const fbody = await readResponseJson(fr);
      if (!fr.ok) {
        setStepError(formatApiError(fbody) || `HTTP ${fr.status}`);
        return false;
      }
      const d = fbody as { workspace_url?: string; token?: string };
      setData({
        workspaceUrl: d.workspace_url || authApi.asclepiusPortalUrl(),
        asclepiusToken: d.token || "",
      });
      setStep("ascSuccess");
      return true;
    },
    [mode, token, setData, signupKind, provisionAndLand, data.attestations,
     data.firstName, data.lastName],
  );

  /* The portal is a different origin, so a session cannot be handed over in
     localStorage. Trade the token for a single-use handoff code the portal
     redeems on load -- the same path SignInDialog has always used.

     If the trade fails (no token, network, an expired session) we still send
     them to the portal, where they get the login screen. That is the old
     behaviour and it is the right fallback: a doctor who can sign in is
     better off than one staring at an error on the success page. */
  /* Sign in and hand off, rather than linking away to a login page and making
     them arrive twice. Same two calls SignInDialog makes. */
  const signInToAsclepius = useCallback(
    async (email: string, password: string) => {
      setStepError("");
      try {
        const res = await authApi.asclepiusLogin(email, password);
        if (!res.token) throw new Error("Sign in failed");
        await authApi.redirectToAsclepiusPortal(res.token);
        return true;
      } catch (e: unknown) {
        setStepError(e instanceof Error ? e.message : "Sign in failed");
        return false;
      }
    },
    [],
  );

  const openAsclepiusWorkspace = useCallback(async () => {
    const sessionToken = data.asclepiusToken;
    if (sessionToken) {
      try {
        await authApi.redirectToAsclepiusPortal(sessionToken);
        return true;
      } catch {
        /* fall through to the plain redirect + manual sign-in */
      }
    }
    window.location.href = data.workspaceUrl || authApi.asclepiusPortalUrl();
    return true;
  }, [data.workspaceUrl, data.asclepiusToken]);

  const handleExit = useCallback(() => {
    window.location.href = "/";
  }, []);

  // ─────────────────────────────────────────
  // Stepper config.
  // ─────────────────────────────────────────
  // ascSignIn is not a step in any flow: it is where a finished or
  // already-registered link lands instead of one. Showing it in the stepper
  // would draw a progress bar for a journey the person is not on.
  const stepperKeys: StepKey[] = order.filter(
    (k) => k !== "success" && k !== "ascSuccess" && k !== "ascSignIn" && k !== "submitted");
  const stepperLabels = stepperKeys.map((k) => STEP_LABELS[k] ?? k);
  const stepperIndex = stepperKeys.indexOf(step);
  const showStepper = stepperIndex >= 0;

  const content = useMemo(() => {
    if (loading) {
      return (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "center", padding: "120px 0" }}>
          <p style={{ color: "var(--ink-soft)", fontSize: 14 }}>Loading onboarding…</p>
        </div>
      );
    }
    if (bootError) {
      return (
        <div style={{ maxWidth: 480, margin: "80px auto", textAlign: "center" }}>
          <h2 style={{ color: "var(--ink)", marginBottom: 12 }}>This onboarding link can&apos;t be loaded.</h2>
          <p style={{ color: "var(--ink-soft)", fontSize: 14 }}>{bootError}</p>
          {/* An expired or already-used link is overwhelmingly likely to belong
              to somebody who already HAS an account. This was a dead end with
              no route anywhere, which is a strange thing to show a physician
              whose only problem is that they finished signing up. */}
          <p style={{ color: "var(--ink-soft)", fontSize: 14, marginTop: 18 }}>
            Already have an account?{" "}
            <button
              type="button"
              onClick={() => { setBootError(""); setStep("ascSignIn"); }}
              style={{
                background: "none", border: "none", padding: 0,
                color: "var(--ink)", fontSize: 14, cursor: "pointer",
                textDecoration: "underline", textUnderlineOffset: 3,
              }}
            >
              Sign in
            </button>
          </p>
        </div>
      );
    }

    switch (step) {
      case "identity":
        return (
          <Step1NameEmail
            data={data}
            setData={setData}
            onNext={submitStep1}
            onSignIn={() => {
              setSignInReason("account_exists");
              setStep("ascSignIn");
            }}
            error={stepError}
            kind={mode === "member" ? "physician" : signupKind}
          />
        );
      case "verify":
        return (
          <Step2Verify
            data={data}
            onSendCode={mode === "member" ? sendMemberOtp : sendOtp}
            onVerify={mode === "member" ? verifyMemberOtp : verifyOtp}
            onBack={goBack}
            error={stepError}
            eyebrow={mode === "member" ? "Step 3 of 3" : "Step 2"}
          />
        );
      case "password":
        return (
          <StepChoosePassword
            data={data}
            onSubmit={submitPassword}
            onBack={goBack}
            error={stepError}
            eyebrow={
              mode === "member" ? "Step 4 of 4"
                : signupKind !== "physician" ? "Step 3 of 3"
                  : "Step 3"
            }
          />
        );
      // Archangel
      case "org":
        return <Step3Org data={data} setData={setData} onNext={submitOrg} onBack={goBack} error={stepError} />;
      case "team":
        return (
          <Step4YourTeam
            data={data}
            onAddMember={addTeamMember}
            onRemoveMember={removeMember}
            onNext={finishOnboarding}
            onBack={goBack}
            error={stepError}
          />
        );
      case "signin":
        return <Step5SignIn data={data} slug={slug} onSignIn={signIn} onBack={goBack} error={stepError} />;
      case "success":
        return <Step6Success data={data} onOpenWorkspace={openWorkspace} />;
      // Asclepius
      case "institution":
        return <Step4Institution data={data} setData={setData} onNext={submitInstitution} onBack={goBack} error={stepError} />;
      case "credentials":
      case "credTraining":
      case "credRare": {
        const phase = step === "credentials" ? 1 : step === "credTraining" ? 2 : 3;
        const eyebrows: Record<number, string> = {
          1: "Step 5 of 8",
          2: "Step 6 of 8",
          3: "Step 7 of 8",
        };
        return (
          <Step5Credentials
            data={data}
            setData={setData}
            // Every screen saves. The endpoint is an idempotent upsert of the
            // whole credentials blob, so a physician who stops after screen 2
            // finds screens 1 and 2 already filled when they come back.
            onNext={mode === "member" ? submitMemberCredentials : submitCredentials}
            onBack={goBack}
            error={stepError}
            eyebrow={mode === "member" ? "Step 1 of 4" : eyebrows[phase]}
            memberMode={mode === "member"}
            phase={mode === "member" ? undefined : (phase as 1 | 2 | 3)}
            // Every non-clinical door relaxes the physician credential
            // screens: they exist to check a doctor, and have nothing to ask
            // someone who is not claiming to be one.
            relaxed={mode !== "member"
              && ["general", "advisor", "referrer"].includes(signupFlavor)}
          />
        );
      }
      // ── Onboarding v2 §2 ──
      case "cv":
        return (
          <StepCv
            data={data}
            stage={cvStage}
            filename={cvFilename}
            uploading={cvUploading}
            onUpload={uploadCv}
            onSkip={skipCv}
            onBack={goBack}
            error={stepError}
            eyebrow="Step 3 of 5"
          />
        );
      case "review":
        return (
          <Step5Credentials
            data={data}
            setData={setData}
            onNext={submitReview}
            onBack={goBack}
            error={stepError}
            eyebrow="Step 4 of 5"
            reviewMode
            submitLabel="Submit my application"
          />
        );
      case "submitted":
        return (
          <StepApplicationSubmitted
            data={data}
            onSignIn={() => {
              setSignInReason("complete");
              setStep("ascSignIn");
            }}
          />
        );
      case "attestations":
        return (
          <Step6Attestations
            data={data}
            setData={setData}
            onNext={mode === "member" ? submitMemberAttestations : submitAttestations}
            onBack={goBack}
            error={stepError}
            eyebrow={mode === "member" ? "Step 2 of 4"
              : signupKind === "physician" && data.product === "asclepius" ? "Step 5 of 5"
                : "Step 8 of 8"}
            // v2 §2: nothing opens on submit — a human reads the application
            // first — so promising a workspace here would be the last thing we
            // said before two days of silence.
            finishLabel={mode === "member" ? "Sign & continue"
              : signupKind === "physician" && data.product === "asclepius"
                ? "Sign & send my application"
                : "Sign & open my workspace"}
          />
        );
      case "ascTeam":
        return (
          <Step7AsclepiusTeam
            data={data}
            onAddMember={addAscMember}
            onRemoveMember={removeAscMember}
            onNext={finishAsclepius}
            onBack={goBack}
            error={stepError}
          />
        );
      case "ascSignIn":
        return (
          <StepAsclepiusSignIn
            data={data}
            onSignIn={signInToAsclepius}
            error={stepError}
            reason={signInReason}
          />
        );
      case "ascSuccess":
      default:
        return (
          <Step8AsclepiusSuccess
            data={data}
            onOpenWorkspace={openAsclepiusWorkspace}
            memberMode={mode === "member"}
            kind={mode === "member" ? "physician" : signupKind}
          />
        );
    }
  }, [
    loading,
    bootError,
    step,
    data,
    setData,
    submitStep1,
    sendOtp,
    verifyOtp,
    submitOrg,
    addTeamMember,
    removeMember,
    finishOnboarding,
    slug,
    signIn,
    openWorkspace,
    submitInstitution,
    submitCredentials,
    submitMemberCredentials,
    submitAttestations,
    submitMemberAttestations,
    sendMemberOtp,
    verifyMemberOtp,
    addAscMember,
    removeAscMember,
    finishAsclepius,
    openAsclepiusWorkspace,
    goBack,
    stepError,
    mode,
    signupFlavor,
    signupKind,
    submitPassword,
    signInToAsclepius,
    signInReason,
    cvStage,
    cvFilename,
    cvUploading,
    uploadCv,
    skipCv,
    submitReview,
  ]);

  return (
    <div className="ah-onb-root">
      <OnboardingStyles />
      <ChromeHeader onExit={handleExit} />
      <main style={{ flex: 1, padding: "56px 24px 80px", position: "relative" }}>
        {showStepper && !loading && !bootError && (
          <Stepper steps={stepperLabels} currentIndex={stepperIndex} />
        )}
        {content}
      </main>
    </div>
  );
}
