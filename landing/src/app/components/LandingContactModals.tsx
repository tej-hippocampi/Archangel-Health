/**
 * Landing lead-capture modals for the v3 "console" landing — light theme,
 * portaled to <body> so they layer cleanly above the sticky nav.
 *
 *  - LeadFormModal: the two-field "Request data" / "Provide data" forms from
 *    the PRD (Landing_Request_and_Provide_Forms_PRD.md). One screen, two
 *    required fields, honeypot + inline validation, "or email us" fallback,
 *    and an in-place success state. Submissions post to the backend lead
 *    endpoint (source-tagged) which emails tejpatel@berkeley.edu.
 *  - ContributorChooser: "Become a contributor" → medical annotator (opens the
 *    sign-up / onboarding flow) or medical data contributor (opens Provide data).
 */

import * as React from "react";
import { createPortal } from "react-dom";
import * as authApi from "@/lib/auth-api";

const FALLBACK_EMAIL = "tejpatel@berkeley.edu";

export type LeadKind = "request_data" | "provide_data";

type LeadCopy = {
  heading: string;
  subhead: string;
  emailLabel: string;
  emailPlaceholder: string;
  messageLabel: string;
  messagePlaceholder: string;
  trust?: string;
  submit: string;
  success: string;
};

const COPY: Record<LeadKind, LeadCopy> = {
  request_data: {
    heading: "Request data",
    subhead: "Tell us what you're building and we'll send a scoped sample.",
    emailLabel: "Work email",
    emailPlaceholder: "you@company.com",
    messageLabel: "What are you building, and what do you need?",
    messagePlaceholder:
      "e.g., improving our medical model's reasoning on hard cases — targeting HealthBench Hard",
    submit: "Request data →",
    success: "Got it — we'll reply within 24h with a scoped sample.",
  },
  provide_data: {
    heading: "Provide de-identified data",
    subhead: "Tell us what you hold and we'll walk you through the rest.",
    emailLabel: "Work email",
    emailPlaceholder: "you@organization.com",
    messageLabel: "What clinical data do you have?",
    messagePlaceholder:
      "e.g., nephrology practice group with de-identified EMR data + outcomes across ~5k patients",
    trust:
      "We only work with de-identified data and will walk you through the process. Nothing is shared without an agreement in place.",
    submit: "Provide data →",
    success: "Thanks — we'll reach out within 24h to walk through your data and de-identification process.",
  },
};

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function useLockBodyScroll(active: boolean) {
  React.useEffect(() => {
    if (!active) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [active]);
}

export function LeadFormModal({
  kind,
  open,
  onClose,
}: {
  kind: LeadKind;
  open: boolean;
  onClose: () => void;
}) {
  const copy = COPY[kind];
  const [email, setEmail] = React.useState("");
  const [message, setMessage] = React.useState("");
  const [honeypot, setHoneypot] = React.useState("");
  const [emailErr, setEmailErr] = React.useState<string | null>(null);
  const [messageErr, setMessageErr] = React.useState<string | null>(null);
  const [formErr, setFormErr] = React.useState<string | null>(null);
  const [submitting, setSubmitting] = React.useState(false);
  const [done, setDone] = React.useState(false);
  const firstFieldRef = React.useRef<HTMLInputElement | null>(null);

  useLockBodyScroll(open);

  // Reset everything each time the modal (re)opens.
  React.useEffect(() => {
    if (open) {
      setEmail("");
      setMessage("");
      setHoneypot("");
      setEmailErr(null);
      setMessageErr(null);
      setFormErr(null);
      setSubmitting(false);
      setDone(false);
      const t = setTimeout(() => firstFieldRef.current?.focus(), 40);
      return () => clearTimeout(t);
    }
  }, [open, kind]);

  React.useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmedEmail = email.trim();
    const trimmedMessage = message.trim();
    let ok = true;
    if (!EMAIL_RE.test(trimmedEmail)) {
      setEmailErr("Enter a valid email address.");
      ok = false;
    } else {
      setEmailErr(null);
    }
    if (!trimmedMessage) {
      setMessageErr("This field is required.");
      ok = false;
    } else {
      setMessageErr(null);
    }
    if (!ok) return;

    // Honeypot: a filled hidden field means a bot — pretend success, send nothing.
    if (honeypot.trim()) {
      setDone(true);
      return;
    }

    setFormErr(null);
    setSubmitting(true);
    try {
      await authApi.submitLead({ source: kind, email: trimmedEmail, message: trimmedMessage });
      setDone(true);
    } catch (err) {
      setFormErr(err instanceof Error ? err.message : "Could not send. Please email us instead.");
    } finally {
      setSubmitting(false);
    }
  };

  const titleId = `lead-${kind}-title`;

  const modal = (
    <div className="arch-portal">
      <div
        className="am-overlay"
        onMouseDown={(e) => {
          if (e.target === e.currentTarget) onClose();
        }}
      >
        <div className="am-card" role="dialog" aria-modal="true" aria-labelledby={titleId}>
          <button type="button" className="am-close" onClick={onClose} aria-label="Close">
            ×
          </button>

          {done ? (
            <div className="am-success">
              <span className="am-check" aria-hidden="true">✓</span>
              <p className="am-success-text">{copy.success}</p>
              <button type="button" className="am-btn am-btn-ghost" onClick={onClose}>
                Close
              </button>
            </div>
          ) : (
            <>
              <h2 id={titleId} className="am-heading">{copy.heading}</h2>
              <p className="am-subhead">{copy.subhead}</p>

              {formErr && (
                <p className="am-form-error" role="alert">{formErr}</p>
              )}

              <form onSubmit={handleSubmit} noValidate>
                {/* honeypot — visually hidden, off the tab order */}
                <div className="am-hp" aria-hidden="true">
                  <label htmlFor={`am-hp-${kind}`}>Company website</label>
                  <input
                    id={`am-hp-${kind}`}
                    type="text"
                    tabIndex={-1}
                    autoComplete="off"
                    value={honeypot}
                    onChange={(e) => setHoneypot(e.target.value)}
                  />
                </div>

                <div className="am-field">
                  <label htmlFor={`am-email-${kind}`} className="am-label">{copy.emailLabel}</label>
                  <input
                    ref={firstFieldRef}
                    id={`am-email-${kind}`}
                    type="email"
                    inputMode="email"
                    autoComplete="email"
                    className="am-input"
                    placeholder={copy.emailPlaceholder}
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    aria-invalid={emailErr ? true : undefined}
                    aria-describedby={emailErr ? `am-email-err-${kind}` : undefined}
                    required
                  />
                  {emailErr && (
                    <p id={`am-email-err-${kind}`} className="am-field-error">{emailErr}</p>
                  )}
                </div>

                <div className="am-field">
                  <label htmlFor={`am-msg-${kind}`} className="am-label">{copy.messageLabel}</label>
                  <textarea
                    id={`am-msg-${kind}`}
                    className="am-textarea"
                    rows={4}
                    placeholder={copy.messagePlaceholder}
                    value={message}
                    onChange={(e) => setMessage(e.target.value)}
                    aria-invalid={messageErr ? true : undefined}
                    aria-describedby={messageErr ? `am-msg-err-${kind}` : undefined}
                    required
                  />
                  {copy.trust && <p className="am-trust">{copy.trust}</p>}
                  {messageErr && (
                    <p id={`am-msg-err-${kind}`} className="am-field-error">{messageErr}</p>
                  )}
                </div>

                <button type="submit" className="am-btn am-btn-primary" disabled={submitting}>
                  {submitting ? "Sending…" : copy.submit}
                </button>
                <p className="am-fallback">
                  or email us: <a href={`mailto:${FALLBACK_EMAIL}`}>{FALLBACK_EMAIL}</a>
                </p>
              </form>
            </>
          )}
        </div>
      </div>
      <style>{MODAL_STYLES}</style>
    </div>
  );

  return createPortal(modal, document.body);
}

export function ContributorChooser({
  open,
  onClose,
  onAnnotator,
  onDataContributor,
}: {
  open: boolean;
  onClose: () => void;
  onAnnotator: () => void;
  onDataContributor: () => void;
}) {
  useLockBodyScroll(open);

  React.useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const modal = (
    <div className="arch-portal">
      <div
        className="am-overlay"
        onMouseDown={(e) => {
          if (e.target === e.currentTarget) onClose();
        }}
      >
        <div className="am-card" role="dialog" aria-modal="true" aria-labelledby="contributor-title">
          <button type="button" className="am-close" onClick={onClose} aria-label="Close">
            ×
          </button>
          <h2 id="contributor-title" className="am-heading">Become a contributor</h2>
          <p className="am-subhead">Two ways to contribute to the frontier.</p>

          <div className="am-choices">
            <button type="button" className="am-choice" onClick={onAnnotator}>
              <span className="am-choice-dot am-dot-green" aria-hidden="true" />
              <span className="am-choice-title">Become a medical annotator</span>
              <span className="am-choice-sub">
                Reason through hard cases on our platform and get paid for your judgment.
              </span>
              <span className="am-choice-arrow" aria-hidden="true">→</span>
            </button>
            <button type="button" className="am-choice" onClick={onDataContributor}>
              <span className="am-choice-dot am-dot-pink" aria-hidden="true" />
              <span className="am-choice-title">Become a medical data contributor</span>
              <span className="am-choice-sub">
                Provide de-identified clinical data from your organization or software.
              </span>
              <span className="am-choice-arrow" aria-hidden="true">→</span>
            </button>
          </div>
        </div>
      </div>
      <style>{MODAL_STYLES}</style>
    </div>
  );

  return createPortal(modal, document.body);
}

/**
 * PhysicianOnboardModal — "Become a contributor" (annotator path). One email
 * field; on submit the backend mints a personal onboarding link (the same
 * magic link the admin console issues) and we redirect straight into the
 * wizard. The link is also emailed so the physician can resume any time.
 */
export function PhysicianOnboardModal({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const [email, setEmail] = React.useState("");
  const [honeypot, setHoneypot] = React.useState("");
  const [emailErr, setEmailErr] = React.useState<string | null>(null);
  const [formErr, setFormErr] = React.useState<string | null>(null);
  const [submitting, setSubmitting] = React.useState(false);
  const [redirecting, setRedirecting] = React.useState(false);
  const fieldRef = React.useRef<HTMLInputElement | null>(null);

  useLockBodyScroll(open);

  React.useEffect(() => {
    if (open) {
      setEmail("");
      setHoneypot("");
      setEmailErr(null);
      setFormErr(null);
      setSubmitting(false);
      setRedirecting(false);
      const t = setTimeout(() => fieldRef.current?.focus(), 40);
      return () => clearTimeout(t);
    }
  }, [open]);

  React.useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = email.trim();
    if (!EMAIL_RE.test(trimmed)) {
      setEmailErr("Enter a valid email address.");
      return;
    }
    setEmailErr(null);
    setFormErr(null);
    setSubmitting(true);
    try {
      const { onboarding_url } = await authApi.createPhysicianOnboardingLink({
        email: trimmed,
        company_website: honeypot,
      });
      setRedirecting(true);
      window.location.assign(onboarding_url);
    } catch (err) {
      setFormErr(err instanceof Error ? err.message : "Could not start onboarding. Please email us instead.");
      setSubmitting(false);
    }
  };

  const modal = (
    <div className="arch-portal">
      <div
        className="am-overlay"
        onMouseDown={(e) => {
          if (e.target === e.currentTarget && !redirecting) onClose();
        }}
      >
        <div className="am-card" role="dialog" aria-modal="true" aria-labelledby="phys-onboard-title">
          <button type="button" className="am-close" onClick={onClose} aria-label="Close">
            ×
          </button>

          {redirecting ? (
            <div className="am-success">
              <span className="am-check" aria-hidden="true">✓</span>
              <p className="am-success-text">Link created — taking you to onboarding…</p>
            </div>
          ) : (
            <>
              <h2 id="phys-onboard-title" className="am-heading">Start onboarding</h2>
              <p className="am-subhead">
                We'll create your personal onboarding link and take you straight there.
              </p>

              {formErr && <p className="am-form-error" role="alert">{formErr}</p>}

              <form onSubmit={handleSubmit} noValidate>
                {/* honeypot — visually hidden, off the tab order */}
                <div className="am-hp" aria-hidden="true">
                  <label htmlFor="am-hp-phys">Company website</label>
                  <input
                    id="am-hp-phys"
                    type="text"
                    tabIndex={-1}
                    autoComplete="off"
                    value={honeypot}
                    onChange={(e) => setHoneypot(e.target.value)}
                  />
                </div>

                <div className="am-field">
                  <label htmlFor="am-email-phys" className="am-label">Email</label>
                  <input
                    ref={fieldRef}
                    id="am-email-phys"
                    type="email"
                    inputMode="email"
                    autoComplete="email"
                    className="am-input"
                    placeholder="you@hospital.org"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    aria-invalid={emailErr ? true : undefined}
                    aria-describedby={emailErr ? "am-email-err-phys" : undefined}
                    required
                  />
                  {emailErr && (
                    <p id="am-email-err-phys" className="am-field-error">{emailErr}</p>
                  )}
                  <p className="am-trust">
                    Credentials are verified during onboarding. We'll also email you the
                    link so you can resume any time — it stays valid for 7 days.
                  </p>
                </div>

                <button type="submit" className="am-btn am-btn-primary" disabled={submitting}>
                  {submitting ? "Creating your link…" : "Begin onboarding →"}
                </button>
                <p className="am-fallback">
                  or email us: <a href={`mailto:${FALLBACK_EMAIL}`}>{FALLBACK_EMAIL}</a>
                </p>
              </form>
            </>
          )}
        </div>
      </div>
      <style>{MODAL_STYLES}</style>
    </div>
  );

  return createPortal(modal, document.body);
}

const MODAL_STYLES = `
.arch-portal {
  --canvas: #eef0ef;
  --card: #fbfcfa;
  --card-in: #f4f5f3;
  --hairline: rgba(26, 27, 26, 0.08);
  --ink: #1a1b1a;
  --ink-soft: #5c5e5a;
  --ink-faint: #8b8d89;
  --green: #4ca63c;
  --pink: #e8447b;
  --lime: #d5e14e;
  --sans: 'Instrument Sans', system-ui, -apple-system, sans-serif;
  --mono: 'IBM Plex Mono', ui-monospace, monospace;
}
.arch-portal, .arch-portal * { box-sizing: border-box; }

.arch-portal .am-overlay {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: grid;
  place-items: center;
  padding: 1.25rem;
  background: rgba(26, 27, 26, 0.42);
  backdrop-filter: blur(6px);
  -webkit-backdrop-filter: blur(6px);
  animation: am-fade 0.18s ease;
}

.arch-portal .am-card {
  position: relative;
  width: 100%;
  max-width: 31rem;
  max-height: 90vh;
  overflow-y: auto;
  background: var(--card);
  border: 1px solid var(--hairline);
  border-radius: 28px;
  padding: 2rem 1.9rem 1.7rem;
  box-shadow: 0 30px 80px -40px rgba(26, 27, 26, 0.45);
  font-family: var(--sans);
  color: var(--ink);
  animation: am-rise 0.22s cubic-bezier(0.2, 0.7, 0.2, 1);
}

.arch-portal .am-close {
  position: absolute;
  top: 1.1rem;
  right: 1.1rem;
  width: 32px;
  height: 32px;
  display: grid;
  place-items: center;
  border-radius: 50%;
  border: 1px solid var(--hairline);
  background: var(--card-in);
  color: var(--ink-soft);
  font-size: 1.2rem;
  line-height: 1;
  cursor: pointer;
  transition: background 0.2s ease, color 0.2s ease;
}
.arch-portal .am-close:hover { background: var(--card); color: var(--ink); }

.arch-portal .am-heading {
  font-family: var(--sans);
  font-weight: 500;
  font-size: 1.5rem;
  letter-spacing: -0.015em;
  line-height: 1.15;
  margin: 0 2.5rem 0.4rem 0;
}
.arch-portal .am-subhead {
  color: var(--ink-soft);
  font-size: 0.95rem;
  margin: 0 0 1.4rem;
}

.arch-portal .am-form-error {
  margin: 0 0 1rem;
  padding: 0.6em 0.9em;
  border-radius: 12px;
  background: rgba(232, 68, 123, 0.08);
  border: 1px solid rgba(232, 68, 123, 0.3);
  color: #b32a5b;
  font-size: 0.85rem;
}

.arch-portal .am-field { margin-bottom: 1.1rem; }

.arch-portal .am-label {
  display: block;
  font-size: 0.9rem;
  font-weight: 500;
  color: var(--ink);
  margin-bottom: 0.45rem;
}

.arch-portal .am-input,
.arch-portal .am-textarea {
  width: 100%;
  font-family: var(--sans);
  font-size: 0.98rem;
  color: var(--ink);
  background: var(--card-in);
  border: 1px solid var(--hairline);
  border-radius: 14px;
  padding: 0.75em 0.9em;
  transition: border-color 0.2s ease, box-shadow 0.2s ease;
}
.arch-portal .am-textarea { resize: vertical; min-height: 6.5rem; line-height: 1.55; }
.arch-portal .am-input::placeholder,
.arch-portal .am-textarea::placeholder { color: var(--ink-faint); }
.arch-portal .am-input:focus,
.arch-portal .am-textarea:focus {
  outline: none;
  border-color: rgba(26, 27, 26, 0.4);
  box-shadow: 0 0 0 3px rgba(76, 166, 60, 0.16);
}
.arch-portal .am-input[aria-invalid="true"],
.arch-portal .am-textarea[aria-invalid="true"] {
  border-color: rgba(232, 68, 123, 0.6);
  box-shadow: 0 0 0 3px rgba(232, 68, 123, 0.14);
}

.arch-portal .am-field-error { margin: 0.4rem 0 0; color: #b32a5b; font-size: 0.8rem; }

.arch-portal .am-trust {
  margin: 0.6rem 0 0;
  font-size: 0.78rem;
  line-height: 1.5;
  color: var(--ink-faint);
}

.arch-portal .am-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 0.5em;
  width: 100%;
  margin-top: 0.4rem;
  padding: 0.85em 1.4em;
  border-radius: 999px;
  font-family: var(--sans);
  font-size: 0.98rem;
  font-weight: 600;
  cursor: pointer;
  border: 1px solid transparent;
  transition: transform 0.18s ease, box-shadow 0.18s ease, background 0.18s ease;
}
.arch-portal .am-btn-primary { background: var(--lime); color: var(--ink); }
.arch-portal .am-btn-primary:hover { transform: translateY(-1px); box-shadow: 0 12px 30px -16px rgba(26, 27, 26, 0.5); }
.arch-portal .am-btn-primary:disabled { opacity: 0.65; cursor: default; transform: none; box-shadow: none; }
.arch-portal .am-btn-ghost { background: var(--card-in); color: var(--ink); border-color: var(--hairline); }
.arch-portal .am-btn-ghost:hover { background: var(--card); }

.arch-portal .am-fallback {
  margin: 0.9rem 0 0;
  text-align: center;
  font-size: 0.82rem;
  color: var(--ink-faint);
}
.arch-portal .am-fallback a { color: var(--ink-soft); text-decoration: underline; text-underline-offset: 3px; }

/* visually-hidden honeypot */
.arch-portal .am-hp {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
}

/* success */
.arch-portal .am-success { text-align: center; padding: 1.4rem 0.5rem 0.6rem; }
.arch-portal .am-check {
  display: grid;
  place-items: center;
  width: 46px;
  height: 46px;
  margin: 0 auto 1rem;
  border-radius: 50%;
  background: var(--lime);
  color: var(--ink);
  font-size: 1.3rem;
}
.arch-portal .am-success-text {
  font-size: 1.02rem;
  line-height: 1.5;
  color: var(--ink);
  margin: 0 auto 1.4rem;
  max-width: 22rem;
}

/* contributor chooser */
.arch-portal .am-choices { display: grid; gap: 0.8rem; }
.arch-portal .am-choice {
  position: relative;
  display: grid;
  gap: 0.45rem;
  text-align: left;
  background: var(--card-in);
  border: 1px solid var(--hairline);
  border-radius: 20px;
  padding: 1.3rem 3rem 1.4rem 1.3rem;
  cursor: pointer;
  font-family: var(--sans);
  transition: transform 0.18s ease, box-shadow 0.18s ease, background 0.18s ease;
}
.arch-portal .am-choice:hover { transform: translateY(-2px); background: var(--card); box-shadow: 0 18px 44px -30px rgba(26, 27, 26, 0.5); }
.arch-portal .am-choice-dot { width: 8px; height: 8px; border-radius: 50%; }
.arch-portal .am-dot-green { background: var(--green); }
.arch-portal .am-dot-pink { background: var(--pink); }
.arch-portal .am-choice-title { font-size: 1.08rem; font-weight: 500; color: var(--ink); letter-spacing: -0.01em; }
.arch-portal .am-choice-sub { font-size: 0.86rem; color: var(--ink-soft); line-height: 1.5; max-width: 24rem; }
.arch-portal .am-choice-arrow {
  position: absolute;
  right: 1.2rem;
  top: 50%;
  transform: translateY(-50%);
  width: 30px;
  height: 30px;
  border-radius: 50%;
  display: grid;
  place-items: center;
  background: var(--card);
  border: 1px solid var(--hairline);
  color: var(--ink);
  transition: background 0.18s ease;
}
.arch-portal .am-choice:hover .am-choice-arrow { background: var(--lime); border-color: transparent; }

@keyframes am-fade { from { opacity: 0; } to { opacity: 1; } }
@keyframes am-rise { from { opacity: 0; transform: translateY(12px); } to { opacity: 1; transform: none; } }

@media (prefers-reduced-motion: reduce) {
  .arch-portal .am-overlay, .arch-portal .am-card { animation: none; }
  .arch-portal .am-btn:hover, .arch-portal .am-choice:hover { transform: none; }
}

@media (max-width: 520px) {
  .arch-portal .am-card { padding: 1.6rem 1.3rem 1.4rem; border-radius: 22px; }
  .arch-portal .am-heading { font-size: 1.3rem; }
}
`;
