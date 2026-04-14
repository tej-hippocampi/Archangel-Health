import { useCallback, useEffect, useState } from "react";

import { API_BASE } from "@/lib/auth-api";

type Props = { token: string };

const api = (path: string, init?: RequestInit) =>
  fetch(`${API_BASE}${path}`, { ...init, headers: { "Content-Type": "application/json", ...(init?.headers || {}) } });

/** FastAPI may return `detail` as a string (HTTPException) or an array (422 validation). */
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
  const [step, setStep] = useState(0);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");
  const [email, setEmail] = useState("");
  const [otp, setOtp] = useState("");
  const [hsName, setHsName] = useState("");
  const [dept, setDept] = useState("");
  const [phone, setPhone] = useState("");
  const [memberName, setMemberName] = useState("");
  const [memberEmail, setMemberEmail] = useState("");
  const [memberRole, setMemberRole] = useState<"doctor" | "nurse">("doctor");

  const loadSession = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const r = await api(`/api/onboarding/session?token=${encodeURIComponent(token)}`);
      const data = await readResponseJson(r);
      if (!r.ok) {
        setError(formatApiError(data) || `HTTP ${r.status}`);
        return;
      }
      const d = data as { status?: string; sign_in_url?: string; step?: number };
      if (d.status === "complete" && d.sign_in_url) {
        window.location.replace(d.sign_in_url);
        return;
      }
      setStep(Math.max(1, Number(d.step) || 1));
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Could not load onboarding");
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void loadSession();
  }, [loadSession]);

  async function submitStep1() {
    setError("");
    try {
      const r = await api("/api/onboarding/step1-identity", {
        method: "POST",
        body: JSON.stringify({ token, first_name: firstName, last_name: lastName, email }),
      });
      const data = await readResponseJson(r);
      if (!r.ok) {
        setError(formatApiError(data) || `HTTP ${r.status}`);
        return;
      }
      setStep(2);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Network error");
    }
  }

  async function requestOtp() {
    setError("");
    try {
      const r = await api("/api/onboarding/request-otp", { method: "POST", body: JSON.stringify({ token }) });
      const data = await readResponseJson(r);
      if (!r.ok) setError(formatApiError(data) || `HTTP ${r.status}`);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Network error");
    }
  }

  async function verifyOtp() {
    setError("");
    if (otp.length !== 6) {
      setError("Enter the full 6-digit code.");
      return;
    }
    try {
      const r = await api("/api/onboarding/verify-otp", {
        method: "POST",
        body: JSON.stringify({ token, code: otp }),
      });
      const data = await readResponseJson(r);
      if (!r.ok) {
        setError(formatApiError(data) || "Invalid code");
        return;
      }
      setStep(3);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Network error");
    }
  }

  async function submitOrg() {
    setError("");
    try {
      const r = await api("/api/onboarding/step3-organization", {
        method: "POST",
        body: JSON.stringify({
          token,
          health_system_name: hsName,
          surgery_department: dept,
          phone,
        }),
      });
      const data = await readResponseJson(r);
      if (!r.ok) {
        setError(formatApiError(data) || `HTTP ${r.status}`);
        return;
      }
      setStep(4);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Network error");
    }
  }

  async function addMember() {
    setError("");
    try {
      const r = await api("/api/onboarding/add-team-member", {
        method: "POST",
        body: JSON.stringify({
          token,
          full_name: memberName,
          email: memberEmail,
          role: memberRole,
        }),
      });
      const data = await readResponseJson(r);
      if (!r.ok) {
        setError(formatApiError(data) || `HTTP ${r.status}`);
        return;
      }
      setMemberName("");
      setMemberEmail("");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Network error");
    }
  }

  async function finish() {
    setError("");
    try {
      const r = await api("/api/onboarding/finish", { method: "POST", body: JSON.stringify({ token }) });
      const data = await readResponseJson(r);
      if (!r.ok) {
        setError(formatApiError(data) || `HTTP ${r.status}`);
        return;
      }
      const d = data as { sign_in_url?: string };
      if (d.sign_in_url) window.location.replace(d.sign_in_url);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Network error");
    }
  }

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-slate-950 text-slate-200 p-6">
        <p>Loading onboarding…</p>
      </div>
    );
  }

  const canVerifyOtp = otp.length === 6;

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 p-6 flex flex-col items-center">
      <div className="w-full max-w-lg space-y-6">
        <h1 className="text-2xl font-semibold text-center">Health system onboarding</h1>
        {error ? <p className="text-red-400 text-sm text-center">{error}</p> : null}

        {step === 1 ? (
          <div className="space-y-3 bg-slate-900/80 border border-slate-800 rounded-xl p-6">
            <p className="text-slate-400 text-sm">Step 1 — Your name and email</p>
            <input
              className="w-full rounded-lg bg-slate-800 border border-slate-700 px-3 py-2"
              placeholder="First name"
              value={firstName}
              onChange={(e) => setFirstName(e.target.value)}
            />
            <input
              className="w-full rounded-lg bg-slate-800 border border-slate-700 px-3 py-2"
              placeholder="Last name"
              value={lastName}
              onChange={(e) => setLastName(e.target.value)}
            />
            <input
              className="w-full rounded-lg bg-slate-800 border border-slate-700 px-3 py-2"
              placeholder="Email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
            <button
              type="button"
              className="w-full py-2 rounded-lg bg-teal-600 hover:bg-teal-500 font-medium"
              onClick={() => void submitStep1()}
            >
              Continue
            </button>
          </div>
        ) : null}

        {step === 2 ? (
          <div className="space-y-3 bg-slate-900/80 border border-slate-800 rounded-xl p-6">
            <p className="text-slate-400 text-sm">Step 2 — Email verification</p>
            <button
              type="button"
              className="w-full py-2 rounded-lg bg-slate-700 hover:bg-slate-600 text-sm"
              onClick={() => void requestOtp()}
            >
              Send 6-digit code to my email
            </button>
            <input
              className="w-full rounded-lg bg-slate-800 border border-slate-700 px-3 py-2 tracking-widest"
              placeholder="6-digit code"
              value={otp}
              onChange={(e) => setOtp(e.target.value.replace(/\D/g, "").slice(0, 6))}
            />
            <p className="text-xs text-slate-500">
              {canVerifyOtp ? "Ready to verify." : "Enter all 6 digits, then tap Verify."}
            </p>
            <button
              type="button"
              disabled={!canVerifyOtp}
              className="w-full py-2 rounded-lg bg-teal-600 hover:bg-teal-500 font-medium disabled:opacity-40 disabled:cursor-not-allowed"
              onClick={() => void verifyOtp()}
            >
              Verify code
            </button>
          </div>
        ) : null}

        {step === 3 ? (
          <div className="space-y-3 bg-slate-900/80 border border-slate-800 rounded-xl p-6">
            <p className="text-slate-400 text-sm">Step 3 — Health system details</p>
            <input
              className="w-full rounded-lg bg-slate-800 border border-slate-700 px-3 py-2"
              placeholder="Health system name"
              value={hsName}
              onChange={(e) => setHsName(e.target.value)}
            />
            <input
              className="w-full rounded-lg bg-slate-800 border border-slate-700 px-3 py-2"
              placeholder="Surgery department (e.g. Orthopedic Surgery)"
              value={dept}
              onChange={(e) => setDept(e.target.value)}
            />
            <input
              className="w-full rounded-lg bg-slate-800 border border-slate-700 px-3 py-2"
              placeholder="Health system phone"
              value={phone}
              onChange={(e) => setPhone(e.target.value)}
            />
            <p className="text-xs text-slate-500">Your role: Director of TEAM Initiative (assigned automatically)</p>
            <button
              type="button"
              className="w-full py-2 rounded-lg bg-teal-600 hover:bg-teal-500 font-medium"
              onClick={() => void submitOrg()}
            >
              Continue
            </button>
          </div>
        ) : null}

        {step === 4 ? (
          <div className="space-y-3 bg-slate-900/80 border border-slate-800 rounded-xl p-6">
            <p className="text-slate-400 text-sm">Step 4 — Add team members (Doctor or Nurse)</p>
            <input
              className="w-full rounded-lg bg-slate-800 border border-slate-700 px-3 py-2"
              placeholder="Full name"
              value={memberName}
              onChange={(e) => setMemberName(e.target.value)}
            />
            <input
              className="w-full rounded-lg bg-slate-800 border border-slate-700 px-3 py-2"
              placeholder="Email"
              type="email"
              value={memberEmail}
              onChange={(e) => setMemberEmail(e.target.value)}
            />
            <select
              className="w-full rounded-lg bg-slate-800 border border-slate-700 px-3 py-2"
              value={memberRole}
              onChange={(e) => setMemberRole(e.target.value as "doctor" | "nurse")}
            >
              <option value="doctor">Doctor</option>
              <option value="nurse">Nurse</option>
            </select>
            <button
              type="button"
              className="w-full py-2 rounded-lg bg-slate-700 hover:bg-slate-600 text-sm"
              onClick={() => void addMember()}
            >
              Add member and send invite email
            </button>
            <button
              type="button"
              className="w-full py-2 rounded-lg bg-teal-600 hover:bg-teal-500 font-medium"
              onClick={() => void finish()}
            >
              Finish onboarding
            </button>
          </div>
        ) : null}
      </div>
    </div>
  );
}
