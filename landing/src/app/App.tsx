import { lazy, Suspense } from "react";
import { AuthProvider } from "@/contexts/AuthContext";
import RecoveryResourcesEmailPreview from "@/app/components/RecoveryResourcesEmailPreview";
import TeamCalculator from "@/app/components/TeamCalculator";
import TeamWhitepaperPage from "@/app/components/TeamWhitepaperPage";
import PodcastAndBlogsPage from "@/app/components/PodcastAndBlogsPage";
import { SiteHeader, parseLandingView } from "@/app/components/SiteHeader";
import JoinEntry from "@/app/components/JoinEntry";
import PartnerInterest from "@/app/components/PartnerInterest";
import OnboardingWizard from "@/app/components/OnboardingWizard";
import TenantSignIn from "@/app/components/TenantSignIn";
import VerifyEmailPage from "@/app/components/VerifyEmailPage";
import ResetPasswordPage from "@/app/components/ResetPasswordPage";

// Lazy so the landing's ~535KB of embedded-font CSS becomes a landing-only
// chunk instead of render-blocking every other route (calculator, onboarding…).
const ArchShell = lazy(() => import("@/app/components/arch/ArchShell"));

// Audience routes served by the landing shell (menu-driven SPA — PRD
// Archangel_Landing_Rebuild_v2 §1). Deep-linkable; vercel.json rewrites match.
const ARCH_ROUTES = new Set(["/", "/research", "/data", "/health-systems", "/physicians", "/mission"]);

export default function App() {
  // Normalize a trailing slash so `/email-preview/` (and friends) don't fall
  // through to the landing shell — vercel rewrites both variants here.
  const rawPath = typeof window !== "undefined" ? window.location.pathname : "/";
  const normalizedPath = rawPath.length > 1 ? rawPath.replace(/\/+$/, "") || "/" : rawPath;

  const isEmailPreviewRoute =
    typeof window !== "undefined" &&
    (normalizedPath === "/email-preview" || window.location.search.includes("emailPreview=1"));

  if (isEmailPreviewRoute) {
    return <RecoveryResourcesEmailPreview />;
  }

  const path = normalizedPath;
  const memberOnboardMatch = path.match(/^\/onboard\/m\/([^/]+)\/?$/);
  if (memberOnboardMatch) {
    return (
      <AuthProvider>
        <OnboardingWizard token={decodeURIComponent(memberOnboardMatch[1])} mode="member" />
      </AuthProvider>
    );
  }
  // The shareable get-started entry: prefilled by query params, straight into
  // the wizard on submit. No token — it mints one through /self-serve.
  if (/^\/join$/.test(path)) {
    return (
      <AuthProvider>
        <JoinEntry />
      </AuthProvider>
    );
  }
  // The health-system one-pager link: interest form, then straight to Calendly.
  // Top-level rather than an ArchShell route because useLandingAuth bounces a
  // signed-in user off the marketing shell, and normalizePath refuses to
  // navigate anywhere outside ARCH_PATHS.
  if (/^\/partner$/.test(path)) {
    return (
      <AuthProvider>
        <PartnerInterest />
      </AuthProvider>
    );
  }
  const onboardMatch = path.match(/^\/onboard\/([^/]+)\/?$/);
  if (onboardMatch) {
    return (
      <AuthProvider>
        <OnboardingWizard token={decodeURIComponent(onboardMatch[1])} />
      </AuthProvider>
    );
  }
  const verifyEmailMatch = path.match(/^\/verify-email\/([^/]+)\/?$/);
  if (verifyEmailMatch) {
    return <VerifyEmailPage token={decodeURIComponent(verifyEmailMatch[1])} />;
  }
  // The reset link carries its token in a query param rather than the path, so
  // it survives an email client that rewrites or wraps the URL path.
  if (/^\/reset-password\/?$/.test(path)) {
    const t = new URLSearchParams(window.location.search).get("token") || "";
    return <ResetPasswordPage token={t} />;
  }
  const tenantSignInMatch = path.match(/^\/t\/([^/]+)\/sign-in\/?$/);
  if (tenantSignInMatch) {
    return (
      <AuthProvider>
        <TenantSignIn slug={decodeURIComponent(tenantSignInMatch[1])} />
      </AuthProvider>
    );
  }

  const view = parseLandingView();

  // The landing shell ships its own fixed nav + footer, so its routes render
  // without SiteHeader; every other view keeps the shared header.
  if (view === "home") {
    const normalized = path.replace(/\/+$/, "") || "/";
    const initialPath = ARCH_ROUTES.has(normalized) ? normalized : "/";
    return (
      <AuthProvider>
        <Suspense fallback={<div style={{ minHeight: "100vh", background: "#eef0ef" }} />}>
          <ArchShell initialPath={initialPath} />
        </Suspense>
      </AuthProvider>
    );
  }

  return (
    <AuthProvider>
      <SiteHeader activeView={view} />
      {view === "whitepaper" && <TeamWhitepaperPage />}
      {view === "calculator" && <TeamCalculator />}
      {view === "podcastBlogs" && <PodcastAndBlogsPage />}
    </AuthProvider>
  );
}
