"use client";

// AuthGuard — redirects to /login when not authenticated.
//
// Wrap around pages that require authentication. The login page itself
// should NOT be wrapped — it reads auth state directly.
//
// Special case: when the cluster is known to be updating (clusterUpdating in
// the store is true) and auth fails, show the UpdatingPage instead of
// redirecting to /login. This prevents users from seeing a confusing password
// prompt during planned cluster restarts.

import { usePathname, useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";

import { UpdatingPage } from "@/components/updating-page";
import { TelemetryPageView } from "@/lib/telemetry-page-view";
import { useAuth } from "@/lib/auth-context";
import { useStore } from "@/lib/store";
import { FLEX } from "@/lib/layout";
import { cn } from "@/lib/utils";

export function AuthGuard({ children }: { children: ReactNode }) {
  const { status } = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  const clusterUpdating = useStore((s) => s.clusterUpdating);

  const isLoginPage = pathname === "/login";

  useEffect(() => {
    if (status === "unauthenticated" && !isLoginPage && !clusterUpdating) {
      router.replace("/login");
    }
  }, [status, isLoginPage, router, clusterUpdating]);

  // Show nothing while checking auth (avoids flash of unprotected content)
  if (status === "loading") {
    return (
      <div className={cn("min-h-screen items-center justify-center", FLEX)}>
        <div className="h-6 w-6 animate-spin rounded-full border-2 border-primary border-t-transparent" />
      </div>
    );
  }

  // On login page, always show children (login form handles its own redirect)
  if (isLoginPage) return <>{children}</>;

  // Authenticated — show the app. TelemetryPageView (page-view tracking)
  // lives here so only the authenticated surface is tracked, never the
  // login page or the pre-auth loading state.
  if (status === "authenticated") {
    return (
      <>
        {children}
        <TelemetryPageView />
      </>
    );
  }

  // Unauthenticated but cluster is known to be updating — show the updating
  // page instead of redirecting to /login. The UpdatingPage retries auth
  // periodically; when the backend comes back, useAuth will flip to
  // "authenticated" and the app will render.
  if (clusterUpdating) {
    return <UpdatingPage />;
  }

  // Unauthenticated + not updating + not login page — show nothing while
  // redirecting
  return null;
}
