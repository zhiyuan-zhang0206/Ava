"use client";

// Plugin-contributed inspector widgets — the console half of the extension
// surface (task #2909; taskList reshaped in #3216).
//
// A widget is DATA: a closed-set kind plus the payload the kernel already
// resolved (for a taskList, the agent's active tasks). The console renders
// the kinds it knows, links the rows it can address, and skips everything
// else — an unknown kind from a newer kernel must not blank the panel or
// throw, and no plugin markup or code enters this bundle at any step (the
// plugin's Python half runs in the gateway, never here).

import { ListChecks } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { Section } from "@/components/inspector-section";
import { fleetTaskHref } from "@/lib/inspector-widgets";
import type { InspectWidget } from "@/lib/types";
import { cn } from "@/lib/utils";
import { FLEX, FLEX_1, MIN_W_0 } from "@/lib/layout";

/** One plugin widget. A taskList renders through the same section chrome as
 *  the built-in sections, one fleet-task link per row; an unknown kind (a
 *  newer kernel's) renders nothing. The console's own localized title stands
 *  in when the plugin declared none. */
export function InspectWidgetSection({ widget }: { widget: InspectWidget }) {
  const t = useTranslations("inspector");
  // Unknown kind: skipped, never interpreted (see the module comment). The
  // local widening is deliberate — this console's own type only knows today's
  // kinds, while the guard exists for a NEWER kernel's payload.
  const kind: string = widget.kind;
  if (kind !== "taskList") return null;
  const tasks = widget.tasks ?? [];
  if (tasks.length === 0) return null;

  return (
    <Section
      icon={<ListChecks className="size-3" />}
      title={widget.title ?? t("sectionTasks")}
      badge={String(tasks.length)}
    >
      <ul className="space-y-1">
        {tasks.map((task) => (
          <li key={task.id}>
            <Link
              href={fleetTaskHref(task.id)}
              className={cn(
                "items-center gap-2 rounded bg-sidebar-accent/40 px-2 py-1 text-[11px] hover:bg-sidebar-accent",
                FLEX,
                MIN_W_0,
              )}
            >
              <span className={cn("truncate text-foreground", MIN_W_0, FLEX_1)}>{task.title}</span>
              <span className="shrink-0 font-mono text-[10px] text-muted-foreground">
                #{task.id}
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </Section>
  );
}
