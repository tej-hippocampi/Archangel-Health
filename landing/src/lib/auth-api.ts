/**
 * Archangel Health landing — auth API client.
 * Uses relative /api in dev (Vite proxy to backend) or VITE_API_URL when set.
 */

const viteEnv = ((import.meta as unknown as {
  env?: { VITE_DASHBOARD_URL?: string; VITE_API_URL?: string; DEV?: boolean };
})?.env ?? {});

/**
 * Production backend origin (FastAPI: API routes + doctor/patient portals).
 * Used as a fallback when VITE_API_URL / VITE_DASHBOARD_URL are not configured
 * on the deployed landing (e.g. missing Vercel env vars). Without this, the
 * static-site origin has no /api backend, so doctor sign-in silently fails.
 */
const PROD_BACKEND_ORIGIN = "https://app.archangelhealth.ai";

function stripTrailingSlash(url: string): string {
  return url.replace(/\/$/, "");
}

/**
 * Resolve the backend origin. Priority: explicit env var → dev fallback (Vite
 * proxy / localhost) → known production backend. Returns "" to keep calls
 * same-origin (dev proxy, or when the page is already served by the backend).
 */
function resolveBackendOrigin(explicit: string | undefined, devFallback: string): string {
  if (explicit) return stripTrailingSlash(explicit);
  if (viteEnv.DEV) return devFallback;
  if (typeof window !== "undefined") {
    try {
      if (window.location.host === new URL(PROD_BACKEND_ORIGIN).host) return "";
    } catch {
      /* ignore malformed location */
    }
  }
  return PROD_BACKEND_ORIGIN;
}

/** Empty in dev (Vite proxies /api); VITE_API_URL or the prod backend otherwise. */
export const API_BASE = resolveBackendOrigin(viteEnv.VITE_API_URL, "");

/** Backend origin for doctor portal redirects (no trailing slash). */
export function dashboardBaseUrl(): string {
  return resolveBackendOrigin(
    viteEnv.VITE_DASHBOARD_URL ?? viteEnv.VITE_API_URL,
    "http://localhost:8000",
  );
}

/** Doctor roster UI — site root redirects to sign-in, so pass JWT here. */
export function doctorAppUrl(): string {
  const base = dashboardBaseUrl();
  return base ? `${base}/doctor/app` : "";
}

export function doctorSignInUrl(): string {
  const base = dashboardBaseUrl();
  return base ? `${base}/doctor/sign-in` : "";
}

export type User = { email: string; name?: string | null; role?: string | null };

export type AuthResponse = {
  access_token: string;
  token_type: string;
  user: User;
};

export type DoctorOnboardPayload = {
  name: string;
  email: string;
  office_phone: string;
  doctor_type: string;
  hospital_affiliations: string;
};

export type DoctorProfile = {
  name: string;
  email: string;
  office_phone: string;
  doctor_type: string;
  hospital_affiliations: string;
  clinic_code: string;
  /** Same value as `clinic_code`; preferred display field from API. */
  health_system_code?: string;
};

export type PatientByCodesResponse = {
  patient_id: string;
  dashboard_url: string;
};

export type DemoSignInRoute = { type: "tenant" | "landing"; slug?: string | null };

export type TenantAuthResponse = {
  access_token: string;
  token_type?: string;
  user?: { email?: string; name?: string; role?: string; is_team_director?: boolean };
};

export type PortalHandoffResponse = {
  handoff_code: string;
  expires_in_seconds: number;
};

/**
 * Build an actionable error from a failed response. A non-JSON body means the
 * request hit the static-site origin / a proxy instead of the backend API —
 * surface that instead of a generic "Sign in failed".
 */
async function errorDetail(res: Response, fallback: string): Promise<string> {
  const contentType = res.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    const body = await res.json().catch(() => null);
    const detail =
      body && typeof body === "object" ? (body as { detail?: string }).detail : undefined;
    if (detail) return detail;
    return `${fallback} (${res.status}).`;
  }
  return `Cannot reach the backend API (got a non-JSON ${res.status} response from ${res.url}). The site's API URL is likely misconfigured.`;
}

export async function getDemoSignInRoutes(): Promise<Record<string, DemoSignInRoute>> {
  try {
    const res = await fetch(`${API_BASE}/api/demo/sign-in-routes`);
    if (!res.ok) return {};
    const data = (await res.json()) as { routes?: Record<string, DemoSignInRoute> };
    return data.routes || {};
  } catch {
    return {};
  }
}

export async function tenantLogin(
  slug: string,
  email: string,
  password: string
): Promise<TenantAuthResponse> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/api/tenant/${encodeURIComponent(slug)}/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
  } catch {
    throw new Error("Cannot reach server. Make sure the backend is running (port 8000).");
  }
  if (!res.ok) {
    throw new Error(await errorDetail(res, "Sign in failed"));
  }
  return res.json();
}

export async function createPortalHandoff(accessToken: string): Promise<PortalHandoffResponse> {
  const res = await fetch(`${API_BASE}/api/auth/portal-handoff`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${accessToken}`,
    },
    body: "{}",
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error((err as { detail?: string }).detail ?? "Could not create portal handoff");
  }
  return res.json();
}

export async function redirectToDoctorPortal(accessToken: string): Promise<void> {
  const signIn = doctorSignInUrl();
  if (!signIn) throw new Error("Could not open doctor portal.");
  const handoff = await createPortalHandoff(accessToken);
  if (!handoff.handoff_code) throw new Error("Could not open doctor portal.");
  window.location.href = `${signIn}?handoff=${encodeURIComponent(handoff.handoff_code)}`;
}

export async function login(email: string, password: string): Promise<AuthResponse> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/api/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
    });
  } catch (e) {
    throw new Error('Cannot reach server. Make sure the backend is running (port 8000).');
  }
  if (!res.ok) {
    throw new Error(await errorDetail(res, 'Sign in failed'));
  }
  return res.json();
}

export async function register(
  email: string,
  password: string,
  name?: string
): Promise<AuthResponse> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/api/auth/register`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password, name: name || undefined }),
    });
  } catch (e) {
    throw new Error('Cannot reach server. Make sure the backend is running (port 8000).');
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error((err as { detail?: string }).detail ?? 'Registration failed');
  }
  return res.json();
}

export async function getMe(token: string): Promise<User | null> {
  const res = await fetch(`${API_BASE}/api/auth/me`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) return null;
  return res.json();
}

export async function doctorOnboard(
  token: string,
  payload: DoctorOnboardPayload
): Promise<DoctorProfile> {
  const res = await fetch(`${API_BASE}/api/doctor/onboard`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error((err as { detail?: string }).detail ?? "Onboarding failed");
  }
  return res.json();
}

export async function getDoctorProfile(token: string): Promise<DoctorProfile | null> {
  const res = await fetch(`${API_BASE}/api/doctor/profile`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) return null;
  return res.json();
}

export async function getPatientByCodes(
  healthSystemCode: string,
  resourceCode: string
): Promise<PatientByCodesResponse> {
  const params = new URLSearchParams({
    health_system_code: healthSystemCode.trim(),
    resource_code: resourceCode.trim(),
  });
  const res = await fetch(`${API_BASE}/api/patient/by-codes?${params}`);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error((err as { detail?: string }).detail ?? "Invalid codes");
  }
  return res.json();
}
