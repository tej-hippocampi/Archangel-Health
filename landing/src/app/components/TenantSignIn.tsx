import { useState, type FormEvent } from "react";

import { API_BASE } from "@/lib/auth-api";
import * as authApi from "@/lib/auth-api";
import { authDialogStyles } from "./authDialogStyles";

type Props = { slug: string };

export default function TenantSignIn({ slug }: Props) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    const r = await fetch(`${API_BASE}/api/tenant/${encodeURIComponent(slug)}/auth/login`, {
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
    await authApi.redirectToDoctorPortal(tok);
  }

  return (
    <div className="adg-scrim adg-page">
      <style>{authDialogStyles}</style>
      <div className="adg-panel">
        <div className="adg-body">
          <div className="adg-head">
            <div>
              <h1 className="adg-title">Health system sign in</h1>
              <p className="adg-sub">Workspace · {slug}</p>
              {authApi.signInServerHost() ? (
                <p className="adg-chrome adg-server">Server · {authApi.signInServerHost()}</p>
              ) : null}
            </div>
          </div>

          {error ? (
            <p className="adg-error" role="alert">
              <span className="adg-dot adg-dot-pink" aria-hidden="true" />
              <span>{error}</span>
            </p>
          ) : null}

          <form onSubmit={onSubmit} className="adg-form">
            <div className="adg-field">
              <label className="adg-label" htmlFor="tenant-email">Email</label>
              <input
                id="tenant-email"
                className="adg-input"
                type="email"
                placeholder="you@example.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                autoComplete="username"
              />
            </div>
            <div className="adg-field">
              <label className="adg-label" htmlFor="tenant-password">Password</label>
              <input
                id="tenant-password"
                className="adg-input"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
              />
            </div>
            <div className="adg-actions">
              <button type="submit" className="adg-btn adg-btn-primary">
                Sign in
              </button>
            </div>
          </form>
        </div>
      </div>
    </div>
  );
}
