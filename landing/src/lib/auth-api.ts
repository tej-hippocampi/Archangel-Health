/// <reference types="vite/client" />
/**
 * Archangel Health landing — auth API client.
 * Uses relative /api in dev (Vite proxy to backend) or VITE_API_URL when set.
 */

// Access `import.meta.env` directly — Vite substitutes it statically, and any
// indirection (casts, optional chaining on import.meta) defeats the
// substitution, leaving env undefined in dev so every call silently fell
// through to the production backend instead of the Vite proxy.
type ViteEnv = { VITE_DASHBOARD_URL?: string; VITE_API_URL?: string; DEV?: boolean };
const viteEnv: ViteEnv = (import.meta.env as ViteEnv | undefined) ?? {};

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

/**
 * Host the sign-in forms authenticate against, for display. Empty when calls
 * are same-origin (dev proxy / served by the backend). Surfacing this matters:
 * without VITE_API_URL a deployed/preview landing silently falls back to the
 * production backend, and a user comparing it to a local backend sees
 * "missing" demo data with no hint the two tabs hit different servers.
 */
export function signInServerHost(): string {
  if (!API_BASE) return "";
  try {
    return new URL(API_BASE).host;
  } catch {
    return API_BASE;
  }
}

/*
 * ── Sandbox PRD §1.3 — the realm this landing session is in ─────────────
 *
 * `archangelhealth.ai/?realm=sandbox` puts the session in the sandbox realm:
 * every request the landing makes then carries `X-Asclepius-Realm: sandbox`,
 * so sign-in, the physician wizard and the health-system signup all land in
 * the sandbox databases — the same code, a different file. The choice is
 * persisted in sessionStorage because the wizard is a multi-page flow and the
 * query string does not survive it. Without the param (or after `?realm=live`)
 * the landing is live, and the header is not sent at all.
 *
 * Every fetch in this module goes through `apiHeaders()`; a lint test in the
 * backend suite asserts no bare `headers: {` object remains here.
 */
export type Realm = "live" | "sandbox";
const REALM_STORAGE_KEY = "asclepius_realm";
export const REALM_HEADER = "X-Asclepius-Realm";

export function currentRealm(): Realm {
  if (typeof window === "undefined") return "live";
  try {
    const param = new URLSearchParams(window.location.search).get("realm");
    if (param === "sandbox") {
      window.sessionStorage.setItem(REALM_STORAGE_KEY, "sandbox");
      return "sandbox";
    }
    if (param === "live") {
      window.sessionStorage.removeItem(REALM_STORAGE_KEY);
      return "live";
    }
    return window.sessionStorage.getItem(REALM_STORAGE_KEY) === "sandbox" ? "sandbox" : "live";
  } catch {
    return "live";
  }
}

export function isSandbox(): boolean {
  return currentRealm() === "sandbox";
}

/** The headers every backend request carries: yours, plus the realm. */
export function apiHeaders(extra?: Record<string, string>): Record<string, string> {
  const h: Record<string, string> = { ...(extra ?? {}) };
  if (currentRealm() === "sandbox") h[REALM_HEADER] = "sandbox";
  return h;
}

/** `?realm=sandbox` re-attached to a same-site link so the realm survives it. */
export function withRealm(url: string): string {
  if (currentRealm() !== "sandbox") return url;
  return url + (url.includes("?") ? "&" : "?") + "realm=sandbox";
}

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

/** Asclepius (data-training) portal — email/password sign-in lives here. */
export function asclepiusPortalUrl(): string {
  const base = dashboardBaseUrl();
  // Sandbox PRD §1.3: a sandbox sign-in lands on the sandbox shell, whose
  // page JS keys its token as asclepius_token_sandbox.
  const path = currentRealm() === "sandbox" ? "/sandbox/asclepius" : "/asclepius";
  return base ? `${base}${path}` : path;
}

/**
 * Health-system upload portal. Sign-in and sign-up both live on this one page:
 * it is a JS state machine, not separate routes, so there is one URL to hand a
 * hospital and no way to send them to the wrong half of it.
 */
export function healthSystemPortalUrl(): string {
  const base = dashboardBaseUrl();
  const path = currentRealm() === "sandbox" ? "/sandbox/provider" : "/provider";
  return base ? `${base}${path}` : path;
}

/*
 * `storeAsclepiusSession` was here. It wrote localStorage["asclepius_token"]
 * and its docstring claimed "the wizard shares the same origin as /asclepius
 * so the value carries over". That is false in production and was the whole
 * of the "signing up makes me log in again" bug:
 *
 *   landing SPA   https://archangelhealth.ai        <- wrote the token here
 *   portal        https://app.archangelhealth.ai    <- read it from here
 *
 * localStorage is partitioned by origin, so the portal's boot() found nothing,
 * fell through trySsoLogin() and rendered the login screen. It failed for
 * EVERY signup in production and worked in local dev, where both are served
 * off :8000 — which is exactly why it survived review.
 *
 * The fix is not a better way to write a token across origins; there isn't
 * one. It is `redirectToAsclepiusPortal` below, which trades the token for a
 * single-use handoff code the portal redeems on load. That already existed and
 * SignInDialog and ResetPasswordPage were already using it correctly. Deleting
 * this function rather than leaving it unused is deliberate: it looks exactly
 * like the thing you want, and the next person will reach for it.
 */

export type User = {
  email: string;
  name?: string | null;
  role?: string | null;
  email_verified?: boolean;
};

export type AuthResponse = {
  access_token: string;
  token_type: string;
  user: User;
};

/** Returned by /api/auth/login when the account exists but hasn't verified
 * its email yet — no token is issued. */
export type VerificationRequiredResponse = {
  verification_required: true;
  email: string;
};

export type LoginResult = AuthResponse | VerificationRequiredResponse;

export function isVerificationRequired(r: LoginResult): r is VerificationRequiredResponse {
  return (r as VerificationRequiredResponse).verification_required === true;
}

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
 * Error for a fetch that threw (DNS failure, connection refused, or — most
 * common in production — a CORS preflight rejection when this site's origin is
 * missing from the backend's ALLOWED_ORIGINS). The old copy blamed a stopped
 * backend on port 8000, which is only the likely cause in local dev.
 */
function networkError(): Error {
  if (viteEnv.DEV) {
    return new Error("Cannot reach server. Make sure the backend is running (port 8000).");
  }
  const target = API_BASE || (typeof window !== "undefined" ? window.location.origin : "the backend");
  const origin = typeof window !== "undefined" ? window.location.origin : "this site's origin";
  return new Error(
    `Cannot reach the backend API at ${target}. The server may be restarting, or it is not ` +
      `accepting requests from ${origin} (CORS) — if this persists, verify the backend's ` +
      `ALLOWED_ORIGINS includes ${origin}.`
  );
}

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
    const res = await fetch(`${API_BASE}/api/demo/sign-in-routes`, { headers: apiHeaders() });
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
      headers: apiHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ email, password }),
    });
  } catch {
    throw networkError();
  }
  if (!res.ok) {
    throw new Error(await errorDetail(res, "Sign in failed"));
  }
  return res.json();
}

export async function createPortalHandoff(accessToken: string): Promise<PortalHandoffResponse> {
  const res = await fetch(`${API_BASE}/api/auth/portal-handoff`, {
    method: "POST",
    headers: apiHeaders({
      "Content-Type": "application/json",
      Authorization: `Bearer ${accessToken}`,
    }),
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

export type AsclepiusLoginResponse = {
  token: string;
  user: Record<string, unknown>;
};

/**
 * Sign in against Asclepius's own auth plane (own secret, own user table —
 * completely separate from the landing/tenant login above). Used by the
 * "Doctor" step so a physician who completed the Asclepius onboarding wizard
 * can come back to the landing page and sign into their real workspace.
 */
export async function asclepiusLogin(email: string, password: string): Promise<AsclepiusLoginResponse> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/api/asclepius/auth/login`, {
      method: "POST",
      headers: apiHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ email, password }),
    });
  } catch {
    throw networkError();
  }
  if (!res.ok) {
    throw new Error(await errorDetail(res, "Sign in failed"));
  }
  return res.json();
}

/**
 * Same cross-origin handoff pattern as createPortalHandoff/redirectToDoctorPortal
 * above, but against the Asclepius plane's own handoff endpoints — an Asclepius
 * token can't be traded through the landing/tenant handoff (different secret,
 * different decoder). Lands on /asclepius already signed in.
 */
export async function redirectToAsclepiusPortal(token: string): Promise<void> {
  const res = await fetch(`${API_BASE}/api/asclepius/auth/portal-handoff`, {
    method: "POST",
    headers: apiHeaders({ Authorization: `Bearer ${token}` }),
  });
  if (!res.ok) {
    throw new Error("Could not open Asclepius workspace.");
  }
  const handoff = (await res.json()) as PortalHandoffResponse;
  if (!handoff.handoff_code) throw new Error("Could not open Asclepius workspace.");
  window.location.href = `${asclepiusPortalUrl()}?asc_handoff=${encodeURIComponent(handoff.handoff_code)}`;
}

export async function login(email: string, password: string): Promise<LoginResult> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/api/auth/login`, {
      method: 'POST',
      headers: apiHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ email, password }),
    });
  } catch (e) {
    throw networkError();
  }
  if (!res.ok) {
    throw new Error(await errorDetail(res, 'Sign in failed'));
  }
  return res.json();
}

export async function register(
  email: string,
  password: string,
  name?: string,
  phone?: string,
  smsConsentOptIn?: boolean
): Promise<AuthResponse> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/api/auth/register`, {
      method: 'POST',
      headers: apiHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({
        email,
        password,
        name: name || undefined,
        phone: phone || undefined,
        sms_consent_opt_in: !!smsConsentOptIn,
      }),
    });
  } catch (e) {
    throw networkError();
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error((err as { detail?: string }).detail ?? 'Registration failed');
  }
  return res.json();
}

export async function verifyEmailOtp(email: string, code: string): Promise<void> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/api/auth/verify-email`, {
      method: "POST",
      headers: apiHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ email, code }),
    });
  } catch {
    throw networkError();
  }
  if (!res.ok) {
    throw new Error(await errorDetail(res, "Verification failed"));
  }
}

export async function verifyEmailToken(token: string): Promise<{ email: string }> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/api/auth/verify-email/by-token?${new URLSearchParams({ token })}`, { headers: apiHeaders() });
  } catch {
    throw networkError();
  }
  if (!res.ok) {
    throw new Error(await errorDetail(res, "This verification link is invalid or expired"));
  }
  return res.json();
}

export async function resendVerification(email: string): Promise<void> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/api/auth/verify-email/resend`, {
      method: "POST",
      headers: apiHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ email }),
    });
  } catch {
    throw networkError();
  }
  if (!res.ok) {
    throw new Error(await errorDetail(res, "Could not resend the verification email"));
  }
}

export async function getMe(token: string): Promise<User | null> {
  const res = await fetch(`${API_BASE}/api/auth/me`, {
    headers: apiHeaders({ Authorization: `Bearer ${token}` }),
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
    headers: apiHeaders({
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    }),
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
    headers: apiHeaders({ Authorization: `Bearer ${token}` }),
  });
  if (!res.ok) return null;
  return res.json();
}

export type LeadSource =
  | "request_data"
  | "provide_data"
  | "research_notify"
  | "health_system_partner";

/**
 * Submit a landing lead-capture form ("Request products" / "Provide data"). The
 * backend stores the row and emails the configured recipient. Throws an
 * actionable error on failure so the form can show its "or email us" fallback.
 */
/** What a referred health-system contact was already sent, for prefill. */
export type HsReferralPrefill = {
  found: boolean;
  contact_name?: string;
  contact_email?: string;
  contact_role?: string;
  hs_name?: string;
  referrer_first_name?: string;
};

/** Resolve a `?hs=` landing token.

    Never throws and never rejects: this only saves the visitor from retyping
    four fields, so a backend that is down, slow, or has forgotten the token
    must degrade to an ordinary empty form rather than an error page in front
    of somebody a physician just vouched for. */
export async function fetchHsReferralPrefill(token: string): Promise<HsReferralPrefill> {
  try {
    const res = await fetch(
      `${API_BASE}/api/asclepius/hs-referral/${encodeURIComponent(token)}`,
      { headers: apiHeaders() },
    );
    if (!res.ok) return { found: false };
    return (await res.json()) as HsReferralPrefill;
  } catch {
    return { found: false };
  }
}

export async function submitLead(payload: {
  source: LeadSource;
  email: string;
  message: string;
  /* The backend has always had this honeypot (routers/leads.py LeadBody) but no
     caller sent it, so the trap was armed and unbaited. Optional, because the
     two-field modals have no field to put it in. */
  company_website?: string;
  /* Ties this submission back to the physician who made the introduction, so
     their funnel row advances on its own. */
  referral_token?: string;
}): Promise<void> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/api/leads`, {
      method: "POST",
      headers: apiHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(payload),
    });
  } catch {
    throw networkError();
  }
  if (!res.ok) {
    throw new Error(await errorDetail(res, "Could not send"));
  }
}

/**
 * Mint a self-serve physician-contributor onboarding link — the same magic
 * link the admin "Generate Health System Link" button issues, created on
 * demand when a physician clicks "Become a contributor". The backend also
 * emails the link to `email` so the wizard can be resumed later. Throws an
 * actionable error (rate limit, cap reached) for the modal to surface.
 */
export async function createPhysicianOnboardingLink(payload: {
  email: string;
  company_website?: string;
  first_name?: string;
  last_name?: string;
  referral_code?: string;
  flavor?: string;
}): Promise<{ onboarding_url: string; expires_at?: string }> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/api/onboarding/self-serve`, {
      method: "POST",
      headers: apiHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(payload),
    });
  } catch {
    throw networkError();
  }
  if (!res.ok) {
    throw new Error(await errorDetail(res, "Could not create your onboarding link"));
  }
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
  const res = await fetch(`${API_BASE}/api/patient/by-codes?${params}`, { headers: apiHeaders() });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error((err as { detail?: string }).detail ?? "Invalid codes");
  }
  return res.json();
}

/* ─── Asclepius password recovery ────────────────────────────────────────────
   The forgot endpoint answers identically whether or not the address has an
   account, so there is nothing here to branch on and nothing to report back
   beyond "we have taken your request". Do not add an "unknown email" path:
   that would rebuild, in the client, the enumeration oracle the server was
   written to avoid. */

export async function asclepiusForgotPassword(email: string): Promise<{ message: string }> {
  const res = await fetch(`${API_BASE}/api/asclepius/auth/password/forgot`, {
    method: "POST",
    headers: apiHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ email }),
  });
  const body = await res.json().catch(() => ({}));
  return {
    message:
      (body && body.message) ||
      "If that email has an Asclepius account, we've sent a reset link.",
  };
}

export async function asclepiusResetPassword(
  token: string,
  newPassword: string,
): Promise<{ token: string }> {
  const res = await fetch(`${API_BASE}/api/asclepius/auth/password/reset`, {
    method: "POST",
    headers: apiHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ token, new_password: newPassword }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error((body && body.detail) || "This reset link is no longer valid.");
  }
  return { token: (body && body.token) || "" };
}

/* ─── Health-system self-serve signup (PRD §2) ──────────────────────────────
 *
 * Three fields and a code. No password field, deliberately: the backend mints a
 * temporary one, mails it, and forces its replacement at first sign-in. The
 * portal's own signup screen still offers a password field and posts to the
 * SAME endpoint — one door, one guard stack, one account — which is why there
 * is nothing here that the portal's version has to be kept in step with.
 *
 * `credentials: "include"` on the verify call is load-bearing. Verification
 * hands back a session as an HttpOnly cookie set on the API origin, and without
 * this the browser discards it and the organization lands on a login screen
 * holding a username it has never seen.
 */

export type HealthSystemSignupResult = {
  username: string;
  organization: string;
  mustReset: boolean;
};

async function readDetail(res: Response, fallback: string): Promise<string> {
  const body = await res.json().catch(() => null);
  const detail = body && typeof body.detail === "string" ? body.detail : "";
  return detail || fallback;
}

export async function healthSystemSignup(input: {
  fullName: string;
  email: string;
  organization: string;
  /** Honeypot. Never shown to a person; a bot fills it and the server drops the request. */
  companyWebsite?: string;
}): Promise<void> {
  const res = await fetch(`${API_BASE}/api/asclepius/hs/signup`, {
    method: "POST",
    headers: apiHeaders({ "Content-Type": "application/json" }),
    credentials: "include",
    body: JSON.stringify({
      full_name: input.fullName,
      email: input.email,
      organization: input.organization,
      company_website: input.companyWebsite || "",
    }),
  });
  if (!res.ok) {
    throw new Error(await readDetail(res, "We could not start that just now. Please try again."));
  }
}

export async function healthSystemResendCode(email: string): Promise<void> {
  await fetch(`${API_BASE}/api/asclepius/hs/signup/resend`, {
    method: "POST",
    headers: apiHeaders({ "Content-Type": "application/json" }),
    credentials: "include",
    body: JSON.stringify({ email }),
  }).catch(() => undefined);
}

export async function healthSystemVerify(
  email: string,
  code: string,
): Promise<HealthSystemSignupResult> {
  const res = await fetch(`${API_BASE}/api/asclepius/hs/signup/verify`, {
    method: "POST",
    headers: apiHeaders({ "Content-Type": "application/json" }),
    credentials: "include",
    body: JSON.stringify({ email, code }),
  });
  if (!res.ok) {
    throw new Error(await readDetail(res, "That code is not right, or it has expired."));
  }
  const body = await res.json().catch(() => ({}));
  return {
    username: String(body.username || ""),
    organization: String(body.organization || ""),
    mustReset: Boolean(body.must_reset),
  };
}
