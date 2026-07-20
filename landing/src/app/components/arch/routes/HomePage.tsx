/**
 * `/` — hero only (PRD §2). Wordmark + menu live in the shell; this page is
 * one H1 and two buttons over the centered aura. Nothing else.
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
        <h1>
          Data to Power<br className="h1-break" /> Clinical and Medical AI
        </h1>
        <div className="hero-ctas">
          <button type="button" className="btn btn-primary" onClick={() => actions.openLead("request_data")}>
            Request data
          </button>
          <button type="button" className="btn" onClick={actions.openContributor}>
            Become a contributor
          </button>
        </div>
      </div>
    </section>
  );
}
