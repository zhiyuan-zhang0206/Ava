"use client";

// The header of a full-page view (Fleet, Insights, Control, the run timeline, the cluster
// view): the page title, a row of quiet facts about what the page shows, and the way back.
// Fleet's header geometry, so every full page reads as one family.

import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import type { ComponentProps, ReactNode } from "react";

import { FLEX, FLEX_1, MIN_W_0 } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";

export function PageHeader({
  title,
  backHref,
  backLabel,
  children,
}: {
  title: string;
  backHref: ComponentProps<typeof Link>["href"];
  /** The accessible name of the back link. */
  backLabel: string;
  /** Quiet facts about what the page shows, after the title. */
  children?: ReactNode;
}) {
  return (
    <header className={cn("shrink-0 items-center gap-3 border-b border-border px-6 py-3", FLEX)}>
      <h1 className="shrink-0 text-sm font-semibold">{title}</h1>
      <div className={cn("items-center gap-3 text-xs text-muted-foreground", FLEX, FLEX_1, MIN_W_0)}>{children}</div>
      <Link
        href={backHref}
        className="shrink-0 rounded p-1 text-muted-foreground hover:bg-sidebar-accent hover:text-foreground"
        aria-label={backLabel}
        title={backLabel}
      >
        <ArrowLeft className="size-5" aria-hidden />
      </Link>
    </header>
  );
}
