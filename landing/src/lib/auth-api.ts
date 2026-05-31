/**
 * Archangel Health landing — auth API client.
 * Uses relative /api in dev (Vite proxy to backend) or VITE_API_URL when set.
 */

/** Empty in dev (Vite proxies /api); set VITE_API_URL in production (e.g. https://app.archangelhealth.ai). */
export const API_BASE =
  (import.meta as unknown as { env: { VITE_API_URL?: string } }).env?.VITE_API_URL ?? '';

const env = import.meta as unknown as {
  env: { VITE_DASHBOARD_URL?: string; VITE_API_URL?: string; DEV?: boolean };
};

/** Backend origin for doctor portal redirects (no trailing slash). */
export function dashboardBaseUrl(): string {
  const raw =
    env.env.VITE_DASHBOARD_URL ?? env.env.VITE_API_URL ?? (env.env.DEV ? "http://localhost:8000" : "");
  return raw.replace(/\/$/, "");
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
    const err = await res.json().catch(() => ({}));
    throw new Error((err as { detail?: string }).detail ?? "Sign in failed");
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
    const err = await res.json().catch(() => ({}));
    throw new Error((err as { detail?: string }).detail ?? 'Sign in failed');
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
