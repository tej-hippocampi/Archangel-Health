/**
 * Archangel Health — console-system styles for the sign-in / sign-up dialogs.
 *
 * The dialogs portal to document.body, outside the .arch-landing scope, so
 * the palette is re-declared here from the same single source
 * (arch/baseStyles.consolePalette). Product-scale geometry (§2.2): dialogs
 * are a tool surface, not a marketing surface.
 */

import "@/styles/clinical-fonts.css";
import { consolePalette } from "./arch/baseStyles";

export const authDialogStyles = `
.adg-scrim {
  ${consolePalette}
  --r-chip: 999px;
  --r-sm: 10px;
  --r-md: 14px;
  --r-lg: 20px;

  position: fixed;
  inset: 0;
  z-index: 9999;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
  background: var(--scrim);
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  font-family: var(--sans);
  color: var(--ink);
  line-height: 1.55;
}

/* Full-page variant (tenant sign-in): same tokens, opaque canvas. */
.adg-page {
  background: var(--canvas);
  backdrop-filter: none;
  -webkit-backdrop-filter: none;
  position: static;
  min-height: 100vh;
}

.adg-scrim *, .adg-scrim *::before, .adg-scrim *::after { box-sizing: border-box; margin: 0; padding: 0; }

.adg-scrim :focus-visible {
  outline: 2px solid var(--ink);
  outline-offset: 2px;
  border-radius: 4px;
}

.adg-panel {
  width: 100%;
  max-width: 26.5rem;
  max-height: 90vh;
  display: flex;
  flex-direction: column;
  background: var(--card);
  border: 1px solid var(--hairline);
  border-radius: var(--r-lg);
  box-shadow: var(--shadow-float);
  overflow: hidden;
}

.adg-body {
  padding: 24px;
  overflow-y: auto;
  min-height: 0;
}

.adg-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 20px;
}

.adg-title {
  font-size: 1.375rem;
  font-weight: 400;
  letter-spacing: -0.015em;
  line-height: 1.25;
  color: var(--ink);
}

.adg-sub {
  margin-top: 5px;
  font-size: 0.875rem;
  color: var(--ink-soft);
}

.adg-chrome {
  font-family: var(--mono);
  font-size: 0.6875rem;
  font-weight: 400;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--ink-faint);
}

.adg-server { margin-top: 8px; }

.adg-close {
  flex: none;
  width: 32px;
  height: 32px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--hairline-strong);
  border-radius: var(--r-chip);
  background: transparent;
  color: var(--ink-soft);
  font-size: 1rem;
  line-height: 1;
  cursor: pointer;
  transition: background 0.16s cubic-bezier(.4,0,.2,1), color 0.16s cubic-bezier(.4,0,.2,1);
}
.adg-close:hover { background: var(--card-in); color: var(--ink); }

/* Error banner — meaning is carried by the alert role, the label and the dot,
   not by color alone; message text stays ink for AA contrast. */
.adg-error {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 16px;
  padding: 10px 14px;
  background: var(--card-in);
  border: 1px solid rgba(232, 68, 123, 0.35);
  border-radius: var(--r-sm);
  font-size: 0.875rem;
  color: var(--ink);
}
.adg-error .adg-dot { transform: translateY(-1px); }

/* Role chooser cards — dot + chrome micro-label + title + sub-line. */
.adg-roles {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
@media (max-width: 420px) { .adg-roles { grid-template-columns: 1fr; } }

.adg-role {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 7px;
  padding: 18px 16px 16px;
  background: var(--card);
  border: 1px solid var(--hairline-strong);
  border-radius: var(--r-md);
  text-align: left;
  cursor: pointer;
  transition: background 0.16s cubic-bezier(.4,0,.2,1), border-color 0.16s cubic-bezier(.4,0,.2,1);
}
.adg-role:hover { background: var(--card-in); border-color: rgba(26, 27, 26, 0.3); }

.adg-role-for {
  display: inline-flex;
  align-items: center;
  gap: 7px;
}

.adg-role-title {
  font-size: 1.0625rem;
  font-weight: 500;
  color: var(--ink);
}

.adg-role-sub {
  font-size: 0.8125rem;
  color: var(--ink-soft);
}

.adg-dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex: none;
}
.adg-dot-green { background: var(--green); }
.adg-dot-pink { background: var(--pink); }
.adg-dot-faint { background: rgba(26, 27, 26, 0.18); }

/* Forms */
.adg-form { display: grid; gap: 14px; }

.adg-field { display: grid; gap: 6px; }

.adg-label {
  font-size: 0.8125rem;
  font-weight: 500;
  color: var(--ink-soft);
}

.adg-input {
  width: 100%;
  font-family: var(--sans);
  font-size: 0.9375rem;
  color: var(--ink);
  background: var(--card-in);
  border: 1px solid var(--hairline);
  border-radius: var(--r-sm);
  padding: 10px 12px;
  transition: border-color 0.16s cubic-bezier(.4,0,.2,1);
}
.adg-input::placeholder { color: var(--ink-faint); }
.adg-input:hover { border-color: var(--hairline-strong); }
.adg-input:focus-visible { outline: 2px solid var(--ink); outline-offset: 2px; }
.adg-input[readonly] { color: var(--ink-soft); }

.adg-input-code {
  font-family: var(--mono);
  letter-spacing: 0.08em;
}

/* Buttons */
.adg-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 10px;
}

.adg-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 0.5em;
  padding: 0.62em 1.4em;
  border-radius: var(--r-chip);
  font-family: var(--sans);
  font-size: 0.9375rem;
  font-weight: 500;
  cursor: pointer;
  transition: background 0.16s cubic-bezier(.4,0,.2,1), border-color 0.16s cubic-bezier(.4,0,.2,1);
  white-space: nowrap;
}
.adg-btn:disabled { opacity: 0.55; cursor: not-allowed; }

.adg-btn-primary {
  background: var(--ink);
  border: 1px solid var(--ink);
  color: var(--card);
}
.adg-btn-primary:hover:not(:disabled) { background: var(--ink-hover); }

.adg-btn-secondary {
  background: transparent;
  border: 1px solid var(--hairline-strong);
  color: var(--ink);
}
.adg-btn-secondary:hover:not(:disabled) { background: var(--card-in); }

@media (prefers-reduced-motion: reduce) {
  .adg-scrim * { transition-duration: 0.01ms !important; }
}
`;
