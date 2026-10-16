import type { CompactTransitionBuffer } from "@/lib/layout/compact-transition";
import type { BackendTimelineItem } from "@/lib/contracts/types";
import type { SavedScroll } from "@/lib/layout/scroll-memory";

import { materializeBeforeAnchor } from "./materialize-prefix";
import { resolveSavedTimelineAnchor } from "./use-timeline-window";

// ava:ref ui/web/src/docs/frontend-components/timeline-view/continuous-segments.ava.okf.md#restore-settles-its-anchor
export function settleSavedTimelineScroll(
  viewport: HTMLElement,
  saved: SavedScroll,
  compactBuffer: CompactTransitionBuffer | null,
  canonicalItems: readonly BackendTimelineItem[],
): boolean {
  if (saved.followBottom || !saved.anchor) return true;
  const target = resolveSavedTimelineAnchor(viewport, saved.anchor, compactBuffer, canonicalItems);
  if (!target.node) return !target.present;
  const offset = target.node.getBoundingClientRect().top - viewport.getBoundingClientRect().top;
  const delta = offset - target.viewportTop;
  if (Math.abs(delta) <= 1) return true;
  materializeBeforeAnchor(viewport, target.node);
  viewport.scrollTop = Math.max(0, viewport.scrollTop + delta);
  return false;
}
