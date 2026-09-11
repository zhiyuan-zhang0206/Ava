"use client";

// Plugin-contributed inspector widgets — the console half of the extension
// surface (task #2909; registration lives in `shared/plugin_inspector.py`).
//
// A widget is DATA: a closed-set kind plus targets the kernel already
// resolved. The console renders the kinds it knows, links the targets it can
// address, and skips everything else — an unknown kind from a newer kernel
// must not blank the panel or throw, and no plugin markup or code enters this
// bundle at any step (the plugin's Python half runs in the gateway, never
// here).

import { Puzzle } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { Section } from "@/components/inspector-section";
import { pluginNavIcon } from "@/components/plugin-nav-icon";
import { FLEX } from "@/lib/layout";
import { jumpButtonHref } from "@/lib/inspector-widgets";
import type { InspectWidget, InspectWidgetButton } from "@/lib/types";
import { cn } from "@/lib/utils";

// The console's default icon per target — names from the shared plugin icon
// vocabulary, so a plugin override and the default are drawn from one set.
const TARGET_ICONS: Record<InspectWidgetButton["target"], string> = {
  notice: "bell",
  task: "kanban",
};

/** One plugin widget: a headerless row of jump buttons, or a titled section
 *  when the widget declared a title. Renders nothing when the widget has no
 *  renderable buttons (either kind), so an emptied widget leaves no shell. */
export function InspectWidgetSection({ widget }: { widget: InspectWidget }) {
  const t = useTranslations("inspector");
  // Unknown kind: skipped, never interpreted (see the module comment). The
  // local widening is deliberate — this console's own type only knows today's
  // kinds, while the guard exists for a NEWER kernel's payload.
  const kind: string = widget.kind;
  if (kind !== "jumpButtons") return null;
  const links = (widget.buttons ?? []).flatMap((button) => {
    const href = jumpButtonHref(button);
    return href == null ? [] : [{ button, href }];
  });
  if (links.length === 0) return null;

  const body = (
    <div className={cn(FLEX, "flex-wrap gap-1.5")}>
      {links.map(({ button, href }, index) => {
        const Icon = pluginNavIcon(button.icon ?? TARGET_ICONS[button.target]);
        const label =
          button.label ?? (button.target === "notice" ? t("jumpNotice") : t("jumpTask"));
        return (
          <Link
            key={`${index}:${button.target}`}
            href={href}
            className="inline-flex items-center gap-1.5 rounded border border-border bg-sidebar-accent/40 px-2 py-1 text-[11px] text-foreground hover:bg-sidebar-accent"
          >
            <Icon className="size-3 shrink-0 text-muted-foreground" />
            <span>{label}</span>
            {button.task_id != null && (
              <span className="font-mono text-[10px] text-muted-foreground">#{button.task_id}</span>
            )}
          </Link>
        );
      })}
    </div>
  );

  if (widget.title == null) return body;
  // A titled widget gets the same header chrome as built-in sections; the
  // puzzle icon marks it as plugin-contributed (the vocabulary's own
  // "unmapped plugin surface" glyph).
  return (
    <Section icon={<Puzzle className="size-3" />} title={widget.title}>
      {body}
    </Section>
  );
}
