"use client";

// Page-view telemetry — one `page-view` interaction per route change on the
// authenticated surface. Mounted inside AuthGuard's authenticated branch (the
// login page and the pre-auth loading states are not tracked).
//
// The initial mount tracks the first page too (a page load is a view); the
// ref guards against double-fire on route-entry re-renders.

import { usePathname } from "next/navigation";
import { useEffect, useRef } from "react";

import { normalizePage, setTelemetryPage, track } from "./telemetry";
import { initWebVitals } from "./web-vitals";

export function TelemetryPageView() {
  const pathname = usePathname();
  const lastPage = useRef<string | null>(null);

  useEffect(() => {
    initWebVitals();
  }, []);

  useEffect(() => {
    const page = normalizePage(pathname);
    setTelemetryPage(page);
    if (lastPage.current !== page) {
      lastPage.current = page;
      track("page-view");
    }
  }, [pathname]);

  return null;
}
