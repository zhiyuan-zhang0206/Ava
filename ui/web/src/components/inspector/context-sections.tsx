"use client";

// The system-prompt section tree of a context breakdown: a recursive list of collapsible rows.

import { ChevronRight } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { formatTokens } from "@/lib/format/format-number";
import { FLEX, FLEX_1, FLEX_COL, MIN_W_0 } from "@/lib/layout/layout";
import type { ContextSection } from "@/lib/contracts/types";
import { cn } from "@/lib/format/utils";

/** One level of the section tree. `depth` drives the indent (inline padding, so
 * no dynamically-composed Tailwind class); the top level carries the testid the
 * panel test keys on. */
export function SectionRows({ nodes, depth }: { nodes: ContextSection[]; depth: number }) {
  return (
    <ul
      className={cn("gap-0.5", FLEX, FLEX_COL)}
      data-testid={depth === 0 ? "context-breakdown-sections" : undefined}
    >
      {nodes.map((node, i) => (
        <SectionRow key={`${node.name}-${i}`} node={node} depth={depth} />
      ))}
    </ul>
  );
}

/** A single section row. A node with `children` gets a disclosure toggle; a
 * leaf gets none — its label starts at the same left edge as sibling chevrons
 * (no spacer). The chevron sits flush against the label (no gap). `min-w-0` +
 * `truncate` keep even deep indentation from forcing horizontal scroll. */
function SectionRow({ node, depth }: { node: ContextSection; depth: number }) {
  const t = useTranslations("contextBreakdown");
  const [open, setOpen] = useState(false);
  const children = node.children ?? [];
  const hasChildren = children.length > 0;
  return (
    <li>
      <div className={cn("items-center text-xs", FLEX)} style={{ paddingLeft: depth * 12 }}>
        {hasChildren ? (
          <button
            type="button"
            onClick={() => setOpen((o) => !o)}
            aria-expanded={open}
            aria-label={
              open ? t("collapse", { target: node.name }) : t("expand", { target: node.name })
            }
            className="shrink-0 rounded-sm text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          >
            <ChevronRight className={cn("size-3 transition-transform", open && "rotate-90")} />
          </button>
        ) : null}
        <span className={cn("truncate text-muted-foreground", MIN_W_0, FLEX_1)}>{node.name}</span>
        <span className="ml-1 shrink-0 tabular-nums text-muted-foreground">
          {formatTokens(node.tokens)}
          {node.estimated ? ` ${t("estimatedSuffix")}` : ""}
        </span>
      </div>
      {open && hasChildren ? <SectionRows nodes={children} depth={depth + 1} /> : null}
    </li>
  );
}
