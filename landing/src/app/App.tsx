import { lazy, Suspense } from "react";
import { AuthProvider } from "@/contexts/AuthContext";
import RecoveryResourcesEmailPreview from "@/app/components/RecoveryResourcesEmailPreview";
import TeamCalculator from "@/app/components/TeamCalculator";
import TeamWhitepaperPage from "@/app/components/TeamWhitepaperPage";
import PodcastAndBlogsPage from "@/app/components/PodcastAndBlogsPage";
import { SiteHeader, parseLandingView } from "@/app/components/SiteHeader";
import OnboardingWizard from "@/app/components/OnboardingWizard";
import TenantSignIn from "@/app/components/TenantSignIn";

// Lazy so the landing's ~535KB of embedded-font CSS becomes a landing-only
// chunk instead of render-blocking every other route (calculator, onboarding…).
const ArchShell = lazy(() => import("@/app/components/arch/ArchShell"));

// Audience routes served by the landing shell (menu-driven SPA — PRD
// Archangel_Landing_Rebuild_v2 §1). Deep-linkable; vercel.json rewrites match.
const ARCH_ROUTES = new Set(["/", "/research", "/data", "/health-systems", "/physicians", "/mission"]);

export default function App() {
  const isEmailPreviewRoute =
    typeof window !== "undefined" &&
    (window.location.pathname === "/email-preview" || window.location.search.includes("emailPreview=1"));

  if (isEmailPreviewRoute) {
    return <RecoveryResourcesEmailPreview />;
  }

  const path = typeof window !== "undefined" ? window.location.pathname : "/";
  const memberOnboardMatch = path.match(/^\/onboard\/m\/([^/]+)\/?$/);
  if (memberOnboardMatch) {
    return (
      <AuthProvider>
        <OnboardingWizard token={decodeURIComponent(memberOnboardMatch[1])} mode="member" />
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
