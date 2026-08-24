"use client";

// AuthGuard — redirects to /login when not authenticated.
//
// Wrap around pages that require authentication. The login page itself
// should NOT be wrapped — it reads auth state directly.
//
import { usePathname, useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";

import { TelemetryPageView } from "@/lib/telemetry-page-view";
import { useAuth } from "@/lib/auth-context";
import { FLEX } from "@/lib/layout";
import { cn } from "@/lib/utils";

export function AuthGuard({ children }: { children: ReactNode }) {
  const { status } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  const isLoginPage = pathname === "/login";

  useEffect(() => {
    if (status === "unauthenticated" && !isLoginPage) {
      router.replace("/login");
    }
  }, [status, isLoginPage, router]);

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

  // Unauthenticated + not login page — show nothing while redirecting.
  return null;
}
