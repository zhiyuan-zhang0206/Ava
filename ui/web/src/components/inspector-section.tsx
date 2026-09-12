// The inspector panel's section chrome — icon + label + optional badge over
// a stack of rows. Extracted (task #2909) so plugin-widget sections render
// through the same header as the built-in ones; the panel and
// `inspector-widgets.tsx` both import this.

import type { ReactNode } from "react";

import { FLEX } from "@/lib/layout";
import { cn } from "@/lib/utils";

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
