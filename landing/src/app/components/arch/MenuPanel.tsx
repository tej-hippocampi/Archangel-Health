/**
 * Full-viewport menu panel (PRD §3). Canvas at ~98% opacity, hairline rows,
 * active route highlighted with a --card fill, Data-buyers accordion, footer
 * strip with Request data / Sign in / contact. Focus-trapped dialog; Esc
 * closes and focus returns to the trigger (the shell handles restore).
 */

import { useEffect, useRef, useState } from "react";
import type { ArchPath } from "./ArchShell";

type Row = {
  path: ArchPath;
  title: string;
  chrome: string;
  comingSoon?: boolean;
  sub?: { label: string; num: string; hash: string }[];
};

const ROWS: Row[] = [
  { path: "/research", title: "Research", chrome: "01 · Research", comingSoon: true },
  {
    path: "/data",
    title: "Data buyers",
    chrome: "02 · Data buyers",
    sub: [
      { label: "Reasoning cases", num: "02.1", hash: "02-1" },
      { label: "Clinical environments", num: "02.2", hash: "02-2" },
      { label: "Benchmarks", num: "02.3", hash: "02-3" },
      { label: "Physical AI", num: "02.4", hash: "02-4" },
    ],
  },
  { path: "/health-systems", title: "Health systems", chrome: "03 · Health systems" },
  { path: "/physicians", title: "Physicians & experts", chrome: "04 · Physicians" },
  { path: "/mission", title: "Mission", chrome: "05 · Mission" },
];

export function MenuPanel({
  active,
  onClose,
  onNavigate,
  onRequestData,
  onSignIn,
  onSignOut,
  signedIn,
  portalUrl,
  portalLabel,
  mail,
}: {
  active: ArchPath;
  onClose: () => void;
  onNavigate: (to: string) => void;
  onRequestData: () => void;
  onSignIn: () => void;
  onSignOut: () => void;
  signedIn: boolean;
  portalUrl: string | null;
  portalLabel: string;
  mail: string;
}) {
  const panelRef = useRef<HTMLDivElement | null>(null);
  const [expanded, setExpanded] = useState<ArchPath | null>(active === "/data" ? "/data" : null);

  /* Esc closes; Tab is trapped inside the panel. */
  useEffect(() => {
    const panel = panelRef.current;
    if (!panel) return;
    // Exclude tabIndex=-1 (collapsed accordion sub-items are kept in the DOM
    // for the height animation but taken out of the tab order) as well as
    // display-hidden nodes.
    const focusables = () =>
      Array.from(panel.querySelectorAll<HTMLElement>("button, a[href]")).filter(
        (el) => el.offsetParent !== null && el.tabIndex >= 0
      );
    focusables()[0]?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
        return;
      }
      if (e.key !== "Tab") return;
      const els = focusables();
      if (!els.length) return;
      const first = els[0];
      const last = els[els.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [onClose]);

  return (
    <div
      ref={panelRef}
      className="menu-overlay menu-open"
      role="dialog"
      aria-modal="true"
      aria-label="Site menu"
    >
      <div className="menu-head">
        <span className="wordmark" aria-hidden="true">
          <svg className="halo" viewBox="0 0 24 24" aria-hidden="true">
            <ellipse cx="12" cy="12" rx="9" ry="5.4" fill="none" stroke="currentColor" strokeWidth="1.7" transform="rotate(-24 12 12)" />
          </svg>
          <span>Archangel&nbsp;Health</span>
        </span>
        <button type="button" className="chrome chrome-box" onClick={onClose}>Close</button>
      </div>

      <nav className="menu-rows" aria-label="Sections">
        {ROWS.map((row, i) => {
          const isActive = active === row.path;
          const isExpanded = expanded === row.path;
          return (
            <div className="menu-row" key={row.path}>
              <button
                type="button"
                className={`menu-row-btn${isActive ? " active" : ""}`}
                style={{ animationDelay: `${i * 28}ms` }}
                aria-current={isActive ? "page" : undefined}
                aria-expanded={row.sub ? isExpanded : undefined}
                onClick={() => {
                  if (row.sub) {
                    setExpanded(isExpanded ? null : row.path);
                  } else {
                    onNavigate(row.path);
                  }
                }}
              >
                <span className="chrome">{row.chrome}</span>
                <span className="menu-row-title">{row.title}</span>
                {row.comingSoon && <span className="chip">Coming soon</span>}
                <span className={`menu-chev${isExpanded ? " openv" : ""}`} aria-hidden="true">
                  {row.sub ? "›" : "→"}
                </span>
              </button>
              {row.sub && (
                <div className={`menu-sub${isExpanded ? " open" : ""}`} aria-hidden={!isExpanded}>
                  <div className="menu-sub-inner">
                    <button
                      type="button"
                      className="menu-sub-item"
                      style={{ animationDelay: "0ms" }}
                      tabIndex={isExpanded ? undefined : -1}
                      onClick={() => onNavigate(row.path)}
                    >
                      <span className="chrome">02.0</span>
                      All data buyers
                    </button>
                    {row.sub.map((s, j) => (
                      <button
                        key={s.hash}
                        type="button"
                        className="menu-sub-item"
                        style={{ animationDelay: `${(j + 1) * 20}ms` }}
                        tabIndex={isExpanded ? undefined : -1}
                        onClick={() => onNavigate(`${row.path}#${s.hash}`)}
                      >
                        <span className="chrome">{s.num}</span>
                        {s.label}
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </nav>

      <div className="menu-foot">
        <button type="button" className="btn btn-primary" onClick={onRequestData}>Request data</button>
        {signedIn ? (
          <>
            {portalUrl && (
              <a className="chrome chrome-box" href={portalUrl}>{portalLabel}</a>
            )}
            <button type="button" className="chrome chrome-box" onClick={onSignOut}>Sign out</button>
          </>
        ) : (
          <button type="button" className="chrome chrome-box" onClick={onSignIn}>Sign in</button>
        )}
        <span className="spacer" />
        <a className="menu-mail" href={`mailto:${mail}`}>{mail}</a>
      </div>
    </div>
  );
}
