import { useState, type FormEvent } from "react";

type Props = { slug: string };

const DASHBOARD =
  (import.meta as unknown as { env: { VITE_DASHBOARD_URL?: string; VITE_API_URL?: string; DEV?: boolean } }).env
    .VITE_DASHBOARD_URL ??
  (import.meta as unknown as { env: { VITE_API_URL?: string } }).env.VITE_API_URL ??
  "http://localhost:8000";

export default function TenantSignIn({ slug }: Props) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    const r = await fetch(`/api/tenant/${encodeURIComponent(slug)}/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      setError(data.detail || "Sign in failed");
      return;
    }
    const tok = data.access_token as string;
    window.location.href = `${DASHBOARD.replace(/\/$/, "")}/#auth=${encodeURIComponent(tok)}`;
  }

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex items-center justify-center p-6">
      <form onSubmit={onSubmit} className="w-full max-w-md space-y-4 bg-slate-900/80 border border-slate-800 rounded-xl p-8">
        <h1 className="text-xl font-semibold text-center">Health system sign in</h1>
        <p className="text-slate-400 text-sm text-center">Workspace: {slug}</p>
        {error ? <p className="text-red-400 text-sm text-center">{error}</p> : null}
        <input
          className="w-full rounded-lg bg-slate-800 border border-slate-700 px-3 py-2"
          type="email"
          placeholder="Email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          autoComplete="username"
        />
        <input
          className="w-full rounded-lg bg-slate-800 border border-slate-700 px-3 py-2"
          type="password"
          placeholder="Password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="current-password"
        />
        <button type="submit" className="w-full py-2 rounded-lg bg-teal-600 hover:bg-teal-500 font-medium">
          Sign in
        </button>
      </form>
    </div>
  );
}
