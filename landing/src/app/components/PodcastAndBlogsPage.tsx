import { useState } from "react";
import TeamWhitepaperPage from "@/app/components/TeamWhitepaperPage";

type PodcastEpisode = {
  id: string;
  title: string;
  guest: string;
  guestTitle: string;
  topic: string;
  summary: string;
  duration: string;
  publishedOn: string;
  audioUrl?: string;
  videoUrl?: string;
  showNotesUrl?: string;
  tags: string[];
};

const PODCAST_EPISODES: PodcastEpisode[] = [
  {
    id: "ep-01",
    title: "Replace with the real episode title",
    guest: "Guest Name",
    guestTitle: "Title, Organization",
    topic: "Value-Based Care",
    summary:
      "Short 1-2 sentence description of what the conversation covers. Edit in PodcastAndBlogsPage.tsx — the PODCAST_EPISODES array drives every card on this page.",
    duration: "00:00",
    publishedOn: "2026-01-01",
    audioUrl: "",
    videoUrl: "",
    showNotesUrl: "",
    tags: ["Value-Based Care", "Policy"],
  },
  {
    id: "ep-02",
    title: "Replace with the real episode title",
    guest: "Guest Name",
    guestTitle: "Title, Organization",
    topic: "Health Policy",
    summary: "Short summary of the second interview. Update fields inline.",
    duration: "00:00",
    publishedOn: "2026-01-01",
    audioUrl: "",
    videoUrl: "",
    showNotesUrl: "",
    tags: ["Health Policy", "CMS"],
  },
  {
    id: "ep-03",
    title: "Replace with the real episode title",
    guest: "Guest Name",
    guestTitle: "Title, Organization",
    topic: "TEAM Model",
    summary: "Short summary of the third interview.",
    duration: "00:00",
    publishedOn: "2026-01-01",
    audioUrl: "",
    videoUrl: "",
    showNotesUrl: "",
    tags: ["TEAM", "Bundled Payments"],
  },
];

type ResourceTab = "podcasts" | "blogs";

export default function PodcastAndBlogsPage() {
  const [tab, setTab] = useState<ResourceTab>("podcasts");

  return (
    <div className="resources-page">
      <header className="resources-hero">
        <p className="resources-eyebrow">Resources</p>
        <h1 className="resources-headline">Podcast and Blogs</h1>
        <p className="resources-sub">
          Conversations and writing on health technology, value-based care, and the policy shaping the next era of episode-based payment.
        </p>

        <div className="resources-tabs" role="tablist" aria-label="Resource type">
          <button
            type="button"
            role="tab"
            aria-selected={tab === "podcasts"}
            className={`resources-tab ${tab === "podcasts" ? "resources-tab-active" : ""}`}
            onClick={() => setTab("podcasts")}
          >
            Podcast Interviews
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "blogs"}
            className={`resources-tab ${tab === "blogs" ? "resources-tab-active" : ""}`}
            onClick={() => setTab("blogs")}
          >
            Blogs &amp; White Papers
          </button>
        </div>
      </header>

      {tab === "podcasts" && (
        <section className="resources-section" aria-labelledby="podcasts-heading">
          <h2 id="podcasts-heading" className="resources-section-title">
            Podcast Interviews
          </h2>
          <p className="resources-section-sub">
            Long-form interviews with operators, policymakers, and clinical leaders building the future of value-based care.
          </p>

          <div className="podcast-grid">
            {PODCAST_EPISODES.map((ep) => (
              <PodcastCard key={ep.id} episode={ep} />
            ))}
          </div>
        </section>
      )}

      {tab === "blogs" && (
        <section className="resources-section" aria-labelledby="blogs-heading">
          <h2 id="blogs-heading" className="resources-section-title">
            Blogs &amp; White Papers
          </h2>
          <p className="resources-section-sub">
            Deep dives on the CMS TEAM model and the operational changes hospitals need to win at episode-based care.
          </p>

          <div className="blog-feature">
            <div className="blog-feature-meta">
              <span className="blog-feature-kind">White paper</span>
              <h3 className="blog-feature-title">The CMS TEAM Model: An Operator&rsquo;s Playbook</h3>
              <p className="blog-feature-desc">
                A practical breakdown of the financial, clinical, and operational levers that determine whether a hospital wins or loses under TEAM.
              </p>
              <a className="blog-feature-link" href="/team-whitepaper.pdf" target="_blank" rel="noopener noreferrer">
                Open as PDF ↗
              </a>
            </div>
            <div className="blog-feature-viewer">
              <TeamWhitepaperPage />
            </div>
          </div>
        </section>
      )}

      <style>{styles}</style>
    </div>
  );
}

function PodcastCard({ episode }: { episode: PodcastEpisode }) {
  return (
    <article className="podcast-card">
      <div className="podcast-card-frame" />
      <div className="podcast-card-body">
        <div className="podcast-card-top">
          <span className="podcast-card-topic">{episode.topic}</span>
          <span className="podcast-card-duration">{episode.duration}</span>
        </div>

        <h3 className="podcast-card-title">{episode.title}</h3>

        <div className="podcast-card-guest">
          <span className="podcast-card-guest-name">{episode.guest}</span>
          <span className="podcast-card-guest-title">{episode.guestTitle}</span>
        </div>

        <p className="podcast-card-summary">{episode.summary}</p>

        <div className="podcast-card-tags">
          {episode.tags.map((tag) => (
            <span key={tag} className="podcast-card-tag">
              {tag}
            </span>
          ))}
        </div>

        <div className="podcast-card-actions">
          {episode.audioUrl && (
            <a href={episode.audioUrl} target="_blank" rel="noopener noreferrer" className="podcast-card-btn podcast-card-btn-primary">
              Listen
            </a>
          )}
          {episode.videoUrl && (
            <a href={episode.videoUrl} target="_blank" rel="noopener noreferrer" className="podcast-card-btn">
              Watch
            </a>
          )}
          {episode.showNotesUrl && (
            <a href={episode.showNotesUrl} target="_blank" rel="noopener noreferrer" className="podcast-card-btn">
              Show notes
            </a>
          )}
        </div>

        <p className="podcast-card-date">Published {episode.publishedOn}</p>
      </div>
    </article>
  );
}

const styles = `
  .resources-page {
    min-height: calc(100vh - 3.5rem);
    background: linear-gradient(180deg, #0a0a0b 0%, #0d0d10 100%);
    color: #f5f5f7;
    padding-bottom: 4rem;
  }

  .resources-hero {
    max-width: 1100px;
    margin: 0 auto;
    padding: 4rem 1.5rem 2.5rem;
    text-align: center;
  }

  .resources-eyebrow {
    font-size: 0.8125rem;
    font-weight: 600;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: rgba(103, 232, 249, 0.85);
    margin: 0 0 0.75rem;
  }

  .resources-headline {
    font-size: clamp(2.25rem, 5vw, 3.5rem);
    font-weight: 500;
    letter-spacing: -0.03em;
    line-height: 1.15;
    margin: 0 0 1rem;
    color: #f5f5f7;
  }

  .resources-sub {
    font-size: 1.0625rem;
    line-height: 1.6;
    color: rgba(245, 245, 247, 0.7);
    max-width: 720px;
    margin: 0 auto 2rem;
  }

  .resources-tabs {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.35rem;
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 9999px;
    background: rgba(255, 255, 255, 0.03);
  }

  .resources-tab {
    appearance: none;
    border: none;
    background: transparent;
    padding: 0.55rem 1.1rem;
    border-radius: 9999px;
    font-size: 0.875rem;
    font-weight: 500;
    color: rgba(245, 245, 247, 0.7);
    cursor: pointer;
    transition: color 0.2s, background 0.2s;
  }

  .resources-tab:hover {
    color: #f5f5f7;
    background: rgba(255, 255, 255, 0.05);
  }

  .resources-tab-active {
    background: #00ffff;
    color: #0a0a0b;
  }

  .resources-tab-active:hover {
    background: #77ffff;
    color: #0a0a0b;
  }

  .resources-section {
    max-width: 1200px;
    margin: 0 auto;
    padding: 1rem 1.5rem 3rem;
  }

  .resources-section-title {
    font-size: clamp(1.5rem, 3vw, 2rem);
    font-weight: 500;
    letter-spacing: -0.02em;
    margin: 0 0 0.5rem;
  }

  .resources-section-sub {
    font-size: 0.9375rem;
    color: rgba(245, 245, 247, 0.65);
    margin: 0 0 2rem;
    max-width: 720px;
  }

  .podcast-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
    gap: 1.5rem;
  }

  .podcast-card {
    position: relative;
    border-radius: 1rem;
    overflow: hidden;
    border: 1px solid rgba(103, 232, 249, 0.1);
    background: linear-gradient(140deg, rgba(22, 22, 25, 0.96) 0%, rgba(10, 10, 11, 0.98) 100%);
    transition: transform 0.3s cubic-bezier(0.16, 1, 0.3, 1), box-shadow 0.3s cubic-bezier(0.16, 1, 0.3, 1);
  }

  .podcast-card:hover {
    transform: translateY(-4px);
    box-shadow: 0 16px 48px rgba(0, 0, 0, 0.5), 0 0 0 1px rgba(103, 232, 249, 0.18);
  }

  .podcast-card-frame {
    position: absolute;
    inset: 0;
    border-radius: 1rem;
    pointer-events: none;
    background: linear-gradient(135deg, rgba(103, 232, 249, 0.14), rgba(45, 212, 191, 0.06));
    -webkit-mask: linear-gradient(#fff 0 0) padding-box, linear-gradient(#fff 0 0);
    -webkit-mask-composite: xor;
    mask-composite: exclude;
    padding: 1px;
    opacity: 0.7;
  }

  .podcast-card-body {
    position: relative;
    padding: 1.5rem 1.5rem 1.25rem;
    display: flex;
    flex-direction: column;
    gap: 0.85rem;
  }

  .podcast-card-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
  }

  .podcast-card-topic {
    font-size: 0.6875rem;
    font-weight: 600;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: rgba(103, 232, 249, 0.9);
  }

  .podcast-card-duration {
    font-size: 0.75rem;
    color: rgba(245, 245, 247, 0.55);
    font-variant-numeric: tabular-nums;
  }

  .podcast-card-title {
    font-size: 1.25rem;
    font-weight: 600;
    letter-spacing: -0.02em;
    line-height: 1.3;
    margin: 0;
    color: #ffffff;
  }

  .podcast-card-guest {
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
  }

  .podcast-card-guest-name {
    font-size: 0.875rem;
    font-weight: 500;
    color: rgba(245, 245, 247, 0.95);
  }

  .podcast-card-guest-title {
    font-size: 0.8125rem;
    color: rgba(245, 245, 247, 0.55);
  }

  .podcast-card-summary {
    font-size: 0.9375rem;
    line-height: 1.6;
    color: rgba(245, 245, 247, 0.78);
    margin: 0;
  }

  .podcast-card-tags {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
  }

  .podcast-card-tag {
    font-size: 0.6875rem;
    font-weight: 500;
    padding: 0.25rem 0.6rem;
    border-radius: 9999px;
    background: rgba(103, 232, 249, 0.08);
    border: 1px solid rgba(103, 232, 249, 0.18);
    color: rgba(103, 232, 249, 0.95);
    letter-spacing: 0.02em;
  }

  .podcast-card-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    margin-top: 0.25rem;
  }

  .podcast-card-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 0.45rem 0.95rem;
    border-radius: 9999px;
    border: 1px solid rgba(255, 255, 255, 0.18);
    background: transparent;
    font-size: 0.8125rem;
    font-weight: 500;
    color: #f5f5f7;
    text-decoration: none;
    transition: background 0.2s, border-color 0.2s, color 0.2s;
  }

  .podcast-card-btn:hover {
    background: rgba(255, 255, 255, 0.08);
    border-color: rgba(255, 255, 255, 0.3);
  }

  .podcast-card-btn-primary {
    background: #00ffff;
    border-color: #00ffff;
    color: #0a0a0b;
  }

  .podcast-card-btn-primary:hover {
    background: #77ffff;
    border-color: #77ffff;
    color: #0a0a0b;
  }

  .podcast-card-date {
    font-size: 0.75rem;
    color: rgba(245, 245, 247, 0.45);
    margin: 0.25rem 0 0;
  }

  .blog-feature {
    display: grid;
    grid-template-columns: 1fr;
    gap: 2rem;
    align-items: start;
  }

  @media (min-width: 960px) {
    .blog-feature {
      grid-template-columns: 320px 1fr;
    }
  }

  .blog-feature-meta {
    position: sticky;
    top: 5rem;
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 1rem;
    padding: 1.5rem;
    background: rgba(255, 255, 255, 0.02);
  }

  .blog-feature-kind {
    font-size: 0.6875rem;
    font-weight: 600;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: rgba(103, 232, 249, 0.9);
  }

  .blog-feature-title {
    font-size: 1.25rem;
    font-weight: 600;
    letter-spacing: -0.02em;
    margin: 0.5rem 0 0.75rem;
    color: #ffffff;
    line-height: 1.3;
  }

  .blog-feature-desc {
    font-size: 0.9375rem;
    line-height: 1.55;
    color: rgba(245, 245, 247, 0.7);
    margin: 0 0 1rem;
  }

  .blog-feature-link {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    padding: 0.5rem 1rem;
    border-radius: 9999px;
    background: #00ffff;
    color: #0a0a0b;
    font-size: 0.8125rem;
    font-weight: 600;
    text-decoration: none;
    transition: background 0.2s;
  }

  .blog-feature-link:hover {
    background: #77ffff;
  }

  .blog-feature-viewer {
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 1rem;
    overflow: hidden;
    background: #0a0a0b;
  }

  @media (max-width: 640px) {
    .resources-hero {
      padding: 2.5rem 1.25rem 1.5rem;
    }
    .resources-section {
      padding: 0.5rem 1.25rem 2rem;
    }
  }
`;
