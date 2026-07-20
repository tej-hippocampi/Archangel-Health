/**
 * `/research` — coming soon (PRD §4). Three one-line cards + email capture
 * posting to /api/leads with source "research_notify". Near-empty by design.
 */

import { useState } from "react";
import * as authApi from "@/lib/auth-api";
import type { ShellActions } from "../ArchShell";

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

const CARDS = [
  { tag: "Evaluation", title: "The failure surface.", line: "How top models fail hard cases, verified by physicians." },
  { tag: "Method", title: "Rubrics as reward.", line: "Clinical rubrics that are discriminative, stable, and hack-resistant." },
  { tag: "Benchmark", title: "Reasoning under clinical conditions.", line: "Sequential, longitudinal, multi-stakeholder — not recall." },
];

export function ResearchPage(_props: { actions: ShellActions }) {
  const [email, setEmail] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = email.trim();
    if (!EMAIL_RE.test(trimmed)) {
      setErr("Enter a valid email address.");
      return;
    }
    setErr(null);
    setBusy(true);
    try {
      await authApi.submitLead({
        source: "research_notify",
        email: trimmed,
        message: "Notify me when Archangel Health publishes research.",
      });
      setDone(true);
    } catch (e2) {
      setErr(e2 instanceof Error ? e2.message : "Could not subscribe just now.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="route">
      <section className="section">
        <p className="crumb chrome reveal"><span className="root">Archangel</span><span className="sep">/</span><span className="here">01 · Research</span></p>
        <div className="reveal">
          <h2>Research <span className="chip" style={{ verticalAlign: "middle", marginLeft: "0.6rem" }}>Coming soon</span></h2>
          <p className="lede">Publishing on how frontier models fail clinical reasoning — and how to measure it.</p>
        </div>

        <div className="r-cards">
          {CARDS.map((c) => (
            <div className="derive reveal" key={c.tag}>
              <span className="chrome chrome-box"><span className="dot dot-faint" />{c.tag}</span>
              <h3>{c.title}</h3>
              <p>{c.line}</p>
            </div>
          ))}
        </div>

        {done ? (
          <p className="notify-done reveal in"><span className="dot dot-green" />You're on the list.</p>
        ) : (
          <form className="notify-form reveal" onSubmit={submit} noValidate>
            <input
              type="email"
              inputMode="email"
              autoComplete="email"
              className="notify-input"
              placeholder="you@lab.com"
              aria-label="Email address"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
            <button type="submit" className="btn btn-primary" disabled={busy}>
              {busy ? "Sending…" : "Get notified"}
            </button>
          </form>
        )}
        {err && <p className="cta-note" role="alert" style={{ marginTop: "0.6rem" }}>{err}</p>}
      </section>
    </div>
  );
}
