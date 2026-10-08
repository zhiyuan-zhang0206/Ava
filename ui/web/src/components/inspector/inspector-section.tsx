// The inspector panel's section chrome — icon + label + optional badge over
// a stack of rows. Extracted (task #2909) so plugin-widget sections render
// through the same header as the built-in ones; the panel and
// `inspector-widgets.tsx` both import this.

import type { ReactNode } from "react";

import { FLEX, FLEX_COL } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";

export function Section({
  icon,
  title,
  badge,
  action,
  children,
}: {
  icon: ReactNode;
  title: string;
  badge?: string;
  /** Optional right-aligned control in the header (a jump link). It only
   *  claims the header's free space that the badge does not already claim. */
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="space-y-1.5">
      <div className={cn("items-center gap-1.5 text-[10px] tracking-wide text-muted-foreground", FLEX)}>
        {icon}
        <span>{title}</span>
        {badge != null && (
          <span className="ml-auto font-mono text-[11px] tabular-nums text-foreground normal-case">
            {badge}
          </span>
        )}
        {action != null && (
          <span className={cn("shrink-0 items-center", FLEX, badge == null && "ml-auto")}>
            {action}
          </span>
        )}
      </div>
      {children}
    </section>
  );
}

/** One labelled figure of a section's metric grid. */
export function Metric({
  className,
  label,
  value,
  sub,
  valueTestId,
}: {
  className?: string;
  label: string;
  value: string;
  sub?: string;
  valueTestId?: string;
}) {
  return (
    <div className={cn("gap-0.5 rounded bg-sidebar-accent/40 px-2 py-1", FLEX, FLEX_COL, className)}>
      <span className="text-[10px] tracking-wide text-muted-foreground">{label}</span>
      <span className="font-mono text-xs tabular-nums text-foreground" data-testid={valueTestId}>
        {value}
      </span>
      {sub != null && (
        <span className="font-mono text-[10px] tabular-nums text-muted-foreground/70">{sub}</span>
      )}
    </div>
  );
}
