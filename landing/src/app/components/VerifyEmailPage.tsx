"use client";

import * as React from "react";
import * as authApi from "@/lib/auth-api";
import { authDialogStyles } from "./authDialogStyles";

type Props = { token: string };

type Status = "verifying" | "success" | "error";

export default function VerifyEmailPage({ token }: Props) {
  const [status, setStatus] = React.useState<Status>("verifying");
  const [message, setMessage] = React.useState<string>("Verifying your email…");

  React.useEffect(() => {
    let cancelled = false;
    authApi
      .verifyEmailToken(token)
      .then(({ email }) => {
        if (cancelled) return;
        setStatus("success");
        setMessage(`${email} is verified. You can close this tab and continue signing in.`);
      })
      .catch((e) => {
        if (cancelled) return;
        setStatus("error");
        setMessage(e instanceof Error ? e.message : "This verification link is invalid or expired.");
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  return (
    <div className="adg-scrim adg-page">
      <style>{authDialogStyles}</style>
      <div className="adg-panel">
        <div className="adg-body">
          <div className="adg-head">
            <div>
              <h2 className="adg-title">
                {status === "verifying" && "Verifying your email…"}
                {status === "success" && "Email verified"}
                {status === "error" && "Verification failed"}
              </h2>
              <p className="adg-sub">{message}</p>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
