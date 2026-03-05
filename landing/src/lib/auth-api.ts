/**
 * Archangel Health landing — auth API client.
 * Uses relative /api in dev (Vite proxy to backend) or VITE_API_URL when set.
 */

const API_BASE = (import.meta as unknown as { env: { VITE_API_URL?: string } }).env?.VITE_API_URL ?? '';

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
};

export type PatientByCodesResponse = {
  patient_id: string;
  dashboard_url: string;
};

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
  clinicCode: string,
  resourceCode: string
): Promise<PatientByCodesResponse> {
  const params = new URLSearchParams({
    clinic_code: clinicCode.trim(),
    resource_code: resourceCode.trim(),
  });
  const res = await fetch(`${API_BASE}/api/patient/by-codes?${params}`);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error((err as { detail?: string }).detail ?? "Invalid codes");
  }
  return res.json();
}
