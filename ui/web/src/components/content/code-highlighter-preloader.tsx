"use client";

// Warms the syntax-highlighter chunk (prism-react-renderer, lazy-loaded by
// python-code.tsx) once the page has painted and gone idle, so that by the
// time a user actually expands a code block the module is already resolved
// and highlighting is synchronous — instead of shipping the ~85KB library in
// the route's initial bundle (ui/web/scripts/check-first-load-js.mjs budget;
// see python-code.tsx for the lazy-load + intent-prefetch contract).
//
// Mount once per page that can show code blocks (home timeline,
// /control/schedules' script viewer) — not in the root layout, since not
// every route needs it.
//
// requestIdleCallback runs after paint and pending input have been
// serviced, with a timeout so a busy tab (idle callback starved by other
// work) still warms the chunk eventually. Safari has no
// requestIdleCallback at all, hence the setTimeout fallback.
import { useEffect } from "react";

import { preloadPythonCodeHighlighter } from "@/components/content/python-code";

const IDLE_TIMEOUT_MS = 4000;
const FALLBACK_DELAY_MS = 2000;

export function CodeHighlighterPreloader() {
  useEffect(() => {
    if (typeof window.requestIdleCallback === "function") {
      const handle = window.requestIdleCallback(() => void preloadPythonCodeHighlighter(), {
        timeout: IDLE_TIMEOUT_MS,
      });
      return () => window.cancelIdleCallback(handle);
    }

    const timer = window.setTimeout(() => void preloadPythonCodeHighlighter(), FALLBACK_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, []);

  return null;
}
