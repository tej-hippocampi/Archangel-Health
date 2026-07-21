/**
 * Archangel Health — onboarding primitives (console design system).
 *
 * Light "console" treatment shared with the landing page and product
 * surfaces: canvas/card neutrals, ink type, the four semantic accents
 * (green = verified, orange = model, pink = flag, lime = highlight),
 * IBM Plex Mono chrome labels for wayfinding.
 *
 * Each primitive is the smallest piece needed to assemble the step screens.
 * They share no state with each other. All colors reference the CSS custom
 * properties declared on .ah-onb-root (OnboardingStyles.tsx) — no raw hex.
 */

import {
  forwardRef,
  useCallback,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type InputHTMLAttributes,
  type KeyboardEvent,
  type MouseEvent,
  type ReactNode,
} from "react";

/* Shared chrome-label style — the wayfinding primitive. */
const CHROME: CSSProperties = {
  fontFamily: "var(--mono)",
  fontSize: 11,
  fontWeight: 400,
  letterSpacing: "0.08em",
  textTransform: "uppercase",
};

/* Focus ring simulated for inline-styled shells: card gap + ink ring. */
const FOCUS_RING = "0 0 0 2px var(--card), 0 0 0 4px var(--ink)";

/* ─────────────────────────────────────────────────────────────
   Brandmark — green halo dot + wordmark, as on the landing nav.
   ───────────────────────────────────────────────────────────── */

type BrandmarkSize = "sm" | "md" | "lg";

const BRAND_SIZES: Record<BrandmarkSize, { dot: number; gap: number; word: number }> = {
  sm: { dot: 7, gap: 8, word: 14 },
  md: { dot: 8, gap: 10, word: 16 },
  lg: { dot: 9, gap: 12, word: 18 },
};

export function Brandmark({ size = "md" }: { size?: BrandmarkSize }) {
  const s = BRAND_SIZES[size];
  return (
    <div style={{ display: "inline-flex", alignItems: "center", gap: s.gap }}>
      <span
        style={{
          width: s.dot,
          height: s.dot,
          borderRadius: "50%",
          background: "var(--green)",
          flexShrink: 0,
        }}
        aria-hidden="true"
      />
      <span
        style={{
          fontFamily: "var(--sans)",
          fontSize: s.word,
          fontWeight: 500,
          letterSpacing: "-0.01em",
          color: "var(--ink)",
        }}
      >
        Archangel Health
      </span>
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────
   ChromeHeader — sticky top bar with brandmark + help/exit.
   ───────────────────────────────────────────────────────────── */

export function ChromeHeader({ onExit, helpHref = "mailto:tejpatel@archangelhealth.ai" }: {
  onExit?: () => void;
  helpHref?: string;
}) {
  return (
    <header
      style={{
        position: "sticky",
        top: 0,
        zIndex: 50,
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        padding: "16px 32px",
        background: "rgba(238, 240, 239, 0.72)",
        backdropFilter: "blur(22px) saturate(1.5)",
        WebkitBackdropFilter: "blur(22px) saturate(1.5)",
        borderBottom: "1px solid var(--hairline)",
      }}
    >
      <Brandmark size="md" />
      <div style={{ display: "flex", alignItems: "center", gap: 18 }}>
        <a
          href={helpHref}
          style={{
            fontSize: 13,
            color: "var(--ink-soft)",
            textDecoration: "none",
            fontWeight: 500,
          }}
        >
          Need help?
        </a>
        <button
          onClick={onExit}
          type="button"
          style={{
            fontSize: 13,
            fontWeight: 500,
            color: "var(--ink-soft)",
            background: "transparent",
            border: "1px solid var(--hairline-strong)",
            padding: "8px 16px",
            borderRadius: 9999,
          }}
        >
          Save & exit
        </button>
      </div>
    </header>
  );
}

/* ─────────────────────────────────────────────────────────────
   Stepper — numbered chips on a hairline rail; green = done.
   ───────────────────────────────────────────────────────────── */

export function Stepper({ steps, currentIndex }: { steps: string[]; currentIndex: number }) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        gap: 0,
        marginBottom: 56,
        flexWrap: "wrap",
      }}
    >
      {steps.map((label, i) => {
        const done = i < currentIndex;
        const active = i === currentIndex;
        return (
          <div key={label} style={{ display: "contents" }}>
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 10,
                opacity: done || active ? 1 : 0.55,
                transition: "opacity 400ms cubic-bezier(0.16, 1, 0.3, 1)",
              }}
            >
              <div
                style={{
                  width: 26,
                  height: 26,
                  borderRadius: "50%",
                  background: done ? "var(--green)" : "var(--card)",
                  border: active
                    ? "1.5px solid var(--ink)"
                    : done
                      ? "1.5px solid var(--green)"
                      : "1px solid var(--hairline-strong)",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  fontSize: 12,
                  fontWeight: 500,
                  color: done ? "var(--card)" : active ? "var(--ink)" : "var(--ink-faint)",
                  fontFamily: "var(--mono)",
                  transition: "all 400ms cubic-bezier(0.16, 1, 0.3, 1)",
                }}
              >
                {done ? (
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
                    <polyline points="20 6 9 17 4 12" />
                  </svg>
                ) : (
                  i + 1
                )}
              </div>
              <span
                style={{
                  ...CHROME,
                  fontSize: 11,
                  color: active ? "var(--ink)" : done ? "var(--ink-soft)" : "var(--ink-faint)",
                  whiteSpace: "nowrap",
                }}
              >
                {label}
              </span>
            </div>
            {i < steps.length - 1 && (
              <div
                style={{
                  width: 38,
                  height: 1,
                  margin: "0 14px",
                  background: done ? "var(--ah-green-line)" : "var(--hairline)",
                  transition: "background 400ms cubic-bezier(0.16, 1, 0.3, 1)",
                }}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────
   OnboardingCard — chrome eyebrow + display title + lede + card.
   ───────────────────────────────────────────────────────────── */

export function OnboardingCard({
  eyebrow,
  title,
  lede,
  children,
  footer,
  maxWidth = 560,
}: {
  eyebrow?: ReactNode;
  title: ReactNode;
  lede?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  maxWidth?: number;
}) {
  return (
    <div
      style={{
        width: "100%",
        maxWidth,
        margin: "0 auto",
        animation: "ah-onb-fade-up 480ms cubic-bezier(0.16, 1, 0.3, 1)",
      }}
    >
      {eyebrow && (
        <div
          style={{
            ...CHROME,
            color: "var(--ink-faint)",
            textAlign: "center",
            marginBottom: 18,
          }}
        >
          {eyebrow}
        </div>
      )}
      <h1
        style={{
          fontFamily: "var(--sans)",
          fontSize: "clamp(30px, 4.4vw, 42px)",
          fontWeight: 400,
          lineHeight: 1.12,
          letterSpacing: "-0.015em",
          textAlign: "center",
          color: "var(--ink)",
          marginTop: 0,
          marginBottom: lede ? 16 : 32,
        }}
      >
        {title}
      </h1>
      {lede && (
        <p
          style={{
            fontSize: 15,
            lineHeight: 1.55,
            color: "var(--ink-soft)",
            textAlign: "center",
            maxWidth: 520,
            margin: "0 auto 36px",
          }}
        >
          {lede}
        </p>
      )}

      <div
        style={{
          background: "var(--card)",
          border: "1px solid var(--hairline)",
          borderRadius: 20,
          padding: "32px 36px",
          boxShadow: "var(--shadow-card)",
        }}
      >
        {children}
      </div>

      {footer && (
        <div style={{ marginTop: 24, textAlign: "center", fontSize: 13, color: "var(--ink-faint)" }}>
          {footer}
        </div>
      )}
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────
   FieldLabel — chrome mini-label inside cards.
   ───────────────────────────────────────────────────────────── */

export function FieldLabel({ children, optional }: { children: ReactNode; optional?: boolean }) {
  return (
    <div
      style={{
        display: "block",
        ...CHROME,
        color: "var(--ink-soft)",
        marginBottom: 10,
      }}
    >
      {children}
      {optional && (
        <span
          style={{
            color: "var(--ink-faint)",
            fontFamily: "var(--sans)",
            fontWeight: 400,
            marginLeft: 6,
            textTransform: "none",
            letterSpacing: 0,
          }}
        >
          {" "}
          Optional
        </span>
      )}
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────
   TextField — card-in input shell, ink focus ring.
   ───────────────────────────────────────────────────────────── */

type TextFieldProps = Omit<InputHTMLAttributes<HTMLInputElement>, "onChange" | "value" | "type"> & {
  label?: ReactNode;
  value: string;
  onChange?: (next: string) => void;
  placeholder?: string;
  type?: string;
  autoFocus?: boolean;
  optional?: boolean;
  hint?: ReactNode;
  prefix?: ReactNode;
  suffix?: ReactNode;
  error?: ReactNode;
};

export function TextField({
  label,
  value,
  onChange,
  placeholder,
  type = "text",
  autoFocus,
  optional,
  hint,
  prefix,
  suffix,
  error,
  ...rest
}: TextFieldProps) {
  const [focused, setFocused] = useState(false);
  return (
    <div style={{ marginBottom: 20 }}>
      {label && <FieldLabel optional={optional}>{label}</FieldLabel>}
      <div
        style={{
          position: "relative",
          display: "flex",
          alignItems: "center",
          background: "var(--card-in)",
          border:
            "1px solid " +
            (error ? "var(--ah-pink-line)" : focused ? "var(--hairline-strong)" : "var(--hairline)"),
          borderRadius: 10,
          padding: "0 16px",
          transition: "border-color 160ms cubic-bezier(.4,0,.2,1), box-shadow 160ms cubic-bezier(.4,0,.2,1)",
          boxShadow: focused ? FOCUS_RING : "none",
        }}
      >
        {prefix && (
          <span style={{ color: "var(--ink-faint)", marginRight: 10, fontSize: 14 }}>{prefix}</span>
        )}
        <input
          {...rest}
          type={type}
          value={value}
          onChange={(e) => onChange?.(e.target.value)}
          placeholder={placeholder}
          autoFocus={autoFocus}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
          style={{
            flex: 1,
            background: "transparent",
            border: "none",
            outline: "none",
            color: "var(--ink)",
            fontSize: 15,
            fontWeight: 400,
            padding: "13px 0",
            fontFamily: "inherit",
            minWidth: 0,
          }}
        />
        {suffix}
      </div>
      {hint && !error && (
        <div style={{ fontSize: 12, color: "var(--ink-soft)", marginTop: 8, paddingLeft: 4 }}>{hint}</div>
      )}
      {error && (
        <div
          style={{
            display: "flex",
            alignItems: "baseline",
            gap: 7,
            fontSize: 12,
            color: "var(--ah-pink-deep)",
            marginTop: 8,
            paddingLeft: 4,
          }}
        >
          <span
            style={{ width: 5, height: 5, borderRadius: "50%", background: "var(--pink)", flexShrink: 0 }}
            aria-hidden="true"
          />
          {error}
        </div>
      )}
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────
   SelectField — same shell as TextField, with chevron.
   ───────────────────────────────────────────────────────────── */

export function SelectField({
  label,
  value,
  onChange,
  options,
  placeholder,
}: {
  label?: ReactNode;
  value: string;
  onChange?: (next: string) => void;
  options: { value: string; label: string; disabled?: boolean }[];
  placeholder?: string;
}) {
  const [focused, setFocused] = useState(false);
  return (
    <div style={{ marginBottom: 20 }}>
      {label && <FieldLabel>{label}</FieldLabel>}
      <div
        style={{
          position: "relative",
          background: "var(--card-in)",
          border: "1px solid " + (focused ? "var(--hairline-strong)" : "var(--hairline)"),
          borderRadius: 10,
          padding: "0 16px",
          transition: "border-color 160ms cubic-bezier(.4,0,.2,1), box-shadow 160ms cubic-bezier(.4,0,.2,1)",
          boxShadow: focused ? FOCUS_RING : "none",
        }}
      >
        <select
          value={value}
          onChange={(e) => onChange?.(e.target.value)}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
          style={{
            width: "100%",
            background: "transparent",
            border: "none",
            outline: "none",
            color: value ? "var(--ink)" : "var(--ink-faint)",
            fontSize: 15,
            padding: "13px 0",
            fontFamily: "inherit",
            appearance: "none",
            WebkitAppearance: "none",
            paddingRight: 32,
          }}
        >
          <option value="" disabled>
            {placeholder || "Select…"}
          </option>
          {options.map((opt) => (
            <option
              key={opt.value}
              value={opt.value}
              disabled={opt.disabled}
              style={{ background: "var(--card)", color: "var(--ink)" }}
            >
              {opt.label}
            </option>
          ))}
        </select>
        <svg
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          style={{
            position: "absolute",
            right: 16,
            top: "50%",
            transform: "translateY(-50%)",
            color: "var(--ink-faint)",
            pointerEvents: "none",
          }}
        >
          <polyline points="6 9 12 15 18 9" />
        </svg>
      </div>
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────
   TextArea — multi-line sibling of TextField (same shell).
   ───────────────────────────────────────────────────────────── */

export function TextArea({
  label,
  value,
  onChange,
  placeholder,
  optional,
  hint,
  rows = 3,
}: {
  label?: ReactNode;
  value: string;
  onChange?: (next: string) => void;
  placeholder?: string;
  optional?: boolean;
  hint?: ReactNode;
  rows?: number;
}) {
  const [focused, setFocused] = useState(false);
  return (
    <div style={{ marginBottom: 20 }}>
      {label && <FieldLabel optional={optional}>{label}</FieldLabel>}
      <div
        style={{
          background: "var(--card-in)",
          border: "1px solid " + (focused ? "var(--hairline-strong)" : "var(--hairline)"),
          borderRadius: 10,
          padding: "2px 16px",
          transition: "border-color 160ms cubic-bezier(.4,0,.2,1), box-shadow 160ms cubic-bezier(.4,0,.2,1)",
          boxShadow: focused ? FOCUS_RING : "none",
        }}
      >
        <textarea
          value={value}
          onChange={(e) => onChange?.(e.target.value)}
          placeholder={placeholder}
          rows={rows}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
          style={{
            width: "100%",
            background: "transparent",
            border: "none",
            outline: "none",
            color: "var(--ink)",
            fontSize: 15,
            lineHeight: 1.6,
            padding: "12px 0",
            fontFamily: "inherit",
            resize: "vertical",
            minHeight: 24 * rows,
          }}
        />
      </div>
      {hint && (
        <div style={{ fontSize: 12, color: "var(--ink-soft)", marginTop: 8, paddingLeft: 4 }}>{hint}</div>
      )}
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────
   ChipMultiSelect — add-as-you-type tag input + suggested chips.
   Caller owns the string[] value.
   ───────────────────────────────────────────────────────────── */

export function ChipMultiSelect({
  label,
  value,
  onChange,
  placeholder,
  suggestions = [],
  optional,
  hint,
}: {
  label?: ReactNode;
  value: string[];
  onChange: (next: string[]) => void;
  placeholder?: string;
  suggestions?: string[];
  optional?: boolean;
  hint?: ReactNode;
}) {
  const [draft, setDraft] = useState("");
  const [focused, setFocused] = useState(false);

  const add = (raw: string) => {
    const v = raw.trim();
    if (!v) return;
    if (value.some((x) => x.toLowerCase() === v.toLowerCase())) {
      setDraft("");
      return;
    }
    onChange([...value, v]);
    setDraft("");
  };
  const remove = (v: string) => onChange(value.filter((x) => x !== v));

  const openSuggestions = suggestions.filter(
    (s) => !value.some((v) => v.toLowerCase() === s.toLowerCase()),
  );

  return (
    <div style={{ marginBottom: 20 }}>
      {label && <FieldLabel optional={optional}>{label}</FieldLabel>}
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          alignItems: "center",
          gap: 8,
          background: "var(--card-in)",
          border: "1px solid " + (focused ? "var(--hairline-strong)" : "var(--hairline)"),
          borderRadius: 10,
          padding: "10px 12px",
          transition: "border-color 160ms cubic-bezier(.4,0,.2,1), box-shadow 160ms cubic-bezier(.4,0,.2,1)",
          boxShadow: focused ? FOCUS_RING : "none",
        }}
      >
        {value.map((v) => (
          <span
            key={v}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 7,
              height: 28,
              padding: "0 6px 0 11px",
              borderRadius: 9999,
              background: "var(--card)",
              border: "1px solid var(--hairline-strong)",
              color: "var(--ink)",
              fontSize: 13,
              fontWeight: 500,
            }}
          >
            {v}
            <button
              type="button"
              onClick={() => remove(v)}
              aria-label={`Remove ${v}`}
              style={{
                width: 18,
                height: 18,
                borderRadius: "50%",
                background: "var(--card-in)",
                border: "none",
                color: "var(--ink-soft)",
                cursor: "pointer",
                display: "inline-flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: 14,
                lineHeight: 1,
              }}
            >
              ×
            </button>
          </span>
        ))}
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onFocus={() => setFocused(true)}
          onBlur={() => {
            setFocused(false);
            add(draft);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === ",") {
              e.preventDefault();
              add(draft);
            } else if (e.key === "Backspace" && !draft && value.length) {
              remove(value[value.length - 1]);
            }
          }}
          placeholder={value.length === 0 ? placeholder : "Add another…"}
          style={{
            flex: "1 1 120px",
            minWidth: 120,
            background: "transparent",
            border: "none",
            outline: "none",
            color: "var(--ink)",
            fontSize: 15,
            padding: "5px 0",
            fontFamily: "inherit",
          }}
        />
      </div>
      {openSuggestions.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 7, marginTop: 10 }}>
          {openSuggestions.map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => add(s)}
              style={{
                height: 26,
                padding: "0 11px",
                borderRadius: 9999,
                background: "transparent",
                border: "1px dashed var(--hairline-strong)",
                color: "var(--ink-soft)",
                fontSize: 12.5,
                cursor: "pointer",
              }}
            >
              + {s}
            </button>
          ))}
        </div>
      )}
      {hint && (
        <div style={{ fontSize: 12, color: "var(--ink-soft)", marginTop: 8, paddingLeft: 4 }}>{hint}</div>
      )}
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────
   YesNoToggle — two-segment control; lime = active selection.
   ───────────────────────────────────────────────────────────── */

export function YesNoToggle({
  label,
  value,
  onChange,
}: {
  label?: ReactNode;
  value: boolean | null;
  onChange: (next: boolean) => void;
}) {
  return (
    <div style={{ marginBottom: 20 }}>
      {label && <FieldLabel>{label}</FieldLabel>}
      <div style={{ display: "flex", gap: 10 }}>
        {[
          { v: true, label: "Yes" },
          { v: false, label: "No" },
        ].map((opt) => {
          const active = value === opt.v;
          return (
            <button
              key={opt.label}
              type="button"
              onClick={() => onChange(opt.v)}
              aria-pressed={active}
              style={{
                flex: 1,
                padding: "12px 0",
                borderRadius: 10,
                background: active ? "var(--ah-lime-wash)" : "var(--card-in)",
                border: "1px solid " + (active ? "var(--ah-lime-line)" : "var(--hairline)"),
                color: active ? "var(--ink)" : "var(--ink-soft)",
                fontSize: 14,
                fontWeight: 500,
                cursor: "pointer",
                transition: "all 160ms cubic-bezier(.4,0,.2,1)",
              }}
            >
              {opt.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────
   ProductOption — large selectable card for the product choice.
   Selection is confirmed with a green check (verified semantics).
   ───────────────────────────────────────────────────────────── */

export function ProductOption({
  title,
  tagline,
  description,
  badges,
  icon,
  selected,
  onSelect,
}: {
  title: string;
  tagline: string;
  description: ReactNode;
  badges?: string[];
  icon: ReactNode;
  selected: boolean;
  onSelect: () => void;
}) {
  const [hover, setHover] = useState(false);
  const lit = selected || hover;
  return (
    <button
      type="button"
      onClick={onSelect}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      aria-pressed={selected}
      style={{
        textAlign: "left",
        display: "flex",
        flexDirection: "column",
        gap: 14,
        padding: "24px 24px",
        borderRadius: 14,
        background: selected ? "var(--card)" : "var(--card-in)",
        border: "1px solid " + (selected ? "rgba(26, 27, 26, 0.55)" : lit ? "var(--hairline-strong)" : "var(--hairline)"),
        boxShadow: selected ? "var(--shadow-card)" : "none",
        cursor: "pointer",
        transition: "background 240ms cubic-bezier(.4,0,.2,1), border-color 240ms cubic-bezier(.4,0,.2,1)",
        height: "100%",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <div
          style={{
            width: 44,
            height: 44,
            borderRadius: 12,
            background: "var(--card)",
            border: "1px solid var(--hairline)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            color: "var(--ink)",
          }}
        >
          {icon}
        </div>
        <span
          style={{
            width: 22,
            height: 22,
            borderRadius: "50%",
            border: "1.5px solid " + (selected ? "var(--green)" : "var(--hairline-strong)"),
            background: selected ? "var(--green)" : "transparent",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            transition: "all 160ms cubic-bezier(.4,0,.2,1)",
          }}
        >
          {selected && (
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="var(--card)" strokeWidth="3.5" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="20 6 9 17 4 12" />
            </svg>
          )}
        </span>
      </div>
      <div>
        <div
          style={{
            fontFamily: "var(--sans)",
            fontSize: 21,
            fontWeight: 500,
            letterSpacing: "-0.01em",
            color: "var(--ink)",
          }}
        >
          {title}
        </div>
        <div style={{ ...CHROME, color: "var(--ink-faint)", marginTop: 6 }}>
          {tagline}
        </div>
      </div>
      <div style={{ fontSize: 14, lineHeight: 1.55, color: "var(--ink-soft)" }}>{description}</div>
      {badges && badges.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 7, marginTop: "auto", paddingTop: 6 }}>
          {badges.map((b) => (
            <span
              key={b}
              style={{
                height: 24,
                padding: "0 10px",
                borderRadius: 9999,
                background: "var(--card)",
                border: "1px solid var(--hairline)",
                color: "var(--ink-soft)",
                fontSize: 11.5,
                fontWeight: 500,
                display: "inline-flex",
                alignItems: "center",
              }}
            >
              {b}
            </span>
          ))}
        </div>
      )}
    </button>
  );
}

/* ─────────────────────────────────────────────────────────────
   PrimaryButton — idle → loading → success → reset lifecycle.
   Ink-filled pill; success turns green (verified semantics).
   The handler may return a Promise; resolving `false` keeps it
   Idle (used to signal a server error without entering Success).
   ───────────────────────────────────────────────────────────── */

type ButtonVariant = "primary" | "secondary";
type ButtonState = "idle" | "loading" | "success";

const BUTTON_STYLES: Record<ButtonVariant, Record<ButtonState, { bg: string; fg: string; border: string }>> = {
  primary: {
    idle: { bg: "var(--ink)", fg: "var(--card)", border: "var(--ink)" },
    loading: { bg: "var(--ink-hover)", fg: "var(--card)", border: "var(--ink-hover)" },
    success: { bg: "var(--green)", fg: "var(--card)", border: "var(--green)" },
  },
  secondary: {
    idle: { bg: "transparent", fg: "var(--ink)", border: "var(--hairline-strong)" },
    loading: { bg: "var(--card-in)", fg: "var(--ink)", border: "var(--hairline-strong)" },
    success: { bg: "var(--ah-green-wash)", fg: "var(--ah-green-deep)", border: "var(--ah-green-line)" },
  },
};

export type PrimaryButtonProps = {
  children: ReactNode;
  onClick?: (e: MouseEvent<HTMLButtonElement>) => void | boolean | Promise<void | boolean>;
  disabled?: boolean;
  loadingLabel?: ReactNode;
  successLabel?: ReactNode;
  asyncMs?: number;
  holdMs?: number;
  fullWidth?: boolean;
  variant?: ButtonVariant;
  icon?: ReactNode;
  type?: "button" | "submit";
};

export function PrimaryButton({
  children,
  onClick,
  disabled,
  loadingLabel,
  successLabel,
  asyncMs = 900,
  holdMs = 700,
  fullWidth,
  variant = "primary",
  icon,
  type = "button",
}: PrimaryButtonProps) {
  const [state, setState] = useState<ButtonState>("idle");
  const timeoutRef = useRef<number[]>([]);

  useEffect(
    () => () => {
      timeoutRef.current.forEach((id) => window.clearTimeout(id));
      timeoutRef.current = [];
    },
    [],
  );

  const handleClick = useCallback(
    async (e: MouseEvent<HTMLButtonElement>) => {
      if (disabled || state !== "idle") return;
      setState("loading");
      let advance = true;
      try {
        const result = onClick?.(e);
        if (result instanceof Promise) {
          const v = await result;
          advance = v !== false;
        } else {
          await new Promise<void>((r) => {
            const id = window.setTimeout(r, asyncMs);
            timeoutRef.current.push(id);
          });
          advance = result !== false;
        }
      } catch {
        advance = false;
      }
      if (!advance) {
        setState("idle");
        return;
      }
      setState("success");
      const id = window.setTimeout(() => setState("idle"), holdMs);
      timeoutRef.current.push(id);
    },
    [onClick, disabled, state, asyncMs, holdMs],
  );

  const s = BUTTON_STYLES[variant][state];

  return (
    <button
      type={type}
      onClick={handleClick}
      disabled={disabled || state !== "idle"}
      style={{
        width: fullWidth ? "100%" : "auto",
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        gap: 10,
        padding: "14px 28px",
        borderRadius: 9999,
        background: s.bg,
        color: s.fg,
        border: "1px solid " + s.border,
        fontSize: 15,
        fontWeight: 500,
        letterSpacing: "-0.005em",
        fontFamily: "inherit",
        opacity: disabled ? 0.45 : 1,
        cursor:
          disabled || state !== "idle" ? (state === "loading" ? "progress" : "default") : "pointer",
        transition: "background 160ms cubic-bezier(.4,0,.2,1), border-color 160ms cubic-bezier(.4,0,.2,1), color 160ms cubic-bezier(.4,0,.2,1)",
      }}
    >
      {state === "idle" && (
        <>
          {icon}
          <span>{children}</span>
        </>
      )}
      {state === "loading" && (
        <>
          <Spinner color={variant === "primary" ? "var(--card)" : "var(--ink)"} />
          <span>{loadingLabel ?? "Working…"}</span>
        </>
      )}
      {state === "success" && (
        <>
          <CheckTick color={variant === "primary" ? "var(--card)" : "var(--ah-green-deep)"} />
          <span>{successLabel ?? "Done"}</span>
        </>
      )}
    </button>
  );
}

export function Spinner({ color = "var(--ink)" }: { color?: string }) {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" style={{ animation: "ah-onb-spin 0.8s linear infinite" }}>
      <circle cx="12" cy="12" r="10" stroke={color} strokeWidth="2.5" strokeLinecap="round" strokeOpacity="0.25" />
      <path d="M22 12 A10 10 0 0 0 12 2" stroke={color} strokeWidth="2.5" strokeLinecap="round" />
    </svg>
  );
}

export function CheckTick({ color = "var(--ink)" }: { color?: string }) {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
      <polyline
        points="20 6 9 17 4 12"
        stroke={color}
        strokeWidth="3"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeDasharray="24"
        strokeDashoffset="0"
        style={{ animation: "ah-onb-tick-in 380ms cubic-bezier(0.16, 1, 0.3, 1)" }}
      />
    </svg>
  );
}

/* ─────────────────────────────────────────────────────────────
   BackLink — quiet "← Back" link.
   ───────────────────────────────────────────────────────────── */

export function BackLink({ onClick }: { onClick?: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 8,
        background: "transparent",
        border: "none",
        color: "var(--ink-soft)",
        fontSize: 13,
        fontWeight: 500,
        padding: "8px 0",
        fontFamily: "inherit",
      }}
    >
      <svg
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <line x1="19" y1="12" x2="5" y2="12" />
        <polyline points="12 19 5 12 12 5" />
      </svg>
      Back
    </button>
  );
}

/* ─────────────────────────────────────────────────────────────
   Avatar — initials circle on card-in; the signed-in director
   ("you") carries the green ring (credentialed semantics).
   ───────────────────────────────────────────────────────────── */

export function Avatar({
  name,
  email,
  size = 40,
  you = false,
}: {
  name?: string;
  email?: string;
  size?: number;
  you?: boolean;
}) {
  const seedSrc = (name || email || "?").trim();
  const initials =
    seedSrc
      .split(/\s+/)
      .slice(0, 2)
      .map((w) => w[0]?.toUpperCase() ?? "")
      .join("") || "?";
  return (
    <div
      style={{
        width: size,
        height: size,
        borderRadius: "50%",
        flexShrink: 0,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: you ? "var(--green)" : "var(--card-in)",
        border: you ? "none" : "1px solid var(--hairline)",
        color: you ? "var(--card)" : "var(--ink-soft)",
        fontSize: size * 0.34,
        fontWeight: 500,
        letterSpacing: "0.01em",
      }}
    >
      {initials}
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────
   RolePill / StatusPill — capsule chips for member rows.
   Neutral chips; status carried by a semantic dot + label
   (never color alone).
   ───────────────────────────────────────────────────────────── */

export function RolePill({ role }: { role: string }) {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        height: 22,
        padding: "0 9px",
        borderRadius: 9999,
        background: "var(--card)",
        border: "1px solid var(--hairline)",
        color: "var(--ink-soft)",
        ...CHROME,
        fontSize: 10.5,
        whiteSpace: "nowrap",
      }}
    >
      {role}
    </span>
  );
}

const STATUS_DOTS: Record<string, string> = {
  Invited: "var(--lime)",
  Active: "var(--green)",
  You: "var(--green)",
};

export function StatusPill({ status }: { status: "Invited" | "Active" | "You" }) {
  const isYou = status === "You";
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        height: 22,
        padding: "0 10px",
        borderRadius: 9999,
        background: isYou ? "var(--lime)" : "var(--card)",
        border: isYou ? "1px solid transparent" : "1px solid var(--hairline)",
        color: isYou ? "var(--ink)" : "var(--ink-soft)",
        ...CHROME,
        fontSize: 10.5,
      }}
    >
      {!isYou && (
        <span
          style={{
            width: 6,
            height: 6,
            borderRadius: "50%",
            background: STATUS_DOTS[status],
            animation: status === "Invited" ? "ah-onb-pulse-dot 2s ease-in-out infinite" : "none",
          }}
        />
      )}
      {status}
    </span>
  );
}

/* ─────────────────────────────────────────────────────────────
   InlineError — uniform error surface for step screens.
   Pink dot + hairline carry the flag; text stays ink for AA.
   ───────────────────────────────────────────────────────────── */

export function InlineError({ children }: { children?: ReactNode }) {
  if (!children) return null;
  return (
    <div
      style={{
        display: "flex",
        alignItems: "baseline",
        gap: 10,
        marginBottom: 16,
        padding: "10px 14px",
        borderRadius: 10,
        background: "var(--card-in)",
        border: "1px solid var(--ah-pink-line)",
        color: "var(--ink)",
        fontSize: 13,
        lineHeight: 1.45,
      }}
      role="alert"
    >
      <span
        style={{ width: 6, height: 6, borderRadius: "50%", background: "var(--pink)", flexShrink: 0, transform: "translateY(-1px)" }}
        aria-hidden="true"
      />
      <span>{children}</span>
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────
   CodeInput — 6-box one-time code field with auto-advance,
   backspace-back, and 6-digit paste handling.
   Caller owns the code string state. Digits render in mono;
   a filled box carries the green (progress) hairline.
   ───────────────────────────────────────────────────────────── */

type CodeInputProps = {
  value: string;
  onChange: (next: string) => void;
  length?: number;
};

export const CodeInput = forwardRef<HTMLInputElement, CodeInputProps>(function CodeInput(
  { value, onChange, length = 6 },
  externalRef,
) {
  const refs = useRef<Array<HTMLInputElement | null>>([]);
  const digits: string[] = Array.from({ length }, (_, i) => value[i] ?? "");

  useEffect(() => {
    if (typeof externalRef === "function") externalRef(refs.current[0] ?? null);
    else if (externalRef && "current" in externalRef) (externalRef as { current: HTMLInputElement | null }).current = refs.current[0] ?? null;
  }, [externalRef]);

  const writeDigit = (i: number, d: string) => {
    const next = [...digits];
    next[i] = d;
    onChange(next.join("").slice(0, length));
  };

  const handleChange = (i: number, raw: string) => {
    const d = raw.replace(/\D/g, "").slice(-1);
    writeDigit(i, d);
    if (d && i < length - 1) refs.current[i + 1]?.focus();
  };

  const handleKeyDown = (i: number, e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Backspace" && !digits[i] && i > 0) {
      refs.current[i - 1]?.focus();
    }
  };

  const handlePaste = (e: React.ClipboardEvent<HTMLDivElement>) => {
    const pasted = e.clipboardData.getData("text").replace(/\D/g, "").slice(0, length);
    if (pasted.length === length) {
      e.preventDefault();
      onChange(pasted);
      window.setTimeout(() => refs.current[length - 1]?.focus(), 0);
    }
  };

  const boxStyle = (filled: boolean): CSSProperties => ({
    flex: "1 1 0",
    minWidth: 0,
    width: 0,
    height: 56,
    textAlign: "center",
    padding: 0,
    background: "var(--card-in)",
    color: "var(--ink)",
    border: "1px solid " + (filled ? "var(--ah-green-line)" : "var(--hairline)"),
    borderRadius: 10,
    fontSize: 22,
    fontWeight: 500,
    fontFamily: "var(--mono)",
    transition: "border-color 160ms cubic-bezier(.4,0,.2,1)",
    boxSizing: "border-box",
  });

  return (
    <div
      style={{ display: "flex", justifyContent: "space-between", gap: 8, marginBottom: 10, width: "100%" }}
      onPaste={handlePaste}
    >
      {digits.map((d, i) => (
        <input
          key={i}
          ref={(el) => {
            refs.current[i] = el;
          }}
          value={d}
          maxLength={1}
          inputMode="numeric"
          autoComplete={i === 0 ? "one-time-code" : "off"}
          onChange={(e) => handleChange(i, e.target.value)}
          onKeyDown={(e) => handleKeyDown(i, e)}
          autoFocus={i === 0}
          aria-label={`Digit ${i + 1}`}
          style={boxStyle(Boolean(d))}
        />
      ))}
    </div>
  );
});
