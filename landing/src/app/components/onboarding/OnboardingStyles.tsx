/**
 * Scoped global styles + keyframes for the Archangel Health onboarding flow.
 *
 * Loaded once by `OnboardingWizard`. Keeps the rest of the landing app
 * untouched — these styles only apply when the onboarding shell is mounted.
 *
 * Token reference: design_handoff_onboarding_flow/README.md "Design Tokens".
 */

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
        --ah-bg-base: #07070A;
        --ah-text-primary: #F5F5F7;
        --ah-text-secondary: rgba(245,245,247,0.72);
        --ah-text-muted: rgba(245,245,247,0.50);
        --ah-text-faint: rgba(245,245,247,0.32);
        --ah-cyan-soft: #67E8F9;
        --ah-blue: #2563EB;
        --ah-hairline: rgba(255,255,255,0.07);

        position: relative;
        min-height: 100vh;
        background:
          radial-gradient(ellipse 1400px 900px at 50% -10%, rgba(38, 99, 235, 0.10) 0%, transparent 55%),
          radial-gradient(ellipse 900px 600px at 90% 110%, rgba(0, 255, 255, 0.06) 0%, transparent 60%),
          var(--ah-bg-base);
        background-attachment: fixed;
        color: var(--ah-text-primary);
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        -webkit-font-smoothing: antialiased;
        text-rendering: optimizeLegibility;
        display: flex;
        flex-direction: column;
      }

      /* Faint film-grain layer for depth — fixed across the viewport. */
      .ah-onb-root::before {
        content: "";
        position: fixed;
        inset: 0;
        pointer-events: none;
        z-index: 0;
        background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='200' height='200'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2' /></filter><rect width='200' height='200' filter='url(%23n)' opacity='0.5'/></svg>");
        opacity: 0.025;
        mix-blend-mode: overlay;
      }

      .ah-onb-root > * { position: relative; z-index: 1; }

      .ah-onb-root *,
      .ah-onb-root *::before,
      .ah-onb-root *::after { box-sizing: border-box; }

      .ah-onb-root ::selection {
        background: rgba(103, 232, 249, 0.30);
        color: #fff;
      }

      .ah-onb-root button {
        font: inherit;
        cursor: pointer;
        font-family: inherit;
      }

      .ah-onb-root input,
      .ah-onb-root select { font: inherit; font-family: inherit; }

      .ah-onb-root a { color: inherit; text-decoration: none; }
    `}</style>
  );
}
