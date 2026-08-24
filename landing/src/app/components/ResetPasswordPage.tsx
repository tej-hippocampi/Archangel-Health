"use client";

import * as React from "react";
import * as authApi from "@/lib/auth-api";
import { authDialogStyles } from "./authDialogStyles";

type Props = { token: string };

const MIN = 12;

/**
 * The page a reset link lands on.
 *
 * On success it signs the physician straight into the portal rather than
 * returning them to a sign-in form. They proved mailbox control and typed the
 * password eight seconds ago; asking them to type it again is how a recovery
 * flow gets abandoned at the last step.
 */
export default function ResetPasswordPage({ token }: Props) {
  const [pw, setPw] = React.useState("");
  const [confirm, setConfirm] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");

  const longEnough = pw.length >= MIN;
  const varied = new Set(pw).size >= 5;
  const matches = !!pw && pw === confirm;
  const valid = longEnough && varied && matches && !busy;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!valid) return;
    setBusy(true);
    setError("");
    try {
      const { token: session } = await authApi.asclepiusResetPassword(token, pw);
      if (session) {
        await authApi.redirectToAsclepiusPortal(session);
        return;
      }
      window.location.href = authApi.asclepiusPortalUrl();
    } catch (err) {
      setError(err instanceof Error ? err.message : "This reset link is no longer valid.");
      setBusy(false);
    }
  };

  const check = (ok: boolean, label: string) => (
    <li style={{ display: "flex", gap: 8, margin: "4px 0", fontSize: "0.85rem", color: ok ? "var(--green)" : "var(--ink-faint)" }}>
      <span aria-hidden="true" style={{ width: 12 }}>{ok ? "✓" : "·"}</span>
      {label}
    </li>
  );

  return (
    <div className="adg-scrim adg-page">
      <style>{authDialogStyles}</style>
      <div className="adg-panel">
        <div className="adg-body">
          <div className="adg-head">
            <div>
              <h2 className="adg-title">Choose a new password</h2>
              <p className="adg-sub">
                This link works once. Once you set a password here, every session
                already signed in to this account is ended.
              </p>
            </div>
          </div>

          <form onSubmit={submit}>
            {error ? <div className="adg-error">{error}</div> : null}

            <label className="adg-label" htmlFor="rp-new">New password</label>
            <input
              id="rp-new"
              className="adg-input"
              type="password"
              autoComplete="new-password"
              value={pw}
              onChange={(e) => setPw(e.target.value)}
              placeholder={`At least ${MIN} characters`}
            />

            <label className="adg-label" htmlFor="rp-confirm">Confirm password</label>
            <input
              id="rp-confirm"
              className="adg-input"
              type="password"
              autoComplete="new-password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              placeholder="Type it again"
            />

            <ul style={{ listStyle: "none", padding: 0, margin: "12px 0 0" }}>
              {check(longEnough, `${MIN} characters or more`)}
              {check(varied, "A mix of characters, not one repeated")}
              {check(matches, "Both entries match")}
            </ul>

            <button className="adg-btn adg-btn-primary" type="submit" disabled={!valid} style={{ marginTop: 18 }}>
              {busy ? "Setting your password…" : "Set password and sign in"}
            </button>
          </form>
        </div>
      </div>
    </div>
  );
}
