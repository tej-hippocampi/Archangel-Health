import { useMemo, useState } from "react";

// ---------------- DATA ----------------
// Drives every card on this page. Add new entries to extend.

type PodcastEpisode = {
  id: string;
  number: string;
  title: string;
  guest: string;
  guestTitle: string;
  duration: string;
  publishedOn: string;
  audioUrl?: string;
  imageUrl?: string;
};

type Whitepaper = {
  id: string;
  label: string;
  title: string;
  audience: string;
  pages: number;
  date: string;
  pdfUrl?: string;
};

const PODCAST_EPISODES: PodcastEpisode[] = [
  {
    id: "ep-01",
    number: "05",
    title: "Inside a TEAM-ready hospital",
    guest: "Dr. Sarah Chen",
    guestTitle: "CMO, Northstar Orthopedic",
    duration: "47:12",
    publishedOn: "May 6, 2026",
    audioUrl: "",
    imageUrl: "/podcast-photo.png",
  },
];

const WHITEPAPERS: Whitepaper[] = [
  {
    id: "wp-01",
    label: "Whitepaper #1",
    title: "How Hospitals Can Use Technology to Win at the CMS TEAM Model",
    audience:
      "Hospital CMOs, CFOs, VP Quality Care, Surgery-Specific Service Line Directors",
    pages: 5,
    date: "Apr 2026",
    pdfUrl: "/team-whitepaper.pdf",
  },
];

type ResourceTab = "podcasts" | "whitepaper";

// ---------------- PAGE ----------------

export default function PodcastAndBlogsPage() {
  const [tab, setTab] = useState<ResourceTab>("podcasts");

  return (
    <div className="resources-page">
      <div className="resources-page-bg" aria-hidden>
        <div className="resources-page-aura" />
      </div>

      <header className="resources-hero">
        <h1 className="resources-headline">
          Podcast <span className="resources-headline-amp">&amp;</span> Blogs
        </h1>

        <div className="resources-tabs" role="tablist" aria-label="Resource type">
          <button
            type="button"
            role="tab"
            aria-selected={tab === "podcasts"}
            className={`resources-tab ${tab === "podcasts" ? "resources-tab-active" : ""}`}
            onClick={() => setTab("podcasts")}
          >
            Podcast
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "whitepaper"}
            className={`resources-tab ${tab === "whitepaper" ? "resources-tab-active" : ""}`}
            onClick={() => setTab("whitepaper")}
          >
            White Paper
          </button>
        </div>
      </header>

      <main className="resources-main">
        {tab === "podcasts" ? (
          <PodcastView episode={PODCAST_EPISODES[0]} />
        ) : (
          <WhitepaperView paper={WHITEPAPERS[0]} />
        )}
      </main>

      <style>{styles}</style>
    </div>
  );
}

// ---------------- PODCAST VIEW ----------------

function PodcastView({ episode }: { episode: PodcastEpisode }) {
  return (
    <article className="pod">
      <div className="pod-frame-outer">
        <div className="pod-frame-inner" />
      </div>

      <div className="pod-media">
        {episode.imageUrl && (
          <img
            src={episode.imageUrl}
            alt="Podcast episode cover"
            className="pod-photo"
          />
        )}
        <div className="pod-photo-fade" aria-hidden />

        <button className="pod-play-big" aria-label={`Play episode ${episode.number}`} type="button">
          <svg viewBox="0 0 24 24" width="26" height="26" aria-hidden>
            <path d="M6 4l14 8-14 8V4z" fill="#0a0a0b" />
          </svg>
        </button>

        <div className="pod-overlay">
          <div className="pod-meta">
            <span className="pod-meta-num">EP.{episode.number}</span>
            <span className="pod-meta-dot" />
            <span className="pod-meta-time">{episode.duration}</span>
          </div>
          <h2 className="pod-title">{episode.title}</h2>
          <div className="pod-guest">
            <span className="pod-guest-name">{episode.guest}</span>
            <span className="pod-guest-sep">·</span>
            <span className="pod-guest-title">{episode.guestTitle}</span>
          </div>
          <Waveform bars={72} height={36} />
        </div>
      </div>
    </article>
  );
}

// ---------------- WHITEPAPER VIEW ----------------

function WhitepaperView({ paper }: { paper: Whitepaper }) {
  return (
    <article className="wp-hero">
      <div className="pod-frame-outer">
        <div className="pod-frame-inner" />
      </div>

      <HeartEKGArt />
      <div className="wp-hero-veil" aria-hidden />

      <div className="wp-hero-inner">
        <div className="wp-meta">
          <span className="pod-meta-num">{paper.label}</span>
          <span className="pod-meta-dot" />
          <span className="pod-meta-time">
            {paper.pages} pages · {paper.date}
          </span>
        </div>

        <h2 className="wp-title">{paper.title}</h2>

        <div className="wp-audience">
          <span className="wp-audience-label">Audience</span>
          <span className="wp-audience-text">{paper.audience}</span>
        </div>

        <div className="wp-actions">
          <a href="/?view=whitepaper" className="btn-primary">
            Read paper
          </a>
          <a
            href={paper.pdfUrl || "#"}
            download
            className="btn-ghost"
          >
            Download PDF
          </a>
        </div>
      </div>
    </article>
  );
}

// ---------------- WAVEFORM ----------------

function Waveform({ bars = 72, height = 36 }: { bars?: number; height?: number }) {
  const items = useMemo(
    () =>
      Array.from({ length: bars }, (_, i) => {
        const t = i / bars;
        const v = 0.35 + 0.45 * Math.abs(Math.sin(i * 1.7) * Math.cos(i * 0.43 + 1.2));
        const h = Math.max(4, v * height);
        const opacity = 0.4 + 0.55 * (1 - Math.abs(t - 0.5) * 1.4);
        return { h, opacity };
      }),
    [bars, height],
  );

  return (
    <div className="wave">
      {items.map((b, i) => (
        <span
          key={i}
          className="wave-bar"
          style={{ height: b.h, opacity: b.opacity }}
        />
      ))}
    </div>
  );
}

// ---------------- HEART + EKG SVG ----------------

function HeartEKGArt() {
  return (
    <svg
      className="wp-hero-bg"
      viewBox="0 0 800 500"
      preserveAspectRatio="xMaxYMin slice"
      aria-hidden
    >
      <defs>
        <radialGradient id="wp-glow" cx="82%" cy="25%" r="38%">
          <stop offset="0%" stopColor="#1a3a8a" stopOpacity="0.55" />
          <stop offset="60%" stopColor="#0a1530" stopOpacity="0.35" />
          <stop offset="100%" stopColor="#060614" stopOpacity="0" />
        </radialGradient>
        <pattern id="wp-grid" x="0" y="0" width="24" height="24" patternUnits="userSpaceOnUse">
          <path d="M 24 0 L 0 0 0 24" fill="none" stroke="rgba(80,160,255,0.07)" strokeWidth="1" />
        </pattern>
        <radialGradient id="wp-grid-mask" cx="82%" cy="25%" r="40%">
          <stop offset="0%" stopColor="#ffffff" stopOpacity="1" />
          <stop offset="100%" stopColor="#ffffff" stopOpacity="0" />
        </radialGradient>
        <mask id="wp-grid-fade">
          <rect x="0" y="0" width="800" height="500" fill="url(#wp-grid-mask)" />
        </mask>
        <filter id="wp-cyan-glow" x="-50%" y="-50%" width="200%" height="200%">
          <feGaussianBlur stdDeviation="3" result="blur1" />
          <feGaussianBlur stdDeviation="8" result="blur2" />
          <feMerge>
            <feMergeNode in="blur2" />
            <feMergeNode in="blur1" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
        <filter id="wp-cyan-glow-soft" x="-50%" y="-50%" width="200%" height="200%">
          <feGaussianBlur stdDeviation="5" result="blur1" />
          <feMerge>
            <feMergeNode in="blur1" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
      </defs>

      <rect x="0" y="0" width="800" height="500" fill="url(#wp-glow)" />
      <g mask="url(#wp-grid-fade)">
        <rect x="0" y="0" width="800" height="500" fill="url(#wp-grid)" />
      </g>

      <g transform="translate(660 140) scale(0.62)" filter="url(#wp-cyan-glow)">
        <path
          d="M 0,-30 C 0,-90 -90,-110 -130,-60 C -170,-20 -170,40 -100,90 L 0,160 L 100,90 C 170,40 170,-20 130,-60 C 90,-110 0,-90 0,-30 Z"
          fill="none"
          stroke="#7fefff"
          strokeWidth="4.5"
          strokeLinejoin="round"
          strokeLinecap="round"
        />
        <path
          d="M -160,15 L -75,15 L -55,-10 L -35,40 L -15,-55 L 10,55 L 35,-25 L 55,15 L 160,15"
          fill="none"
          stroke="#a5f8ff"
          strokeWidth="3.5"
          strokeLinejoin="round"
          strokeLinecap="round"
        />
      </g>

      <g filter="url(#wp-cyan-glow-soft)" opacity="0.7">
        <g transform="translate(580 95)">
          <rect x="-8" y="-1.5" width="16" height="3" fill="#a5f8ff" rx="1" />
          <rect x="-1.5" y="-8" width="3" height="16" fill="#a5f8ff" rx="1" />
        </g>
        <g transform="translate(720 215)" opacity="0.85">
          <rect x="-7" y="-1.5" width="14" height="3" fill="#7fefff" rx="1" />
          <rect x="-1.5" y="-7" width="3" height="14" fill="#7fefff" rx="1" />
        </g>
        <g transform="translate(515 175)" opacity="0.5">
          <rect x="-5" y="-1" width="10" height="2" fill="#7fefff" rx="1" />
          <rect x="-1" y="-5" width="2" height="10" fill="#7fefff" rx="1" />
        </g>
      </g>
    </svg>
  );
}

// ---------------- STYLES ----------------

const styles = `
  .resources-page {
    position: relative;
    min-height: calc(100vh - 3.5rem);
    background: #0a0a0b;
    color: #f5f5f7;
    overflow: hidden;
    padding-bottom: 6rem;
  }
  .resources-page-bg { position: absolute; inset: 0; pointer-events: none; z-index: 0; }
  .resources-page-aura {
    position: absolute;
    width: 720px; height: 720px;
    top: -300px; left: 50%;
    transform: translateX(-50%);
    border-radius: 50%;
    filter: blur(140px);
    opacity: 0.35;
    background: radial-gradient(circle, rgba(0,255,255,0.2), rgba(0,255,255,0) 60%);
  }

  .resources-hero {
    position: relative; z-index: 1;
    max-width: 1100px;
    margin: 0 auto;
    padding: 6rem 1.5rem 3rem;
    text-align: center;
  }
  .resources-headline {
    font-size: clamp(2.75rem, 6vw, 4rem);
    font-weight: 500;
    letter-spacing: -0.035em;
    line-height: 1.05;
    margin: 0 0 2.5rem;
    text-shadow: 0 0 40px rgba(0,255,255,0.08);
  }
  .resources-headline-amp {
    font-style: italic;
    font-weight: 400;
    color: rgba(103,232,249,0.85);
    margin: 0 0.1em;
  }

  .resources-tabs {
    display: inline-flex;
    gap: 0.3rem;
    padding: 0.35rem;
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 9999px;
    background: rgba(255,255,255,0.03);
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
  }
  .resources-tab {
    appearance: none; border: none;
    background: transparent;
    padding: 0.6rem 1.4rem;
    border-radius: 9999px;
    font-size: 0.875rem; font-weight: 500;
    color: rgba(245,245,247,0.7);
    cursor: pointer;
    transition: color 0.2s, background 0.2s, box-shadow 0.2s;
  }
  .resources-tab:hover { color: #f5f5f7; }
  .resources-tab-active {
    background: #00ffff; color: #0a0a0b;
    box-shadow: 0 0 24px rgba(0,255,255,0.25);
  }

  .resources-main {
    position: relative; z-index: 1;
    max-width: 1180px;
    margin: 0 auto;
    padding: 0 1.5rem;
  }

  /* PODCAST */
  .pod {
    position: relative;
    border-radius: 1.5rem;
    overflow: hidden;
    background: #0a0a0b;
  }
  .pod-frame-outer {
    position: absolute; inset: 0;
    border: 6px solid transparent;
    border-radius: 1.5rem;
    background: linear-gradient(135deg, rgba(103,232,249,0.32) 0%, rgba(45,212,191,0.18) 50%, rgba(103,232,249,0.24) 100%) border-box;
    -webkit-mask: linear-gradient(#fff 0 0) padding-box, linear-gradient(#fff 0 0);
    -webkit-mask-composite: xor; mask-composite: exclude;
    opacity: 0.6; pointer-events: none; z-index: 3;
  }
  .pod-frame-inner {
    position: absolute; inset: 10px;
    border: 1px solid rgba(103,232,249,0.2);
    border-radius: 1rem;
    box-shadow: inset 0 0 8px rgba(45,212,191,0.12), 0 0 8px rgba(103,232,249,0.08);
    pointer-events: none;
  }
  .pod-media {
    position: relative;
    aspect-ratio: 1920 / 1008;
    width: 100%;
    overflow: hidden;
  }
  .pod-photo {
    position: absolute; inset: 0;
    width: 100%; height: 100%;
    object-fit: cover;
    filter: contrast(1.02) saturate(0.92);
  }
  .pod-photo-fade {
    position: absolute; inset: 0;
    background:
      linear-gradient(180deg, rgba(10,10,11,0) 35%, rgba(10,10,11,0.55) 70%, rgba(10,10,11,0.95) 100%),
      linear-gradient(90deg, rgba(10,10,11,0.5) 0%, rgba(10,10,11,0) 35%, rgba(10,10,11,0) 65%, rgba(10,10,11,0.5) 100%);
    pointer-events: none;
  }

  .pod-overlay {
    position: absolute;
    left: 0; right: 0; bottom: 0;
    padding: 2rem 2.5rem 2rem;
    display: flex; flex-direction: column;
    gap: 0.75rem;
    z-index: 2;
  }
  .pod-meta {
    display: inline-flex; align-items: center; gap: 0.65rem;
    font-family: 'JetBrains Mono', ui-monospace, monospace;
    font-size: 0.75rem;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: rgba(103,232,249,0.95);
    text-shadow: 0 1px 8px rgba(0,0,0,0.6);
  }
  .pod-meta-num { font-weight: 500; }
  .pod-meta-dot {
    width: 3px; height: 3px; border-radius: 50%;
    background: rgba(245,245,247,0.3);
  }
  .pod-meta-time { color: rgba(245,245,247,0.55); }

  .pod-title {
    font-size: clamp(1.75rem, 3.4vw, 2.75rem);
    font-weight: 500;
    letter-spacing: -0.03em;
    line-height: 1.1;
    margin: 0;
    color: #ffffff;
    text-wrap: balance;
    max-width: 22ch;
    text-shadow: 0 2px 16px rgba(0,0,0,0.6);
  }
  .pod-guest {
    display: flex; flex-wrap: wrap; align-items: center; gap: 0.4rem;
    font-size: 0.9375rem;
    text-shadow: 0 1px 8px rgba(0,0,0,0.7);
  }
  .pod-guest-name { color: #f5f5f7; font-weight: 500; }
  .pod-guest-sep { color: rgba(245,245,247,0.3); }
  .pod-guest-title { color: rgba(245,245,247,0.75); }

  .pod-play-big {
    appearance: none; border: none;
    position: absolute;
    top: 50%; left: 50%;
    transform: translate(-50%, -50%);
    z-index: 2;
    width: 104px; height: 104px;
    border-radius: 50%;
    background: #00ffff;
    color: #0a0a0b;
    cursor: pointer;
    display: inline-flex; align-items: center; justify-content: center;
    flex-shrink: 0;
    box-shadow: 0 0 0 1px rgba(0,255,255,0.4), 0 0 40px rgba(0,255,255,0.35);
    transition: transform 0.2s cubic-bezier(0.16, 1, 0.3, 1),
                box-shadow 0.2s cubic-bezier(0.16, 1, 0.3, 1);
  }
  .pod-play-big:hover {
    transform: translate(-50%, -50%) scale(1.05);
    box-shadow: 0 0 0 1px rgba(0,255,255,0.6), 0 0 56px rgba(0,255,255,0.55);
  }
  .pod-play-big svg { transform: translateX(3px); }

  .pod-overlay .wave {
    margin-top: 0.5rem;
    opacity: 0.85;
  }

  .wave {
    display: flex; align-items: center; gap: 3px;
    height: 56px;
    flex: 1;
    min-width: 0;
  }
  .wave-bar {
    flex: 1; min-width: 2px;
    background: linear-gradient(180deg, rgba(103,232,249,0.95), rgba(45,212,191,0.5));
    border-radius: 2px;
  }

  /* WHITE PAPER */
  .wp-hero {
    position: relative;
    border-radius: 1.5rem;
    overflow: hidden;
    background: #060614;
    min-height: 460px;
  }
  .wp-hero-bg {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    z-index: 0;
    display: block;
  }
  .wp-hero::before {
    content: '';
    position: absolute; inset: 0;
    background: radial-gradient(ellipse 70% 60% at 85% 20%, #0e1a3a 0%, #060614 70%);
    z-index: 0;
  }
  .wp-hero-veil {
    position: absolute; inset: 0;
    z-index: 1;
    background:
      linear-gradient(90deg, rgba(10,10,11,0.92) 0%, rgba(10,10,11,0.72) 38%, rgba(10,10,11,0.2) 70%, rgba(10,10,11,0) 100%),
      linear-gradient(180deg, rgba(10,10,11,0) 30%, rgba(10,10,11,0.55) 75%, rgba(10,10,11,0.9) 100%);
    pointer-events: none;
  }
  .wp-hero-inner {
    position: relative; z-index: 2;
    padding: 3.5rem 3rem 3rem;
    display: flex; flex-direction: column;
    gap: 1.25rem;
    max-width: 640px;
  }

  .wp-meta {
    display: inline-flex; align-items: center; gap: 0.65rem;
    font-family: 'JetBrains Mono', ui-monospace, monospace;
    font-size: 0.75rem;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: rgba(103,232,249,0.95);
  }

  .wp-title {
    font-size: clamp(1.75rem, 3.2vw, 2.5rem);
    font-weight: 500;
    letter-spacing: -0.03em;
    line-height: 1.12;
    margin: 0;
    color: #ffffff;
    text-wrap: balance;
  }

  .wp-audience {
    display: flex; gap: 0.85rem;
    padding: 0.85rem 1rem;
    border-radius: 0.6rem;
    border: 1px solid rgba(103,232,249,0.22);
    background: rgba(6, 6, 20, 0.7);
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
    align-items: flex-start;
  }
  .wp-audience-label {
    font-family: 'JetBrains Mono', ui-monospace, monospace;
    font-size: 0.6875rem;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: rgba(103,232,249,0.9);
    white-space: nowrap;
    padding-top: 0.1rem;
    flex-shrink: 0;
  }
  .wp-audience-text {
    font-size: 0.9375rem;
    line-height: 1.55;
    color: rgba(245,245,247,0.85);
    text-wrap: pretty;
  }

  .wp-actions {
    display: flex; flex-wrap: wrap; gap: 0.6rem;
    margin-top: 0.25rem;
  }

  .btn-primary {
    display: inline-flex; align-items: center; justify-content: center;
    padding: 0.7rem 1.5rem;
    border-radius: 9999px;
    background: #00ffff; color: #0a0a0b;
    font-size: 0.875rem; font-weight: 600;
    text-decoration: none;
    box-shadow: 0 0 24px rgba(0,255,255,0.25);
    transition: background 0.2s, box-shadow 0.2s;
  }
  .btn-primary:hover {
    background: #77ffff;
    box-shadow: 0 0 36px rgba(0,255,255,0.5);
  }
  .btn-ghost {
    display: inline-flex; align-items: center; justify-content: center;
    padding: 0.7rem 1.5rem;
    border-radius: 9999px;
    background: transparent;
    border: 1px solid rgba(255,255,255,0.2);
    color: #f5f5f7;
    font-size: 0.875rem; font-weight: 500;
    text-decoration: none;
    transition: background 0.2s, border-color 0.2s;
  }
  .btn-ghost:hover {
    background: rgba(255,255,255,0.06);
    border-color: rgba(255,255,255,0.35);
  }

  /* RESPONSIVE */
  @media (max-width: 720px) {
    .wp-hero-inner { padding: 2.25rem 1.5rem 2rem; gap: 1rem; max-width: none; }
    .wp-hero-veil {
      background:
        linear-gradient(180deg, rgba(10,10,11,0) 10%, rgba(10,10,11,0.55) 45%, rgba(10,10,11,0.92) 100%);
    }
  }
  @media (max-width: 640px) {
    .resources-hero { padding: 4rem 1.25rem 2rem; }
    .resources-main { padding: 0 1.25rem; }
    .pod-overlay { padding: 1.25rem 1.25rem 1.25rem; gap: 0.5rem; }
    .pod-play-big { width: 72px; height: 72px; }
    .pod-overlay .wave { display: none; }
  }
`;
