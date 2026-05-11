/**
 * Archangel Health — onboarding primitives.
 *
 * 1:1 port of the design handoff prototype components
 * (see .tmp-flat/archangel/design_handoff_onboarding_flow/design_files/components.jsx)
 * into typed, idiomatic React.
 *
 * Each primitive is the smallest piece needed to assemble the 6 step screens
 * and the email-shell preview. They share no state with each other.
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

/* ─────────────────────────────────────────────────────────────
   Brandmark — gradient tile + Archangel wordmark.
   ───────────────────────────────────────────────────────────── */

type BrandmarkSize = "sm" | "md" | "lg";

const BRAND_SIZES: Record<BrandmarkSize, { tile: number; shield: number; gap: number; word: number }> = {
  sm: { tile: 28, shield: 16, gap: 10, word: 12 },
  md: { tile: 36, shield: 20, gap: 12, word: 14 },
  lg: { tile: 44, shield: 24, gap: 14, word: 16 },
};

export function Brandmark({ size = "md" }: { size?: BrandmarkSize }) {
  const s = BRAND_SIZES[size];
  return (
    <div style={{ display: "inline-flex", alignItems: "center", gap: s.gap }}>
      <div
        style={{
          width: s.tile,
          height: s.tile,
          borderRadius: s.tile * 0.32,
          background: "linear-gradient(135deg, #1A3C8F 0%, #2563EB 100%)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          boxShadow: "0 0 0 1px rgba(103,232,249,0.18), 0 8px 24px rgba(38,99,235,0.30)",
          flexShrink: 0,
        }}
      >
        <svg viewBox="0 0 120 120" width={s.shield} height={s.shield} fill="none" aria-hidden="true">
          <rect x="58" y="20" width="4" height="80" fill="#fff" rx="2" />
          <circle cx="60" cy="28" r="12" stroke="#fff" strokeWidth="1.5" fill="none" opacity="0.9" />
          <circle cx="60" cy="28" r="4" fill="#67E8F9" opacity="0.95" />
          <path d="M60 45 Q50 50 48 58 Q46 66 54 70" stroke="#fff" strokeWidth="2.5" fill="none" strokeLinecap="round" />
          <path d="M60 55 Q70 60 72 68 Q74 76 66 80" stroke="#fff" strokeWidth="2.5" fill="none" strokeLinecap="round" />
          <circle cx="47" cy="58" r="3.5" fill="#fff" />
          <circle cx="73" cy="68" r="3.5" fill="#fff" />
        </svg>
      </div>
      <span
        style={{
          fontFamily: "'Inter', sans-serif",
          fontSize: s.word,
          fontWeight: 600,
          letterSpacing: "0.14em",
          textTransform: "uppercase",
          color: "#F5F5F7",
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
        padding: "20px 32px",
        background: "rgba(7, 7, 10, 0.72)",
        backdropFilter: "blur(14px) saturate(140%)",
        WebkitBackdropFilter: "blur(14px) saturate(140%)",
        borderBottom: "1px solid rgba(255,255,255,0.06)",
      }}
    >
      <Brandmark size="md" />
      <div style={{ display: "flex", alignItems: "center", gap: 18 }}>
        <a
          href={helpHref}
          style={{
            fontSize: 13,
            color: "rgba(245,245,247,0.62)",
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
            color: "rgba(245,245,247,0.62)",
            background: "transparent",
            border: "none",
            padding: "8px 14px",
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
   Stepper — numbered chips connected by hairlines.
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
        const chipColor = done || active ? "#67E8F9" : "rgba(245,245,247,0.5)";
        return (
          <div key={label} style={{ display: "contents" }}>
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 10,
                opacity: done || active ? 1 : 0.42,
                transition: "opacity 400ms cubic-bezier(0.16, 1, 0.3, 1)",
              }}
            >
              <div
                style={{
                  width: 26,
                  height: 26,
                  borderRadius: "50%",
                  background: done ? "rgba(103,232,249,0.16)" : "transparent",
                  border: active
                    ? "1.5px solid #67E8F9"
                    : done
                      ? "1.5px solid rgba(103,232,249,0.4)"
                      : "1px solid rgba(255,255,255,0.16)",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  fontSize: 12,
                  fontWeight: 600,
                  color: chipColor,
                  fontFamily: "'Inter', sans-serif",
                  boxShadow: active
                    ? "0 0 0 6px rgba(103,232,249,0.06), 0 0 22px rgba(103,232,249,0.18)"
                    : "none",
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
                  fontSize: 11,
                  fontWeight: 600,
                  letterSpacing: "0.12em",
                  textTransform: "uppercase",
                  color: active ? "#F5F5F7" : done ? "rgba(245,245,247,0.72)" : "rgba(245,245,247,0.42)",
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
                  background: done ? "rgba(103,232,249,0.35)" : "rgba(255,255,255,0.10)",
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
   OnboardingCard — eyebrow + Fraunces title + lede + body card.
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
            fontSize: 11,
            fontWeight: 700,
            letterSpacing: "0.18em",
            textTransform: "uppercase",
            color: "#67E8F9",
            textAlign: "center",
            marginBottom: 18,
            opacity: 0.85,
          }}
        >
          {eyebrow}
        </div>
      )}
      <h1
        style={{
          fontFamily: "'Fraunces', 'Iowan Old Style', 'Charter', Georgia, serif",
          fontSize: "clamp(36px, 5vw, 52px)",
          fontWeight: 500,
          lineHeight: 1.05,
          letterSpacing: "-0.025em",
          textAlign: "center",
          color: "#F5F5F7",
          marginTop: 0,
          marginBottom: lede ? 18 : 36,
          fontVariationSettings: '"opsz" 96, "SOFT" 30',
        }}
      >
        {title}
      </h1>
      {lede && (
        <p
          style={{
            fontSize: 16,
            lineHeight: 1.55,
            color: "rgba(245,245,247,0.62)",
            textAlign: "center",
            maxWidth: 520,
            margin: "0 auto 40px",
          }}
        >
          {lede}
        </p>
      )}

      <div
        style={{
          background: "linear-gradient(180deg, rgba(20, 22, 30, 0.85) 0%, rgba(13, 14, 20, 0.85) 100%)",
          backdropFilter: "blur(20px) saturate(140%)",
          WebkitBackdropFilter: "blur(20px) saturate(140%)",
          border: "1px solid rgba(255,255,255,0.08)",
          borderRadius: 20,
          padding: "36px 40px",
          boxShadow:
            "0 24px 60px rgba(0,0,0,0.35), 0 0 0 1px rgba(103,232,249,0.04), inset 0 1px 0 rgba(255,255,255,0.04)",
        }}
      >
        {children}
      </div>

      {footer && (
        <div style={{ marginTop: 24, textAlign: "center", fontSize: 13, color: "rgba(245,245,247,0.45)" }}>
          {footer}
        </div>
      )}
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────
   FieldLabel — uppercase mini-label inside cards.
   ───────────────────────────────────────────────────────────── */

export function FieldLabel({ children, optional }: { children: ReactNode; optional?: boolean }) {
  return (
    <div
      style={{
        display: "block",
        fontSize: 11,
        fontWeight: 700,
        letterSpacing: "0.10em",
        textTransform: "uppercase",
        color: "rgba(245,245,247,0.62)",
        marginBottom: 10,
      }}
    >
      {children}
      {optional && (
        <span
          style={{
            color: "rgba(245,245,247,0.32)",
            fontWeight: 500,
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
   TextField — calm dark input, animated focus ring.
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
          background: focused ? "rgba(15, 17, 24, 0.85)" : "rgba(15, 17, 24, 0.55)",
          border:
            "1px solid " +
            (error
              ? "rgba(248,113,113,0.55)"
              : focused
                ? "rgba(103,232,249,0.45)"
                : "rgba(255,255,255,0.10)"),
          borderRadius: 12,
          padding: "0 16px",
          transition: "all 220ms cubic-bezier(0.16, 1, 0.3, 1)",
          boxShadow: focused
            ? "0 0 0 4px rgba(103,232,249,0.10), inset 0 1px 0 rgba(255,255,255,0.03)"
            : "inset 0 1px 0 rgba(255,255,255,0.02)",
        }}
      >
        {prefix && (
          <span style={{ color: "rgba(245,245,247,0.5)", marginRight: 10, fontSize: 14 }}>{prefix}</span>
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
            color: "#F5F5F7",
            fontSize: 15,
            fontWeight: 400,
            padding: "14px 0",
            fontFamily: "inherit",
            minWidth: 0,
          }}
        />
        {suffix}
      </div>
      {hint && !error && (
        <div style={{ fontSize: 12, color: "rgba(245,245,247,0.45)", marginTop: 8, paddingLeft: 4 }}>{hint}</div>
      )}
      {error && (
        <div style={{ fontSize: 12, color: "#FCA5A5", marginTop: 8, paddingLeft: 4 }}>{error}</div>
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
          background: focused ? "rgba(15, 17, 24, 0.85)" : "rgba(15, 17, 24, 0.55)",
          border: "1px solid " + (focused ? "rgba(103,232,249,0.45)" : "rgba(255,255,255,0.10)"),
          borderRadius: 12,
          padding: "0 16px",
          transition: "all 220ms cubic-bezier(0.16, 1, 0.3, 1)",
          boxShadow: focused ? "0 0 0 4px rgba(103,232,249,0.10)" : "none",
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
            color: value ? "#F5F5F7" : "rgba(245,245,247,0.45)",
            fontSize: 15,
            padding: "14px 0",
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
              style={{ background: "#14161E", color: "#F5F5F7" }}
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
            color: "rgba(245,245,247,0.5)",
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
   PrimaryButton — idle → loading → success → reset lifecycle.
   The handler may return a Promise; resolving `false` keeps it Idle
   (used to signal a server error without entering Success).
   ───────────────────────────────────────────────────────────── */

type ButtonVariant = "primary" | "secondary";
type ButtonState = "idle" | "loading" | "success";

const BUTTON_STYLES: Record<ButtonVariant, Record<ButtonState, { bg: string; fg: string; border: string; shadow: string }>> = {
  primary: {
    idle: { bg: "#F5F5F7", fg: "#07070A", border: "transparent", shadow: "0 0 0 1px rgba(255,255,255,0.0)" },
    loading: {
      bg: "rgba(245,245,247,0.78)",
      fg: "#07070A",
      border: "transparent",
      shadow: "0 0 0 1px rgba(255,255,255,0.0)",
    },
    success: {
      bg: "#67E8F9",
      fg: "#07070A",
      border: "transparent",
      shadow: "0 0 0 1px rgba(103,232,249,0.4), 0 0 32px rgba(103,232,249,0.35)",
    },
  },
  secondary: {
    idle: {
      bg: "rgba(255,255,255,0.06)",
      fg: "#F5F5F7",
      border: "rgba(255,255,255,0.16)",
      shadow: "none",
    },
    loading: {
      bg: "rgba(255,255,255,0.10)",
      fg: "#F5F5F7",
      border: "rgba(255,255,255,0.20)",
      shadow: "none",
    },
    success: {
      bg: "rgba(103,232,249,0.12)",
      fg: "#67E8F9",
      border: "rgba(103,232,249,0.45)",
      shadow: "0 0 24px rgba(103,232,249,0.20)",
    },
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
        padding: "15px 28px",
        borderRadius: 12,
        background: s.bg,
        color: s.fg,
        border: "1px solid " + s.border,
        fontSize: 15,
        fontWeight: 600,
        letterSpacing: "-0.005em",
        fontFamily: "inherit",
        opacity: disabled ? 0.45 : 1,
        cursor:
          disabled || state !== "idle" ? (state === "loading" ? "progress" : "default") : "pointer",
        transition: "all 280ms cubic-bezier(0.16, 1, 0.3, 1)",
        boxShadow: s.shadow,
        transform: state === "loading" ? "scale(0.985)" : "scale(1)",
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
          <Spinner color={variant === "primary" ? "#07070A" : "#F5F5F7"} />
          <span>{loadingLabel ?? "Working…"}</span>
        </>
      )}
      {state === "success" && (
        <>
          <CheckTick color={variant === "primary" ? "#07070A" : "#67E8F9"} />
          <span>{successLabel ?? "Done"}</span>
        </>
      )}
    </button>
  );
}

export function Spinner({ color = "#07070A" }: { color?: string }) {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" style={{ animation: "ah-onb-spin 0.8s linear infinite" }}>
      <circle cx="12" cy="12" r="10" stroke={color} strokeWidth="2.5" strokeLinecap="round" strokeOpacity="0.25" />
      <path d="M22 12 A10 10 0 0 0 12 2" stroke={color} strokeWidth="2.5" strokeLinecap="round" />
    </svg>
  );
}

export function CheckTick({ color = "#07070A" }: { color?: string }) {
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
        color: "rgba(245,245,247,0.55)",
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
   Avatar — initials circle. Deterministic hue from name/email.
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
  const hue = seedSrc.split("").reduce((a, c) => a + c.charCodeAt(0), 0) % 360;
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
        background: you
          ? "linear-gradient(135deg, #1A3C8F 0%, #2563EB 60%, #67E8F9 100%)"
          : `linear-gradient(135deg, hsl(${hue} 60% 28%) 0%, hsl(${hue} 70% 42%) 100%)`,
        color: "#F5F5F7",
        fontSize: size * 0.36,
        fontWeight: 600,
        letterSpacing: "0.01em",
        boxShadow: you
          ? "0 0 0 1.5px rgba(103,232,249,0.55), 0 4px 16px rgba(38,99,235,0.35)"
          : "0 0 0 1px rgba(255,255,255,0.08), inset 0 1px 0 rgba(255,255,255,0.06)",
      }}
    >
      {initials}
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────
   RolePill / StatusPill — capsule chips for member rows.
   ───────────────────────────────────────────────────────────── */

const ROLE_PALETTE: Record<string, { bg: string; border: string; fg: string }> = {
  "Director of TEAM Initiative": {
    bg: "rgba(103,232,249,0.10)",
    border: "rgba(103,232,249,0.32)",
    fg: "#67E8F9",
  },
  Surgeon: {
    bg: "rgba(38,99,235,0.14)",
    border: "rgba(96,165,250,0.32)",
    fg: "#93C5FD",
  },
  "RN Care Coordinator": {
    bg: "rgba(45,212,191,0.10)",
    border: "rgba(45,212,191,0.32)",
    fg: "#5EEAD4",
  },
  "NP / PA": {
    bg: "rgba(167,139,250,0.12)",
    border: "rgba(167,139,250,0.32)",
    fg: "#C4B5FD",
  },
};

export function RolePill({ role }: { role: string }) {
  const p = ROLE_PALETTE[role] ?? ROLE_PALETTE["Surgeon"];
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        height: 22,
        padding: "0 9px",
        borderRadius: 9999,
        background: p.bg,
        border: "1px solid " + p.border,
        color: p.fg,
        fontSize: 10.5,
        fontWeight: 700,
        letterSpacing: "0.06em",
        textTransform: "uppercase",
        whiteSpace: "nowrap",
      }}
    >
      {role}
    </span>
  );
}

const STATUS_PALETTE: Record<string, { bg: string; border: string; fg: string; dot: string }> = {
  Invited: { bg: "rgba(251,191,36,0.10)", border: "rgba(251,191,36,0.32)", fg: "#FCD34D", dot: "#FCD34D" },
  Active: { bg: "rgba(34,197,94,0.10)", border: "rgba(34,197,94,0.32)", fg: "#86EFAC", dot: "#86EFAC" },
  You: { bg: "rgba(245,245,247,0.08)", border: "rgba(245,245,247,0.20)", fg: "#F5F5F7", dot: "#67E8F9" },
};

export function StatusPill({ status }: { status: "Invited" | "Active" | "You" }) {
  const p = STATUS_PALETTE[status];
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        height: 22,
        padding: "0 10px",
        borderRadius: 9999,
        background: p.bg,
        border: "1px solid " + p.border,
        color: p.fg,
        fontSize: 10.5,
        fontWeight: 700,
        letterSpacing: "0.06em",
        textTransform: "uppercase",
      }}
    >
      <span
        style={{
          width: 6,
          height: 6,
          borderRadius: "50%",
          background: p.dot,
          boxShadow: status === "Invited" ? `0 0 8px ${p.dot}` : "none",
          animation: status === "Invited" ? "ah-onb-pulse-dot 2s ease-in-out infinite" : "none",
        }}
      />
      {status}
    </span>
  );
}

/* ─────────────────────────────────────────────────────────────
   InlineError — uniform error surface for step screens.
   ───────────────────────────────────────────────────────────── */

export function InlineError({ children }: { children?: ReactNode }) {
  if (!children) return null;
  return (
    <div
      style={{
        marginBottom: 16,
        padding: "10px 14px",
        borderRadius: 10,
        background: "rgba(248,113,113,0.08)",
        border: "1px solid rgba(248,113,113,0.32)",
        color: "#FCA5A5",
        fontSize: 13,
        lineHeight: 1.45,
      }}
      role="alert"
    >
      {children}
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────
   CodeInput — 6-box one-time code field with auto-advance,
   backspace-back, and 6-digit paste handling.
   Caller owns the code string state.
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
    background: "rgba(15,17,24,0.55)",
    color: "#F5F5F7",
    border: "1px solid " + (filled ? "rgba(103,232,249,0.45)" : "rgba(255,255,255,0.10)"),
    borderRadius: 12,
    fontSize: 22,
    fontWeight: 600,
    fontFamily: "'Fraunces', 'Iowan Old Style', 'Charter', Georgia, serif",
    outline: "none",
    transition: "all 220ms cubic-bezier(0.16, 1, 0.3, 1)",
    boxShadow: filled ? "0 0 0 4px rgba(103,232,249,0.08)" : "none",
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
