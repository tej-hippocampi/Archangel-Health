/// <reference types="vite/client" />
/**
 * Landing build-time configuration.
 *
 * Today this holds the Calendly links, and it exists because the repo shipped
 * two of them hardcoded in two components pointing at two DIFFERENT Calendly
 * accounts. That is the drift an env var exists to end: a link changes, someone
 * greps for it, finds one copy, and the other keeps sending people to a
 * calendar nobody watches.
 *
 * WHY TWO VALUES AND NOT ONE. The two links are not two copies of one booking
 * link, they are two different destinations that happen to both be Calendly:
 * the partner call on one founder's calendar, the team-calculator demo on
 * another's. Collapsing them here would silently retarget one audience's
 * meetings to the other founder, which is a routing decision for the founders
 * to make deliberately rather than a side effect of adding an env var. Point
 * both env vars at one account the day they decide to consolidate.
 *
 * Each falls back to the constant its call site shipped with, so a build with
 * neither variable set behaves exactly as the build before this file did.
 */

// Read straight off `import.meta.env`, per `lib/auth-api.ts`: Vite substitutes
// it statically and any indirection defeats the substitution, leaving the value
// undefined at runtime.
type LandingEnv = {
  VITE_CALENDLY_URL?: string;
  VITE_CALENDLY_TEAM_URL?: string;
};
const env: LandingEnv = (import.meta.env as LandingEnv | undefined) ?? {};

/**
 * The /partner booking link: "Quick Meeting", 20 minutes, Google Meet.
 *
 * The `month` param is carried through as given. Calendly treats it as the
 * month to OPEN on rather than the only month it will show, and pages forward
 * on its own, so a month in the past does not strand anyone.
 */
const PARTNER_BOOKING_FALLBACK =
  "https://calendly.com/aryaabhatia-berkeley/new-meeting?month=2026-03";

/** The team-calculator demo link, on a different Calendly account. */
const TEAM_INTRO_FALLBACK =
  "https://calendly.com/tejxpatel23/archangel-health-intro";

/** Where /partner sends a health system after it answers the questions. */
export const PARTNER_BOOKING_URL: string =
  env.VITE_CALENDLY_URL || PARTNER_BOOKING_FALLBACK;

/** Where the team calculator's "Book a demo" goes. */
export const TEAM_INTRO_URL: string =
  env.VITE_CALENDLY_TEAM_URL || TEAM_INTRO_FALLBACK;
