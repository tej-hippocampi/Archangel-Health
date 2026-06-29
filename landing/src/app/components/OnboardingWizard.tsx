/**
 * OnboardingWizard — Archangel Health onboarding.
 *
 * Two products, one account. After identity (Step 1) + email verification
 * (Step 2), the director picks a product (Step 3):
 *
 *   Archangel (clinical TEAM platform) — unchanged 5-step flow:
 *     identity → verify → product → health system → your TEAM → sign in → success
 *     (backend: /step1-identity, /request-otp+/verify-otp, /select-product,
 *      /step3-organization, /add-team-member + /finish, /tenant login)
 *
 *   Asclepius (data-training product) — Steps 4–8:
 *     identity → verify → product → institution → credentials → attestations
 *       → team → success
 *     (backend: /asclepius/{institution,credentials,attestations,add-member,finish})
 *     Compliance/HIPAA gates do not apply to this plane — no PHI is collected.
 *
 * Invited clinicians (mode="member") open /onboard/m/<token> and run a short
 * flow: credentials → attestations → workspace, inheriting org + specialty
 * from their director (backend: /member/{session,credentials,attestations,finish}).
 *
 * Each PrimaryButton handler returns Promise<boolean>; resolving `false` keeps
 * the button Idle so server errors don't fake-flash success.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import { API_BASE } from "@/lib/auth-api";
import * as authApi from "@/lib/auth-api";

import OnboardingStyles from "./onboarding/OnboardingStyles";
import { ChromeHeader, Stepper } from "./onboarding/primitives";
import {
  ASCLEPIUS_ROLE_LABELS,
  Step1NameEmail,
  Step2Verify,
  Step3Org,
  Step3Product,
  Step4Institution,
  Step4YourTeam,
  Step5Credentials,
  Step5SignIn,
  Step6Attestations,
  Step6Success,
  Step7AsclepiusTeam,
  Step8AsclepiusSuccess,
  emptyAttestations,
  emptyCredentials,
  type AsclepiusMember,
  type AsclepiusRole,
  type Member,
  type OnboardingData,
  type Product,
  type RoleLabel,
} from "./onboarding/steps";

type Mode = "director" | "member";
type Props = { token: string; mode?: Mode };

type StepKey =
  | "identity"
  | "verify"
  | "product"
  | "org"
  | "team"
  | "signin"
  | "success"
  | "institution"
  | "credentials"
  | "attestations"
  | "ascTeam"
  | "ascSuccess";

const STEP_LABELS: Partial<Record<StepKey, string>> = {
  identity: "You",
  verify: "Verify",
  product: "Product",
  org: "Health system",
  team: "Your TEAM",
  signin: "Sign in",
  institution: "Institution",
  credentials: "Credentials",
  attestations: "Attestations",
  ascTeam: "Team",
};

/** Ordered step list for the active flow (drives Back + the stepper). */
function orderFor(mode: Mode, product: Product | ""): StepKey[] {
  if (mode === "member") return ["credentials", "attestations", "ascSuccess"];
  const head: StepKey[] = ["identity", "verify", "product"];
  if (product === "asclepius") {
    return [...head, "institution", "credentials", "attestations", "ascTeam", "ascSuccess"];
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
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
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
    try {
      return JSON.stringify(detail);
    } catch {
      return "Request failed";
    }
  }
  return "Request failed";
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
  };
}

export default function OnboardingWizard({ token, mode = "director" }: Props) {
  const [step, setStep] = useState<StepKey>(mode === "member" ? "credentials" : "identity");
  const [data, setDataState] = useState<OnboardingData>(initialData);
  const setData = useCallback((patch: Partial<OnboardingData>) => {
    setDataState((d) => ({ ...d, ...patch }));
  }, []);

  const [slug, setSlug] = useState("");
  const [authToken, setAuthToken] = useState("");
  const [stepError, setStepError] = useState("");
  const [bootError, setBootError] = useState("");
  const [loading, setLoading] = useState(true);

  // ─────────────────────────────────────────
  // Session bootstrap — resume in-progress onboarding.
  // ─────────────────────────────────────────
  const loadDirectorSession = useCallback(async () => {
    const r = await api(`/api/onboarding/session?token=${encodeURIComponent(token)}`);
    const body = await readResponseJson(r);
    if (!r.ok) {
      setBootError(formatApiError(body) || `HTTP ${r.status}`);
      return;
    }
    const d = body as Record<string, any>;
    if (d.slug) setSlug(d.slug);
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
      credentials: savedCreds
        ? { ...emptyCredentials(fullLegal), ...d.director_credentials }
        : emptyCredentials(fullLegal),
      attestations:
        d.director_attestations && Object.keys(d.director_attestations).length > 0
          ? { ...emptyAttestations(), ...d.director_attestations }
          : emptyAttestations(),
    }));

    if (d.status === "complete") {
      setStep(product === "asclepius" ? "ascSuccess" : "signin");
      return;
    }
    // Resume to the right screen. `step` is the backend's highest completed
    // step (0 identity, 1 verify-done, 2 verified, 3 org/institution saved).
    // Below step 3 we always show product selection so a reload right after
    // verification doesn't silently default to Archangel (product col defaults
    // to 'archangel'). Credentials/attestations don't bump the counter, so for
    // Asclepius we resume by inspecting what's already saved.
    const stepNum = Number(d.step) || 0;
    const savedAtts =
      d.director_attestations && Object.keys(d.director_attestations).length > 0;
    if (stepNum < 1) setStep("identity");
    else if (stepNum < 2) setStep("verify");
    else if (stepNum < 3) setStep("product");
    else if (product === "asclepius") {
      if (!savedCreds) setStep("credentials");
      else if (!savedAtts) setStep("attestations");
      else setStep("ascTeam");
    } else setStep("team");
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

  const order = useMemo(() => orderFor(mode, data.product), [mode, data.product]);

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
      }),
    });
    const body = await readResponseJson(r);
    if (!r.ok) {
      setStepError(formatApiError(body) || `HTTP ${r.status}`);
      return false;
    }
    setStep("verify");
    return true;
  }, [token, data.firstName, data.lastName, data.email]);

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
      setStep("product");
      return true;
    },
    [token],
  );

  const selectProduct = useCallback(
    async (product: Product) => {
      setStepError("");
      const r = await api("/api/onboarding/select-product", {
        method: "POST",
        body: JSON.stringify({ token, product }),
      });
      const body = await readResponseJson(r);
      if (!r.ok) {
        setStepError(formatApiError(body) || `HTTP ${r.status}`);
        return false;
      }
      setData({ product });
      setStep(product === "asclepius" ? "institution" : "org");
      return true;
    },
    [token, setData],
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
        setStepError("Workspace not ready yet — refresh and try again.");
        return false;
      }
      const r = await fetch(`${API_BASE}/api/tenant/${encodeURIComponent(slug)}/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
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
        org_name: data.orgName,
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

  const submitCredentials = useCallback(async () => {
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
    setStep("ascTeam");
    return true;
  }, [token, data.attestations]);

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
    const d = body as { workspace_url?: string };
    setData({ workspaceUrl: d.workspace_url || authApi.asclepiusPortalUrl() });
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
    const fr = await api("/api/onboarding/member/finish", {
      method: "POST",
      body: JSON.stringify({ token }),
    });
    const fbody = await readResponseJson(fr);
    if (!fr.ok) {
      setStepError(formatApiError(fbody) || `HTTP ${fr.status}`);
      return false;
    }
    const d = fbody as { workspace_url?: string };
    setData({ workspaceUrl: d.workspace_url || authApi.asclepiusPortalUrl() });
    setStep("ascSuccess");
    return true;
  }, [token, data.attestations, setData]);

  const openAsclepiusWorkspace = useCallback(() => {
    window.location.href = data.workspaceUrl || authApi.asclepiusPortalUrl();
    return true;
  }, [data.workspaceUrl]);

  const handleExit = useCallback(() => {
    window.location.href = "/";
  }, []);

  // ─────────────────────────────────────────
  // Stepper config.
  // ─────────────────────────────────────────
  const stepperKeys: StepKey[] = order.filter((k) => k !== "success" && k !== "ascSuccess");
  const stepperLabels = stepperKeys.map((k) => STEP_LABELS[k] ?? k);
  const stepperIndex = stepperKeys.indexOf(step);
  const showStepper = stepperIndex >= 0;

  const content = useMemo(() => {
    if (loading) {
      return (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "center", padding: "120px 0" }}>
          <p style={{ color: "rgba(245,245,247,0.62)", fontSize: 14 }}>Loading onboarding…</p>
        </div>
      );
    }
    if (bootError) {
      return (
        <div style={{ maxWidth: 480, margin: "80px auto", textAlign: "center" }}>
          <h2 style={{ color: "#F5F5F7", marginBottom: 12 }}>This onboarding link can&apos;t be loaded.</h2>
          <p style={{ color: "rgba(245,245,247,0.62)", fontSize: 14 }}>{bootError}</p>
        </div>
      );
    }

    switch (step) {
      case "identity":
        return <Step1NameEmail data={data} setData={setData} onNext={submitStep1} error={stepError} />;
      case "verify":
        return <Step2Verify data={data} onSendCode={sendOtp} onVerify={verifyOtp} onBack={goBack} error={stepError} />;
      case "product":
        return <Step3Product data={data} onSelect={selectProduct} onBack={goBack} error={stepError} />;
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
        return (
          <Step5Credentials
            data={data}
            setData={setData}
            onNext={mode === "member" ? submitMemberCredentials : submitCredentials}
            onBack={goBack}
            error={stepError}
            eyebrow={mode === "member" ? "Step 1 of 2" : "Step 5 of 7"}
            memberMode={mode === "member"}
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
            eyebrow={mode === "member" ? "Step 2 of 2" : "Step 6 of 7"}
            finishLabel={mode === "member" ? "Sign & open my workspace" : "Sign & continue"}
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
      case "ascSuccess":
      default:
        return <Step8AsclepiusSuccess data={data} onOpenWorkspace={openAsclepiusWorkspace} memberMode={mode === "member"} />;
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
    selectProduct,
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
    addAscMember,
    removeAscMember,
    finishAsclepius,
    openAsclepiusWorkspace,
    goBack,
    stepError,
    mode,
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
