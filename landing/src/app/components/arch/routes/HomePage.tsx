/**
 * `/` — hero only (PRD §2). Wordmark + menu live in the shell; this page is
 * one H1, two buttons, and the YC badge over the centered aura. Nothing else.
 */

import type { ShellActions } from "../ArchShell";

export function HomePage({ actions }: { actions: ShellActions }) {
  return (
    <section className="hero-min" aria-label="Archangel Health">
      <div className="glow-field" aria-hidden="true">
        <i className="glow-a" />
        <i className="glow-b" />
      </div>
      <div>
        {/* The headline carries the hero alone, so the line break is authored
            rather than left to the browser: each half is its own block, which
            makes "Medical AI" a deliberate second line at every width instead
            of a stray orphan. The lead half still wraps internally on phones. */}
        <h1>
          <span className="h1-lead">Infrastructure to Power</span>
          <span className="h1-tail">Medical AI</span>
        </h1>
        <div className="hero-ctas">
          <button type="button" className="btn btn-primary" onClick={() => actions.openLead("request_data")}>
            Request products
          </button>
          <button type="button" className="btn" onClick={actions.openContributor}>
            Become a contributor
          </button>
        </div>
        {/* Backed by Y Combinator. The mark is an inline SVG rather than an
            asset file, following the wordmark halo in ArchShell: the landing
            app ships no image assets and this way the badge is sharp at any
            size with no extra request. The orange is YC's own brand colour, so
            it is a literal rather than currentColor. */}
        <a
          className="chrome chrome-box hero-yc"
          href="https://www.ycombinator.com/"
          target="_blank"
          rel="noopener noreferrer"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
            <rect width="24" height="24" rx="3" fill="#FF6600" />
            <path
              d="M11.42 13.55 7.6 6.6h1.86l2.05 3.95c.19.37.36.72.5 1.05h.03c.15-.35.32-.7.5-1.05l2.06-3.95h1.78l-3.8 6.93v3.87h-1.16z"
              fill="#fff"
            />
          </svg>
          Backed by Y Combinator
        </a>
      </div>
    </section>
  );
}
