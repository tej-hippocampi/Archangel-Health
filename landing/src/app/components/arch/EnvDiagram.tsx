/**
 * 02.2 signature animation (PRD §5) — the clinical RL environment. Case node
 * at left, time flows right, stakeholder lanes (patient · payer · care team);
 * the agent's decision path threads between lanes as the sequence plays.
 * Accent semantics: green = resolved/verified · orange = model action/probe ·
 * pink = flag/denial. Scrubbable; pauses on hover and off-screen; freezes to
 * the composed final state under prefers-reduced-motion. Vertical list at
 * narrow widths.
 */

import { useEffect, useMemo, useRef, useState } from "react";

const LANES = [
  { label: "Patient", y: 84 },
  { label: "Payer", y: 168 },
  { label: "Care team", y: 252 },
];

type Dot = "green" | "orange" | "pink";

const EVENTS: { label: string; x: number; y: number; kind: Dot }[] = [
  { label: "Order labs", x: 190, y: 252, kind: "orange" },
  { label: "Results return", x: 282, y: 252, kind: "green" },
  { label: "Dx revised", x: 374, y: 252, kind: "orange" },
  { label: "Plan submitted", x: 466, y: 168, kind: "orange" },
  { label: "Payer denies", x: 558, y: 168, kind: "pink" },
  { label: "Plan revised", x: 650, y: 252, kind: "orange" },
  { label: "Follow-up", x: 742, y: 84, kind: "orange" },
  { label: "Outcome resolves", x: 848, y: 84, kind: "green" },
];

const START = { x: 84, y: 168 };
const COLORS: Record<Dot, string> = { green: "#4ca63c", orange: "#ec9440", pink: "#e8447b" };
const DURATION_MS = 12000;

export function EnvDiagram() {
  const pathRef = useRef<SVGPathElement | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const [t, setT] = useState(0);
  const [len, setLen] = useState(1);
  const playing = useRef(true);
  const hovering = useRef(false);
  const visible = useRef(false);
  const tRef = useRef(0);
  const reduced = useMemo(
    () => typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches,
    []
  );

  const d = useMemo(() => {
    let s = `M ${START.x} ${START.y}`;
    let prev = START;
    for (const ev of EVENTS) {
      const mx = (prev.x + ev.x) / 2;
      s += ` C ${mx} ${prev.y}, ${mx} ${ev.y}, ${ev.x} ${ev.y}`;
      prev = ev;
    }
    return s;
  }, []);

  useEffect(() => {
    if (pathRef.current) setLen(pathRef.current.getTotalLength());
  }, [d]);

  useEffect(() => {
    tRef.current = t;
  }, [t]);

  useEffect(() => {
    if (reduced) {
      setT(1);
      return;
    }
    const wrap = wrapRef.current;
    let io: IntersectionObserver | null = null;
    if (wrap && "IntersectionObserver" in window) {
      io = new IntersectionObserver((entries) => {
        visible.current = entries.some((e) => e.isIntersecting);
      }, { threshold: 0.2 });
      io.observe(wrap);
    } else {
      visible.current = true;
    }

    let raf = 0;
    let last = performance.now();
    const tick = (now: number) => {
      const dt = now - last;
      last = now;
      if (playing.current && visible.current && !hovering.current && !document.hidden) {
        let next = tRef.current + dt / DURATION_MS;
        if (next > 1.12) next = 0; // brief hold on the composed end state, then loop
        tRef.current = next;
        setT(next);
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(raf);
      io?.disconnect();
    };
  }, [reduced]);

  const clamped = Math.min(1, t);
  const fractionFor = (i: number) => (i + 1) / EVENTS.length;

  return (
    <div
      ref={wrapRef}
      className="c-card env-card"
      onMouseEnter={() => (hovering.current = true)}
      onMouseLeave={() => (hovering.current = false)}
    >
      <span className="label">Clinical environment — one episode</span>

      <div className="env-svg-wrap">
        <svg
          className="env-svg"
          viewBox="0 0 900 320"
          role="img"
          aria-label="A medical agent's episode over time: it orders labs, results return, the diagnosis is revised, a plan is submitted, the payer denies it, the plan is revised, follow-up happens, and the outcome resolves — across patient, payer, and care-team lanes."
        >
          {LANES.map((lane) => (
            <g key={lane.label}>
              <line className="env-lane-line" x1="130" y1={lane.y} x2="880" y2={lane.y} />
              <text className="env-lane-label" x="16" y={lane.y + 3}>{lane.label}</text>
            </g>
          ))}

          <path
            ref={pathRef}
            className="env-path"
            d={d}
            strokeDasharray={len}
            strokeDashoffset={len * (1 - clamped)}
          />

          {/* case node */}
          <circle cx={START.x} cy={START.y} r="7" fill="#fbfcfa" stroke="#1a1b1a" strokeWidth="1.2" />
          <text className="env-node-label" x={START.x} y={START.y - 14} textAnchor="middle">Case</text>

          {EVENTS.map((ev, i) => {
            const on = clamped >= fractionFor(i) - 0.001;
            const labelY = ev.y === 252 ? ev.y + 22 : ev.y - 14;
            return (
              <g key={ev.label}>
                <circle
                  className={`env-node${on ? "" : " dim"}`}
                  cx={ev.x}
                  cy={ev.y}
                  r="5.5"
                  fill={on ? COLORS[ev.kind] : "#f4f5f3"}
                  stroke={on ? COLORS[ev.kind] : "rgba(26,27,26,0.25)"}
                />
                <text
                  className={`env-node-label${on ? "" : " dim"}`}
                  x={ev.x}
                  y={labelY}
                  textAnchor="middle"
                >
                  {ev.label}
                </text>
              </g>
            );
          })}
        </svg>
      </div>

      {!reduced && (
        <div className="env-scrub">
          <span className="chrome">Scrub</span>
          <input
            className="env-range"
            type="range"
            min={0}
            max={1000}
            value={Math.round(clamped * 1000)}
            aria-label="Scrub through the episode"
            onPointerDown={() => (playing.current = false)}
            onPointerUp={() => (playing.current = true)}
            onChange={(e) => {
              const v = Number(e.target.value) / 1000;
              tRef.current = v;
              setT(v);
            }}
          />
        </div>
      )}

      {/* vertical sequence at narrow widths */}
      <div className="env-steps-mobile" aria-hidden="true">
        {EVENTS.map((ev) => (
          <span className="env-step-m" key={ev.label}>
            <span className="dot" style={{ background: COLORS[ev.kind] }} />
            {ev.label}
          </span>
        ))}
      </div>
    </div>
  );
}
