/**
 * Desktop inline section nav (shown on the full page; collapses to the menu
 * button below the header breakpoint). Active route highlighted with a rounded
 * --card fill, mirroring the menu panel's active row. Data buyers carries a
 * disclosure to its four sub-sections — a click-only popover (predictable,
 * keyboard-safe): Esc, outside-click, and a viewport resize all close it, and
 * selecting an item returns focus to the trigger.
 */

import { useEffect, useRef, useState } from "react";
import type { ArchPath } from "./ArchShell";

const DATA_SUB = [
  { label: "All data buyers", to: "/data" },
  { label: "Reasoning cases", to: "/data#02-1" },
  { label: "Clinical environments", to: "/data#02-2" },
  { label: "Benchmarks", to: "/data#02-3" },
  { label: "Physical AI", to: "/data#02-4" },
];

export function TopNav({
  active,
  onNavigate,
}: {
  active: ArchPath;
  onNavigate: (to: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const dropRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (dropRef.current && !dropRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        triggerRef.current?.focus();
      }
    };
    // Collapsing the header (≤1200px) hides the nav; close so no state/listener
    // is stranded behind a display:none popover.
    const onResize = () => setOpen(false);
    document.addEventListener("mousedown", onDoc);
    window.addEventListener("keydown", onKey);
    window.addEventListener("resize", onResize);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", onResize);
    };
  }, [open]);

  // Top-level items: navigate (the button stays mounted, so focus is fine).
  const go = (to: string) => {
    setOpen(false);
    onNavigate(to);
  };

  // Dropdown items unmount on close — return focus to the trigger, then navigate.
  const goFromDrop = (to: string) => {
    setOpen(false);
    triggerRef.current?.focus();
    onNavigate(to);
  };

  const item = (to: ArchPath, label: string) => (
    <button
      type="button"
      className={`topnav-item${active === to ? " active" : ""}`}
      aria-current={active === to ? "page" : undefined}
      onClick={() => go(to)}
    >
      {label}
    </button>
  );

  return (
    <nav className="topnav" aria-label="Sections">
      {item("/research", "Research")}

      <div className="topnav-drop" ref={dropRef}>
        <button
          ref={triggerRef}
          type="button"
          className={`topnav-item${active === "/data" ? " active" : ""}`}
          aria-expanded={open}
          aria-current={active === "/data" ? "page" : undefined}
          onClick={() => setOpen((o) => !o)}
        >
          Data buyers <span className={`topnav-chev${open ? " openv" : ""}`} aria-hidden="true">⌄</span>
        </button>
        {open && (
          <div className="topnav-menu">
            {DATA_SUB.map((s) => (
              <button
                key={s.to}
                type="button"
                className="topnav-menu-item"
                onClick={() => goFromDrop(s.to)}
              >
                {s.label}
              </button>
            ))}
          </div>
        )}
      </div>

      {item("/health-systems", "Health systems")}
      {item("/physicians", "Physicians & experts")}
      {item("/mission", "Mission")}
    </nav>
  );
}
