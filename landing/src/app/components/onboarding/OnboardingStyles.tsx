/**
 * Scoped global styles + keyframes for the Archangel Health onboarding flow.
 *
 * Loaded once by `OnboardingWizard`. Keeps the rest of the landing app
 * untouched — these styles only apply when the onboarding shell is mounted.
 *
 * Console design system: the palette is imported from arch/baseStyles.ts
 * (single source — §2.1). Legacy --ah-* names are kept and remapped so the
 * step components keep working; accent washes are derived from the four
 * canonical accents (never new hues).
 */

import "@/styles/clinical-fonts.css";
import { consolePalette } from "../arch/baseStyles";

export default function OnboardingStyles() {
  return (
    <style>{`
      @keyframes ah-onb-spin { to { transform: rotate(360deg); } }
      @keyframes ah-onb-tick-in {
        0%   { stroke-dashoffset: 24; }
        100% { stroke-dashoffset: 0; }
      }
      @keyframes ah-onb-fade-up {
        from { opacity: 0; transform: translateY(8px); }
        to   { opacity: 1; transform: translateY(0); }
      }
      @keyframes ah-onb-pulse-dot {
        0%, 100% { opacity: 0.55; }
        50%      { opacity: 1; }
      }

      .ah-onb-root {
        ${consolePalette}

        /* Legacy names — remapped to the console palette. */
        --ah-bg-base: var(--canvas);
        --ah-text-primary: var(--ink);
        --ah-text-secondary: var(--ink-soft);
        --ah-text-muted: var(--ink-faint);
        --ah-text-faint: var(--ink-faint);
        --ah-hairline: var(--hairline);

        /* Accent washes / lines / deep-text — derived from §2.1 accents. */
        --ah-green-wash: rgba(76, 166, 60, 0.10);
        --ah-green-line: rgba(76, 166, 60, 0.38);
        --ah-green-deep: #3c7a31;
        --ah-green-glow: rgba(76, 166, 60, 0.22);
        --ah-pink-wash: rgba(232, 68, 123, 0.09);
        --ah-pink-line: rgba(232, 68, 123, 0.35);
        --ah-pink-deep: #bb2f60;
        --ah-lime-wash: rgba(213, 225, 78, 0.22);
        --ah-lime-line: rgba(178, 190, 40, 0.55);
        --ah-orange-wash: rgba(236, 148, 64, 0.12);
        --ah-orange-line: rgba(236, 148, 64, 0.42);
        --ah-orange-deep: #9c5c1b;
        --ah-faint-30: rgba(26, 27, 26, 0.30);

        position: relative;
        min-height: 100vh;
        background: var(--canvas);
        color: var(--ink);
        font-family: var(--sans);
        -webkit-font-smoothing: antialiased;
        text-rendering: optimizeLegibility;
        display: flex;
        flex-direction: column;
      }

      /* Page atmosphere — felt, not seen (same auras as the landing). */
      .ah-onb-root::before {
        content: "";
        position: fixed;
        inset: 0;
        pointer-events: none;
        z-index: 0;
        background:
          radial-gradient(56rem 40rem at 12% -6%, rgba(76, 166, 60, 0.055), transparent 70%),
          radial-gradient(52rem 44rem at 96% 44%, rgba(236, 148, 64, 0.05), transparent 70%);
      }

      .ah-onb-root > * { position: relative; z-index: 1; }

      .ah-onb-root *,
      .ah-onb-root *::before,
      .ah-onb-root *::after { box-sizing: border-box; }

      .ah-onb-root ::selection {
        background: var(--lime);
        color: var(--ink);
      }

      .ah-onb-root :focus-visible {
        outline: 2px solid var(--ink);
        outline-offset: 2px;
        border-radius: 4px;
      }

      .ah-onb-root button {
        font: inherit;
        cursor: pointer;
        font-family: inherit;
      }

      .ah-onb-root input,
      .ah-onb-root select { font: inherit; font-family: inherit; }

      .ah-onb-root a { color: inherit; text-decoration: none; }

      .ah-onb-root .ah-chrome {
        font-family: var(--mono);
        font-size: 0.6875rem;
        font-weight: 400;
        letter-spacing: 0.08em;
        text-transform: uppercase;
      }

      @media (prefers-reduced-motion: reduce) {
        .ah-onb-root *, .ah-onb-root *::before, .ah-onb-root *::after {
          animation-duration: 0.01ms !important;
          animation-iteration-count: 1 !important;
          transition-duration: 0.01ms !important;
        }
      }
    `}</style>
  );
}
