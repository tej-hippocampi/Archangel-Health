/**
 * OnboardingWizard — Archangel Health "Director of TEAM Initiative"
 * health-system onboarding (5 steps + success).
 *
 * Visual spec: design_handoff_onboarding_flow/README.md (the redesigned flow).
 * Step → backend endpoint mapping:
 *   1  NameEmail   →  POST /api/onboarding/step1-identity
 *   2  Verify      →  POST /api/onboarding/request-otp + /verify-otp
 *   3  Org         →  POST /api/onboarding/step3-organization
 *   4  Your TEAM   →  POST /api/onboarding/add-team-member (per row)
 *                    + POST /api/onboarding/finish (on Continue)
 *   5  Sign-in     →  POST /api/tenant/{slug}/auth/login
 *   6  Success     →  redirect to dashboard with auth token
 *
 * The visual stepper has 5 nodes ("You", "Verify", "Health system",
 * "Your TEAM", "Sign in") and is hidden on step 6 (success).
 *
 * Each PrimaryButton handler returns a Promise<boolean>. The button stays
 * Idle (no fake success) when the handler resolves `false` — this is how
 * we surface server errors without misleading the user.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import { API_BASE } from "@/lib/auth-api";

import OnboardingStyles from "./onboarding/OnboardingStyles";
import { ChromeHeader, Stepper } from "./onboarding/primitives";
import {
  Step1NameEmail,
  Step2Verify,
  Step3Org,
  Step4YourTeam,
  Step5SignIn,
  Step6Success,
  type Member,
  type OnboardingData,
  type RoleLabel,
} from "./onboarding/steps";

type Props = { token: string };

const STEPPER_LABELS = ["You", "Verify", "Health system", "Your TEAM", "Sign in"];

// Pass-4 role taxonomy: the director slot is auto-seeded as a `surgeon` on
// /finish, so the wizard only invites RN coordinators and NP/PAs. Caps:
// 1 RN coordinator + 2 NP/PAs + 1 director (surgeon) = 4 total.
const ROLE_TO_API: Record<RoleLabel, "rn_coordinator" | "np_pa"> = {
  "RN Care Coordinator": "rn_coordinator",
  "NP / PA": "np_pa",
};

/** Map server-side role labels (incl. legacy "doctor"/"nurse" + new
 *  pass-4 tokens) onto the display labels the wizard uses. */
function normalizeRoleLabel(raw: unknown): RoleLabel {
  const s = typeof raw === "string" ? raw.trim().toLowerCase() : "";
  if (
    s === "rn_coordinator" ||
    s === "rn care coordinator" ||
    s === "nurse" ||
    s === "nurse / care coordinator" ||
    s === "nurse/care coordinator"
  ) {
    return "RN Care Coordinator";
  }
  if (s === "np_pa" || s === "np / pa" || s === "np/pa" || s === "nppa") {
    return "NP / PA";
  }
  return "RN Care Coordinator";
}

type SessionResponse = {
  status?: "pending" | "complete";
  step?: number;
  slug?: string;
  sign_in_url?: string;
  director_first_name?: string;
  director_last_name?: string;
  director_email?: string;
  health_system_name?: string;
  surgery_department?: string;
  phone?: string;
  team_members?: Array<{
    id?: number;
    first_name?: string;
    last_name?: string;
    email?: string;
    role?: string;
    status?: string;
  }>;
};

const DASHBOARD_URL =
  (import.meta as unknown as { env: { VITE_DASHBOARD_URL?: string; VITE_API_URL?: string } }).env
    .VITE_DASHBOARD_URL ??
  (import.meta as unknown as { env: { VITE_API_URL?: string } }).env.VITE_API_URL ??
  "http://localhost:8000";

function api(path: string, init?: RequestInit): Promise<Response> {
  return fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
}

/** FastAPI errors come as `detail: string` (HTTPException) or `detail: array` (422). */
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

export default function OnboardingWizard({ token }: Props) {
  const [stepIndex, setStepIndex] = useState(0);
  const [data, setDataState] = useState<OnboardingData>({
    firstName: "",
    lastName: "",
    email: "",
    orgName: "",
    department: "",
    phone: "",
    members: [],
  });
  const setData = useCallback((patch: Partial<OnboardingData>) => {
    setDataState((d) => ({ ...d, ...patch }));
  }, []);

  const [slug, setSlug] = useState("");
  const [authToken, setAuthToken] = useState("");

  /** Per-step transient error surface; cleared whenever the user advances. */
  const [stepError, setStepError] = useState("");
  const [bootError, setBootError] = useState("");
  const [loading, setLoading] = useState(true);

  // ─────────────────────────────────────────
  // Session bootstrap — resume in-progress onboarding.
  // ─────────────────────────────────────────
  const loadSession = useCallback(async () => {
    setLoading(true);
    setBootError("");
    try {
      const r = await api(`/api/onboarding/session?token=${encodeURIComponent(token)}`);
      const body = await readResponseJson(r);
      if (!r.ok) {
        setBootError(formatApiError(body) || `HTTP ${r.status}`);
        return;
      }
      const d = body as SessionResponse;
      if (d.slug) setSlug(d.slug);

      // Hydrate the form state from previously-saved server values so a
      // reload mid-flow (or returning via the magic link later) doesn't drop
      // what the director already typed. Empty strings overwrite nothing
      // because the initial state is also empty.
      const hydratedMembers: Member[] = (d.team_members ?? []).map((m, idx) => ({
        id: typeof m.id === "number" && m.id > 0 ? m.id : Date.now() + idx,
        firstName: (m.first_name ?? "").trim(),
        lastName: (m.last_name ?? "").trim(),
        email: (m.email ?? "").trim(),
        role: normalizeRoleLabel(m.role),
        status: m.status === "Active" ? "Active" : "Invited",
      }));
      setDataState({
        firstName: (d.director_first_name ?? "").trim(),
        lastName: (d.director_last_name ?? "").trim(),
        email: (d.director_email ?? "").trim(),
        orgName: (d.health_system_name ?? "").trim(),
        department: (d.surgery_department ?? "").trim(),
        phone: (d.phone ?? "").trim(),
        members: hydratedMembers,
      });

      if (d.status === "complete") {
        // Onboarding already finished → drop the user at the in-wizard sign-in
        // step (matches the design's "Step 5 of 5") so they can sign in with
        // the temporary password we mailed them.
        setStepIndex(4);
        return;
      }
      // Backend's `step` is the highest completed step (0..3). The wizard's
      // `stepIndex` is the screen to show (0..5). Resume on the next pending
      // screen — which is just the same number.
      setStepIndex(Math.min(4, Math.max(0, Number(d.step) || 0)));
    } catch (e: unknown) {
      setBootError(e instanceof Error ? e.message : "Could not load onboarding");
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void loadSession();
  }, [loadSession]);

  const goBack = useCallback(() => {
    setStepError("");
    setStepIndex((i) => Math.max(0, i - 1));
  }, []);

  // ─────────────────────────────────────────
  // Per-step actions.
  // Each returns Promise<boolean>: false = stay on Idle (button), true = advance.
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
    setStepIndex(1);
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
      setStepIndex(2);
      return true;
    },
    [token],
  );

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
    setStepIndex(3);
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
    // Local-only — there's no backend `delete-team-member` (yet). The next
    // navigation away from this step settles the team list as-displayed.
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
    setStepIndex(4);
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
      setStepIndex(5);
      return true;
    },
    [slug],
  );

  const openWorkspace = useCallback(() => {
    const dest = `${DASHBOARD_URL.replace(/\/$/, "")}/#auth=${encodeURIComponent(authToken)}`;
    window.location.href = dest;
    // Resolve true so the success state can flash before the browser navigates.
    return true;
  }, [authToken]);

  const handleExit = useCallback(() => {
    // "Save & exit" — your token stays valid; redirect home so progress isn't
    // lost (the next visit picks up from /api/onboarding/session).
    window.location.href = "/";
  }, []);

  const showStepper = stepIndex < 5;

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

    switch (stepIndex) {
      case 0:
        return <Step1NameEmail data={data} setData={setData} onNext={submitStep1} error={stepError} />;
      case 1:
        return <Step2Verify data={data} onSendCode={sendOtp} onVerify={verifyOtp} onBack={goBack} error={stepError} />;
      case 2:
        return <Step3Org data={data} setData={setData} onNext={submitOrg} onBack={goBack} error={stepError} />;
      case 3:
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
      case 4:
        return <Step5SignIn data={data} slug={slug} onSignIn={signIn} onBack={goBack} error={stepError} />;
      case 5:
      default:
        return <Step6Success data={data} onOpenWorkspace={openWorkspace} />;
    }
  }, [
    loading,
    bootError,
    stepIndex,
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
    goBack,
    stepError,
  ]);

  return (
    <div className="ah-onb-root">
      <OnboardingStyles />
      <ChromeHeader onExit={handleExit} />
      <main style={{ flex: 1, padding: "56px 24px 80px", position: "relative" }}>
        {showStepper && !loading && !bootError && <Stepper steps={STEPPER_LABELS} currentIndex={stepIndex} />}
        {content}
      </main>
    </div>
  );
}
