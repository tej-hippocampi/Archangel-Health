/**
 * `/mission` — statement, 3-line thesis, team, contact (PRD §8). Opens with
 * the preserved statement block, unchanged.
 */

import { mailto, type ShellActions } from "../ArchShell";

const TEAM = [
  {
    initials: "TP",
    name: "Tej Patel",
    role: "Co-Founder / Co-CEO",
    affiliation: "UC Berkeley",
    linkedin: "https://www.linkedin.com/in/tej-patel-873b952a8/",
  },
  {
    initials: "AB",
    name: "Aryaa Bhatia",
    role: "Co-Founder / Co-CEO",
    affiliation: "UC Berkeley",
    linkedin: "https://www.linkedin.com/in/aryaa-bhatia-8aaaa0319/",
  },
];

const CONTACTS = [
  { tag: "General", subject: "Hello — Archangel Health" },
  { tag: "Partnerships", subject: "Partnership — Archangel Health" },
  { tag: "Press", subject: "Press — Archangel Health" },
];

export function MissionPage({ actions }: { actions: ShellActions }) {
  return (
    <div className="route">
      {/* Preserved statement block — unchanged. */}
      <section className="statement">
        <p className="statement-line reveal">
          Doctors earn from their judgment.<br />
          Models learn from it.<br />
          <span className="quiet">The hardest cases become the most valuable data.</span>
        </p>
      </section>

      <section className="section">
        <p className="crumb chrome reveal"><span className="root">Archangel</span><span className="sep">/</span><span className="here">05 · Mission</span></p>

        <div className="thesis reveal">
          <p>Verification is the scarce input in medical AI.</p>
          <p><mark className="thesis-mark">A 70% benchmark score is irrelevant when a patient is downstream.</mark></p>
          <p>The people who carry the consequences should define what correct means.</p>
          <p className="thesis-byline">
            <span className="chrome">Written by</span>
            <a href="https://www.linkedin.com/in/tej-patel-873b952a8/" target="_blank" rel="noreferrer">Tej&nbsp;Patel</a>
            <span className="amp">&amp;</span>
            <a href="https://www.linkedin.com/in/aryaa-bhatia-8aaaa0319/" target="_blank" rel="noreferrer">Aryaa&nbsp;Bhatia</a>
          </p>
        </div>

        <div className="sub-crumb reveal">
          <h2>Team</h2>
        </div>
        <div className="team">
          {TEAM.map((t) => (
            <div className="c-card team-card reveal" key={t.name}>
              <span className="t-avatar" aria-hidden="true">{t.initials}</span>
              <h3>{t.name}</h3>
              <span className="t-role">{t.role}</span>
              <span className="t-aff label"><span className="dot dot-faint" />{t.affiliation}</span>
              <a className="chrome chrome-box" href={t.linkedin} target="_blank" rel="noreferrer">
                LinkedIn ↗
              </a>
            </div>
          ))}
        </div>

        <p className="place-line reveal">
          <span className="chrome">Where we are</span>
          <span>Berkeley, California</span>
        </p>

        <div className="contact-rows reveal">
          {CONTACTS.map((c) => (
            <div className="contact-row" key={c.tag}>
              <span className="chrome">{c.tag}</span>
              <a href={mailto(c.subject)} onClick={actions.handleMailto}>
                {mailto(c.subject).replace("mailto:", "").split("?")[0]}
              </a>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
