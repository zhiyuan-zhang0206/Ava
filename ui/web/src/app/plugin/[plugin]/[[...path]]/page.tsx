"use client";

// /plugin/<plugin>/<path> — the console route that frames a plugin-served page.
//
// The page itself comes from the gateway's plugin mount
// (`/api/plugin-ui/<plugin>/<path>`), embedded in a sandboxed iframe. This
// route contributes only the chrome around it: which plugin, what the entry
// was called, and the way back. The label is looked up in the declaration set
// rather than passed through the URL, so a hand-typed or bookmarked link is
// labelled the same as one opened from the nav.

import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

import { ErrorBoundary } from "@/components/error-boundary";
import { PluginPageFrame } from "@/components/plugin-page-frame";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0 } from "@/lib/layout";
import { useUiContributions } from "@/lib/ui-contributions";
import { cn } from "@/lib/utils";

export default function PluginPage({
  params,
}: {
  params: Promise<{ plugin: string; path?: string[] }>;
}) {
  // Next.js 16 passes params as a Promise. Unwrapped with useState+useEffect
  // (not use(), to avoid Suspense) — the same shape the shell monitor route
  // uses.
  const [resolved, setResolved] = useState<{ plugin: string; path?: string[] } | null>(null);
  useEffect(() => {
    let cancelled = false;
    params
      .then((p) => {
        if (!cancelled) setResolved(p);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [params]);

  const { contributions } = useUiContributions();

  if (resolved === null) return null;

  const plugin = resolved.plugin;
  // The catch-all drops the trailing slash a declaration may carry, and the
  // mount answers a bare directory with a redirect to it — so both spellings
  // land on the same page.
  const page = (resolved.path ?? []).join("/");
  const entry = (contributions?.nav ?? []).find(
    (n) => n.plugin === plugin && n.page.replace(/\/$/, "") === page,
  );
  const label = entry?.label ?? plugin;

  return (
    // <main> landmark (a11y), like every other primary surface.
    <main id="main-content" className={cn(FLEX, FLEX_1, MIN_H_0, FLEX_COL)}>
      <header
        className={cn("shrink-0 items-center gap-2 border-b border-border px-4 py-2", FLEX)}
      >
        <Link
          href="/"
          className="rounded p-1 text-muted-foreground hover:bg-sidebar-accent hover:text-foreground"
          aria-label="Back to conversation"
        >
          <ArrowLeft className="size-4" aria-hidden />
        </Link>
        <h1 className="text-sm font-semibold">{label}</h1>
        {/* Provenance: a surface the console did not write says whose it is. */}
        <span className="text-xs text-muted-foreground">{plugin}</span>
      </header>
      <div className={cn(FLEX_1, MIN_H_0)}>
        <ErrorBoundary>
          <PluginPageFrame plugin={plugin} page={page} title={label} />
        </ErrorBoundary>
      </div>
    </main>
  );
}
